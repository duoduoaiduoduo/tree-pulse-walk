#!/usr/bin/env python3
"""Synthesize a tree-pulse timing audition from shot.json. Requires NumPy and SciPy."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import wave

import numpy as np
from scipy import signal


RATE = 48000


def number(v, name):
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
        raise ValueError(f"{name} needs a finite number")
    return float(v)


def root_x(keys, t):
    if keys is None:
        return .5
    if not isinstance(keys, list) or not keys:
        raise ValueError("root needs at least one keyframe")
    times, positions = [], []
    for key in keys:
        points = key["points"]
        if not isinstance(points, list) or len(points) != 1 or len(points[0]) != 2:
            raise ValueError("each root keyframe needs points: [[x,y]]")
        k = number(key["t"], "root.t")
        x, y = [number(v, "root coordinate") for v in points[0]]
        if not (0 <= x <= 1 and 0 <= y <= 1 and k >= 0 and (not times or k > times[-1])):
            raise ValueError("root coordinates need [0,1] and key times must increase")
        times.append(k)
        positions.append(x)
    return np.interp(t, times, positions)


def noise(rng, n, low, high):
    waveform = rng.standard_normal(n)
    sos = signal.butter(2, [low, high], btype="bandpass", fs=RATE, output="sos")
    filtered = signal.sosfilt(sos, waveform)
    return filtered / max(1.e-6, np.sqrt(np.mean(filtered ** 2)))


def deposit(dst, index, src):
    count = min(len(dst) - index, len(src))
    if count > 0:
        dst[index:index + count] += src[:count]


def pan(mono, x):
    # Keep the low body and melody readable on a narrow phone speaker.
    angle = (.20 + .60 * x) * np.pi / 2
    return np.column_stack((mono * np.cos(angle), mono * np.sin(angle)))


def headphone_pan(mono, root, direction, sweep=False):
    """Pan upper detail without removing anything from the fold-down or delaying a hit."""
    anchor = .64 * (2*np.asarray(root) - 1)
    position = anchor
    seconds = len(mono) / RATE
    if sweep and seconds > .11:
        u = np.linspace(0., 1., len(mono))
        swings = np.clip(seconds/.18, .70, 5.5)
        travel = swings * np.pi * (.84*u + .16*u*u)
        # Use the ends of the gesture to return to its tree. Short gestures travel
        # part of one arc; longer gestures cross the path repeatedly.
        blend = np.minimum(np.clip(u/.09, 0, 1), np.clip((1.-u)/.19, 0, 1))
        blend *= blend*(3.-2.*blend) * np.clip((seconds-.11)/.19, 0, 1)
        position = (1.-blend)*anchor + blend*.81*direction*np.sin(travel)
    # Low frequencies are shared. Equal and opposite upper-band gain deviations sum
    # to zero, retaining a stable mono waveform even on wide sweeps.
    upper = signal.sosfilt(signal.butter(3, 390, btype="highpass", fs=RATE, output="sos"), mono)
    side = upper * position
    return np.column_stack((mono-side, mono+side))/np.sqrt(2.)


def ticks(rng, seconds):
    """Fiber catches increase in density and migrate into the trunk's lower register."""
    n = max(1, round(seconds * RATE))
    line = np.zeros(n)
    pos = 0.
    while pos < seconds:
        u = pos / seconds
        length = round(RATE * rng.uniform(.010, .038))
        t = np.arange(length) / RATE
        decay = np.exp(-t / (.005 + .009 * (1 - u)))
        rise = 1 - np.exp(-t / .0015)
        grain = noise(rng, length, 460 - 260*u, 3600 - 1500*u)
        deposit(line, round(pos * RATE), grain * decay * rise * (.012 + .032*u))
        pos += (.027 + .139 * (1 - u)**1.6) * rng.uniform(.77, 1.28)
    t = np.arange(n) / RATE
    drift = np.interp(t, np.linspace(0, seconds, 13), rng.uniform(.55, 1., 13))
    line += noise(rng, n, 190, 1320) * (.003 + .025*(t/seconds)**1.25) * drift
    # A little space precedes impact. On very short descents scale the gap to the action.
    gap = min(.013, seconds*.14)
    return line * np.clip((seconds-gap-t) / min(.026, seconds*.25), 0, 1) * np.clip(t/.013, 0, 1)


def contact(rng, final):
    t = np.arange(round(RATE*(.39 if final else .32))) / RATE
    crack = noise(rng, len(t), 730, 4690) * (1-np.exp(-t/.0008)) * np.exp(-t/.014)
    soil = noise(rng, len(t), 69, 565) * (1-np.exp(-t/.004)) * np.exp(-t/.052)
    body = np.zeros_like(t)
    for hz, weight in [(87,.31), (164,.21), (257,.15), (390,.07)]:
        f = hz*rng.uniform(.87, 1.12)
        body += weight*np.sin(2*np.pi*f*t) * np.exp(-t/rng.uniform(.024,.078))
    body *= (1-np.exp(-t/.003))
    return .28*crack + .31*soil + body*(1.16 if final else 1.)


def grit(rng, final):
    length = 1.17 if final else rng.uniform(.40,.65)
    out = np.zeros(round(length*RATE))
    for u in np.sort(rng.beta(1.15,2.3,21 if final else 12)):
        t = np.arange(round(RATE*rng.uniform(.009,.029))) / RATE
        grain = noise(rng, len(t), 900, rng.uniform(2700,5200))
        envelope = (1-np.exp(-t/.0015)) * np.exp(-t/.005)
        deposit(out, round(u*len(out)), grain*envelope*rng.uniform(.025,.064)*(1-u)**1.45)
    return out


def render(events, duration, reflection, headphones=False):
    n = round(duration*RATE)
    if n < 2 or n*4 + 64 >= 2**32:
        raise ValueError("choose a positive duration that fits a stereo PCM WAV (RIFF)")
    if not isinstance(events, list) or not events:
        raise ValueError("shot.json needs a nonempty events array")
    dry = np.zeros((n, 2))
    final_send = np.zeros((n, 2))
    last = max(number(e["hit"], "hit") for e in events)
    for i, e in enumerate(events):
        start, hit = number(e["start"], "start"), number(e["hit"], "hit")
        first, impact = round(start*RATE), round(hit*RATE)
        if not 0 <= start < hit < duration or not 0 <= first < impact < n:
            raise ValueError(f"event {i+1} needs 0 <= start < hit < duration with distinct sample positions")
        x = root_x(e.get("root"), hit)
        seed = hashlib.sha256(f'{i}/{e.get("id", "")}/{hit:.10f}'.encode()).digest()[:8]
        rng = np.random.default_rng(int.from_bytes(seed, "little"))
        last_pair = abs(last-hit)<.0005
        fiber = ticks(rng, (impact-first)/RATE)
        moving = root_x(e.get("root"), (first+np.arange(len(fiber)))/RATE)
        source = contact(rng,last_pair)
        sand_at = impact + round(rng.uniform(.042,.064)*RATE)
        sand = grit(rng,last_pair)
        if headphones:
            # Adjacent passes may approach from opposite sides even within one shot.
            direction = (1 if x >= .5 else -1) * (-1 if i % 2 else 1)
            branches = headphone_pan(fiber, moving, direction, sweep=True)
            strike = headphone_pan(source, x, direction)
            falling = headphone_pan(sand, x, -direction, sweep=True)
        else:
            branches = pan(fiber, moving)
            strike = pan(source, x)
            falling = pan(sand, x*.87+.065)
        deposit(dry, first, branches)
        deposit(dry, impact, strike)
        deposit(dry, sand_at, falling)
        if last_pair:
            deposit(final_send, impact, strike)
    mixed = dry.copy()
    # The same short exterior response receives wood, soil and grit.
    for dest in range(2):
        own = dry[:,dest]
        other = dry[:,1-dest]
        for delay, gain in [(.023,.135),(.047,.090),(.078,.064),(.126,.040)]:
            k = round((delay+.0035*dest)*RATE)
            if k<n:
                deposit(mixed[:,dest],k,(.75*own[:n-k]+.25*other[:n-k])*gain*reflection)
        # Last impact excites a quiet, diffuse return. It contains no additional strike.
        tail_rng = np.random.default_rng(7219 + dest)
        t = np.arange(round(1.25*RATE))/RATE
        ir = noise(tail_rng,len(t),260,2600) * np.clip((t-.055)/.064,0,1) * np.exp(-6.91*np.maximum(t-.08,0)/1.14)
        ir /= np.sqrt(np.sum(ir**2))
        origin = round(last*RATE)-round(.001*RATE)
        origin = max(0,origin)
        feed = final_send[origin:min(n,round((last+.42)*RATE)),dest]
        if reflection and len(feed):
            deposit(mixed[:,dest],origin,signal.fftconvolve(feed,ir)*.13*reflection)
    # Preserve event-to-event dynamics; reduce only peaks that would exceed -2 dBFS.
    mixed *= min(1.,10**(-2/20)/max(np.max(np.abs(mixed)),1.e-9))
    end_count = min(round(.030*RATE), max(1,n-1-round(last*RATE)))
    mixed[-end_count:] *= np.linspace(1,0,end_count)[:,None] if end_count>1 else 0
    return mixed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("shot", type=Path)
    ap.add_argument("output", type=Path)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--duration", type=float, help="seconds, from first displayed picture frame")
    src.add_argument("--survey", type=Path, help="shot-info.json from survey.py, field duration_s")
    ap.add_argument("--reflection", type=float, default=.8, help="0..1, strength of the shared street returns")
    ap.add_argument("--headphones", action="store_true", help="sweep fibers and falling grit, anchor impact, center bass")
    ap.add_argument("--mono", action="store_true", help="write a mono fold-down for a phone check")
    args = ap.parse_args()
    try:
        plan = json.loads(args.shot.read_text(encoding="utf-8-sig"))
        duration = args.duration
        if args.survey:
            duration = json.loads(args.survey.read_text(encoding="utf-8-sig"))["duration_s"]
        duration = number(duration,"duration")
        if args.output.resolve() in {p.resolve() for p in [args.shot,args.survey] if p}:
            raise ValueError("output must differ from input")
        if not 0<=args.reflection<=1:
            raise ValueError("--reflection needs a value from 0 to 1")
        mix = render(plan["events"],duration,args.reflection,args.headphones)
        if args.mono:
            mix = mix.mean(axis=1,keepdims=True)
        pcm = np.rint(mix*32767).astype("<i2")
        with tempfile.NamedTemporaryFile(dir=args.output.parent,suffix=".wav",delete=False) as temp:
            partial=Path(temp.name)
        try:
            with wave.open(str(partial),"wb") as wav:
                wav.setparams((pcm.shape[1],2,RATE,0,"NONE","not compressed"))
                wav.writeframes(pcm.tobytes())
            os.replace(partial,args.output)
        finally:
            if partial.exists():
                partial.unlink()
        print(f"{len(pcm)} samples at {RATE} Hz, {pcm.shape[1]} channel(s), peak {20*np.log10(np.max(abs(mix))+1.e-12):.1f} dBFS")
        if duration-max(e["hit"] for e in plan["events"])<.28:
            print("The final hit is close to the cut. Check its release when you audition the proof.")
    except (ValueError,KeyError,TypeError,OSError) as exc:
        ap.exit(2,f"Could not render foley: {exc}\n")

if __name__=="__main__":
    main()

#!/usr/bin/env python3
"""Render a muted SDR timing, tracking and floor-occlusion proof from annotated footage."""
import argparse
import bisect
import json
import math
import os
import tempfile
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

# survey.py prints normalized PTS with nine fractional decimal places.
CLOCK_EPS = 1e-9


def number(value, where):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{where}: expected finite number")
    return float(value)


class Track:
    def __init__(self, data, minimum, where):
        if not isinstance(data, list) or not data:
            raise ValueError(f"{where}: expected keyframes")
        self.times = []
        self.points = []
        for i, key in enumerate(data):
            t = number(key["t"], where)
            pts = np.array(key["points"], dtype=np.float32)
            if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < minimum:
                raise ValueError(f"{where}[{i}]: wrong point count or shape")
            if minimum == 1 and len(pts) != 1:
                raise ValueError(f"{where}[{i}]: a root needs exactly one point")
            if not np.all(np.isfinite(pts)) or float(pts.min()) < 0 or float(pts.max()) > 1:
                raise ValueError(f"{where}[{i}]: coordinates must be within [0,1]")
            if t < 0 or (i and (t <= self.times[-1] or pts.shape != self.points[-1].shape)):
                raise ValueError(f"{where}[{i}]: require increasing times and matching vertices")
            self.times.append(t)
            self.points.append(pts)

    def at(self, t):
        i = bisect.bisect_right(self.times, t)
        if i == 0:
            return self.points[0]
        if i == len(self.times):
            return self.points[-1]
        a = (t - self.times[i-1]) / (self.times[i] - self.times[i-1])
        return self.points[i-1] * (1-a) + self.points[i] * a


def tracks(config, key, minimum):
    return [Track(v, minimum, f"{key}[{i}]") for i, v in enumerate(config.get(key, []))]


class GroundPlane:
    """A metric plane established by four fixed road marks seen by a moving camera."""

    def __init__(self, item):
        self.quad = Track(item["quad"], 4, "plane.quad")
        self.width, self.depth = (number(x, "plane.size") for x in item["size"])
        if min(self.width, self.depth) <= 0:
            raise ValueError("plane.size needs positive physical width and depth")
        for t in self.quad.times:
            self.matrix(t)

    def matrix(self, t):
        image = self.quad.at(t)
        if len(image) != 4:
            raise ValueError("plane.quad needs four physical marks")
        # Image axes are right and down. Near-left -> near-right -> far-right ->
        # far-left runs counterclockwise numerically for this forward-facing shot.
        area = cv2.contourArea(image, oriented=True)
        ordered = (image[0,0] < image[1,0] and image[3,0] < image[2,0]
                   and image[:2,1].mean() > image[2:,1].mean())
        if not ordered or area >= -.0001 or not cv2.isContourConvex(image):
            raise ValueError(f"Invalid plane quad at {t:.3f}s: track near-L, near-R, far-R, far-L")
        unit = np.array([[0,0],[1,0],[1,1],[0,1]], np.float32)
        h = cv2.getPerspectiveTransform(image, unit)
        condition = np.linalg.cond(h)
        if not np.isfinite(condition) or condition > 1.e6:
            raise ValueError(f"Ill-conditioned ground transform at {t:.3f}s")
        # Fix sign and scale by the reference road, so denominators <= 0 are
        # on/behind its horizon. Check all four corners stay on the ground side.
        denominators = image @ h[2,:2] + h[2,2]
        center = denominators.mean()
        if abs(center) < 1.e-10 or np.min(denominators / center) < 1.e-4:
            raise ValueError(f"Reference quad touches the horizon at {t:.3f}s")
        return h / center

    def anchor(self, xy, t):
        h = self.matrix(t)
        pos = h @ np.r_[xy, 1.]
        if pos[2] <= 1.e-4:
            raise ValueError(f"Ground contact at {t:.3f}s is on/behind the horizon")
        return pos[0]/pos[2]*self.width, pos[1]/pos[2]*self.depth

    def grid(self, t, x, y, visible_floor):
        h = self.matrix(t)
        w = h[2,0]*x+h[2,1]*y+h[2,2]
        valid = (w > 1.e-4) & (visible_floor > 0)
        coords=[]
        for row, scale in zip(h[:2], (self.width,self.depth)):
            numerator = (row[0]*x+row[1]*y+row[2])*scale
            coords.append(np.divide(numerator,w,out=np.zeros_like(w),where=valid))
        return coords[0],coords[1],valid


class Event:
    def __init__(self, item, plane):
        self.start, self.hit, self.spread = (
            number(item[k], k) for k in ("start", "hit", "spread"))
        radius_key = "world_radius" if plane else "radius"
        self.radius = number(item[radius_key], radius_key)
        if plane is None and "world_radius" in item:
            raise ValueError("world_radius requires plane.size and plane.quad")
        if not 0 <= self.start < self.hit or min(self.spread, self.radius) <= 0:
            raise ValueError("Need 0 <= start < hit; spread and radius must be positive")
        self.root = Track(item["root"], 1, "root")
        self.world_root = plane.anchor(self.root.at(self.hit)[0],self.hit) if plane else None
        self.paths = tracks(item, "paths", 2)
        if not self.paths:
            raise ValueError("Each event needs a crown-to-root path")
        self.occluders = tracks(item, "occluders", 3)
        self.ratio = number(item.get("depth_ratio", .34), "depth_ratio")
        if not 0 < self.ratio <= 1:
            raise ValueError("depth_ratio should be in (0,1]")


def ease(x):
    q = np.clip(x, 0, 1)
    return q*q*(3-2*q)


class Scene:
    def __init__(self, config):
        self.ground = Track(config["ground"], 3, "ground")
        self.front = tracks(config, "occluders", 3)
        self.ground_holdouts = tracks(config, "ground_occluders", 3)
        self.plane = GroundPlane(config["plane"]) if "plane" in config else None
        self.events = [Event(item, self.plane) for item in config["events"]]
        if not self.events or any(a.hit > b.hit for a,b in zip(self.events, self.events[1:])):
            raise ValueError("List the events chronologically; simultaneous hits are allowed")
        self.finish = config.get("finish")
        if self.finish is not None:
            a,b = (number(x, "finish") for x in self.finish)
            if a + CLOCK_EPS < max(e.hit + e.spread for e in self.events) or a >= b:
                raise ValueError("Start whole-scene finish after the last ground sweep has settled")
            self.finish = a,b
        self.cues = sorted({q for e in self.events for q in (e.start,e.hit,e.hit+e.spread)}
                           | set(self.finish or ()))
        for e in self.events:
            root = tuple(map(float, e.root.at(e.hit)[0]))
            if cv2.pointPolygonTest(self.ground.at(e.hit), root, True) < -1.e-6:
                raise ValueError(f"Root at {e.hit:.3f}s falls outside the receiving ground")

    def set_size(self, width, height):
        self.size = np.array([width-1, height-1], np.float32)
        self.shape = height,width
        self.xx = np.arange(width, dtype=np.float32)[None,:]
        self.yy = np.arange(height, dtype=np.float32)[:,None]
        self.nx = self.xx / (width-1)
        self.ny = self.yy / (height-1)
        self.stroke_scale = min(width,height) / 480

    def coverage(self, tracks_, t):
        plane = np.zeros(self.shape, np.uint8)
        for track in tracks_:
            shape = np.rint(track.at(t)*self.size*16).astype(np.int32)
            cv2.fillPoly(plane, [shape], 255, lineType=cv2.LINE_AA, shift=4)
        return plane.astype(np.float32)/255

    def ribbon(self, track, t, phase):
        pts = track.at(t)*self.size
        distances = np.sqrt(np.sum(np.diff(pts, axis=0)**2, axis=1))
        stops = np.r_[0, np.cumsum(distances)]
        mask = np.zeros(self.shape, np.uint8)
        if stops[-1] < 1:
            return mask.astype(np.float32)
        stops /= stops[-1]
        for k, length in enumerate(distances):
            steps = max(1, math.ceil(length/max(2,5*self.stroke_scale)))
            for j in range(steps):
                pos = stops[k]+(j+.5)/steps*(stops[k+1]-stops[k])
                strength = math.exp(-.5*((pos-phase)/.095)**2)
                if strength < .007:
                    continue
                p,q = pts[k:k+2]
                a = np.rint((p+(q-p)*j/steps)*16).astype(int)
                b = np.rint((p+(q-p)*(j+1)/steps)*16).astype(int)
                cv2.line(mask, tuple(a), tuple(b), round(strength*255),
                         max(1, round((4+4*pos)*self.stroke_scale)), cv2.LINE_AA, shift=4)
        return mask.astype(np.float32)/255

    def composite(self, bgr, t):
        # Treat nine-decimal annotations of an exact source PTS as that same
        # frame, without moving a genuine between-frame cue to an earlier image.
        index = bisect.bisect_left(self.cues,t)
        for cue in self.cues[max(0,index-1):index+1]:
            if abs(cue-t) <= CLOCK_EPS:
                t=cue
                break
        src = bgr.astype(np.float32)/255
        gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
        blur = lambda m,s: cv2.GaussianBlur(m,(0,0),max(.32, s*self.stroke_scale))
        local = blur(gray,5.5)
        bark = ease((gray-local-.002)/.072)*ease((gray-.035)/.18)
        visible = 1-self.coverage(self.front,t)
        floor = self.coverage([self.ground],t)*(1-self.coverage(self.ground_holdouts,t))*visible
        ground_world = None
        if self.plane and t >= self.events[0].hit:
            world_x,world_y,valid = self.plane.grid(t,self.nx,self.ny,floor)
            floor *= valid
            ground_world = (world_x,world_y)
        deposit = np.zeros_like(gray)
        live = np.zeros_like(gray)
        grit = np.zeros_like(gray)
        for e in self.events:
            if t < e.start:continue
            clear = 1-self.coverage(e.occluders,t)
            if t < e.hit+.07:
                phase = min(1, (t-e.start)/(e.hit-e.start))**2.35
                drive = ease((t-e.start)/min(.085,(e.hit-e.start)*.25))*(1-ease((t-e.hit)/.07))
                strand = np.maximum.reduce([self.ribbon(p,t,phase) for p in e.paths])
                core = strand*bark*drive
                light = .92*core+.37*blur(core,2.6)+.19*blur(core,8.)
                live = np.maximum(live, np.minimum(light,1)*clear*visible)
            if t < e.hit:continue
            if ground_world is None:
                rx,ry = e.root.at(t)[0]*self.size
                radius=e.radius*self.shape[1]
                distance=np.sqrt((self.xx-rx)**2+((self.yy-ry)/e.ratio)**2)/radius
            else:
                rx,ry=e.world_root
                world_x,world_y=ground_world
                distance=np.hypot(world_x-rx,world_y-ry)/e.radius
            progress=min(1,max(0.,t-e.hit)/e.spread)**.72
            # Small terrain-dependent timing variation on original photographed detail.
            distance *= (1+.036*np.tanh((gray-local)*8))
            coverage=ease((progress-distance)/.095+.48)*floor*clear
            deposit=np.maximum(deposit,coverage)
            if progress<1:
                edge=np.exp(-.5*((distance-progress)/.044)**2)*floor*clear
                grit=np.maximum(grit,edge*(.08+.88*bark)*(1-ease((progress-.82)/.18)))
        dark = gray*(.22+.22*ease(gray/.75))
        base = .90*dark[:,:,None]+.10*src*.41
        open_ = np.clip(np.maximum(deposit,live),0,1)
        if self.finish:
            a,b=self.finish
            phase = (t-a)/(b-a)
            height_above_bottom = 1-self.yy/max(1,self.shape[0]-1)
            open_ += (1-open_)*ease((phase-.32*height_above_bottom)/.68)
        result = base + (src-base)*open_[:,:,None]
        # Hue is sampled from the recorded branch/ground. No free-floating stroke is drawn.
        tint = (src+.035)/(src.max(axis=2,keepdims=True)+.035)
        amount = (.49*live+.25*grit)[:,:,None]
        result += (1-result)*tint*(1-np.exp(-amount))
        return np.uint8(np.rint(np.clip(result,0,1)*255))


def render(source, spec, output, width):
    if output.suffix.lower() != ".mp4" or any(output.resolve()==p.resolve() for p in (source,spec)):
        raise ValueError("Choose a separate output .mp4 file")
    if width < 32:
        raise ValueError("--width must be at least 32")
    scene = Scene(json.loads(spec.read_text(encoding="utf-8")))
    output.parent.mkdir(parents=True, exist_ok=True)
    handle,name = tempfile.mkstemp(dir=output.parent, suffix=".mp4")
    os.close(handle)
    temp = Path(name)
    try:
        with av.open(source) as inp, av.open(temp, "w", options={"movflags":"+faststart"}) as out:
            stream=inp.streams.video[0]
            w=max(2,min(width,stream.width)//2*2)
            h=max(2,round(w*stream.height/stream.width/2)*2)
            scene.set_size(w,h)
            rate=stream.average_rate or stream.guessed_rate or Fraction(30,1)
            tb=stream.time_base
            target=out.add_stream("libx264", rate=rate)
            target.width,target.height=w,h
            target.pix_fmt="yuv420p"
            target.time_base=target.codec_context.time_base=tb
            target.codec_context.color_range=target.codec_context.colorspace=1
            target.codec_context.color_trc=target.codec_context.color_primaries=1
            target.options={"preset":"veryfast","crf":"19","bf":"0"}
            source_frames=iter(inp.decode(stream))
            frame=next(source_frames,None)
            if frame is None or frame.pts is None:raise ValueError("Source needs timestamped video frames")
            origin=frame.pts
            durations={}
            count=0

            def mux(packets):
                for packet in packets:
                    pts=round(packet.pts*packet.time_base/tb)
                    duration=durations.pop(pts)
                    packet.duration=round(duration*tb/packet.time_base)
                    out.mux(packet)

            while frame is not None:
                if frame.pts is None or frame.time_base!=tb:
                    raise ValueError("Source has missing or inconsistent PTS")
                pts=frame.pts-origin
                t=float(pts*tb)
                following=next(source_frames,None)
                if following is None:
                    # MP4 B-frame decoding may attach the previous coded sample duration
                    # to the last displayed frame. Prefer the container's presentation end.
                    duration=getattr(frame, "duration", 0) or max(1,round(1/rate/tb))
                    if stream.duration is not None:
                        first_pts=stream.start_time if stream.start_time is not None else origin
                        remaining=first_pts+stream.duration-frame.pts
                        if remaining>0:duration=remaining
                elif following.pts is None or following.pts<=frame.pts:
                    raise ValueError("Non-increasing presentation timestamps")
                else:
                    duration=following.pts-frame.pts
                im=frame.reformat(width=w,height=h,format="bgr24",src_colorspace="ITU709").to_ndarray()
                converted=av.VideoFrame.from_ndarray(scene.composite(im,t),format="bgr24")
                yuv=converted.reformat(format="yuv420p",dst_colorspace="ITU709")
                yuv.pts,yuv.time_base=pts,tb
                if hasattr(yuv, "duration"):
                    yuv.duration=duration
                # Earlier PyAV releases expose some color properties only on the
                # encoder. Its four Rec.709 fields are already set above.
                for prop in ("color_range", "colorspace", "color_trc", "color_primaries"):
                    if hasattr(yuv, prop):
                        setattr(yuv, prop, 1)
                durations[pts]=duration
                mux(target.encode(yuv))
                frame=following
                count+=1
            mux(target.encode())
            end=float((pts+duration)*tb)
            if scene.events[-1].hit>t+CLOCK_EPS or scene.finish and scene.finish[1]>end+CLOCK_EPS:
                raise ValueError(f"Timeline ends at {end:.3f}s; move hit/finish inside the clip")
            if durations:raise RuntimeError("Encoder omitted one or more frames")
        os.replace(temp,output)
        print(f"{output}: {count} frames, {w}x{h}, {end:.3f}s, silent H.264")
    finally:
        temp.unlink(missing_ok=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source",type=Path)
    ap.add_argument("shot",type=Path)
    ap.add_argument("output",type=Path)
    ap.add_argument("--width",type=int,default=720,help="maximum output width in pixels (default 720)")
    args=ap.parse_args()
    try:render(args.source,args.shot,args.output,args.width)
    except (ValueError,KeyError,OSError,IndexError,TypeError) as exc:ap.exit(2,f"preview: {exc}\n")


if __name__=="__main__":
    main()

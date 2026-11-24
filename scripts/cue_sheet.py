#!/usr/bin/env python3
"""由树脉预演的事件 JSON 生成按触地先后排列的音画卡点 CSV，仅需 Python 3。"""
import argparse
import csv
import json
import sys
from bisect import bisect_left
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


def nonnegative(value, name):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} 需要以秒为单位的数值") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} 需要有限的非负秒数")
    return number


def event_time(event, name, required=False):
    if name not in event and not required:
        return None
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{name} 应使用 JSON 数字")
    return nonnegative(value, name)


def read_frames(path):
    times = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"index", "time_s"}.issubset(reader.fieldnames or []):
            raise ValueError("帧表需含 index,time_s，使用 survey.py 输出的完整 frames.csv")
        for row in reader:
            if row["index"] is None or int(row["index"]) != len(times):
                raise ValueError("帧表 index 应从 0 按显示顺序连续递增")
            t = nonnegative(row["time_s"], "time_s")
            if (not times and t != 0) or (times and t <= times[-1]):
                raise ValueError("帧表 time_s 需要从 0 开始并严格递增")
            times.append(t)
    if not times:
        raise ValueError("帧表没有数据行")
    return times


def frame_cues(hit, times):
    if not times:
        return [""] * 7
    after = bisect_left(times, hit)
    candidates = [i for i in (after - 1, after) if 0 <= i < len(times)]
    index = min(candidates, key=lambda i: abs(times[i] - hit))
    nearest = [index, format(times[index], "f"), milliseconds(times[index] - hit)]
    # Match preview.py clock tolerance. Survey serializes PTS with nine decimal
    # places, so a repeating rational and its serialized form are the same cue.
    first = bisect_left(times, hit - Decimal(".000000001"))
    if first == len(times):
        return [*nearest, "", "", "", "该时刻在末帧起点之后，本段没有新的显示帧"]
    lag = max(Decimal(0), times[first] - hit)
    return [*nearest, first, format(times[first], "f"), milliseconds(lag), ""]


def milliseconds(delta):
    return format((delta * 1000).quantize(Decimal(".001"), rounding=ROUND_HALF_UP), "f")


def make_rows(plan, times, rate):
    if not isinstance(plan, dict) or not isinstance(plan.get("events"), list) or not plan["events"]:
        raise ValueError("JSON 需要非空的 events 数组")
    rows = []
    for sequence, event in enumerate(plan["events"], start=1):
        if not isinstance(event, dict):
            raise ValueError(f"事件 {sequence} 应为 JSON 对象")
        label = event.get("id", f"树 {sequence}")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"事件 {sequence} 的 id 需要非空文字")
        hit = event_time(event, "hit", required=True)
        start = event_time(event, "start")
        if start is not None and start > hit:
            raise ValueError(f"{label} 下行起点应在触地之前或同时")
        audio = event.get("audio", {})
        if not isinstance(audio, dict):
            raise ValueError(f"{label} 的 audio 需要是 JSON 对象")
        sample = int((hit * rate).to_integral_value(rounding=ROUND_HALF_UP))
        row = [label, format(start, "f") if start is not None else "", format(hit, "f"), sample]
        for field in ("foley_onset", "music_onset"):
            t = event_time(audio, field)
            row.extend([format(t, "f"), milliseconds(t - hit)] if t is not None else ["", ""])
        row.extend(frame_cues(hit, times))
        rows.append((hit, row))
    return [row for _, row in sorted(rows, key=lambda pair: pair[0])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="包含 events 的 JSON")
    parser.add_argument("-o", "--output", type=Path, help="CSV 路径，省略则写标准输出")
    parser.add_argument("--frames", type=Path, help="可选，逐帧实际 PTS，列为 index,time_s")
    parser.add_argument("--sample-rate", type=int, default=48000, help="音频工程采样率，默认 48000")
    args = parser.parse_args()
    if args.sample_rate <= 0:
        parser.error("采样率应为正整数")
    inputs = [p.resolve() for p in (args.plan, args.frames) if p]
    if args.output and args.output.resolve() in inputs:
        parser.error("输出 CSV 需要与输入文件使用不同路径")

    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        times = read_frames(args.frames) if args.frames else []
        rows = make_rows(plan, times, args.sample_rate)
        columns = ["事件", "下行开始秒", "画面触地秒", f"触地采样点@{args.sample_rate}Hz",
                   "拟音音头秒", "拟音偏差毫秒", "音乐音头秒", "音乐偏差毫秒",
                   "最近显示帧索引", "该帧起始秒", "该帧相对触地毫秒",
                   "首个可触地显示帧索引", "首个可触地帧起始秒", "画面可见延迟毫秒", "帧表备注"]
        if args.output:
            with args.output.open("w", encoding="utf-8-sig", newline="") as stream:
                csv.writer(stream).writerows([columns, *rows])
        else:
            csv.writer(sys.stdout, lineterminator="\n").writerows([columns, *rows])
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(2, f"生成失败：{exc}\n")


if __name__ == "__main__":
    main()

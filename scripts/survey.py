"""Make an indexed contact sheet and a presentation-time table for a walking shot."""
import argparse
import bisect
import csv
import json
import math
import statistics
import tempfile
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageDraw


def scan(source):
    stamps, frame_durations = [], []
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError("No video stream")
        stream = container.streams.video[0]
        codec = stream.codec_context
        container_end = None
        if stream.start_time is not None and stream.duration is not None:
            container_end = (stream.start_time + stream.duration) * stream.time_base
        info = {"source": source.name, "coded_size": [codec.width, codec.height],
                "codec": codec.name, "pixel_format": codec.format.name if codec.format else None,
                "stream_time_base": str(stream.time_base), "sample_aspect_ratio": str(stream.sample_aspect_ratio or 1),
                "audio_streams": len(container.streams.audio),
                "colour_tags": {k: getattr(codec, k, None) for k in
                               ("color_range", "color_primaries", "color_trc", "colorspace")},
                "creation_time_tag": container.metadata.get("creation_time"),
                "sheet_coordinate_space": "coded raster, origin top-left, before display rotation"}
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise ValueError("Frame has no presentation timestamp; conform the shot before annotating.")
            ts = frame.pts * frame.time_base
            if stamps and ts <= stamps[-1][0]:
                raise ValueError("Displayed timestamps are not strictly increasing; inspect or conform the shot.")
            if not stamps:
                info["rotation_degrees"] = getattr(frame, "rotation", None)
                info["display_matrix_side_data"] = any("DISPLAYMATRIX" in str(getattr(item, "type", item)).upper() for item in frame.side_data)
            stamps.append((ts, frame.pts, frame.time_base))
            frame_durations.append((getattr(frame, "duration", 0) or 0) * frame.time_base)
        if not stamps:
            raise ValueError("No decoded frames")
        diffs = [b[0]-a[0] for a,b in zip(stamps, stamps[1:])]
        last = frame_durations[-1]
        end_basis = "final frame duration"
        if container_end is not None and container_end > stamps[-1][0]:
            # The decoder can attach a previous coded sample's duration to the last B-frame.
            last = container_end - stamps[-1][0]
            end_basis = "stream presentation endpoint"
        elif last <= 0:
            last = statistics.median(diffs[-12:]) if diffs else Fraction(0)
            end_basis = "estimated from preceding intervals" if diffs else "unknown"
    durations = diffs+[last]
    origin = stamps[0][0]
    times = [float(s[0]-origin) for s in stamps]
    info.update(first_video_pts=stamps[0][1], first_video_time_base=str(stamps[0][2]),
                frames=len(stamps), duration_s=float(stamps[-1][0]-origin+last),
                interval_range_ms=[round(float(min(diffs))*1000,3),round(float(max(diffs))*1000,3)] if diffs else None,
                end_basis=end_basis)
    return stamps, times, durations, info


def nearest(times, t):
    j = bisect.bisect_left(times, t)
    if j == 0 or j == len(times):
        return min(j, len(times)-1)
    return j-1 if t-times[j-1] <= times[j]-t else j


def sheet(source, times, count):
    n = min(len(times), count)
    indices = sorted({nearest(times, times[-1]*i/max(n-1,1)) for i in range(n)})
    slots = {index: j for j,index in enumerate(indices)}
    width, height, label = 294, 208, 21
    columns = min(4, len(slots))
    canvas = Image.new("RGB", (columns*width, math.ceil(len(slots)/columns)*(height+label)), "#161819")
    draw = ImageDraw.Draw(canvas)
    with av.open(str(source)) as c:
        for i, frame in enumerate(c.decode(c.streams.video[0])):
            if i not in slots:
                continue
            factor = min((width-4)/frame.width,(height-4)/frame.height)
            w,h = max(1,round(frame.width*factor)),max(1,round(frame.height*factor))
            im = frame.reformat(width=w,height=h,format="rgb24").to_image()
            col,row = slots[i]%columns, slots[i]//columns
            x, y = col*width,row*(height+label)
            canvas.paste(im,(x+(width-w)//2,y+(height-h)//2))
            draw.text((x+7,y+height+2), f"{times[i]:.3f} s   frame {i}", fill="white")
    return canvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("output_dir",type=Path)
    parser.add_argument("--count",type=int,default=12,help="Number of contact-sheet samples, 2 to 36")
    args = parser.parse_args()
    if not 2 <= args.count <= 36:
        parser.error("--count must be between 2 and 36")
    stamps,times,durations,info = scan(args.source)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output_dir) as folder:
        temp=Path(folder)
        with (temp/"frames.csv").open("w",encoding="utf-8",newline="") as f:
            writer=csv.writer(f)
            writer.writerow(("index","time_s","pts","time_base","duration_s"))
            for i,((_,pts,tb),t,d) in enumerate(zip(stamps,times,durations)):
                writer.writerow((i,f"{t:.9f}",pts,str(tb),f"{float(d):.9f}"))
        (temp/"shot-info.json").write_text(json.dumps(info,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        sheet(args.source,times,args.count).save(temp/"contact.jpg",quality=93)
        for name in ["contact.jpg","frames.csv","shot-info.json"]:
            (temp/name).replace(args.output_dir/name)
    print(f"{info['frames']} frames, {info['duration_s']:.3f} seconds -> {args.output_dir}")


if __name__=="__main__":
    main()

---
name: tree-pulse-walk
description: 用在实拍树荫步道、街边行道树或穿林而行的移动镜头上，沿原有树皮亮纹制作向根脚加速的光脉冲，让可见地面恢复色彩，并按树的位置生成同步拟音预览。
---

# 树脉 · 林荫步道

深圳的夜路上，路灯在每棵树上留下了一条不一样的亮纹。Gemosdodo 用这些纹理做了一部近 17 秒的短片。这个公开版本把其中可以迁移到新镜头的步骤交给 AI。输入一段能看到树皮与地面的实拍，输出带光流、地面回色及拟音的预览，并保留每次击地的帧号和音效时刻。

## 先做一个能检验的落点

1. 按实际拍摄方向与色彩空间读片。把素材转为方形像素、方向正确的 SDR 工作代理。Log 素材先正常还原。使用稳像时，在稳像完成的同一份代理上进行后面的跟踪、标注与渲染。
2. 取首帧、接近树根和经过树身时的画面，挑一棵可以从分叉连续看到根脚的树。镜头最靠近的树和画面另一侧的树都要观察。根脚始终出画时，在可见枝干上结束脉冲，或换一棵落点清楚的树。
3. 用归一化坐标给分叉、树皮亮纹、根脚和地面边缘标点，分别圈出挡住它们的前景。把选中根脚的真实接触帧时间写入 `hit`。路径靠后几段让光加速，先沿木纹流动，触地后再让地面的颜色从根脚向外恢复。
4. 检查移动镜头里已经亮过的地砖是否固定在原处。近树从地面前经过时，树身应继续遮住它后方的地波。标注格式、前景遮挡与四点地面配准见[预演指南](references/preview.md)。
5. 一次冲击通过后，再处理后续树木。每个镜头重新观察枝干、根脚与空间尺度。前几次地面响应清楚可辨，再逐次扩大，留完整环境回色作为片尾。

## 运行

依赖为 Python 3.10 以上、PyAV、NumPy、OpenCV、Pillow 和 SciPy。画面工具使用支持 H.264 的 FFmpeg 库，合并视听轨道时需可调用 FFmpeg。

```sh
python -m pip install "av>=16.1" numpy opencv-python-headless Pillow scipy
python scripts/survey.py plate.mp4 survey --count 12
```

从 `survey/contact.jpg` 选树，在 `survey/frames.csv` 找触地帧。复制[屏幕空间示例](assets/shot.example.json)或[地面透视示例](assets/shot-plane.example.json)为 `shot.json`，按新镜头替换坐标、事件与时间。两个示例都使用三秒时间轴。短片更短时，同步缩短所有事件及收尾时段。使用同一段输入和同一份标注运行下列命令。

```sh
python scripts/preview.py plate.mp4 shot.json picture.mp4 --width 720
python scripts/foley.py shot.json foley.wav --survey survey/shot-info.json
python scripts/cue_sheet.py shot.json --frames survey/frames.csv -o cues.csv
ffmpeg -n -i picture.mp4 -i foley.wav -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -movflags +faststart tree-pulse-preview.mp4
```

`picture.mp4` 是画面预演，`foley.wav` 是 48 kHz 原创合成拟音，`tree-pulse-preview.mp4` 是合并后的带声预览。示范短片另外做了逐镜精调、环境反光、粒子、字幕和音乐混音。四个工具可以各自独立执行。

## 音乐与音效

让拟音保持稳定的实景位置。左树在左，偏右的树偏右，落砂跟着相应的根脚。两棵树同时被击中时，用两个事件分别标根。用上述默认拟音命令生成示例，`--mono` 可输出手机单声道复听版。深圳成片里，配乐旋律由右耳逐渐经过中央，再移至左耳，低音保持稳定；只有音乐分轨安排这种往返。配乐由使用者另行提供、核实授权并在剪辑软件中混合。音乐、私人原片与第三方采样均不随包分发。

## 完成检查

静音逐帧确认光流先到根脚，地面后回色。闭眼试听同一处的木裂、土层低响和稍后落下的细砂，再用手机连播。对易漂移的近树，至少复查枝杈、触地帧、落地后地砖三个位置。交付预览时，附上最值得修改的那一帧及对应时刻。

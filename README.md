# 树脉 · Tree Pulse

在一段实拍林荫路上，让光沿着树皮亮纹加速落到根脚。撞击发生时，脚边的草地恢复颜色，撞击声也落在那棵树所在的方向。Gemosdodo 的深圳短片《树脉》使用了这套方法。

[观看短片与下载](https://gemosdodo.art/works/tree-pulse/) · [下载 v1.0.0 公开包](https://github.com/duoduoaiduoduo/tree-pulse-walk/releases/download/v1.0.0/tree-pulse-walk.zip)

## 用自己的视频试做

1. 从 Releases 下载 ZIP，解压至单独的文件夹。将这个文件夹与实拍素材交给支持本地脚本的 AI 助手。支持自定义技能目录的客户端也可以把该文件夹作为一个 Skill 安装，入口为根目录下的 [SKILL.md](SKILL.md)。
2. 选一棵从分叉到根脚都看得清的树。移动镜头先完成稳像，随后在同一份素材上标出树皮亮纹、触地点和遮挡。包里两份 JSON 是标注示例，每个新镜头都需要重新取点。
3. 可以这样开始：

   > 用树脉处理这段林荫步道。先沿第一棵树真实的树纹做一次向下脉冲，光击中根脚时让脚边的地面恢复颜色，给我一段带空间拟音的预览。

## 本地运行

需要 Python 3.10 或更高版本、支持 H.264 的 FFmpeg，以及 PyAV、NumPy、OpenCV、Pillow 和 SciPy。完整运行顺序和合并音视频的命令见 [SKILL.md](SKILL.md)、[预演指南](references/preview.md)。

公开版包括画面抽样、光流预演、方位拟音和触地时间表四个工具。视频与配乐由使用者自行提供。随包生成的音效来自本地合成。

作者：[Gemosdodo](https://gemosdodo.art/)。

# Ego 视频切分工具

人工可视化切分多路相机 session：播放 `left.mp4` 标注起止，导出完整多模态切片到当前 session 下的 `divide/`。原 videos / timestamps / imu / audio / calibrations 只读保留。

## 环境与依赖（一键）

需要：

- Python 3.10+（脚本默认建 3.11）
- `ffmpeg` / `ffprobe`（切视频、探测时长）
- Python 包见 [`requirements.txt`](requirements.txt)：`streamlit`、`opencv-python-headless`、`numpy`

在项目目录执行一条命令即可：有 conda 就建名为 `video_cutter` 的环境，没有 conda 就在本目录建 `.venv`，并安装 Python 依赖；缺 ffmpeg 时会尝试 `apt-get install`。

```bash
cd /video_cutter
bash setup.sh
```

换环境名或 Python 版本：

```bash
ENV_NAME=video_cutter PYTHON_VERSION=3.11 bash setup.sh
```

### 以后每次启动

conda：

```bash
conda activate video_cutter
cd /path/to/video_cutter
streamlit run app.py
```

venv：

```bash
cd /path/to/video_cutter
source .venv/bin/activate
streamlit run app.py
```

浏览器打开终端里给出的地址（一般是 `http://localhost:8501`）。侧边栏可改数据根，默认：

`/media/adminpc1/34C618D6C6189A66/头环/baai_ego_task`

切片写到所选 `session_*/divide/`。

### 手动安装（不跑 setup.sh 时）

系统 ffmpeg：

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

conda：

```bash
conda create -y -n video_cutter python=3.11 pip
conda activate video_cutter
pip install -r requirements.txt
```

venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 功能

- 切分时长校验 `<= 10` 分钟
- 中文动作名命名输出目录（`session_时间_动作_导出时间戳`，单下划线）
- 未导出的标注自动写到 `divide/draft_segments.json`，下次打开同一 session 会恢复
- 全程好数据、不需要切片时，点「整段合格，无需切分」：只写 `divide/session_review.json`，不复制视频；侧边栏可按「整段合格」筛选，并从「未处理」中移出
- 侧边栏按「未处理 / 有草稿 / 已切分 / 整段合格」筛选，列表里能看出哪些已经处理过
- 界面顶部与右侧表单展示纳入标准：双手可见、动作语义完整，两项都达标才能标「好」
- 同时切分：4 路视频、timestamps、IMU、audio，并拷贝 calibrations
- 每段 `cut_info.json`，以及 `divide/logs/cut_history.jsonl`
- 同一 session 可标注并导出多段

## 导出编码（跨机器）

顺序始终是：

1. **stream copy**（不重编码，只按时间裁；和 GPU 无关）
2. 本机 ffmpeg 里**实际编进构建、且能跑通**的硬件 H.264：NVIDIA `h264_nvenc` → Intel `h264_qsv` → AMD `h264_amf`
3. 都没有或失败 → **CPU `libx264 -preset ultrafast`**

别人电脑没有 4070 / 没有 NVIDIA / ffmpeg 没编 NVENC，都会自动落到第 3 步，功能不变，只是重编码更慢。多数切片会停在第 1 步。

需要强制某条路径时：

```bash
export VIDEO_CUTTER_ENCODER=libx264    # 只用 CPU
export VIDEO_CUTTER_ENCODER=h264_nvenc # 只试 NVIDIA，失败再 CPU
```

## 挽救截断视频（moov atom not found）

采集写满约 5GB 时，mp4 往往只剩画面数据、没有片尾索引，播放器和切分工具都会打不开。**时间戳 / IMU / 音频一般是完整的**，不必整段丢掉。

不改原文件，把 4 路重封到 `session_*/videos_recovered/`：

```bash
cd /path/to/video_cutter
python recover_truncated_mp4.py --session \
  /media/adminpc1/34C618D6C6189A66/头环/baai_ego_task/cs_0001/20260820/session_20260819_162034
```

每路大约再占 5GB 磁盘。完成后刷新 Session 列表，工具会优先播 `videos_recovered/`。末尾可能缺最后一小段（文件被 5GB 卡断处）。

剩余未修复的 session 已写在 `remaining_sessions.txt`。有空再跑：

```bash
cd /path/to/video_cutter
python3 recover_remaining.py --list          # 只看还剩哪些
bash run_recover_remaining.sh                # 后台跑，可断点续跑
tail -f recover_remaining.log
# 停止：kill $(cat recover_remaining.pid)
```

## 命令行冒烟（无 UI）

```bash
cd /path/to/video_cutter
python smoke_test.py
```

## 输出结构

```
session_xxx/
  videos/ timestamps/ imu/ audio/ calibrations/   # 原数据不动
  divide/
    good/
      session_xxx_拿杯子_20260827155901/
        videos/{left,right,bright,bleft}.mp4
        timestamps/*_timestamps.txt
        imu/imu0.csv
        audio/audio.wav
        calibrations/
        meta.json
        cut_info.json
    bad/
      ...
    logs/
      cut_history.jsonl
    draft_segments.json            # 未导出草稿，重开自动恢复
```

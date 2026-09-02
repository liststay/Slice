# Ego 视频切分工具

人工可视化切分多路相机 session：播放 `left.mp4` 标注起止，按帧导出 left / right 切片到当前 session 下的 `divide/`。原 videos / timestamps / imu / audio / calibrations 只读保留。

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
- 全程好数据或不达标、都不需要切片时，点「整段合格，无需切分」或「整段不合格」：只写 `divide/session_review.json`，不复制视频；侧边栏可按对应项筛选，并从「未处理」中移出
- 侧边栏按「未处理 / 有草稿 / 已切分 / 整段合格 / 整段不合格」筛选，列表里能看出做到哪里
- 编辑草稿片段时，播放器跳到该段起点
- 界面顶部与右侧表单展示纳入标准：双手可见、动作语义完整，两项都达标才能标「好」
- 同时切分：left / right 两路视频、timestamps，并整份拷贝 IMU 与 calibrations（不处理 bright / bleft / audio；IMU 不按时间裁切）
- 视频按帧号区间切分（精确 seek + `-frames:v` 卡死帧数），timestamps 保留源文件中的绝对时间戳
- left / right 按绝对时间对齐：切后两路首帧、尾帧时间戳相同（原始数据可能差一帧）
- 每段 `cut_info.json`，以及 `divide/logs/cut_history.jsonl`
- 同一 session 可标注并导出多段

## 导出编码（跨机器）

顺序始终是卡死输出帧数（与标注帧区间一致）：

1. **stream copy**（仅当输出帧数正好等于目标才保留）
2. 精确 seek 后硬件 H.264：NVIDIA `h264_nvenc` → Intel `h264_qsv` → AMD `h264_amf`，并用 `-frames:v` 卡死帧数
3. 都没有或失败 → **CPU `libx264 -preset ultrafast`**，同样卡死帧数


需要强制某条路径时：

```bash
export VIDEO_CUTTER_ENCODER=libx264    # 只用 CPU
export VIDEO_CUTTER_ENCODER=h264_nvenc # 只试 NVIDIA，失败再 CPU
```

## 挽救截断视频（moov atom not found）

采集写满约 5GB 时，mp4 往往只剩画面数据、没有片尾索引，播放器和切分工具都会打不开。

不改原始 `videos/*.mp4`。会：

1. 把 4 路重封到 `session_*/videos_recovered/`
2. 把原始 `meta.json` 和 `timestamps/`（以及将改写的 `imu/`、`audio/`）备份到 `session_*/recovery_backup/`
3. 按恢复后的帧数裁切 `timestamps` / `imu` / `audio`，并对 left/right 等相机做首尾帧对齐（丢掉对不上的更早首帧、更晚尾帧）；`videos_recovered` 会裁成与对齐后的 timestamps 一一对应
4. 修正 `meta.json` 里的 `duration_sec` / `synced_frames` 等时间字段

```bash
cd /path/to/video_cutter
python recover_truncated_mp4.py --session \
  /media/adminpc1/34C618D6C6189A66/头环/baai_ego_task/cs_0001/20260820/session_20260819_162034
```

视频已经重封、只需补裁 sidecar 时：

```bash
python recover_truncated_mp4.py --session /path/to/session_xxx --sidecars-only
```

每路视频大约再占 5GB 磁盘。完成后刷新 Session 列表，工具会优先播 `videos_recovered/`。末尾可能缺最后一小段（文件被 5GB 卡断处）。

剩余未修复的 session 已写在 `remaining_sessions.txt`。有空再跑：

```bash
cd /path/to/video_cutter
python3 recover_remaining.py --list          # 只看还剩哪些
bash run_recover_remaining.sh                # 后台跑，可断点续跑
tail -f recover_remaining.log
# 停止：kill $(cat recover_remaining.pid)
```

## 已切分数据：timestamps 对齐 + meta 时间修正

NAS / `divide/` 里旧切片的 `meta.json` 常仍带着整段 session 的 `span_sec`。可对切分结果单独跑（不改原始 session）：

```bash
cd /path/to/video_cutter
python align_cut_exports.py --root /mnt/nas/synnas/ego/baai_ego_task --dry-run
python align_cut_exports.py --root /mnt/nas/synnas/ego/baai_ego_task
python align_cut_exports.py --cut /path/to/divide/good/session_xxx_动作_时间
```

会原地对齐 left/right 首尾时间戳，并改写 `duration_sec` / `span_sec` / `synced_frames`（不备份 `meta.json` 和 `timestamps/`）。

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
        videos/{left,right}.mp4
        timestamps/{left,right}_timestamps.txt
        imu/imu0.csv
        calibrations/
        meta.json
        cut_info.json
    bad/
      ...
    logs/
      cut_history.jsonl
    draft_segments.json            # 未导出草稿，重开自动恢复
```

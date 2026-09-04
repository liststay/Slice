# Ego 视频切分工具

人工可视化切分多路相机 session：并排播放 `left.mp4` / `right.mp4` 同步标注起止，按帧导出 left / right 切片到当前 session 下的 `divide/`。原 videos / timestamps / imu / audio / calibrations 只读保留。

## 环境与依赖

需要：

- Python 3.10+
- `ffmpeg` / `ffprobe`（在 PATH 里）
- Python 包见 [`requirements.txt`](requirements.txt)

Linux / macOS：

```bash
cd /path/to/video_cutter
bash setup.sh
```

Windows（cmd）：

```bat
cd C:\path\to\video_cutter
setup.bat
```

任意系统也都可以：

```bash
python setup_env.py
```

有 conda 会建名为 `video_cutter` 的环境；没有则在本目录建 `.venv`。Linux 上若缺 ffmpeg，会尝试 `apt-get install`。

安装 Python 包默认走**清华 PyPI 镜像**。换源或改回官方：

```bash
VIDEO_CUTTER_PIP_MIRROR=aliyun python setup_env.py   # 阿里云
VIDEO_CUTTER_PIP_MIRROR=ustc python setup_env.py     # 中科大
VIDEO_CUTTER_PIP_MIRROR=official python setup_env.py # 官方 pypi.org
```

换环境名或强制用 venv：

```bash
ENV_NAME=video_cutter PYTHON_VERSION=3.11 python setup_env.py
VIDEO_CUTTER_VENV=1 python setup_env.py
```

### 以后每次启动

conda：

```bash
conda activate video_cutter
cd /path/to/video_cutter
streamlit run app.py
```

venv（Linux / macOS）：

```bash
cd /path/to/video_cutter
source .venv/bin/activate
streamlit run app.py
```

venv（Windows）：

```bat
cd C:\path\to\video_cutter
.venv\Scripts\activate
streamlit run app.py
```

浏览器打开终端里给出的地址（一般是 `http://localhost:8501`）。侧边栏可改数据根；也可设环境变量 `VIDEO_CUTTER_DATA_ROOT`。默认会在本机已有的 `baai_ego_task` 目录里挑一个（含常见 Linux 挂载盘和 `D:\baai_ego_task` 等）。

每个 session 第一次打开会在后台生成 `divide/.preview/` 约 720p 预览（不改原视频、不挡界面）。之后播放、跳转走预览；**导出仍切原片**，时间轴一致。

切片写到所选 `session_*/divide/`。

Windows 额外注意：把 ffmpeg 的 `bin` 加到系统 PATH 后**新开一个终端**再启动。本机防火墙若拦截 `8501` / `18765` 端口，需允许 Python。

### 手动安装（不跑 setup 时）

系统 ffmpeg：

- Ubuntu/Debian：`sudo apt-get update && sudo apt-get install -y ffmpeg`
- Windows：`winget install Gyan.FFmpeg` 或从 https://www.gyan.dev/ffmpeg/builds/ 安装
- macOS：`brew install ffmpeg`
- 或：`conda install -y -c conda-forge ffmpeg`

conda：

```bash
conda create -y -n video_cutter python=3.11 pip
conda activate video_cutter
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

venv：

```bash
python -m venv .venv
```

Linux/macOS：`source .venv/bin/activate`  
Windows：`.venv\Scripts\activate`

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

## 功能

- 切分时长校验 `<= 10` 分钟
- 中文动作名命名输出目录（`session_时间_动作_导出时间戳`，单下划线）
- 未导出的标注自动写到 `divide/draft_segments.json`，下次打开同一 session 会恢复
- 全程好数据或不达标、都不需要切片时，点「整段合格，无需切分」或「整段不合格」：只写 `divide/session_review.json`，不复制视频；侧边栏可按对应项筛选，并从「未处理」中移出
- 侧边栏按「未处理 / 有草稿 / 已切分 / 整段合格 / 整段不合格」筛选，列表里能看出做到哪里
- 编辑草稿片段时，播放器跳到该段起点
- 界面顶部与右侧表单展示纳入标准：双手可见、动作语义完整，两项都达标才能标「好」
- 播放器并排显示 left / right，同步播放、暂停和跳转；标注时可勾选「遮挡 / 模糊」
- 同时切分：left / right 两路视频、timestamps、IMU（按对齐后的绝对时间窗裁切），并原样拷贝 `meta.json` 与 calibrations（不处理 bright / bleft / audio）
- 视频按帧号区间切分（精确 seek + `-frames:v` 卡死帧数），timestamps 保留源文件中的绝对时间戳
- left / right 按绝对时间对齐：切后两路首帧、尾帧时间戳相同（原始数据可能差一帧）
- 每段 `cut_info.json`，以及 `divide/logs/cut_history.jsonl`
- 同一 session 可标注并导出多段

## 导出编码（跨机器）

顺序始终是卡死输出帧数（与标注帧区间、timestamps 行数一致）：

1. **stream copy**（仅当输出帧数正好等于目标才保留；GOP 与原片相同）
2. 精确 seek 后硬件 H.264：NVIDIA `h264_nvenc` → Intel `h264_qsv` → AMD `h264_amf`，并用 `-frames:v` 卡死帧数
3. 都没有或失败 → **CPU `libx264 -preset ultrafast`**，同样卡死帧数

原片 GOP=30（约 1s @ 30fps）、无 B 帧。copy 保持原 GOP；任何重编码都显式 `-g 30 -bf 0`（libx264 另加 `-keyint_min 30 -sc_threshold 0`），不用 ffmpeg/NVENC 默认 GOP=250。

中间文件写在目标盘同目录的 `*.partial.mp4`，写完再改名为最终 mp4（Linux / Windows 都这样），避免先写系统盘临时目录再整段拷贝。


需要强制某条路径时：

```bash
export VIDEO_CUTTER_ENCODER=libx264    # 只用 CPU
export VIDEO_CUTTER_ENCODER=h264_nvenc # 只试 NVIDIA，失败再 CPU
```

Windows cmd：`set VIDEO_CUTTER_ENCODER=libx264`

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

视频已经重封、只需补裁 timestamps / IMU / meta 时（视频能播但时间戳不对，或 `--root` 因已标记对齐而跳过）：

```bash
python recover_truncated_mp4.py --session /path/to/session_xxx --sidecars-only
```

`--root` 现在也会把「已有 `videos_recovered/` 但时间戳帧数/首尾仍对不齐」的 session 捡回来，不只看 `meta.json` 里的对齐标记。

每路视频大约再占 5GB 磁盘。完成后刷新 Session 列表，工具会优先播 `videos_recovered/`。末尾可能缺最后一小段（文件被 5GB 卡断处）。

剩余未修复的 session 已写在 `remaining_sessions.txt`。有空再跑：

```bash
cd /path/to/video_cutter
python3 recover_remaining.py --list          # 只看还剩哪些
bash run_recover_remaining.sh                # 后台跑，可断点续跑
tail -f recover_remaining.log
# 停止：kill $(cat recover_remaining.pid)
```

## 已切分数据：timestamps 对齐

可对 `divide/` 旧切片单独跑（不改原始 session，也不改切片里的 `meta.json`）：

```bash
cd /path/to/video_cutter
python align_cut_exports.py --root /mnt/nas/synnas/ego/baai_ego_task --dry-run
python align_cut_exports.py --root /mnt/nas/synnas/ego/baai_ego_task
python align_cut_exports.py --cut /path/to/divide/good/session_xxx_动作_时间
```

会原地对齐 left/right 首尾时间戳，按同一时间窗裁切 `imu/`，并更新 `cut_info.json`（不备份）。

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

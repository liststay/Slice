#!/usr/bin/env bash
# One-shot: create Python env and install video_cutter dependencies.
set -euo pipefail
cd "$(dirname "$0")"
ENV_NAME="${ENV_NAME:-video_cutter}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

need_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1)"
    return 0
  fi
  echo "未检测到 ffmpeg / ffprobe。"
  if command -v apt-get >/dev/null 2>&1; then
    echo "尝试安装系统依赖：ffmpeg"
    sudo apt-get update -y
    sudo apt-get install -y ffmpeg
  else
    echo "请先自行安装 ffmpeg，然后再跑本脚本。"
    echo "  Ubuntu/Debian:  sudo apt-get install -y ffmpeg"
    echo "  conda:          conda install -y -c conda-forge ffmpeg"
    exit 1
  fi
}

need_ffmpeg

if command -v conda >/dev/null 2>&1; then
  echo "使用 conda 创建环境：${ENV_NAME} (Python ${PYTHON_VERSION})"
  # shellcheck disable=SC1091
  source "$(conda info --base)/etc/profile.d/conda.sh"
  if conda env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
    echo "环境已存在，跳过 create。"
  else
    conda create -y -n "${ENV_NAME}" "python=${PYTHON_VERSION}" pip
  fi
  conda activate "${ENV_NAME}"
  python -m pip install -U pip
  python -m pip install -r requirements.txt
  echo
  echo "完成。以后每次使用："
  echo "  conda activate ${ENV_NAME}"
  echo "  cd $(pwd)"
  echo "  streamlit run app.py"
else
  echo "未找到 conda，改用 venv：$(pwd)/.venv"
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -U pip
  python -m pip install -r requirements.txt
  echo
  echo "完成。以后每次使用："
  echo "  source $(pwd)/.venv/bin/activate"
  echo "  cd $(pwd)"
  echo "  streamlit run app.py"
fi

python -c "import streamlit,cv2,numpy; print('python ok', streamlit.__version__)"
echo "setup ok"

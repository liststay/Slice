"""Create a Python env and install video_cutter deps. Works on Windows / macOS / Linux."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ENV_NAME = os.environ.get("ENV_NAME", "video_cutter")
PYTHON_VERSION = os.environ.get("PYTHON_VERSION", "3.11")

# Default: Tsinghua PyPI. Override with VIDEO_CUTTER_PIP_MIRROR=aliyun|ustc|official
# or VIDEO_CUTTER_PIP_INDEX=https://...
_PIP_MIRRORS = {
    "tuna": ("https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    "tsinghua": ("https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    "aliyun": ("https://mirrors.aliyun.com/pypi/simple/", "mirrors.aliyun.com"),
    "ustc": ("https://pypi.mirrors.ustc.edu.cn/simple/", "pypi.mirrors.ustc.edu.cn"),
    "douban": ("https://pypi.douban.com/simple/", "pypi.douban.com"),
}


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd)


def _ffmpeg_ok() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _pip_index() -> tuple[str, str] | None:
    """Return (index_url, trusted_host) or None for official PyPI."""
    custom = os.environ.get("VIDEO_CUTTER_PIP_INDEX", "").strip()
    if custom:
        from urllib.parse import urlparse

        host = urlparse(custom).hostname or ""
        return custom, host
    name = os.environ.get("VIDEO_CUTTER_PIP_MIRROR", "tuna").strip().lower()
    if name in ("official", "pypi", "none", "0", "off"):
        return None
    return _PIP_MIRRORS.get(name, _PIP_MIRRORS["tuna"])


def _pip_args() -> list[str]:
    idx = _pip_index()
    if not idx:
        return []
    url, host = idx
    args = ["-i", url]
    if host:
        args += ["--trusted-host", host]
    return args


def _write_venv_pip_conf(venv: Path) -> None:
    idx = _pip_index()
    if not idx:
        return
    url, host = idx
    lines = ["[global]", f"index-url = {url}"]
    if host:
        lines.append(f"trusted-host = {host}")
    conf = venv / ("pip.ini" if sys.platform == "win32" else "pip.conf")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pip_install(python: str, *pkgs: str) -> None:
    extra = _pip_args()
    if extra:
        print(f"pip 镜像: {extra[1]}")
    _run([python, "-m", "pip", "install", "-U", "pip", *extra])
    _run([python, "-m", "pip", "install", *extra, *pkgs])


def _ffmpeg_hint() -> None:
    print("未检测到 ffmpeg / ffprobe，请先安装并加入 PATH。")
    if sys.platform == "win32":
        print("  winget install Gyan.FFmpeg")
        print("  或 https://www.gyan.dev/ffmpeg/builds/ 解压后把 bin 目录加到 PATH")
        print("  或 conda install -y -c conda-forge ffmpeg")
    elif sys.platform == "darwin":
        print("  brew install ffmpeg")
        print("  或 conda install -y -c conda-forge ffmpeg")
    else:
        print("  sudo apt-get install -y ffmpeg")
        print("  或 conda install -y -c conda-forge ffmpeg")


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _activate_hint(venv: Path) -> str:
    if sys.platform == "win32":
        return str(venv / "Scripts" / "activate")
    return f"source {venv / 'bin' / 'activate'}"


def _setup_venv() -> Path:
    venv = ROOT / ".venv"
    py = _venv_python(venv)
    if not py.is_file():
        print(f"创建 venv：{venv}")
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    _write_venv_pip_conf(venv)
    _pip_install(str(py), "-r", str(ROOT / "requirements.txt"))
    return py


def _conda() -> str | None:
    return shutil.which("conda")


def _setup_conda(conda: str) -> None:
    env = ENV_NAME
    listed = subprocess.check_output([conda, "env", "list"], text=True, errors="replace")
    names = {line.split()[0] for line in listed.splitlines() if line.strip() and not line.startswith("#")}
    if env not in names:
        _run([conda, "create", "-y", "-n", env, f"python={PYTHON_VERSION}", "pip"])
    else:
        print(f"conda 环境已存在：{env}")
    extra = _pip_args()
    if extra:
        print(f"pip 镜像: {extra[1]}")
    _run(
        [
            conda,
            "run",
            "-n",
            env,
            "python",
            "-m",
            "pip",
            "install",
            "-U",
            "pip",
            *extra,
        ]
    )
    _run(
        [
            conda,
            "run",
            "-n",
            env,
            "python",
            "-m",
            "pip",
            "install",
            *extra,
            "-r",
            str(ROOT / "requirements.txt"),
        ]
    )


def main() -> int:
    os.chdir(ROOT)
    if not _ffmpeg_ok():
        if sys.platform.startswith("linux") and shutil.which("apt-get"):
            print("尝试用 apt 安装 ffmpeg …")
            try:
                _run(["sudo", "apt-get", "update", "-y"])
                _run(["sudo", "apt-get", "install", "-y", "ffmpeg"])
            except (OSError, subprocess.CalledProcessError):
                _ffmpeg_hint()
                return 1
        if not _ffmpeg_ok():
            _ffmpeg_hint()
            return 1
    print("ffmpeg:", shutil.which("ffmpeg"))

    force_venv = os.environ.get("VIDEO_CUTTER_VENV", "").strip() in ("1", "true", "yes")
    conda = None if force_venv else _conda()
    if conda:
        print(f"使用 conda 环境：{ENV_NAME} (Python {PYTHON_VERSION})")
        _setup_conda(conda)
        print("完成。以后每次使用：")
        print(f"  conda activate {ENV_NAME}")
        print(f"  cd {ROOT}")
        print("  streamlit run app.py")
        checker = [conda, "run", "-n", ENV_NAME, "python", "-c", "import streamlit,cv2,numpy; print('python ok')"]
        _run(checker)
    else:
        py = _setup_venv()
        print("完成。以后每次使用：")
        print(f"  {_activate_hint(ROOT / '.venv')}")
        print(f"  cd {ROOT}")
        print("  streamlit run app.py")
        _run([str(py), "-c", "import streamlit,cv2,numpy; print('python ok')"])
    print("setup ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

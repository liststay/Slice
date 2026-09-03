"""OS-independent helpers: temp files, ffmpeg binaries, default data root."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import shutil
import subprocess
import sys
import tempfile

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_windows() -> bool:
    return sys.platform == "win32"


def _win_kwargs() -> dict[str, Any]:
    if is_windows():
        return {"creationflags": _NO_WINDOW}
    return {}


def which_bin(name: str) -> str:
    """Resolve ffmpeg/ffprobe on PATH, including Windows .exe."""
    found = shutil.which(name)
    if found:
        return found
    if is_windows() and not name.lower().endswith(".exe"):
        found = shutil.which(name + ".exe")
        if found:
            return found
    return name


def ffmpeg() -> str:
    return which_bin("ffmpeg")


def ffprobe() -> str:
    return which_bin("ffprobe")


def ffmpeg_head() -> list[str]:
    return [
        ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.update(_win_kwargs())
    return subprocess.run(cmd, **kwargs)


def popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    kwargs.update(_win_kwargs())
    return subprocess.Popen(cmd, **kwargs)


def check_output(cmd: list[str], **kwargs: Any) -> str:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.update(_win_kwargs())
    return subprocess.check_output(cmd, **kwargs)


def state_file(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


def default_data_root() -> str:
    env = os.environ.get("VIDEO_CUTTER_DATA_ROOT", "").strip()
    if env:
        return env
    home = Path.home()
    candidates = [
        Path("/media/adminpc1/新加卷K/baai_ego_task"),
        Path("/media/adminpc1/34C618D6C6189A66/头环/baai_ego_task"),
        home / "baai_ego_task",
        Path("D:/baai_ego_task"),
        Path("E:/baai_ego_task"),
        Path("F:/baai_ego_task"),
    ]
    for path in candidates:
        try:
            if path.is_dir():
                return str(path)
        except OSError:
            continue
    return str(home / "baai_ego_task")

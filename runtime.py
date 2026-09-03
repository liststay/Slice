"""OS-independent helpers: temp files, ffmpeg binaries, default data root."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import errno
import os
import shutil
import subprocess
import sys
import tempfile

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_BIN_CACHE: dict[str, str] = {}


def is_windows() -> bool:
    return sys.platform == "win32"


def _win_kwargs() -> dict[str, Any]:
    if is_windows():
        return {"creationflags": _NO_WINDOW}
    return {}


def _win_short_path(path: Path) -> str:
    try:
        import ctypes
    except ImportError:
        return ""
    GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
    buf = ctypes.create_unicode_buffer(32768)
    n = GetShortPathNameW(str(path), buf, len(buf))
    if n and buf.value:
        return buf.value.replace("\\", "/")
    return ""


def ffmpeg_arg(path: Path | str) -> str:
    """Path string ffmpeg/ffprobe accept on Windows (POSIX slashes, 8.3 if needed)."""
    p = Path(path)
    if not is_windows():
        return str(p)
    try:
        if p.exists():
            p = p.resolve()
    except OSError:
        pass
    text = str(p)
    if any(ord(c) > 127 for c in text):
        target = p if p.exists() else p.parent
        short = _win_short_path(target) if target.exists() else ""
        if short:
            if p.exists():
                return short
            return str(Path(short) / p.name).replace("\\", "/")
    return p.as_posix()


def is_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    win = getattr(exc, "winerror", None)
    if win in (5, 19, 21, 32, 33):
        return True
    err = getattr(exc, "errno", None)
    if err in (errno.EACCES, errno.EPERM, getattr(errno, "EROFS", 30)):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "access is denied",
            "permission denied",
            "read-only",
            "readonly",
            "只读",
            "拒绝访问",
        )
    )


def format_io_error(exc: BaseException, path: Path | str | None = None) -> str:
    loc = f"：`{path}`" if path else ""
    if is_permission_error(exc):
        return (
            f"没有文件夹读写权限{loc}。"
            "草稿和导出都要写入 session 下的 divide/，"
            "请给当前 Windows 用户加上该目录的「修改」权限，或换到可写的数据盘。"
            f"（{exc}）"
        )
    return f"写入失败{loc}：{exc}"


def ensure_writable_dir(path: Path | str) -> None:
    """Create dir if needed and verify we can write a file there."""
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".video_cutter_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise PermissionError(format_io_error(exc, target)) from exc


def dir_is_writable(path: Path | str) -> bool:
    try:
        ensure_writable_dir(path)
        return True
    except OSError:
        return False


def replace_file(src: Path | str, dst: Path | str) -> None:
    """Rename/move src onto dst. Falls back to copy on Windows/exFAT/SMB."""
    src_p = Path(src)
    dst_p = Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src_p, dst_p)
        return
    except OSError:
        pass
    shutil.copy2(src_p, dst_p)
    try:
        src_p.unlink()
    except OSError:
        pass


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text without depending on os.replace (fails on some Windows volumes)."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding=encoding)
        try:
            replace_file(tmp, path)
        except OSError:
            path.write_text(text, encoding=encoding)
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        raise PermissionError(format_io_error(exc, path)) from exc


def normalize_data_root(text: str) -> str:
    s = (text or "").strip().strip('"').strip("'")
    if not s:
        return s
    return str(Path(s).expanduser())


def which_bin(name: str) -> str:
    """Resolve ffmpeg/ffprobe on PATH, conda env, and common Windows install dirs."""
    key = name.lower()
    cached = _BIN_CACHE.get(key)
    if cached:
        return cached
    found = shutil.which(name)
    if not found and is_windows() and not name.lower().endswith(".exe"):
        found = shutil.which(name + ".exe")
    if not found:
        exe = name + ".exe" if is_windows() and not name.lower().endswith(".exe") else name
        extra: list[Path] = [
            Path(sys.prefix) / "Library" / "bin",
            Path(sys.prefix) / "bin",
            Path(sys.prefix) / "Scripts",
        ]
        conda = os.environ.get("CONDA_PREFIX", "").strip()
        if conda:
            extra += [Path(conda) / "Library" / "bin", Path(conda) / "bin"]
        if is_windows():
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            extra += [
                Path(r"C:\ffmpeg\bin"),
                Path(pf) / "ffmpeg" / "bin",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            ]
        for root in extra:
            cand = root / exe
            if cand.is_file():
                found = str(cand)
                break
    resolved = found or name
    _BIN_CACHE[key] = resolved
    return resolved


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


def _missing_bin_message(cmd: list[str]) -> str:
    prog = cmd[0] if cmd else "ffmpeg"
    if is_windows():
        return (
            f"找不到 {prog}（系统找不到指定的文件）。"
            "请安装 ffmpeg 并加入 PATH，然后新开终端再运行。"
            "可用 winget install Gyan.FFmpeg，或把 ffmpeg.exe 所在 bin 目录加到系统环境变量。"
        )
    return f"找不到 {prog}，请先安装 ffmpeg / ffprobe 并加入 PATH。"


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.update(_win_kwargs())
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise FileNotFoundError(_missing_bin_message(cmd)) from exc


def popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    kwargs.update(_win_kwargs())
    try:
        return subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise FileNotFoundError(_missing_bin_message(cmd)) from exc


def check_output(cmd: list[str], **kwargs: Any) -> str:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.update(_win_kwargs())
    try:
        return subprocess.check_output(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise FileNotFoundError(_missing_bin_message(cmd)) from exc


def state_file(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


def default_data_root() -> str:
    env = os.environ.get("VIDEO_CUTTER_DATA_ROOT", "").strip()
    if env:
        return normalize_data_root(env)
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

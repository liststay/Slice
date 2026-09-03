"""Low-bitrate playback proxies. Export still uses original videos."""

from __future__ import annotations

from pathlib import Path
import threading

from runtime import ffmpeg_arg, ffmpeg_head, replace_file, run as _run_proc

PREVIEW_SUBDIR = Path("divide") / ".preview"
_FAILED_HW: set[str] = set()
_JOBS: dict[str, threading.Thread] = {}
_ERRS: dict[str, str] = {}
_JOBS_LOCK = threading.Lock()


def preview_dir(session_path: Path) -> Path:
    return Path(session_path) / PREVIEW_SUBDIR


def preview_file(session_path: Path, camera: str) -> Path:
    return preview_dir(session_path) / f"{camera}.mp4"


def preview_rel(camera: str) -> str:
    return f"divide/.preview/{camera}.mp4"


def _src_mtime(src: Path) -> float:
    try:
        return src.stat().st_mtime
    except OSError:
        return 0.0


def preview_ready(session_path: Path, src: Path, camera: str) -> bool:
    if not src.is_file():
        return camera != "left"
    dst = preview_file(session_path, camera)
    try:
        if not dst.is_file() or dst.stat().st_size < 4096:
            return False
        return dst.stat().st_mtime + 1.0 >= _src_mtime(src)
    except OSError:
        return False


def session_previews_ready(session) -> bool:
    left = session.video_path("left")
    right = session.video_path("right")
    ok = preview_ready(session.path, left, "left")
    if right.is_file():
        ok = ok and preview_ready(session.path, right, "right")
    return ok


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _encode_one(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    work = dst.with_name(dst.stem + ".partial.mp4")
    _unlink(work)
    vf = "scale=720:-2:flags=fast_bilinear"
    attempts: list[tuple[str, list[str]]] = [
        (
            "h264_nvenc",
            ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "28", "-b:v", "0", "-pix_fmt", "yuv420p"],
        ),
        (
            "h264_qsv",
            ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "28"],
        ),
        (
            "libx264",
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", "0", "-pix_fmt", "yuv420p"],
        ),
    ]
    last_err = ""
    for name, extra in attempts:
        if name in _FAILED_HW:
            continue
        cmd = [
            *ffmpeg_head(),
            "-i",
            ffmpeg_arg(src),
            "-an",
            "-vf",
            vf,
            *extra,
            "-g",
            "30",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            ffmpeg_arg(work),
        ]
        proc = _run_proc(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            try:
                if work.is_file() and work.stat().st_size > 4096:
                    replace_file(work, dst)
                    return
            except OSError as exc:
                last_err = str(exc)
        else:
            last_err = (proc.stderr or "")[-800:]
            if name != "libx264":
                _FAILED_HW.add(name)
        _unlink(work)
    raise RuntimeError(f"预览生成失败 {src.name}: {last_err}")


def _session_key(session) -> str:
    return str(Path(session.path).resolve())


def preview_error(session) -> str:
    return _ERRS.get(_session_key(session), "")


def preview_busy(session) -> bool:
    t = _JOBS.get(_session_key(session))
    return t is not None and t.is_alive()


def start_previews_async(session) -> None:
    """Encode in a daemon thread so the Streamlit page can render immediately."""
    if session_previews_ready(session):
        return
    key = _session_key(session)
    with _JOBS_LOCK:
        t = _JOBS.get(key)
        if t is not None and t.is_alive():
            return
        _ERRS.pop(key, None)

        def _run() -> None:
            try:
                ensure_previews(session)
            except Exception as exc:
                _ERRS[key] = str(exc)

        th = threading.Thread(target=_run, daemon=True, name="ego-preview")
        _JOBS[key] = th
        th.start()


def ensure_previews(session) -> None:
    """Build left/right 720p proxies under session/divide/.preview/. Idempotent.

    Encode cameras one after another: USB/exFAT cannot sustain two full-size reads.
    """
    jobs: list[tuple[Path, Path]] = []
    for cam in ("left", "right"):
        src = session.video_path(cam)
        if not src.is_file():
            continue
        if preview_ready(session.path, src, cam):
            continue
        jobs.append((src, preview_file(session.path, cam)))
    if not jobs:
        return
    preview_dir(session.path).mkdir(parents=True, exist_ok=True)
    for src, dst in jobs:
        _encode_one(src, dst)

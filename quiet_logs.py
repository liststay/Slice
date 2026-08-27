"""Hide noisy ffmpeg / HTTP disconnect logs from the terminal."""

from __future__ import annotations

import os
import sys
import threading

_installed = False

_DROP_ONE_LINE = (
    "moov atom not found",
    "Connection reset by peer",
)

_SKIP_BLOCK_START = (
    "Exception occurred during processing of request from",
)


def silence_native_logs() -> None:
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")
    os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
    os.environ.setdefault("OPENCV_FFMPEG_DEBUG", "0")


def _keep_line(plain: str) -> bool:
    for token in _DROP_ONE_LINE:
        if token in plain:
            return False
    for token in _SKIP_BLOCK_START:
        if token in plain:
            return False
    return True


def _pump(src_fd: int, dst_fd: int) -> None:
    skip_tb = False
    held: bytes | None = None
    buf = b""
    try:
        while True:
            chunk = os.read(src_fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw + b"\n"
                text = raw.decode("utf-8", "replace").strip()
                if held is not None:
                    prev, held = held, None
                    if text.startswith("Exception occurred during processing of request"):
                        skip_tb = True
                        continue
                    os.write(dst_fd, prev)
                if text == "-" * 40:
                    if skip_tb:
                        skip_tb = False
                        continue
                    held = line
                    continue
                if skip_tb:
                    continue
                if not _keep_line(text):
                    if text.startswith("Exception occurred during processing of request"):
                        skip_tb = True
                    continue
                os.write(dst_fd, line)
        if held is not None:
            os.write(dst_fd, held)
        if buf:
            os.write(dst_fd, buf)
    except OSError:
        return
    finally:
        try:
            os.close(src_fd)
        except OSError:
            pass


def install() -> None:
    """Idempotent: filter C-level stderr (OpenCV/FFmpeg) and Python tracebacks."""
    global _installed
    if _installed:
        return
    _installed = True
    silence_native_logs()
    try:
        orig_fd = os.dup(2)
        r_fd, w_fd = os.pipe()
        os.dup2(w_fd, 2)
        os.close(w_fd)
        threading.Thread(
            target=_pump,
            args=(r_fd, orig_fd),
            daemon=True,
            name="stderr-filter",
        ).start()
    except OSError:
        return

    try:
        import cv2
        from cv2.utils import logging as cvlog

        cvlog.setLogLevel(cvlog.LOG_LEVEL_SILENT)
    except Exception:
        pass

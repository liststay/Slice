"""Scan and load ego-task session directories."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import bisect
import json
import os
import subprocess

os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2

try:
    from cv2.utils import logging as cvlog

    cvlog.setLogLevel(cvlog.LOG_LEVEL_SILENT)
except Exception:
    pass

from models import CAMERAS
from workstate import (
    count_draft_unexported,
    count_exported,
    is_keep_whole,
    is_reject_whole,
)


@dataclass
class SessionInfo:
    path: Path
    session_id: str
    meta: dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    fps: float = 30.0
    frame_count: int = 0
    cameras_present: list[str] = field(default_factory=list)
    exported_count: int = 0
    draft_count: int = 0
    keep_whole: bool = False
    reject_whole: bool = False

    @property
    def videos_dir(self) -> Path:
        return self.path / "videos"

    @property
    def timestamps_dir(self) -> Path:
        return self.path / "timestamps"

    @property
    def imu_csv(self) -> Path:
        return self.path / "imu" / "imu0.csv"

    @property
    def audio_wav(self) -> Path:
        return self.path / "audio" / "audio.wav"

    @property
    def calibrations_dir(self) -> Path:
        return self.path / "calibrations"

    def video_path(self, camera: str = "left") -> Path:
        recovered = self.path / "videos_recovered" / f"{camera}.mp4"
        if recovered.is_file():
            return recovered
        return self.videos_dir / f"{camera}.mp4"

    def timestamps_path(self, camera: str) -> Path:
        return self.timestamps_dir / f"{camera}_timestamps.txt"

    @property
    def divide_dir(self) -> Path:
        return self.path / "divide"


def _probe_video(path: Path) -> tuple[float, float, int]:
    """Return (fps, duration_sec, frame_count)."""
    if not path.is_file():
        return 30.0, 0.0, 0
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 30.0, 0.0, 0
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = n / fps if fps > 0 else 0.0
    # Prefer ffprobe duration when available (more accurate for some encodings)
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            duration = float(out)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        pass
    return fps, duration, n


def load_session(session_path: Path | str) -> SessionInfo:
    path = Path(session_path).resolve()
    session_id = path.name
    meta: dict[str, Any] = {}
    meta_path = path / "meta.json"
    if meta_path.is_file():
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

    cameras = [c for c in CAMERAS if (path / "videos" / f"{c}.mp4").is_file()]
    left = path / "videos" / "left.mp4"
    fps_meta = float(meta.get("fps") or 30.0)
    fps, duration, frame_count = _probe_video(left)
    if fps <= 0:
        fps = fps_meta
    if duration <= 0 and meta.get("duration_sec"):
        duration = float(meta["duration_sec"])
    if frame_count <= 0 and meta.get("synced_frames"):
        frame_count = int(meta["synced_frames"])

    return SessionInfo(
        path=path,
        session_id=session_id,
        meta=meta,
        duration_sec=duration,
        fps=fps,
        frame_count=frame_count,
        cameras_present=cameras,
        exported_count=count_exported(path),
        draft_count=count_draft_unexported(path),
        keep_whole=is_keep_whole(path),
        reject_whole=is_reject_whole(path),
    )


def discover_sessions(data_root: Path | str) -> list[SessionInfo]:
    """Find session_* directories that contain videos/left.mp4 under data_root."""
    root = Path(data_root)
    if not root.is_dir():
        return []

    found: list[Path] = []
    # Direct children or nested: device/date/session_*
    # Skip session_*/divide/** (exported cuts are also named session_*)
    for p in root.rglob("session_*"):
        if not p.is_dir():
            continue
        if "divide" in p.parts:
            continue
        if (p / "videos" / "left.mp4").is_file():
            found.append(p)

    found = sorted(set(found), key=lambda x: x.name)
    sessions: list[SessionInfo] = []
    for p in found:
        try:
            sessions.append(load_session(p))
        except Exception:
            continue
    return sessions


def load_timestamps(path: Path) -> list[tuple[int, float]]:
    """Parse 'frame_idx absolute_ts' lines."""
    rows: list[tuple[int, float]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            rows.append((int(parts[0]), float(parts[1])))
    return rows


def abs_time_to_frame_index(
    timestamps: list[tuple[int, float]], abs_ts: float
) -> int:
    """Map an absolute timestamp to the nearest frame index."""
    if not timestamps:
        return 0
    i = bisect.bisect_left(timestamps, abs_ts, key=lambda row: row[1])
    candidates = []
    if i < len(timestamps):
        candidates.append(i)
    if i > 0:
        candidates.append(i - 1)
    return min(candidates, key=lambda idx: abs(timestamps[idx][1] - abs_ts))


def first_index_ge(
    timestamps: list[tuple[int, float]], abs_ts: float
) -> int:
    """First index with timestamp >= abs_ts, or len(timestamps) if none."""
    if not timestamps:
        return 0
    return bisect.bisect_left(timestamps, abs_ts, key=lambda row: row[1])


def last_index_le(
    timestamps: list[tuple[int, float]], abs_ts: float
) -> int:
    """Last index with timestamp <= abs_ts, or -1 if none."""
    if not timestamps:
        return -1
    return bisect.bisect_right(timestamps, abs_ts, key=lambda row: row[1]) - 1


def relative_time_to_frame_index(
    timestamps: list[tuple[int, float]], t_rel: float, fps: float
) -> int:
    """Map relative video time (seconds from start) to nearest frame index."""
    if not timestamps:
        return max(0, int(round(t_rel * fps)))
    return abs_time_to_frame_index(timestamps, timestamps[0][1] + t_rel)

"""Cut multi-modal session bundles without modifying source data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os
import shutil
import subprocess

from models import EXPORT_CAMERAS, Segment
from session_loader import (
    SessionInfo,
    first_index_ge,
    last_index_le,
    load_timestamps,
    relative_time_to_frame_index,
)

DIVIDE_DIR_NAME = "divide"
_FFMPEG_HEAD = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-y",
]
_ffmpeg_encoders: set[str] | None = None
_failed_hw: set[str] = set()
_ACCURATE_PREROLL_SEC = 5.0


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )


def _list_ffmpeg_encoders() -> set[str]:
    global _ffmpeg_encoders
    if _ffmpeg_encoders is not None:
        return _ffmpeg_encoders
    found: set[str] = set()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0][:1] in "VAS":
                found.add(parts[1])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        found = set()
    _ffmpeg_encoders = found
    return found


def _hw_encoder_attempts() -> list[tuple[str, list[str]]]:
    """Hardware H.264 encoders to try, based on this machine's ffmpeg build.

    Not tied to a specific GPU. NVIDIA / Intel / AMD are all optional.
    Override with env VIDEO_CUTTER_ENCODER=h264_nvenc|h264_qsv|h264_amf|libx264
    """
    forced = os.environ.get("VIDEO_CUTTER_ENCODER", "").strip().lower()
    if forced in ("libx264", "cpu", "none"):
        return []
    available = _list_ffmpeg_encoders()
    # (ffmpeg encoder name, attempt id, extra args)
    catalog: list[tuple[str, str, list[str]]] = [
        (
            "h264_nvenc",
            "h264_nvenc_p1",
            ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
        ),
        (
            "h264_nvenc",
            "h264_nvenc_fast",
            ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
        ),
        (
            "h264_qsv",
            "h264_qsv",
            ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "18"],
        ),
        (
            "h264_amf",
            "h264_amf",
            ["-c:v", "h264_amf", "-quality", "speed", "-qp_i", "18", "-qp_p", "18"],
        ),
    ]
    picked: list[tuple[str, list[str]]] = []
    for encoder, attempt_id, extra in catalog:
        if forced and encoder != forced:
            continue
        if not forced and encoder not in available:
            continue
        if attempt_id in _failed_hw:
            continue
        picked.append((attempt_id, extra))
    return picked


def probe_nb_frames(path: Path) -> int | None:
    """Return video packet/frame count, or None if unreadable."""
    if not path.is_file():
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (proc.stdout or "").strip()
        if text and text not in ("N/A", "0"):
            return int(text)
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_entries",
                "stream=nb_read_packets",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        text = (proc.stdout or "").strip()
        if text and text != "N/A":
            return int(text)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def _unlink_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def _frame_count_ok(dst: Path, n_frames: int | None) -> bool:
    if not dst.is_file() or dst.stat().st_size <= 0:
        return False
    if n_frames is None or n_frames <= 0:
        return True
    return probe_nb_frames(dst) == n_frames


def _accurate_seek_args(src: Path, t0: float) -> list[str]:
    """Keyframe-near input seek, then decode the remainder to land on t0."""
    t0 = max(0.0, float(t0))
    preroll = min(_ACCURATE_PREROLL_SEC, t0)
    args: list[str] = []
    if t0 > preroll:
        args += ["-ss", f"{t0 - preroll:.6f}"]
    args += ["-i", str(src)]
    if t0 > 0:
        args += ["-ss", f"{preroll if t0 > preroll else t0:.6f}"]
    return args


def cut_video(
    src: Path,
    dst: Path,
    start_frame: int,
    n_frames: int,
    t0: float | None = None,
    fps: float = 30.0,
) -> None:
    """Extract n_frames starting at start_frame; output count is exact."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    start_frame = max(0, int(start_frame))
    n_frames = max(1, int(n_frames))
    fps = float(fps) if fps and fps > 0 else 30.0
    if t0 is None:
        t0 = start_frame / fps
    duration = n_frames / fps
    frame_limit = ["-vsync", "cfr", "-frames:v", str(n_frames)]

    copy_cmd = [
        *_FFMPEG_HEAD,
        "-fflags",
        "+fastseek",
        "-ss",
        f"{t0:.6f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.6f}",
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dst),
    ]
    try:
        _run(copy_cmd)
        if _frame_count_ok(dst, n_frames):
            return
    except RuntimeError:
        pass
    _unlink_if_exists(dst)

    accurate = _accurate_seek_args(src, t0)
    for name, extra in _hw_encoder_attempts():
        cmd = [
            *_FFMPEG_HEAD,
            *accurate,
            "-map",
            "0:v:0",
            *frame_limit,
            *extra,
            "-an",
            str(dst),
        ]
        try:
            _run(cmd)
            if _frame_count_ok(dst, n_frames):
                return
        except RuntimeError:
            _failed_hw.add(name)
        _unlink_if_exists(dst)

    reenc_cmd = [
        *_FFMPEG_HEAD,
        *accurate,
        "-map",
        "0:v:0",
        *frame_limit,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-threads",
        "0",
        "-an",
        str(dst),
    ]
    _run(reenc_cmd)
    if not _frame_count_ok(dst, n_frames):
        got = probe_nb_frames(dst)
        raise RuntimeError(
            f"cut_video frame count mismatch: expected {n_frames}, got {got} ({dst})"
        )


_TS_EQ_EPS = 1e-6


def _ts_eq(a: float, b: float) -> bool:
    return abs(a - b) <= _TS_EQ_EPS


def common_abs_frame_window(
    cam_ts: dict[str, list[tuple[int, float]]],
    ts_first: float,
    ts_last: float,
) -> tuple[float, float, dict[str, tuple[int, int]]]:
    """Drop unmatched leading/trailing frames until first and last timestamps match.

    Example: left[0] is earlier than right[0], but left[1] == right[0] → drop
    left frame 0. Same from the tail: drop whichever camera has extra later frames.

    Returns (common_first, common_last_inclusive, {cam: (i0, i1_exclusive)}).
    """
    present = {cam: rows for cam, rows in cam_ts.items() if rows}
    if not present:
        return ts_first, ts_last, {}

    bounds: dict[str, list[int]] = {}
    for cam, rows in present.items():
        lo = first_index_ge(rows, ts_first)
        hi = last_index_le(rows, ts_last)
        if lo > hi or lo >= len(rows) or hi < 0:
            continue
        bounds[cam] = [lo, hi]
    if not bounds:
        return ts_first, ts_last, {}

    # Drop earlier first frames (left[0] in the example) until first ts match.
    while len(bounds) >= 2:
        firsts = {
            cam: present[cam][lo][1]
            for cam, (lo, hi) in bounds.items()
            if lo <= hi
        }
        if not firsts:
            break
        target = max(firsts.values())
        if all(_ts_eq(v, target) for v in firsts.values()):
            break
        progressed = False
        for cam, (lo, hi) in list(bounds.items()):
            if lo <= hi and present[cam][lo][1] < target - _TS_EQ_EPS:
                bounds[cam][0] = lo + 1
                progressed = True
        if not progressed:
            break

    # Drop later last frames until last ts match.
    while len(bounds) >= 2:
        lasts = {
            cam: present[cam][hi][1]
            for cam, (lo, hi) in bounds.items()
            if lo <= hi
        }
        if not lasts:
            break
        target = min(lasts.values())
        if all(_ts_eq(v, target) for v in lasts.values()):
            break
        progressed = False
        for cam, (lo, hi) in list(bounds.items()):
            if lo <= hi and present[cam][hi][1] > target + _TS_EQ_EPS:
                bounds[cam][1] = hi - 1
                progressed = True
        if not progressed:
            break

    bounds = {cam: (lo, hi) for cam, (lo, hi) in bounds.items() if lo <= hi}
    if not bounds:
        return ts_first, ts_last, {}

    firsts = [present[cam][lo][1] for cam, (lo, hi) in bounds.items()]
    lasts = [present[cam][hi][1] for cam, (lo, hi) in bounds.items()]
    ranges = {cam: (lo, hi + 1) for cam, (lo, hi) in bounds.items()}
    return max(firsts), min(lasts), ranges


def cut_timestamps_by_frame_range(
    src: Path,
    dst: Path,
    start_frame: int,
    end_frame: int,
) -> int:
    """Keep source rows [start_frame, end_frame) in file order.

    Second column stays the original absolute timestamp. Local index is
    renumbered from 0 so line i matches video frame i.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    start_frame = max(0, int(start_frame))
    end_frame = max(start_frame, int(end_frame))
    abs_ts: list[float] = []
    with src.open("r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                abs_ts.append(float(parts[1]))
            except ValueError:
                continue
    chosen = abs_ts[start_frame:end_frame]
    with dst.open("w", encoding="utf-8") as fout:
        for n, ts in enumerate(chosen):
            fout.write(f"{n} {ts:.9f}\n")
    return len(chosen)


def append_cut_log(log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def divide_output_root(session: SessionInfo) -> Path:
    """Per-session output root: session_*/divide."""
    return session.path / DIVIDE_DIR_NAME


def export_segment(
    session: SessionInfo,
    segment: Segment,
    output_root: Path | str | None = None,
) -> Path:
    """
    Export one segment bundle under session_*/divide/good|bad/...
    Cuts left/right videos by frame index; writes absolute timestamps.
    Copies imu/ as-is (not trimmed). Does not cut audio.
    Does not modify original session files.
    """
    errors = segment.validate()
    if errors:
        raise ValueError("; ".join(errors))
    if segment.t1 > session.duration_sec + 0.5:
        raise ValueError(
            f"终点 {segment.t1:.2f}s 超出视频时长 {session.duration_sec:.2f}s"
        )

    output_root = Path(output_root) if output_root is not None else divide_output_root(session)
    quality_dir = output_root / ("good" if segment.quality == "good" else "bad")
    quality_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    folder = segment.folder_name(session.session_id, stamp=stamp)
    out_dir = quality_dir / folder
    if out_dir.exists():
        out_dir = quality_dir / f"{folder}_{segment.segment_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    left_ts = load_timestamps(session.timestamps_path("left"))
    if not left_ts:
        n = max(1, int(round(session.duration_sec * session.fps)))
        base = 0.0
        left_ts = [(i, base + i / session.fps) for i in range(n)]

    i0 = relative_time_to_frame_index(left_ts, segment.t0, session.fps)
    i1 = relative_time_to_frame_index(left_ts, segment.t1, session.fps)
    if i1 <= i0:
        i1 = min(len(left_ts), i0 + 1)
    i1 = min(i1, len(left_ts))
    i0 = max(0, min(i0, len(left_ts) - 1))

    cam_ts: dict[str, list[tuple[int, float]]] = {"left": left_ts}
    for cam in EXPORT_CAMERAS:
        if cam == "left":
            continue
        rows = load_timestamps(session.timestamps_path(cam))
        if rows:
            cam_ts[cam] = rows

    ts_first = left_ts[i0][1]
    ts_last = left_ts[i1 - 1][1] if i1 > i0 else ts_first
    common_first, common_last, ranges = common_abs_frame_window(
        cam_ts, ts_first, ts_last
    )
    if "left" not in ranges:
        ranges["left"] = (i0, i1)

    i0, i1 = ranges["left"]
    n_frames = max(1, i1 - i0)
    ts_start = common_first
    if i1 < len(left_ts):
        ts_end = left_ts[i1][1]
    else:
        ts_end = common_last + (1.0 / max(session.fps, 1.0))

    videos_out = out_dir / "videos"
    ts_out = out_dir / "timestamps"
    videos_out.mkdir(parents=True, exist_ok=True)
    ts_out.mkdir(parents=True, exist_ok=True)

    def _cut_range(cam: str) -> tuple[int, int, int, float]:
        start, end = ranges.get(cam, (i0, i1))
        n = max(1, end - start)
        rows = cam_ts.get(cam) or left_ts
        t0_cam = (
            max(0.0, rows[start][1] - rows[0][1]) if rows and start < len(rows) else segment.t0
        )
        return start, end, n, t0_cam

    def _cut_video(cam: str) -> None:
        src_v = session.video_path(cam)
        if not src_v.is_file():
            return
        start, _, n, t0_cam = _cut_range(cam)
        cut_video(
            src_v,
            videos_out / f"{cam}.mp4",
            start_frame=start,
            n_frames=n,
            t0=t0_cam,
            fps=session.fps,
        )

    def _cut_ts(cam: str) -> tuple[str, int | None]:
        src_ts = session.timestamps_path(cam)
        if not src_ts.is_file():
            return cam, None
        start, end, _, _ = _cut_range(cam)
        n = cut_timestamps_by_frame_range(
            src_ts, ts_out / f"{cam}_timestamps.txt", start, end
        )
        return cam, n

    ts_counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_cut_video, cam) for cam in EXPORT_CAMERAS]
        ts_futs = [pool.submit(_cut_ts, cam) for cam in EXPORT_CAMERAS]
        calib_fut = None
        if session.calibrations_dir.is_dir():
            calib_fut = pool.submit(
                shutil.copytree, session.calibrations_dir, out_dir / "calibrations"
            )
        imu_fut = None
        imu_dir = session.imu_csv.parent
        if imu_dir.is_dir():
            imu_fut = pool.submit(shutil.copytree, imu_dir, out_dir / "imu")
        for fut in futs:
            fut.result()
        for fut in ts_futs:
            cam, n = fut.result()
            if n is not None:
                ts_counts[cam] = n
        if calib_fut is not None:
            calib_fut.result()
        if imu_fut is not None:
            imu_fut.result()

    # Meta for sliced bundle: copy session meta, then overwrite times for this cut.
    span_sec = max(0.0, float(common_last) - float(common_first))
    duration_sec = max(0.0, float(ts_end) - float(ts_start))
    if duration_sec <= 0:
        duration_sec = segment.duration()
    n_left = int(ts_counts.get("left", n_frames))
    sliced_meta = {
        **session.meta,
        "source_session": str(session.path),
        "source_session_id": session.session_id,
        "cut": {
            "action_zh": segment.action_zh,
            "quality": segment.quality,
            "t0": segment.t0,
            "t1": segment.t1,
            "duration_sec": duration_sec,
            "frame_range_left": [i0, i1],
            "frame_ranges": {cam: list(ranges[cam]) for cam in ranges},
            "expected_frames": n_frames,
            "expected_frame_counts": {
                cam: max(1, end - start) for cam, (start, end) in ranges.items()
            },
            "abs_time_window": [ts_start, ts_end],
            "abs_first_last": [common_first, common_last],
            "note": segment.note,
            "segment_id": segment.segment_id,
        },
        "duration_sec": duration_sec,
        "synced_frames": n_left,
        "span_sec": round(span_sec, 6),
        "total_written_frames": n_left,
    }
    if session.fps > 0 and span_sec > 0:
        sliced_meta["avg_fps_per_cam"] = round(n_left / span_sec, 4)
    topics = sliced_meta.get("topics")
    if isinstance(topics, dict):
        for cam, n in ts_counts.items():
            if cam in topics and isinstance(topics[cam], dict):
                topics[cam]["written_frames"] = int(n)
    with (out_dir / "meta.json").open("w", encoding="utf-8") as f:
        json.dump(sliced_meta, f, ensure_ascii=False, indent=2)

    cut_info = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_session": str(session.path),
        "source_session_id": session.session_id,
        "output_dir": str(out_dir),
        "action_zh": segment.action_zh,
        "quality": segment.quality,
        "t0": segment.t0,
        "t1": segment.t1,
        "duration": duration_sec,
        "frame_range": [i0, i1],
        "frame_ranges": {cam: list(ranges[cam]) for cam in ranges},
        "expected_frames": n_frames,
        "expected_frame_counts": {
            cam: max(1, end - start) for cam, (start, end) in ranges.items()
        },
        "abs_time_window": [ts_start, ts_end],
        "abs_first_last": [common_first, common_last],
        "timestamp_counts": ts_counts,
        "cameras": [c for c in EXPORT_CAMERAS if (videos_out / f"{c}.mp4").is_file()],
        "operator_note": segment.note,
        "segment_id": segment.segment_id,
    }
    with (out_dir / "cut_info.json").open("w", encoding="utf-8") as f:
        json.dump(cut_info, f, ensure_ascii=False, indent=2)

    log_record = {
        "timestamp": cut_info["exported_at"],
        "source_session": str(session.path),
        "action_zh": segment.action_zh,
        "quality": segment.quality,
        "t0": segment.t0,
        "t1": segment.t1,
        "duration": segment.duration(),
        "output_dir": str(out_dir),
        "frame_range": [i0, i1],
        "frame_ranges": {cam: list(ranges[cam]) for cam in ranges},
        "operator_note": segment.note,
        "segment_id": segment.segment_id,
    }
    append_cut_log(output_root / "logs" / "cut_history.jsonl", log_record)

    return out_dir


def export_segments(
    session: SessionInfo,
    segments: list[Segment],
    output_root: Path | str | None = None,
) -> list[Path]:
    results: list[Path] = []
    for seg in segments:
        results.append(export_segment(session, seg, output_root))
    return results

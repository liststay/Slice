"""Cut multi-modal session bundles without modifying source data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import csv
import json
import os
import shutil
import subprocess

from models import CAMERAS, Segment
from session_loader import (
    SessionInfo,
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


def cut_video(src: Path, dst: Path, t0: float, t1: float) -> None:
    """Cut [t0, t1). Stream copy first; optional GPU encode; then CPU x264."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.001, t1 - t0)
    seek = [
        "-fflags",
        "+fastseek",
        "-ss",
        f"{t0:.6f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.6f}",
    ]
    copy_cmd = [
        *_FFMPEG_HEAD,
        *seek,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dst),
    ]
    try:
        _run(copy_cmd)
        if dst.is_file() and dst.stat().st_size > 0:
            return
    except RuntimeError:
        if dst.exists():
            dst.unlink()

    for name, extra in _hw_encoder_attempts():
        cmd = [*_FFMPEG_HEAD, *seek, "-map", "0:v:0", *extra, str(dst)]
        try:
            _run(cmd)
            if dst.is_file() and dst.stat().st_size > 0:
                return
        except RuntimeError:
            _failed_hw.add(name)
            if dst.exists():
                dst.unlink()

    reenc_cmd = [
        *_FFMPEG_HEAD,
        *seek,
        "-map",
        "0:v:0",
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


def cut_audio(src: Path, dst: Path, t0: float, t1: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.001, t1 - t0)
    cmd = [
        *_FFMPEG_HEAD,
        "-ss",
        f"{t0:.6f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.6f}",
        "-c",
        "copy",
        str(dst),
    ]
    try:
        _run(cmd)
    except RuntimeError:
        if dst.exists():
            dst.unlink()
        cmd = [
            *_FFMPEG_HEAD,
            "-ss",
            f"{t0:.6f}",
            "-i",
            str(src),
            "-t",
            f"{duration:.6f}",
            str(dst),
        ]
        _run(cmd)


def cut_timestamps_by_abs_window(
    src: Path,
    dst: Path,
    ts_start: float,
    ts_end: float,
) -> int:
    """Keep rows with abs timestamp in [ts_start, ts_end), renumber from 0."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                ts = float(parts[1])
            except ValueError:
                continue
            if ts < ts_start:
                continue
            if ts >= ts_end:
                break
            fout.write(f"{n} {ts:.9f}\n")
            n += 1
    return n


def cut_imu(src: Path, dst: Path, ts_start: float, ts_end: float) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        return 0
    n = 0
    with src.open("r", encoding="utf-8", newline="") as fin, dst.open(
        "w", encoding="utf-8", newline=""
    ) as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)
        header = next(reader, None)
        if header:
            writer.writerow(header)
        for row in reader:
            if not row:
                continue
            try:
                ts = float(row[0])
            except ValueError:
                continue
            if ts < ts_start:
                continue
            if ts >= ts_end:
                break
            writer.writerow(row)
            n += 1
    return n


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
    Does not modify original videos / timestamps / imu / audio / calibrations.
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
        # Fallback: synthesize from fps
        n = max(1, int(round(session.duration_sec * session.fps)))
        base = 0.0
        left_ts = [(i, base + i / session.fps) for i in range(n)]

    i0 = relative_time_to_frame_index(left_ts, segment.t0, session.fps)
    i1 = relative_time_to_frame_index(left_ts, segment.t1, session.fps)
    if i1 <= i0:
        i1 = min(len(left_ts), i0 + 1)
    i1 = min(i1, len(left_ts))
    i0 = max(0, min(i0, len(left_ts) - 1))

    ts_start = left_ts[i0][1]
    ts_end = left_ts[i1 - 1][1] + (1.0 / session.fps) if i1 > i0 else ts_start + 1e-3
    # Prefer half-open window using next frame abs time when available
    if i1 < len(left_ts):
        ts_end = left_ts[i1][1]
    else:
        ts_end = left_ts[-1][1] + (1.0 / max(session.fps, 1.0))

    videos_out = out_dir / "videos"
    ts_out = out_dir / "timestamps"
    videos_out.mkdir(parents=True, exist_ok=True)
    ts_out.mkdir(parents=True, exist_ok=True)

    def _cut_video(cam: str) -> None:
        src_v = session.video_path(cam)
        if src_v.is_file():
            cut_video(src_v, videos_out / f"{cam}.mp4", segment.t0, segment.t1)

    def _cut_ts(cam: str) -> tuple[str, int | None]:
        src_ts = session.timestamps_path(cam)
        if not src_ts.is_file():
            return cam, None
        n = cut_timestamps_by_abs_window(
            src_ts, ts_out / f"{cam}_timestamps.txt", ts_start, ts_end
        )
        return cam, n

    ts_counts: dict[str, int] = {}
    imu_n = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_cut_video, cam) for cam in CAMERAS]
        ts_futs = [pool.submit(_cut_ts, cam) for cam in CAMERAS]
        if session.audio_wav.is_file():
            futs.append(
                pool.submit(
                    cut_audio,
                    session.audio_wav,
                    out_dir / "audio" / "audio.wav",
                    segment.t0,
                    segment.t1,
                )
            )
        imu_fut = None
        if session.imu_csv.is_file():
            imu_fut = pool.submit(
                cut_imu,
                session.imu_csv,
                out_dir / "imu" / "imu0.csv",
                ts_start,
                ts_end,
            )
        calib_fut = None
        if session.calibrations_dir.is_dir():
            calib_fut = pool.submit(
                shutil.copytree, session.calibrations_dir, out_dir / "calibrations"
            )
        for fut in futs:
            fut.result()
        for fut in ts_futs:
            cam, n = fut.result()
            if n is not None:
                ts_counts[cam] = n
        if imu_fut is not None:
            imu_n = imu_fut.result()
        if calib_fut is not None:
            calib_fut.result()

    # Meta for sliced bundle
    sliced_meta = {
        **session.meta,
        "source_session": str(session.path),
        "source_session_id": session.session_id,
        "cut": {
            "action_zh": segment.action_zh,
            "quality": segment.quality,
            "t0": segment.t0,
            "t1": segment.t1,
            "duration_sec": segment.duration(),
            "frame_range_left": [i0, i1],
            "abs_time_window": [ts_start, ts_end],
            "note": segment.note,
            "segment_id": segment.segment_id,
        },
        "duration_sec": segment.duration(),
        "synced_frames": ts_counts.get("left", i1 - i0),
        "imu": {
            **(session.meta.get("imu") or {}),
            "written_samples": imu_n,
        },
    }
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
        "duration": segment.duration(),
        "frame_range": [i0, i1],
        "abs_time_window": [ts_start, ts_end],
        "timestamp_counts": ts_counts,
        "imu_samples": imu_n,
        "cameras": [c for c in CAMERAS if (videos_out / f"{c}.mp4").is_file()],
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

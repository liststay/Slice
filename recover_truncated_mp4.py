"""Remux truncated MP4s that have H.264 in mdat but never got a moov atom.

Does not modify the source videos. Writes playable mp4s to videos_recovered/,
then trims timestamps / IMU / audio to the recovered length and corrects
meta.json. Original meta.json and timestamps are copied to recovery_backup/
before any sidecar rewrite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import csv
import json
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models import CAMERAS

_FFMPEG_HEAD = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-y",
]
_START_CODE = b"\x00\x00\x00\x01"
_MAX_NAL = 8_000_000
BACKUP_DIR_NAME = "recovery_backup"


def find_mdat_payload(path: Path) -> tuple[int, int]:
    """Return [start, end) byte offsets of mdat payload. end is min(box end, file size)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        off = 0
        while off + 8 <= size:
            f.seek(off)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            box_size = int.from_bytes(hdr[:4], "big")
            kind = hdr[4:8]
            header_len = 8
            if box_size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                box_size = int.from_bytes(ext, "big")
                header_len = 16
            elif box_size == 0:
                box_size = size - off
            if box_size < header_len:
                break
            if kind == b"mdat":
                start = off + header_len
                end = min(off + box_size, size)
                return start, end
            off += box_size
    raise RuntimeError(f"No mdat atom in {path}")


def iter_avcc_nals(path: Path, start: int, end: int, max_frames: int | None):
    """Yield length-prefixed H.264 NAL units from mdat. Stops on a truncated tail."""
    frames = 0
    with path.open("rb") as f:
        f.seek(start)
        while True:
            pos = f.tell()
            if pos + 4 > end:
                break
            hdr = f.read(4)
            if len(hdr) < 4:
                break
            n = int.from_bytes(hdr, "big")
            if n <= 0 or n > _MAX_NAL or pos + 4 + n > end:
                break
            nal = f.read(n)
            if len(nal) != n:
                break
            yield nal
            ntype = nal[0] & 0x1F
            if ntype in (1, 5):
                frames += 1
                if max_frames is not None and frames >= max_frames:
                    break


def recover_mp4(
    src: Path,
    dst: Path,
    fps: float = 30.0,
    max_frames: int | None = None,
) -> dict[str, int]:
    src = src.resolve()
    dst = dst.resolve()
    if dst == src:
        raise ValueError("Refusing to overwrite the source file")
    dst.parent.mkdir(parents=True, exist_ok=True)
    start, end = find_mdat_payload(src)
    cmd = [
        *_FFMPEG_HEAD,
        "-fflags",
        "+genpts",
        "-f",
        "h264",
        "-framerate",
        f"{fps:g}",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    nals = 0
    frames = 0
    stderr = b""
    try:
        for nal in iter_avcc_nals(src, start, end, max_frames):
            proc.stdin.write(_START_CODE)
            proc.stdin.write(nal)
            nals += 1
            if (nal[0] & 0x1F) in (1, 5):
                frames += 1
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait()
    except BrokenPipeError:
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait()
    stdout = b""
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        err = (stderr or stdout or b"").decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"ffmpeg remux failed ({proc.returncode}): {err}")
    return {"nals": nals, "frames": frames, "mdat_bytes": end - start}


def needs_moov_repair(path: Path) -> bool:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return False
    err = (proc.stderr or "").lower()
    return "moov" in err or proc.returncode != 0


def probe_nb_frames(path: Path) -> int | None:
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
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def probe_duration_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        proc = subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (proc.stdout or "").strip()
        if text and text != "N/A":
            return float(text)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def _copy_if_absent(src: Path, dst: Path) -> bool:
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return True


def backup_sidecars(session_dir: Path) -> Path:
    """Copy original meta.json and timestamps/ (plus imu/audio if present)."""
    bak = session_dir / BACKUP_DIR_NAME
    bak.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    if _copy_if_absent(session_dir / "meta.json", bak / "meta.json"):
        copied.append("meta.json")
    if _copy_if_absent(session_dir / "timestamps", bak / "timestamps"):
        copied.append("timestamps/")
    if _copy_if_absent(session_dir / "imu" / "imu0.csv", bak / "imu" / "imu0.csv"):
        copied.append("imu/imu0.csv")
    if _copy_if_absent(session_dir / "audio" / "audio.wav", bak / "audio" / "audio.wav"):
        copied.append("audio/audio.wav")
    if copied:
        print(f"backup -> {bak}: {', '.join(copied)}", file=sys.stderr)
    else:
        print(f"backup already present: {bak}", file=sys.stderr)
    return bak


def save_recovered_frame_counts(session_dir: Path, frame_counts: dict[str, int]) -> None:
    path = session_dir / BACKUP_DIR_NAME / "recovered_frame_counts.json"
    if path.is_file() or not frame_counts:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({k: int(v) for k, v in frame_counts.items()}, indent=2) + "\n",
        encoding="utf-8",
    )


def load_recovered_frame_counts(
    session_dir: Path, fallback: dict[str, int]
) -> dict[str, int]:
    path = session_dir / BACKUP_DIR_NAME / "recovered_frame_counts.json"
    if not path.is_file():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict):
        return fallback
    out: dict[str, int] = {}
    for cam, n in data.items():
        try:
            out[str(cam)] = int(n)
        except (TypeError, ValueError):
            continue
    return out or fallback


def _timestamp_src(session_dir: Path, cam: str) -> Path:
    bak = session_dir / BACKUP_DIR_NAME / "timestamps" / f"{cam}_timestamps.txt"
    if bak.is_file():
        return bak
    return session_dir / "timestamps" / f"{cam}_timestamps.txt"


def load_timestamp_rows(path: Path) -> list[tuple[int, float]]:
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
            try:
                rows.append((int(float(parts[0])), float(parts[1])))
            except ValueError:
                continue
    return rows


def write_timestamp_times(dst: Path, times: list[float]) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8") as f:
        for n, ts in enumerate(times):
            f.write(f"{n} {ts:.9f}\n")
    return len(times)


def load_timestamp_times(path: Path) -> list[float]:
    return [ts for _, ts in load_timestamp_rows(path)]


def trim_timestamp_file(src: Path, dst: Path, n_frames: int) -> int:
    """Keep the first n_frames rows (recording was cut off at the end)."""
    if n_frames <= 0 or not src.is_file():
        return 0
    lines: list[str] = []
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                lines.append(line if line.endswith("\n") else line + "\n")
            if len(lines) >= n_frames:
                break
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8") as f:
        f.writelines(lines)
    return len(lines)


def trim_imu_csv(src: Path, dst: Path, ts_start: float, ts_end: float) -> int:
    if not src.is_file():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
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


def trim_wav(src: Path, dst: Path, duration_sec: float) -> None:
    if not src.is_file() or duration_sec <= 0:
        return
    current = probe_duration_sec(src)
    if current is not None and current <= duration_sec + 0.05:
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp.wav")
    cmd = [
        *_FFMPEG_HEAD,
        "-i",
        str(src),
        "-t",
        f"{duration_sec:.6f}",
        "-c",
        "copy",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 0:
        if tmp.exists():
            tmp.unlink()
        cmd = [
            *_FFMPEG_HEAD,
            "-i",
            str(src),
            "-t",
            f"{duration_sec:.6f}",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError(
                f"audio trim failed ({proc.returncode}): {(proc.stderr or '')[-1500:]}"
            )
    tmp.replace(dst)


def trim_recovered_mp4(
    src: Path,
    dst: Path,
    start_frame: int,
    n_frames: int,
    fps: float,
) -> None:
    """Keep [start_frame, start_frame + n_frames) of a recovered mp4."""
    start_frame = max(0, int(start_frame))
    n_frames = max(1, int(n_frames))
    current = probe_nb_frames(src)
    if current == n_frames and start_frame == 0 and src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".align.tmp.mp4")
    if tmp.exists():
        tmp.unlink()
    if start_frame == 0:
        cmd = [
            *_FFMPEG_HEAD,
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-frames:v",
            str(n_frames),
            "-avoid_negative_ts",
            "make_zero",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if (
            proc.returncode == 0
            and tmp.is_file()
            and tmp.stat().st_size > 0
            and probe_nb_frames(tmp) == n_frames
        ):
            tmp.replace(dst)
            return
        if tmp.exists():
            tmp.unlink()
    from cutter import cut_video

    t0 = start_frame / max(float(fps), 1e-6)
    cut_video(src, tmp, start_frame, n_frames, t0=t0, fps=fps)
    tmp.replace(dst)


def _imu_src(session_dir: Path) -> Path:
    bak = session_dir / BACKUP_DIR_NAME / "imu" / "imu0.csv"
    if bak.is_file():
        return bak
    return session_dir / "imu" / "imu0.csv"


def _audio_src(session_dir: Path) -> Path:
    bak = session_dir / BACKUP_DIR_NAME / "audio" / "audio.wav"
    if bak.is_file():
        return bak
    return session_dir / "audio" / "audio.wav"


def patch_meta(
    session_dir: Path,
    *,
    fps: float,
    frame_counts: dict[str, int],
    duration_sec: float,
    span_sec: float,
    imu_samples: int | None,
    audio_duration: float | None,
    audio_size: int | None,
) -> None:
    meta_path = session_dir / "meta.json"
    if not meta_path.is_file():
        return
    bak_meta = session_dir / BACKUP_DIR_NAME / "meta.json"
    src = bak_meta if bak_meta.is_file() else meta_path
    meta = json.loads(src.read_text(encoding="utf-8"))
    original = {
        "duration_sec": meta.get("duration_sec"),
        "synced_frames": meta.get("synced_frames"),
        "span_sec": meta.get("span_sec"),
        "total_written_frames": meta.get("total_written_frames"),
    }
    ref_frames = frame_counts.get("left") or next(iter(frame_counts.values()), 0)
    meta["duration_sec"] = round(float(duration_sec), 6)
    meta["synced_frames"] = int(ref_frames)
    meta["span_sec"] = round(float(span_sec), 6)
    if meta.get("total_written_frames") is not None:
        meta["total_written_frames"] = int(ref_frames)
    if fps > 0 and span_sec > 0:
        meta["avg_fps_per_cam"] = round(ref_frames / span_sec, 4)
    topics = meta.get("topics")
    if isinstance(topics, dict):
        for cam, n in frame_counts.items():
            if cam in topics and isinstance(topics[cam], dict):
                topics[cam]["written_frames"] = int(n)
    imu = meta.get("imu")
    if isinstance(imu, dict) and imu_samples is not None:
        imu["written_samples"] = int(imu_samples)
    audio = meta.get("audio")
    if isinstance(audio, dict):
        if audio_duration is not None:
            audio["duration_sec"] = round(float(audio_duration), 6)
        if audio_size is not None:
            audio["file_size_bytes"] = int(audio_size)
    meta["recovery"] = {
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "backup_dir": BACKUP_DIR_NAME,
        "sidecars_trimmed": True,
        "timestamps_aligned": True,
        "frame_counts": {k: int(v) for k, v in frame_counts.items()},
        "original": original,
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


_TS_EQ_EPS = 1e-6


def timestamp_count_and_ends(path: Path) -> tuple[int, float, float] | None:
    """Return (n, first_ts, last_ts) or None if the file is missing/empty."""
    if not path.is_file():
        return None
    first = last = None
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
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
            if first is None:
                first = ts
            last = ts
            n += 1
    if n == 0 or first is None or last is None:
        return None
    return n, first, last


def playable_recovered_or_src(session_dir: Path, cam: str) -> Path | None:
    recovered = session_dir / "videos_recovered" / f"{cam}.mp4"
    if recovered.is_file() and not needs_moov_repair(recovered):
        return recovered
    src = session_dir / "videos" / f"{cam}.mp4"
    if src.is_file() and not needs_moov_repair(src):
        return src
    return None


def timestamps_need_repair(session_dir: Path) -> bool:
    """True if timestamp files do not match playable video / each other."""
    ends: dict[str, tuple[int, float, float]] = {}
    for cam in CAMERAS:
        ts_path = session_dir / "timestamps" / f"{cam}_timestamps.txt"
        info = timestamp_count_and_ends(ts_path)
        if info is None:
            continue
        video = playable_recovered_or_src(session_dir, cam)
        if video is not None:
            n_vid = probe_nb_frames(video)
            if n_vid is not None and int(n_vid) != int(info[0]):
                return True
        ends[cam] = info
    if len(ends) >= 2:
        firsts = [v[1] for v in ends.values()]
        lasts = [v[2] for v in ends.values()]
        if any(abs(v - max(firsts)) > _TS_EQ_EPS for v in firsts):
            return True
        if any(abs(v - min(lasts)) > _TS_EQ_EPS for v in lasts):
            return True
        counts = {v[0] for v in ends.values()}
        if len(counts) > 1:
            return True
    meta_path = session_dir / "meta.json"
    if meta_path.is_file() and "left" in ends:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return True
        synced = meta.get("synced_frames")
        if synced is not None and int(synced) != int(ends["left"][0]):
            return True
    return False


def sidecars_need_repair(
    session_dir: Path, frame_counts: dict[str, int] | None = None
) -> bool:
    recovered_dir = session_dir / "videos_recovered"
    if not any((recovered_dir / f"{c}.mp4").is_file() for c in CAMERAS):
        return False
    meta_path = session_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return True
        rec = meta.get("recovery")
        if (
            isinstance(rec, dict)
            and rec.get("sidecars_trimmed")
            and rec.get("timestamps_aligned")
        ):
            return False
    return True


def repair_sidecars(
    session_dir: Path,
    frame_counts: dict[str, int],
    fps: float,
) -> None:
    """Trim timestamps / IMU / audio to recovered length, align first/last frames.

    Timestamps and videos_recovered stay 1:1: leftover unmatched head/tail
    frames (e.g. left[0] earlier than right[0]) are dropped from both.
    """
    if not frame_counts:
        return
    backup_sidecars(session_dir)
    save_recovered_frame_counts(session_dir, frame_counts)
    frame_counts = load_recovered_frame_counts(session_dir, frame_counts)
    from cutter import common_abs_frame_window

    cam_ts: dict[str, list[tuple[int, float]]] = {}
    for cam, n_frames in frame_counts.items():
        src = _timestamp_src(session_dir, cam)
        rows = load_timestamp_rows(src)
        if not rows:
            print(f"skip timestamps {cam}: missing", file=sys.stderr)
            continue
        cam_ts[cam] = rows[: max(1, n_frames)]

    ranges: dict[str, tuple[int, int]] = {}
    common_first = None
    common_last = None
    if len(cam_ts) >= 2:
        ts_first = min(rows[0][1] for rows in cam_ts.values())
        ts_last = max(rows[-1][1] for rows in cam_ts.values())
        common_first, common_last, ranges = common_abs_frame_window(
            cam_ts, ts_first, ts_last
        )
        print(
            f"  align first/last ts: {common_first} .. {common_last}",
            file=sys.stderr,
        )
        for cam, (lo, hi) in ranges.items():
            print(
                f"  {cam}: drop head {lo} frame(s), keep {hi - lo} "
                f"(recovered {len(cam_ts[cam])})",
                file=sys.stderr,
            )
    else:
        for cam, rows in cam_ts.items():
            ranges[cam] = (0, len(rows))

    aligned_counts: dict[str, int] = {}
    for cam, rows in cam_ts.items():
        lo, hi = ranges.get(cam, (0, len(rows)))
        times = [ts for _, ts in rows[lo:hi]]
        dst = session_dir / "timestamps" / f"{cam}_timestamps.txt"
        kept = write_timestamp_times(dst, times)
        aligned_counts[cam] = kept
        print(f"  timestamps {cam}: {kept} rows (aligned)", file=sys.stderr)

        video = session_dir / "videos_recovered" / f"{cam}.mp4"
        if video.is_file() and kept > 0:
            video_n = probe_nb_frames(video)
            if video_n == kept:
                print(f"  video {cam}: already {kept} frames", file=sys.stderr)
            else:
                print(
                    f"  video {cam}: frames [{lo}, {hi}) -> {kept} frames",
                    file=sys.stderr,
                )
                trim_recovered_mp4(video, video, lo, kept, fps)

    if not aligned_counts:
        return

    ref = next((c for c in ("left", "right", *CAMERAS) if c in aligned_counts), None)
    if ref is None:
        return
    times = load_timestamp_times(session_dir / "timestamps" / f"{ref}_timestamps.txt")
    ts_first = times[0] if times else 0.0
    ts_last = times[-1] if times else ts_first
    if common_first is not None:
        ts_first = common_first
    if common_last is not None:
        ts_last = common_last
    span_sec = max(0.0, ts_last - ts_first)
    n_ref = aligned_counts[ref]
    duration_sec = probe_duration_sec(session_dir / "videos_recovered" / f"{ref}.mp4")
    if duration_sec is None or duration_sec <= 0:
        duration_sec = span_sec if span_sec > 0 else (n_ref / max(fps, 1e-6))
    ts_end = ts_last + (1.0 / max(fps, 1.0))

    imu_n: int | None = None
    imu_src = _imu_src(session_dir)
    if imu_src.is_file() and times:
        imu_n = trim_imu_csv(
            imu_src, session_dir / "imu" / "imu0.csv", ts_first, ts_end
        )
        print(f"  imu: {imu_n} samples", file=sys.stderr)

    audio_duration: float | None = None
    audio_size: int | None = None
    audio_src = _audio_src(session_dir)
    audio_dst = session_dir / "audio" / "audio.wav"
    if audio_src.is_file():
        trim_wav(audio_src, audio_dst, duration_sec)
        audio_duration = probe_duration_sec(audio_dst)
        if audio_dst.is_file():
            audio_size = audio_dst.stat().st_size
        print(
            f"  audio: duration={audio_duration}s size={audio_size}",
            file=sys.stderr,
        )

    patch_meta(
        session_dir,
        fps=fps,
        frame_counts=aligned_counts,
        duration_sec=duration_sec,
        span_sec=span_sec if span_sec > 0 else duration_sec,
        imu_samples=imu_n,
        audio_duration=audio_duration,
        audio_size=audio_size,
    )
    rec_path = session_dir / "meta.json"
    if rec_path.is_file():
        meta = json.loads(rec_path.read_text(encoding="utf-8"))
        rec = meta.get("recovery")
        if isinstance(rec, dict):
            rec["recovered_frame_counts"] = {k: int(v) for k, v in frame_counts.items()}
            rec["frame_ranges"] = {
                cam: [int(lo), int(hi)] for cam, (lo, hi) in ranges.items()
            }
            rec_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    print(
        f"  meta.json duration_sec={duration_sec:.3f} synced_frames={n_ref}",
        file=sys.stderr,
    )


def recovered_frame_counts(session_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    out_dir = session_dir / "videos_recovered"
    for cam in CAMERAS:
        dst = out_dir / f"{cam}.mp4"
        n = probe_nb_frames(dst)
        if n:
            counts[cam] = n
    return counts


def recover_session(
    session_dir: Path,
    fps: float | None = None,
    *,
    repair_sidecars_after: bool = True,
) -> list[Path]:
    """Recover truncated camera mp4s into session_dir/videos_recovered/.

    Then trim timestamps / IMU / audio, align first/last timestamps across
    cameras (and recut videos_recovered to stay 1:1), and correct meta.json.
    Original meta.json and timestamps are backed up under recovery_backup/.
    """
    session_dir = session_dir.resolve()
    meta_path = session_dir / "meta.json"
    if fps is None:
        fps = 30.0
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fps = float(meta.get("fps") or 30.0)
    out_dir = session_dir / "videos_recovered"
    written: list[Path] = []
    errors: list[str] = []
    saw_src = False
    frame_counts: dict[str, int] = {}
    for cam in CAMERAS:
        src = session_dir / "videos" / f"{cam}.mp4"
        try:
            src_ok = False
            for _try in range(3):
                try:
                    src_ok = src.is_file()
                    break
                except OSError as exc:
                    print(f"{cam}: stat retry {_try + 1}/3: {exc}", file=sys.stderr)
                    time.sleep(2)
            if not src_ok:
                print(f"skip {cam}: missing", file=sys.stderr)
                continue
            saw_src = True
            dst = out_dir / f"{cam}.mp4"
            if not needs_moov_repair(src):
                print(f"skip {cam}: already playable", file=sys.stderr)
                ref = dst if dst.is_file() and not needs_moov_repair(dst) else src
                n = probe_nb_frames(ref)
                if n:
                    frame_counts[cam] = n
                if dst.is_file() and not needs_moov_repair(dst):
                    written.append(dst)
                continue
            if dst.is_file() and not needs_moov_repair(dst):
                print(f"skip {cam}: recovered already playable", file=sys.stderr)
                written.append(dst)
                n = probe_nb_frames(dst)
                if n:
                    frame_counts[cam] = n
                continue
            print(f"recovering {cam} ...", file=sys.stderr)
            stats = recover_mp4(src, dst, fps=fps)
            print(
                f"  {cam}: {stats['frames']} frames -> {dst}",
                file=sys.stderr,
            )
            written.append(dst)
            if stats["frames"] > 0:
                frame_counts[cam] = int(stats["frames"])
            else:
                n = probe_nb_frames(dst)
                if n:
                    frame_counts[cam] = n
        except OSError as exc:
            msg = f"{cam}: {exc}"
            print(f"FAILED {msg}", file=sys.stderr)
            errors.append(msg)
    if not saw_src:
        raise FileNotFoundError(f"no source videos in {session_dir}")
    if errors:
        raise RuntimeError("; ".join(errors))
    if not frame_counts:
        frame_counts = recovered_frame_counts(session_dir)
    if repair_sidecars_after and frame_counts:
        print("repairing timestamps / imu / audio / meta.json ...", file=sys.stderr)
        repair_sidecars(session_dir, frame_counts, fps=fps)
    return written


def iter_sessions(root: Path):
    for p in sorted(root.rglob("session_*")):
        if not p.is_dir() or "divide" in p.parts:
            continue
        if (p / "videos" / "left.mp4").is_file():
            yield p


def session_needs_recovery(session_dir: Path) -> bool:
    has_recovered = False
    for cam in CAMERAS:
        src = session_dir / "videos" / f"{cam}.mp4"
        if not src.is_file():
            continue
        recovered = session_dir / "videos_recovered" / f"{cam}.mp4"
        if recovered.is_file() and not needs_moov_repair(recovered):
            has_recovered = True
            continue
        if needs_moov_repair(src):
            return True
    if not has_recovered:
        return False
    if sidecars_need_repair(session_dir):
        return True
    return timestamps_need_repair(session_dir)


def recover_tree(root: Path, fps: float | None = None) -> None:
    root = root.resolve()
    sessions = [p for p in iter_sessions(root) if session_needs_recovery(p)]
    print(f"sessions needing recovery: {len(sessions)} under {root}", file=sys.stderr)
    failed: list[str] = []
    for i, session_dir in enumerate(sessions, 1):
        print(f"\n[{i}/{len(sessions)}] {session_dir}", file=sys.stderr)
        try:
            recover_session(session_dir, fps=fps)
        except Exception as exc:
            print(f"FAILED {session_dir}: {exc}", file=sys.stderr)
            failed.append(str(session_dir))
    print(f"\ndone. ok={len(sessions) - len(failed)} failed={len(failed)}", file=sys.stderr)
    for p in failed:
        print(f"  fail {p}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild a playable MP4 from a truncated recording (missing moov), "
            "then trim timestamps/IMU/audio and correct meta.json."
        )
    )
    parser.add_argument("src", nargs="?", type=Path, help="Broken mp4 (not modified)")
    parser.add_argument(
        "--root",
        type=Path,
        help="Scan all session_* under this tree and recover truncated videos",
    )
    parser.add_argument(
        "--session",
        type=Path,
        help="Recover truncated videos/ under this session into videos_recovered/",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output mp4 (default: <src>.recovered.mp4)",
    )
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Stop after N video frames (for a quick test)",
    )
    parser.add_argument(
        "--no-sidecars",
        action="store_true",
        help="Only remux videos; do not trim timestamps/IMU/audio or rewrite meta.json",
    )
    parser.add_argument(
        "--sidecars-only",
        action="store_true",
        help="Skip video remux; only backup and trim sidecars using videos_recovered/",
    )
    args = parser.parse_args()
    if args.sidecars_only and args.no_sidecars:
        parser.error("use only one of --sidecars-only / --no-sidecars")
    if args.root:
        if args.sidecars_only:
            parser.error("--sidecars-only requires --session")
        recover_tree(args.root, fps=args.fps)
        return
    if args.session:
        if args.sidecars_only:
            session_dir = args.session.resolve()
            fps = args.fps
            if fps is None:
                fps = 30.0
                meta_path = session_dir / "meta.json"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    fps = float(meta.get("fps") or 30.0)
            counts = recovered_frame_counts(session_dir)
            if not counts:
                raise FileNotFoundError(
                    f"no playable videos_recovered/*.mp4 in {session_dir}"
                )
            print("repairing timestamps / imu / audio / meta.json ...", file=sys.stderr)
            repair_sidecars(session_dir, counts, fps=fps)
            return
        recover_session(
            args.session,
            fps=args.fps,
            repair_sidecars_after=not args.no_sidecars,
        )
        return
    if not args.src:
        parser.error("provide an mp4 path or --session")
    fps = 30.0 if args.fps is None else args.fps
    dst = args.output or args.src.with_name(args.src.stem + ".recovered.mp4")
    stats = recover_mp4(args.src, dst, fps=fps, max_frames=args.max_frames)
    print(
        f"recovered {stats['frames']} frames, {stats['nals']} NALs -> {dst}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

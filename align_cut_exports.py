"""Align timestamps of already-exported cut bundles and correct meta.json times.

Use this on divide/ outputs (local or NAS). Does not touch original sessions.
Rewrites timestamps/ and meta.json in place (no file backup).

Usage:
  python align_cut_exports.py --root /mnt/nas/synnas/ego/baai_ego_task
  python align_cut_exports.py --cut /path/to/one/cut/folder
  python align_cut_exports.py --root ... --dry-run
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cutter import common_abs_frame_window
from models import CAMERAS
from recover_truncated_mp4 import (
    load_timestamp_rows,
    probe_duration_sec,
    probe_nb_frames,
    trim_recovered_mp4,
    write_timestamp_times,
)

_TS_EQ_EPS = 1e-6


def ts_src(cut_dir: Path, cam: str) -> Path:
    return cut_dir / "timestamps" / f"{cam}_timestamps.txt"


def cameras_in_cut(cut_dir: Path) -> list[str]:
    return [cam for cam in CAMERAS if ts_src(cut_dir, cam).is_file()]


def iter_cut_dirs(root: Path):
    for meta in sorted(root.rglob("meta.json")):
        parent = meta.parent
        if "alignment_backup" in parent.parts:
            continue
        if not (parent / "timestamps").is_dir():
            continue
        if not (parent / "videos").is_dir():
            continue
        if (parent / "cut_info.json").is_file() or "divide" in parent.parts:
            if (parent / "timestamps" / "left_timestamps.txt").is_file() or (
                parent / "timestamps" / "right_timestamps.txt"
            ).is_file():
                yield parent


def _ts_eq(a: float, b: float) -> bool:
    return abs(a - b) <= _TS_EQ_EPS


def already_aligned(cam_ts: dict[str, list[tuple[int, float]]]) -> bool:
    rows = [r for r in cam_ts.values() if r]
    if len(rows) < 2:
        return True
    firsts = [r[0][1] for r in rows]
    lasts = [r[-1][1] for r in rows]
    return all(_ts_eq(v, max(firsts)) for v in firsts) and all(
        _ts_eq(v, min(lasts)) for v in lasts
    )


def meta_needs_time_fix(
    meta: dict, times: list[float], n_frames: int, fps: float
) -> bool:
    if not times:
        return False
    span = times[-1] - times[0]
    meta_span = meta.get("span_sec")
    if meta_span is None or abs(float(meta_span) - span) > 0.5:
        return True
    synced = meta.get("synced_frames")
    if synced is None or int(synced) != int(n_frames):
        return True
    dur = meta.get("duration_sec")
    expect = span + (1.0 / max(fps, 1.0))
    if dur is None or abs(float(dur) - expect) > 1.5:
        if dur is None or abs(float(dur) - span) > 1.5:
            return True
    return False


def patch_cut_meta(
    cut_dir: Path,
    *,
    fps: float,
    counts: dict[str, int],
    times: list[float],
    ranges: dict[str, tuple[int, int]],
    duration_sec: float,
) -> None:
    meta_path = cut_dir / "meta.json"
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    original = {
        "duration_sec": meta.get("duration_sec"),
        "synced_frames": meta.get("synced_frames"),
        "span_sec": meta.get("span_sec"),
        "total_written_frames": meta.get("total_written_frames"),
        "avg_fps_per_cam": meta.get("avg_fps_per_cam"),
    }
    n_ref = counts.get("left") or next(iter(counts.values()), 0)
    span = max(0.0, times[-1] - times[0]) if times else 0.0
    ts_first = times[0] if times else 0.0
    ts_last = times[-1] if times else 0.0
    meta["duration_sec"] = round(float(duration_sec), 6)
    meta["synced_frames"] = int(n_ref)
    meta["span_sec"] = round(float(span), 6)
    if meta.get("total_written_frames") is not None:
        meta["total_written_frames"] = int(n_ref)
    if fps > 0 and span > 0:
        meta["avg_fps_per_cam"] = round(n_ref / span, 4)
    topics = meta.get("topics")
    if isinstance(topics, dict):
        for cam, n in counts.items():
            if cam in topics and isinstance(topics[cam], dict):
                topics[cam]["written_frames"] = int(n)
    cut = meta.get("cut")
    if isinstance(cut, dict):
        cut["duration_sec"] = round(float(duration_sec), 6)
        cut["abs_time_window"] = [ts_first, ts_last + (1.0 / max(fps, 1.0))]
        if "left" in ranges:
            cut["frame_range_left"] = list(ranges["left"])
        cut["expected_frames"] = int(n_ref)
        cut["frame_ranges"] = {cam: list(r) for cam, r in ranges.items()}
    meta["cut_alignment"] = {
        "aligned_at": datetime.now(timezone.utc).isoformat(),
        "frame_ranges": {cam: list(r) for cam, r in ranges.items()},
        "original": original,
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    info_path = cut_dir / "cut_info.json"
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        info["duration"] = round(float(duration_sec), 6)
        info["abs_time_window"] = [ts_first, ts_last + (1.0 / max(fps, 1.0))]
        info["expected_frames"] = int(n_ref)
        info["timestamp_counts"] = {cam: int(n) for cam, n in counts.items()}
        if "left" in ranges:
            info["frame_range"] = list(ranges["left"])
        info["frame_ranges"] = {cam: list(r) for cam, r in ranges.items()}
        info_path.write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def align_one(cut_dir: Path, *, dry_run: bool, force: bool) -> str:
    cams = cameras_in_cut(cut_dir)
    cam_ts: dict[str, list[tuple[int, float]]] = {}
    for cam in cams:
        rows = load_timestamp_rows(ts_src(cut_dir, cam))
        if rows:
            cam_ts[cam] = rows
    if not cam_ts:
        return "skip (no timestamps)"

    meta = {}
    meta_path = cut_dir / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    fps = float(meta.get("fps") or 30.0)
    if fps <= 0:
        fps = 30.0

    ts_first = min(rows[0][1] for rows in cam_ts.values())
    ts_last = max(rows[-1][1] for rows in cam_ts.values())
    if len(cam_ts) >= 2:
        common_first, common_last, ranges = common_abs_frame_window(
            cam_ts, ts_first, ts_last
        )
    else:
        cam = next(iter(cam_ts))
        ranges = {cam: (0, len(cam_ts[cam]))}
        common_first, common_last = ts_first, ts_last

    need_align = not already_aligned(cam_ts) or any(
        lo != 0 or hi != len(cam_ts[cam]) for cam, (lo, hi) in ranges.items()
    )
    ref = next((c for c in ("left", "right", *CAMERAS) if c in cam_ts), None)
    assert ref is not None
    lo, hi = ranges.get(ref, (0, len(cam_ts[ref])))
    aligned_times = [ts for _, ts in cam_ts[ref][lo:hi]]
    n_ref = max(1, hi - lo)
    need_meta = meta_needs_time_fix(meta, aligned_times, n_ref, fps)
    if meta.get("cut_alignment", {}).get("aligned_at") and not force:
        if not need_align and not need_meta:
            return "skip (already aligned)"

    if not need_align and not need_meta and not force:
        return "skip (timestamps aligned, meta times ok)"

    actions = []
    if need_align:
        actions.append("timestamps+video")
    if need_meta:
        actions.append("meta")
    if dry_run:
        extra = []
        for cam, (a, b) in ranges.items():
            extra.append(f"{cam}[{a}:{b}]/{len(cam_ts[cam])}")
        return f"would fix ({', '.join(actions)}) " + " ".join(extra)

    counts: dict[str, int] = {}
    for cam, rows in cam_ts.items():
        a, b = ranges.get(cam, (0, len(rows)))
        times = [ts for _, ts in rows[a:b]]
        write_timestamp_times(
            cut_dir / "timestamps" / f"{cam}_timestamps.txt", times
        )
        counts[cam] = len(times)
        video = cut_dir / "videos" / f"{cam}.mp4"
        if need_align and video.is_file() and times:
            got = probe_nb_frames(video)
            if got == len(times) and a == 0:
                continue
            if got == len(times):
                continue
            trim_recovered_mp4(video, video, a, len(times), fps)

    times = [ts for _, ts in cam_ts[ref][ranges[ref][0] : ranges[ref][1]]]
    duration_sec = probe_duration_sec(cut_dir / "videos" / f"{ref}.mp4")
    span = (times[-1] - times[0]) if times else 0.0
    if duration_sec is None or duration_sec <= 0:
        duration_sec = span + (1.0 / max(fps, 1.0)) if times else 0.0
    patch_cut_meta(
        cut_dir,
        fps=fps,
        counts=counts,
        times=times,
        ranges=ranges,
        duration_sec=duration_sec,
    )
    return f"fixed ({', '.join(actions)}) n={counts.get(ref)} span={span:.3f}s"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Align cut-bundle timestamps and correct meta.json duration/span."
    )
    parser.add_argument("--root", type=Path, help="Scan divide/ cut folders under this tree")
    parser.add_argument("--cut", type=Path, help="One exported cut folder")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rewrite even if already marked")
    args = parser.parse_args()
    if not args.root and not args.cut:
        parser.error("provide --root or --cut")

    dirs: list[Path] = []
    if args.cut:
        dirs.append(args.cut.resolve())
    if args.root:
        dirs.extend(iter_cut_dirs(args.root.resolve()))
    # unique preserve order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            uniq.append(d)

    print(f"cut folders: {len(uniq)}", file=sys.stderr)
    ok = skip = fail = 0
    for i, cut_dir in enumerate(uniq, 1):
        try:
            msg = align_one(cut_dir, dry_run=args.dry_run, force=args.force)
        except Exception as exc:
            fail += 1
            print(f"[{i}/{len(uniq)}] FAIL {cut_dir}: {exc}", file=sys.stderr)
            continue
        if msg.startswith("skip"):
            skip += 1
        else:
            ok += 1
        print(f"[{i}/{len(uniq)}] {msg}  {cut_dir.name}", file=sys.stderr)
    print(f"done. fixed={ok} skipped={skip} failed={fail}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

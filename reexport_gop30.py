"""Re-export existing divide/ cuts from the source session with GOP=30.

Uses cut_info.json t0/t1 (not the rewritten [0, N) frame_range). Writes a
new bundle, verifies GOP and frame counts, then replaces the old folder.

Usage:
  python reexport_gop30.py --root /media/.../baai_ego_task --names reexport_gop30_names.txt
  python reexport_gop30.py --cut /path/to/divide/good/session_xxx
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cutter import export_segment, probe_nb_frames
from models import EXPORT_CAMERAS, Segment
from recover_truncated_mp4 import trim_recovered_mp4, write_timestamp_times
from session_loader import load_session
from runtime import ffprobe


def ts_count(path: Path) -> int:
    n = 0
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def gop_sizes(path: Path, *, max_packets: int | None = None) -> list[int]:
    cmd = [
        ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=flags",
        "-of",
        "csv=p=0",
    ]
    if max_packets:
        cmd.extend(["-read_intervals", f"%+#{int(max_packets)}"])
    cmd.append(str(path))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
    )
    flags = (proc.stdout or "").splitlines()
    keys = [i for i, ln in enumerate(flags) if "K" in ln]
    return [keys[i + 1] - keys[i] for i in range(len(keys) - 1)]


def gop_is_30(path: Path, *, quick: bool = False) -> bool:
    sizes = gop_sizes(path, max_packets=120 if quick else None)
    if not sizes:
        n = probe_nb_frames(path) or 0
        return 0 < n <= 30
    return all(g == 30 for g in sizes)


def find_cut_dirs(root: Path, names: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for info in root.rglob("cut_info.json"):
        if "divide" not in info.parts:
            continue
        parent = info.parent
        if parent.name in names and parent.name not in found:
            found[parent.name] = parent
        if len(found) == len(names):
            break
    return found


def resolve_source_session(src: str, root: Path) -> Path | None:
    """Map cut_info source_session onto the current --root mount.

    Older hosts wrote /media/user/<uuid>/... or /media/key/<uuid>/...; the
    disk may now be mounted under a different username.
    """
    p = Path(src)
    recovered = p / "videos_recovered" / "left.mp4"
    original = p / "videos" / "left.mp4"
    if p.is_dir() and (recovered.is_file() or original.is_file()):
        return p
    parts = p.parts
    try:
        i = parts.index("baai_ego_task")
    except ValueError:
        i = -1
    if i >= 0:
        cand = root.joinpath(*parts[i + 1 :])
        recovered = cand / "videos_recovered" / "left.mp4"
        original = cand / "videos" / "left.mp4"
        if cand.is_dir() and (recovered.is_file() or original.is_file()):
            return cand
    sid = p.name
    if sid.startswith("session_"):
        for hit in root.rglob(sid):
            if not hit.is_dir() or "divide" in hit.parts:
                continue
            recovered = hit / "videos_recovered" / "left.mp4"
            original = hit / "videos" / "left.mp4"
            if recovered.is_file() or original.is_file():
                return hit
    return None


def already_ok(cut_dir: Path) -> bool:
    """Skip only if this bundle was already re-exported from the source session.

    Frame-count trim of old GOP=250 cuts also produced GOP=30, but from the
    cut file (second generation). Those still have cut_info frame_range [0, N].
    """
    info_path = cut_dir / "cut_info.json"
    if not info_path.is_file():
        return False
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    fr = info.get("frame_range") or [0, 0]
    if not fr or int(fr[0]) <= 0:
        return False
    for cam in EXPORT_CAMERAS:
        video = cut_dir / "videos" / f"{cam}.mp4"
        ts = cut_dir / "timestamps" / f"{cam}_timestamps.txt"
        if not video.is_file() or not ts.is_file():
            continue
        n_v = probe_nb_frames(video)
        n_t = ts_count(ts)
        if n_v != n_t or not gop_is_30(video, quick=True):
            return False
    return True


def inherit_timestamps(exported: Path, old: Path) -> None:
    """Keep the previously labeled timestamps when the source session has none."""
    src_dir = old / "timestamps"
    if not src_dir.is_dir():
        return
    dst_dir = exported / "timestamps"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.iterdir():
        if src.is_file() and src.suffix == ".txt":
            shutil.copy2(src, dst_dir / src.name)
    old_imu = old / "imu"
    if old_imu.is_dir():
        new_imu = exported / "imu"
        if new_imu.exists():
            shutil.rmtree(new_imu)
        shutil.copytree(old_imu, new_imu)


def match_video_to_timestamps(cut_dir: Path, fps: float) -> None:
    for cam in EXPORT_CAMERAS:
        video = cut_dir / "videos" / f"{cam}.mp4"
        ts = cut_dir / "timestamps" / f"{cam}_timestamps.txt"
        if not video.is_file():
            continue
        n_v = probe_nb_frames(video)
        n_t = ts_count(ts)
        if n_v is None or n_t <= 0:
            continue
        if n_v > n_t:
            trim_recovered_mp4(video, video, 0, n_t, fps)
        elif n_v < n_t:
            times: list[float] = []
            with ts.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            times.append(float(parts[1]))
                        except ValueError:
                            continue
            write_timestamp_times(ts, times[:n_v])


def verify_bundle(cut_dir: Path) -> str | None:
    cams = [
        c
        for c in EXPORT_CAMERAS
        if (cut_dir / "videos" / f"{c}.mp4").is_file()
    ]
    if "left" not in cams:
        return "missing left.mp4"
    for cam in cams:
        video = cut_dir / "videos" / f"{cam}.mp4"
        ts = cut_dir / "timestamps" / f"{cam}_timestamps.txt"
        n_v = probe_nb_frames(video)
        n_t = ts_count(ts)
        if n_v != n_t:
            return f"{cam} frames {n_v} != ts {n_t}"
        sizes = gop_sizes(video)
        bad = [g for g in sizes if g != 30]
        if bad:
            return f"{cam} GOP {Counter(sizes)}"
    return None


def reexport_one(
    cut_dir: Path, session_cache: dict[str, object], root: Path
) -> str:
    info_path = cut_dir / "cut_info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    src_raw = str(info.get("source_session") or "")
    if not src_raw:
        return "fail (no source_session)"
    if src_raw not in session_cache:
        src = resolve_source_session(src_raw, root)
        if src is None:
            return f"fail source not found: {src_raw}"
        session_cache[src_raw] = load_session(src)
    session = session_cache[src_raw]
    keep_name = cut_dir.name
    seg = Segment(
        action_zh=str(info.get("action_zh") or "unnamed"),
        t0=float(info["t0"]),
        t1=float(info["t1"]),
        quality=info.get("quality") or "good",
        note=str(info.get("operator_note") or ""),
        occlusion=bool(info.get("occlusion", False)),
        blur=bool(info.get("blur", False)),
        segment_id=str(info.get("segment_id") or "reexport"),
    )
    seg.folder_name = lambda session_id, stamp=None, n=keep_name: n  # type: ignore[method-assign]

    dur = float(getattr(session, "duration_sec", 0.0) or 0.0)
    orig_span = max(seg.t1 - seg.t0, 1e-6)
    if dur > 0 and seg.t0 >= dur:
        return f"fail t0 {seg.t0:.2f} past recovered duration {dur:.2f}"
    if dur > 0 and seg.t1 > dur + 0.5:
        avail = dur - seg.t0
        if avail / orig_span < 0.5:
            return (
                f"fail most of cut is past recovered duration {dur:.2f}s "
                f"(t0={seg.t0:.2f} t1={seg.t1:.2f})"
            )
        seg.t1 = dur

    tmp_root = Path(tempfile.mkdtemp(prefix="reexport_gop30_", dir=str(cut_dir.parent)))
    staging = cut_dir.with_name(cut_dir.name + ".reexporting")
    old = cut_dir.with_name(cut_dir.name + ".old_gop")
    try:
        exported = export_segment(session, seg, tmp_root)
        inherit_timestamps(exported, cut_dir)
        match_video_to_timestamps(exported, float(getattr(session, "fps", 30.0) or 30.0))
        err = verify_bundle(exported)
        if err:
            return f"fail verify new bundle: {err}"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.move(str(exported), str(staging))
        if old.exists():
            shutil.rmtree(old)
        cut_dir.rename(old)
        staging.rename(cut_dir)
        try:
            shutil.rmtree(old)
        except OSError:
            trash = cut_dir.with_name(cut_dir.name + ".old_gop.trash")
            try:
                if trash.exists():
                    shutil.rmtree(trash, ignore_errors=True)
                old.rename(trash)
            except OSError:
                pass
            return "replaced GOP=30 (old leftover)"
        return "replaced GOP=30"
    except Exception as exc:
        if cut_dir.exists() and old.exists() and not cut_dir.joinpath("cut_info.json").is_file():
            # rename failed mid-way; try restore
            pass
        if old.exists() and not cut_dir.exists():
            old.rename(cut_dir)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        return f"fail {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-export cuts from source with GOP=30 and replace.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--names", type=Path, help="Text file of cut folder names, one per line")
    parser.add_argument("--cut", type=Path, help="One cut folder")
    args = parser.parse_args()
    root = args.root.resolve()

    dirs: list[Path] = []
    if args.cut:
        dirs.append(args.cut.resolve())
    if args.names:
        names = {
            ln.strip()
            for ln in args.names.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        found = find_cut_dirs(root, names)
        missing = names - set(found)
        if missing:
            print(f"missing {len(missing)} names, e.g. {next(iter(missing))}", file=sys.stderr)
        dirs.extend(found[n] for n in sorted(found))
    if not dirs:
        parser.error("provide --names or --cut")

    print(f"cuts: {len(dirs)}", file=sys.stderr)
    session_cache: dict[str, object] = {}
    ok = skip = fail = 0
    for i, cut_dir in enumerate(dirs, 1):
        if already_ok(cut_dir):
            skip += 1
            print(f"[{i}/{len(dirs)}] skip (already GOP=30)  {cut_dir.name}", file=sys.stderr)
            continue
        msg = reexport_one(cut_dir, session_cache, root)
        if msg.startswith("fail"):
            fail += 1
        else:
            ok += 1
        print(f"[{i}/{len(dirs)}] {msg}  {cut_dir.name}", file=sys.stderr)
    print(f"done. replaced={ok} skipped={skip} failed={fail}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

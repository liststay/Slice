"""Remux truncated MP4s that have H.264 in mdat but never got a moov atom.

Does not modify the source file. Writes a new playable mp4 via ffmpeg stream copy.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
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


def recover_session(session_dir: Path, fps: float | None = None) -> list[Path]:
    """Recover all truncated camera mp4s into session_dir/videos_recovered/."""
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
            if not needs_moov_repair(src):
                print(f"skip {cam}: already playable", file=sys.stderr)
                continue
            dst = out_dir / f"{cam}.mp4"
            if dst.is_file() and not needs_moov_repair(dst):
                print(f"skip {cam}: recovered already playable", file=sys.stderr)
                written.append(dst)
                continue
            print(f"recovering {cam} ...", file=sys.stderr)
            stats = recover_mp4(src, dst, fps=fps)
            print(
                f"  {cam}: {stats['frames']} frames -> {dst}",
                file=sys.stderr,
            )
            written.append(dst)
        except OSError as exc:
            msg = f"{cam}: {exc}"
            print(f"FAILED {msg}", file=sys.stderr)
            errors.append(msg)
    if not saw_src:
        raise FileNotFoundError(f"no source videos in {session_dir}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return written


def iter_sessions(root: Path):
    for p in sorted(root.rglob("session_*")):
        if not p.is_dir() or "divide" in p.parts:
            continue
        if (p / "videos" / "left.mp4").is_file():
            yield p


def session_needs_recovery(session_dir: Path) -> bool:
    for cam in CAMERAS:
        src = session_dir / "videos" / f"{cam}.mp4"
        if not src.is_file():
            continue
        recovered = session_dir / "videos_recovered" / f"{cam}.mp4"
        if recovered.is_file() and not needs_moov_repair(recovered):
            continue
        if needs_moov_repair(src):
            return True
    return False


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
        description="Rebuild a playable MP4 from a truncated recording (missing moov)."
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
        help="Recover all truncated videos/ under this session into videos_recovered/",
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
    args = parser.parse_args()
    if args.root:
        recover_tree(args.root, fps=args.fps)
        return
    if args.session:
        recover_session(args.session, fps=args.fps)
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

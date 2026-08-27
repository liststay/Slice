#!/usr/bin/env python3
"""Recover remaining truncated session videos. Safe to re-run (skips done cameras).

Does not modify original videos. Writes session_*/videos_recovered/*.mp4

Usage:
  python3 recover_remaining.py --list
  python3 recover_remaining.py
  nohup python3 -u recover_remaining.py >> recover_remaining.log 2>&1 &
"""

from __future__ import annotations

from pathlib import Path
import argparse
import sys
import time

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recover_truncated_mp4 import recover_session, session_needs_recovery

DEFAULT_LIST = ROOT / "remaining_sessions.txt"


def load_sessions(list_path: Path) -> list[Path]:
    sessions: list[Path] = []
    for raw in list_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sessions.append(Path(line))
    return sessions


def recover_one(session_dir: Path, retries: int, retry_wait: float) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            recover_session(session_dir)
            return
        except OSError as exc:
            last_exc = exc
            print(
                f"I/O error attempt {attempt}/{retries}: {exc}",
                file=sys.stderr,
            )
            if attempt < retries:
                time.sleep(retry_wait)
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover remaining truncated sessions.")
    parser.add_argument(
        "--list-file",
        type=Path,
        default=DEFAULT_LIST,
        help="Session path list (default: remaining_sessions.txt)",
    )
    parser.add_argument("--list", action="store_true", help="Print remaining sessions and exit")
    parser.add_argument("--retries", type=int, default=3, help="Retries per session on I/O error")
    parser.add_argument("--retry-wait", type=float, default=5.0)
    args = parser.parse_args()

    if not args.list_file.is_file():
        print(f"missing list file: {args.list_file}", file=sys.stderr)
        return 2

    listed = load_sessions(args.list_file)
    remaining: list[Path] = []
    missing: list[Path] = []
    done: list[Path] = []
    for p in listed:
        if not p.is_dir():
            missing.append(p)
            continue
        if session_needs_recovery(p):
            remaining.append(p)
        else:
            done.append(p)

    print(f"listed={len(listed)} remaining={len(remaining)} already_ok={len(done)} missing={len(missing)}")
    for p in remaining:
        print(f"  TODO {p}")
    for p in missing:
        print(f"  MISSING {p}", file=sys.stderr)
    if args.list:
        return 0 if not missing else 1

    failed: list[str] = []
    for i, session_dir in enumerate(remaining, 1):
        print(f"\n[{i}/{len(remaining)}] {session_dir}", file=sys.stderr)
        try:
            recover_one(session_dir, retries=args.retries, retry_wait=args.retry_wait)
        except Exception as exc:
            print(f"FAILED {session_dir}: {exc}", file=sys.stderr)
            failed.append(str(session_dir))

    print(
        f"\ndone. ok={len(remaining) - len(failed)} failed={len(failed)} skipped_ok={len(done)}",
        file=sys.stderr,
    )
    for p in failed:
        print(f"  fail {p}", file=sys.stderr)
    return 1 if failed or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

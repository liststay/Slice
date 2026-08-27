"""Smoke test: cut a short segment from session_20260816_171256."""

from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cutter import divide_output_root, export_segment
from models import Segment
from session_loader import load_session

SESSION = Path(
    "/media/adminpc1/34C618D6C6189A66/头环/baai_ego_task/cs_0001/20260818/session_20260816_171256"
)


def main() -> None:
    session = load_session(SESSION)
    print(
        f"session={session.session_id} duration={session.duration_sec:.3f}s "
        f"fps={session.fps} frames={session.frame_count}"
    )
    assert session.duration_sec > 0, "duration must be > 0"
    assert (session.video_path("left")).is_file()

    output_root = divide_output_root(session)
    assert output_root == SESSION / "divide"
    divide_existed = output_root.exists()

    t1 = min(1.0, session.duration_sec)
    seg = Segment(
        action_zh="冒烟测试动作",
        t0=0.0,
        t1=t1,
        quality="good",
        note="smoke_test",
    )
    out = export_segment(session, seg)
    print(f"exported -> {out}")
    assert out.parent == output_root / "good"
    assert "__" not in out.name, out.name
    assert out.name.startswith(session.session_id + "_")

    try:
        for cam in ("left", "right", "bright", "bleft"):
            assert (out / "videos" / f"{cam}.mp4").is_file(), f"missing video {cam}"
            assert (out / "timestamps" / f"{cam}_timestamps.txt").is_file(), f"missing ts {cam}"
        assert (out / "imu" / "imu0.csv").is_file()
        assert (out / "audio" / "audio.wav").is_file()
        assert (out / "calibrations").is_dir()
        assert (out / "meta.json").is_file()
        assert (out / "cut_info.json").is_file()
        log = output_root / "logs" / "cut_history.jsonl"
        assert log.is_file()
        last = log.read_text(encoding="utf-8").strip().splitlines()[-1]
        rec = json.loads(last)
        assert rec["action_zh"] == "冒烟测试动作"
        assert rec["quality"] == "good"

        # Source media still present
        assert SESSION.is_dir()
        assert (SESSION / "videos" / "left.mp4").is_file()
        print("SMOKE OK")
    finally:
        if out.exists():
            shutil.rmtree(out)
        if not divide_existed and output_root.exists():
            shutil.rmtree(output_root)


if __name__ == "__main__":
    main()

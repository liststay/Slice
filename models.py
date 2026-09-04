"""Data models for video segment annotations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
import re
import uuid

Quality = Literal["good", "bad"]

MAX_SEGMENT_SEC = 600.0  # <= 10 minutes
CAMERAS = ("left", "right", "bright", "bleft")
EXPORT_CAMERAS = CAMERAS


def sanitize_name(name: str) -> str:
    """Replace characters unsafe for filesystem paths (Windows + POSIX)."""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    if cleaned.upper() in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        cleaned = f"_{cleaned}"
    return cleaned[:80] or "unnamed"


@dataclass
class Segment:
    """One cut annotation on a source session."""

    action_zh: str
    t0: float
    t1: float
    quality: Quality = "good"
    note: str = ""
    occlusion: bool = False
    blur: bool = False
    segment_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    exported: bool = False
    output_dir: str = ""

    def duration(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.action_zh.strip():
            errors.append("动作名不能为空")
        if self.t1 <= self.t0:
            errors.append("终点必须大于起点")
        if self.duration() > MAX_SEGMENT_SEC:
            errors.append(f"切分时长必须 <= {MAX_SEGMENT_SEC:.0f} 秒（10 分钟）")
        if self.t0 < 0:
            errors.append("起点不能为负")
        if self.quality not in ("good", "bad"):
            errors.append("质量只能是 good 或 bad")
        return errors

    def folder_name(self, session_id: str, stamp: str | None = None) -> str:
        """session_20260826_140943_收银_20260827155901 — single underscores, no t0-t1 range."""
        action = sanitize_name(self.action_zh)
        stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{session_id}_{action}_{stamp}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration"] = self.duration()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            action_zh=str(data["action_zh"]),
            t0=float(data["t0"]),
            t1=float(data["t1"]),
            quality=data.get("quality", "good"),  # type: ignore[arg-type]
            note=str(data.get("note", "")),
            occlusion=bool(data.get("occlusion", False)),
            blur=bool(data.get("blur", False)),
            segment_id=str(data.get("segment_id", uuid.uuid4().hex[:8])),
            exported=bool(data.get("exported", False)),
            output_dir=str(data.get("output_dir") or ""),
        )

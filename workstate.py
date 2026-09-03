"""Persist unexported drafts and detect already-cut sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import uuid

from models import Segment
from runtime import ensure_writable_dir, write_text_atomic

DRAFT_FILENAME = "draft_segments.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    write_text_atomic(path, text)


def draft_path(session_dir: Path) -> Path:
    return session_dir / "divide" / DRAFT_FILENAME


def count_exported(session_dir: Path) -> int:
    n = 0
    for quality in ("good", "bad"):
        qdir = session_dir / "divide" / quality
        if not qdir.is_dir():
            continue
        n += sum(1 for p in qdir.iterdir() if p.is_dir())
    return n


def count_draft_unexported(session_dir: Path) -> int:
    data = read_draft(session_dir)
    if not data:
        return 0
    return sum(1 for d in data.get("segments") or [] if not d.get("exported"))


REVIEW_FILENAME = "session_review.json"
KEEP_WHOLE = "keep_whole"
REJECT_WHOLE = "reject_whole"


def review_path(session_dir: Path) -> Path:
    return session_dir / "divide" / REVIEW_FILENAME


def read_review(session_dir: Path) -> dict[str, Any] | None:
    path = review_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def review_status(session_dir: Path) -> str:
    data = read_review(session_dir)
    if not data:
        return ""
    return str(data.get("status") or "")


def is_keep_whole(session_dir: Path) -> bool:
    return review_status(session_dir) == KEEP_WHOLE


def is_reject_whole(session_dir: Path) -> bool:
    return review_status(session_dir) == REJECT_WHOLE


def save_review(
    session_dir: Path,
    session_id: str,
    status: str,
    quality: str,
    note: str = "",
) -> Path:
    path = review_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "session_id": session_id,
        "status": status,
        "quality": quality,
        "note": note,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(path, payload)
    return path


def save_keep_whole(
    session_dir: Path,
    session_id: str,
    note: str = "",
) -> Path:
    return save_review(session_dir, session_id, KEEP_WHOLE, "good", note)


def save_reject_whole(
    session_dir: Path,
    session_id: str,
    note: str = "",
) -> Path:
    return save_review(session_dir, session_id, REJECT_WHOLE, "bad", note)


def clear_review(session_dir: Path) -> None:
    path = review_path(session_dir)
    if path.is_file():
        path.unlink()


clear_keep_whole = clear_review


def cut_status_label(
    exported_n: int,
    draft_n: int,
    keep_whole: bool = False,
    reject_whole: bool = False,
) -> str:
    parts: list[str] = []
    if keep_whole:
        parts.append("整段合格")
    if reject_whole:
        parts.append("整段不合格")
    if exported_n:
        parts.append(f"已切分{exported_n}")
    if draft_n:
        parts.append(f"草稿{draft_n}")
    return " · ".join(parts) if parts else "未处理"


def read_draft(session_dir: Path) -> dict[str, Any] | None:
    path = draft_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_draft(
    session_dir: Path,
    session_id: str,
    segments: list[dict[str, Any]],
    form: dict[str, Any] | None = None,
) -> Path:
    path = draft_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_writable_dir(path.parent)
    payload = {
        "version": 1,
        "session_id": session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "segments": segments,
        "form": form or {},
    }
    _write_json(path, payload)
    return path


def clear_draft(session_dir: Path) -> None:
    path = draft_path(session_dir)
    if path.is_file():
        path.unlink()


def list_exported_segments(session_dir: Path) -> list[dict[str, Any]]:
    """Rebuild segment dicts from already exported cut_info.json files."""
    segs: list[dict[str, Any]] = []
    for quality in ("good", "bad"):
        qdir = session_dir / "divide" / quality
        if not qdir.is_dir():
            continue
        for folder in sorted(qdir.iterdir()):
            if not folder.is_dir():
                continue
            info_path = folder / "cut_info.json"
            data: dict[str, Any] = {}
            if info_path.is_file():
                try:
                    loaded = json.loads(info_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
            sid = str(data.get("segment_id") or uuid.uuid4().hex[:8])
            action = str(data.get("action_zh") or folder.name)
            try:
                t0 = float(data.get("t0", 0.0))
                t1 = float(data.get("t1", 0.0))
            except (TypeError, ValueError):
                t0, t1 = 0.0, 0.0
            seg = Segment(
                action_zh=action,
                t0=t0,
                t1=t1,
                quality=quality,  # type: ignore[arg-type]
                note=str(data.get("operator_note") or ""),
                occlusion=bool(data.get("occlusion", False)),
                blur=bool(data.get("blur", False)),
                segment_id=sid,
                exported=True,
                output_dir=str(folder),
            )
            segs.append(seg.to_dict())
    return segs


def load_work(session_dir: Path) -> dict[str, Any]:
    """Draft (unexported work) plus already exported cuts, merged by segment_id."""
    draft = read_draft(session_dir)
    exported = list_exported_segments(session_dir)
    exported_by_id = {d.get("segment_id"): d for d in exported if d.get("segment_id")}
    if draft:
        segs = [dict(d) for d in (draft.get("segments") or [])]
        seen = {d.get("segment_id") for d in segs}
        for d in segs:
            match = exported_by_id.get(d.get("segment_id"))
            if match:
                d["exported"] = True
                d["output_dir"] = match.get("output_dir") or d.get("output_dir") or ""
        for d in exported:
            if d.get("segment_id") not in seen:
                segs.append(d)
        return {"segments": segs, "form": dict(draft.get("form") or {})}
    return {"segments": exported, "form": {}}

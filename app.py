"""Streamlit UI for multi-modal ego session video cutting."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

# Allow running as `streamlit run app.py` from this directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quiet_logs import install as _install_quiet_logs

_install_quiet_logs()

import streamlit as st
import streamlit.components.v1 as components

from cutter import append_cut_log, divide_output_root, export_segment
from media_server import PORT, ensure_server
from models import MAX_SEGMENT_SEC, Segment
from session_loader import discover_sessions
from workstate import (
    clear_draft,
    clear_review,
    count_draft_unexported,
    count_exported,
    cut_status_label,
    is_keep_whole,
    is_reject_whole,
    load_work,
    save_draft,
    save_keep_whole,
    save_reject_whole,
)

DEFAULT_DATA_ROOT = "/media/adminpc1/新加卷K/baai_ego_task"

CUT_RULES_TITLE = "纳入数据集须同时满足（标「好」前看一眼）"
CUT_RULES_MD = """
1. **双手全程可见**：允许单次短暂消失（≤10秒），排除多次进出画面的片段。
2. **动作语义完整**：覆盖完整动作过程（起始→执行→结束），起止姿态清晰可辨。
3. **双条件同时满足**：手部可见性与语义完整性均达标，方可纳入数据集（标「好」）；任一不满足标「坏」。
"""

_player = components.declare_component(
    "cutter_player",
    path=str(Path(__file__).resolve().parent / "player_component"),
)
_bridge = components.declare_component(
    "cutter_bridge",
    path=str(Path(__file__).resolve().parent / "player_component" / "bridge"),
)


def _fmt_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:06.3f}"


def init_state() -> None:
    if "segments" not in st.session_state:
        st.session_state.segments = []  # list[dict]
    if "loaded_session_path" not in st.session_state:
        st.session_state.loaded_session_path = ""
    if "t0_input" not in st.session_state:
        st.session_state.t0_input = 0.0
    if "t1_input" not in st.session_state:
        st.session_state.t1_input = 1.0
    if "last_player_n" not in st.session_state:
        st.session_state.last_player_n = None
    if "editing_id" not in st.session_state:
        st.session_state.editing_id = None
    if "action_zh_input" not in st.session_state:
        st.session_state.action_zh_input = ""
    if "quality_input" not in st.session_state:
        st.session_state.quality_input = "good"
    if "note_input" not in st.session_state:
        st.session_state.note_input = ""
    if "player_seek_t" not in st.session_state:
        st.session_state.player_seek_t = 0.0
    if "player_seek_n" not in st.session_state:
        st.session_state.player_seek_n = 0
    if "add_errors" not in st.session_state:
        st.session_state.add_errors = []
    if "flash" not in st.session_state:
        st.session_state.flash = ""


def _queue_player_seek(t: float) -> None:
    st.session_state.player_seek_t = float(t)
    st.session_state.player_seek_n = int(st.session_state.get("player_seek_n") or 0) + 1


def _seek_to_segment(segment_id: str) -> None:
    for d in st.session_state.segments:
        if d["segment_id"] == segment_id:
            _queue_player_seek(float(d["t0"]))
            break


def _start_edit(segment_id: str) -> None:
    for d in st.session_state.segments:
        if d["segment_id"] != segment_id:
            continue
        st.session_state.editing_id = segment_id
        st.session_state.t0_input = float(d["t0"])
        st.session_state.t1_input = float(d["t1"])
        st.session_state.action_zh_input = str(d.get("action_zh") or "")
        st.session_state.quality_input = d.get("quality") or "good"
        st.session_state.note_input = str(d.get("note") or "")
        _queue_player_seek(float(d["t0"]))
        break


def _cancel_edit() -> None:
    st.session_state.editing_id = None


def _apply_player_mark(event: object, duration: float) -> None:
    """Update t0/t1 from the player without remounting the video iframe."""
    if not isinstance(event, dict):
        return
    if event.get("n") == st.session_state.last_player_n:
        return
    st.session_state.last_player_n = event.get("n")
    t = min(max(0.0, float(event.get("t") or 0.0)), float(duration))
    kind = event.get("kind")
    if kind == "t0":
        st.session_state.t0_input = t
        t1 = float(st.session_state.get("t1_input") or 0.0)
        if t >= t1:
            st.session_state.t1_input = min(duration, round(t + 0.033, 3))
    elif kind == "t1":
        st.session_state.t1_input = t
        t0 = float(st.session_state.get("t0_input") or 0.0)
        if t <= t0:
            st.session_state.t0_input = max(0.0, round(t - 0.033, 3))


def _submit_segment() -> None:
    duration = float(st.session_state.get("_duration") or 0.0)
    t0 = float(st.session_state.get("t0_input") or 0.0)
    t1 = float(st.session_state.get("t1_input") or 0.0)
    action_zh = str(st.session_state.get("action_zh_input") or "").strip()
    quality = st.session_state.get("quality_input") or "good"
    note = str(st.session_state.get("note_input") or "").strip()
    editing_id = st.session_state.get("editing_id")
    seg = Segment(
        action_zh=action_zh,
        t0=t0,
        t1=t1,
        quality=quality,  # type: ignore[arg-type]
        note=note,
    )
    if editing_id:
        seg.segment_id = str(editing_id)
    errs = seg.validate()
    if t1 > duration + 0.05:
        errs.append("终点超出视频时长")
    if errs:
        st.session_state.add_errors = errs
        return
    st.session_state.add_errors = []
    if editing_id:
        updated = False
        for j, d in enumerate(st.session_state.segments):
            if d["segment_id"] == editing_id:
                st.session_state.segments[j] = seg.to_dict()
                updated = True
                break
        if not updated:
            st.session_state.add_errors = ["未找到要编辑的片段，可能已被删除"]
            return
        st.session_state.editing_id = None
        st.session_state.flash = f"已更新：{seg.action_zh} [{t0:.2f}-{t1:.2f}]"
    else:
        st.session_state.segments.append(seg.to_dict())
        st.session_state.flash = f"已添加：{seg.action_zh} [{t0:.2f}-{t1:.2f}]"
    # Keep playhead, but do not bump seek_n: that remounts/aborts the large mp4.
    st.session_state.player_seek_t = float(t1)
    path = Path(st.session_state.get("loaded_session_path") or "")
    if path:
        save_draft(path, path.name, list(st.session_state.segments), _form_state())
        if is_keep_whole(path) or is_reject_whole(path):
            clear_review(path)
            _set_review_flags(path, keep_whole=False, reject_whole=False)


def _remove_export_folder(session_dir: Path, output_dir: str) -> bool:
    """Delete an exported cut folder only if it lives under session/divide/good|bad."""
    if not output_dir:
        return False
    out = Path(output_dir).resolve()
    divide = (session_dir / "divide").resolve()
    try:
        out.relative_to(divide)
    except ValueError:
        return False
    if out.parent.name not in ("good", "bad") or not out.is_dir():
        return False
    shutil.rmtree(out)
    return True


def _delete_segment(segment_id: str) -> None:
    segs = list(st.session_state.segments)
    target = next((d for d in segs if d["segment_id"] == segment_id), None)
    st.session_state.segments = [d for d in segs if d["segment_id"] != segment_id]
    if st.session_state.editing_id == segment_id:
        st.session_state.editing_id = None
    path = Path(st.session_state.get("loaded_session_path") or "")
    removed = False
    if path and target and target.get("exported"):
        removed = _remove_export_folder(path, str(target.get("output_dir") or ""))
        if removed:
            append_cut_log(
                path / "divide" / "logs" / "cut_history.jsonl",
                {
                    "action": "delete_export",
                    "segment_id": segment_id,
                    "action_zh": target.get("action_zh"),
                    "output_dir": target.get("output_dir"),
                    "source_session": str(path),
                },
            )
        for s in st.session_state.get("session_list") or []:
            if str(s.path) == str(path):
                s.exported_count = count_exported(path)
                break
        st.session_state.flash = (
            "已删除导出切片文件夹" if removed else "已从列表移除（未找到对应切片文件夹）"
        )
    if path:
        remaining = list(st.session_state.segments)
        form = _form_state()
        if remaining or _form_dirty(form, float(st.session_state.get("_duration") or 0.0)):
            save_draft(path, path.name, remaining, form)
        else:
            clear_draft(path)


def _form_state() -> dict:
    return {
        "t0": float(st.session_state.get("t0_input") or 0.0),
        "t1": float(st.session_state.get("t1_input") or 0.0),
        "action_zh": str(st.session_state.get("action_zh_input") or ""),
        "quality": st.session_state.get("quality_input") or "good",
        "note": str(st.session_state.get("note_input") or ""),
        "editing_id": st.session_state.get("editing_id"),
    }


def _form_dirty(form: dict, duration: float) -> bool:
    if (form.get("action_zh") or "").strip() or (form.get("note") or "").strip():
        return True
    if form.get("editing_id"):
        return True
    t0 = float(form.get("t0") or 0.0)
    t1 = float(form.get("t1") or 0.0)
    default_t1 = min(1.0, max(0.1, duration))
    return t0 > 0.05 or abs(t1 - default_t1) > 0.08


def _persist_session(session) -> None:
    segs = list(st.session_state.get("segments") or [])
    form = _form_state()
    if segs or _form_dirty(form, session.duration_sec):
        save_draft(session.path, session.session_id, segs, form)
    elif not segs:
        clear_draft(session.path)


def _set_review_flags(
    path: Path, *, keep_whole: bool, reject_whole: bool
) -> None:
    for s in st.session_state.get("session_list") or []:
        if str(s.path) == str(path):
            s.keep_whole = keep_whole
            s.reject_whole = reject_whole
            break


def _mark_keep_whole() -> None:
    path = Path(st.session_state.get("loaded_session_path") or "")
    if not path:
        st.session_state.add_errors = ["未加载 session"]
        return
    note = str(st.session_state.get("note_input") or "").strip()
    save_keep_whole(path, path.name, note=note)
    _set_review_flags(path, keep_whole=True, reject_whole=False)
    st.session_state.add_errors = []
    st.session_state.flash = "已标记：整段合格，无需切分（不复制视频）"


def _mark_reject_whole() -> None:
    path = Path(st.session_state.get("loaded_session_path") or "")
    if not path:
        st.session_state.add_errors = ["未加载 session"]
        return
    note = str(st.session_state.get("note_input") or "").strip()
    save_reject_whole(path, path.name, note=note)
    _set_review_flags(path, keep_whole=False, reject_whole=True)
    st.session_state.add_errors = []
    st.session_state.flash = "已标记：整段不合格（不复制视频）"


def _unmark_review() -> None:
    path = Path(st.session_state.get("loaded_session_path") or "")
    if not path:
        return
    clear_review(path)
    _set_review_flags(path, keep_whole=False, reject_whole=False)
    st.session_state.flash = "已取消整段标记"


def _session_label(s) -> str:
    extra = cut_status_label(
        s.exported_count, s.draft_count, s.keep_whole, s.reject_whole
    )
    return (
        f"{s.session_id}  ({s.duration_sec:.1f}s, {s.frame_count}f)  {extra}"
    )


def _segment_extra(d: dict) -> str:
    note = str(d.get("note") or "").strip()
    if note:
        return note
    out = str(d.get("output_dir") or "").strip()
    if out:
        return Path(out).name
    return ""


def _render_segment_row(i: int, d: dict) -> None:
    sid = d["segment_id"]
    is_editing = st.session_state.editing_id == sid
    exported = bool(d.get("exported"))
    c1, c2, c3, c4, c5 = st.columns([4, 1, 3, 1, 1])
    with c1:
        mark = " ← 编辑中" if is_editing else ""
        label = (
            f"{d['action_zh']}{mark} · {_fmt_time(d['t0'])} → {_fmt_time(d['t1'])} "
            f"({d['t1'] - d['t0']:.2f}s)"
        )
        st.button(
            label,
            key=f"seek_{sid}_{i}",
            on_click=_seek_to_segment,
            args=(sid,),
            use_container_width=True,
            type="tertiary",
            help="点击这一行，播放器跳到该段起点",
        )
    with c2:
        st.write("好" if d["quality"] == "good" else "坏")
    with c3:
        st.caption(_segment_extra(d))
    with c4:
        st.button(
            "编辑",
            key=f"edit_{sid}_{i}",
            on_click=_start_edit,
            args=(sid,),
            disabled=is_editing or exported,
        )
    with c5:
        st.button(
            "删除",
            key=f"del_{sid}_{i}",
            on_click=_delete_segment,
            args=(sid,),
            help="从未导出列表去掉；若已导出，同时删除 divide 下的切片文件夹（原 session 视频不动）。",
        )


def _annotation_ui(session, duration: float, output_root: Path) -> None:
    """Form + list; call this from the right-hand column so it sits beside the player."""
    _apply_player_mark(
        _bridge(
            key="cutter_bridge",
            seek_t=float(st.session_state.get("player_seek_t") or 0.0),
            seek_n=int(st.session_state.get("player_seek_n") or 0),
        ),
        duration,
    )
    st.caption(
        f"已选区间：**{_fmt_time(float(st.session_state.t0_input))}** → "
        f"**{_fmt_time(float(st.session_state.t1_input))}**"
    )
    editing_id = st.session_state.editing_id
    st.subheader("编辑切分片段" if editing_id else "添加切分片段")
    st.info(f"**切分标准**\n{CUT_RULES_MD}")
    if editing_id:
        st.caption("正在修改列表中的一段，保存后会覆盖原条目。播放器已跳到该段起点。")
    t0 = st.number_input(
        "起点 t0 (秒)",
        min_value=0.0,
        max_value=float(duration),
        step=0.033,
        format="%.3f",
        key="t0_input",
    )
    t1 = st.number_input(
        "终点 t1 (秒)",
        min_value=0.0,
        max_value=float(duration),
        step=0.033,
        format="%.3f",
        key="t1_input",
    )

    dur = t1 - t0
    st.write(f"片段时长: **{dur:.2f}s** / 上限 {MAX_SEGMENT_SEC:.0f}s")
    if dur > MAX_SEGMENT_SEC:
        st.error("超过 10 分钟上限")
    elif dur <= 0:
        st.warning("终点须大于起点")

    st.text_input("中文动作名", placeholder="例如：拿杯子", key="action_zh_input")
    st.radio(
        "数据质量（两项都达标才选「好」）",
        ["good", "bad"],
        horizontal=True,
        format_func=lambda x: "好" if x == "good" else "坏",
        key="quality_input",
    )
    st.text_area("备注", height=80, key="note_input")

    save_label = "保存修改" if editing_id else "加入片段列表"
    st.button(
        save_label,
        type="primary",
        use_container_width=True,
        on_click=_submit_segment,
    )
    for e in st.session_state.get("add_errors") or []:
        st.error(e)
    flash = st.session_state.pop("flash", "")
    if flash:
        st.success(flash)
    if editing_id:
        st.button("取消编辑", use_container_width=True, on_click=_cancel_edit)

    if session.keep_whole:
        st.success("已标记为整段合格，无需切分。")
        st.button(
            "取消整段合格标记",
            use_container_width=True,
            on_click=_unmark_review,
        )
    elif session.reject_whole:
        st.warning("已标记为整段不合格。")
        st.button(
            "取消整段不合格标记",
            use_container_width=True,
            on_click=_unmark_review,
        )
    else:
        k1, k2 = st.columns(2)
        with k1:
            st.button(
                "整段合格，无需切分",
                use_container_width=True,
                on_click=_mark_keep_whole,
                help="全程都是好数据、不需要切片。只在 divide/session_review.json 留下标记，不复制视频。",
            )
        with k2:
            st.button(
                "整段不合格",
                use_container_width=True,
                on_click=_mark_reject_whole,
                help="全程都是坏数据、不需要切片。只留下标记，不复制视频；侧边栏会从「未处理」移出。",
            )

    segs = st.session_state.segments
    pending = [d for d in segs if not d.get("exported")]
    exported = [d for d in segs if d.get("exported")]
    st.subheader("片段列表")
    st.caption("点片段名称可跳到该段起点；点「编辑」改草稿；已导出也可点「删除」去掉切片文件夹。")
    if not segs:
        st.info(
            "尚无片段。全程合格或不合格、不需要切时点对应标记；"
            "否则在播放器上标区间后点「加入片段列表」。"
            "未导出的标注会自动保存，下次打开此 session 仍在。"
        )
        return

    if pending:
        st.markdown(f"**未导出草稿（{len(pending)}）**")
        e1, e2 = st.columns(2)
        with e1:
            export_label = f"导出未导出片段（{len(pending)}）"
            if st.button(
                export_label,
                type="primary",
                use_container_width=True,
            ):
                out_paths = []
                progress = st.progress(0.0, text="导出中（视频 / timestamps / IMU）...")
                try:
                    done_n = 0
                    for i, d in enumerate(segs):
                        if d.get("exported"):
                            continue
                        seg = Segment.from_dict(d)
                        out = export_segment(session, seg)
                        st.session_state.segments[i]["exported"] = True
                        st.session_state.segments[i]["output_dir"] = str(out)
                        out_paths.append(out)
                        done_n += 1
                        progress.progress(
                            done_n / max(len(pending), 1),
                            text=f"已导出 {done_n}/{len(pending)}（含 IMU 裁切）",
                        )
                    session.exported_count = count_exported(session.path)
                    _persist_session(session)
                    session.draft_count = count_draft_unexported(session.path)
                    st.success(
                        f"导出完成，共 {len(out_paths)} 段 → `{output_root}`"
                    )
                except Exception as exc:
                    st.exception(exc)
        with e2:
            if st.button("清空未导出草稿", use_container_width=True):
                st.session_state.segments = [d for d in segs if d.get("exported")]
                st.session_state.editing_id = None
                _persist_session(session)
        for i, d in enumerate(list(segs)):
            if d.get("exported"):
                continue
            _render_segment_row(i, d)

    if exported:
        st.markdown(f"**已导出（{len(exported)}）**")
        for i, d in enumerate(list(segs)):
            if not d.get("exported"):
                continue
            _render_segment_row(i, d)


st.set_page_config(page_title="视频切分工具", layout="wide")
if hasattr(st, "fragment"):
    _annotation_ui = st.fragment(_annotation_ui)


def main() -> None:
    init_state()

    st.title("Ego 视频切分工具")
    st.caption(
        "以 left.mp4 为主视角标注；导出时按帧切 left / right 视频和 timestamps，"
        "并按同一时间窗裁切 IMU；calibrations 整份拷贝。切片写入当前 session 的 divide/。"
    )
    st.warning(f"**{CUT_RULES_TITLE}**\n{CUT_RULES_MD}")

    with st.sidebar:
        st.header("路径设置")
        data_root = st.text_input("数据根目录", value=DEFAULT_DATA_ROOT)
        refresh = st.button("刷新 Session 列表", use_container_width=True)

        if refresh or "session_list" not in st.session_state:
            with st.spinner("扫描 session..."):
                st.session_state.session_list = discover_sessions(data_root)

        sessions = st.session_state.get("session_list") or []
        if not sessions:
            st.warning("未找到含 videos/left.mp4 的 session_*")
            st.stop()

        st.caption(
            "已切分 "
            f"{sum(1 for s in sessions if s.exported_count)} 个 · 整段合格 "
            f"{sum(1 for s in sessions if s.keep_whole)} 个 · 整段不合格 "
            f"{sum(1 for s in sessions if s.reject_whole)} 个 · 有草稿 "
            f"{sum(1 for s in sessions if s.draft_count)} 个 · 共 {len(sessions)} 个"
        )
        status = st.radio(
            "切分进度",
            ["全部", "未处理", "有草稿", "已切分", "整段合格", "整段不合格"],
            horizontal=True,
            key="status_filter",
        )

        query = st.text_input(
            "精准查找 Session",
            placeholder="例如 session_20260818_165628 或 165628",
            help="按 session 目录名筛选，支持完整 id 或其中一段日期/时间。",
            key="session_filter",
        ).strip()
        needle = query.lower().removeprefix("session_").strip("_")
        if needle:
            filtered = [
                s
                for s in sessions
                if needle in s.session_id.lower() or needle in str(s.path).lower()
            ]
        else:
            filtered = sessions
        if status == "未处理":
            filtered = [
                s
                for s in filtered
                if s.exported_count == 0
                and s.draft_count == 0
                and not s.keep_whole
                and not s.reject_whole
            ]
        elif status == "有草稿":
            filtered = [s for s in filtered if s.draft_count > 0]
        elif status == "已切分":
            filtered = [s for s in filtered if s.exported_count > 0]
        elif status == "整段合格":
            filtered = [s for s in filtered if s.keep_whole]
        elif status == "整段不合格":
            filtered = [s for s in filtered if s.reject_whole]

        if not filtered:
            st.warning("没有匹配的 session_*，请改筛选条件。")
            st.stop()

        by_path = {str(s.path): s for s in filtered}
        paths = list(by_path)
        if st.session_state.get("session_path_pick") not in by_path:
            st.session_state.session_path_pick = paths[0]
        st.caption(f"当前列表 {len(filtered)} 个")
        chosen_path = st.selectbox(
            "选择 Session",
            paths,
            format_func=lambda p: _session_label(by_path[p]),
            key="session_path_pick",
        )
        session = by_path[chosen_path]
        output_root = divide_output_root(session)

        if st.session_state.loaded_session_path != str(session.path):
            prev_path = st.session_state.loaded_session_path
            if prev_path:
                prev = next((s for s in sessions if str(s.path) == prev_path), None)
                if prev is not None:
                    _persist_session(prev)
            work = load_work(session.path)
            form = work.get("form") or {}
            st.session_state.loaded_session_path = str(session.path)
            st.session_state.segments = work["segments"]
            st.session_state.t0_input = float(form.get("t0") or 0.0)
            st.session_state.t1_input = float(
                form.get("t1") or min(1.0, max(0.1, session.duration_sec))
            )
            st.session_state.action_zh_input = str(form.get("action_zh") or "")
            st.session_state.quality_input = form.get("quality") or "good"
            st.session_state.note_input = str(form.get("note") or "")
            st.session_state.editing_id = None
            st.session_state.last_player_n = None
            st.session_state.player_seek_t = 0.0
            st.session_state.player_seek_n = int(
                st.session_state.get("player_seek_n") or 0
            ) + 1
            st.session_state.add_errors = []
            st.session_state.flash = ""

        st.divider()
        st.markdown(f"**路径**: `{session.path}`")
        st.markdown(f"**输出**: `{output_root}`")
        st.markdown(f"**时长**: {session.duration_sec:.2f}s")
        st.markdown(f"**FPS**: {session.fps:.2f}")
        st.markdown(f"**相机**: {', '.join(session.cameras_present)}")
        live_exported = sum(
            1 for d in st.session_state.segments if d.get("exported")
        ) or session.exported_count
        live_draft = sum(
            1 for d in st.session_state.segments if not d.get("exported")
        )
        st.markdown(
            f"**进度**: {cut_status_label(live_exported, live_draft, session.keep_whole, session.reject_whole)}"
        )
        play_cam = st.selectbox(
            "播放相机（导出切 left / right 视频、timestamps 和 IMU）",
            session.cameras_present or ["left"],
            index=(session.cameras_present or ["left"]).index("left")
            if "left" in (session.cameras_present or ["left"])
            else 0,
        )

    duration = max(session.duration_sec, 0.1)
    st.session_state._duration = duration
    video_path = session.video_path(play_cam)

    live_exported = sum(1 for d in st.session_state.segments if d.get("exported"))
    live_draft = sum(1 for d in st.session_state.segments if not d.get("exported"))
    if session.keep_whole:
        st.info(
            "此 session 已标记为**整段合格，无需切分**。"
            "全程好数据，不导出切片；侧边栏可按「整段合格」筛选。"
            "若之后仍要切段，加入片段列表会自动取消该标记。"
        )
    elif session.reject_whole:
        st.info(
            "此 session 已标记为**整段不合格**。"
            "全程坏数据，不导出切片；侧边栏可按「整段不合格」筛选。"
            "若之后仍要切段，加入片段列表会自动取消该标记。"
        )
    elif live_exported or live_draft or session.exported_count:
        st.info(
            f"此 session 已导出 **{max(live_exported, session.exported_count)}** 段，"
            f"未导出草稿 **{live_draft}** 段。关掉页面再打开会自动恢复，侧边栏可按「已切分 / 有草稿」筛选。"
        )

    col_player, col_form = st.columns([7, 3], gap="large")
    with col_player:
        st.subheader(f"{play_cam}.mp4")
        if video_path.is_file():
            ensure_server(video_path.parent)
            _player(
                file=f"{play_cam}.mp4",
                sid=session.session_id,
                port=PORT,
                seek_t=float(st.session_state.get("player_seek_t") or 0.0),
                seek_n=int(st.session_state.get("player_seek_n") or 0),
                key="cutter_player",
            )
        else:
            st.error(f"找不到视频: {video_path}")
    with col_form:
        _annotation_ui(session, duration, output_root)

    st.divider()
    st.caption(
        f"切分日志：`{output_root / 'logs' / 'cut_history.jsonl'}` · "
        f"未导出草稿：`{output_root / 'draft_segments.json'}` · "
        f"整段标记：`{output_root / 'session_review.json'}` · "
        "原 videos / timestamps / imu / audio / calibrations 不会被修改"
    )


if __name__ == "__main__":
    main()

"""Streamlit UI for multi-modal ego session video cutting."""

from __future__ import annotations

from pathlib import Path
import sys

# Allow running as `streamlit run app.py` from this directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quiet_logs import install as _install_quiet_logs

_install_quiet_logs()

import streamlit as st
import streamlit.components.v1 as components

from cutter import divide_output_root, export_segment
from media_server import PORT, ensure_server
from models import MAX_SEGMENT_SEC, Segment
from session_loader import discover_sessions
from workstate import (
    clear_draft,
    count_draft_unexported,
    count_exported,
    cut_status_label,
    load_work,
    save_draft,
)

DEFAULT_DATA_ROOT = "/media/adminpc1/34C618D6C6189A66/头环/baai_ego_task"

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
        break


def _cancel_edit() -> None:
    st.session_state.editing_id = None


def _keep_player_at(t: float) -> None:
    st.session_state.player_seek_t = max(0.0, float(t))
    st.session_state.player_seek_n = int(st.session_state.get("player_seek_n") or 0) + 1


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
    _keep_player_at(t1)
    path = Path(st.session_state.get("loaded_session_path") or "")
    if path:
        save_draft(path, path.name, list(st.session_state.segments), _form_state())


def _delete_segment(segment_id: str) -> None:
    segs = st.session_state.segments
    st.session_state.segments = [d for d in segs if d["segment_id"] != segment_id]
    if st.session_state.editing_id == segment_id:
        st.session_state.editing_id = None


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


def _session_label(s) -> str:
    extra = cut_status_label(s.exported_count, s.draft_count)
    return (
        f"{s.session_id}  ({s.duration_sec:.1f}s, {s.frame_count}f)  {extra}"
    )


def main() -> None:
    st.set_page_config(page_title="视频切分工具", layout="wide")
    init_state()

    st.title("Ego 视频切分工具")
    st.caption(
        "以 left.mp4 为主视角标注；导出 4 路视频 + timestamps + IMU + audio + "
        "calibrations；切片写入当前 session 下的 divide/。"
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
            f"{sum(1 for s in sessions if s.exported_count)} 个 · 有草稿 "
            f"{sum(1 for s in sessions if s.draft_count)} 个 · 共 {len(sessions)} 个"
        )
        status = st.radio(
            "切分进度",
            ["全部", "未处理", "有草稿", "已切分"],
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
            filtered = [s for s in filtered if s.exported_count == 0 and s.draft_count == 0]
        elif status == "有草稿":
            filtered = [s for s in filtered if s.draft_count > 0]
        elif status == "已切分":
            filtered = [s for s in filtered if s.exported_count > 0]

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
            st.session_state.editing_id = form.get("editing_id")
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
        st.markdown(f"**进度**: {cut_status_label(live_exported, live_draft)}")
        play_cam = st.selectbox(
            "播放相机（导出仍切 4 路）",
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
    if live_exported or live_draft or session.exported_count:
        st.info(
            f"此 session 已导出 **{max(live_exported, session.exported_count)}** 段，"
            f"未导出草稿 **{live_draft}** 段。关掉页面再打开会自动恢复，侧边栏可按「已切分 / 有草稿」筛选。"
        )

    col_player, col_form = st.columns([7, 3], gap="large")

    with col_player:
        st.subheader(f"{play_cam}.mp4")
        if video_path.is_file():
            ensure_server(video_path.parent)
            event = _player(
                file=f"{play_cam}.mp4",
                sid=session.session_id,
                port=PORT,
                seek=float(st.session_state.get("player_seek_t") or 0.0),
                seek_n=int(st.session_state.get("player_seek_n") or 0),
                key="cutter_player",
            )
            if (
                isinstance(event, dict)
                and event.get("n") != st.session_state.last_player_n
            ):
                st.session_state.last_player_n = event.get("n")
                t = float(event.get("t") or 0.0)
                t = min(max(0.0, t), float(duration))
                st.session_state.player_seek_t = t
                if event.get("kind") == "t0":
                    st.session_state.t0_input = t
                elif event.get("kind") == "t1":
                    st.session_state.t1_input = t
        else:
            st.error(f"找不到视频: {video_path}")

        st.caption(
            f"已选区间：**{_fmt_time(float(st.session_state.t0_input))}** → "
            f"**{_fmt_time(float(st.session_state.t1_input))}**"
        )

    with col_form:
        editing_id = st.session_state.editing_id
        st.subheader("编辑切分片段" if editing_id else "添加切分片段")
        st.info(f"**切分标准**\n{CUT_RULES_MD}")
        if editing_id:
            st.caption("正在修改列表中的一段，保存后会覆盖原条目。")
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

        action_zh = st.text_input(
            "中文动作名", placeholder="例如：拿杯子", key="action_zh_input"
        )
        quality = st.radio(
            "数据质量（两项都达标才选「好」）",
            ["good", "bad"],
            horizontal=True,
            format_func=lambda x: "好" if x == "good" else "坏",
            key="quality_input",
        )
        note = st.text_area("备注", height=80, key="note_input")

        save_label = "保存修改" if editing_id else "加入片段列表"
        st.button(
            save_label,
            type="primary",
            use_container_width=True,
            on_click=_submit_segment,
        )
        for e in st.session_state.get("add_errors") or []:
            st.error(e)
        if st.session_state.get("flash"):
            st.success(st.session_state.flash)
            st.session_state.flash = ""
        if editing_id:
            st.button("取消编辑", use_container_width=True, on_click=_cancel_edit)

    st.divider()
    st.subheader("片段列表（同一 session 可多段）")
    segs = st.session_state.segments
    if not segs:
        st.info("尚无片段。在上方标注后点击「加入片段列表」。未导出的标注会自动保存，下次打开此 session 仍在。")
    else:
        for i, d in enumerate(list(segs)):
            sid = d["segment_id"]
            is_editing = st.session_state.editing_id == sid
            c1, c2, c3, c4, c5 = st.columns([3, 1, 2, 1, 1])
            with c1:
                mark = " ← 编辑中" if is_editing else ""
                done = " · 已导出" if d.get("exported") else " · 未导出"
                st.write(
                    f"**{d['action_zh']}**{mark}{done} · {_fmt_time(d['t0'])} → {_fmt_time(d['t1'])} "
                    f"({d['t1'] - d['t0']:.2f}s)"
                )
            with c2:
                st.write("好" if d["quality"] == "good" else "坏")
            with c3:
                st.caption(d.get("note") or d.get("output_dir") or "")
            with c4:
                st.button(
                    "编辑",
                    key=f"edit_{sid}_{i}",
                    on_click=_start_edit,
                    args=(sid,),
                    disabled=is_editing or bool(d.get("exported")),
                )
            with c5:
                st.button(
                    "删除",
                    key=f"del_{sid}_{i}",
                    on_click=_delete_segment,
                    args=(sid,),
                    disabled=bool(d.get("exported")),
                )

        e1, e2 = st.columns(2)
        with e1:
            pending = [d for d in segs if not d.get("exported")]
            export_label = (
                f"导出未导出片段（{len(pending)}）" if pending else "没有未导出片段"
            )
            if st.button(
                export_label,
                type="primary",
                use_container_width=True,
                disabled=not pending,
            ):
                out_paths = []
                progress = st.progress(0.0, text="导出中...")
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
                            text=f"已导出 {done_n}/{len(pending)}",
                        )
                    session.exported_count = count_exported(session.path)
                    _persist_session(session)
                    session.draft_count = count_draft_unexported(session.path)
                    st.success(f"导出完成，共 {len(out_paths)} 段 → `{output_root}`")
                    for p in out_paths:
                        st.code(str(p))
                except Exception as exc:
                    st.exception(exc)
        with e2:
            if st.button("清空未导出草稿", use_container_width=True):
                st.session_state.segments = [
                    d for d in segs if d.get("exported")
                ]
                st.session_state.editing_id = None
                _keep_player_at(st.session_state.get("player_seek_t") or 0.0)
                _persist_session(session)

    st.divider()
    st.caption(
        f"切分日志：`{output_root / 'logs' / 'cut_history.jsonl'}` · "
        f"未导出草稿：`{output_root / 'draft_segments.json'}` · "
        "原 videos / timestamps / imu / audio / calibrations 不会被修改"
    )
    _persist_session(session)


if __name__ == "__main__":
    main()

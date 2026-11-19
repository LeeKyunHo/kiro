"""
gui/gallery.py
Kiro Studio PC 통합 갤러리 · 캐릭터×포즈 매트릭스 · 라이트박스 페이지 (/gallery).

- 모든 에셋 읽기·삭제·복원·재검열·리터치·리롤은 Wave P4-A 백엔드
  (AssetManager, GlobalJobManager, /api/* REST API)를 일관되게 재사용한다.
- Kiro 20개 로스터 및 V2 8개 섹션(emotions, poses, events, romance, h_scenes,
  special_h, scenes_otokonoko, special_otokonoko) 동적 필터링 지원.
- 라이트박스 단축키: Left/Right 화살표 이동, Space 키로 검열본 ↔ _origin 원본 즉시 토글.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any
import urllib.parse

from nicegui import ui

from generator.asset_manager import AssetManager
from generator.option_specs import get_option_spec
from generator.pose_db import V2_BUILTIN_SECTIONS
from gui.components import build_header
from gui.job_manager import GlobalJobManager, global_job_manager
from gui.retouch_editor import (
    RETOUCH_EDITOR_CSS,
    RETOUCH_EDITOR_HTML,
    build_retouch_editor_script,
)
from gui.state import GuiRuntimeState, check_forge_connection

logger = logging.getLogger("generator.gui.gallery")

V2_SECTION_LABELS: dict[str, str] = {
    "all": "전체 V2 섹션",
    "emotions": "감정 (000~019)",
    "poses": "일상 포즈 (020~039)",
    "events": "이벤트 (040~052)",
    "romance": "로맨스 (060~063)",
    "h_scenes": "여성 H-씬 (080~127)",
    "special_h": "여성 스페셜 (130~132)",
    "scenes_otokonoko": "오토코노코 H-씬 (150~197)",
    "special_otokonoko": "오토코노코 스페셜 (133~135)",
}


def build_image_api_url(
    rel_path: str,
    *,
    origin: bool = False,
    version: int | None = None,
) -> str:
    """Wave P4-A /api/image 엔드포인트 URL 을 생성한다 (캐시 버스팅 지원)."""
    q_path = urllib.parse.quote(rel_path, safe="/")
    params = [f"path={q_path}"]
    if origin:
        params.append("origin=1")
    if version is not None:
        params.append(f"t={int(version)}")
    return "/api/image?" + "&".join(params)


def filter_gallery_matrix_by_section(
    matrix: dict[str, Any],
    section: str = "all",
    *,
    char_filter: str = "all",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    AssetManager.build_gallery_matrix() 결과에 V2 섹션 및 캐릭터 필터를 적용해
    (filtered_poses, filtered_characters, flat_items) 3-튜플을 반환한다.
    """
    sec_clean = (section or "all").strip()
    ch_clean = (char_filter or "all").strip().lower()

    all_poses: list[dict[str, Any]] = list(matrix.get("poses", []))
    filtered_poses = (
        all_poses
        if sec_clean == "all"
        else [p for p in all_poses if str(p.get("section", "")) == sec_clean]
    )
    allowed_code_strs = {str(p["code_str"]) for p in filtered_poses}
    pose_meta_by_code = {str(p["code_str"]): p for p in filtered_poses}

    all_chars: list[dict[str, Any]] = list(matrix.get("characters", []))
    filtered_chars = (
        all_chars
        if ch_clean == "all"
        else [
            c
            for c in all_chars
            if str(c.get("prefix", "")).lower() == ch_clean
            or str(c.get("name", "")).lower() == ch_clean
        ]
    )

    flat_items: list[dict[str, Any]] = []
    for ch in filtered_chars:
        assets_by_code = ch.get("assets_by_code") or {}
        for pose in filtered_poses:
            c_str = str(pose["code_str"])
            asset = assets_by_code.get(c_str)
            if asset is not None:
                flat_items.append({
                    **asset,
                    "roster": matrix.get("roster", ""),
                    "char_name": ch.get("name", ch.get("prefix", "")),
                    "prefix": ch.get("prefix", ""),
                    "section": pose.get("section", ""),
                    "pose_label": pose.get("label", asset.get("label", "")),
                    "code_str": c_str,
                })
        # 포즈 DB에 매칭되지 않았지만 섹션이 all 인 에셋도 포함
        if sec_clean == "all":
            seen_rels = {it["rel_path"] for it in flat_items}
            for asset in ch.get("assets") or []:
                if asset["rel_path"] not in seen_rels:
                    c_str = str(asset.get("code_str") or "")
                    p_meta = pose_meta_by_code.get(c_str, {})
                    flat_items.append({
                        **asset,
                        "roster": matrix.get("roster", ""),
                        "char_name": ch.get("name", ch.get("prefix", "")),
                        "prefix": ch.get("prefix", ""),
                        "section": p_meta.get("section", ""),
                        "pose_label": p_meta.get("label", asset.get("label", "")),
                    })

    return filtered_poses, filtered_chars, flat_items


def render_gallery_page(
    base_dir: Path,
    state: GuiRuntimeState | None = None,
    *,
    asset_manager: AssetManager | None = None,
    job_manager: GlobalJobManager | None = None,
) -> None:
    """PC 통합 갤러리 · 캐릭터×포즈 매트릭스 · 라이트박스 페이지를 렌더링한다."""
    assets = asset_manager or AssetManager(base_dir)
    jobs = job_manager or (state.job_manager if state is not None else global_job_manager)
    available_rosters = assets.list_rosters() or ["dark_generals"]
    default_roster = (
        state.current_roster_name
        if (state is not None and state.current_roster_name in available_rosters)
        else available_rosters[0]
    )

    censor_style_spec = get_option_spec("censor_style")
    censor_style_choices = (
        list(censor_style_spec.choices)
        if censor_style_spec and censor_style_spec.choices
        else ["white_silhouette", "black_silhouette", "manga_bar", "manga_glow", "mosaic", "wide_bar"]
    )

    ui.dark_mode(True)
    ui.page_title("Kiro Studio - 통합 갤러리")
    build_header(check_forge_connection, active_route="/gallery", job_manager=jobs)

    retouch_script = build_retouch_editor_script(
        on_saved_js="if (typeof emitEvent === 'function') { emitEvent('kiro_retouch_saved', savedPayload); }",
        on_notify_js=(
            "if (typeof emitEvent === 'function') { emitEvent('kiro_retouch_notify', {msg: msg, is_error: !!isError}); }"
        ),
    )
    ui.add_head_html(RETOUCH_EDITOR_CSS)
    ui.add_body_html(RETOUCH_EDITOR_HTML + "\n" + retouch_script)

    g_state: dict[str, Any] = {
        "roster": default_roster,
        "view_mode": "grid",  # "grid" | "matrix"
        "section": "all",
        "char_filter": "all",
        "censor_style": "white_silhouette",
        "matrix": {},
        "flat_items": [],
        "lb_index": -1,
        "lb_show_origin": False,
        "versions": {},
        "last_trash": None,
    }

    def _version_for(rel_path: str, mtime: float | None = None) -> int:
        if rel_path in g_state["versions"]:
            return int(g_state["versions"][rel_path])
        return int((mtime or time.time()) * 1000)

    def _bump_version(rel_path: str) -> int:
        ts = int(time.time() * 1000)
        g_state["versions"][rel_path] = ts
        return ts

    # ── 라이트박스 모달 ───────────────────────────────────────────
    lightbox_dialog = ui.dialog().props("maximized")
    with lightbox_dialog, ui.card().classes(
        "w-full h-full bg-slate-950/95 text-slate-100 p-4 flex flex-col justify-between"
    ):
        with ui.row().classes("w-full items-center justify-between border-b border-slate-800 pb-2"):
            lb_title = ui.label("").classes("text-base font-bold text-indigo-300")
            with ui.row().classes("items-center gap-2"):
                lb_origin_badge = ui.badge("검열본", color="indigo").classes("text-xs")
                ui.button("닫기 (ESC)", on_click=lightbox_dialog.close).props("outline dense").classes("text-xs")

        with ui.row().classes("w-full flex-1 items-center justify-center gap-4 overflow-hidden my-2"):
            ui.button("◀", on_click=lambda: step_lightbox(-1)).props("round outline").classes("text-lg")
            lb_image = ui.image("").props("fit=contain").classes("max-h-[72vh] max-w-[78vw] rounded-lg shadow-2xl")
            ui.button("▶", on_click=lambda: step_lightbox(1)).props("round outline").classes("text-lg")

        with ui.row().classes("w-full items-center justify-center gap-2 flex-wrap border-t border-slate-800 pt-3"):
            lb_toggle_origin_btn = ui.button(
                "👁️ 원본 비교 (Space)", on_click=lambda: toggle_lightbox_origin()
            ).props("outline dense color=amber").classes("text-xs")
            ui.button("✏️ 인라인 리터치", on_click=lambda: open_lightbox_retouch()).props(
                "dense color=emerald"
            ).classes("text-xs")
            ui.button("🛡️ 즉시 재검열", on_click=lambda: action_recensor_current()).props(
                "outline dense color=indigo"
            ).classes("text-xs")
            ui.button("🔄 원본 복원", on_click=lambda: action_restore_origin_current()).props(
                "outline dense color=amber"
            ).classes("text-xs")
            ui.button("🎲 단건 리롤", on_click=lambda: action_reroll_current()).props(
                "outline dense color=purple"
            ).classes("text-xs")
            ui.button("🗑️ 휴지통 삭제", on_click=lambda: action_delete_current()).props(
                "outline dense color=rose"
            ).classes("text-xs")

    def _current_lb_item() -> dict[str, Any] | None:
        items = g_state["flat_items"]
        idx = g_state["lb_index"]
        if 0 <= idx < len(items):
            return items[idx]
        return None

    def _refresh_lightbox_view() -> None:
        item = _current_lb_item()
        if item is None:
            lightbox_dialog.close()
            return
        show_orig = bool(g_state["lb_show_origin"] and item.get("has_origin"))
        ver = _version_for(item["rel_path"], item.get("mtime"))
        lb_image.set_source(build_image_api_url(item["rel_path"], origin=show_orig, version=ver))
        lb_title.set_text(
            f"[{item['prefix']}] #{item.get('code_str', '')} {item.get('pose_label', '')} "
            f"({g_state['lb_index'] + 1}/{len(g_state['flat_items'])})"
        )
        if show_orig:
            lb_origin_badge.set_text("무검열 원본 (_origin)")
            lb_origin_badge.props("color=amber")
        else:
            lb_origin_badge.set_text("현재 에셋" + (" (_origin 보유)" if item.get("has_origin") else ""))
            lb_origin_badge.props("color=indigo")
        lb_toggle_origin_btn.set_enabled(bool(item.get("has_origin")))

    def open_lightbox_at(idx: int) -> None:
        if not (0 <= idx < len(g_state["flat_items"])):
            return
        g_state["lb_index"] = idx
        g_state["lb_show_origin"] = False
        _refresh_lightbox_view()
        lightbox_dialog.open()

    def step_lightbox(delta: int) -> None:
        total = len(g_state["flat_items"])
        if total == 0:
            return
        g_state["lb_index"] = (g_state["lb_index"] + delta) % total
        g_state["lb_show_origin"] = False
        _refresh_lightbox_view()

    def toggle_lightbox_origin() -> None:
        item = _current_lb_item()
        if item is None or not item.get("has_origin"):
            ui.notify("대응하는 무검열 원본(_origin)이 없습니다.", color="amber")
            return
        g_state["lb_show_origin"] = not g_state["lb_show_origin"]
        _refresh_lightbox_view()

    def open_lightbox_retouch() -> None:
        item = _current_lb_item()
        if item is None:
            return
        title = f"{item['prefix']} #{item.get('code_str', '')} {item.get('pose_label', '')}"
        ui.run_javascript(
            f"window.openKiroRetouchEditor({json.dumps(item['rel_path'])}, "
            f"{json.dumps(title)}, {json.dumps(bool(item.get('has_origin')))});"
        )

    async def _post_api(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        js = f"""
        (async () => {{
            const headers = {{ 'Content-Type': 'application/json' }};
            try {{
                const tok = sessionStorage.getItem('kiro_api_token') || new URLSearchParams(window.location.search).get('token');
                if (tok) headers['X-Kiro-Token'] = tok;
            }} catch (e) {{}}
            const resp = await fetch({json.dumps(endpoint)}, {{
                method: 'POST', headers, body: JSON.stringify({json.dumps(payload)})
            }});
            const data = await resp.json();
            return {{ ok: resp.ok, status: resp.status, data }};
        }})()
        """
        return await ui.run_javascript(js, timeout=30.0)

    async def action_recensor_current() -> None:
        item = _current_lb_item()
        if item is None:
            return
        res = await _post_api("/api/recensor", {"path": item["rel_path"], "style": g_state["censor_style"]})
        if res and res.get("ok"):
            _bump_version(item["rel_path"])
            g_state["lb_show_origin"] = False
            reload_gallery(keep_lightbox=True)
            ui.notify(f"즉시 재검열 완료 ({g_state['censor_style']})", color="emerald")
        else:
            ui.notify(f"재검열 실패: {(res or {}).get('data', {})}", color="rose")

    async def action_restore_origin_current() -> None:
        item = _current_lb_item()
        if item is None:
            return
        res = await _post_api("/api/restore_origin", {"path": item["rel_path"]})
        if res and res.get("ok"):
            _bump_version(item["rel_path"])
            g_state["lb_show_origin"] = False
            reload_gallery(keep_lightbox=True)
            ui.notify("무검열 원본으로 복원 완료 (_origin 유지)", color="emerald")
        else:
            ui.notify(f"원본 복원 실패: {(res or {}).get('data', {})}", color="rose")

    async def action_reroll_current() -> None:
        item = _current_lb_item()
        if item is None:
            return
        res = await _post_api(
            "/api/reroll",
            {
                "roster": item["roster"],
                "char": item["char_name"],
                "code": item["code_str"],
                "censor_style": g_state["censor_style"],
            },
        )
        if res and res.get("ok"):
            job_id = res["data"].get("job", {}).get("job_id", "")
            ui.notify(f"FIFO 대기열에 단건 리롤 등록됨 ({job_id})", color="indigo")
        else:
            ui.notify(f"리롤 등록 실패: {(res or {}).get('data', {})}", color="rose")

    async def action_delete_current() -> None:
        item = _current_lb_item()
        if item is None:
            return
        res = await _post_api("/api/asset/delete", {"path": item["rel_path"]})
        if res and res.get("ok"):
            g_state["last_trash"] = {
                "trash_id": res["data"]["trash_id"],
                "roster": res["data"]["roster"],
                "filename": res["data"]["filename"],
            }
            undo_banner.classes(remove="hidden")
            undo_label.set_text(f"🗑️ 휴지통 이동됨: {res['data']['filename']}")
            lightbox_dialog.close()
            reload_gallery(keep_lightbox=False)
            ui.notify("휴지통으로 안전 이동됨 (상단 Undo 버튼으로 즉시 복원 가능)", color="amber")
        else:
            ui.notify(f"삭제 실패: {(res or {}).get('data', {})}", color="rose")

    async def action_undo_last_delete() -> None:
        last_t = g_state.get("last_trash")
        if not last_t:
            return
        res = await _post_api(
            "/api/asset/restore",
            {"trash_id": last_t["trash_id"], "roster": last_t["roster"]},
        )
        if res and res.get("ok"):
            g_state["last_trash"] = None
            undo_banner.classes(add="hidden")
            reload_gallery(keep_lightbox=False)
            ui.notify(f"복원 완료: {last_t['filename']}", color="emerald")
        else:
            ui.notify("복원 실패", color="rose")

    # 키보드 단축키 (라이트박스 열린 상태에서 Left/Right 이동, Space 원본 비교)
    def _on_key(e: Any) -> None:
        if not lightbox_dialog.value:
            return
        if e.action.keydown:
            if e.key.arrow_left:
                step_lightbox(-1)
            elif e.key.arrow_right:
                step_lightbox(1)
            elif e.key.space:
                toggle_lightbox_origin()

    ui.keyboard(on_key=_on_key)

    def _handle_retouch_saved(e: Any) -> None:
        payload = e.args if hasattr(e, "args") and isinstance(e.args, dict) else {}
        rel = payload.get("rel_path")
        if rel:
            _bump_version(str(rel))
        reload_gallery(keep_lightbox=bool(lightbox_dialog.value))

    def _handle_retouch_notify(e: Any) -> None:
        payload = e.args if hasattr(e, "args") and isinstance(e.args, dict) else {}
        ui.notify(str(payload.get("msg", "")), color="rose" if payload.get("is_error") else "emerald")

    ui.on("kiro_retouch_saved", _handle_retouch_saved)
    ui.on("kiro_retouch_notify", _handle_retouch_notify)

    # ── 메인 레이아웃 ─────────────────────────────────────────────
    with ui.column().classes("w-full max-w-[1520px] mx-auto p-6 gap-4"):
        with ui.card().classes("w-full bg-slate-900/90 border border-slate-800 p-4 rounded-xl"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-3"):
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    ui.label("🖼️ 통합 에셋 갤러리").classes("text-lg font-bold text-indigo-300")
                    roster_sel = ui.select(
                        label="로스터",
                        options=available_rosters,
                        value=g_state["roster"],
                        on_change=lambda e: on_filter_change("roster", e.value),
                    ).props("dense outlined dark").classes("w-40 text-xs")
                    char_sel = ui.select(
                        label="캐릭터",
                        options={"all": "전체 캐릭터"},
                        value="all",
                        on_change=lambda e: on_filter_change("char_filter", e.value),
                    ).props("dense outlined dark").classes("w-40 text-xs")
                    section_sel = ui.select(
                        label="V2 섹션",
                        options=V2_SECTION_LABELS,
                        value="all",
                        on_change=lambda e: on_filter_change("section", e.value),
                    ).props("dense outlined dark").classes("w-48 text-xs")
                    ui.select(
                        label="재검열 스타일",
                        options=censor_style_choices,
                        value=g_state["censor_style"],
                        on_change=lambda e: g_state.__setitem__("censor_style", str(e.value)),
                    ).props("dense outlined dark").classes("w-40 text-xs")

                with ui.row().classes("items-center gap-2"):
                    ui.toggle(
                        {"grid": "그리드 뷰", "matrix": "매트릭스 비교"},
                        value="grid",
                        on_change=lambda e: on_filter_change("view_mode", e.value),
                    ).props("dense dark").classes("text-xs")
                    ui.button("🔄 새로고침", on_click=lambda: reload_gallery()).props(
                        "outline dense"
                    ).classes("text-xs")

        # 휴지통 삭제 직후 즉시 Undo 배너
        undo_banner = ui.row().classes(
            "w-full items-center justify-between bg-amber-950/70 border border-amber-700 px-4 py-2 rounded-lg hidden"
        )
        with undo_banner:
            undo_label = ui.label("").classes("text-xs text-amber-200")
            ui.button("↩️ 삭제 취소 (Undo)", on_click=action_undo_last_delete).props(
                "dense color=amber"
            ).classes("text-xs")

        summary_label = ui.label("").classes("text-xs text-slate-400 font-mono")
        content_container = ui.column().classes("w-full gap-4")

    def on_filter_change(key: str, val: Any) -> None:
        g_state[key] = val
        if key == "roster":
            g_state["char_filter"] = "all"
        reload_gallery(keep_lightbox=False)

    def reload_gallery(*, keep_lightbox: bool = False) -> None:
        try:
            matrix = assets.build_gallery_matrix(g_state["roster"])
        except Exception as exc:
            logger.error("[Gallery] 매트릭스 로드 실패: %s", exc)
            matrix = {"roster": g_state["roster"], "poses": [], "characters": []}
        g_state["matrix"] = matrix

        # 캐릭터 드롭다운 동적 갱신
        char_opts = {"all": f"전체 캐릭터 ({len(matrix.get('characters', []))}명)"}
        for ch in matrix.get("characters", []):
            char_opts[ch["prefix"]] = f"{ch['name']} ({ch['total_assets']}장)"
        char_sel.options = char_opts
        if g_state["char_filter"] not in char_opts:
            g_state["char_filter"] = "all"
        char_sel.value = g_state["char_filter"]

        # V2 섹션 드롭다운 (커스텀 섹션 포함)
        sec_opts = dict(V2_SECTION_LABELS)
        for p in matrix.get("poses", []):
            s = str(p.get("section", ""))
            if s and s not in sec_opts and s not in V2_BUILTIN_SECTIONS:
                sec_opts[s] = f"커스텀 ({s})"
        section_sel.options = sec_opts

        filtered_poses, filtered_chars, flat_items = filter_gallery_matrix_by_section(
            matrix,
            g_state["section"],
            char_filter=g_state["char_filter"],
        )
        g_state["flat_items"] = flat_items
        summary_label.set_text(
            f"로스터: {g_state['roster']} | 캐릭터 {len(filtered_chars)}명 | "
            f"표시 포즈 {len(filtered_poses)}종 | 보유 에셋 {len(flat_items)}장"
        )

        content_container.clear()
        with content_container:
            if g_state["view_mode"] == "matrix":
                _render_matrix_view(filtered_poses, filtered_chars, flat_items)
            else:
                _render_grid_view(flat_items)

        if keep_lightbox:
            _refresh_lightbox_view()

    def _render_grid_view(flat_items: list[dict[str, Any]]) -> None:
        if not flat_items:
            ui.label("표시할 에셋이 없습니다.").classes("text-slate-500 text-sm py-8")
            return
        with ui.element("div").classes("w-full grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-3"):
            for idx, item in enumerate(flat_items):
                ver = _version_for(item["rel_path"], item.get("mtime"))
                img_url = build_image_api_url(item["rel_path"], version=ver)
                with ui.card().classes(
                    "bg-slate-900 border border-slate-800 hover:border-indigo-500 p-1.5 rounded-lg cursor-pointer transition"
                ).on("click", lambda _e, i=idx: open_lightbox_at(i)):
                    ui.image(img_url).props("fit=cover").classes("w-full h-44 rounded")
                    with ui.row().classes("w-full items-center justify-between px-1 pt-1"):
                        ui.label(f"{item['prefix']} #{item.get('code_str', '')}").classes(
                            "text-[11px] font-mono text-indigo-300"
                        )
                        if item.get("has_origin"):
                            ui.badge("원본", color="amber").classes("text-[9px] px-1")
                    ui.label(str(item.get("pose_label") or item.get("filename"))).classes(
                        "text-[11px] text-slate-300 truncate px-1"
                    )

    def _render_matrix_view(
        filtered_poses: list[dict[str, Any]],
        filtered_chars: list[dict[str, Any]],
        flat_items: list[dict[str, Any]],
    ) -> None:
        if not filtered_chars or not filtered_poses:
            ui.label("비교할 캐릭터 또는 포즈가 없습니다.").classes("text-slate-500 text-sm py-8")
            return
        idx_by_rel = {it["rel_path"]: i for i, it in enumerate(flat_items)}
        with ui.scroll_area().classes("w-full h-[72vh]"):
            with ui.column().classes("gap-2 min-w-max"):
                with ui.row().classes("items-center gap-2 pb-1 border-b border-slate-800"):
                    ui.label("포즈 코드").classes("w-36 text-xs font-bold text-slate-300")
                    for ch in filtered_chars:
                        ui.label(f"{ch['name']} ({ch['total_assets']})").classes(
                            "w-28 text-center text-xs font-bold text-indigo-300 truncate"
                        )
                for pose in filtered_poses:
                    c_str = str(pose["code_str"])
                    with ui.row().classes("items-center gap-2 py-1 border-b border-slate-900"):
                        with ui.column().classes("w-36 gap-0"):
                            ui.label(f"#{c_str} {pose['label']}").classes("text-xs text-slate-200 truncate")
                            ui.label(str(pose["section"])).classes("text-[10px] text-slate-500")
                        for ch in filtered_chars:
                            asset = (ch.get("assets_by_code") or {}).get(c_str)
                            if asset is not None:
                                ver = _version_for(asset["rel_path"], asset.get("mtime"))
                                url = build_image_api_url(asset["rel_path"], version=ver)
                                flat_i = idx_by_rel.get(asset["rel_path"], -1)
                                ui.image(url).props("fit=cover").classes(
                                    "w-28 h-36 rounded border border-slate-800 hover:border-indigo-400 cursor-pointer"
                                ).on("click", lambda _e, i=flat_i: open_lightbox_at(i))
                            else:
                                async def _enqueue_missing(
                                    _r: str = g_state["roster"],
                                    _c: str = ch["name"],
                                    _code: str = c_str,
                                ) -> None:
                                    res = await _post_api(
                                        "/api/reroll",
                                        {"roster": _r, "char": _c, "code": _code, "censor_style": g_state["censor_style"]},
                                    )
                                    if res and res.get("ok"):
                                        ui.notify(f"누락 포즈 #{_code} ({_c}) 리롤 큐 등록됨", color="indigo")

                                ui.button("누락 · 생성", on_click=_enqueue_missing).props(
                                    "outline dense color=slate"
                                ).classes("w-28 h-36 text-[10px] text-slate-500")

    reload_gallery()


__all__ = [
    "V2_SECTION_LABELS",
    "build_image_api_url",
    "filter_gallery_matrix_by_section",
    "render_gallery_page",
]

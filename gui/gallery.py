"""
gui/gallery.py
Kiro Studio PC 통합 갤러리 · 캐릭터×포즈 매트릭스 · 라이트박스 페이지 (/gallery).

- 모든 에셋 읽기·삭제·복원·재검열·리터치·리롤은 Wave P4-A 백엔드
  (AssetManager, GlobalJobManager, /api/* REST API)를 일관되게 재사용한다.
- 캐릭터 드롭다운 초기값: 에셋이 존재하는 첫 번째 캐릭터를 기본 선택하여 초기 전체 로딩 병목을 방지한다.
- 대량 이미지 버퍼링 방지: 한 번에 모든 DOM/이미지를 생성하지 않고 24장 단위 비동기 순차 스트리밍
  (Progressive Sequential Chunk Loading) + 브라우저 네이티브 lazy decoding 을 적용한다.
- 라이트박스 단축키: Left/Right 화살표 이동, Space 키로 검열본 ↔ _origin 원본 즉시 토글.
"""

from __future__ import annotations

import html
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

GRID_CHUNK_SIZE: int = 24
MATRIX_ROW_CHUNK_SIZE: int = 16

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


def pick_default_character(characters: list[dict[str, Any]]) -> str:
    """
    로스터 캐릭터 목록에서 초기 기본 선택할 캐릭터 prefix 를 반환한다.
    에셋이 1장 이상 있는 첫 번째 캐릭터를 우선 선택하고, 없으면 첫 캐릭터(또는 'all')를 반환한다.
    """
    for ch in characters:
        if int(ch.get("total_assets", 0)) > 0 and ch.get("prefix"):
            return str(ch["prefix"])
    if characters and characters[0].get("prefix"):
        return str(characters[0]["prefix"])
    return "all"


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
        else ("spy" if "spy" in available_rosters else available_rosters[0])
    )

    censor_style_spec = get_option_spec("censor_style")
    censor_style_choices = (
        list(censor_style_spec.choices)
        if censor_style_spec and censor_style_spec.choices
        else ["white_silhouette", "black_silhouette", "manga_bar", "manga_glow", "mosaic", "wide_bar"]
    )

    ui.dark_mode(True)
    ui.page_title("Kiro Studio - 통합 갤러리")
    top_header = build_header(check_forge_connection, active_route="/gallery", job_manager=jobs)

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
        "char_filter": "",    # 빈 문자열이면 로스터의 첫 번째 보유 캐릭터 자동 선택
        "censor_style": "white_silhouette",
        "matrix": {},
        "flat_items": [],
        "lb_index": -1,
        "lb_show_origin": False,
        "versions": {},
        "last_trash": None,
        "render_seq": 0,
        "chunk_timer": None,
    }

    def _cancel_chunk_timer() -> None:
        old_timer = g_state.get("chunk_timer")
        if old_timer is not None:
            try:
                old_timer.cancel()
            except Exception:
                pass
            g_state["chunk_timer"] = None

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

    # ── 상단 고정 필터 바 (스크롤 시에도 상단 헤더에 항상 고정) ─────
    with top_header:
        with ui.row().classes(
            "w-full max-w-[1520px] mx-auto items-center justify-between flex-wrap gap-3 pt-2 border-t border-slate-800/90"
        ):
            with ui.row().classes("items-center gap-2.5 flex-wrap"):
                ui.label("🖼️ 갤러리 필터").classes("text-sm font-bold text-indigo-300 mr-1")
                roster_sel = ui.select(
                    label="로스터",
                    options=available_rosters,
                    value=g_state["roster"],
                    on_change=lambda e: on_filter_change("roster", e.value),
                ).props("dense outlined dark options-dense").classes("w-40 text-xs")
                char_sel = ui.select(
                    label="캐릭터",
                    options={"all": "전체 캐릭터"},
                    value="all",
                    on_change=lambda e: on_filter_change("char_filter", e.value),
                ).props("dense outlined dark options-dense").classes("w-48 text-xs")
                section_sel = ui.select(
                    label="V2 포즈 섹션",
                    options=V2_SECTION_LABELS,
                    value="all",
                    on_change=lambda e: on_filter_change("section", e.value),
                ).props("dense outlined dark options-dense").classes("w-48 text-xs")
                ui.select(
                    label="재검열 스타일",
                    options=censor_style_choices,
                    value=g_state["censor_style"],
                    on_change=lambda e: g_state.__setitem__("censor_style", str(e.value)),
                ).props("dense outlined dark options-dense").classes("w-40 text-xs")

            with ui.row().classes("items-center gap-2 flex-wrap"):
                summary_label = ui.label("").classes("text-xs text-slate-300 font-mono mr-1")
                stream_badge = ui.badge("준비 중", color="slate").classes("text-xs font-mono")
                ui.toggle(
                    {"grid": "그리드 뷰", "matrix": "매트릭스 비교"},
                    value="grid",
                    on_change=lambda e: on_filter_change("view_mode", e.value),
                ).props("dense dark").classes("text-xs")
                ui.button("🔄 새로고침", on_click=lambda: reload_gallery()).props(
                    "outline dense"
                ).classes("text-xs")

        undo_banner = ui.row().classes(
            "w-full max-w-[1520px] mx-auto items-center justify-between bg-amber-950/90 border border-amber-700 px-3 py-1.5 rounded-lg hidden"
        )
        with undo_banner:
            undo_label = ui.label("").classes("text-xs text-amber-200")
            ui.button("↩️ 삭제 취소 (Undo)", on_click=action_undo_last_delete).props(
                "dense color=amber"
            ).classes("text-xs")

    # ── 메인 갤러리 콘텐츠 영역 ───────────────────────────────────
    with ui.column().classes("w-full max-w-[1520px] mx-auto p-6 gap-4"):
        content_container = ui.column().classes("w-full gap-4")

    _suppress_filter_event = {"active": False}

    def on_filter_change(key: str, val: Any) -> None:
        if _suppress_filter_event["active"]:
            return
        if g_state.get(key) == val:
            return
        g_state[key] = val
        if key == "roster":
            # 로스터 변경 시 해당 로스터의 첫 번째 캐릭터로 자동 리셋 (전체 이미지 동시 로딩 방지)
            g_state["char_filter"] = ""
        reload_gallery(keep_lightbox=False)

    def reload_gallery(*, keep_lightbox: bool = False) -> None:
        _cancel_chunk_timer()
        g_state["render_seq"] += 1
        my_seq = g_state["render_seq"]

        try:
            matrix = assets.build_gallery_matrix(g_state["roster"])
        except Exception as exc:
            logger.error("[Gallery] 매트릭스 로드 실패: %s", exc)
            matrix = {"roster": g_state["roster"], "poses": [], "characters": []}
        g_state["matrix"] = matrix

        all_chars = matrix.get("characters", [])
        char_opts: dict[str, str] = {}
        for ch in all_chars:
            char_opts[ch["prefix"]] = f"{ch['name']} ({ch['total_assets']}장)"
        total_roster_assets = sum(int(c.get("total_assets", 0)) for c in all_chars)
        char_opts["all"] = f"전체 캐릭터 ({len(all_chars)}명 · {total_roster_assets}장)"

        # 초기 진입 또는 로스터 변경 시 첫 번째 보유 캐릭터를 기본 선택
        if not g_state["char_filter"] or g_state["char_filter"] not in char_opts:
            g_state["char_filter"] = pick_default_character(all_chars)

        _suppress_filter_event["active"] = True
        try:
            char_sel.options = char_opts
            char_sel.value = g_state["char_filter"]
            char_sel.update()

            sec_opts = dict(V2_SECTION_LABELS)
            for p in matrix.get("poses", []):
                s = str(p.get("section", ""))
                if s and s not in sec_opts and s not in V2_BUILTIN_SECTIONS:
                    sec_opts[s] = f"커스텀 ({s})"
            section_sel.options = sec_opts
            section_sel.update()
        finally:
            _suppress_filter_event["active"] = False

        # 매트릭스 뷰에서는 캐릭터가 단일 선택되어 있어도 전체 캐릭터 가로 비교가 가능하도록 처리
        effective_char_filter = g_state["char_filter"]
        filtered_poses, filtered_chars, flat_items = filter_gallery_matrix_by_section(
            matrix,
            g_state["section"],
            char_filter=effective_char_filter,
        )
        g_state["flat_items"] = flat_items
        summary_label.set_text(
            f"로스터: {g_state['roster']} | 선택: {char_opts.get(effective_char_filter, effective_char_filter)} | "
            f"포즈 {len(filtered_poses)}종 | 에셋 {len(flat_items)}장"
        )

        content_container.clear()
        with content_container:
            if g_state["view_mode"] == "matrix":
                _render_matrix_view_progressive(filtered_poses, filtered_chars, flat_items, my_seq)
            else:
                _render_grid_view_progressive(flat_items, my_seq)

        if keep_lightbox:
            _refresh_lightbox_view()

    def _append_grid_card(grid_el: Any, idx: int, item: dict[str, Any]) -> None:
        ver = _version_for(item["rel_path"], item.get("mtime"))
        img_url = html.escape(build_image_api_url(item["rel_path"], version=ver), quote=True)
        alt_txt = html.escape(str(item.get("filename", "")), quote=True)
        with grid_el:
            with ui.card().classes(
                "bg-slate-900 border border-slate-800 hover:border-indigo-500 p-1.5 rounded-lg cursor-pointer transition"
            ).on("click", lambda _e, i=idx: open_lightbox_at(i)):
                ui.html(
                    f'<img src="{img_url}" alt="{alt_txt}" loading="lazy" decoding="async" '
                    f'style="width:100%;height:11rem;object-fit:cover;border-radius:0.375rem;background:#020617;display:block;" />'
                )
                with ui.row().classes("w-full items-center justify-between px-1 pt-1"):
                    ui.label(f"{item['prefix']} #{item.get('code_str', '')}").classes(
                        "text-[11px] font-mono text-indigo-300"
                    )
                    if item.get("has_origin"):
                        ui.badge("원본", color="amber").classes("text-[9px] px-1")
                ui.label(str(item.get("pose_label") or item.get("filename"))).classes(
                    "text-[11px] text-slate-300 truncate px-1"
                )

    def _render_grid_view_progressive(flat_items: list[dict[str, Any]], seq: int) -> None:
        total = len(flat_items)
        if total == 0:
            stream_badge.set_text("에셋 0장")
            stream_badge.props("color=slate")
            ui.label("표시할 에셋이 없습니다.").classes("text-slate-500 text-sm py-8")
            return

        grid_el = ui.element("div").classes(
            "w-full grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-3"
        )
        cursor = {"idx": 0}

        def _pump_next_batch() -> None:
            if g_state["render_seq"] != seq:
                _cancel_chunk_timer()
                return
            start = cursor["idx"]
            end = min(total, start + GRID_CHUNK_SIZE)
            for i in range(start, end):
                _append_grid_card(grid_el, i, flat_items[i])
            cursor["idx"] = end
            if end < total:
                stream_badge.set_text(f"⚡ 순차 로딩 중 ({end}/{total}장)")
                stream_badge.props("color=indigo")
            else:
                stream_badge.set_text(f"✅ 로드 완료 ({total}장)")
                stream_badge.props("color=emerald")
                _cancel_chunk_timer()

        # 1차 청크(24장) 즉시 표시 후 나머지는 60ms 간격 비동기 순차 스트리밍
        _pump_next_batch()
        if cursor["idx"] < total:
            g_state["chunk_timer"] = ui.timer(0.06, _pump_next_batch)

    def _render_matrix_view_progressive(
        filtered_poses: list[dict[str, Any]],
        filtered_chars: list[dict[str, Any]],
        flat_items: list[dict[str, Any]],
        seq: int,
    ) -> None:
        if not filtered_chars or not filtered_poses:
            stream_badge.set_text("비교 대상 없음")
            stream_badge.props("color=slate")
            ui.label("비교할 캐릭터 또는 포즈가 없습니다.").classes("text-slate-500 text-sm py-8")
            return

        idx_by_rel = {it["rel_path"]: i for i, it in enumerate(flat_items)}
        total_rows = len(filtered_poses)

        with ui.scroll_area().classes("w-full h-[72vh]"):
            rows_col = ui.column().classes("gap-2 min-w-max")
            with rows_col:
                with ui.row().classes("items-center gap-2 pb-1 border-b border-slate-800"):
                    ui.label("포즈 코드").classes("w-36 text-xs font-bold text-slate-300")
                    for ch in filtered_chars:
                        ui.label(f"{ch['name']} ({ch['total_assets']})").classes(
                            "w-28 text-center text-xs font-bold text-indigo-300 truncate"
                        )

        row_cursor = {"idx": 0}

        def _append_matrix_row(pose: dict[str, Any]) -> None:
            c_str = str(pose["code_str"])
            with rows_col:
                with ui.row().classes("items-center gap-2 py-1 border-b border-slate-900"):
                    with ui.column().classes("w-36 gap-0"):
                        ui.label(f"#{c_str} {pose['label']}").classes("text-xs text-slate-200 truncate")
                        ui.label(str(pose["section"])).classes("text-[10px] text-slate-500")
                    for ch in filtered_chars:
                        asset = (ch.get("assets_by_code") or {}).get(c_str)
                        if asset is not None:
                            ver = _version_for(asset["rel_path"], asset.get("mtime"))
                            url = html.escape(build_image_api_url(asset["rel_path"], version=ver), quote=True)
                            flat_i = idx_by_rel.get(asset["rel_path"], -1)
                            cell = ui.element("div").classes(
                                "w-28 h-36 rounded border border-slate-800 hover:border-indigo-400 cursor-pointer overflow-hidden"
                            ).on("click", lambda _e, i=flat_i: open_lightbox_at(i))
                            with cell:
                                ui.html(
                                    f'<img src="{url}" loading="lazy" decoding="async" '
                                    f'style="width:100%;height:100%;object-fit:cover;background:#020617;display:block;" />'
                                )
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

        def _pump_matrix_rows() -> None:
            if g_state["render_seq"] != seq:
                _cancel_chunk_timer()
                return
            start = row_cursor["idx"]
            end = min(total_rows, start + MATRIX_ROW_CHUNK_SIZE)
            for r_i in range(start, end):
                _append_matrix_row(filtered_poses[r_i])
            row_cursor["idx"] = end
            if end < total_rows:
                stream_badge.set_text(f"⚡ 매트릭스 순차 로딩 ({end}/{total_rows}행)")
                stream_badge.props("color=indigo")
            else:
                stream_badge.set_text(f"✅ 매트릭스 완료 ({total_rows}행)")
                stream_badge.props("color=emerald")
                _cancel_chunk_timer()

        _pump_matrix_rows()
        if row_cursor["idx"] < total_rows:
            g_state["chunk_timer"] = ui.timer(0.06, _pump_matrix_rows)

    reload_gallery()


__all__ = [
    "GRID_CHUNK_SIZE",
    "MATRIX_ROW_CHUNK_SIZE",
    "V2_SECTION_LABELS",
    "build_image_api_url",
    "filter_gallery_matrix_by_section",
    "pick_default_character",
    "render_gallery_page",
]

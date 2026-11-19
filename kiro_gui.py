"""
kiro_gui.py
Kiro Studio - SD WebUI Forge 기반 초경량 캐릭터 에셋 배치 생성기 GUI (NiceGUI).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from nicegui import app, run, ui

from generator.checkpoint import (
    load_checkpoint_profiles,
    match_checkpoint_profile,
    normalize_webui_sampler,
    normalize_webui_scheduler,
)
from generator.config import REF_WEIGHT_DEFAULT, REFERENCES_DIRNAME, get_api_url
from generator.models import (
    BatchResult,
    CharacterConfig,
    GenerationOptions,
    PoseDatabase,
    RosterPaths,
    RunHooks,
)
from generator.pose_db import (
    expand_v2_mode,
    load_pose_db,
    looks_like_code_expr,
    parse_codes_expr,
    resolve_targets,
)
from generator.precheck import run_precheck
from generator.reference import find_reference_candidates
from generator.reporter import build_genit_block, code_width, open_in_explorer
from generator.roster import RosterPathManager, _resolve_default_mode, apply_character_to_args, load_character
from generator.runner import execute, run_all_chars
from generator.webui_client import interrupt_forge


SETTINGS_FILENAME = "gui_settings.json"
DEFAULT_SETTINGS: dict[str, Any] = {
    "steps": 30,
    "sampler": "DPM++ 2M",
    "scheduler": "Karras",
    "cfg_scale": 8.0,
    "use_reference": True,
    "naming": "hybrid",
    "overwrite": True,
    "auto_qc": False,
    "max_retries": 3,
    "censor": False,
    "censor_style": "white_silhouette",
}


def load_gui_settings(base_dir: Path) -> dict[str, Any]:
    path = base_dir / SETTINGS_FILENAME
    merged = dict(DEFAULT_SETTINGS)
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            merged.update(data)
        except Exception as e:
            print(f"[WARN] 설정 파일 로드 실패: {e}")
    return merged


def save_gui_settings(base_dir: Path, data: dict[str, Any]) -> None:
    path = base_dir / SETTINGS_FILENAME
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] 설정 파일 저장 실패: {e}")


class KiroGuiState:
    ALL_CHARS_OPTION = "🌟 [ALL] 전체 캐릭터 일괄 순차 생성"

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.rpm = RosterPathManager(base_dir)
        self.available_rosters = self.rpm.list_available_rosters() or ["dark_generals"]
        self.current_roster_name = self.available_rosters[0]
        self.roster: RosterPaths = self.rpm.resolve_roster(self.current_roster_name)
        self.ckpt_profiles = load_checkpoint_profiles(base_dir)

        self.characters: list[str] = []
        self.selected_char_name: str = ""
        self.char_cfg: CharacterConfig | None = None
        self.ref_path: Path | None = None

        self.db: PoseDatabase = load_pose_db(base_dir, self.roster.events_file)
        self.is_running: bool = False
        self.cancel_requested: bool = False
        self.active_run_id: int = 0
        self.current_active_prefix: str = ""
        self._mounted_prefixes: set[str] = set()

    def ensure_static_mounted(self, prefix: str, save_dir: Path):
        if prefix not in self._mounted_prefixes:
            app.add_static_files(f"/assets/{prefix}", str(save_dir))
            self._mounted_prefixes.add(prefix)

    def update_roster(self, roster_name: str):
        self.current_roster_name = roster_name
        self.roster = self.rpm.resolve_roster(roster_name)
        self.db = load_pose_db(self.base_dir, self.roster.events_file)
        self.refresh_characters()

    def refresh_characters(self):
        chars_dir = self.roster.characters_dir
        if chars_dir.is_dir():
            files = sorted(chars_dir.glob("*.json"))
            self.characters = [f.stem for f in files]
        else:
            self.characters = []
        # 기본 선택은 첫 번째 캐릭터
        if not self.selected_char_name or (
            self.selected_char_name != self.ALL_CHARS_OPTION and self.selected_char_name not in self.characters
        ):
            self.selected_char_name = self.characters[0] if self.characters else ""
        self.load_selected_char()

    def is_all_chars(self) -> bool:
        return self.selected_char_name == self.ALL_CHARS_OPTION

    def load_selected_char(self):
        if self.is_all_chars():
            self.char_cfg = None
            self.ref_path = None
            return

        if not self.selected_char_name:
            self.char_cfg = None
            self.ref_path = None
            return

        try:
            self.char_cfg = load_character(self.roster.characters_dir, self.selected_char_name)
            candidates = find_reference_candidates(self.roster.references_dir, self.char_cfg.prefix)
            self.ref_path = candidates[0] if candidates else None
        except Exception as e:
            print(f"[ERROR] 캐릭터 로드 실패: {e}")
            self.char_cfg = None
            self.ref_path = None


_GLOBAL_STATE: KiroGuiState | None = None


_ADULT_SCENE_ALIASES: frozenset[str] = frozenset({
    "48-99",
    "148-199",
    "80-127,130-132",
    "133-135,150-197",
    "h_scenes",
    "scenes_otokonoko",
    "h_scenes,special_h",
    "scenes_otokonoko,special_otokonoko",
})


def resolve_gui_mode_and_codes(
    raw_expr: str,
    char_gender: str,
    available_sections: set[str] | None = None,
) -> tuple[str, str | None]:
    """GUI 범위 입력값을 V2 파이프라인 (mode, codes) 쌍으로 정규화한다."""
    expr = (raw_expr or "").strip() or "all"
    is_otk = str(char_gender).lower() in ("otokonoko", "femboy")
    if expr in _ADULT_SCENE_ALIASES:
        candidates = (
            ["scenes_otokonoko", "special_otokonoko"]
            if is_otk
            else ["h_scenes", "special_h"]
        )
        if available_sections is not None:
            filtered = [s for s in candidates if s in available_sections]
            if filtered:
                return (",".join(filtered), None)
        return (",".join(candidates), None)
    if expr == "all" or not looks_like_code_expr(expr):
        return (expr, None)
    return ("all", expr)


def check_forge_connection() -> bool:
    try:
        r = requests.get(get_api_url("/sdapi/v1/sd-models"), timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def build_gui(base_dir: Path):
    global _GLOBAL_STATE
    settings = load_gui_settings(base_dir)
    if _GLOBAL_STATE is None:
        _GLOBAL_STATE = KiroGuiState(base_dir)
    state = _GLOBAL_STATE
    state.db = load_pose_db(base_dir, state.roster.events_file)
    state.refresh_characters()

    ui.dark_mode(True)
    ui.page_title("Kiro Studio - 배치 생성기")

    # ── 상단 헤더 ──
    with ui.header().classes("bg-slate-900 border-b border-slate-800 px-6 py-3 flex items-center justify-between"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("auto_awesome", size="2rem").classes("text-indigo-400")
            ui.label("Kiro Batch Studio").classes("text-xl font-bold text-white tracking-wide")
            ui.badge("Forge SDXL", color="indigo-600").classes("text-xs font-mono")

        with ui.row().classes("items-center gap-3"):
            forge_badge = ui.badge("Forge 연결 확인 중...", color="slate").classes("text-xs font-semibold px-2.5 py-1")

            async def refresh_forge_badge():
                online = await run.io_bound(check_forge_connection)
                if online:
                    forge_badge.set_text("✅ Forge 연결 정상")
                    forge_badge.props("color=emerald")
                else:
                    forge_badge.set_text("⚠️ Forge 미감지 (WebUI 확인 필요)")
                    forge_badge.props("color=amber")

            ui.timer(3.0, refresh_forge_badge)
            ui.timer(0.1, refresh_forge_badge, once=True)

    # ── 본문 2단 레이아웃 ──
    with ui.row().classes("w-full p-6 gap-6 items-start no-wrap"):
        # ── [좌측] 제어 & 설정 패널 (폭 45%) ──
        with ui.card().classes("w-[45%] bg-slate-900/80 border border-slate-800 p-5 rounded-xl shadow-lg"):
            ui.label("⚙️ 생성 제어 패널").classes("text-lg font-bold text-indigo-300 mb-3")

            # 1. 로스터 및 캐릭터 선택
            with ui.row().classes("w-full gap-4"):
                roster_select = ui.select(
                    label="프로젝트 (로스터)",
                    options=state.available_rosters,
                    value=state.current_roster_name,
                ).classes("w-1/2")

                char_select_options = [KiroGuiState.ALL_CHARS_OPTION] + state.characters
                char_select = ui.select(
                    label="대상 캐릭터",
                    options=char_select_options,
                    value=state.selected_char_name,
                ).classes("w-1/2")

            # 2. 레퍼런스(IP-Adapter) 사용 토글 & 상태 배지
            with ui.row().classes("w-full items-center justify-between mt-2"):
                ref_switch = ui.switch(
                    "레퍼런스(IP-Adapter) 사용",
                    value=settings.get("use_reference", True),
                ).classes("text-xs font-semibold text-slate-200")

            ref_badge = ui.badge("참조 이미지 확인 중", color="slate").classes("w-full py-1.5 text-center mt-1")
            char_info_label = ui.label("").classes("text-xs text-slate-400 mt-1 line-clamp-2")

            def update_ref_display():
                use_ref = ref_switch.value
                if state.is_all_chars():
                    char_info_label.text = f"✨ 로스터 내 모든 캐릭터 ({len(state.characters)}명)를 순차적으로 일괄 생성합니다."
                    if use_ref:
                        ref_badge.text = f"✅ 레퍼런스 자동 연동 (캐릭터별 {REFERENCES_DIRNAME}/[prefix].* 탐색)"
                        ref_badge.props("color=emerald-800 text-color=emerald-200")
                    else:
                        ref_badge.text = "⚠️ 레퍼런스 비활성화 (순수 텍스트 프롬프트 생성 - 연산 고속화)"
                        ref_badge.props("color=slate-800 text-color=slate-300")
                elif state.char_cfg:
                    char_info_label.text = f"외형: {state.char_cfg.char_prompt[:80]}..."
                    if not use_ref:
                        ref_badge.text = "⚠️ 레퍼런스 비활성화 (순수 텍스트 프롬프트 생성)"
                        ref_badge.props("color=slate-800 text-color=slate-300")
                    elif state.ref_path:
                        ref_badge.text = f"✅ 레퍼런스 연동됨: {state.ref_path.name}"
                        ref_badge.props("color=emerald-800 text-color=emerald-200")
                    else:
                        ref_badge.text = f"⚠️ 레퍼런스 없음 ({REFERENCES_DIRNAME}/{state.char_cfg.prefix}.*) - 순수 텍스트 생성"
                        ref_badge.props("color=slate-800 text-color=slate-300")
                else:
                    char_info_label.text = "캐릭터 설정을 로드할 수 없습니다."
                    ref_badge.text = "캐릭터 미선택"
                    ref_badge.props("color=slate")

            def on_char_change():
                state.selected_char_name = char_select.value
                state.load_selected_char()
                update_ref_display()

            def on_roster_change():
                state.update_roster(roster_select.value)
                new_opts = [KiroGuiState.ALL_CHARS_OPTION] + state.characters
                char_select.options = new_opts
                char_select.value = state.selected_char_name
                on_char_change()

            ref_switch.on_value_change(update_ref_display)
            roster_select.on_value_change(on_roster_change)
            char_select.on_value_change(on_char_change)
            on_char_change()

            ui.separator().classes("my-3 border-slate-800")

            # 3. 샘플러, 스케줄러(카라스 등), 스텝 수, CFG Scale 제어부
            ui.label("생성 파라미터 세부 제어").classes("text-sm font-semibold text-slate-300")
            with ui.row().classes("w-full gap-3 mt-1"):
                sampler_options = ["DPM++ 2M", "DPM++ 2M SDE", "DPM++ SDE", "Euler a", "Euler", "DPM++ 3M SDE", "DDIM"]
                sampler_select = ui.select(
                    label="샘플러 (Sampler)",
                    options=sampler_options,
                    value=settings.get("sampler", "DPM++ 2M"),
                ).classes("w-[48%]")

                scheduler_options = ["Karras", "Automatic", "Exponential", "SGM Uniform", "Simple"]
                scheduler_select = ui.select(
                    label="스케줄러 (Scheduler)",
                    options=scheduler_options,
                    value=settings.get("scheduler", "Karras"),
                ).classes("w-[48%]")

            with ui.row().classes("w-full gap-3 mt-1"):
                steps_input = ui.number(
                    label="생성 스텝 (Steps)",
                    value=settings.get("steps", 25),
                    min=10, max=60, step=1,
                ).classes("w-[48%]")

                cfg_input = ui.number(
                    label="CFG Scale",
                    value=settings.get("cfg_scale", 6.0),
                    min=1.0, max=15.0, step=0.5,
                ).classes("w-[48%]")

            # 4. 체크포인트 프로필
            ckpt_options = list(state.ckpt_profiles.keys())
            default_ckpt = (
                "unholyDesireMixSinister_v90.safetensors"
                if "unholyDesireMixSinister_v90.safetensors" in ckpt_options
                else (ckpt_options[0] if ckpt_options else None)
            )
            ckpt_select = ui.select(
                label="체크포인트 프로필",
                options=ckpt_options,
                value=default_ckpt,
            ).classes("w-full mt-1")

            def on_ckpt_change():
                prof = state.ckpt_profiles.get(ckpt_select.value)
                if prof is None:
                    return
                steps_input.set_value(prof.steps)
                cfg_input.set_value(prof.cfg)
                norm_sampler = normalize_webui_sampler(prof.sampler)
                if norm_sampler not in sampler_select.options:
                    sampler_select.options = [*sampler_select.options, norm_sampler]
                sampler_select.set_value(norm_sampler)
                norm_sched = normalize_webui_scheduler(prof.scheduler)
                if norm_sched not in scheduler_select.options:
                    scheduler_select.options = [*scheduler_select.options, norm_sched]
                scheduler_select.set_value(norm_sched)

            ckpt_select.on_value_change(on_ckpt_change)

            # 5. 생성 범위 선택 (퀵 버튼 + 직접 입력 - V2 표준 대역)
            ui.label("생성 범위 선택").classes("text-sm font-semibold text-slate-300 mt-3")
            codes_input = ui.input(
                label="코드 범위 지정 (예: 0-19, 20-39, 40-52, 60-63, h_scenes,special_h, all)",
                value="0-19",
            ).classes("w-full")

            def _is_otokonoko_selected() -> bool:
                return bool(
                    state.char_cfg
                    and str(state.char_cfg.gender).lower() in ("otokonoko", "femboy")
                )

            with ui.row().classes("w-full gap-2 mt-1 flex-wrap"):
                ui.button("감정 씬 (0-19)", on_click=lambda: codes_input.set_value("0-19")).props("outline dense").classes("text-xs")
                ui.button("일상 포즈 (20-39)", on_click=lambda: codes_input.set_value("20-39")).props("outline dense").classes("text-xs")
                ui.button("이벤트 씬 (40-52)", on_click=lambda: codes_input.set_value("40-52")).props("outline dense").classes("text-xs")
                ui.button("로맨스 씬 (60-63)", on_click=lambda: codes_input.set_value("60-63")).props("outline dense").classes("text-xs")
                h_scene_btn = ui.button(
                    "성인 씬 (80-132)",
                    on_click=lambda: codes_input.set_value("scenes_otokonoko,special_otokonoko" if _is_otokonoko_selected() else "h_scenes,special_h"),
                ).props("outline dense").classes("text-xs")
                ui.button("전체 (all)", on_click=lambda: codes_input.set_value("all")).props("outline dense color=indigo").classes("text-xs")

            def update_h_scene_btn():
                if state.is_all_chars():
                    h_scene_btn.set_text("성인 씬 (80-132/133-197)")
                elif _is_otokonoko_selected():
                    h_scene_btn.set_text("성인 씬 (133-197)")
                    if codes_input.value.strip() in ("h_scenes,special_h", "48-99", "h_scenes"):
                        codes_input.set_value("scenes_otokonoko,special_otokonoko")
                else:
                    h_scene_btn.set_text("성인 씬 (80-132)")
                    if codes_input.value.strip() in ("scenes_otokonoko,special_otokonoko", "148-199", "scenes_otokonoko"):
                        codes_input.set_value("h_scenes,special_h")

            char_select.on_value_change(lambda: update_h_scene_btn())
            update_h_scene_btn()

            # 6. 세부 옵션 & 설정 저장 버튼
            with ui.row().classes("w-full items-center justify-between mt-3"):
                naming_select = ui.select(
                    label="파일명 명명 규칙",
                    options=["hybrid", "code", "label"],
                    value=settings.get("naming", "hybrid"),
                ).classes("w-[48%]")

                with ui.column().classes("w-[48%] gap-1"):
                    overwrite_check = ui.checkbox("기존 파일 덮어쓰기", value=settings.get("overwrite", False)).classes("text-xs")
                    mock_check = ui.checkbox("모의 생성 (WebUI 통신 없음)", value=False).classes("text-xs")
                    auto_qc_check = ui.checkbox("Auto-QC 자동 검수/재시도", value=settings.get("auto_qc", False)).classes("text-xs")
                    censor_check = ui.checkbox("성인 씬 자동 검열", value=settings.get("censor", False)).classes("text-xs")

            with ui.row().classes("w-full justify-end mt-2"):
                save_cfg_btn = ui.button(
                    "💾 현재 파라미터 저장",
                    on_click=lambda: do_save_settings(),
                ).props("outline dense").classes("text-xs text-indigo-300 hover:text-white")

            # 7. 실행 & 점검 버튼 그룹
            with ui.row().classes("w-full gap-3 mt-4"):
                precheck_btn = ui.button("🔍 5대 사전점검", on_click=lambda: do_precheck()).classes("w-[35%] bg-slate-800 hover:bg-slate-700")
                start_btn = ui.button("🚀 생성 시작", on_click=lambda: start_generation()).classes("flex-1 bg-indigo-600 hover:bg-indigo-500 font-bold")
                stop_btn = ui.button("⏹ 중단", on_click=lambda: stop_generation()).props("color=rose").classes("w-[20%] hidden")

        # ── [우측] 실시간 뷰어 & 젠잇 마크다운 패널 (폭 55%) ──
        with ui.card().classes("w-[55%] bg-slate-900/80 border border-slate-800 p-5 rounded-xl shadow-lg min-h-[640px] flex flex-col justify-between"):
            with ui.column().classes("w-full"):
                ui.label("📊 실시간 모니터링 & 결과").classes("text-lg font-bold text-indigo-300 mb-2")

                # 실시간 진행률
                progress_bar = ui.linear_progress(value=0.0).classes("w-full rounded h-3.5 my-2")
                progress_label = ui.label("대기 중...").classes("text-xs text-slate-400 font-mono mb-3")

                # 실시간 프리뷰 캔버스/이미지 & 라이트박스 뷰어
                with ui.dialog() as zoom_dialog, ui.card().classes("p-3 bg-slate-950/95 border border-slate-700 max-w-[92vw] max-h-[95vh] flex flex-col items-center justify-center overflow-hidden rounded-xl shadow-2xl"):
                    zoom_image = ui.image("").props("fit=contain").classes("max-h-[82vh] max-w-[88vw] rounded cursor-pointer")
                    with ui.row().classes("w-full justify-between items-center px-3 pt-2"):
                        zoom_caption = ui.label("").classes("text-xs text-slate-300 font-mono")
                        ui.button("닫기 (ESC)", on_click=zoom_dialog.close).props("outline dense").classes("text-xs bg-slate-800")
                    zoom_image.on("click", zoom_dialog.close)

                def open_zoom():
                    if preview_image.source:
                        zoom_image.set_source(preview_image.source)
                        zoom_caption.set_text(progress_label.text)
                        zoom_dialog.open()

                with ui.card().classes("w-full h-[480px] bg-slate-950/90 border border-slate-800 rounded-lg flex items-center justify-center overflow-hidden p-2 relative group"):
                    preview_image = ui.image("").props("fit=contain").classes("w-full h-full rounded cursor-zoom-in hidden")
                    preview_image.on("click", open_zoom)
                    preview_placeholder = ui.label("생성된 이미지가 여기에 실시간으로 표시됩니다 (완료 시 클릭하여 확대)").classes("text-slate-500 text-sm")
                    zoom_btn = ui.button("🔍 원본 확대 보기", on_click=open_zoom).props("dense outline").classes("absolute bottom-3 right-3 text-xs bg-slate-900/90 hover:bg-slate-800 text-indigo-300 hidden")

            # 하단 젠잇 마크다운 아웃풋
            with ui.column().classes("w-full mt-4"):
                ui.label("젠잇(ZenIt) 마크다운 블록").classes("text-xs font-bold text-slate-300")
                zenit_output = ui.textarea().props("readonly rows=6").classes("w-full font-mono text-xs bg-slate-950 border border-slate-800 rounded p-2")

                with ui.row().classes("w-full justify-end gap-3 mt-2"):
                    copy_btn = ui.button("📋 클립보드 복사", on_click=lambda: copy_markdown()).props("outline dense").classes("text-xs")
                    open_btn = ui.button("📁 결과 폴더 열기", on_click=lambda: open_folder()).props("outline dense").classes("text-xs")

    def _collect_current_settings() -> dict[str, Any]:
        return {
            "steps": int(steps_input.value or 25),
            "sampler": str(sampler_select.value),
            "scheduler": str(scheduler_select.value),
            "cfg_scale": float(cfg_input.value or 6.0),
            "use_reference": bool(ref_switch.value),
            "naming": str(naming_select.value),
            "overwrite": bool(overwrite_check.value),
            "auto_qc": bool(auto_qc_check.value),
            "max_retries": int(settings.get("max_retries", 3)),
            "censor": bool(censor_check.value),
            "censor_style": str(settings.get("censor_style", "white_silhouette")),
        }

    # ── 액션 핸들러 ──
    def do_save_settings():
        save_gui_settings(base_dir, _collect_current_settings())
        ui.notify("생성 파라미터가 gui_settings.json에 성공적으로 저장되었습니다!", color="emerald")

    async def do_precheck():
        report = run_precheck(state.base_dir, state.roster)
        with ui.dialog() as dialog, ui.card().classes("w-96 bg-slate-900 border border-slate-700 p-4"):
            status_text = "✅ 5대 사전점검 통과 (PASS)" if report.passed else "❌ 사전점검 위반 발견"
            ui.label(status_text).classes(f"text-base font-bold {'text-emerald-400' if report.passed else 'text-rose-400'}")
            ui.label(f"• 검사 캐릭터: {report.total_characters}명").classes("text-xs text-slate-300")
            ui.label(f"• 검사 포즈: {report.total_poses}개").classes("text-xs text-slate-300")
            if report.errors:
                ui.separator().classes("my-2")
                ui.label("오류 내역:").classes("text-xs text-rose-400 font-bold")
                for err in report.errors:
                    ui.label(f"- {err}").classes("text-xs text-rose-300")
            ui.button("닫기", on_click=dialog.close).classes("mt-4 w-full bg-slate-800")
        dialog.open()

    def copy_markdown():
        if zenit_output.value:
            ui.run_javascript(f"navigator.clipboard.writeText({repr(zenit_output.value)});")
            ui.notify("젠잇 마크다운 블록이 클립보드에 복사되었습니다!", color="emerald")
        else:
            ui.notify("복사할 내용이 없습니다.", color="amber")

    def open_folder():
        if state.is_all_chars():
            # 전체 모드인 경우 에셋 루트 폴더 오픈
            target_dir = state.roster.assets_dir
        elif state.char_cfg:
            target_dir = state.roster.assets_dir / state.char_cfg.prefix
        else:
            target_dir = state.roster.assets_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(target_dir)

    def on_progress_update(code: int, label: str, save_path: Path, current: int, total: int):
        progress_val = current / total if total > 0 else 0.0
        progress_bar.set_value(progress_val)
        prefix_tag = f"[{state.current_active_prefix}] " if state.current_active_prefix else ""
        cur_steps = int(steps_input.value or 25)
        cur_cfg = float(cfg_input.value or 6.0)
        progress_label.set_text(
            f"{prefix_tag}[{code:03d}] {label} ({current}/{total} 완료, {int(progress_val*100)}%) | {cur_steps}스텝, CFG {cur_cfg}"
        )
        # 이미지 프리뷰 갱신 (브라우저 캐시 무효화 쿼리 적용)
        if save_path.exists() and state.current_active_prefix:
            preview_image.set_source(f"/assets/{state.current_active_prefix}/{save_path.name}?t={int(time.time()*1000)}")
            preview_image.classes(remove="hidden")
            zoom_btn.classes(remove="hidden")
            preview_placeholder.classes(add="hidden")

    async def start_generation():
        if not state.is_all_chars() and not state.char_cfg:
            ui.notify("생성할 대상 캐릭터가 선택되지 않았습니다.", color="rose")
            return

        # 최신 pose_database.json 및 캐릭터 JSON 리로드, 현재 파라미터 자동 저장
        state.db = load_pose_db(state.base_dir, state.roster.events_file)
        state.load_selected_char()
        current_cfg = _collect_current_settings()
        user_steps = current_cfg["steps"]
        user_sampler = current_cfg["sampler"]
        user_scheduler = current_cfg["scheduler"]
        user_cfg = current_cfg["cfg_scale"]
        use_ref = current_cfg["use_reference"]

        save_gui_settings(base_dir, current_cfg)

        was_running = state.is_running
        state.active_run_id += 1
        my_run_id = state.active_run_id
        state.is_running = True
        state.cancel_requested = False
        start_btn.classes(add="hidden")
        stop_btn.classes(remove="hidden")

        if was_running:
            interrupt_forge()
            await asyncio.sleep(0.5)

        def is_run_cancelled(rid: int = my_run_id) -> bool:
            return state.cancel_requested or (state.active_run_id != rid)

        progress_bar.set_value(0.0)
        progress_label.set_text(
            f"🚀 생성 파이프라인 초기화: {user_steps}스텝 | {user_sampler} | {user_scheduler} | "
            f"CFG {user_cfg} | 레퍼런스: {'ON' if use_ref else 'OFF'}"
        )

        expr = codes_input.value.strip() or "all"

        def _resolve_char_mode_and_codes(raw_expr: str, char_gender: str) -> tuple[str, str | None]:
            return resolve_gui_mode_and_codes(
                raw_expr,
                char_gender,
                available_sections=set(state.db.sections.keys()),
            )

        def _expand_all_target_mode(target_mode: str, codes_arg: str | None, char_gender: str) -> str:
            if target_mode == "all" and not codes_arg:
                return expand_v2_mode("all", state.db, expand_all_by_gender=char_gender)
            return target_mode

        def _build_base_options(mode_val: str, codes_val: str | None) -> GenerationOptions:
            """GUI 공통 실행 설정을 GenerationOptions 로 조립한다."""
            active_ckpt = state.ckpt_profiles.get(ckpt_select.value) if ckpt_select.value else None
            return GenerationOptions(
                mode=mode_val,
                codes=codes_val,
                bg_preset="default",
                ref_weight=REF_WEIGHT_DEFAULT,
                no_ref=not use_ref,
                dry_run=False,
                mock=bool(mock_check.value),
                steps=user_steps,
                sampler=user_sampler,
                scheduler=user_scheduler,
                cfg_scale=user_cfg,
                naming=str(naming_select.value),
                overwrite=bool(overwrite_check.value),
                auto_qc=current_cfg["auto_qc"],
                max_retries=current_cfg["max_retries"],
                censor=current_cfg["censor"],
                censor_style=current_cfg["censor_style"],
                ckpt_profile=ckpt_select.value,
                active_ckpt=active_ckpt,
            )

        def _build_char_options(cfg: CharacterConfig) -> GenerationOptions:
            mode_val, codes_val = _resolve_char_mode_and_codes(expr, cfg.gender)
            opts = _build_base_options(mode_val, codes_val)
            apply_character_to_args(cfg, opts, state.db)
            return opts

        # ── 전체 캐릭터 순차 생성 (ALL) 분기 (공용 run_all_chars 일원화) ──
        if state.is_all_chars():
            chars_to_run = list(state.characters)
            if not chars_to_run:
                ui.notify("로스터에 생성 가능한 캐릭터 JSON 파일이 없습니다.", color="rose")
                if state.active_run_id == my_run_id:
                    state.is_running = False
                    start_btn.classes(remove="hidden")
                    stop_btn.classes(add="hidden")
                return

            zenit_all_blocks: list[str] = []
            base_mode, base_codes = _resolve_char_mode_and_codes(expr, "female")
            all_base_opts = _build_base_options(base_mode, base_codes)

            def on_char_start_cb(_idx: int, _total: int, _name: str, cfg: CharacterConfig) -> None:
                state.current_active_prefix = cfg.prefix
                save_dir = state.roster.assets_dir / cfg.prefix
                save_dir.mkdir(parents=True, exist_ok=True)
                state.ensure_static_mounted(cfg.prefix, save_dir)

            def char_progress_cb(
                c_idx: int,
                c_total: int,
                c_name: str,
                cfg: CharacterConfig,
                code: int,
                label: str,
                save_path: Path,
                current: int,
                total: int,
            ) -> None:
                if is_run_cancelled():
                    return
                progress_val = current / total if total > 0 else 0.0
                progress_bar.set_value(progress_val)
                progress_label.set_text(
                    f"[{c_idx}/{c_total} {c_name}] [{code:03d}] {label} ({current}/{total} 완료, {int(progress_val*100)}%) | {user_steps}스텝, CFG {user_cfg}"
                )
                if save_path.exists():
                    preview_image.set_source(f"/assets/{cfg.prefix}/{save_path.name}?t={int(time.time()*1000)}")
                    preview_image.classes(remove="hidden")
                    zoom_btn.classes(remove="hidden")
                    preview_placeholder.classes(add="hidden")

            def on_char_complete_cb(
                _idx: int,
                _total: int,
                c_name: str,
                cfg: CharacterConfig,
                _batch_res: BatchResult | None,
                exit_code: int,
            ) -> None:
                if is_run_cancelled():
                    return
                char_opts = _build_char_options(cfg)
                target_mode = _expand_all_target_mode(char_opts.mode, char_opts.codes, cfg.gender or "female")
                targets = resolve_targets(state.db, target_mode, char_opts.codes)
                w = code_width(state.db.all_codes)
                block = build_genit_block(cfg.prefix, targets, state.db, w, naming=naming_select.value)
                status_suffix = " ⚠️(일부 실패)" if exit_code != 0 else ""
                zenit_all_blocks.append(f"### 🎭 캐릭터: {c_name} ({cfg.prefix}){status_suffix}\n{block}")
                zenit_output.set_value("\n\n".join(zenit_all_blocks))

            all_hooks = RunHooks(
                is_cancelled=is_run_cancelled,
                on_char_start=on_char_start_cb,
                char_progress_callback=char_progress_cb,
                on_char_complete=on_char_complete_cb,
            )

            try:
                await run.io_bound(
                    lambda: run_all_chars(state.roster, options=all_base_opts, hooks=all_hooks)
                )
            except Exception as e:
                print(f"[ERROR] 전체 캐릭터 생성 중 오류: {e}")

            if state.active_run_id != my_run_id:
                return

            state.is_running = False
            start_btn.classes(remove="hidden")
            stop_btn.classes(add="hidden")

            all_summary = all_hooks.last_all_result
            total_chars = all_summary.total if all_summary else len(chars_to_run)
            failed_chars = list(all_summary.failed) if all_summary else []
            all_qa_warnings = list(all_summary.qa_issues) if all_summary else []

            if is_run_cancelled():
                progress_label.set_text("⏹ 전체 생성이 사용자에 의해 중단되었습니다.")
                ui.notify("생성 작업 중단됨", color="amber")
            else:
                if all_qa_warnings:
                    qa_lines = [f"### ⚠️ Auto-QC 품질 경고 요약 ({len(all_qa_warnings)}건)"]
                    for q_char, q_code, q_lbl, q_rsn in all_qa_warnings:
                        qa_lines.append(f"- [{q_char}] 포즈 {q_code:03d} ({q_lbl}): {q_rsn}")
                    zenit_all_blocks.append("\n".join(qa_lines))
                    zenit_output.set_value("\n\n".join(zenit_all_blocks))

                progress_bar.set_value(1.0)
                succeeded_count = total_chars - len(failed_chars)
                if failed_chars:
                    fail_preview = ", ".join(failed_chars[:5]) + ("..." if len(failed_chars) > 5 else "")
                    qa_note = f" | Auto-QC 경고 {len(all_qa_warnings)}건" if all_qa_warnings else ""
                    progress_label.set_text(
                        f"⚠️ 전체 생성 완료: {succeeded_count}/{total_chars}명 성공, {len(failed_chars)}명 실패 ({fail_preview}){qa_note}"
                    )
                    ui.notify(
                        f"전체 생성 완료 (실패 {len(failed_chars)}명: {fail_preview}{qa_note})",
                        color="amber",
                    )
                elif all_qa_warnings:
                    progress_label.set_text(
                        f"✅ 전체 {total_chars}명 완료 (⚠️ Auto-QC 경고 {len(all_qa_warnings)}건 - 마크다운 하단 참조)"
                    )
                    ui.notify(
                        f"전체 생성 완료! Auto-QC 경고 {len(all_qa_warnings)}건이 감지되었습니다.",
                        color="amber",
                    )
                else:
                    progress_label.set_text(f"✅ 전체 {total_chars}명 캐릭터 에셋 생성이 성공적으로 완료되었습니다!")
                    ui.notify("전체 캐릭터 생성 완료! 젠잇 마크다운 블록이 통합 준비되었습니다.", color="emerald")
            return

        # ── 단일 캐릭터 생성 분기 ──
        state.current_active_prefix = state.char_cfg.prefix
        save_dir = state.roster.assets_dir / state.char_cfg.prefix
        save_dir.mkdir(parents=True, exist_ok=True)
        state.ensure_static_mounted(state.char_cfg.prefix, save_dir)

        single_opts = _build_char_options(state.char_cfg)
        single_hooks = RunHooks(
            progress_callback=lambda c, l, p, cur, tot: (
                None if is_run_cancelled() else on_progress_update(c, l, p, cur, tot)
            ),
            is_cancelled=is_run_cancelled,
        )
        try:
            exit_code = await run.io_bound(
                lambda _o=single_opts, _h=single_hooks: execute(_o, state.roster, hooks=_h)
            )
        except Exception as e:
            print(f"[ERROR] 생성 실행 중 예외 발생: {e}")
            exit_code = 1

        if state.active_run_id != my_run_id:
            return

        state.is_running = False
        start_btn.classes(remove="hidden")
        stop_btn.classes(add="hidden")

        if exit_code == 0 and not is_run_cancelled():
            progress_bar.set_value(1.0)
            last_res = single_hooks.last_result
            qa_warns = getattr(last_res, "qa_warnings", []) if last_res else []
            qa_suffix = f" | ⚠️ Auto-QC 경고 {len(qa_warns)}건" if qa_warns else ""
            progress_label.set_text(
                f"✅ [{state.char_cfg.prefix}] 에셋 생성이 성공적으로 완료되었습니다! ({user_steps}스텝, CFG {user_cfg}){qa_suffix}"
            )
            target_mode = _expand_all_target_mode(single_opts.mode, single_opts.codes, state.char_cfg.gender)
            targets = resolve_targets(state.db, target_mode, single_opts.codes)
            w = code_width(state.db.all_codes)
            markdown_block = build_genit_block(
                state.char_cfg.prefix, targets, state.db, w, naming=naming_select.value
            )
            if qa_warns:
                warn_text = "\n".join(f"- [{c:03d} {lbl}] {rsn}" for c, lbl, rsn in qa_warns)
                markdown_block = f"{markdown_block}\n### ⚠️ Auto-QC 품질 경고 ({len(qa_warns)}건)\n{warn_text}"
            zenit_output.set_value(markdown_block)
            ui.notify(
                f"생성 완료! (Auto-QC 경고 {len(qa_warns)}건)" if qa_warns else "생성 완료! 젠잇 코드가 준비되었습니다.",
                color="amber" if qa_warns else "emerald",
            )
        else:
            progress_label.set_text("⏹ 생성이 중단되었거나 일부 항목이 실패했습니다.")
            ui.notify("생성 중단 또는 실패 발생", color="amber")

    async def stop_generation():
        state.cancel_requested = True
        state.active_run_id += 1
        cancelled_id = state.active_run_id
        state.is_running = False
        start_btn.classes(remove="hidden")
        stop_btn.classes(add="hidden")
        interrupt_forge()
        progress_label.set_text("⏹ 즉시 중단 신호 전송 완료 (이전 작업 대기열 완전 폐기)")
        ui.notify("생성 중단 완료! 이전 작업 루프가 완전히 종료되었습니다.", color="amber")
        # ControlNet/IP-Adapter 전처리 구간에서 첫 interrupt가 무시되는 현상을 막기 위해 0.7초 후 재차 전송
        await asyncio.sleep(0.7)
        if state.active_run_id == cancelled_id:
            interrupt_forge()


def main():
    parser = argparse.ArgumentParser(description="Kiro Batch Studio GUI")
    parser.add_argument("--port", type=int, default=8080, help="웹 서버 포트 (기본: 8080)")
    parser.add_argument("--native", action="store_true", help="네이티브 윈도우 창으로 실행")
    args, _ = parser.parse_known_args()

    base_dir = Path(__file__).resolve().parent

    @ui.page("/")
    def index_page():
        build_gui(base_dir)

    ui.run(title="Kiro Studio", port=args.port, reload=False, dark=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()

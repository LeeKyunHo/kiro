"""
gui/components.py
Kiro Studio GUI NiceGUI 컴포넌트 빌더(상단 헤더, 좌측 제어 패널, 우측 모니터링 & 로그 패널).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nicegui import run, ui

from generator.checkpoint import normalize_webui_sampler, normalize_webui_scheduler
from generator.config import REFERENCES_DIRNAME
from gui.state import GuiRuntimeState


@dataclass
class ControlPanelWidgets:
    """좌측 제어 패널 위젯 핸들 모음."""

    roster_select: Any
    char_select: Any
    ref_switch: Any
    ref_badge: Any
    char_info_label: Any
    sampler_select: Any
    scheduler_select: Any
    steps_input: Any
    cfg_input: Any
    ckpt_select: Any
    codes_input: Any
    naming_select: Any
    overwrite_check: Any
    mock_check: Any
    auto_qc_check: Any
    strict_censor_qc_check: Any
    censor_check: Any
    save_cfg_btn: Any
    precheck_btn: Any
    start_btn: Any
    stop_btn: Any

    def collect_settings(self, base_settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "steps": int(self.steps_input.value or 30),
            "sampler": str(self.sampler_select.value),
            "scheduler": str(self.scheduler_select.value),
            "cfg_scale": float(self.cfg_input.value or 6.0),
            "use_reference": bool(self.ref_switch.value),
            "naming": str(self.naming_select.value),
            "overwrite": bool(self.overwrite_check.value),
            "auto_qc": bool(self.auto_qc_check.value),
            "strict_censor_qc": bool(self.strict_censor_qc_check.value),
            "max_retries": int(base_settings.get("max_retries", 3)),
            "censor": bool(self.censor_check.value),
            "censor_style": str(base_settings.get("censor_style", "white_silhouette")),
        }


@dataclass
class MonitorPanelWidgets:
    """우측 모니터링·로그·젠잇 마크다운 패널 위젯 핸들 모음."""

    progress_bar: Any
    progress_label: Any
    preview_image: Any
    preview_placeholder: Any
    zoom_btn: Any
    log_output: Any
    zenit_output: Any
    copy_btn: Any
    open_btn: Any


def build_header(check_connection_fn: Callable[[], bool]) -> Any:
    """상단 타이틀 및 비동기 Forge 헬스 체크 배지 헤더를 생성한다."""
    with ui.header().classes(
        "bg-slate-900 border-b border-slate-800 px-6 py-3 flex items-center justify-between"
    ):
        with ui.row().classes("items-center gap-3"):
            ui.icon("auto_awesome", size="2rem").classes("text-indigo-400")
            ui.label("Kiro Batch Studio").classes("text-xl font-bold text-white tracking-wide")
            ui.badge("Forge SDXL", color="indigo-600").classes("text-xs font-mono")

        with ui.row().classes("items-center gap-3"):
            forge_badge = ui.badge("Forge 연결 확인 중...", color="slate").classes(
                "text-xs font-semibold px-2.5 py-1"
            )

            async def refresh_forge_badge() -> None:
                online = await run.io_bound(check_connection_fn)
                if online:
                    forge_badge.set_text("✅ Forge 연결 정상")
                    forge_badge.props("color=emerald")
                else:
                    forge_badge.set_text("⚠️ Forge 미감지 (WebUI 확인 필요)")
                    forge_badge.props("color=amber")

            ui.timer(3.0, refresh_forge_badge)
            ui.timer(0.1, refresh_forge_badge, once=True)

    return forge_badge


def build_control_panel(
    state: GuiRuntimeState,
    settings: dict[str, Any],
    *,
    on_save_settings: Callable[[], Any],
    on_precheck: Callable[[], Any],
    on_start: Callable[[], Any],
    on_stop: Callable[[], Any],
) -> ControlPanelWidgets:
    """좌측 생성 제어 및 파라미터 설정 패널을 생성한다."""
    with ui.card().classes("w-[45%] bg-slate-900/80 border border-slate-800 p-5 rounded-xl shadow-lg"):
        ui.label("⚙️ 생성 제어 패널").classes("text-lg font-bold text-indigo-300 mb-3")

        with ui.row().classes("w-full gap-4"):
            roster_select = ui.select(
                label="프로젝트 (로스터)",
                options=state.available_rosters,
                value=state.current_roster_name,
            ).classes("w-1/2")

            char_select_options = [GuiRuntimeState.ALL_CHARS_OPTION] + state.characters
            char_select = ui.select(
                label="대상 캐릭터",
                options=char_select_options,
                value=state.selected_char_name,
            ).classes("w-1/2")

        with ui.row().classes("w-full items-center justify-between mt-2"):
            ref_switch = ui.switch(
                "레퍼런스(IP-Adapter) 사용",
                value=settings.get("use_reference", True),
            ).classes("text-xs font-semibold text-slate-200")

        ref_badge = ui.badge("참조 이미지 확인 중", color="slate").classes(
            "w-full py-1.5 text-center mt-1"
        )
        char_info_label = ui.label("").classes("text-xs text-slate-400 mt-1 line-clamp-2")

        def update_ref_display() -> None:
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

        def on_char_change() -> None:
            state.selected_char_name = char_select.value
            state.load_selected_char()
            update_ref_display()

        def on_roster_change() -> None:
            state.update_roster(roster_select.value)
            new_opts = [GuiRuntimeState.ALL_CHARS_OPTION] + state.characters
            char_select.options = new_opts
            char_select.value = state.selected_char_name
            on_char_change()

        ref_switch.on_value_change(update_ref_display)
        roster_select.on_value_change(on_roster_change)
        char_select.on_value_change(on_char_change)
        on_char_change()

        ui.separator().classes("my-3 border-slate-800")

        ui.label("생성 파라미터 세부 제어").classes("text-sm font-semibold text-slate-300")
        with ui.row().classes("w-full gap-3 mt-1"):
            sampler_options = [
                "DPM++ 2M",
                "DPM++ 2M SDE",
                "DPM++ SDE",
                "Euler a",
                "Euler",
                "DPM++ 3M SDE",
                "DDIM",
            ]
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
                value=settings.get("steps", 30),
                min=10,
                max=60,
                step=1,
            ).classes("w-[48%]")

            cfg_input = ui.number(
                label="CFG Scale",
                value=settings.get("cfg_scale", 6.0),
                min=1.0,
                max=15.0,
                step=0.5,
            ).classes("w-[48%]")

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

        def on_ckpt_change() -> None:
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
            ui.button("감정 씬 (0-19)", on_click=lambda: codes_input.set_value("0-19")).props(
                "outline dense"
            ).classes("text-xs")
            ui.button("일상 포즈 (20-39)", on_click=lambda: codes_input.set_value("20-39")).props(
                "outline dense"
            ).classes("text-xs")
            ui.button("이벤트 씬 (40-52)", on_click=lambda: codes_input.set_value("40-52")).props(
                "outline dense"
            ).classes("text-xs")
            ui.button("로맨스 씬 (60-63)", on_click=lambda: codes_input.set_value("60-63")).props(
                "outline dense"
            ).classes("text-xs")
            h_scene_btn = (
                ui.button(
                    "성인 씬 (80-132)",
                    on_click=lambda: codes_input.set_value(
                        "scenes_otokonoko,special_otokonoko"
                        if _is_otokonoko_selected()
                        else "h_scenes,special_h"
                    ),
                )
                .props("outline dense")
                .classes("text-xs")
            )
            ui.button("전체 (all)", on_click=lambda: codes_input.set_value("all")).props(
                "outline dense color=indigo"
            ).classes("text-xs")

        def update_h_scene_btn() -> None:
            if state.is_all_chars():
                h_scene_btn.set_text("성인 씬 (80-132/133-197)")
            elif _is_otokonoko_selected():
                h_scene_btn.set_text("성인 씬 (133-197)")
                if codes_input.value.strip() in ("h_scenes,special_h", "48-99", "h_scenes"):
                    codes_input.set_value("scenes_otokonoko,special_otokonoko")
            else:
                h_scene_btn.set_text("성인 씬 (80-132)")
                if codes_input.value.strip() in (
                    "scenes_otokonoko,special_otokonoko",
                    "148-199",
                    "scenes_otokonoko",
                ):
                    codes_input.set_value("h_scenes,special_h")

        char_select.on_value_change(lambda: update_h_scene_btn())
        update_h_scene_btn()

        with ui.row().classes("w-full items-center justify-between mt-3"):
            naming_select = ui.select(
                label="파일명 명명 규칙",
                options=["hybrid", "code", "label"],
                value=settings.get("naming", "hybrid"),
            ).classes("w-[48%]")

            with ui.column().classes("w-[48%] gap-1"):
                overwrite_check = ui.checkbox(
                    "기존 파일 덮어쓰기", value=settings.get("overwrite", False)
                ).classes("text-xs")
                mock_check = ui.checkbox("모의 생성 (WebUI 통신 없음)", value=False).classes("text-xs")
                auto_qc_check = ui.checkbox(
                    "Auto-QC 자동 검수/재시도", value=settings.get("auto_qc", False)
                ).classes("text-xs")
                strict_censor_qc_check = ui.checkbox(
                    "성인 씬 타겟 미탐지 리롤 (Strict QC)",
                    value=settings.get("strict_censor_qc", False),
                ).classes("text-xs")
                censor_check = ui.checkbox(
                    "성인 씬 자동 검열", value=settings.get("censor", False)
                ).classes("text-xs")

        with ui.row().classes("w-full justify-end mt-2"):
            save_cfg_btn = (
                ui.button("💾 현재 파라미터 저장", on_click=lambda: on_save_settings())
                .props("outline dense")
                .classes("text-xs text-indigo-300 hover:text-white")
            )

        with ui.row().classes("w-full gap-3 mt-4"):
            precheck_btn = ui.button("🔍 5대 사전점검", on_click=lambda: on_precheck()).classes(
                "w-[35%] bg-slate-800 hover:bg-slate-700"
            )
            start_btn = ui.button("🚀 생성 시작", on_click=lambda: on_start()).classes(
                "flex-1 bg-indigo-600 hover:bg-indigo-500 font-bold"
            )
            stop_btn = (
                ui.button("⏹ 중단", on_click=lambda: on_stop())
                .props("color=rose")
                .classes("w-[20%] hidden")
            )

    return ControlPanelWidgets(
        roster_select=roster_select,
        char_select=char_select,
        ref_switch=ref_switch,
        ref_badge=ref_badge,
        char_info_label=char_info_label,
        sampler_select=sampler_select,
        scheduler_select=scheduler_select,
        steps_input=steps_input,
        cfg_input=cfg_input,
        ckpt_select=ckpt_select,
        codes_input=codes_input,
        naming_select=naming_select,
        overwrite_check=overwrite_check,
        mock_check=mock_check,
        auto_qc_check=auto_qc_check,
        strict_censor_qc_check=strict_censor_qc_check,
        censor_check=censor_check,
        save_cfg_btn=save_cfg_btn,
        precheck_btn=precheck_btn,
        start_btn=start_btn,
        stop_btn=stop_btn,
    )


def build_monitor_panel(
    *,
    on_copy_markdown: Callable[[], Any],
    on_open_folder: Callable[[], Any],
    on_clear_logs: Callable[[], Any],
) -> MonitorPanelWidgets:
    """우측 실시간 모니터링, 프리뷰 뷰어, 실행 로그 및 ZenIt 마크다운 패널을 생성한다."""
    with ui.card().classes(
        "w-[55%] bg-slate-900/80 border border-slate-800 p-5 rounded-xl shadow-lg min-h-[640px] flex flex-col justify-between"
    ):
        with ui.column().classes("w-full"):
            ui.label("📊 실시간 모니터링 & 결과").classes("text-lg font-bold text-indigo-300 mb-2")

            progress_bar = ui.linear_progress(value=0.0).classes("w-full rounded h-3.5 my-2")
            progress_label = ui.label("대기 중...").classes("text-xs text-slate-400 font-mono mb-3")

            with ui.dialog() as zoom_dialog, ui.card().classes(
                "p-3 bg-slate-950/95 border border-slate-700 max-w-[92vw] max-h-[95vh] flex flex-col items-center justify-center overflow-hidden rounded-xl shadow-2xl"
            ):
                zoom_image = (
                    ui.image("")
                    .props("fit=contain")
                    .classes("max-h-[82vh] max-w-[88vw] rounded cursor-pointer")
                )
                with ui.row().classes("w-full justify-between items-center px-3 pt-2"):
                    zoom_caption = ui.label("").classes("text-xs text-slate-300 font-mono")
                    ui.button("닫기 (ESC)", on_click=zoom_dialog.close).props("outline dense").classes(
                        "text-xs bg-slate-800"
                    )
                zoom_image.on("click", zoom_dialog.close)

            def open_zoom() -> None:
                if preview_image.source:
                    zoom_image.set_source(preview_image.source)
                    zoom_caption.set_text(progress_label.text)
                    zoom_dialog.open()

            with ui.card().classes(
                "w-full h-[420px] bg-slate-950/90 border border-slate-800 rounded-lg flex items-center justify-center overflow-hidden p-2 relative group"
            ):
                preview_image = (
                    ui.image("")
                    .props("fit=contain")
                    .classes("w-full h-full rounded cursor-zoom-in hidden")
                )
                preview_image.on("click", open_zoom)
                preview_placeholder = ui.label(
                    "생성된 이미지가 여기에 실시간으로 표시됩니다 (완료 시 클릭하여 확대)"
                ).classes("text-slate-500 text-sm")
                zoom_btn = (
                    ui.button("🔍 원본 확대 보기", on_click=open_zoom)
                    .props("dense outline")
                    .classes(
                        "absolute bottom-3 right-3 text-xs bg-slate-900/90 hover:bg-slate-800 text-indigo-300 hidden"
                    )
                )

            with ui.expansion("📜 실시간 파이프라인 로그", icon="terminal").classes(
                "w-full mt-3 bg-slate-950/60 border border-slate-800 rounded text-xs text-slate-300"
            ):
                log_output = (
                    ui.textarea()
                    .props("readonly rows=5")
                    .classes("w-full font-mono text-xs bg-slate-950 border border-slate-800 rounded p-2")
                )
                with ui.row().classes("w-full justify-end mt-1"):
                    ui.button("로그 비우기", on_click=lambda: on_clear_logs()).props(
                        "outline dense"
                    ).classes("text-xs")

        with ui.column().classes("w-full mt-4"):
            ui.label("젠잇(ZenIt) 마크다운 블록").classes("text-xs font-bold text-slate-300")
            zenit_output = (
                ui.textarea()
                .props("readonly rows=6")
                .classes("w-full font-mono text-xs bg-slate-950 border border-slate-800 rounded p-2")
            )

            with ui.row().classes("w-full justify-end gap-3 mt-2"):
                copy_btn = (
                    ui.button("📋 클립보드 복사", on_click=lambda: on_copy_markdown())
                    .props("outline dense")
                    .classes("text-xs")
                )
                open_btn = (
                    ui.button("📁 결과 폴더 열기", on_click=lambda: on_open_folder())
                    .props("outline dense")
                    .classes("text-xs")
                )

    return MonitorPanelWidgets(
        progress_bar=progress_bar,
        progress_label=progress_label,
        preview_image=preview_image,
        preview_placeholder=preview_placeholder,
        zoom_btn=zoom_btn,
        log_output=log_output,
        zenit_output=zenit_output,
        copy_btn=copy_btn,
        open_btn=open_btn,
    )

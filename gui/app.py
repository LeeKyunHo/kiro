"""
gui/app.py
Kiro Studio NiceGUI 페이지 조립(build_gui) 및 실행 엔트리포인트(main).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import time

from nicegui import app as nicegui_app, run, ui

from generator.models import BatchResult, CharacterConfig, RunHooks
from generator.pose_db import load_pose_db
from generator.precheck import run_precheck
from generator.reporter import open_in_explorer
from generator.runner import execute, run_all_chars
from generator.webui_client import interrupt_forge
from gui.api_routes import register_api_routes
from gui.components import build_control_panel, build_header, build_monitor_panel
from gui.runner_bridge import (
    build_base_options,
    build_char_options,
    build_char_zenit_block,
    format_all_chars_qa_markdown,
    format_qa_warnings_markdown,
)
from gui.settings import load_gui_settings, save_gui_settings
from gui.state import (
    GuiLogBufferHandler,
    GuiRuntimeState,
    attach_gui_log_handler,
    check_forge_connection,
    detach_gui_log_handler,
    resolve_gui_mode_and_codes,
)


logger = logging.getLogger("generator.gui")

_SHARED_STATE: GuiRuntimeState | None = None


def get_or_create_runtime_state(base_dir: Path) -> GuiRuntimeState:
    """프로세스 단위 공유 GuiRuntimeState를 반환하여 다중 탭 간 중복 실행 가드를 보장한다."""
    global _SHARED_STATE
    if _SHARED_STATE is None or _SHARED_STATE.base_dir != base_dir:
        _SHARED_STATE = GuiRuntimeState(base_dir)
    return _SHARED_STATE


def build_gui(base_dir: Path, state: GuiRuntimeState | None = None) -> GuiRuntimeState:
    """Kiro Studio 메인 페이지를 조립하고 런타임 상태 객체를 반환한다."""
    settings = load_gui_settings(base_dir)
    if state is None:
        state = get_or_create_runtime_state(base_dir)
    register_api_routes(nicegui_app, base_dir, job_manager=state.job_manager)
    state.db = load_pose_db(base_dir, state.roster.events_file)
    state.refresh_characters()

    ui.dark_mode(True)
    ui.page_title("Kiro Studio - 배치 생성기")

    build_header(check_forge_connection)

    with ui.row().classes("w-full p-6 gap-6 items-start no-wrap"):
        ctrl = build_control_panel(
            state,
            settings,
            on_save_settings=lambda: do_save_settings(),
            on_precheck=lambda: do_precheck(),
            on_start=lambda: start_generation(),
            on_stop=lambda: stop_generation(),
        )
        mon = build_monitor_panel(
            on_copy_markdown=lambda: copy_markdown(),
            on_open_folder=lambda: open_folder(),
            on_clear_logs=lambda: log_handler.clear(),
        )

    # 현재 탭이 열릴 때 이미 실행 중이면 버튼 상태 동기화
    if state.is_running:
        ctrl.start_btn.classes(add="hidden")
        ctrl.stop_btn.classes(remove="hidden")

    # 클라이언트 세션 단위 GUI 로그 스트리밍 핸들러 연결 및 재접속 중복 방지
    try:
        client_id = str(getattr(ui.context.client, "id", "default"))
    except Exception:
        client_id = "default"

    def _sync_log_view(text: str) -> None:
        try:
            mon.log_output.set_value(text)
        except Exception:
            pass

    log_handler = GuiLogBufferHandler(
        max_lines=200,
        handler_id=f"gui_client_{client_id}",
        on_emit=_sync_log_view,
    )
    attach_gui_log_handler(log_handler)
    try:
        ui.context.client.on_disconnect(lambda: detach_gui_log_handler(log_handler))
    except Exception:
        pass

    def do_save_settings() -> None:
        save_gui_settings(base_dir, ctrl.collect_settings(settings))
        ui.notify("생성 파라미터가 gui_settings.json에 성공적으로 저장되었습니다!", color="emerald")

    async def do_precheck() -> None:
        report = run_precheck(state.base_dir, state.roster)
        with ui.dialog() as dialog, ui.card().classes("w-96 bg-slate-900 border border-slate-700 p-4"):
            status_text = "✅ 5대 사전점검 통과 (PASS)" if report.passed else "❌ 사전점검 위반 발견"
            ui.label(status_text).classes(
                f"text-base font-bold {'text-emerald-400' if report.passed else 'text-rose-400'}"
            )
            ui.label(f"• 검사 캐릭터: {report.total_characters}명").classes("text-xs text-slate-300")
            ui.label(f"• 검사 포즈: {report.total_poses}개").classes("text-xs text-slate-300")
            if report.errors:
                ui.separator().classes("my-2")
                ui.label("오류 내역:").classes("text-xs text-rose-400 font-bold")
                for err in report.errors:
                    ui.label(f"- {err}").classes("text-xs text-rose-300")
            ui.button("닫기", on_click=dialog.close).classes("mt-4 w-full bg-slate-800")
        dialog.open()

    def copy_markdown() -> None:
        if mon.zenit_output.value:
            ui.run_javascript(f"navigator.clipboard.writeText({repr(mon.zenit_output.value)});")
            ui.notify("젠잇 마크다운 블록이 클립보드에 복사되었습니다!", color="emerald")
        else:
            ui.notify("복사할 내용이 없습니다.", color="amber")

    def open_folder() -> None:
        if state.is_all_chars() or not state.char_cfg:
            target_dir = state.roster.assets_dir
        else:
            target_dir = state.roster.assets_dir / state.char_cfg.prefix
        target_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(target_dir)

    def on_progress_update(
        code: int, label: str, save_path: Path, current: int, total: int
    ) -> None:
        progress_val = current / total if total > 0 else 0.0
        mon.progress_bar.set_value(progress_val)
        prefix_tag = f"[{state.current_active_prefix}] " if state.current_active_prefix else ""
        cur_steps = int(ctrl.steps_input.value or 25)
        cur_cfg = float(ctrl.cfg_input.value or 6.0)
        mon.progress_label.set_text(
            f"{prefix_tag}[{code:03d}] {label} ({current}/{total} 완료, {int(progress_val * 100)}%) | {cur_steps}스텝, CFG {cur_cfg}"
        )
        if save_path.exists() and state.current_active_prefix:
            mon.preview_image.set_source(
                f"/assets/{state.current_active_prefix}/{save_path.name}?t={int(time.time() * 1000)}"
            )
            mon.preview_image.classes(remove="hidden")
            mon.zoom_btn.classes(remove="hidden")
            mon.preview_placeholder.classes(add="hidden")

    async def start_generation() -> None:
        if not state.is_all_chars() and not state.char_cfg:
            ui.notify("생성할 대상 캐릭터가 선택되지 않았습니다.", color="rose")
            return

        acquired, my_run_id, _was_running = state.try_begin_run(allow_preempt=False)
        if not acquired:
            ui.notify("이미 배치 생성 작업이 실행 중입니다. 중단 후 다시 시도해 주세요.", color="amber")
            return

        ctrl.start_btn.classes(add="hidden")
        ctrl.stop_btn.classes(remove="hidden")

        state.db = load_pose_db(state.base_dir, state.roster.events_file)
        state.load_selected_char()
        current_cfg = ctrl.collect_settings(settings)
        user_steps = current_cfg["steps"]
        user_sampler = current_cfg["sampler"]
        user_scheduler = current_cfg["scheduler"]
        user_cfg = current_cfg["cfg_scale"]
        use_ref = current_cfg["use_reference"]
        save_gui_settings(base_dir, current_cfg)

        def is_run_cancelled(rid: int = my_run_id) -> bool:
            return state.is_run_cancelled(rid)

        mon.progress_bar.set_value(0.0)
        mon.progress_label.set_text(
            f"🚀 생성 파이프라인 초기화: {user_steps}스텝 | {user_sampler} | {user_scheduler} | "
            f"CFG {user_cfg} | 레퍼런스: {'ON' if use_ref else 'OFF'}"
        )

        expr = ctrl.codes_input.value.strip() or "all"
        ckpt_name = ctrl.ckpt_select.value
        mock_mode = bool(ctrl.mock_check.value)
        naming_val = str(ctrl.naming_select.value)

        # ── 전체 캐릭터 순차 생성 (ALL) 분기 ──
        if state.is_all_chars():
            chars_to_run = list(state.characters)
            if not chars_to_run:
                ui.notify("로스터에 생성 가능한 캐릭터 JSON 파일이 없습니다.", color="rose")
                if state.finish_run(my_run_id):
                    ctrl.start_btn.classes(remove="hidden")
                    ctrl.stop_btn.classes(add="hidden")
                return

            zenit_all_blocks: list[str] = []
            base_mode, base_codes = resolve_gui_mode_and_codes(
                expr,
                "female",
                available_sections=set(state.db.sections.keys()),
            )
            all_base_opts = build_base_options(
                state,
                current_cfg,
                mode_val=base_mode,
                codes_val=base_codes,
                ckpt_name=ckpt_name,
                mock=mock_mode,
            )

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
                mon.progress_bar.set_value(progress_val)
                mon.progress_label.set_text(
                    f"[{c_idx}/{c_total} {c_name}] [{code:03d}] {label} ({current}/{total} 완료, {int(progress_val * 100)}%) | {user_steps}스텝, CFG {user_cfg}"
                )
                if save_path.exists():
                    mon.preview_image.set_source(
                        f"/assets/{cfg.prefix}/{save_path.name}?t={int(time.time() * 1000)}"
                    )
                    mon.preview_image.classes(remove="hidden")
                    mon.zoom_btn.classes(remove="hidden")
                    mon.preview_placeholder.classes(add="hidden")

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
                char_opts = build_char_options(
                    state,
                    cfg,
                    expr,
                    current_cfg,
                    ckpt_name=ckpt_name,
                    mock=mock_mode,
                )
                block = build_char_zenit_block(state, cfg, char_opts, naming=naming_val)
                status_suffix = " ⚠️(일부 실패)" if exit_code != 0 else ""
                zenit_all_blocks.append(f"### 🎭 캐릭터: {c_name} ({cfg.prefix}){status_suffix}\n{block}")
                mon.zenit_output.set_value("\n\n".join(zenit_all_blocks))

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
                logger.error("[ERROR] 전체 캐릭터 생성 중 오류: %s", e)

            if not state.finish_run(my_run_id):
                return

            ctrl.start_btn.classes(remove="hidden")
            ctrl.stop_btn.classes(add="hidden")

            all_summary = all_hooks.last_all_result
            total_chars = all_summary.total if all_summary else len(chars_to_run)
            failed_chars = list(all_summary.failed) if all_summary else []
            all_qa_warnings = list(all_summary.qa_issues) if all_summary else []

            if is_run_cancelled():
                mon.progress_label.set_text("⏹ 전체 생성이 사용자에 의해 중단되었습니다.")
                ui.notify("생성 작업 중단됨", color="amber")
            else:
                if all_qa_warnings:
                    zenit_all_blocks.append(format_all_chars_qa_markdown(all_qa_warnings))
                    mon.zenit_output.set_value("\n\n".join(zenit_all_blocks))

                mon.progress_bar.set_value(1.0)
                succeeded_count = total_chars - len(failed_chars)
                if failed_chars:
                    fail_preview = ", ".join(failed_chars[:5]) + ("..." if len(failed_chars) > 5 else "")
                    qa_note = f" | Auto-QC 경고 {len(all_qa_warnings)}건" if all_qa_warnings else ""
                    mon.progress_label.set_text(
                        f"⚠️ 전체 생성 완료: {succeeded_count}/{total_chars}명 성공, {len(failed_chars)}명 실패 ({fail_preview}){qa_note}"
                    )
                    ui.notify(
                        f"전체 생성 완료 (실패 {len(failed_chars)}명: {fail_preview}{qa_note})",
                        color="amber",
                    )
                elif all_qa_warnings:
                    mon.progress_label.set_text(
                        f"✅ 전체 {total_chars}명 완료 (⚠️ Auto-QC 경고 {len(all_qa_warnings)}건 - 마크다운 하단 참조)"
                    )
                    ui.notify(
                        f"전체 생성 완료! Auto-QC 경고 {len(all_qa_warnings)}건이 감지되었습니다.",
                        color="amber",
                    )
                else:
                    mon.progress_label.set_text(
                        f"✅ 전체 {total_chars}명 캐릭터 에셋 생성이 성공적으로 완료되었습니다!"
                    )
                    ui.notify("전체 캐릭터 생성 완료! 젠잇 마크다운 블록이 통합 준비되었습니다.", color="emerald")
            return

        # ── 단일 캐릭터 생성 분기 ──
        assert state.char_cfg is not None
        state.current_active_prefix = state.char_cfg.prefix
        save_dir = state.roster.assets_dir / state.char_cfg.prefix
        save_dir.mkdir(parents=True, exist_ok=True)
        state.ensure_static_mounted(state.char_cfg.prefix, save_dir)

        single_opts = build_char_options(
            state,
            state.char_cfg,
            expr,
            current_cfg,
            ckpt_name=ckpt_name,
            mock=mock_mode,
        )
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
            logger.error("[ERROR] 생성 실행 중 예외 발생: %s", e)
            exit_code = 1

        if not state.finish_run(my_run_id):
            return

        ctrl.start_btn.classes(remove="hidden")
        ctrl.stop_btn.classes(add="hidden")

        if exit_code == 0 and not is_run_cancelled():
            mon.progress_bar.set_value(1.0)
            last_res = single_hooks.last_result
            qa_warns = getattr(last_res, "qa_warnings", []) if last_res else []
            qa_suffix = f" | ⚠️ Auto-QC 경고 {len(qa_warns)}건" if qa_warns else ""
            mon.progress_label.set_text(
                f"✅ [{state.char_cfg.prefix}] 에셋 생성이 성공적으로 완료되었습니다! ({user_steps}스텝, CFG {user_cfg}){qa_suffix}"
            )
            markdown_block = build_char_zenit_block(
                state, state.char_cfg, single_opts, naming=naming_val
            )
            if qa_warns:
                markdown_block = f"{markdown_block}\n{format_qa_warnings_markdown(qa_warns)}"
            mon.zenit_output.set_value(markdown_block)
            ui.notify(
                f"생성 완료! (Auto-QC 경고 {len(qa_warns)}건)"
                if qa_warns
                else "생성 완료! 젠잇 코드가 준비되었습니다.",
                color="amber" if qa_warns else "emerald",
            )
        else:
            mon.progress_label.set_text("⏹ 생성이 중단되었거나 일부 항목이 실패했습니다.")
            ui.notify("생성 중단 또는 실패 발생", color="amber")

    async def stop_generation() -> None:
        cancelled_id = state.request_cancel()
        ctrl.start_btn.classes(remove="hidden")
        ctrl.stop_btn.classes(add="hidden")
        interrupt_forge()
        mon.progress_label.set_text("⏹ 즉시 중단 신호 전송 완료 (이전 작업 대기열 완전 폐기)")
        ui.notify("생성 중단 완료! 이전 작업 루프가 완전히 종료되었습니다.", color="amber")
        await asyncio.sleep(0.7)
        if state.active_run_id == cancelled_id:
            interrupt_forge()

    return state


def main(base_dir: Path | None = None) -> None:
    parser = argparse.ArgumentParser(description="Kiro Batch Studio GUI")
    parser.add_argument("--port", type=int, default=8080, help="웹 서버 포트 (기본: 8080)")
    parser.add_argument("--native", action="store_true", help="네이티브 윈도우 창으로 실행")
    args, _ = parser.parse_known_args()

    resolved_base_dir = base_dir or Path(__file__).resolve().parent.parent
    shared_state = get_or_create_runtime_state(resolved_base_dir)
    register_api_routes(nicegui_app, resolved_base_dir, job_manager=shared_state.job_manager)

    @ui.page("/")
    def index_page() -> None:
        build_gui(resolved_base_dir)

    ui.run(title="Kiro Studio", port=args.port, reload=False, dark=True)

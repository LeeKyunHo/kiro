"""
kiro_gui.py
Kiro Studio - SD WebUI Forge 기반 초경량 캐릭터 에셋 배치 생성기 GUI (NiceGUI) 진입점 래퍼.
실제 구현은 gui/ 패키지(settings, state, runner_bridge, components, app)로 분리되어 있다.
"""
from __future__ import annotations

from pathlib import Path

from gui import (
    DEFAULT_SETTINGS,
    SETTINGS_FILENAME,
    GuiLogBufferHandler,
    GuiRuntimeState,
    KiroGuiState,
    attach_gui_log_handler,
    build_base_options,
    build_char_options,
    build_char_zenit_block,
    build_gui,
    check_forge_connection,
    detach_gui_log_handler,
    expand_gui_target_mode,
    format_all_chars_qa_markdown,
    format_qa_warnings_markdown,
    get_or_create_runtime_state,
    load_gui_settings,
    main as _gui_main,
    resolve_gui_mode_and_codes,
    save_gui_settings,
)


def main() -> None:
    _gui_main(base_dir=Path(__file__).resolve().parent)


__all__ = [
    "DEFAULT_SETTINGS",
    "SETTINGS_FILENAME",
    "GuiLogBufferHandler",
    "GuiRuntimeState",
    "KiroGuiState",
    "attach_gui_log_handler",
    "build_base_options",
    "build_char_options",
    "build_char_zenit_block",
    "build_gui",
    "check_forge_connection",
    "detach_gui_log_handler",
    "expand_gui_target_mode",
    "format_all_chars_qa_markdown",
    "format_qa_warnings_markdown",
    "get_or_create_runtime_state",
    "load_gui_settings",
    "main",
    "resolve_gui_mode_and_codes",
    "save_gui_settings",
]


if __name__ in {"__main__", "__mp_main__"}:
    main()

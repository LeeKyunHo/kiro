"""
gui/__init__.py
Kiro Studio GUI 패키지 공개 심볼 엔트리포인트.
"""
from __future__ import annotations

from gui.api_routes import create_api_router, register_api_routes
from gui.app import build_gui, get_or_create_runtime_state, main
from gui.job_manager import GlobalJobManager, JobExecutionContext, global_job_manager
from gui.runner_bridge import (
    build_base_options,
    build_char_options,
    build_char_zenit_block,
    expand_gui_target_mode,
    format_all_chars_qa_markdown,
    format_qa_warnings_markdown,
)
from gui.settings import (
    DEFAULT_SETTINGS,
    SETTINGS_FILENAME,
    load_gui_settings,
    save_gui_settings,
)
from gui.state import (
    GuiLogBufferHandler,
    GuiRuntimeState,
    KiroGuiState,
    attach_gui_log_handler,
    check_forge_connection,
    detach_gui_log_handler,
    resolve_gui_mode_and_codes,
)


__all__ = [
    "DEFAULT_SETTINGS",
    "SETTINGS_FILENAME",
    "GlobalJobManager",
    "GuiLogBufferHandler",
    "GuiRuntimeState",
    "JobExecutionContext",
    "KiroGuiState",
    "attach_gui_log_handler",
    "build_base_options",
    "build_char_options",
    "build_char_zenit_block",
    "build_gui",
    "check_forge_connection",
    "create_api_router",
    "detach_gui_log_handler",
    "expand_gui_target_mode",
    "format_all_chars_qa_markdown",
    "format_qa_warnings_markdown",
    "get_or_create_runtime_state",
    "global_job_manager",
    "load_gui_settings",
    "main",
    "register_api_routes",
    "resolve_gui_mode_and_codes",
    "save_gui_settings",
]

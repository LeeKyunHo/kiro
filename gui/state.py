"""
gui/state.py
Kiro Studio GUI 런타임 상태 캡슐화(GuiRuntimeState), 실행 가드 및 로그 스트리밍 핸들러.
"""
from __future__ import annotations

from collections import deque
import logging
from pathlib import Path
from typing import Callable

from nicegui import app
import requests

from generator.checkpoint import load_checkpoint_profiles
from generator.config import configure_logging, get_api_url
from generator.models import CharacterConfig, PoseDatabase, RosterPaths
from generator.pose_db import load_pose_db, looks_like_code_expr
from generator.reference import find_reference_candidates
from generator.roster import RosterPathManager, load_character
from gui.job_manager import GlobalJobManager, global_job_manager


logger = logging.getLogger("generator.gui")

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
    """Forge/A1111 WebUI 서버 상태를 확인한다."""
    try:
        r = requests.get(get_api_url("/sdapi/v1/sd-models"), timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


class GuiLogBufferHandler(logging.Handler):
    """GUI 로그 뷰어로 generator.* 로그를 스트리밍하는 링 버퍼 핸들러."""

    _kiro_gui_log_handler = True

    def __init__(
        self,
        max_lines: int = 200,
        handler_id: str = "default",
        on_emit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.handler_id = handler_id
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.on_emit = on_emit
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.lines.append(msg)
            if self.on_emit is not None:
                self.on_emit(self.get_text())
        except Exception:
            pass

    def get_text(self) -> str:
        return "\n".join(self.lines)

    def clear(self) -> None:
        self.lines.clear()
        if self.on_emit is not None:
            try:
                self.on_emit("")
            except Exception:
                pass


def attach_gui_log_handler(handler: GuiLogBufferHandler) -> logging.Logger:
    """동일 handler_id를 가진 기존 GUI 핸들러를 제거한 뒤 새 핸들러를 등록해 누수를 방지한다."""
    root_gen_logger = logging.getLogger("generator")
    for existing in list(root_gen_logger.handlers):
        if getattr(existing, "_kiro_gui_log_handler", False) and getattr(
            existing, "handler_id", None
        ) == handler.handler_id:
            root_gen_logger.removeHandler(existing)
    return configure_logging(extra_handler=handler)


def detach_gui_log_handler(handler: GuiLogBufferHandler) -> None:
    """등록된 GUI 로그 핸들러를 안전하게 해제한다."""
    root_gen_logger = logging.getLogger("generator")
    if handler in root_gen_logger.handlers:
        root_gen_logger.removeHandler(handler)


class GuiRuntimeState:
    """GUI 런타임 상태, 중복 실행 방지 가드 및 취소 생명주기 관리 객체."""

    ALL_CHARS_OPTION = "🌟 [ALL] 전체 캐릭터 일괄 순차 생성"

    def __init__(self, base_dir: Path) -> None:
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
        self.job_manager: GlobalJobManager = global_job_manager

    def ensure_static_mounted(self, prefix: str, save_dir: Path) -> None:
        if prefix not in self._mounted_prefixes:
            app.add_static_files(f"/assets/{prefix}", str(save_dir))
            self._mounted_prefixes.add(prefix)

    def update_roster(self, roster_name: str) -> None:
        self.current_roster_name = roster_name
        self.roster = self.rpm.resolve_roster(roster_name)
        self.db = load_pose_db(self.base_dir, self.roster.events_file)
        self.refresh_characters()

    def refresh_characters(self) -> None:
        chars_dir = self.roster.characters_dir
        if chars_dir.is_dir():
            files = sorted(chars_dir.glob("*.json"))
            self.characters = [f.stem for f in files]
        else:
            self.characters = []
        if not self.selected_char_name or (
            self.selected_char_name != self.ALL_CHARS_OPTION
            and self.selected_char_name not in self.characters
        ):
            self.selected_char_name = self.characters[0] if self.characters else ""
        self.load_selected_char()

    def is_all_chars(self) -> bool:
        return self.selected_char_name == self.ALL_CHARS_OPTION

    def load_selected_char(self) -> None:
        if self.is_all_chars() or not self.selected_char_name:
            self.char_cfg = None
            self.ref_path = None
            return

        try:
            self.char_cfg = load_character(self.roster.characters_dir, self.selected_char_name)
            candidates = find_reference_candidates(self.roster.references_dir, self.char_cfg.prefix)
            self.ref_path = candidates[0] if candidates else None
        except Exception as e:
            logger.error("[ERROR] 캐릭터 로드 실패: %s", e)
            self.char_cfg = None
            self.ref_path = None

    def try_begin_run(self, *, allow_preempt: bool = False) -> tuple[bool, int, bool]:
        """중복 실행 방지 가드. 이미 실행 중이고 allow_preempt=False이면 (False, active_run_id, True)를 반환한다."""
        was_running = self.is_running
        if was_running and not allow_preempt:
            return (False, self.active_run_id, True)
        self.active_run_id += 1
        self.is_running = True
        self.cancel_requested = False
        return (True, self.active_run_id, was_running)

    def request_cancel(self) -> int:
        """현재 실행 중인 작업을 취소 처리하고 무효화된 다음 run_id를 반환한다."""
        self.cancel_requested = True
        self.active_run_id += 1
        self.is_running = False
        return self.active_run_id

    def finish_run(self, run_id: int) -> bool:
        """해당 run_id가 여전히 활성 실행인 경우에만 is_running을 해제한다."""
        if self.active_run_id != run_id:
            return False
        self.is_running = False
        return True

    def is_run_cancelled(self, run_id: int) -> bool:
        """해당 run_id가 취소되었거나 후속 실행으로 대체되었는지 판정한다."""
        return self.cancel_requested or (self.active_run_id != run_id)


# 하위 호환 별칭
KiroGuiState = GuiRuntimeState

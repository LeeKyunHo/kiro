"""
generator.pose_background
로스터별 pose_background_mapping.json 규칙(단일 코드·범위)을 해석해
Kiro 플랫 background.json 프리셋 키 및 배경 프롬프트로 자동 매핑하는 서비스.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from generator.prompt import resolve_background

logger = logging.getLogger(__name__)

POSE_BG_MAPPING_FILE = "pose_background_mapping.json"


class PoseBackgroundService:
    """로스터별 포즈-배경 자동 매핑을 관리한다 (Kiro 플랫 background.json 호환)."""

    def __init__(self, projects_dir: Optional[Path] = None) -> None:
        self.projects_dir = projects_dir
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _resolve_roster_dir(self, roster_or_dir: str | Path) -> Path:
        p = Path(roster_or_dir)
        if p.is_dir() or p.is_absolute():
            return p
        base = self.projects_dir if self.projects_dir is not None else (Path.cwd() / "projects")
        return base / str(roster_or_dir)

    def has_mapping(self, roster_or_dir: str | Path) -> bool:
        """해당 로스터에 pose_background_mapping.json 파일이 존재하는지 확인한다."""
        roster_dir = self._resolve_roster_dir(roster_or_dir)
        return (roster_dir / POSE_BG_MAPPING_FILE).is_file()

    def load_mapping(self, roster_or_dir: str | Path) -> Dict[str, Any]:
        """로스터의 pose_background_mapping.json 을 로드한다 (UTF-8-SIG 지원, 부재 시 기본값)."""
        roster_dir = self._resolve_roster_dir(roster_or_dir)
        cache_key = str(roster_dir.resolve())
        if cache_key in self._cache:
            return self._cache[cache_key]

        mapping_file = roster_dir / POSE_BG_MAPPING_FILE
        default_mapping: Dict[str, Any] = {
            "default_background": "default",
            "pose_mappings": {},
        }
        if not mapping_file.is_file():
            self._cache[cache_key] = default_mapping
            return default_mapping

        try:
            with open(mapping_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache[cache_key] = data
                return data
        except Exception as e:
            logger.warning(f"[WARN] 포즈-배경 매핑 로드 실패 ({mapping_file}): {e}")

        self._cache[cache_key] = default_mapping
        return default_mapping

    @staticmethod
    def _normalize_code(pose_code: str | int) -> str:
        """포즈 코드를 정규화한다 (예: 0 -> '000', '44' -> '044', 'A0' -> 'A00')."""
        s = str(pose_code).strip().upper()
        if s.isdigit():
            return f"{int(s):03d}"
        if len(s) >= 2 and s[0].isalpha() and s[1:].isdigit():
            return f"{s[0]}{int(s[1:]):02d}"
        return s

    @classmethod
    def _in_range(cls, norm_code: str, range_key: str) -> bool:
        """정규화된 포즈 코드가 '000-059' 또는 'A00-A32' 범위 키에 속하는지 확인한다."""
        if "-" not in range_key:
            return False
        start_s, end_s = [part.strip().upper() for part in range_key.split("-", 1)]
        if norm_code.isdigit() and start_s.isdigit() and end_s.isdigit():
            return int(start_s) <= int(norm_code) <= int(end_s)
        if (
            len(norm_code) >= 2 and norm_code[0].isalpha() and norm_code[1:].isdigit()
            and len(start_s) >= 2 and start_s[0].isalpha() and start_s[1:].isdigit()
            and len(end_s) >= 2 and end_s[0].isalpha() and end_s[1:].isdigit()
            and norm_code[0] == start_s[0] == end_s[0]
        ):
            return int(start_s[1:]) <= int(norm_code[1:]) <= int(end_s[1:])
        return False

    def get_background_for_pose(self, roster_or_dir: str | Path, pose_code: str | int) -> str:
        """특정 포즈에 매핑된 배경 프리셋 키를 반환한다 (단일 코드 우선 > 범위 매칭 > 기본 배경)."""
        mapping = self.load_mapping(roster_or_dir)
        pose_mappings = mapping.get("pose_mappings") or {}
        default_bg = str(mapping.get("default_background") or "default").strip() or "default"

        raw_code = str(pose_code).strip()
        norm_code = self._normalize_code(pose_code)

        if norm_code in pose_mappings:
            return str(pose_mappings[norm_code])
        if raw_code in pose_mappings:
            return str(pose_mappings[raw_code])

        for key, bg in pose_mappings.items():
            if self._in_range(norm_code, str(key)):
                return str(bg)

        return default_bg

    def get_backgrounds_for_poses(
        self, roster_or_dir: str | Path, pose_codes: Iterable[str | int]
    ) -> Dict[str, str]:
        """여러 포즈 코드에 대한 배경 프리셋 키 매핑을 반환한다."""
        return {str(code): self.get_background_for_pose(roster_or_dir, code) for code in pose_codes}

    def resolve_pose_background(
        self,
        roster_or_dir: str | Path,
        pose_code: str | int,
        *,
        cli_bg: Optional[str] = None,
        explicit_preset: Optional[str] = None,
        bg_file: Optional[Path] = None,
    ) -> Optional[str]:
        """
        최종 배경 프롬프트 문자열을 우선순위에 따라 해석한다.
        우선순위:
          1. cli_bg 직접 지정
          2. explicit_preset 명시 프리셋 ('default' 외 명시 값)
          3. pose_background_mapping.json 존재 시 포즈별 매핑 프리셋 -> 플랫 background.json 조회
          4. 매핑 부재 시 플랫 background.json 의 'default' 프리셋 조회
        """
        roster_dir = self._resolve_roster_dir(roster_or_dir)
        target_bg_file = bg_file if bg_file is not None else (roster_dir / "background.json")

        if cli_bg and cli_bg.strip():
            return cli_bg.strip()

        if explicit_preset and explicit_preset.strip() and explicit_preset.strip() != "default":
            return resolve_background(None, target_bg_file, preset=explicit_preset.strip())

        if self.has_mapping(roster_dir):
            mapped_preset = self.get_background_for_pose(roster_dir, pose_code)
            return resolve_background(None, target_bg_file, preset=mapped_preset)

        return resolve_background(None, target_bg_file, preset="default")

    def clear_cache(self) -> None:
        """메모리 매핑 캐시를 초기화한다."""
        self._cache.clear()


default_pose_bg_service = PoseBackgroundService()

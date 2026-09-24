"""
generator.roster
로스터(프로젝트) 경로 관리, 캐릭터 프리셋 로드 및 설정 적용.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from generator.config import (
    ASSETS_DIRNAME,
    CHARACTERS_DIRNAME,
    DEFAULT_PROFILE,
    PROJECTS_DIRNAME,
    REF_WEIGHT_DEFAULT,
    REFERENCES_DIRNAME,
    ROSTER_ALIAS,
    ConfigError,
)
from generator.models import CharacterConfig, PoseDatabase, RosterPaths
from generator.prompt import join_tags, validate_prefix

# default_mode 값 → --mode 표현식 기본 매핑
_DEFAULT_MODE_MAP: dict[str, str] = {
    "female": "emotions,poses,h_scenes",
    "otokonoko": "emotions,poses,scenes_otokonoko",
}


def _resolve_default_mode(default_mode: str, db: PoseDatabase | None = None) -> str:
    """
    default_mode 문자열을 --mode 표현식으로 변환한다.
    db 에 로스터 전용 이벤트 섹션(event_*)이 등록되어 있으면 기본 실행 목록 뒤에 자동으로 덧붙인다.
    """
    resolved = _DEFAULT_MODE_MAP.get(default_mode.lower())
    if resolved is None:
        print(f"[WARN] 알 수 없는 default_mode '{default_mode}' - 'all' 로 폴백")
        return "all"

    # 로스터 전용 이벤트 섹션 자동 병합
    if db:
        event_sections = [sec for sec in db.sections if sec.startswith("event_")]
        if event_sections:
            resolved = f"{resolved},{','.join(event_sections)}"

    return resolved


class RosterPathManager:
    """
    로스터(프로젝트) 단위 경로 해석 및 동적 탐색.
    
    OCP 원칙: 신규 로스터 추가 시 코드 수정 없이 폴더 생성만으로 자동 인식.
    SRP 원칙: CLI 파싱과 파일 시스템 탐색을 분리.
    """
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
    
    def resolve_roster(self, name: str | None) -> RosterPaths:
        """
        로스터 이름을 실제 경로로 해석.
        1. Alias 테이블 조회 (dar -> dark_generals)
        2. Alias에 없으면 그대로 사용 (xyz -> xyz)
        3. projects/{roster}/ 하위에 characters/, assets/, references/ 구성
        """
        if name is None or name == "":
            name = ROSTER_ALIAS["default"]

        roster_key = ROSTER_ALIAS.get(name, name)

        project_dir = self.base_dir / PROJECTS_DIRNAME / roster_key
        characters_dir = project_dir / CHARACTERS_DIRNAME
        assets_dir = project_dir / ASSETS_DIRNAME
        references_dir = project_dir / REFERENCES_DIRNAME

        return RosterPaths(
            roster_name=roster_key,
            characters_dir=characters_dir,
            assets_dir=assets_dir,
            references_dir=references_dir,
        )

    def list_available_rosters(self) -> list[str]:
        """projects/ 하위에서 characters/ 가 존재하는 유효 로스터 목록 탐색."""
        projects_dir = self.base_dir / PROJECTS_DIRNAME
        if not projects_dir.is_dir():
            return []
        found = []
        for path in projects_dir.iterdir():
            if path.is_dir() and (path / CHARACTERS_DIRNAME).is_dir():
                found.append(path.name)
        return sorted(found)

    def validate_roster(self, paths: RosterPaths) -> tuple[bool, str]:
        """로스터 경로 검증 및 안내 메시지 생성."""
        ok, msg = paths.validate()
        if not ok:
            available = self.list_available_rosters()
            if available:
                avail_list = ", ".join(available)
                msg += f"\n\n사용 가능한 로스터: {avail_list}"
            else:
                msg += f"\n\n{self.base_dir / PROJECTS_DIRNAME} 에 로스터 폴더가 없습니다."
        return ok, msg


def _characters_dir(base_dir: Path) -> Path:
    return base_dir / CHARACTERS_DIRNAME


def load_character(chars_dir: Path, name: str) -> CharacterConfig:
    """{chars_dir}/{name}.json 을 읽어 CharacterConfig 로 변환한다."""
    path = chars_dir / f"{name}.json"
    if not path.is_file():
        raise ConfigError(
            f"캐릭터 프리셋을 찾을 수 없습니다: {path.name}",
            f"{chars_dir} 폴더에 {name}.json 파일이 있는지 확인하세요.",
        )

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path.name} JSON 문법 오류: line {e.lineno}, column {e.colno}: {e.msg}"
        ) from None
    except OSError as e:
        raise ConfigError(f"{path.name} 읽기 실패: {e}") from None

    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} 최상위는 딕셔너리여야 합니다")

    raw_positive = raw.get("positive")
    raw_negative = raw.get("negative")
    positive = raw_positive.strip() if isinstance(raw_positive, str) and raw_positive.strip() else None
    negative = raw_negative.strip() if isinstance(raw_negative, str) and raw_negative.strip() else None

    raw_char_prompt = raw.get("char_prompt", "")
    char_prompt = raw_char_prompt.strip() if isinstance(raw_char_prompt, str) else ""
    if not positive and not char_prompt:
        raise ConfigError(
            f"{path.name} 'char_prompt' 또는 'positive' 중 하나는 있어야 합니다"
        )

    raw_prefix = raw.get("prefix") or name
    prefix = validate_prefix(str(raw_prefix))

    return CharacterConfig(
        name=name,
        char_prompt=char_prompt,
        prefix=prefix,
        profile=raw.get("profile") or None,
        custom_neg=str(raw.get("custom_neg") or "").strip(),
        ref_weight=float(raw["ref_weight"]) if "ref_weight" in raw else None,
        positive=positive,
        negative=negative,
        default_mode=str(raw["default_mode"]).strip() or None if "default_mode" in raw else None,
        background=str(raw["background"]).strip() or None if "background" in raw and isinstance(raw["background"], str) else None,
    )


def list_characters(chars_dir: Path) -> int:
    """캐릭터 디렉터리의 목록을 출력한다."""
    if not chars_dir.is_dir():
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 폴더가 없습니다. 캐릭터를 추가하세요.")
        return 0

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 에 json 파일이 없습니다.")
        return 0

    print(f"\n{'캐릭터':16}  {'프로필':20}  {'prefix'}")
    print("-" * 56)
    errors: list[str] = []
    for path in files:
        try:
            cfg = load_character(chars_dir, path.stem)
            profile_display = cfg.profile or f"(기본값: {DEFAULT_PROFILE})"
            prefix_display = cfg.prefix if cfg.prefix != path.stem else "(파일명과 동일)"
            print(f"  {path.stem:<14}  {profile_display:<20}  {prefix_display}")
        except ConfigError as e:
            errors.append(f"  [ERROR] {path.name}: {e}")

    if errors:
        print()
        for msg in errors:
            print(msg)
        return 1

    print()
    return 0


def apply_character_to_args(
    cfg: CharacterConfig, args: argparse.Namespace, db: PoseDatabase | None = None
) -> None:
    """CharacterConfig 값을 args 에 채운다. 커맨드라인 명시값이 있으면 건드리지 않는다."""
    if not getattr(args, "prefix", None):
        args.prefix = cfg.prefix

    if not getattr(args, "char_prompt", None):
        args.char_prompt = cfg.char_prompt

    if getattr(args, "profile", None) is None and cfg.profile:
        args.profile = cfg.profile

    if cfg.custom_neg:
        existing = (getattr(args, "custom_neg", "") or "").strip()
        args.custom_neg = join_tags(existing, cfg.custom_neg) if existing else cfg.custom_neg

    if cfg.ref_weight is not None and getattr(args, "ref_weight", REF_WEIGHT_DEFAULT) == REF_WEIGHT_DEFAULT:
        args.ref_weight = cfg.ref_weight

    if cfg.positive and not getattr(args, "positive", None):
        args.positive = cfg.positive
    if cfg.negative and not getattr(args, "negative", None):
        args.negative = cfg.negative

    if cfg.default_mode and getattr(args, "mode", "all") == "all":
        args.mode = _resolve_default_mode(cfg.default_mode, db)

    if cfg.background and not getattr(args, "bg", None):
        args.bg = cfg.background

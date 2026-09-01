"""
pose_database.json 로드 및 파싱.

I/O(`read_pose_json`)와 순수 파싱(`parse_pose_database`)을 분리한다.
진단 모듈이 파싱 함수를 합성 픽스처로 직접 호출할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import config
from .errors import DatabaseError, ValidationError
from .logging_setup import get_logger
from .models import PoseDatabase, PoseEntry, Profile
from .tags import parse_exclusive_groups

_logger = get_logger("database")


def iter_pose_sections(raw: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """
    포즈 섹션만 순회한다.

    `_` 로 시작하는 최상위 키는 주석/메타(`_schema`, `_profiles`, `_rules`)이며
    포즈 파싱 대상이 아니다. 이 규칙 덕분에 `_profiles` 를 추가할 때
    별도 필터가 필요하지 않았다.
    """
    for name, body in raw.items():
        if not name.startswith(config.SECTION_COMMENT_PREFIX):
            yield name, body


def read_pose_json(base_dir: Path) -> dict[str, Any]:
    """
    JSON 파일을 읽어 원본 딕셔너리를 반환한다.

    Raises:
        DatabaseError: 파일 부재, 읽기 실패, 문법 오류, 최상위 타입 불일치.
    """
    db_path = base_dir / config.POSE_DB_FILENAME

    try:
        text = db_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DatabaseError(
            f"프롬프트 DB 파일을 찾을 수 없습니다: {db_path}",
            f"{config.POSE_DB_FILENAME} 을 스크립트와 같은 폴더에 두세요.",
        ) from None
    except OSError as exc:
        raise DatabaseError(f"프롬프트 DB 파일을 읽을 수 없습니다: {exc}") from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatabaseError(
            f"JSON 문법 오류: {db_path}",
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
        ) from None

    if not isinstance(raw, dict):
        raise DatabaseError(
            "JSON 최상위는 섹션 딕셔너리여야 합니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )
    return raw


def _parse_profiles(
    raw: dict[str, Any], warnings: list[str]
) -> dict[str, Profile]:
    """`_profiles` 섹션을 Profile 매핑으로 변환한다."""
    section = raw.get(config.PROFILES_KEY)
    if section is None:
        return {}
    if not isinstance(section, dict):
        warnings.append(f"'{config.PROFILES_KEY}' 가 딕셔너리가 아님 - 프로필 무시")
        return {}

    profiles: dict[str, Profile] = {}
    for name, body in section.items():
        if not isinstance(body, dict):
            warnings.append(f"프로필 '{name}' 이 딕셔너리가 아님 - 무시")
            continue

        positive = body.get(config.PROFILE_POSITIVE_KEY)
        negative = body.get(config.PROFILE_NEGATIVE_KEY, "")

        if not isinstance(positive, str) or not positive.strip():
            warnings.append(
                f"프로필 '{name}' 에 {config.PROFILE_POSITIVE_KEY} 가 "
                "없거나 비어 있음 - 무시"
            )
            continue
        if not isinstance(negative, str):
            warnings.append(
                f"프로필 '{name}' 의 {config.PROFILE_NEGATIVE_KEY} 가 "
                "문자열이 아님 - 빈 값 사용"
            )
            negative = ""

        profiles[name] = Profile(name, positive.strip(), negative.strip())
    return profiles


def parse_pose_database(raw: dict[str, Any]) -> PoseDatabase:
    """
    최상위 딕셔너리를 PoseDatabase 로 정규화한다.

    파일 I/O 도 프로세스 종료도 하지 않는 순수 함수다.

    데이터 품질 문제는 경고로 수집하고 해당 항목만 건너뛴다. 50개 항목 중
    오타 하나로 전체 배치가 막히면 운영에 불편하다. 단 경고는 반드시
    눈에 보이게 출력한다.
    """
    warnings: list[str] = []
    entries: dict[int, PoseEntry] = {}
    sections: dict[str, tuple[int, ...]] = {}

    profiles = _parse_profiles(raw, warnings)
    exclusive_groups = parse_exclusive_groups(
        (raw.get(config.RULES_KEY) or {}).get(config.RULES_EXCLUSIVE_KEY)
        if isinstance(raw.get(config.RULES_KEY), dict)
        else None
    )

    for section, body in iter_pose_sections(raw):
        if not isinstance(body, dict):
            warnings.append(f"섹션 '{section}' 이 딕셔너리가 아님 - 무시")
            continue

        section_codes: list[int] = []
        for key, value in body.items():
            try:
                code = int(key)
            except (TypeError, ValueError):
                warnings.append(
                    f"섹션 '{section}' 의 키 '{key}' 는 정수가 아님 - 무시"
                )
                continue

            if not config.CODE_MIN <= code <= config.CODE_MAX:
                warnings.append(
                    f"코드 {code} 가 허용 범위"
                    f"({config.CODE_MIN}~{config.CODE_MAX})를 벗어남 - 무시"
                )
                continue

            if not isinstance(value, str) or not value.strip():
                warnings.append(f"코드 {key} 의 프롬프트가 비어 있음 - 무시")
                continue

            if code in entries:
                previous = entries[code].section
                warnings.append(
                    f"코드 {code} 중복 정의 ('{previous}' -> '{section}') "
                    "- 나중 값 사용"
                )

            entries[code] = PoseEntry(code, value.strip(), section)
            section_codes.append(code)

        sections[section] = tuple(sorted(section_codes))

    return PoseDatabase(
        entries=entries,
        sections=sections,
        profiles=profiles,
        exclusive_groups=exclusive_groups,
        warnings=tuple(warnings),
    )


def load_pose_database(base_dir: Path) -> PoseDatabase:
    """
    JSON 을 읽고 검증까지 완료한 PoseDatabase 를 반환한다.

    Raises:
        DatabaseError: 파일/문법 오류 또는 유효 엔트리 0개.
    """
    database = parse_pose_database(read_pose_json(base_dir))
    if not database.entries:
        raise DatabaseError(
            "유효한 프롬프트 항목이 없습니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )
    return database


def log_warnings(database: PoseDatabase) -> None:
    """로드 경고를 생성 로그 시작 전에 한 번에 출력한다."""
    for message in database.warnings:
        _logger.warning(message)


def resolve_profile(database: PoseDatabase, requested: str | None) -> Profile:
    """
    `--profile` 값을 Profile 로 해석한다.

    `_profiles` 가 없는 JSON 에서는 내장 기본값을 가상 프로필로 감싸
    기존 동작을 그대로 유지한다.

    `--profile` 을 필수로 만들면 실수가 불가능해지지만 기존 명령이 전부
    깨진다. 기본값 + 명시적 로그 출력으로 양쪽을 얻는다.

    Raises:
        ValidationError: 요청한 프로필이 정의되지 않았을 때.
    """
    if not database.profiles:
        if requested:
            raise ValidationError(
                f"프로필 '{requested}' 을 쓸 수 없습니다. "
                f"{config.POSE_DB_FILENAME} 에 '{config.PROFILES_KEY}' 섹션이 없습니다.",
                f"'{config.PROFILES_KEY}' 를 추가하거나 --profile 을 생략하세요.",
            )
        return Profile(
            config.BUILTIN_PROFILE_NAME,
            config.FALLBACK_POSITIVE,
            config.FALLBACK_NEGATIVE,
        )

    if requested:
        if requested not in database.profiles:
            raise ValidationError(
                f"알 수 없는 프로필 '{requested}'. "
                f"사용 가능: {list(database.profile_names)}",
                f"{config.POSE_DB_FILENAME} 의 "
                f"'{config.PROFILES_KEY}' 섹션을 확인하세요.",
            )
        return database.profiles[requested]

    if config.DEFAULT_PROFILE_NAME in database.profiles:
        return database.profiles[config.DEFAULT_PROFILE_NAME]

    # 기본 프로필명이 없으면 정의 순서상 첫 프로필로 폴백한다.
    return next(iter(database.profiles.values()))


def peek_choices(base_dir: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """
    `--help` 문구용 (섹션명, 프로필명) 을 조회한다.

    argparse 의 `choices` 는 파서 생성 시점에 확정되므로 쓸 수 없지만,
    help 문구는 JSON 을 먼저 읽어 동적으로 채울 수 있다.

    읽기에 실패해도 `--help` 자체는 동작해야 하므로 예외를 삼킨다.
    """
    try:
        raw = read_pose_json(base_dir)
    except DatabaseError:
        return (), ()

    sections = tuple(
        name for name, body in iter_pose_sections(raw) if isinstance(body, dict)
    )
    profiles = tuple(_parse_profiles(raw, []))
    return sections, profiles

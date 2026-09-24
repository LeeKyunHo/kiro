"""
generator.pose_db
pose_database.json 및 events.json 파싱, 프로필/모드/코드 타겟 해석.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from generator.config import (
    CODE_EXPR_PATTERN,
    COMMON_NEG,
    DEFAULT_PROFILE,
    FALLBACK_PROFILE,
    MAX_CODE,
    POSE_DB_FILE,
    POS_BASE,
    PROFILE_NEGATIVE_KEY,
    PROFILE_POSITIVE_KEY,
    PROFILES_KEY,
    SECTION_COMMENT_PREFIX,
    ConfigError,
)
from generator.models import PoseDatabase, PoseEntry, Profile


def _iter_sections(raw: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    """주석 섹션(_ 시작)을 걸러 순회한다."""
    for name, body in raw.items():
        if not name.startswith(SECTION_COMMENT_PREFIX):
            yield name, body


def _parse_profiles(raw: dict[str, Any], warnings: list[str]) -> dict[str, Profile]:
    """_profiles 섹션을 Profile 매핑으로 변환한다."""
    section = raw.get(PROFILES_KEY)
    if section is None:
        return {}
    if not isinstance(section, dict):
        warnings.append(f"'{PROFILES_KEY}' 가 딕셔너리가 아님 - 프로필 무시")
        return {}

    profiles: dict[str, Profile] = {}
    for name, body in section.items():
        if not isinstance(body, dict):
            warnings.append(f"프로필 '{name}' 이 딕셔너리가 아님 - 무시")
            continue

        positive = body.get(PROFILE_POSITIVE_KEY)
        negative = body.get(PROFILE_NEGATIVE_KEY, "")

        if not isinstance(positive, str) or not positive.strip():
            warnings.append(
                f"프로필 '{name}' 에 {PROFILE_POSITIVE_KEY} 가 없거나 비어 있음 - 무시"
            )
            continue
        if not isinstance(negative, str):
            warnings.append(f"프로필 '{name}' 의 {PROFILE_NEGATIVE_KEY} 가 문자열이 아님 - 빈 값 사용")
            negative = ""

        profiles[name] = Profile(name, positive.strip(), negative.strip())

    return profiles


def parse_pose_db(raw: dict[str, Any]) -> PoseDatabase:
    """최상위 섹션 딕셔너리를 PoseDatabase 로 정규화한다 (순수 함수)."""
    db = PoseDatabase()
    db.profiles = _parse_profiles(raw, db.warnings)

    for section, body in _iter_sections(raw):
        if not isinstance(body, dict):
            db.warnings.append(f"섹션 '{section}' 이 딕셔너리가 아님 - 무시")
            continue

        section_codes: list[int] = []
        for key, value in body.items():
            try:
                code = int(key)
            except (TypeError, ValueError):
                db.warnings.append(
                    f"섹션 '{section}' 의 키 '{key}' 는 정수가 아님 - 무시"
                )
                continue

            if isinstance(value, dict):
                prompt = value.get("prompt", "")
                width = value.get("width")
                height = value.get("height")
                if not isinstance(prompt, str) or not prompt.strip():
                    db.warnings.append(f"코드 {key} 의 prompt 가 비어 있음 - 무시")
                    continue
            elif isinstance(value, str):
                prompt = value
                width = None
                height = None
            else:
                db.warnings.append(f"코드 {key} 의 값이 문자열/딕셔너리가 아님 - 무시")
                continue

            if not prompt.strip():
                db.warnings.append(f"코드 {key} 의 프롬프트가 비어 있음 - 무시")
                continue

            if code in db.entries:
                previous = db.entries[code].section
                db.warnings.append(
                    f"코드 {code} 중복 정의 ('{previous}' -> '{section}') - 나중 값 사용"
                )

            db.entries[code] = PoseEntry(code, prompt.strip(), section, width, height)
            section_codes.append(code)

        db.sections[section] = sorted(section_codes)

    return db


def read_pose_json(base_dir: Path) -> dict[str, Any]:
    """pose_database.json 을 읽어 원본 딕셔너리를 반환한다."""
    db_path = base_dir / POSE_DB_FILE

    try:
        text = db_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(
            f"프롬프트 DB 파일을 찾을 수 없습니다: {db_path}",
            f"{POSE_DB_FILE} 을 스크립트와 같은 폴더에 두세요.",
        ) from None
    except OSError as e:
        raise ConfigError(f"프롬프트 DB 파일을 읽을 수 없습니다: {e}") from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"JSON 문법 오류: {db_path}",
            f"line {e.lineno}, column {e.colno}: {e.msg}",
        ) from None

    if not isinstance(raw, dict):
        raise ConfigError(
            "JSON 최상위는 섹션 딕셔너리여야 합니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )

    return raw


def load_pose_db(base_dir: Path, events_file: Path | None = None) -> PoseDatabase:
    """JSON 을 읽고 검증까지 완료한 PoseDatabase 를 반환한다."""
    raw = read_pose_json(base_dir)

    if events_file and events_file.exists():
        try:
            with open(events_file, encoding="utf-8-sig") as f:
                events_raw = json.load(f)
            if isinstance(events_raw, dict):
                merged_sections: list[str] = []
                for sec, body in events_raw.items():
                    if sec.startswith(SECTION_COMMENT_PREFIX):
                        continue
                    if isinstance(body, dict):
                        if sec not in raw:
                            raw[sec] = {}
                        raw[sec].update(body)
                        merged_sections.append(sec)
                if merged_sections:
                    print(f"[EVENTS] 로스터 전용 이벤트 병합: {events_file.name} ({', '.join(merged_sections)})")
        except Exception as e:
            print(f"[WARN] 로스터 이벤트 파일({events_file}) 읽기 실패: {e}")

    db = parse_pose_db(raw)

    if not db.entries:
        raise ConfigError(
            "유효한 프롬프트 항목이 없습니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )

    return db


def peek_choices(base_dir: Path) -> tuple[list[str], list[str]]:
    """--help 문구용 (섹션명, 프로필명) 목록."""
    try:
        raw = read_pose_json(base_dir)
    except ConfigError:
        return [], []

    sections = [name for name, body in _iter_sections(raw) if isinstance(body, dict)]
    profiles = list(_parse_profiles(raw, []))
    return sections, profiles


def print_warnings(db: PoseDatabase) -> None:
    """로드 경고를 생성 로그 시작 전에 한 번에 출력한다."""
    if not db.warnings:
        return
    for message in db.warnings:
        print(f"[WARN] {message}")
    print()


def resolve_profile(db: PoseDatabase, requested: str | None) -> Profile:
    """--profile 값을 Profile 로 해석한다."""
    if not db.profiles:
        if requested:
            raise ConfigError(
                f"프로필 '{requested}' 을 쓸 수 없습니다. "
                f"{POSE_DB_FILE} 에 '{PROFILES_KEY}' 섹션이 없습니다.",
                f"'{PROFILES_KEY}' 를 추가하거나 --profile 을 생략하세요.",
            )
        return Profile(FALLBACK_PROFILE, POS_BASE, COMMON_NEG)

    if requested:
        if requested not in db.profiles:
            raise ConfigError(
                f"알 수 없는 프로필 '{requested}'. 사용 가능: {db.profile_names}",
                f"{POSE_DB_FILE} 의 '{PROFILES_KEY}' 섹션을 확인하세요.",
            )
        return db.profiles[requested]

    if DEFAULT_PROFILE in db.profiles:
        return db.profiles[DEFAULT_PROFILE]

    return next(iter(db.profiles.values()))


def _parse_code_token(token: str) -> Iterable[int]:
    """단일 토큰('7' 또는 '10-14')을 코드로 확장한다."""
    if "-" in token:
        start_text, _, end_text = token.partition("-")
        start, end = int(start_text.strip()), int(end_text.strip())
        if start > end:
            start, end = end, start
        if start < 0:
            raise ValueError(f"음수 코드는 허용되지 않습니다: {token}")
        if end > MAX_CODE:
            raise ValueError(f"코드 상한({MAX_CODE})을 초과했습니다: {token}")
        return range(start, end + 1)

    code = int(token)
    if not 0 <= code <= MAX_CODE:
        raise ValueError(f"코드는 0~{MAX_CODE} 범위여야 합니다: {token}")
    return (code,)


def parse_codes_expr(expr: str) -> list[int]:
    """코드 표현식을 정수 리스트로 변환한다."""
    codes: set[int] = set()
    for raw_token in expr.split(","):
        token = raw_token.strip()
        if token:
            codes.update(_parse_code_token(token))
    return sorted(codes)


def looks_like_code_expr(value: str) -> bool:
    """숫자·콤마·하이픈·공백만으로 구성되면 코드 표현식으로 간주한다."""
    return bool(value) and CODE_EXPR_PATTERN.match(value) is not None


def _pick_code_expr(mode: str, codes_expr: str | None) -> str | None:
    """--codes 와 --mode 중 코드 표현식으로 쓸 값을 고른다."""
    if codes_expr:
        if mode and mode != "all" and looks_like_code_expr(mode):
            print(f"[WARN] --codes 가 우선합니다. --mode '{mode}' 무시됨")
        return codes_expr
    if mode and looks_like_code_expr(mode):
        return mode
    return None


def resolve_targets(
    db: PoseDatabase, mode: str, codes_expr: str | None = None
) -> list[int]:
    """--codes / --mode 를 해석해 순회 대상 코드를 반환한다."""
    expr = _pick_code_expr(mode, codes_expr)

    if expr is not None:
        try:
            requested = parse_codes_expr(expr)
        except ValueError as e:
            raise ConfigError(
                f"코드 표현식을 해석할 수 없습니다: '{expr}' ({e})",
                "예: 20-29 / 0,3,7 / 0-5,10,20-22",
            ) from None

        if missing := [code for code in requested if code not in db.entries]:
            print(f"[WARN] DB에 없는 코드 무시: {missing}")
        return [code for code in requested if code in db.entries]

    if mode == "all":
        return db.all_codes
    if mode in db.sections:
        return db.sections[mode]

    tokens = [t.strip() for t in mode.split(",") if t.strip()]
    if len(tokens) > 1 and all(t in db.sections for t in tokens):
        seen: set[int] = set()
        result: list[int] = []
        for token in tokens:
            for code in db.sections[token]:
                if code not in seen:
                    seen.add(code)
                    result.append(code)
        return sorted(result)

    raise ConfigError(
        f"알 수 없는 모드 '{mode}'. 사용 가능: {['all'] + sorted(db.sections)}",
        "코드 리스트 직접 지정도 가능합니다. 예: --mode 0,5,12 / --mode 10-14",
    )

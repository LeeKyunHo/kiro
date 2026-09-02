"""
characters.json 로드, 파싱, 그리고 CLI 인자와의 병합.

`database.py` 와 같은 구조를 쓴다. I/O(`read_characters_json`)와 순수
파싱(`parse_roster`)을 분리하고, 병합(`merge_character`)은 파일도 전역
상태도 건드리지 않는 순수 함수로 둔다. 진단이 합성 픽스처로 세 함수를
각각 직접 호출할 수 있어야 하기 때문이다.

**우선순위 규칙**

    CLI 명시값  >  프리셋  >  내장 기본값

이 규칙을 정직하게 구현하려면 "사용자가 `--mode all` 을 명시했다" 와
"argparse 기본값 all 이 채워졌다" 를 구분해야 한다. 구분할 수 없으면
프리셋의 `mode` 가 영원히 무시된다. 그래서 프리셋 대상 인자의 argparse
기본값을 전부 `None` 으로 바꾸고, 기본값 채우기를 이 모듈 한 곳으로
모았다.

부수 효과로 `--custom_neg ""` 가 "프리셋 네거티브를 비워라" 라는 명확한
의사표시가 된다. 빈 문자열이 argparse 기본값이면 이 의도를 표현할 방법이
없다.

**프리셋이 담지 않는 것**

`--mock` / `--dry-run` / `--test` / `--benchmark` 는 실행 의도이고
`--cn_module` / `--cn_model` 은 환경 설정이다. 둘 다 캐릭터의 속성이
아니므로 프리셋 축에서 제외한다. 캐릭터 파일이 실행 스크립트로 변질되면
"이 캐릭터를 돌리면 왜 mock 이 나오지" 같은 사고가 난다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, TypeVar

from . import config
from .errors import DatabaseError, ValidationError
from .logging_setup import get_logger
from .models import CharacterPreset, CharacterRoster, ResolvedCharacter
from .validators import validate_prefix, validate_ref_weight

_logger = get_logger("roster")

_T = TypeVar("_T")

# 병합에서 오버라이드 여부를 판정할 축. CLI 플래그 이름과 프리셋 필드
# 이름이 같아 매핑 테이블이 필요 없다.
_MERGE_FIELDS: Final = (
    config.CHAR_FIELD_PROMPT,
    config.CHAR_FIELD_PROFILE,
    config.CHAR_FIELD_NEGATIVE,
    config.CHAR_FIELD_REF_WEIGHT,
    config.CHAR_FIELD_REF_IMAGE,
    config.CHAR_FIELD_MODE,
)


# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────
def read_characters_json(base_dir: Path) -> dict[str, Any] | None:
    """
    characters.json 을 읽어 원본 딕셔너리를 반환한다.

    파일이 없으면 `None` 을 반환한다. 예외로 만들지 않는 이유: 이 파일은
    선택 기능이고 `--char` 를 쓰지 않는 사용자에게는 없는 것이 정상이다.
    `pose_database.json` 은 없으면 아무것도 못 하므로 예외가 맞지만,
    같은 정책을 여기 적용하면 기존 명령이 전부 깨진다.

    Raises:
        DatabaseError: 읽기 실패, 문법 오류, 최상위 타입 불일치.
                       파일이 있는데 깨진 것은 조용히 넘기지 않는다.
    """
    path = base_dir / config.CHARACTERS_FILENAME

    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatabaseError(
            f"캐릭터 프리셋 파일을 읽을 수 없습니다: {path}", str(exc)
        ) from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatabaseError(
            f"JSON 문법 오류: {path}",
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
        ) from None

    if not isinstance(raw, dict):
        raise DatabaseError(
            f"{config.CHARACTERS_FILENAME} 최상위는 캐릭터 딕셔너리여야 합니다.",
            '예: {"mika": {"char_prompt": "silver hair, blue eyes"}}',
        )
    return raw


# ─────────────────────────────────────────────
# 순수 파싱
# ─────────────────────────────────────────────
def _read_text_field(
    body: dict[str, Any], key: str, name: str, warnings: list[str]
) -> str | None:
    """문자열 필드를 읽는다. 부재는 None, 타입 불일치는 경고 후 None."""
    if key not in body:
        return None
    value = body[key]
    if not isinstance(value, str):
        warnings.append(f"캐릭터 '{name}' 의 {key} 가 문자열이 아님 - 무시")
        return None
    return value.strip()


def _read_optional_text(
    body: dict[str, Any], key: str, name: str, warnings: list[str]
) -> str | None:
    """
    빈 문자열을 None 으로 접는 변형.

    `profile`, `ref_image`, `mode` 는 "빈 값" 이라는 상태가 의미를 갖지
    않는다. `""` 를 그대로 두면 하위 계층이 빈 프로필명을 조회하게 된다.
    반면 `custom_neg` 는 빈 문자열이 "비워라" 라는 유효한 의사표시이므로
    `_read_text_field` 를 그대로 쓴다.
    """
    value = _read_text_field(body, key, name, warnings)
    return value or None


def _read_weight(
    body: dict[str, Any], name: str, warnings: list[str]
) -> float | None:
    """
    ref_weight 를 읽는다.

    `bool` 을 배제하는 이유: 파이썬에서 `isinstance(True, int)` 가 참이므로
    `"ref_weight": true` 가 1.0 으로 조용히 통과한다. JSON 편집 실수를
    값으로 받아들이면 안 된다.
    """
    key = config.CHAR_FIELD_REF_WEIGHT
    if key not in body:
        return None

    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"캐릭터 '{name}' 의 {key} 가 숫자가 아님 - 무시")
        return None

    try:
        return validate_ref_weight(float(value))
    except ValidationError as exc:
        warnings.append(f"캐릭터 '{name}' 의 {key} 무효 ({exc.message}) - 무시")
        return None


def _is_valid_name(name: str) -> bool:
    """
    캐릭터 이름이 prefix 규격을 만족하는지.

    정규식을 다시 쓰지 않고 `validate_prefix` 를 재사용한다. 규칙이 두 곳에
    있으면 한쪽만 고쳐져 어긋난다. 여기서는 예외가 아니라 경고로 처리해야
    하므로 잡아서 불리언으로 바꾼다.
    """
    try:
        return validate_prefix(name) == name
    except ValidationError:
        return False


def parse_roster(raw: dict[str, Any] | None) -> CharacterRoster:
    """
    최상위 딕셔너리를 CharacterRoster 로 정규화한다.

    파일 I/O 도 프로세스 종료도 하지 않는 순수 함수다.

    데이터 품질 문제는 경고로 수집하고 해당 항목만 건너뛴다. 캐릭터 10명
    중 한 명의 오타로 나머지 9명을 못 쓰게 만들 이유가 없다.
    `pose_database.json` 파싱과 동일한 정책이다.
    """
    if raw is None:
        return CharacterRoster(available=False)

    warnings: list[str] = []
    entries: dict[str, CharacterPreset] = {}

    for name, body in raw.items():
        # '_' 로 시작하는 키는 메타다. pose_database.json 과 같은 규약이라
        # 별도 필터 목록을 관리할 필요가 없다.
        if name.startswith(config.SECTION_COMMENT_PREFIX):
            continue

        # 캐릭터 이름은 그대로 prefix 가 되어 경로와 파일명에 들어간다.
        # 로드 시점에 거르지 않으면 실행 직전에야 실패한다.
        if not _is_valid_name(name):
            warnings.append(
                f"캐릭터 이름 '{name}' 이 prefix 규격을 벗어남 "
                "(영문·숫자·_·- 1~64자) - 무시"
            )
            continue

        if not isinstance(body, dict):
            warnings.append(f"캐릭터 '{name}' 이 딕셔너리가 아님 - 무시")
            continue

        if unknown := sorted(set(body) - config.CHAR_FIELDS):
            # 조용히 무시하면 'char_promt' 같은 오타가 "프리셋이 안 먹는다"
            # 로만 드러나 원인을 찾기 어렵다.
            warnings.append(f"캐릭터 '{name}' 의 알 수 없는 필드: {unknown}")

        prompt = _read_text_field(body, config.CHAR_FIELD_PROMPT, name, warnings)
        if not prompt:
            warnings.append(
                f"캐릭터 '{name}' 에 {config.CHAR_FIELD_PROMPT} 가 "
                "없거나 비어 있음 - 무시"
            )
            continue

        entries[name] = CharacterPreset(
            name=name,
            char_prompt=prompt,
            profile=_read_optional_text(
                body, config.CHAR_FIELD_PROFILE, name, warnings
            ),
            custom_neg=_read_text_field(
                body, config.CHAR_FIELD_NEGATIVE, name, warnings
            ),
            ref_weight=_read_weight(body, name, warnings),
            ref_image=_read_optional_text(
                body, config.CHAR_FIELD_REF_IMAGE, name, warnings
            ),
            mode=_read_optional_text(body, config.CHAR_FIELD_MODE, name, warnings),
            note=_read_text_field(body, config.CHAR_FIELD_NOTE, name, warnings) or "",
        )

    return CharacterRoster(entries=entries, available=True, warnings=tuple(warnings))


# ─────────────────────────────────────────────
# 로드 및 조회
# ─────────────────────────────────────────────
def load_roster(base_dir: Path) -> CharacterRoster:
    """
    characters.json 을 읽고 정규화한다. 파일이 없으면 빈 로스터를 반환한다.

    Raises:
        DatabaseError: 파일이 있으나 문법·스키마가 깨졌을 때.
    """
    return parse_roster(read_characters_json(base_dir))


def log_warnings(roster: CharacterRoster) -> None:
    """로드 경고를 생성 로그 시작 전에 한 번에 출력한다."""
    for message in roster.warnings:
        _logger.warning(message)


def resolve_preset(roster: CharacterRoster, name: str) -> CharacterPreset:
    """
    `--char` 값을 CharacterPreset 으로 해석한다.

    Raises:
        ValidationError: 파일 부재, 항목 0개, 또는 미등록 이름.
                         세 경우의 안내를 구분한다. "없다" 만 알려주고
                         무엇을 해야 하는지 안 알려주면 사용자가 막힌다.
    """
    if not roster.available:
        raise ValidationError(
            f"{config.CHARACTERS_FILENAME} 이 없어 --char 를 쓸 수 없습니다.",
            f"{config.CHARACTERS_FILENAME} 을 만들거나 "
            "--prefix / --char_prompt 를 직접 지정하세요.",
        )

    if not roster.entries:
        raise ValidationError(
            f"{config.CHARACTERS_FILENAME} 에 등록된 캐릭터가 없습니다.",
            '예: {"mika": {"char_prompt": "silver hair, blue eyes"}}',
        )

    preset = roster.get(name)
    if preset is None:
        raise ValidationError(
            f"등록되지 않은 캐릭터 '{name}'. 사용 가능: {list(roster.names)}",
            f"{config.CHARACTERS_FILENAME} 을 확인하세요.",
        )
    return preset


def peek_character_names(base_dir: Path) -> tuple[str, ...]:
    """
    `--help` 문구용 캐릭터 이름 목록을 조회한다.

    읽기에 실패해도 `--help` 자체는 동작해야 하므로 예외를 삼킨다.
    `database.peek_choices` 와 같은 정책이다.
    """
    try:
        return parse_roster(read_characters_json(base_dir)).names
    except DatabaseError:
        return ()


# ─────────────────────────────────────────────
# 병합 (순수)
# ─────────────────────────────────────────────
def _pick(cli_value: _T | None, preset_value: _T | None, fallback: _T) -> _T:
    """CLI > 프리셋 > 기본값 순으로 첫 비-None 값을 고른다."""
    if cli_value is not None:
        return cli_value
    if preset_value is not None:
        return preset_value
    return fallback


def _pick_optional(cli_value: _T | None, preset_value: _T | None) -> _T | None:
    """기본값이 없는 축. 둘 다 없으면 None 을 유지한다."""
    return cli_value if cli_value is not None else preset_value


def merge_character(
    preset: CharacterPreset | None,
    *,
    prefix: str | None = None,
    char_prompt: str | None = None,
    profile: str | None = None,
    custom_neg: str | None = None,
    ref_weight: float | None = None,
    ref_image: str | None = None,
    mode: str | None = None,
) -> ResolvedCharacter:
    """
    프리셋과 CLI 값을 병합해 모든 축이 확정된 결과를 만든다.

    순수 함수다. 파일도 Namespace 도 건드리지 않는다. 호출부가 결과를
    Namespace 에 되쓰는 것은 `cli` 의 몫이다.

    Args:
        preset: `--char` 로 선택된 프리셋. `None` 이면 CLI 값만 쓴다.
        prefix ~ mode: CLI 에서 명시된 값. 명시되지 않은 축은 `None`.

    Returns:
        ResolvedCharacter. `mode`, `ref_weight`, `custom_neg` 는 기본값까지
        채워지므로 절대 `None` 이 아니다.

        `prefix` 와 `char_prompt` 는 양쪽 모두 없을 때 `None` 으로 남긴다.
        여기서 임의로 채우면 argparse 의 "필수 인자 누락" 메시지가 사라지고
        대신 알 수 없는 지점에서 실패한다.

    프리셋 이름은 prefix 의 기본값이 된다. `--char mika --prefix mika_v2`
    로 같은 외형을 다른 폴더에 뽑는 변형 실험이 가능하다.
    """
    # CLI 값을 필드 이름으로 색인해 오버라이드를 판정한다. 프리셋이 값을
    # 정했는데 CLI 가 값을 준 축만 "덮어썼다" 로 본다. 프리셋이 정하지
    # 않은 축에 CLI 값을 주는 것은 오버라이드가 아니라 그냥 지정이다.
    cli_values: dict[str, object | None] = {
        config.CHAR_FIELD_PROMPT: char_prompt,
        config.CHAR_FIELD_PROFILE: profile,
        config.CHAR_FIELD_NEGATIVE: custom_neg,
        config.CHAR_FIELD_REF_WEIGHT: ref_weight,
        config.CHAR_FIELD_REF_IMAGE: ref_image,
        config.CHAR_FIELD_MODE: mode,
    }
    overridden: tuple[str, ...] = ()
    if preset is not None:
        overridden = tuple(
            name
            for name in _MERGE_FIELDS
            if cli_values[name] is not None and preset.field_value(name) is not None
        )

    return ResolvedCharacter(
        prefix=_pick_optional(prefix, preset.name if preset else None),
        char_prompt=_pick_optional(
            char_prompt, preset.char_prompt if preset else None
        ),
        profile=_pick_optional(profile, preset.profile if preset else None),
        custom_neg=_pick(custom_neg, preset.custom_neg if preset else None, ""),
        ref_weight=_pick(
            ref_weight,
            preset.ref_weight if preset else None,
            config.REF_WEIGHT_DEFAULT,
        ),
        ref_image=_pick_optional(ref_image, preset.ref_image if preset else None),
        mode=_pick(mode, preset.mode if preset else None, config.MODE_DEFAULT),
        preset_name=preset.name if preset else None,
        overridden=overridden,
    )


# ─────────────────────────────────────────────
# 로그 및 검증
# ─────────────────────────────────────────────
def describe(resolved: ResolvedCharacter) -> str:
    """로그 한 줄용 요약."""
    parts = [f"prefix={resolved.prefix}", f"mode={resolved.mode}"]
    if resolved.profile:
        parts.append(f"profile={resolved.profile}")
    parts.append(f"ref_weight={resolved.ref_weight:g}")
    return f"{resolved.source} | {' | '.join(parts)}"


def log_resolution(resolved: ResolvedCharacter) -> None:
    """
    프리셋 적용 결과를 로그로 남긴다.

    프리셋을 쓰면 화면에 보이지 않는 값이 프롬프트에 들어간다. 무엇이
    적용됐는지 출력하지 않으면 사용자가 결과를 되짚을 수 없다.
    """
    if not resolved.from_preset:
        return

    _logger.info("[CHAR]  %s", describe(resolved))
    if resolved.overridden:
        _logger.info("        CLI 가 덮어쓴 축: %s", ", ".join(resolved.overridden))
    if resolved.char_prompt:
        _logger.info("        외형: %s", resolved.char_prompt)


def audit_preset(
    preset: CharacterPreset,
    *,
    profile_names: tuple[str, ...] = (),
    section_names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """
    프리셋 정의 자체의 문제를 검사해 경고 메시지 목록을 반환한다.

    `--test` 에서 characters.json 을 편집한 직후 확인하는 용도다. 런타임에도
    같은 문제가 드러나지만 실행 전에 아는 것이 낫다.

    예외를 올리지 않고 메시지를 반환하는 이유는 `validators.audit_profile`
    과 같다. 항목 하나로 배치를 막으면 운영에 불편하다.

    지연 import 를 쓰는 이유: `codes` 와 `tags` 는 이 모듈의 주 경로
    (로드·병합)에 필요하지 않다. 검증 경로에서만 쓰이므로 import 를 함수
    안에 두어 의존 방향을 좁힌다.
    """
    from .codes import looks_like_code_expression
    from .tags import partition_gender_tags, split_tags

    messages: list[str] = []

    if preset.profile and profile_names and preset.profile not in profile_names:
        messages.append(
            f"프로필 '{preset.profile}' 이 {config.POSE_DB_FILENAME} 에 없음 "
            f"(사용 가능: {list(profile_names)})"
        )

    # 성별 태그는 프로필 축에서 다룬다. char_prompt 에 넣으면 프로필
    # 네거티브와 충돌한다.
    _kept, gender = partition_gender_tags(split_tags(preset.char_prompt))
    if gender:
        messages.append(
            f"{config.CHAR_FIELD_PROMPT} 에 성별·인원 태그가 있음: {list(gender)} "
            f"({config.PROFILES_KEY} 에서 다루므로 제거 권장)"
        )

    if preset.mode:
        known = preset.mode == config.MODE_ALL or preset.mode in section_names
        if not known and not looks_like_code_expression(preset.mode):
            messages.append(
                f"{config.CHAR_FIELD_MODE} '{preset.mode}' 가 "
                f"'{config.MODE_ALL}' 도 섹션명도 코드 표현식도 아님"
            )

    return tuple(messages)

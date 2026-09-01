"""
입력값 검증 — 순수 계층.

검증 실패는 예외로 표현하고 종료 코드는 예외 클래스가 들고 있다.
검증 함수가 직접 `sys.exit()` 하지 않는 이유는 단위 검증에서 함수를
그대로 호출할 수 있어야 하고, 종료 정책이 여러 파일에 흩어지면 안 되기
때문이다.
"""

from __future__ import annotations

import re
from typing import Final

from . import config
from .errors import ValidationError
from .models import Profile
from .tags import find_duplicate_tags, find_exclusive_conflicts

_SAFE_PREFIX: Final = re.compile(config.SAFE_PREFIX_PATTERN_SOURCE)


def validate_prefix(prefix: str | None) -> str:
    """
    prefix 를 안전한 단일 경로 세그먼트로 제한한다.

    prefix 는 저장 경로와 파일명에 그대로 들어가므로, 검증하지 않으면
    `../` 로 대상 폴더를 벗어나거나 인용부호로 셸 인자를 깨뜨릴 수 있다.
    화이트리스트 방식이라 새 위험 문자가 생겨도 자동으로 막힌다.

    이 검증이 통과한 값만 `CodeFormatter.filename()` 에 전달되므로,
    별도의 파일명 정제(sanitize)가 필요하지 않다.

    Raises:
        ValidationError: 규격을 벗어난 값.
    """
    candidate = (prefix or "").strip()
    if not _SAFE_PREFIX.match(candidate):
        raise ValidationError(
            f"prefix '{prefix}' 를 사용할 수 없습니다.",
            "영문·숫자·밑줄·하이픈 1~64자만 허용합니다. (예: mika, test_01)",
        )
    return candidate


def validate_ref_weight(value: float) -> float:
    """
    IP-Adapter 적용 강도를 검증한다.

    Raises:
        ValidationError: 범위를 벗어난 값.
    """
    if not config.REF_WEIGHT_MIN <= value <= config.REF_WEIGHT_MAX:
        raise ValidationError(
            f"--ref_weight 는 {config.REF_WEIGHT_MIN}~{config.REF_WEIGHT_MAX} "
            f"범위여야 합니다: {value}",
            "0.5~0.8 이 실무 범위입니다. 1.0 이상은 참조 이미지의 포즈까지 전이됩니다.",
        )
    return float(value)


def validate_interrogator(name: str) -> str:
    """태그 역추출 모델명을 검증한다."""
    if name not in config.INTERROGATORS:
        raise ValidationError(
            f"알 수 없는 추출 모델 '{name}'.",
            f"사용 가능: {', '.join(config.INTERROGATORS)}",
        )
    return name


def audit_prompt_conflicts(
    positive: str,
    negative: str,
    exclusive_groups: tuple[frozenset[str], ...] = (),
) -> tuple[str, ...]:
    """
    프롬프트 충돌을 검사해 경고 메시지 목록을 반환한다.

    두 종류를 함께 본다.

    1. **동일 문자열 충돌** — 같은 태그가 포지티브와 네거티브에 동시 존재.
       모델이 모순된 지시를 받는다.
    2. **의미적 상충** — `1girl` 과 `1boy` 처럼 문자열은 다르지만 의미가
       충돌. 상호배타 그룹 목록으로 검사한다.

    예외를 올리지 않고 메시지를 반환하는 이유: 의도적으로 같은 태그를
    양쪽에 두는 프롬프트 기법이 존재하고, 항목 하나로 배치를 막으면
    운영에 불편하다. 호출부가 경고로 출력한다.
    """
    messages: list[str] = []

    if duplicates := find_duplicate_tags(positive, negative):
        messages.append(
            f"태그 충돌: {list(duplicates)} 가 포지티브와 네거티브에 동시 존재"
        )

    groups = exclusive_groups or None
    for conflict in find_exclusive_conflicts(positive, groups):
        messages.append(f"상호배타 태그가 포지티브에 함께 있음: {list(conflict)}")
    for conflict in find_exclusive_conflicts(negative, groups):
        messages.append(f"상호배타 태그가 네거티브에 함께 있음: {list(conflict)}")

    return tuple(messages)


def audit_profile(
    profile: Profile, exclusive_groups: tuple[frozenset[str], ...] = ()
) -> tuple[str, ...]:
    """
    프로필 정의 자체의 충돌을 검사한다.

    `--test` 에서 JSON 을 편집한 직후 확인하는 용도다. 런타임 검사만으로는
    프로필 내부 모순을 잡을 수 없다 (char_prompt 와 섞인 뒤에 보게 되므로).
    """
    return audit_prompt_conflicts(
        profile.base_positive, profile.base_negative, exclusive_groups
    )

"""
태그 정규화 및 충돌 감지 — 순수 계층.

이 모듈은 config 와 stdlib 만 참조한다. 네트워크·파일·전역 상태에
접근하지 않으므로 GPU 없는 환경에서 전량 검증할 수 있다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from . import config

_BRACKET_TABLE: Final = str.maketrans("", "", config.BRACKET_CHARS)
_WEIGHT_SUFFIX: Final = re.compile(config.WEIGHT_SUFFIX_PATTERN_SOURCE)


def normalize_tag(raw: str) -> str:
    """
    비교 가능한 형태로 태그를 정규화한다.

    괄호 강조와 가중치 표기를 제거하고 소문자·단일 공백으로 맞춘다.

        "(huge:1.3)"    -> "huge"
        "((tag))"       -> "tag"
        "[soft]"        -> "soft"
        " Bad   Hands " -> "bad hands"

    괄호를 먼저 제거하고 가중치 접미사를 나중에 지우는 **순서가 중요하다**.
    `(breasts:1.3)` -> `breasts:1.3` -> `breasts`. 순서가 반대면 괄호가 남는다.
    """
    tag = raw.strip().lower().translate(_BRACKET_TABLE)
    tag = _WEIGHT_SUFFIX.sub("", tag)
    return " ".join(tag.split())


def split_tags(text: str) -> tuple[str, ...]:
    """쉼표로 분리해 정규화한 태그 목록. 빈 토큰은 버린다."""
    return tuple(tag for tag in map(normalize_tag, text.split(",")) if tag)


def join_tags(*parts: str) -> str:
    """빈 조각을 건너뛰고 쉼표로 이어붙인다. 원문 표기는 보존한다."""
    return ", ".join(part.strip() for part in parts if part and part.strip())


def find_duplicate_tags(positive: str, negative: str) -> tuple[str, ...]:
    """
    포지티브와 네거티브에 동시에 존재하는 태그를 찾는다.

    **같은 문자열만 잡는다.** '1girl' 과 '1boy' 처럼 의미가 상충하지만
    문자열이 다른 경우는 검출되지 않는다. 그런 경우는
    `find_exclusive_conflicts` 가 담당한다.
    """
    return tuple(sorted(set(split_tags(positive)) & set(split_tags(negative))))


def find_exclusive_conflicts(
    text: str, groups: Sequence[frozenset[str]] | None = None
) -> tuple[tuple[str, ...], ...]:
    """
    한 프롬프트 안에 상호배타 태그가 함께 있는지 검사한다.

    Args:
        text: 검사할 프롬프트 문자열.
        groups: 상호배타 그룹 목록. None 이면 config 기본값.

    Returns:
        위반한 그룹별 태그 튜플. 위반이 없으면 빈 튜플.

    **이 검사는 완전할 수 없다.** 상호배타 조합을 전부 열거하는 것은
    불가능하므로 기본 목록은 최소한만 두고, JSON 의
    `_rules.mutually_exclusive` 로 운영 중 겪은 조합을 추가하는 방식을 쓴다.

    경고에 그치고 중단하지 않는다. 불완전한 규칙으로 배치를 막으면
    오탐 하나가 작업 전체를 세운다.
    """
    active = config.MUTUALLY_EXCLUSIVE_DEFAULT if groups is None else tuple(groups)
    present = set(split_tags(text))
    conflicts: list[tuple[str, ...]] = []
    for group in active:
        hit = present & group
        if len(hit) > 1:
            conflicts.append(tuple(sorted(hit)))
    return tuple(conflicts)


def partition_gender_tags(
    tags: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """
    성별·인원 태그를 분리한다.

    Returns:
        (유지할 태그, 제거된 태그). 원문 표기를 보존한다.

    성별은 프로필 축에서 결정되므로 --char_prompt 에 들어가면 프로필과
    충돌한다. 경고만 하지 않고 필터링된 버전을 함께 제시하는 이유는,
    사용자가 출력을 그대로 복사해 쓸 가능성이 높기 때문이다.
    """
    kept: list[str] = []
    removed: list[str] = []
    for tag in tags:
        target = removed if normalize_tag(tag) in config.GENDER_TAGS else kept
        target.append(tag)
    return tuple(kept), tuple(removed)


def parse_exclusive_groups(raw: object) -> tuple[frozenset[str], ...]:
    """
    JSON `_rules.mutually_exclusive` 를 상호배타 그룹으로 변환한다.

    기대 형식은 문자열 리스트의 리스트다.

        "_rules": {
          "mutually_exclusive": [["1girl", "1boy"], ["solo", "2girls"]]
        }

    항목이 2개 미만인 그룹은 검사 의미가 없으므로 버린다.
    형식이 어긋나면 조용히 무시한다. 규칙 정의 실수로 배치가 막히면 안 된다.
    """
    if not isinstance(raw, (list, tuple)):
        return ()

    groups: list[frozenset[str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)):
            continue
        members = {
            normalize_tag(str(member))
            for member in item
            if isinstance(member, str) and member.strip()
        }
        if len(members) > 1:
            groups.append(frozenset(members))
    return tuple(groups)

"""
코드 선택 및 파일명 포맷 — 순수 계층.

`CodeFormatter` 가 이 리팩터링의 핵심 개선 중 하나다.

**개선 전**: `width: int` 를 생성 루프, 스킵 판정, 마크다운 조립, 섹션
가이드, 더미 이미지 생성 등 6개 지점에 인자로 전달했다. 한 곳에서
잘못된 값을 넘기면 파일명 규칙이 갈라져 스킵 판정과 실제 파일명이
어긋난다.

**개선 후**: width 와 파일명 규칙을 한 값 객체에 응집시킨다. 인자
전달이 사라지고, 규칙 변경 시 고칠 곳이 한 클래스로 줄어든다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from . import config
from .errors import ValidationError

_CODE_EXPR: Final = re.compile(config.CODE_EXPR_PATTERN_SOURCE)


@dataclass(frozen=True, slots=True)
class CodeFormatter:
    """
    코드 제로 패딩과 에셋 파일명 규칙을 캡슐화한 값 객체.

    파일명 조립의 단일 진입점이다. 루프·스킵 판정·마크다운이 모두 이
    객체를 경유하므로 규칙이 갈라질 수 없다.
    """

    width: int

    @classmethod
    def for_codes(cls, codes: Sequence[int]) -> CodeFormatter:
        """
        코드 집합에 맞는 패딩 폭을 산출한다.

        최소 2자리를 보장하는 이유는 기존에 생성된
        `{prefix}_00.webp` ~ `{prefix}_19.webp` 와의 하위 호환이다.
        코드가 100 이상이면 그 자릿수로 확장된다.

        폭은 **전체 DB 기준으로 한 번** 계산해 배치 전체에 적용한다.
        선택된 코드만 기준으로 하면 `--mode 0,1` 실행과 `--mode all`
        실행이 다른 파일명을 만들어 스킵 판정이 깨진다.
        """
        if not codes:
            return cls(config.CODE_MIN_WIDTH)
        largest = max(codes)
        digits = len(str(largest)) if largest >= 0 else config.CODE_MIN_WIDTH
        return cls(max(config.CODE_MIN_WIDTH, digits))

    def tag(self, code: int) -> str:
        """제로 패딩된 코드 문자열. 트리거 태그와 로그에 함께 쓰인다."""
        return f"{code:0{self.width}d}"

    def filename(self, prefix: str, code: int) -> str:
        """
        에셋 파일명.

        prefix 는 호출 전에 `validators.validate_prefix` 로 검증되어
        영문·숫자·밑줄·하이픈만 포함한다. 따라서 별도 파일명 정제가
        필요하지 않으며, 그 불변식을 여기서 단언한다.
        """
        assert prefix, "prefix 는 비어 있을 수 없다 (validate_prefix 선행 필요)"
        return f"{prefix}_{self.tag(code)}{config.ASSET_SUFFIX}"

    def trigger(self, prefix: str, code: int) -> str:
        """프롬프트 말미에 붙는 트리거 태그."""
        return f"{prefix}_{self.tag(code)}"


def looks_like_code_expression(value: str) -> bool:
    """
    값이 섹션명이 아니라 코드 표현식인지 판별한다.

    섹션명에는 알파벳이 있고 코드 표현식에는 숫자·콤마·하이픈·공백만
    있으므로 문자 구성으로 구분된다. 충돌하지 않는다.

    `bool(value)` 선행 검사가 필요한 이유: 정규식 `+` 는 빈 문자열에
    매칭되지 않지만 공백만 있는 `"  "` 는 매칭된다.
    """
    return bool(value) and _CODE_EXPR.match(value) is not None


def _expand_token(token: str) -> Iterable[int]:
    """단일 토큰('7' 또는 '10-14')을 코드로 확장한다."""
    if "-" in token:
        start_text, _, end_text = token.partition("-")
        start, end = int(start_text.strip()), int(end_text.strip())
        if start > end:
            start, end = end, start  # 역순 입력 허용
        if start < config.CODE_MIN:
            raise ValueError(f"음수 코드는 허용되지 않습니다: {token}")
        if end > config.CODE_MAX:
            raise ValueError(f"코드 상한({config.CODE_MAX})을 초과했습니다: {token}")
        return range(start, end + 1)

    code = int(token)
    if not config.CODE_MIN <= code <= config.CODE_MAX:
        raise ValueError(
            f"코드는 {config.CODE_MIN}~{config.CODE_MAX} 범위여야 합니다: {token}"
        )
    return (code,)


def parse_code_expression(expression: str) -> tuple[int, ...]:
    """
    코드 표현식을 정수 튜플로 변환한다.

    지원 형식:
        "20-29"         범위
        "0,3,7"         열거
        "0-5,10,20-22"  혼합
        "29-20"         역순 (교정됨)
        "0-5,3"         중복 (정규화됨)

    상한을 두는 이유: `0-999999999` 같은 입력이 set 에 10억 개 정수를
    넣으려 한다. DB 필터링은 그 뒤에 일어나므로 아무 소용이 없다.

    Raises:
        ValueError: 정수로 해석할 수 없거나 범위를 벗어날 때.
    """
    codes: set[int] = set()
    for raw_token in expression.split(","):
        token = raw_token.strip()
        if token:
            codes.update(_expand_token(token))
    return tuple(sorted(codes))


@dataclass(frozen=True, slots=True)
class CodeSelection:
    """대상 코드 결정 결과. 로그에 근거를 남기기 위해 출처를 함께 담는다."""

    codes: tuple[int, ...]
    source: str

    def __len__(self) -> int:
        return len(self.codes)

    def __bool__(self) -> bool:
        return bool(self.codes)


def resolve_codes(
    *,
    available: dict[int, object],
    section_map: dict[str, tuple[int, ...]],
    mode: str,
    explicit_expression: str | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> CodeSelection:
    """
    `--mode` / `--codes` 를 해석해 대상 코드를 결정한다.

    Args:
        available: 코드 → 엔트리 매핑. 존재 확인용.
        section_map: 섹션명 → 코드 튜플.
        mode: `all` | 섹션명 | 코드 표현식.
        explicit_expression: `--codes` 값. mode 보다 우선한다.
        on_warning: 경고 콜백. None 이면 무시한다.

    Returns:
        CodeSelection.

    Raises:
        ValidationError: 표현식 구문 오류 또는 미등록 섹션명.

    `--mode` 가 코드 표현식을 받아들이지만 기존 값(`all`, 섹션명)은
    알파벳을 포함하므로 판별 정규식에 매칭되지 않는다. 따라서 기존
    명령의 해석이 바뀌지 않는다.
    """
    warn = on_warning if on_warning is not None else (lambda _message: None)

    expression: str | None = None
    source = ""

    if explicit_expression:
        expression = explicit_expression
        source = "--codes"
        if mode and mode != "all" and looks_like_code_expression(mode):
            warn(f"--codes 가 우선합니다. --mode '{mode}' 무시됨")
    elif mode and looks_like_code_expression(mode):
        expression = mode
        source = "--mode(코드)"

    if expression is not None:
        try:
            requested = parse_code_expression(expression)
        except ValueError as exc:
            raise ValidationError(
                f"코드 표현식을 해석할 수 없습니다: '{expression}' ({exc})",
                "예: 20-29 / 0,3,7 / 0-5,10,20-22",
            ) from None

        if missing := [code for code in requested if code not in available]:
            warn(f"DB에 없는 코드 무시: {missing}")
        return CodeSelection(
            tuple(code for code in requested if code in available), source
        )

    if mode == "all":
        return CodeSelection(tuple(sorted(available)), "all")

    if mode in section_map:
        return CodeSelection(section_map[mode], f"section:{mode}")

    raise ValidationError(
        f"알 수 없는 모드 '{mode}'. 사용 가능: {['all'] + sorted(section_map)}",
        "코드 리스트 직접 지정도 가능합니다. 예: --mode 0,5,12 / --mode 10-14",
    )

"""
프롬프트 조립 — 순수 계층.

조립 순서를 한 곳에 고정한다. 여기저기서 f-string 으로 이어붙이면
순서가 갈라지고, 어떤 태그가 앞에 오는지가 결과에 영향을 주므로
재현성이 깨진다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .codes import CodeFormatter
from .models import PoseEntry, Profile
from .tags import join_tags


@dataclass(frozen=True, slots=True)
class PromptPair:
    """한 장을 생성하기 위한 포지티브/네거티브 쌍."""

    positive: str
    negative: str


@dataclass(frozen=True, slots=True)
class PromptComposer:
    """
    프로필과 캐릭터 태그를 고정한 뒤 코드별 프롬프트를 만든다.

    프로필과 char_prompt 는 배치 내에서 불변이므로 생성자에 고정하고,
    코드마다 바뀌는 부분만 메서드 인자로 받는다. 호출부가 매번 같은
    인자를 반복 전달하지 않아도 된다.

    조립 순서:
        {profile.base_positive}, {char_prompt}, {entry.prompt}, {trigger}

    네거티브:
        {profile.base_negative}, {custom_negative}

    custom_negative 는 프로필 네거티브를 **대체하지 않고 추가**된다.
    프로필은 축을 결정하고 custom_negative 는 캐릭터별 예외를 처리하는
    역할 분담이다.
    """

    profile: Profile
    char_prompt: str
    custom_negative: str = ""

    @property
    def negative(self) -> str:
        """배치 전체에서 동일한 네거티브 프롬프트."""
        return join_tags(self.profile.base_negative, self.custom_negative)

    @property
    def positive_preview(self) -> str:
        """
        코드별 포즈를 제외한 포지티브 앞부분.

        충돌 검사와 로그 표시에 쓴다. 포즈 프롬프트는 코드마다 달라
        배치 시작 시점에 확정할 수 없다.
        """
        return join_tags(self.profile.base_positive, self.char_prompt)

    def compose(
        self, entry: PoseEntry, prefix: str, formatter: CodeFormatter
    ) -> PromptPair:
        """단일 코드에 대한 프롬프트 쌍을 만든다."""
        positive = join_tags(
            self.profile.base_positive,
            self.char_prompt,
            entry.prompt,
            formatter.trigger(prefix, entry.code),
        )
        return PromptPair(positive=positive, negative=self.negative)

"""
대화형 마법사 — 선택 결과를 argv 로 조립한다.

**Namespace 를 직접 만들지 않고 argv 를 만드는 이유**

Namespace 를 직접 조립하면 기본값 채우기, 타입 변환, 상호 배타 검사를
여기서 다시 구현해야 한다. 규칙이 두 곳에 존재하면 반드시 어긋난다.

argv 를 만들어 기존 파서에 넣으면 검증 경로가 수동 CLI 와 완전히 동일해진다.
부수 효과로 조립된 명령을 화면에 보여줄 수 있어 사용자가 CLI 사용법을
자연히 익힌다. 같은 작업을 다시 할 때 명령을 복사해 쓰면 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .database import peek_choices
from .errors import UserAbort
from .storage import find_reference_candidates
from .tui import Choice, ask_text, banner, confirm, select
from .validators import validate_prefix, validate_ref_weight

# 실행 모드 선택지. 값은 CLI 플래그 이름과 일치시킨다.
_MODE_CHOICES = (
    Choice("real", "실제 생성", "WebUI 필요. 결과물을 만듭니다"),
    Choice("mock", "모의 생성 (--mock)", "WebUI 불필요. 더미 이미지로 점검"),
    Choice("dry-run", "계획 확인 (--dry-run)", "파일을 만들지 않고 대상만 확인"),
    Choice("benchmark", "가중치 벤치마크 (--benchmark)", "참조 이미지 필수"),
    Choice("test", "자체 진단 (--test)", "JSON·로직 검사"),
)


@dataclass(slots=True)
class WizardResult:
    """마법사 산출물."""

    argv: list[str] = field(default_factory=list)

    @property
    def command_line(self) -> str:
        """화면에 보여줄 명령 문자열. 공백이 있는 인자는 인용한다."""
        parts: list[str] = []
        for token in self.argv:
            parts.append(f'"{token}"' if " " in token or not token else token)
        return " ".join(parts)


def _scope_choices(sections: tuple[str, ...]) -> tuple[Choice, ...]:
    items = [Choice("all", "전체", "JSON 의 모든 코드")]
    items.extend(
        Choice(name, f"섹션: {name}", "해당 섹션만") for name in sections
    )
    items.append(Choice("__custom__", "직접 입력", "0,5,12 또는 10-14"))
    return tuple(items)


def _profile_choices(profiles: tuple[str, ...]) -> tuple[Choice, ...]:
    if not profiles:
        return (Choice("__default__", "내장 기본값", "_profiles 섹션이 없습니다"),)
    hints = {
        "female": "여성 캐릭터",
        "male": "남성 캐릭터",
        "male_otokonoko": "중성적 외형의 남성",
    }
    items = [Choice(name, name, hints.get(name, "")) for name in profiles]
    return tuple(items)


def _default_profile_index(profiles: tuple[str, ...]) -> int:
    try:
        return profiles.index(config.DEFAULT_PROFILE_NAME)
    except ValueError:
        return 0


def run_wizard(base_dir: Path, prog: str) -> WizardResult:
    """
    대화형으로 인자를 수집해 argv 를 만든다.

    Raises:
        UserAbort: 사용자가 중간에 취소했을 때.
    """
    sections, profiles = peek_choices(base_dir)

    banner("에셋 생성 파이프라인 — 대화형 모드")
    print("  q 를 누르거나 Ctrl+C 로 언제든 취소할 수 있습니다.")

    mode = select("실행 모드", _MODE_CHOICES)

    # 진단은 다른 인자를 전혀 요구하지 않는다.
    if mode == "test":
        return WizardResult(["--test"])

    argv: list[str] = []

    prefix = ask_text(
        "약칭 (영문·숫자·_·- 1~64자)", validate=validate_prefix
    )
    argv += ["--prefix", prefix]

    char_prompt = ask_text("외형 태그 (예: silver hair, blue eyes)")
    argv += ["--char_prompt", char_prompt]

    if profiles:
        profile = select(
            "프로필",
            _profile_choices(profiles),
            default=_default_profile_index(profiles),
        )
        if profile != "__default__":
            argv += ["--profile", profile]

    scope = select("생성 범위", _scope_choices(sections))
    if scope == "__custom__":
        codes = ask_text("코드 지정 (0,5,12 또는 10-14)")
        argv += ["--codes", codes]
    else:
        argv += ["--mode", scope]

    # 기본값 "-" 는 "입력 없음"을 뜻한다. 빈 문자열을 기본값으로 두면
    # ask_text 가 재입력을 요구하므로 자리표시자가 필요하다.
    custom_neg = ask_text("추가 네거티브 (없으면 Enter)", default="-")
    if custom_neg and custom_neg != "-":
        argv += ["--custom_neg", custom_neg]

    argv += _reference_args(base_dir, prefix, mode)

    if mode == "mock":
        argv.append("--mock")
    elif mode == "dry-run":
        argv.append("--dry-run")
    elif mode == "benchmark":
        argv.append("--benchmark")
        weights = ask_text(
            "비교할 가중치",
            default=",".join(f"{w:g}" for w in config.BENCHMARK_WEIGHTS_DEFAULT),
        )
        argv += ["--bench_weights", weights]

    if mode in ("real", "benchmark") and not confirm(
        "완료 후 폴더를 자동으로 열까요?", default=True
    ):
        argv.append("--no-open")

    result = WizardResult(argv)

    banner("실행할 명령")
    print(f"  {prog} {result.command_line}")
    if not confirm("\n이대로 실행할까요?", default=True):
        raise UserAbort("실행이 취소되었습니다.")

    return result


def _reference_args(base_dir: Path, prefix: str, mode: str) -> list[str]:
    """
    참조 이미지 관련 인자를 결정한다.

    벤치마크는 참조 이미지가 없으면 성립하지 않으므로 경로를 반드시 받는다.
    그 외 모드는 자동 탐색 결과를 알려주고 사용할지 묻는다.
    """
    found = find_reference_candidates(base_dir, prefix)

    if mode == "benchmark":
        if found:
            print(f"\n  참조 이미지 발견: {found[0].name}")
            if confirm("이 이미지를 사용할까요?", default=True):
                return []
        path = ask_text("참조 이미지 경로 (벤치마크에 필수)")
        return ["--ref_image", path]

    if not found:
        print(
            f"\n  참조 이미지 없음 ({config.REFERENCES_DIRNAME}/{prefix}.*)"
            " — 텍스트 프롬프트만 사용합니다."
        )
        return []

    print(f"\n  참조 이미지 발견: {found[0].name}")
    if not confirm("참조 이미지를 사용할까요?", default=True):
        return ["--no_ref"]

    weight = ask_text(
        "적용 강도",
        default=f"{config.REF_WEIGHT_DEFAULT:g}",
        validate=lambda value: str(validate_ref_weight(float(value))),
    )
    return ["--ref_weight", weight]

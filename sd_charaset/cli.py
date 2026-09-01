"""
CLI 인자 파싱 및 진입점.

**2단계 파싱을 쓰는 이유**

argparse 의 `choices` 는 파서 생성 시점에 확정되지만, 섹션명과 프로필명은
`pose_database.json` 을 읽어야 알 수 있다. 순서 의존성이 있으므로
`choices` 를 쓰지 않고 자유 문자열로 받은 뒤 도메인 계층에서 검증한다.

다만 `--help` 문구는 동적으로 채울 수 있다. 1차 파서는 `add_help=False`
로 두고 `--test` / `--from_image` 만 판별한 뒤, JSON 을 읽어 실제 섹션명과
프로필명을 넣은 2차 파서를 만든다. 그래서 `--help` 출력 시점에는 이미
실제 목록이 보인다.

**종료 코드는 여기서만 결정한다.** 도메인 함수는 `CharasetError` 를 올리고
그 클래스가 `exit_code` 를 들고 있다. 종료 정책이 여러 파일에 흩어지지
않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import config
from .commands import (
    BenchmarkCommand,
    Command,
    DiagnoseCommand,
    GenerateCommand,
    InterrogateCommand,
)
from .database import peek_choices
from .errors import CharasetError, UserAbort
from .logging_setup import configure_logging, configure_stdio

PROGRAM_NAME = "sd_charaset"
DESCRIPTION = "캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI)"


def _mode_help(section_names: Sequence[str]) -> str:
    if section_names:
        return (
            f"all | 섹션명({', '.join(section_names)}) | "
            "코드 리스트(0,5,12 / 10-14)"
        )
    return "all | JSON 섹션명 | 코드 리스트 (0,5,12 / 10-14)"


def _profile_help(profile_names: Sequence[str]) -> str:
    if profile_names:
        return (
            f"캐릭터 프로필: {', '.join(profile_names)} "
            f"(생략 시 '{config.DEFAULT_PROFILE_NAME}')"
        )
    return (
        f"캐릭터 프로필 ({config.PROFILES_KEY} 섹션에서 선택, "
        f"생략 시 '{config.DEFAULT_PROFILE_NAME}')"
    )


def build_parser(
    section_names: Sequence[str] = (),
    profile_names: Sequence[str] = (),
    add_help: bool = True,
    prog: str = PROGRAM_NAME,
) -> argparse.ArgumentParser:
    """인자 파서를 만든다."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=DESCRIPTION,
        add_help=add_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            f"  {prog} --prefix mika --char_prompt \"silver hair, blue eyes\"\n"
            f"  {prog} --prefix ryu --char_prompt \"short black hair\" --profile male\n"
            f"  {prog} --prefix mika --char_prompt \"...\" --mode 0,5,12\n"
            f"  {prog} --prefix test --char_prompt none --mock\n"
            f"  {prog} --prefix mika --char_prompt \"...\" --benchmark\n"
            f"  {prog} --from_image references/mika.png\n"
            f"  {prog} --interactive\n"
            f"  {prog} --test\n"
        ),
    )

    core = parser.add_argument_group("캐릭터")
    core.add_argument("--prefix", help="에셋 식별자 (영문·숫자·_·- 1~64자)")
    core.add_argument("--char_prompt", help="캐릭터 외형 태그")
    core.add_argument(
        "--custom_neg", default="", help="프로필 네거티브에 추가할 태그 (대체 아님)"
    )
    core.add_argument("--profile", default=None, help=_profile_help(profile_names))

    scope = parser.add_argument_group("생성 범위")
    scope.add_argument("--mode", default="all", help=_mode_help(section_names))
    scope.add_argument(
        "--codes", default=None, help="코드 직접 지정 (20-29 / 0,3,7). --mode 보다 우선"
    )

    reference = parser.add_argument_group("참조 이미지 (IP-Adapter)")
    reference.add_argument(
        "--ref_image",
        default=None,
        help=(
            f"참조 이미지 경로 직접 지정 "
            f"(생략 시 {config.REFERENCES_DIRNAME}/{{prefix}}.png 자동 탐색)"
        ),
    )
    reference.add_argument(
        "--ref_weight",
        type=float,
        default=config.REF_WEIGHT_DEFAULT,
        help=(
            f"적용 강도 {config.REF_WEIGHT_MIN}~{config.REF_WEIGHT_MAX} "
            f"(기본 {config.REF_WEIGHT_DEFAULT})"
        ),
    )
    reference.add_argument(
        "--no_ref",
        action="store_true",
        help="참조 이미지를 무시하고 텍스트 프롬프트만 사용",
    )
    reference.add_argument(
        "--cn_module", default=None, help="ControlNet 전처리기 수동 지정"
    )
    reference.add_argument(
        "--cn_model", default=None, help="ControlNet 모델 수동 지정"
    )

    bench = parser.add_argument_group("가중치 벤치마크")
    bench.add_argument(
        "--benchmark",
        action="store_true",
        help="가중치를 순회 비교하고 HTML 뷰어를 생성 (참조 이미지 필수)",
    )
    bench.add_argument(
        "--bench_weights",
        default=None,
        help=(
            "비교할 가중치 목록 (기본 "
            f"{','.join(f'{w:g}' for w in config.BENCHMARK_WEIGHTS_DEFAULT)})"
        ),
    )

    modes = parser.add_argument_group("실행 모드")
    modes.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="파일 쓰기 없이 대상·파일명·마크다운만 출력",
    )
    modes.add_argument(
        "--mock",
        action="store_true",
        help=f"WebUI 없이 더미 이미지를 {config.MOCK_ASSETS_DIRNAME}/ 에 생성",
    )
    modes.add_argument(
        "--test", action="store_true", help="데이터·로직 자체 진단 후 종료"
    )
    modes.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="방향키로 선택하는 대화형 모드 (긴 명령 타이핑 불필요)",
    )
    modes.add_argument(
        "--from_image",
        default=None,
        help="이미지에서 태그를 추출해 출력하고 종료 (생성하지 않음)",
    )
    modes.add_argument(
        "--interrogator",
        default=config.INTERROGATOR_DEFAULT,
        choices=config.INTERROGATORS,
        help=f"태그 추출 모델 (기본 {config.INTERROGATOR_DEFAULT})",
    )

    misc = parser.add_argument_group("기타")
    misc.add_argument(
        "--no-open",
        dest="no_open",
        action="store_true",
        help="완료 후 파일 관리자를 열지 않음 (반복 실행·자동화용)",
    )
    misc.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG 레벨 로그 출력"
    )

    return parser


def _select_command(
    args: argparse.Namespace, base_dir: Path, prog: str
) -> Command:
    """
    실행할 Command 를 고른다.

    우선순위: --test > --from_image > --benchmark > 생성(기본/mock/dry-run)

    진단과 태그 추출은 생성과 무관한 독립 작업이므로 먼저 분기해 즉시
    종료한다. 벤치마크는 생성 작업이지만 여러 배치를 묶고 산출물이
    비교 리포트라 별도 Command 다.
    """
    if args.test:
        return DiagnoseCommand(base_dir)
    if args.from_image:
        return InterrogateCommand(
            base_dir=base_dir,
            image_path=args.from_image,
            model=args.interrogator,
            program=prog,
        )
    if args.benchmark:
        return BenchmarkCommand(base_dir=base_dir, args=args)
    return GenerateCommand(base_dir=base_dir, args=args)


def resolve_base_dir(explicit: Path | None = None) -> Path:
    """
    작업 루트를 결정한다.

    패키지 위치의 부모를 쓴다. `sd_charaset/` 안에 코드가 있고 그 옆에
    `pose_database.json` 과 `references/` 가 놓이는 구조다.
    """
    if explicit is not None:
        return explicit.resolve()
    return Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None, prog: str = PROGRAM_NAME) -> int:
    """
    CLI 진입점.

    Args:
        argv: 인자 목록. None 이면 sys.argv 를 쓴다.
        prog: 도움말과 예시에 표시할 프로그램 이름.

    Returns:
        종료 코드.
    """
    configure_stdio()
    base_dir = resolve_base_dir()

    # 1차 파싱: help 를 끄고 실행 모드만 판별한다.
    # --help 는 여기서 소비되지 않고 2차 파서로 넘어가므로, 도움말 출력
    # 시점에는 이미 JSON 섹션명과 프로필명이 로드되어 있다.
    pre_parser = build_parser(add_help=False, prog=prog)
    pre_args, _unknown = pre_parser.parse_known_args(argv)

    logger = configure_logging(verbose=bool(pre_args.verbose))

    sections, profiles = peek_choices(base_dir)
    parser = build_parser(sections, profiles, add_help=True, prog=prog)

    # 대화형 모드는 선택 결과를 argv 로 조립해 **같은 파서에 다시 넣는다.**
    # Namespace 를 직접 만들면 기본값·타입 변환·상호 배타 검사를 마법사
    # 쪽에서 다시 구현해야 하고, 규칙이 두 곳에 있으면 반드시 어긋난다.
    if pre_args.interactive:
        from .wizard import run_wizard

        try:
            wizard = run_wizard(base_dir, prog)
        except CharasetError as exc:
            for line in exc.render():
                logger.error("%s", line.removeprefix("[ERROR] "))
            return exc.exit_code
        except KeyboardInterrupt:
            abort = UserAbort("사용자에 의해 취소되었습니다.")
            logger.error("%s", abort.message)
            return abort.exit_code
        argv = wizard.argv

    args = parser.parse_args(argv)

    # 생성 계열이 아닐 때는 필수 인자를 강제하지 않는다.
    # argparse 의 required=True 는 무조건 강제하므로 쓸 수 없다.
    if not args.test and not args.from_image:
        missing = [
            name for name in ("prefix", "char_prompt") if not getattr(args, name)
        ]
        if missing:
            parser.error(
                "다음 인자가 필요합니다: "
                + ", ".join(f"--{name}" for name in missing)
            )

    command = _select_command(args, base_dir, prog)

    try:
        return command.run()
    except CharasetError as exc:
        for line in exc.render():
            logger.error("%s", line.removeprefix("[ERROR] "))
        return exc.exit_code
    except KeyboardInterrupt:
        abort = UserAbort("사용자에 의해 취소되었습니다.")
        logger.error("%s", abort.message)
        return abort.exit_code

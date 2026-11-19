"""
sd_batch_generator.py
캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI API 연동) - CLI 진입점.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from generator.checkpoint import load_checkpoint_profiles, match_checkpoint_profile
from generator.config import (
    CHARACTERS_DIRNAME,
    DEFAULT_PROFILE,
    REF_WEIGHT_DEFAULT,
    REF_WEIGHT_MAX,
    REF_WEIGHT_MIN,
    REFERENCES_DIRNAME,
    ConfigError,
    configure_stdio,
)
from generator.diagnostics.self_test import run_self_test
from generator.models import GenerationOptions
from generator.pose_db import load_pose_db, peek_choices
from generator.precheck import run_precheck
from generator.reference import INTERROGATE_DEFAULT, INTERROGATORS
from generator.roster import (
    RosterPathManager,
    apply_character_to_args,
    list_characters,
    load_character,
)
from generator.runner import execute, run_all_chars, run_interrogate

# ─────────────────────────────────────────────
# CLI 파서 구성
# ─────────────────────────────────────────────
def build_parser(
    section_names: Sequence[str] | None = None,
    profile_names: Sequence[str] | None = None,
    add_help: bool = True,
) -> argparse.ArgumentParser:
    if section_names:
        mode_help = (
            f"all | 섹션명({', '.join(section_names)}) | 코드 리스트(0,5,12 / 10-14)"
        )
    else:
        mode_help = "all | JSON 섹션명 | 코드 리스트 (0,5,12 / 10-14)"

    if profile_names:
        profile_help = (
            f"캐릭터 프로필: {', '.join(profile_names)} "
            f"(생략 시 '{DEFAULT_PROFILE}')"
        )
    else:
        profile_help = f"캐릭터 프로필 (_profiles 섹션에서 선택, 생략 시 '{DEFAULT_PROFILE}')"

    parser = argparse.ArgumentParser(
        prog="sd_batch_generator.py",
        description="캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI)",
        add_help=add_help,
    )
    parser.add_argument(
        "--char", default=None,
        metavar="NAME",
        help=f"{CHARACTERS_DIRNAME}/NAME.json 을 읽어 프리셋 적용. 커맨드라인 인자가 있으면 그쪽 우선",
    )
    parser.add_argument(
        "--list", dest="list_chars", action="store_true",
        help=f"{CHARACTERS_DIRNAME}/ 폴더의 캐릭터 목록 출력 후 종료",
    )
    parser.add_argument(
        "--all-chars", dest="all_chars", action="store_true",
        help=f"{CHARACTERS_DIRNAME}/ 의 모든 캐릭터를 순서대로 생성. 이미 있는 파일은 건너뜀",
    )
    parser.add_argument(
        "--roster", "-r", default=None, metavar="NAME",
        help="로스터(프로젝트) 선택. dar=dark_generals, oto=oto. 생략 시 기본 로스터 사용",
    )
    parser.add_argument("--prefix", help="에셋 식별자 (영문·숫자·_·- 1~64자)")
    parser.add_argument("--char_prompt", help="캐릭터 외형 태그")
    parser.add_argument("--custom_neg", default="", help="추가 네거티브 태그 (선택)")
    parser.add_argument("--profile", default=None, help=profile_help)
    parser.add_argument("--mode", default="all", help=mode_help)
    parser.add_argument("--codes", default=None, help="코드 직접 지정 (20-29 / 0,3,7)")
    parser.add_argument(
        "--bg", default=None,
        help="감정 씬 배경 프롬프트 직접 지정 (기존 clean background 대체)",
    )
    parser.add_argument(
        "--bg-preset", default="default",
        help="projects/{roster}/background.json 에서 사용할 프리셋 키 (기본: default)",
    )
    parser.add_argument(
        "--nude-positive", "--nude_positive", dest="nude_positive", default=None,
        help="탈의/H-씬 전용 프롬프트 직접 지정 (생략 시 캐릭터 JSON의 nude_positive 또는 의상 태그 자동 제거)",
    )
    parser.add_argument(
        "--gender", default=None,
        help="캐릭터 성별 직접 지정 (female, otokonoko 등. 포즈 DB의 prompt_female/prompt_otokonoko 분기용)",
    )

    ref = parser.add_argument_group("참조 이미지 (IP-Adapter)")
    ref.add_argument(
        "--ref_image", default=None,
        help=f"참조 이미지 경로 직접 지정 (생략 시 {REFERENCES_DIRNAME}/{{prefix}}.png 자동 탐색)",
    )
    ref.add_argument(
        "--ref_weight", type=float, default=REF_WEIGHT_DEFAULT,
        help=f"적용 강도 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} (기본 {REF_WEIGHT_DEFAULT})",
    )
    ref.add_argument(
        "--no_ref", action="store_true",
        help="참조 이미지를 무시하고 텍스트 프롬프트만 사용",
    )
    ref.add_argument(
        "--cn_module", default=None,
        help="ControlNet 전처리기 수동 지정 (자동 탐지 실패 시)",
    )
    ref.add_argument(
        "--cn_model", default=None,
        help="ControlNet 모델 수동 지정 (자동 탐지 실패 시)",
    )

    forge = parser.add_argument_group("Forge 내장 기능")
    forge.add_argument(
        "--enable-freeu", action="store_true",
        help="FreeU 활성화 (SDXL 최적화 - 디테일 강화)",
    )
    forge.add_argument(
        "--freeu-b1", type=float, default=1.1,
        help="FreeU backbone1 스케일 (기본 1.1)",
    )
    forge.add_argument(
        "--freeu-b2", type=float, default=1.2,
        help="FreeU backbone2 스케일 (기본 1.2)",
    )
    forge.add_argument(
        "--freeu-s1", type=float, default=0.9,
        help="FreeU skip1 스케일 (기본 0.9)",
    )
    forge.add_argument(
        "--freeu-s2", type=float, default=0.2,
        help="FreeU skip2 스케일 (기본 0.2)",
    )
    forge.add_argument(
        "--enable-adetailer", action="store_true",
        help="ADetailer 활성화 (얼굴/손 자동 보정)",
    )

    lightning = parser.add_argument_group("SDXL Lightning 가속")
    lightning.add_argument(
        "--enable-lightning", action="store_true",
        help="SDXL Lightning 2-step LoRA 가속 모드 활성화 (초고속 생성)",
    )
    lightning.add_argument(
        "--lightning-steps", type=int, default=2,
        help="Lightning 모드 스텝 수 (기본 2, 품질 보강 시 최대 4~6)",
    )
    lightning.add_argument(
        "--lightning-cfg", type=float, default=1.0,
        help="Lightning 모드 CFG Scale (기본 1.0, 최대 1.5 권장)",
    )
    lightning.add_argument(
        "--lightning-lora-name", type=str, default="sdxl_lightning_2step_lora",
        help="Lightning LoRA 파일명 (기본 sdxl_lightning_2step_lora)",
    )

    gen_opts = parser.add_argument_group("생성 파라미터 세부 제어")
    gen_opts.add_argument(
        "--steps", type=int, default=None,
        help="생성 스텝 수 직접 지정 (기본 30)",
    )
    gen_opts.add_argument(
        "--sampler", default=None,
        help="샘플러 직접 지정 (예: DPM++ 2M, DPM++ 2M SDE, Euler a)",
    )
    gen_opts.add_argument(
        "--scheduler", default=None,
        help="스케줄러 직접 지정 (예: Karras, Automatic, Exponential, SGM Uniform)",
    )
    gen_opts.add_argument(
        "--cfg-scale", "--cfg_scale", dest="cfg_scale", type=float, default=None,
        help="CFG Scale 직접 지정 (기본 6.0)",
    )
    gen_opts.add_argument(
        "--clip-skip", "--clip_skip", dest="clip_skip", type=int, default=None,
        help="CLIP Skip 레이어 수 직접 지정 (예: 2)",
    )
    gen_opts.add_argument(
        "--clamp-weights", dest="clamp_weights", action="store_true",
        help="과도한 프롬프트 강조 가중치를 1.15 이하로 클램핑 (선택 옵션, 기본 비활성)",
    )

    parser.add_argument(
        "--naming", default="code", choices=["code", "hybrid", "label"],
        help="에셋 파일명 명명 규칙: code(기본값, prefix_00.webp), hybrid(prefix_000_라벨.webp), label(prefix_라벨.webp)",
    )
    parser.add_argument(
        "--overwrite", "-f", action="store_true",
        help="이미 존재하는 에셋 파일을 건너뛰지 않고 강제로 덮어쓰기 생성",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="파일 쓰기 없이 대상·파일명·마크다운만 출력",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="WebUI 없이 더미 이미지를 생성해 전체 파이프라인 검증",
    )
    parser.add_argument(
        "--test", action="store_true", help="데이터·로직 자체 진단 후 종료"
    )
    parser.add_argument(
        "--precheck", action="store_true", help="로스터 및 포즈 DB 사전 점검 후 결과 보고"
    )
    parser.add_argument(
        "--ckpt-profile", "--ckpt_profile", dest="ckpt_profile", default=None,
        help="체크포인트 최적 파라미터 프로필 적용 (unholy, wai, flatbread, hexmix, pony, animagine 등)",
    )

    qc_group = parser.add_argument_group("검열 및 품질 검증 (QC)")
    qc_group.add_argument(
        "--auto-qc", "--auto-retry-qa", dest="auto_qc", action="store_true",
        help="Auto-QC 품질 검수 및 불량(인원 초과, 손/머리 증식 등) 감지 시 자동 재시도",
    )
    qc_group.add_argument(
        "--max-retries", type=int, default=3,
        help="Auto-QC 불량 발생 시 최대 재시도 횟수 (기본 3)",
    )
    qc_group.add_argument(
        "--censor", action="store_true",
        help="성인/탈의 씬 생성 시 MobileSAM 화이트 실루엣 자동 검열 및 assets/{char}_origin/ 원본 보존",
    )
    qc_group.add_argument(
        "--censor-style", default="white_silhouette",
        choices=["white_silhouette", "black_silhouette", "mosaic", "wide_bar", "slim_bar", "shadow"],
        help="검열 렌더링 스타일 (기본 white_silhouette)",
    )

    interrogate_group = parser.add_argument_group("이미지 태그 역추출 (--from_image)")
    interrogate_group.add_argument(
        "--from_image", default=None, metavar="PATH",
        help="참조 이미지에서 외형 태그를 역추출하고 출력 후 종료 (--prefix/--char_prompt 불필요)",
    )
    interrogate_group.add_argument(
        "--interrogator", default=INTERROGATE_DEFAULT, choices=INTERROGATORS,
        help=f"태그 역추출 모델 선택 (기본: {INTERROGATE_DEFAULT})",
    )
    return parser


# ─────────────────────────────────────────────
# 메인 함수
# ─────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    base_dir = Path(__file__).resolve().parent

    pre_args, _ = build_parser(add_help=False).parse_known_args(argv)
    if pre_args.test:
        return run_self_test(base_dir)

    sections, profiles = peek_choices(base_dir)
    parser = build_parser(sections, profiles)
    args = parser.parse_args(argv)

    if getattr(args, "from_image", None):
        try:
            return run_interrogate(args.from_image, model=args.interrogator)
        except ConfigError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            return 1

    rpm = RosterPathManager(base_dir)
    roster = rpm.resolve_roster(getattr(args, "roster", None))
    ok, errmsg = rpm.validate_roster(roster)
    if not ok:
        print(f"[ERROR] {errmsg}", file=sys.stderr)
        return 1

    if getattr(args, "precheck", False):
        report = run_precheck(base_dir, roster)
        print(f"\n[PRECHECK] 로스터 '{report.roster_name}' 사전 점검 결과:")
        print(f"  - 검사 캐릭터 수: {report.total_characters}명")
        print(f"  - 검사 포즈 수: {report.total_poses}개")
        if report.warnings:
            print("\n  [주의 사항]")
            for w in report.warnings:
                print(f"    - {w}")
        if not report.passed:
            print("\n  [발견된 오류]")
            for err in report.errors:
                print(f"    - {err}")
            return 1
        print("\n[PRECHECK] 모든 안전성 및 무결성 점검 통과 (PASS)")
        return 0

    roster.assets_dir.mkdir(parents=True, exist_ok=True)

    if getattr(args, "ckpt_profile", None):
        ckpt_profiles = load_checkpoint_profiles(base_dir)
        matched_ckpt = match_checkpoint_profile(ckpt_profiles, args.ckpt_profile)
        if matched_ckpt:
            print(f"[CKPT] 프로필 '{matched_ckpt.name}' 매칭 적용 ({matched_ckpt.description})")
            args.active_ckpt = matched_ckpt
        else:
            print(f"[WARN] 체크포인트 프로필 '{args.ckpt_profile}' 을 찾지 못했습니다.", file=sys.stderr)

    if args.roster:
        print(f"[ROSTER] '{roster.roster_name}' -> {roster.characters_dir}")

    if args.list_chars:
        return list_characters(roster.characters_dir)

    if args.all_chars:
        return run_all_chars(
            roster,
            options=GenerationOptions.from_namespace(args),
        )

    if args.char:
        try:
            cfg = load_character(roster.characters_dir, args.char)
        except ConfigError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            return 1
        db_for_args = load_pose_db(base_dir, roster.events_file)
        apply_character_to_args(cfg, args, db_for_args)
        print(f"[CHAR]  '{args.char}' 프리셋 로드 ({roster.characters_dir}/{args.char}.json)")

    char_prompt_needed = not getattr(args, "positive", None)
    if missing := [n for n in ("prefix",) + (("char_prompt",) if char_prompt_needed else ()) if not getattr(args, n)]:
        parser.error("다음 인자가 필요합니다: " + ", ".join(f"--{m}" for m in missing))

    try:
        return execute(GenerationOptions.from_namespace(args), roster)
    except ConfigError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if e.hint:
            print(f"        {e.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[중단] 사용자에 의해 취소되었습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
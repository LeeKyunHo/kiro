"""
sd_batch_generator.py
캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI API 연동) - CLI 진입점.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

# ── 패키지 심볼 Re-export (하위 호환성 100% 보장) ──
from generator.config import (
    API_HOST,
    API_URL,
    ASSETS_DIRNAME,
    CHARACTERS_DIRNAME,
    CN_MODELS_URL,
    CN_MODULES_URL,
    COMMON_NEG,
    DEFAULT_HOST,
    DEFAULT_PROFILE,
    FALLBACK_PROFILE,
    GENDER_TAGS,
    GIB,
    IMAGE_SIZE,
    INTERROGATE_DEFAULT,
    INTERROGATE_TIMEOUT,
    INTERROGATE_URL,
    INTERROGATORS,
    IP_ADAPTER_MODEL_PATTERNS,
    IP_ADAPTER_MODULE_PATTERNS,
    LORA_STRING,
    MAX_CODE,
    MEMORY_TIMEOUT,
    MEMORY_URL,
    MIN_CODE_WIDTH,
    MOCK_SIZE,
    POS_BASE,
    POSE_DB_FILE,
    PROFILES_KEY,
    PROJECTS_DIRNAME,
    REF_WEIGHT_DEFAULT,
    REF_WEIGHT_MAX,
    REF_WEIGHT_MIN,
    REFERENCE_EXTENSIONS,
    REFERENCES_DIRNAME,
    ROSTER_ALIAS,
    SAMPLER_CANDIDATES,
    SAMPLERS_TIMEOUT,
    SAMPLERS_URL,
    SEPARATOR,
    STEPS,
    TXT2IMG_TIMEOUT,
    URL_PLACEHOLDER,
    WEBP_METHOD,
    WEBP_QUALITY,
    ConfigError,
    configure_stdio,
)
from generator.diagnostics.self_test import run_self_test
from generator.models import (
    BatchResult,
    CharacterConfig,
    ControlNetSpec,
    InterrogateResult,
    PoseDatabase,
    PoseEntry,
    Profile,
    ReferenceImage,
    RosterPaths,
    TestReport,
    summarize_durations,
)
from generator.pose_db import (
    load_pose_db,
    looks_like_code_expr,
    parse_codes_expr,
    parse_pose_db,
    peek_choices,
    print_warnings,
    read_pose_json,
    resolve_profile,
    resolve_targets,
)
from generator.prompt import (
    assemble_prompt,
    find_tag_conflicts,
    join_tags,
    normalize_tag,
    resolve_background,
    split_tags,
    validate_prefix,
    validate_ref_weight,
)
from generator.reference import (
    build_controlnet_unit,
    find_reference_candidates,
    load_reference,
    match_model_name,
    resolve_controlnet_spec,
    resolve_reference_image,
)
from generator.reporter import (
    asset_filename,
    build_genit_block,
    build_section_guide,
    code_width,
    format_code,
    mode_badge,
    open_in_explorer,
    print_summary,
)
from generator.roster import (
    RosterPathManager,
    apply_character_to_args,
    list_characters,
    load_character,
)
from generator.runner import execute, run_all_chars, run_batch
from generator.webui_client import (
    build_interrogate_payload,
    build_txt2img_payload,
    extract_vram_peak,
    fetch_vram_peak,
    filter_gender_tags,
    generate_image,
    get_session,
    inject_alwayson_scripts,
    inject_controlnet,
    make_dummy_png,
    resolve_sampler,
    run_interrogate,
    save_as_webp,
)

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

    interrogate = parser.add_argument_group("태그 역추출")
    interrogate.add_argument(
        "--from_image", default=None,
        help="이미지에서 태그를 추출해 출력하고 종료 (생성하지 않음)",
    )
    interrogate.add_argument(
        "--interrogator", default=INTERROGATE_DEFAULT, choices=INTERROGATORS,
        help=f"추출 모델 (기본 {INTERROGATE_DEFAULT})",
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

    rpm = RosterPathManager(base_dir)
    roster = rpm.resolve_roster(getattr(args, "roster", None))
    ok, errmsg = rpm.validate_roster(roster)
    if not ok:
        print(f"[ERROR] {errmsg}", file=sys.stderr)
        return 1

    roster.assets_dir.mkdir(parents=True, exist_ok=True)

    if args.roster:
        print(f"[ROSTER] '{roster.roster_name}' -> {roster.characters_dir}")

    if args.list_chars:
        return list_characters(roster.characters_dir)

    if args.all_chars:
        return run_all_chars(
            roster,
            mode=args.mode,
            codes_expr=args.codes,
            dry_run=args.dry_run,
            mock=args.mock,
            cli_bg=args.bg,
            bg_preset=args.bg_preset,
            enable_freeu=args.enable_freeu,
            freeu_b1=args.freeu_b1,
            freeu_b2=args.freeu_b2,
            freeu_s1=args.freeu_s1,
            freeu_s2=args.freeu_s2,
            enable_adetailer=args.enable_adetailer,
            enable_lightning=args.enable_lightning,
            lightning_steps=args.lightning_steps,
            lightning_cfg=args.lightning_cfg,
            lightning_lora_name=args.lightning_lora_name,
        )

    if args.from_image:
        try:
            return run_interrogate(args.from_image, args.interrogator)
        except ConfigError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            return 1

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
        return execute(args, roster)
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
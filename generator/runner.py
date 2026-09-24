"""
generator.runner
단일 및 전체 캐릭터 배치 생성 실행 엔진 (run_batch, execute, run_all_chars).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

import requests

from generator.config import (
    CHARACTERS_DIRNAME,
    IMAGE_SIZE,
    LORA_STRING,
    REF_WEIGHT_DEFAULT,
    REFERENCES_DIRNAME,
    ConfigError,
)
from generator.models import (
    BatchResult,
    ControlNetSpec,
    PoseDatabase,
    ReferenceImage,
    RosterPaths,
)
from generator.pose_db import load_pose_db, print_warnings, resolve_profile, resolve_targets
from generator.prompt import (
    assemble_prompt,
    find_tag_conflicts,
    join_tags,
    resolve_background,
    validate_prefix,
    validate_ref_weight,
)
from generator.reference import (
    build_controlnet_unit,
    find_reference_candidates,
    resolve_controlnet_spec,
    resolve_reference_image,
)
from generator.reporter import (
    asset_filename,
    build_genit_block,
    code_width,
    format_code,
    mode_badge,
    open_in_explorer,
    print_summary,
)
from generator.roster import apply_character_to_args, load_character
from generator.webui_client import (
    build_txt2img_payload,
    fetch_vram_peak,
    generate_image,
    inject_alwayson_scripts,
    make_dummy_png,
    resolve_sampler,
    save_as_webp,
)


def run_batch(
    *,
    prefix: str,
    base_positive: str,
    char_prompt: str,
    negative_prompt: str,
    targets: Sequence[int],
    db: PoseDatabase,
    save_dir: Path,
    width: int,
    sampler_name: str,
    reference: ReferenceImage | None = None,
    cn_spec: ControlNetSpec | None = None,
    ref_weight: float = REF_WEIGHT_DEFAULT,
    enable_freeu: bool = False,
    freeu_b1: float = 1.1,
    freeu_b2: float = 1.2,
    freeu_s1: float = 0.9,
    freeu_s2: float = 0.2,
    enable_adetailer: bool = False,
    enable_lightning: bool = False,
    lightning_steps: int = 2,
    lightning_cfg: float = 1.0,
    lightning_lora_name: str = "sdxl_lightning_2step_lora",
    active_bg: str | None = None,
    dry_run: bool = False,
    mock: bool = False,
) -> BatchResult:
    result = BatchResult(dry_run=dry_run)

    controlnet_unit = (
        build_controlnet_unit(reference, cn_spec, ref_weight)
        if reference and cn_spec
        else None
    )

    for code in targets:
        entry = db.entries[code]
        tag = format_code(code, width)
        filename = asset_filename(prefix, code, width)
        save_path = save_dir / filename

        if dry_run:
            result.planned.append(code)
            bg_info = f" [BG: {active_bg}]" if (active_bg and entry.section == "emotions") else ""
            print(f"  [{tag}] (계획) {filename}  <- {entry.label}{bg_info}")
            continue

        if save_path.exists():
            print(f"  [{tag}] 이미 존재 (건너뜀) -> {filename}")
            result.skipped.append(code)
            continue

        print(f"  [{tag}] {'모의 생성' if mock else '생성'} 중... ", end="", flush=True)

        started = time.perf_counter()
        try:
            pose_prompt = entry.prompt
            if active_bg and entry.section == "emotions":
                if "clean background" in pose_prompt:
                    pose_prompt = pose_prompt.replace("clean background", active_bg)
                else:
                    pose_prompt = join_tags(pose_prompt, active_bg)

            full_prompt = assemble_prompt(
                base_positive, char_prompt, pose_prompt, f"{prefix}_{tag}"
            )
            
            actual_sampler = sampler_name
            actual_steps = None
            actual_cfg = None
            actual_scheduler = None
            actual_freeu = enable_freeu
            
            if enable_lightning:
                actual_sampler = "DPM++ SDE"
                actual_steps = lightning_steps
                actual_cfg = lightning_cfg
                actual_scheduler = "Karras"
                lora_tag = f"<lora:{lightning_lora_name}:1.0>"
                full_prompt = f"{lora_tag}, {full_prompt}"
                if enable_freeu:
                    actual_freeu = False
            else:
                if LORA_STRING:
                    full_prompt = f"{LORA_STRING}, {full_prompt}"
                    if code == targets[0]:
                        print(f"[LORA] 전역 LoRA 적용: {LORA_STRING}")
            
            actual_width = entry.width if entry.width else IMAGE_SIZE[0]
            actual_height = entry.height if entry.height else IMAGE_SIZE[1]
            
            payload = build_txt2img_payload(
                prompt=full_prompt,
                negative_prompt=negative_prompt,
                sampler_name=actual_sampler,
                width=actual_width,
                height=actual_height,
                steps=actual_steps,
                cfg_scale=actual_cfg,
                scheduler=actual_scheduler,
            )

            controlnet_units: list[dict] = []
            if controlnet_unit is not None:
                controlnet_units.append(controlnet_unit)

            payload = inject_alwayson_scripts(
                payload,
                controlnet_units,
                enable_freeu=actual_freeu,
                freeu_b1=freeu_b1,
                freeu_b2=freeu_b2,
                freeu_s1=freeu_s1,
                freeu_s2=freeu_s2,
                enable_adetailer=enable_adetailer,
            )

            if mock:
                png_bytes = make_dummy_png(prefix, code, width, entry, reference)
            else:
                try:
                    png_bytes = generate_image(payload)
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code in (422, 500):
                        print(f"\n[WARN] 확장 기능 충돌 감지 (HTTP {e.response.status_code}). 순정 모드로 재시도...")
                        fallback_payload = build_txt2img_payload(
                            prompt=full_prompt,
                            negative_prompt=negative_prompt,
                            sampler_name=actual_sampler,
                            width=actual_width,
                            height=actual_height,
                            steps=actual_steps,
                            cfg_scale=actual_cfg,
                            scheduler=actual_scheduler,
                        )
                        fallback_payload = inject_alwayson_scripts(
                            fallback_payload, controlnet_units,
                            False, 1.1, 1.2, 0.9, 0.2, False
                        )
                        png_bytes = generate_image(fallback_payload)
                        print(f"  [{tag}] 재시도 성공 (순정 모드)", end=" ")
                    else:
                        raise
            save_as_webp(png_bytes, save_path)
        except requests.exceptions.ConnectionError:
            print("실패: WebUI 연결 불가 (--api 옵션 실행 여부 확인)")
            result.failed.append((code, "connection"))
            result.aborted = True
            break
        except Exception as e:
            print(f"실패: {e}")
            result.failed.append((code, str(e)))
            continue

        elapsed = time.perf_counter() - started
        result.durations.append((code, elapsed))
        print(f"완료 ({elapsed:.1f}초) -> {filename}")
        result.success.append(code)

    return result


def _setup_prompt(
    args: argparse.Namespace,
    db: PoseDatabase,
    char_prompt: str,
) -> tuple[str, str, str, str]:
    """포지티브/네거티브 프롬프트를 결정하고 Forge 기능 사용을 로깅한다.

    Returns:
        base_positive   : 조립된 포지티브 프롬프트
        negative_prompt : 조립된 네거티브 프롬프트
        effective_char  : run_batch 에 전달할 char_prompt
                          (positive 방식이면 빈 문자열)
        profile_label   : 요약 출력용 레이블 ("전용" 또는 프로필명)
    """
    raw_positive: str | None = getattr(args, "positive", None)
    raw_negative: str | None = getattr(args, "negative", None)

    if raw_positive:
        base_positive = raw_positive
        negative_prompt = raw_negative or ""
        print("[POSITIVE] 캐릭터 전용 프롬프트 사용")
        if raw_negative:
            print("[NEGATIVE] 캐릭터 전용 네거티브 사용")
        else:
            print("[WARN] 'negative' 미지정 - 네거티브 없이 생성합니다")
        profile_label = "전용"
        effective_char = ""
    else:
        profile = resolve_profile(db, args.profile)
        if args.profile:
            print(f"[PROFILE] '{profile.name}' 적용")
        else:
            print(f"[PROFILE] 미지정 - 기본값 '{profile.name}' 적용")
        base_positive = join_tags(profile.base_positive, char_prompt)
        negative_prompt = join_tags(profile.base_negative, args.custom_neg)
        profile_label = profile.name
        effective_char = char_prompt

    if conflicts := find_tag_conflicts(base_positive, negative_prompt):
        print(f"[WARN] 태그 충돌: {conflicts} 가 포지티브와 네거티브에 동시 존재")

    if args.enable_freeu:
        print(
            f"[FORGE] FreeU 활성화 (b1={args.freeu_b1}, b2={args.freeu_b2}, "
            f"s1={args.freeu_s1}, s2={args.freeu_s2})"
        )
    if args.enable_adetailer:
        print("[FORGE] ADetailer 활성화 (face_yolov8n.pt)")

    if args.enable_lightning:
        lora_tag = f"<lora:{args.lightning_lora_name}:1.0>"
        print(
            f"[LIGHTNING] Mode Enabled: {args.lightning_steps} Steps | "
            f"CFG {args.lightning_cfg} | Sampler: DPM++ SDE Karras | LoRA: {lora_tag}"
        )
        if args.enable_freeu:
            print("[LIGHTNING] FreeU 자동 비활성화 (Lightning과 충돌 방지)")

    return base_positive, negative_prompt, effective_char, profile_label


def _setup_reference(
    args: argparse.Namespace,
    roster: RosterPaths,
    prefix: str,
    ref_weight: float,
    dry_run: bool,
    mock: bool,
) -> tuple[ReferenceImage | None, ControlNetSpec | None]:
    """참조 이미지를 해석하고 ControlNet 스펙을 결정한다."""
    if dry_run:
        # dry-run 에서는 실제 로드 없이 로그만 출력
        if args.no_ref:
            print("[REF]  --no_ref 지정 - 참조 이미지 사용 안 함")
        elif args.ref_image:
            print(f"[REF]  {args.ref_image} (지정) weight {ref_weight}")
        elif found := find_reference_candidates(roster.references_dir, prefix):
            print(f"[REF]  {found[0].name} 발견 weight {ref_weight}")
        else:
            print(f"[REF]  없음 ({roster.references_dir}/{prefix}.*) - 텍스트만 사용")
        return None, None

    reference = resolve_reference_image(
        roster.references_dir, prefix, args.ref_image, disabled=args.no_ref
    )

    if reference is None:
        if not args.no_ref:
            print(
                f"[WARN] 참조 이미지 없음 ({REFERENCES_DIRNAME}/{prefix}.*) "
                "- 텍스트 프롬프트만 사용"
            )
        return None, None

    # 참조 이미지가 있을 때 ControlNet 스펙 해석
    if mock:
        # mock 모드: cn_module/model 수동 지정이 있으면 사용, 없으면 None (API 호출 없음)
        cn_spec: ControlNetSpec | None = (
            ControlNetSpec(args.cn_module, args.cn_model, "manual")
            if args.cn_module and args.cn_model
            else None
        )
    else:
        cn_spec = resolve_controlnet_spec(args.cn_module, args.cn_model)

    if cn_spec:
        print(f"[REF]  {reference.label} weight {ref_weight}")
        print(f"[CN]   {cn_spec.module} / {cn_spec.model} ({cn_spec.source})")
    else:
        print(f"[REF]  {reference.label} - ControlNet 미해석, 텍스트만 사용")

    return reference, cn_spec


def _setup_sampler(
    args: argparse.Namespace,
    dry_run: bool,
    mock: bool,
) -> str:
    """샘플러 이름을 결정한다 (mock/dry-run/Lightning/일반 순으로 판단)."""
    if mock or dry_run:
        return "(mock)"
    if args.enable_lightning:
        # Lightning 모드는 전용 샘플러를 강제 사용
        print("[SAMPLER] Lightning 모드 → 'DPM++ SDE' 고정")
        return "DPM++ SDE"
    return resolve_sampler()


def execute(args: argparse.Namespace, roster: RosterPaths) -> int:
    """생성 파이프라인 오케스트레이터. ConfigError 는 호출자가 처리한다."""
    dry_run: bool = args.dry_run
    mock: bool = args.mock and not dry_run
    if args.mock and dry_run:
        print("[WARN] --dry-run 이 우선합니다. --mock 무시됨")

    prefix = validate_prefix(args.prefix)
    char_prompt = (args.char_prompt or "").strip()

    base_dir = Path.cwd()
    db = load_pose_db(base_dir, roster.events_file)
    print_warnings(db)

    # ── 프롬프트 결정 ──────────────────────────────
    base_positive, negative_prompt, effective_char, profile_label = _setup_prompt(
        args, db, char_prompt
    )

    # ── 대상 코드 / 저장 경로 ──────────────────────
    targets = resolve_targets(db, args.mode, args.codes)
    if not targets:
        raise ConfigError(
            "생성 대상 코드가 없습니다.", "--mode / --codes 값을 확인하세요."
        )

    width = code_width(db.all_codes)
    save_dir = roster.assets_dir / prefix
    if not dry_run:
        save_dir.mkdir(parents=True, exist_ok=True)

    # ── 참조 이미지 / 샘플러 ───────────────────────
    ref_weight = validate_ref_weight(args.ref_weight)
    reference, cn_spec = _setup_reference(args, roster, prefix, ref_weight, dry_run, mock)
    sampler_name = _setup_sampler(args, dry_run, mock)
    badge = mode_badge(dry_run, mock)

    # ── 배경 ──────────────────────────────────────
    cli_bg: str | None = getattr(args, "bg", None)
    bg_preset: str = getattr(args, "bg_preset", "default")
    active_bg = resolve_background(cli_bg, roster.background_file, bg_preset)

    # ── 작업 요약 출력 ─────────────────────────────
    print(
        f"\n[작업 시작]{badge} 캐릭터: {prefix} | 프로필: {profile_label} | "
        f"모드: {args.mode} ({len(targets)}장) | 폭: {width}"
    )
    print(f"[저장] {save_dir}")
    if active_bg:
        print(f"[BG]   감정(emotions) 씬 배경: '{active_bg}'")
    print(f"[POS]  {base_positive}")
    print(f"[NEG]  {negative_prompt}\n")

    # ── 배치 생성 ─────────────────────────────────
    result = run_batch(
        prefix=prefix,
        base_positive=base_positive,
        char_prompt=effective_char,
        negative_prompt=negative_prompt,
        targets=targets,
        db=db,
        save_dir=save_dir,
        width=width,
        sampler_name=sampler_name,
        reference=reference,
        cn_spec=cn_spec,
        ref_weight=ref_weight,
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
        active_bg=active_bg,
        dry_run=dry_run,
        mock=mock,
    )

    vram = None if (mock or dry_run) else fetch_vram_peak()
    print_summary(result, save_dir, badge, vram)

    existing = result.existing
    if not existing:
        print("\n[INFO] 생성된 파일이 없어 마크다운을 출력하지 않습니다.")
        return 1 if result.failed else 0

    if not dry_run:
        open_in_explorer(save_dir)

    print(build_genit_block(prefix, existing, db, width, badge))
    return 1 if result.aborted else 0


def run_all_chars(
    roster: RosterPaths,
    mode: str,
    codes_expr: str | None,
    dry_run: bool,
    mock: bool,
    cli_bg: str | None = None,
    bg_preset: str = "default",
    enable_freeu: bool = False,
    freeu_b1: float = 1.1,
    freeu_b2: float = 1.2,
    freeu_s1: float = 0.9,
    freeu_s2: float = 0.2,
    enable_adetailer: bool = False,
    enable_lightning: bool = False,
    lightning_steps: int = 2,
    lightning_cfg: float = 1.0,
    lightning_lora_name: str = "sdxl_lightning_2step_lora",
) -> int:
    """로스터의 모든 캐릭터를 순서대로 생성한다."""
    chars_dir = roster.characters_dir
    if not chars_dir.is_dir():
        print(f"[ERROR] {CHARACTERS_DIRNAME}/ 폴더가 없습니다.", file=sys.stderr)
        return 1

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 에 json 파일이 없습니다.")
        return 0

    # 미리 포즈 DB를 로드하여 이벤트 씬 존재 여부를 파악 (default_mode 자동 병합용)
    db = load_pose_db(Path.cwd(), roster.events_file)

    total = len(files)
    succeeded: list[str] = []
    failed: list[str] = []

    print(f"\n[ALL-CHARS] {total}명 순차 생성 시작\n{'=' * 64}")

    for idx, path in enumerate(files, 1):
        name = path.stem
        print(f"\n[ALL-CHARS] ({idx}/{total}) {name}")
        print("-" * 40)

        try:
            cfg = load_character(chars_dir, name)
        except ConfigError as e:
            print(f"[ERROR] {name} 로드 실패: {e}", file=sys.stderr)
            failed.append(name)
            continue

        args = argparse.Namespace(
            prefix=cfg.prefix,
            char_prompt=cfg.char_prompt,
            profile=cfg.profile,
            custom_neg=cfg.custom_neg,
            positive=cfg.positive,
            negative=cfg.negative,
            mode=mode,
            codes=codes_expr,
            bg=cli_bg or cfg.background,
            bg_preset=bg_preset,
            ref_image=None,
            ref_weight=cfg.ref_weight if cfg.ref_weight is not None else REF_WEIGHT_DEFAULT,
            no_ref=False,
            cn_module=None,
            cn_model=None,
            dry_run=dry_run,
            mock=mock,
            enable_freeu=enable_freeu,
            freeu_b1=freeu_b1,
            freeu_b2=freeu_b2,
            freeu_s1=freeu_s1,
            freeu_s2=freeu_s2,
            enable_adetailer=enable_adetailer,
            enable_lightning=enable_lightning,
            lightning_steps=lightning_steps,
            lightning_cfg=lightning_cfg,
            lightning_lora_name=lightning_lora_name,
        )
        # 캐릭터 설정 적용 시 db를 함께 전달하여 이벤트 섹션 자동 병합 반영
        apply_character_to_args(cfg, args, db)

        try:
            code = execute(args, roster)
            (succeeded if code == 0 else failed).append(name)
        except ConfigError as e:
            print(f"[ERROR] {name}: {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            failed.append(name)
        except KeyboardInterrupt:
            print(f"\n[중단] {name} 처리 중 취소되었습니다.", file=sys.stderr)
            failed.append(name)
            break

    print(f"\n{'=' * 64}")
    print(f"[ALL-CHARS] 완료: {len(succeeded)}명 성공 / {len(failed)}명 실패 (총 {total}명)")
    if succeeded:
        print(f"  성공: {succeeded}")
    if failed:
        print(f"  실패: {failed}")
    return 1 if failed else 0

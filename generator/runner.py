"""
generator.runner
단일 및 전체 캐릭터 배치 생성 실행 엔진 (run_interrogate, run_batch, execute, run_all_chars).
프롬프트/네거티브 씬 규칙은 generator.scene_composer 순수 도메인 모듈에 위임한다.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import io
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import requests

from generator.checkpoint import (
    CheckpointProfile,
    load_checkpoint_profiles,
    match_checkpoint_profile,
    resolve_generation_params,
)
from generator.config import (
    CHARACTERS_DIRNAME,
    REF_WEIGHT_DEFAULT,
    REFERENCES_DIRNAME,
    ConfigError,
)
from generator.models import (
    AllCharsResult,
    BatchResult,
    ControlNetSpec,
    GenerationOptions,
    PoseDatabase,
    PoseEntry,
    ReferenceImage,
    RosterPaths,
    RunHooks,
)
from generator.pose_db import (
    expand_v2_mode,
    load_pose_db,
    print_warnings,
    resolve_profile,
    resolve_targets,
)
from generator.prompt import (
    find_tag_conflicts,
    join_tags,
    resolve_background,
    validate_prefix,
    validate_ref_weight,
)
from generator.reference import (
    INTERROGATE_DEFAULT,
    INTERROGATORS,
    InterrogateResult,
    build_controlnet_unit,
    filter_gender_tags,
    find_reference_candidates,
    load_reference,
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
from generator.scene_composer import SceneComposition, compose_scene_prompt
from generator.webui_client import (
    build_txt2img_payload,
    fetch_vram_peak,
    generate_image,
    inject_alwayson_scripts,
    interrogate_image,
    interrupt_forge,
    make_dummy_png,
    resolve_sampler,
    save_as_webp,
)

logger = logging.getLogger(__name__)


def run_interrogate(
    image_path: str | Path,
    model: str = INTERROGATE_DEFAULT,
    host: str | None = None,
    session: Any | None = None,
) -> int:
    """단일 이미지에서 태그를 역추출해 성별 태그 필터링 결과와 실행 예시를 출력한다."""
    if model not in INTERROGATORS:
        raise ConfigError(
            f"지원하지 않는 interrogator 모델입니다: '{model}'",
            f"사용 가능: {', '.join(INTERROGATORS)}",
        )

    target = Path(image_path).expanduser()
    if target.is_dir():
        raise ConfigError(
            f"--from_image 에 디렉터리가 지정되었습니다: {target}",
            "이미지 파일 경로를 지정하세요.",
        )
    if not target.is_file():
        raise ConfigError(
            f"--from_image 이미지 파일을 찾을 수 없습니다: {target}",
            "올바른 이미지 경로를 지정하세요.",
        )

    ref = load_reference(target)
    try:
        caption = interrogate_image(
            ref.b64,
            model=model,
            host=host,
            session=session,
        )
    except Exception as e:
        logger.error(f"[ERROR] WebUI 태그 역추출 실패 ({model}): {e}")
        logger.error("        SD WebUI가 --api 옵션으로 실행 중인지 확인하세요.")
        return 1

    if not caption:
        logger.warning("[WARN] 추출된 태그가 비어 있습니다.")
        return 1

    tags = [t.strip() for t in caption.split(",") if t.strip()]
    _, removed = filter_gender_tags(tags)
    res = InterrogateResult(raw=caption, tags=tags, gender_tags=removed)
    recommended = res.filtered or caption

    bar = "=" * 64
    logger.info(f"\n{bar}")
    logger.info(f"  태그 추출 결과 ({model}) | {ref.label}")
    logger.info(bar)
    logger.info(f"\n[원본]\n{res.raw}")
    if res.gender_tags:
        logger.warning(f"\n[WARN] 성별·인원 태그가 감지되었습니다: {res.gender_tags}")
        logger.warning("       성별/인원은 캐릭터 기본 설정과 중복될 수 있으므로 제외 버전을 권장합니다.")
    logger.info(f"\n[권장]\n{recommended}")
    logger.info("\n[그대로 실행하려면]")
    logger.info(f'python sd_batch_generator.py --prefix PREFIX --char_prompt "{recommended}"')
    logger.info(bar)
    return 0


def _request_image_with_fallback(
    payload: dict[str, Any],
    scene: SceneComposition,
    clip_skip: int | None,
    tag: str,
) -> bytes:
    """WebUI txt2img 호출을 수행하고 HTTP 422/500 확장 충돌 시 순정 페이로드로 폴백한다."""
    try:
        return generate_image(payload)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (422, 500):
            logger.warning(
                f"[WARN] 확장 기능 충돌 감지 (HTTP {e.response.status_code}). 순정 모드로 재시도..."
            )
            fallback_payload = build_txt2img_payload(
                prompt=scene.full_prompt,
                negative_prompt=scene.actual_negative,
                sampler_name=scene.actual_sampler,
                width=scene.actual_width,
                height=scene.actual_height,
                steps=scene.actual_steps,
                cfg_scale=scene.actual_cfg,
                scheduler=scene.actual_scheduler,
                clip_skip=clip_skip,
            )
            fallback_payload = inject_alwayson_scripts(
                fallback_payload,
                scene.controlnet_units,
                False,
                1.1,
                1.2,
                0.9,
                0.2,
                False,
            )
            png_bytes = generate_image(fallback_payload)
            logger.info(f"  [{tag}] 재시도 성공 (순정 모드)")
            return png_bytes
        raise


def _generate_and_validate_image(
    *,
    payload: dict[str, Any],
    scene: SceneComposition,
    entry: PoseEntry,
    code: int,
    tag: str,
    clip_skip: int | None,
    auto_qc: bool,
    max_retries: int,
    result: BatchResult,
) -> bytes:
    """이미지 생성 및 Auto-QC 품질 검증/시드 리롤 루프를 수행한다."""
    max_attempts = (max_retries + 1) if auto_qc else 1
    png_bytes = b""

    for attempt in range(max_attempts):
        if attempt > 0:
            payload["seed"] = random.randint(1, 2**32 - 1)
        png_bytes = _request_image_with_fallback(payload, scene, clip_skip, tag)

        if not auto_qc:
            break

        try:
            from PIL import Image
            from generator.qa_validator import validate_generated_image

            img_pil = Image.open(io.BytesIO(png_bytes))
            passed, fail_reason, _ = validate_generated_image(
                image=img_pil,
                is_solo_scene=scene.is_solo_scene,
                is_two_person=scene.is_interactive,
                is_offscreen_partner=scene.is_offscreen_partner,
                is_kiss_scene=scene.is_kiss_scene,
                section=entry.section,
                prompt=scene.pose_prompt,
            )
            if not passed:
                if attempt < max_retries:
                    logger.warning(
                        f"  [{tag}] Auto-QC 불량: {fail_reason} -> 재시도 {attempt + 1}/{max_retries}"
                    )
                    continue
                logger.warning(f"  [{tag}] Auto-QC 불량(최대 재시도 초과): {fail_reason}")
                result.qa_warnings.append((code, entry.label, fail_reason))
            elif attempt > 0:
                logger.info(f"  [{tag}] Auto-QC 통과(재시도 {attempt}회)")
        except Exception as qe:
            logger.warning(f"  [{tag}] Auto-QC 건너뜀: {qe}")
        break

    return png_bytes


def _apply_post_censor(
    *,
    png_bytes: bytes,
    save_path: Path,
    prefix: str,
    filename: str,
    code: int,
    entry: PoseEntry,
    is_nude_scene: bool,
    censor_style: str,
) -> None:
    """성인/탈의 씬 검열 대상인 경우 _origin 원본 백업 후 자동 검열을 적용한다."""
    from generator.censor import censor_file, get_origin_backup_path, is_censor_target

    if not is_censor_target(code, section=entry.section, is_nude_scene=is_nude_scene):
        return

    origin_path = get_origin_backup_path(save_path)
    origin_path.parent.mkdir(parents=True, exist_ok=True)
    if not origin_path.exists():
        save_as_webp(png_bytes, origin_path)

    try:
        tmp_censored = save_path.parent / f".tmp_{filename}"
        c_res = censor_file(
            save_path,
            dst_path=tmp_censored,
            style=censor_style,
            pose_code=code,
            enable_anchor_fallback=False,
        )
        if c_res and c_res.exists():
            if save_path.exists():
                save_path.unlink()
            tmp_censored.rename(save_path)
            logger.info(
                f"  [검열본 적용: assets/{prefix}/{filename} (원본: {prefix}_origin/)]"
            )
        elif tmp_censored.exists():
            tmp_censored.unlink()
    except Exception as ce:
        logger.warning(f"  [검열 처리 예외: {ce}]")


def run_batch(
    *,
    prefix: str = "", base_positive: str = "", char_prompt: str = "", negative_prompt: str = "",
    targets: Sequence[int] = (), db: PoseDatabase, save_dir: Path, width: int, sampler_name: str,
    reference: ReferenceImage | None = None, cn_spec: ControlNetSpec | None = None,
    ref_weight: float = REF_WEIGHT_DEFAULT,
    enable_freeu: bool = False, freeu_b1: float = 1.1, freeu_b2: float = 1.2,
    freeu_s1: float = 0.9, freeu_s2: float = 0.2, enable_adetailer: bool = False,
    enable_lightning: bool = False, lightning_steps: int = 2, lightning_cfg: float = 1.0,
    lightning_lora_name: str = "sdxl_lightning_2step_lora",
    steps: int | None = None, sampler: str | None = None, scheduler: str | None = None,
    cfg_scale: float | None = None, image_width: int | None = None, image_height: int | None = None,
    clip_skip: int | None = None, naming: str = "code", overwrite: bool = False,
    active_bg: str | None = None, nude_positive: str | None = None, gender: str = "female",
    outfits: dict[str, str] | None = None, dry_run: bool = False, mock: bool = False,
    auto_qc: bool = False, max_retries: int = 3, censor: bool = False,
    censor_style: str = "white_silhouette", clamp_weights: bool = False,
    options: GenerationOptions | None = None, hooks: RunHooks | None = None,
    progress_callback: Callable[[int, str, Path, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> BatchResult:
    """단일 캐릭터의 타겟 포즈 코드 목록을 순회하며 이미지를 생성·저장한다."""
    if options is not None:
        prefix = options.prefix or prefix
        enable_freeu, enable_adetailer = options.enable_freeu, options.enable_adetailer
        freeu_b1, freeu_b2, freeu_s1, freeu_s2 = options.freeu_b1, options.freeu_b2, options.freeu_s1, options.freeu_s2
        enable_lightning, lightning_steps, lightning_cfg = options.enable_lightning, options.lightning_steps, options.lightning_cfg
        lightning_lora_name = options.lightning_lora_name
        naming, overwrite, nude_positive = options.naming, options.overwrite, options.nude_positive
        gender, outfits = options.gender or gender, options.outfits
        dry_run, mock = options.dry_run, (options.mock and not options.dry_run)
        auto_qc, max_retries = options.auto_qc, options.max_retries
        censor, censor_style, clamp_weights = options.censor, options.censor_style, options.clamp_weights

    progress_cb = hooks.progress_callback if (hooks and hooks.progress_callback) else progress_callback
    cancel_check = hooks.is_cancelled if (hooks and hooks.is_cancelled) else is_cancelled
    result = BatchResult(dry_run=dry_run)
    base_cn_unit = build_controlnet_unit(reference, cn_spec, ref_weight) if (reference and cn_spec) else None

    total_targets = len(targets)
    for idx, code in enumerate(targets, 1):
        if cancel_check and cancel_check():
            interrupt_forge()
            logger.info("[INFO] 생성이 중단되었습니다.")
            result.aborted = True
            break

        entry = db.entries[code]
        tag = format_code(code, width)
        filename = asset_filename(prefix, code, width, label=entry.label, naming=naming)
        save_path = save_dir / filename

        scene = compose_scene_prompt(
            entry=entry, prefix=prefix, tag=tag, base_positive=base_positive,
            char_prompt=char_prompt, negative_prompt=negative_prompt, sampler_name=sampler_name,
            gender=gender, active_bg=active_bg, nude_positive=nude_positive, outfits=outfits,
            steps=steps, sampler=sampler, scheduler=scheduler, cfg_scale=cfg_scale,
            image_width=image_width, image_height=image_height,
            enable_freeu=enable_freeu, enable_lightning=enable_lightning,
            lightning_steps=lightning_steps, lightning_cfg=lightning_cfg,
            lightning_lora_name=lightning_lora_name, base_controlnet_unit=base_cn_unit,
            enable_clamp_weights=clamp_weights,
        )

        if dry_run:
            result.planned.append(code)
            bg_info = f" [BG: {scene.bg_applied}]" if scene.bg_applied else ""
            logger.info(f"  [{tag}] (계획) {filename}  <- {entry.label}{bg_info}")
            continue

        if save_path.exists() and not overwrite:
            logger.info(f"  [{tag}] 이미 존재 (건너뜀) -> {filename}")
            result.skipped.append(code)
            if progress_cb:
                progress_cb(code, f"{entry.label} (기존 파일 건너뜀)", save_path, idx, total_targets)
            continue

        if scene.applied_global_lora and code == targets[0]:
            logger.info(f"[LORA] 전역 LoRA 적용: {scene.applied_global_lora}")

        step_display = f" ({scene.actual_steps or 30}스텝, CFG {scene.actual_cfg or 6.0})" if not mock else ""
        logger.info(f"  [{tag}] {'모의 생성' if mock else '생성'} 중...{step_display}")

        started = time.perf_counter()
        try:
            payload = build_txt2img_payload(
                prompt=scene.full_prompt, negative_prompt=scene.actual_negative,
                sampler_name=scene.actual_sampler, width=scene.actual_width, height=scene.actual_height,
                steps=scene.actual_steps, cfg_scale=scene.actual_cfg,
                scheduler=scene.actual_scheduler, clip_skip=clip_skip,
            )
            payload = inject_alwayson_scripts(
                payload, scene.controlnet_units, enable_freeu=scene.actual_freeu,
                freeu_b1=freeu_b1, freeu_b2=freeu_b2, freeu_s1=freeu_s1, freeu_s2=freeu_s2,
                enable_adetailer=enable_adetailer,
            )
            png_bytes = (
                make_dummy_png(prefix, code, width, entry, reference)
                if mock
                else _generate_and_validate_image(
                    payload=payload, scene=scene, entry=entry, code=code, tag=tag,
                    clip_skip=clip_skip, auto_qc=auto_qc, max_retries=max_retries, result=result,
                )
            )

            if cancel_check and cancel_check():
                logger.info("중단됨 (미완성 이미지 저장 안 함)")
                result.aborted = True
                break

            save_as_webp(png_bytes, save_path)
            if censor:
                _apply_post_censor(
                    png_bytes=png_bytes, save_path=save_path, prefix=prefix,
                    filename=filename, code=code, entry=entry,
                    is_nude_scene=scene.is_nude_scene, censor_style=censor_style,
                )
        except requests.exceptions.ConnectionError:
            logger.error("실패: WebUI 연결 불가 (--api 옵션 실행 여부 확인)")
            result.failed.append((code, "connection"))
            result.aborted = True
            break
        except Exception as e:
            logger.error(f"실패: {e}")
            result.failed.append((code, str(e)))
            continue

        elapsed = time.perf_counter() - started
        result.durations.append((code, elapsed))
        logger.info(f"완료 ({elapsed:.1f}초) -> {filename}")
        result.success.append(code)
        if progress_cb:
            progress_cb(code, entry.label, save_path, idx, total_targets)

    return result


def _setup_prompt(
    opts: GenerationOptions,
    db: PoseDatabase,
    char_prompt: str,
) -> tuple[str, str, str, str]:
    """포지티브/네거티브 프롬프트를 결정하고 Forge 기능 사용을 로깅한다."""
    if opts.positive:
        base_positive = opts.positive
        negative_prompt = opts.negative or ""
        logger.info("[POSITIVE] 캐릭터 전용 프롬프트 사용")
        if opts.negative:
            logger.info("[NEGATIVE] 캐릭터 전용 네거티브 사용")
        else:
            logger.warning("[WARN] 'negative' 미지정 - 네거티브 없이 생성합니다")
        profile_label = "전용"
        effective_char = ""
    else:
        profile = resolve_profile(db, opts.profile)
        if opts.profile:
            logger.info(f"[PROFILE] '{profile.name}' 적용")
        else:
            logger.info(f"[PROFILE] 미지정 - 기본값 '{profile.name}' 적용")
        base_positive = join_tags(profile.base_positive, char_prompt)
        negative_prompt = join_tags(profile.base_negative, opts.custom_neg)
        profile_label = profile.name
        effective_char = char_prompt

    if conflicts := find_tag_conflicts(base_positive, negative_prompt):
        logger.warning(f"[WARN] 태그 충돌: {conflicts} 가 포지티브와 네거티브에 동시 존재")

    if opts.enable_freeu:
        logger.info(
            f"[FORGE] FreeU 활성화 (b1={opts.freeu_b1}, b2={opts.freeu_b2}, "
            f"s1={opts.freeu_s1}, s2={opts.freeu_s2})"
        )
    if opts.enable_adetailer:
        logger.info("[FORGE] ADetailer 활성화 (face_yolov8n.pt)")

    if opts.enable_lightning:
        lora_tag = f"<lora:{opts.lightning_lora_name}:1.0>"
        logger.info(
            f"[LIGHTNING] Mode Enabled: {opts.lightning_steps} Steps | "
            f"CFG {opts.lightning_cfg} | Sampler: DPM++ SDE Karras | LoRA: {lora_tag}"
        )
        if opts.enable_freeu:
            logger.info("[LIGHTNING] FreeU 자동 비활성화 (Lightning과 충돌 방지)")

    return base_positive, negative_prompt, effective_char, profile_label


def _setup_reference(
    opts: GenerationOptions,
    roster: RosterPaths,
    prefix: str,
    ref_weight: float,
    dry_run: bool,
    mock: bool,
) -> tuple[ReferenceImage | None, ControlNetSpec | None]:
    """참조 이미지를 해석하고 ControlNet 스펙을 결정한다."""
    if dry_run:
        if opts.no_ref:
            logger.info("[REF]  --no_ref 지정 - 참조 이미지 사용 안 함")
        elif opts.ref_image:
            logger.info(f"[REF]  {opts.ref_image} (지정) weight {ref_weight}")
        elif found := find_reference_candidates(roster.references_dir, prefix):
            logger.info(f"[REF]  {found[0].name} 발견 weight {ref_weight}")
        else:
            logger.info(f"[REF]  없음 ({roster.references_dir}/{prefix}.*) - 텍스트만 사용")
        return None, None

    reference = resolve_reference_image(
        roster.references_dir, prefix, opts.ref_image, disabled=opts.no_ref
    )

    if reference is None:
        if not opts.no_ref:
            logger.warning(
                f"[WARN] 참조 이미지 없음 ({REFERENCES_DIRNAME}/{prefix}.*) "
                "- 텍스트 프롬프트만 사용"
            )
        return None, None

    if mock:
        cn_spec: ControlNetSpec | None = (
            ControlNetSpec(opts.cn_module, opts.cn_model, "manual")
            if opts.cn_module and opts.cn_model
            else None
        )
    else:
        cn_spec = resolve_controlnet_spec(opts.cn_module, opts.cn_model)

    if cn_spec:
        logger.info(f"[REF]  {reference.label} weight {ref_weight}")
        logger.info(f"[CN]   {cn_spec.module} / {cn_spec.model} ({cn_spec.source})")
    else:
        logger.info(f"[REF]  {reference.label} - ControlNet 미해석, 텍스트만 사용")

    return reference, cn_spec


def _setup_sampler(
    opts: GenerationOptions,
    dry_run: bool,
    mock: bool,
) -> str:
    """샘플러 이름을 결정한다 (mock/dry-run/Lightning/일반 순으로 판단)."""
    if mock or dry_run:
        return "(mock)"
    if opts.enable_lightning:
        logger.info("[SAMPLER] Lightning 모드 → 'DPM++ SDE' 고정")
        return "DPM++ SDE"
    if opts.sampler:
        logger.info(f"[SAMPLER] 사용자 지정 '{opts.sampler}' 적용")
        return opts.sampler
    return resolve_sampler()


def execute(
    args: argparse.Namespace | GenerationOptions,
    roster: RosterPaths,
    hooks: RunHooks | None = None,
) -> int:
    """생성 파이프라인 오케스트레이터. ConfigError 는 호출자가 처리한다."""
    opts = GenerationOptions.from_namespace(args)
    active_hooks = hooks if hooks is not None else RunHooks.from_namespace(args)

    dry_run: bool = opts.dry_run
    mock: bool = opts.mock and not dry_run
    if opts.mock and dry_run:
        logger.warning("[WARN] --dry-run 이 우선합니다. --mock 무시됨")

    prefix = validate_prefix(opts.prefix)
    char_prompt = (opts.char_prompt or "").strip()

    base_dir = Path.cwd()
    db = load_pose_db(base_dir, roster.events_file)
    print_warnings(db)

    base_positive, negative_prompt, effective_char, profile_label = _setup_prompt(
        opts, db, char_prompt
    )

    # ── 대상 코드 / 저장 경로 (V2 섹션 확장 SSOT) ──────
    target_mode = opts.mode
    if target_mode == "all" and not opts.codes:
        target_mode = expand_v2_mode(
            "all", db, expand_all_by_gender=opts.gender or "female"
        )

    targets = resolve_targets(db, target_mode, opts.codes)
    if not targets:
        raise ConfigError(
            "생성 대상 코드가 없습니다.", "--mode / --codes 값을 확인하세요."
        )

    width = code_width(db.all_codes)
    save_dir = roster.assets_dir / prefix
    if not dry_run:
        save_dir.mkdir(parents=True, exist_ok=True)

    ref_weight = validate_ref_weight(opts.ref_weight)
    reference, cn_spec = _setup_reference(opts, roster, prefix, ref_weight, dry_run, mock)
    sampler_name = _setup_sampler(opts, dry_run, mock)
    badge = mode_badge(dry_run, mock)

    active_bg = resolve_background(opts.bg, roster.background_file, opts.bg_preset)

    # ── 체크포인트 프로필 & 파라미터 우선순위 병합 ───────────
    active_ckpt: CheckpointProfile | None = opts.active_ckpt
    if active_ckpt is None and opts.ckpt_profile:
        ckpt_profiles = load_checkpoint_profiles(base_dir)
        active_ckpt = match_checkpoint_profile(ckpt_profiles, str(opts.ckpt_profile))
        if active_ckpt:
            logger.info(f"[CKPT] 프로필 '{active_ckpt.name}' 자동 해석 적용")

    resolved_params = resolve_generation_params(
        fallback_sampler=sampler_name,
        ckpt_profile=active_ckpt,
        char_overrides=opts.char_gen_overrides,
        explicit_steps=opts.steps,
        explicit_cfg_scale=opts.cfg_scale,
        explicit_sampler=opts.sampler,
        explicit_scheduler=opts.scheduler,
        explicit_width=opts.width,
        explicit_height=opts.height,
        explicit_clip_skip=opts.clip_skip,
    )

    clip_skip_str = f" | Clip Skip: {resolved_params.clip_skip}" if resolved_params.clip_skip else ""
    ckpt_str = f" | 체크포인트: {active_ckpt.name}" if active_ckpt else ""
    ref_status_str = "비활성화 (--no_ref)" if opts.no_ref else f"활성화 (강도 {ref_weight})"

    logger.info(
        f"\n[작업 시작]{badge} 캐릭터: {prefix} | 프로필: {profile_label} | "
        f"모드: {opts.mode} ({len(targets)}장) | 폭: {width}{ckpt_str}"
    )
    logger.info(
        f"[파라미터] 스텝: {resolved_params.steps} | 샘플러: {resolved_params.sampler} | "
        f"스케줄러: {resolved_params.scheduler} | CFG: {resolved_params.cfg_scale} | "
        f"해상도: {resolved_params.width}x{resolved_params.height}{clip_skip_str} | 레퍼런스: {ref_status_str}"
    )
    logger.info(f"[저장] {save_dir}")
    if active_bg:
        logger.info(f"[BG]   감정(emotions) 씬 배경: '{active_bg}'")
    logger.info(f"[POS]  {base_positive}")
    logger.info(f"[NEG]  {negative_prompt}\n")

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
        steps=resolved_params.steps,
        sampler=resolved_params.sampler,
        scheduler=resolved_params.scheduler,
        cfg_scale=resolved_params.cfg_scale,
        image_width=resolved_params.width,
        image_height=resolved_params.height,
        clip_skip=resolved_params.clip_skip,
        active_bg=active_bg,
        options=opts,
        hooks=active_hooks,
    )

    vram = None if (mock or dry_run) else fetch_vram_peak()
    print_summary(result, save_dir, badge, vram)

    if result.qa_warnings:
        logger.warning(
            f"\n[⚠️ Auto-QC 경고] '{prefix}' 캐릭터에서 최대 재시도(3회) 후에도 결함이 감지된 이미지 {len(result.qa_warnings)}건:"
        )
        for q_code, q_lbl, q_rsn in result.qa_warnings:
            logger.warning(f"  - [{q_code:03d} {q_lbl}] 사유: {q_rsn}")
        logger.warning("  💡 해당 이미지는 일단 저장되었으나, 포즈 데이터 또는 프롬프트 수정을 권장합니다.")

    active_hooks.last_result = result
    if not isinstance(args, GenerationOptions):
        setattr(args, "last_result", result)

    existing = result.existing
    if not existing:
        logger.info("\n[INFO] 생성된 파일이 없어 마크다운을 출력하지 않습니다.")
        return 1 if result.failed else 0

    if not dry_run:
        open_in_explorer(save_dir)

    logger.info(build_genit_block(prefix, existing, db, width, badge, naming=opts.naming))
    return 1 if (result.aborted or result.failed) else 0


def run_all_chars(
    roster: RosterPaths,
    mode: str = "all",
    codes_expr: str | None = None,
    dry_run: bool = False,
    mock: bool = False,
    naming: str = "code",
    overwrite: bool = False,
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
    steps: int | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    cfg_scale: float | None = None,
    no_ref: bool = False,
    auto_qc: bool = False,
    max_retries: int = 3,
    censor: bool = False,
    censor_style: str = "white_silhouette",
    active_ckpt: CheckpointProfile | None = None,
    ckpt_profile: str | None = None,
    clip_skip: int | None = None,
    clamp_weights: bool = False,
    *,
    options: GenerationOptions | None = None,
    hooks: RunHooks | None = None,
) -> int:
    """로스터의 모든 캐릭터를 순서대로 생성한다 (CLI 및 GUI 공용 오케스트레이터)."""
    base_opts = (
        replace(options)
        if options is not None
        else GenerationOptions(
            mode=mode,
            codes=codes_expr,
            dry_run=dry_run,
            mock=mock,
            naming=naming,
            overwrite=overwrite,
            bg=cli_bg,
            bg_preset=bg_preset,
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
            steps=steps,
            sampler=sampler,
            scheduler=scheduler,
            cfg_scale=cfg_scale,
            no_ref=no_ref,
            auto_qc=auto_qc,
            max_retries=max_retries,
            censor=censor,
            censor_style=censor_style,
            active_ckpt=active_ckpt,
            ckpt_profile=ckpt_profile,
            clip_skip=clip_skip,
            clamp_weights=clamp_weights,
        )
    )
    active_hooks = hooks if hooks is not None else RunHooks()
    summary = AllCharsResult()
    active_hooks.last_all_result = summary

    chars_dir = roster.characters_dir
    if not chars_dir.is_dir():
        logger.error(f"[ERROR] {CHARACTERS_DIRNAME}/ 폴더가 없습니다.")
        return 1

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        logger.info(f"[INFO] {CHARACTERS_DIRNAME}/ 에 json 파일이 없습니다.")
        return 0

    db = load_pose_db(Path.cwd(), roster.events_file)
    total = len(files)
    summary.total = total

    logger.info(f"\n[ALL-CHARS] {total}명 순차 생성 시작\n{'=' * 64}")

    for idx, path in enumerate(files, 1):
        if active_hooks.is_cancelled and active_hooks.is_cancelled():
            summary.aborted = True
            break

        name = path.stem
        logger.info(f"\n[ALL-CHARS] ({idx}/{total}) {name}")
        logger.info("-" * 40)

        try:
            cfg = load_character(chars_dir, name)
        except ConfigError as e:
            logger.error(f"[ERROR] {name} 로드 실패: {e}")
            summary.failed.append(name)
            continue

        if active_hooks.on_char_start:
            active_hooks.on_char_start(idx, total, name, cfg)

        char_mode = base_opts.mode
        if char_mode in ("h_scenes,special_h", "scenes_otokonoko,special_otokonoko", "h_scenes", "scenes_otokonoko"):
            is_otk = str(cfg.gender).lower() in ("otokonoko", "femboy")
            if char_mode in ("h_scenes,special_h", "scenes_otokonoko,special_otokonoko"):
                char_mode = "scenes_otokonoko,special_otokonoko" if is_otk else "h_scenes,special_h"
            else:
                char_mode = "scenes_otokonoko" if is_otk else "h_scenes"

        char_opts = replace(
            base_opts,
            prefix="",
            char_prompt="",
            profile=None,
            custom_neg="",
            positive=None,
            negative=None,
            mode=char_mode,
            bg=base_opts.bg,
            ref_weight=REF_WEIGHT_DEFAULT,
            nude_positive=None,
            gender=None,
            outfits=None,
            char_gen_overrides=None,
        )
        apply_character_to_args(cfg, char_opts, db)

        def _make_pose_cb(
            _idx: int = idx,
            _name: str = name,
            _cfg: Any = cfg,
        ) -> Callable[[int, str, Path, int, int], None]:
            def _cb(code: int, label: str, save_path: Path, cur: int, tot: int) -> None:
                if active_hooks.progress_callback:
                    active_hooks.progress_callback(code, label, save_path, cur, tot)
                if active_hooks.char_progress_callback:
                    active_hooks.char_progress_callback(
                        _idx, total, _name, _cfg, code, label, save_path, cur, tot
                    )
            return _cb

        char_hooks = RunHooks(
            progress_callback=_make_pose_cb(),
            is_cancelled=active_hooks.is_cancelled,
        )

        try:
            exit_code = execute(char_opts, roster, hooks=char_hooks)
            if active_hooks.is_cancelled and active_hooks.is_cancelled():
                summary.aborted = True
                break
            (summary.succeeded if exit_code == 0 else summary.failed).append(name)
            last_res = char_hooks.last_result
            if last_res and last_res.qa_warnings:
                for q_code, q_lbl, q_rsn in last_res.qa_warnings:
                    summary.qa_issues.append((name, q_code, q_lbl, q_rsn))
            if active_hooks.on_char_complete:
                active_hooks.on_char_complete(idx, total, name, cfg, last_res, exit_code)
        except ConfigError as e:
            logger.error(f"[ERROR] {name}: {e}")
            if e.hint:
                logger.error(f"        {e.hint}")
            summary.failed.append(name)
            if active_hooks.on_char_complete:
                active_hooks.on_char_complete(idx, total, name, cfg, None, 1)
        except KeyboardInterrupt:
            logger.error(f"\n[중단] {name} 처리 중 취소되었습니다.")
            summary.failed.append(name)
            summary.aborted = True
            break

    logger.info(f"\n{'=' * 64}")
    logger.info(
        f"[ALL-CHARS] 완료: {len(summary.succeeded)}명 성공 / {len(summary.failed)}명 실패 (총 {total}명)"
    )
    if summary.succeeded:
        logger.info(f"  성공: {summary.succeeded}")
    if summary.failed:
        logger.info(f"  실패: {summary.failed}")

    if summary.qa_issues:
        logger.warning(f"\n{'=' * 64}")
        logger.warning("🚨 [Auto-QC 종합 품질 검수 리포트: 포즈 수정 권장]")
        logger.warning(
            f"총 {len(summary.qa_issues)}개 에셋에서 최대 재시도(3회) 후에도 결함이 감지되었습니다:"
        )
        for char_name, q_code, q_lbl, q_rsn in summary.qa_issues:
            logger.warning(f"  - [{char_name}] 포즈 {q_code:03d} ({q_lbl}) ➔ {q_rsn}")
        logger.warning(
            "\n💡 [권장 조치] 위 이미지들은 시드를 변경하며 3회 재시도하였음에도 인체/인원/얼굴 결함이 지속되었습니다."
        )
        logger.warning(
            "   안정적인 결과물을 위해 해당 포즈의 구도(프롬프트), 화각, 가중치 조정을 권장합니다."
        )
        logger.warning(f"{'=' * 64}")

    return 1 if (summary.failed or summary.aborted) else 0

"""
generator.runner
단일 및 전체 캐릭터 배치 생성 실행 엔진 (run_batch, execute, run_all_chars).
"""

from __future__ import annotations

import argparse
import io
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

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
    normalize_tag,
    resolve_background,
    strip_outfit_tags,
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
    interrupt_forge,
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
    steps: int | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    cfg_scale: float | None = None,
    naming: str = "code",
    overwrite: bool = False,
    active_bg: str | None = None,
    nude_positive: str | None = None,
    gender: str = "female",
    outfits: dict[str, str] | None = None,
    dry_run: bool = False,
    mock: bool = False,
    auto_qc: bool = False,
    max_retries: int = 3,
    censor: bool = False,
    censor_style: str = "white_silhouette",
    progress_callback: Callable[[int, str, Path, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> BatchResult:
    result = BatchResult(dry_run=dry_run)

    controlnet_unit = (
        build_controlnet_unit(reference, cn_spec, ref_weight)
        if reference and cn_spec
        else None
    )

    total_targets = len(targets)
    for idx, code in enumerate(targets, 1):
        if is_cancelled and is_cancelled():
            interrupt_forge()
            print("\n[INFO] 생성이 중단되었습니다.")
            result.aborted = True
            break
        entry = db.entries[code]
        tag = format_code(code, width)
        filename = asset_filename(prefix, code, width, label=entry.label, naming=naming)
        save_path = save_dir / filename

        if dry_run:
            result.planned.append(code)
            intrinsic_kws = ("bed", "shower", "bath", "sofa", "classroom", "cafe", "library", "festival", "cathedral", "desk", "wall")
            p_check = entry.get_prompt(gender).lower()
            has_intr = any(re.search(r"\b" + re.escape(kw) + r"\b", p_check) for kw in intrinsic_kws)
            apply_bg = entry.section in ("emotions", "genit_emotions") or (not has_intr and entry.section not in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko", "adult"))
            bg_info = f" [BG: {active_bg}]" if (active_bg and apply_bg) else ""
            print(f"  [{tag}] (계획) {filename}  <- {entry.label}{bg_info}")
            continue

        if save_path.exists() and not overwrite:
            print(f"  [{tag}] 이미 존재 (건너뜀) -> {filename}")
            result.skipped.append(code)
            if progress_callback:
                progress_callback(code, f"{entry.label} (기존 파일 건너뜀)", save_path, idx, total_targets)
            continue

        actual_sampler = sampler or sampler_name
        actual_steps = steps
        actual_cfg = cfg_scale
        actual_scheduler = scheduler
        actual_freeu = enable_freeu

        step_display = f" ({actual_steps or 30}스텝, CFG {actual_cfg or 6.0})" if not mock else ""
        print(f"  [{tag}] {'모의 생성' if mock else '생성'} 중...{step_display} ", end="", flush=True)

        started = time.perf_counter()
        try:
            pose_prompt = entry.get_prompt(gender)
            intrinsic_bg_keywords = (
                "bed", "shower", "bath", "sofa", "classroom", "cafe", "library",
                "festival", "cathedral", "desk", "wall"
            )
            p_low = pose_prompt.lower()
            has_intrinsic_bg = any(re.search(r"\b" + re.escape(kw) + r"\b", p_low) for kw in intrinsic_bg_keywords)
            apply_bg = (
                entry.section in ("emotions", "genit_emotions")
                or (not has_intrinsic_bg and entry.section not in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko", "adult"))
            )
            if active_bg and apply_bg:
                if "clean background" in pose_prompt:
                    pose_prompt = pose_prompt.replace("clean background", active_bg)
                else:
                    pose_prompt = join_tags(pose_prompt, active_bg)

            # 탈의/H-씬 또는 테마 의상 스왑 분기:
            active_base_pos = base_positive
            active_char_prompt = char_prompt
            is_clothed_scene = (entry.required_outfit == "default")
            is_nude_scene = (
                not is_clothed_scene
                and (
                    entry.required_outfit == "nude"
                    or (
                        not entry.required_outfit
                        and (
                            entry.section in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko")
                            or any(kw in pose_prompt.lower() for kw in ("nude", "unclothed", "bare skin", "bare body"))
                        )
                    )
                )
            )
            if is_nude_scene:
                if nude_positive and nude_positive.strip():
                    if not char_prompt and base_positive:
                        active_base_pos = f"{nude_positive.strip()}, nude, completely nude"
                        active_char_prompt = ""
                    else:
                        active_base_pos = strip_outfit_tags(base_positive)
                        active_char_prompt = f"{nude_positive.strip()}, nude, completely nude"
                else:
                    active_base_pos = strip_outfit_tags(base_positive)
                    active_char_prompt = strip_outfit_tags(char_prompt)
            elif entry.required_outfit and entry.required_outfit not in ("default", "nude"):
                # 테마 의상(wedding, swimsuit, bunny, maid 등) 스왑
                theme_outfit = (
                    outfits[entry.required_outfit].strip()
                    if (outfits and entry.required_outfit in outfits)
                    else ""
                )
                if nude_positive and nude_positive.strip():
                    cleaned_nude = strip_outfit_tags(nude_positive.strip())
                    if not char_prompt and base_positive:
                        active_base_pos = f"{cleaned_nude}, {theme_outfit}" if theme_outfit else cleaned_nude
                        active_char_prompt = ""
                    else:
                        active_base_pos = strip_outfit_tags(base_positive)
                        active_char_prompt = f"{cleaned_nude}, {theme_outfit}" if theme_outfit else cleaned_nude
                else:
                    active_base_pos = strip_outfit_tags(base_positive)
                    stripped_char = strip_outfit_tags(char_prompt)
                    active_char_prompt = f"{stripped_char}, {theme_outfit}".strip(", ") if theme_outfit else stripped_char

            full_prompt = assemble_prompt(
                active_base_pos, active_char_prompt, pose_prompt, f"{prefix}_{tag}"
            )
            
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
            
            # 파트너가 존재하는 포즈/H-씬인 경우 남성 억제 네거티브 및 solo 태그 자동 완화, 2인 페어링 보강
            actual_negative = negative_prompt
            if entry.negative_add and entry.negative_add.strip():
                actual_negative = f"{actual_negative}, {entry.negative_add.strip()}"

            partner_keywords = (
                "partner", "faceless male", "penetration", "kiss", "fellatio",
                "hetero", "missionary", "cowgirl", "doggystyle", "cunnilingus", "fingering",
                "groping", "ass grab"
            )
            is_aftermath = any(kw in pose_prompt.lower() for kw in ("aftermath", "aftersex", "afterglow"))
            is_offscreen_partner = any(kw in pose_prompt.lower() for kw in ("off-screen", "offscreen", "completely off-screen"))
            is_solo_scene = (
                "solo focus" in pose_prompt.lower()
                or "solo, " in pose_prompt.lower()
                or ", solo" in pose_prompt.lower()
                or ("spreading own legs" in pose_prompt.lower() and "partner" not in pose_prompt.lower())
            )
            is_interactive = (
                any(k in pose_prompt.lower() for k in partner_keywords)
                or entry.section in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko")
            ) and not is_aftermath and not is_solo_scene

            if is_interactive:
                male_neg_tokens = {
                    "1boy", "2boys", "male", "masculine", "man", "men", "guy", "boy", "boys",
                    "yaoi", "multiple characters", "beard", "mustache", "facial hair"
                }
                neg_tags = [t.strip() for t in actual_negative.split(",") if t.strip()]
                filtered_neg = []
                for t in neg_tags:
                    clean_t = normalize_tag(t)
                    if clean_t not in male_neg_tokens:
                        filtered_neg.append(t)
                actual_negative = ", ".join(filtered_neg)

                # 상호작용 씬에서 두 인물이 서로 멀리 떨어져 생성되는 현상 차단
                if not is_offscreen_partner:
                    actual_negative = f"{actual_negative}, (standing apart, separated, distance between characters:1.3)"

                # 모브 남성에게 얼굴이나 표정/감정이 이식되는 현상 및 추가 머리/복수 남성 증식 차단
                actual_negative = f"{actual_negative}, ((male face, detailed male face, handsome male, male eyes, visible male face, male expression, male blush:1.55)), ((2boys, 2males, multiple males, extra male, extra head, two heads:1.4))"

                # 파트너가 화면 밖(off-screen)인 POV 씬: 남성 몸통/하반신/바지 화면 침범 원천 차단
                if is_offscreen_partner:
                    actual_negative = f"{actual_negative}, ((male body, male torso, male lower body, male legs, pants, trousers, black pants, jeans, lap, sitting between legs, legs of partner, 1boy:1.5))"

                # 샤워 씬의 경우 유리문/타일이 창살/격자/블라인드로 왜곡되는 현상 방지
                if "shower" in pose_prompt.lower():
                    actual_negative = f"{actual_negative}, (bars, vertical bars, fence, lattice, cage, blinds, grating:1.3)"

                # 탈의 씬에서 목이나 팔 등에 의상 파편(하이넥, 소매, 칼라 등), 언더붑/언더버스트 탑, 코르셋 및 수영복 생성 차단
                if is_nude_scene:
                    actual_negative = f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, crop top, halter, straps:1.35)"

                # 침대 및 탈의 씬에서 불필요한 와인/음료/유리잔/병 오브젝트 생성 차단
                if is_nude_scene or "on bed" in pose_prompt.lower():
                    actual_negative = f"{actual_negative}, (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"

                # solo 태그 제거 (off-screen 파트너 씬은 제외)
                if not is_offscreen_partner:
                    for solo_pat in (", solo", "solo,", " solo "):
                        full_prompt = full_prompt.replace(solo_pat, " ")
                    full_prompt = " ".join(full_prompt.split())

                # 2인 구도 페어링 태그 보강 및 Attention 격리 (화면 내 파트너가 직접 등장하는 경우에만 적용)
                if not is_offscreen_partner:
                    is_otokonoko_char = "otokonoko" in active_base_pos.lower() or "otokonoko" in active_char_prompt.lower()
                    if entry.section in ("scenes_otokonoko", "special_otokonoko") or (is_otokonoko_char and is_interactive):
                        if "yaoi" not in full_prompt.lower() and "2boys" not in full_prompt.lower():
                            full_prompt = f"yaoi, 2boys, faceless male, {full_prompt}"
                        full_prompt = full_prompt.replace("hetero,", "").replace("hetero", "")
                    elif entry.section in ("h_scenes", "special_h") or "hetero" in pose_prompt.lower() or is_interactive:
                        if "hetero" not in full_prompt.lower():
                            full_prompt = f"hetero, 1boy, faceless male, {full_prompt}"
                        elif "1boy" not in full_prompt.lower():
                            full_prompt = f"1boy, faceless male, {full_prompt}"

                    # 모브 남성 파트너 무특징화: BREAK를 통한 여주인공과 모브의 Attention 완전 격리 (이염 원천 차단)
                    # 착의 스킨십 씬(단색 블랙 팬츠 고정) vs 탈의 성인 씬(완전 탈의) 하의 정밀 분기
                    # featureless silhouette 제거(암전/가면 결함 방지), 순수 음영 및 구도 중심 모브화
                    is_head_hug_scene = any(
                        kw in pose_prompt.lower()
                        for kw in (
                            "head on chest", "burying face", "face buried", "holding head",
                            "hugging head", "patting head", "kissing head", "hair from behind",
                            "back of male", "back of head"
                        )
                    )
                    is_rear_scene = (
                        any(kw in pose_prompt.lower() for kw in ("back hug", "doggystyle", "sumata", "rear", "prone", "grabbed from behind", "hugging from behind"))
                        or ("behind" in pose_prompt.lower() and not any(f in pose_prompt.lower() for f in ("head from behind", "hair from behind", "face buried", "cleavage", "buried between breasts", "nestled in cleavage")))
                    )
                    rear_direction = "(from behind:1.15), " if is_rear_scene else ""
                    male_face_suppression = "(detailed male face:1.4), (detailed eyes on male:1.35), (handsome male:1.4), (attractive male face:1.3), (expressive male face:1.3), (male portrait:1.3), male hair, male bangs, male haircut"

                    if is_head_hug_scene:
                        mob_positive = ""
                        mob_negative = f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), (standing male:1.5), (tall male:1.5), (standing beside:1.5), (2boys:1.8), (multiple boys:1.8), (duplicate male:1.8)"
                    elif is_nude_scene:
                        mob_positive = f"BREAK (single faceless male partner:1.15), (unseen eyes:1.15), (obscured male face:1.15), (male head out of frame:1.15), {rear_direction}(naked male:1.15), (bottomless male:1.15), muscular build"
                        mob_negative = f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), male clothes, male shirt, pants, trousers, jeans, shorts, underwear, (mask, white mask, noh mask, blank face, pupil-less:1.3), (2boys:1.6), (multiple boys:1.6), (multiple males:1.6), (extra male:1.6)"
                    else:
                        mob_positive = f"BREAK (single faceless male partner:1.15), (unseen eyes:1.15), (obscured male face:1.15), (male head out of frame:1.15), {rear_direction}(shirtless male:1.1), (bare shoulders:1.1), (solid black pants:1.2), muscular build"
                        mob_negative = f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), male clothes, male shirt, male t-shirt, male jacket, male suit, (naked male:1.2), (bottomless:1.2), (male underwear:1.2), jeans, blue pants, (mask, white mask, noh mask, blank face, pupil-less:1.3), (2boys:1.6), (multiple boys:1.6), (multiple males:1.6), (extra male:1.6)"

                    if mob_positive:
                        full_prompt = f"{full_prompt} {mob_positive}"
                    actual_negative = f"{actual_negative}, {mob_negative}"

                    # 가로로 눕는 2인 체위에서 세로 캔버스 여백에 남성 하반신이 복제되지 않도록 spitroast 차단
                    is_horizontal_scene = any(kw in pose_prompt.lower() for kw in ("lying", "reclining", "on bed", "on back", "missionary"))
                    if is_horizontal_scene:
                        actual_negative = f"{actual_negative}, (spitroast, duplicate torso, extra legs at bottom, extra male lower body:1.4)"

            # 사후여운 씬인 경우 모브/남성 파트너 얼굴 및 신체 생성 원천 차단
            if is_aftermath:
                actual_negative = f"{actual_negative}, ((1boy, 2boys, male, masculine, partner, faceless male, multiple characters, extra face:1.5))"
                if is_nude_scene:
                    actual_negative = f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, crop top, halter, straps:1.35), (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"

            # 솔로 씬인 경우 모브/남성 파트너 얼굴 및 신체 생성 원천 차단
            if is_solo_scene:
                actual_negative = f"{actual_negative}, ((1boy, 2boys, male, masculine, partner, faceless male, multiple characters, extra face:1.5))"
                if is_nude_scene:
                    actual_negative = f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, crop top, halter, straps:1.35), (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"

            # 목욕(bath) 씬인 경우 물 위에 구슬/공/장난감/이물질/동물이 뜨거나 좁은 통 욕조가 생성되는 현상 차단
            if "bath" in pose_prompt.lower():
                actual_negative = (
                    f"{actual_negative}, ((marbles, glass marbles, balls, floating balls, spheres, orbs, "
                    f"bath bombs, rubber duck, toys, floating objects, petals, bubbles, animal, cat, dog, pet, "
                    f"fire, flame, flames, torch, white border, picture frame, "
                    f"narrow bathtub, cramped tub, small plastic tub:1.45))"
                )

            # 웨딩드레스(wedding) 테마 의상인 경우:
            # 1) 캐릭터 기본 네거티브에 걸려 있는 흰색 드레스/스커트/긴소매/티아라/프릴 억제 태그를 자동 해제
            # 2) 과도한 노출/란제리/비키니/가슴골/슬릿 요소를 네거티브로 강력 차단하여 품격 있는 웨딩드레스 보장
            if entry.required_outfit == "wedding":
                wedding_conflict_neg = {
                    "white dress", "white clothes", "white sweater", "white skirt", "white bodice",
                    "white chest", "two-tone dress", "bicolored clothes", "half white", "white robe",
                    "white vest", "white blouse", "long skirt", "maxi dress", "train", "cape",
                    "sleeves", "long sleeves", "puffy sleeves", "frills", "frilled skirt", "pleated skirt",
                    "tiara", "circlet", "hair ornament", "headwear"
                }
                neg_parts = [t.strip() for t in actual_negative.split(",") if t.strip()]
                cleaned_neg_parts = [
                    t for t in neg_parts if normalize_tag(t) not in wedding_conflict_neg
                ]
                actual_negative = ", ".join(cleaned_neg_parts)
                actual_negative = (
                    f"{actual_negative}, ((cleavage, deep cleavage, exposed breasts, plunging neckline, "
                    f"strapless, low-cut, bare breasts, nipples, lingerie, bikini, micro bikini, corset, "
                    f"garter belt, garter straps, thighhighs, black stockings, cutout, chest cutout, "
                    f"side slit, pelvic curtain, navel, midriff, short dress, micro skirt, mini skirt, "
                    f"see-through breasts:1.4))"
                )

            payload = build_txt2img_payload(
                prompt=full_prompt,
                negative_prompt=actual_negative,
                sampler_name=actual_sampler,
                width=actual_width,
                height=actual_height,
                steps=actual_steps,
                cfg_scale=actual_cfg,
                scheduler=actual_scheduler,
            )

            controlnet_units: list[dict] = []
            if controlnet_unit is not None:
                unit = dict(controlnet_unit)
                # 탈의 씬 또는 테마 의상(웨딩드레스 등) 스왑인 경우 참조 이미지의 기본 의상이 강제 투영되지 않도록 IP-Adapter 가중치를 0.5로 완화
                is_theme_swap = bool(entry.required_outfit and entry.required_outfit not in ("default", "nude"))
                if (is_nude_scene or is_theme_swap) and unit.get("weight", 0) > 0.5:
                    unit["weight"] = 0.5
                controlnet_units.append(unit)

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
                max_attempts = (max_retries + 1) if auto_qc else 1
                for attempt in range(max_attempts):
                    if attempt > 0:
                        payload["seed"] = random.randint(1, 2**32 - 1)
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

                    if auto_qc:
                        try:
                            from PIL import Image
                            from generator.qa_validator import validate_generated_image
                            img_pil = Image.open(io.BytesIO(png_bytes))
                            is_kiss = "kiss" in pose_prompt.lower() or code in (60, 61, 198, 199, 19)
                            passed, fail_reason, _ = validate_generated_image(
                                image=img_pil,
                                is_solo_scene=is_solo_scene,
                                is_two_person=is_interactive,
                                is_offscreen_partner=is_offscreen_partner,
                                is_kiss_scene=is_kiss,
                                section=entry.section,
                                pose_code=code,
                            )
                            if not passed:
                                if attempt < max_retries:
                                    print(f" [Auto-QC 불량: {fail_reason} -> 재시도 {attempt+1}/{max_retries}]", end="", flush=True)
                                    continue
                                else:
                                    print(f" [Auto-QC 불량(최대 재시도 초과): {fail_reason}]", end="", flush=True)
                                    result.qa_warnings.append((code, entry.label, fail_reason))
                            elif attempt > 0:
                                print(f" [Auto-QC 통과(재시도 {attempt}회)]", end="", flush=True)
                        except Exception as qe:
                            print(f" [Auto-QC 건너뜀: {qe}]", end="", flush=True)
                    break

            if is_cancelled and is_cancelled():
                print("중단됨 (미완성 이미지 저장 안 함)")
                result.aborted = True
                break

            save_as_webp(png_bytes, save_path)

            # 성인 씬 검열 및 무검열 원본 assets/{prefix}_origin/ 백업
            if censor and (is_nude_scene or entry.section in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko")):
                origin_dir = save_dir.parent / f"{prefix}_origin"
                origin_dir.mkdir(parents=True, exist_ok=True)
                origin_path = origin_dir / filename
                if not origin_path.exists():
                    save_as_webp(png_bytes, origin_path)
                try:
                    from generator.censor import censor_file
                    tmp_censored = save_dir / f".tmp_{filename}"
                    c_res = censor_file(save_path, dst_path=tmp_censored, style=censor_style, pose_code=code, enable_anchor_fallback=False)
                    if c_res and c_res.exists():
                        if save_path.exists():
                            save_path.unlink()
                        tmp_censored.rename(save_path)
                        print(f" [검열본 적용: assets/{prefix}/{filename} (원본: {prefix}_origin/)]", end="")
                    elif tmp_censored.exists():
                        tmp_censored.unlink()
                except Exception as ce:
                    print(f" [검열 처리 예외: {ce}]", end="")
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

        if progress_callback:
            progress_callback(code, entry.label, save_path, idx, total_targets)

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
    if getattr(args, "sampler", None):
        user_sampler = args.sampler
        print(f"[SAMPLER] 사용자 지정 '{user_sampler}' 적용")
        return user_sampler
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
    # mode가 'all'이고 특정 codes가 없을 때, 캐릭터의 default_mode/gender를 기반으로 여성 100장 또는 오토코노코 100장으로 자동 라우팅
    target_mode = args.mode
    if target_mode == "all" and not getattr(args, "codes", None):
        char_gender = getattr(args, "gender", "female")
        is_otk = str(char_gender).lower() in ("otokonoko", "femboy")
        base_secs = (
            ["emotions", "poses", "events", "romance", "scenes_otokonoko", "special_otokonoko"]
            if is_otk
            else ["emotions", "poses", "events", "romance", "h_scenes", "special_h"]
        )
        active_secs = [s for s in base_secs if s in db.sections]
        builtin_sections = {
            "emotions", "poses", "events", "romance",
            "h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko",
            "genit_actions"
        }
        custom_sections = [sec for sec in db.sections if sec not in builtin_sections]
        target_mode = ",".join(active_secs + custom_sections)

    targets = resolve_targets(db, target_mode, args.codes)
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
    active_steps = getattr(args, "steps", 30) or 30
    active_sampler_str = getattr(args, "sampler", None) or sampler_name
    active_sched_str = getattr(args, "scheduler", None) or "Automatic"
    active_cfg_val = getattr(args, "cfg_scale", 6.0) or 6.0
    ref_status_str = "비활성화 (--no_ref)" if getattr(args, "no_ref", False) else f"활성화 (강도 {ref_weight})"

    print(
        f"\n[작업 시작]{badge} 캐릭터: {prefix} | 프로필: {profile_label} | "
        f"모드: {args.mode} ({len(targets)}장) | 폭: {width}"
    )
    print(
        f"[파라미터] 스텝: {active_steps} | 샘플러: {active_sampler_str} | "
        f"스케줄러: {active_sched_str} | CFG: {active_cfg_val} | 레퍼런스: {ref_status_str}"
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
        steps=getattr(args, "steps", None),
        sampler=getattr(args, "sampler", None),
        scheduler=getattr(args, "scheduler", None),
        cfg_scale=getattr(args, "cfg_scale", None),
        naming=getattr(args, "naming", "code"),
        overwrite=getattr(args, "overwrite", False),
        active_bg=active_bg,
        nude_positive=getattr(args, "nude_positive", None),
        gender=getattr(args, "gender", "female"),
        outfits=getattr(args, "outfits", None),
        dry_run=dry_run,
        mock=mock,
        auto_qc=getattr(args, "auto_qc", False) or getattr(args, "auto_retry_qa", False),
        max_retries=getattr(args, "max_retries", 3),
        censor=getattr(args, "censor", False),
        censor_style=getattr(args, "censor_style", "white_silhouette"),
        progress_callback=getattr(args, "progress_callback", None),
        is_cancelled=getattr(args, "is_cancelled", None),
    )

    vram = None if (mock or dry_run) else fetch_vram_peak()
    print_summary(result, save_dir, badge, vram)

    if result.qa_warnings:
        print(f"\n[⚠️ Auto-QC 경고] '{prefix}' 캐릭터에서 최대 재시도(3회) 후에도 결함이 감지된 이미지 {len(result.qa_warnings)}건:")
        for q_code, q_lbl, q_rsn in result.qa_warnings:
            print(f"  - [{q_code:03d} {q_lbl}] 사유: {q_rsn}")
        print("  💡 해당 이미지는 일단 저장되었으나, 포즈 데이터 또는 프롬프트 수정을 권장합니다.")

    setattr(args, "last_result", result)

    existing = result.existing
    if not existing:
        print("\n[INFO] 생성된 파일이 없어 마크다운을 출력하지 않습니다.")
        return 1 if result.failed else 0

    if not dry_run:
        open_in_explorer(save_dir)

    naming_mode = getattr(args, "naming", "code")
    print(build_genit_block(prefix, existing, db, width, badge, naming=naming_mode))
    return 1 if result.aborted else 0


def run_all_chars(
    roster: RosterPaths,
    mode: str,
    codes_expr: str | None,
    dry_run: bool,
    mock: bool,
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
    all_qa_issues: list[tuple[str, int, str, str]] = []  # (char_name, code, label, reason)

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
            no_ref=no_ref,
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
            steps=steps,
            sampler=sampler,
            scheduler=scheduler,
            cfg_scale=cfg_scale,
            naming=naming,
            overwrite=overwrite,
            nude_positive=cfg.nude_positive,
            gender=cfg.gender,
            outfits=cfg.outfits,
            auto_qc=auto_qc,
            max_retries=max_retries,
            censor=censor,
            censor_style=censor_style,
        )
        # 캐릭터 설정 적용 시 db를 함께 전달하여 이벤트 섹션 자동 병합 반영
        apply_character_to_args(cfg, args, db)

        try:
            code = execute(args, roster)
            (succeeded if code == 0 else failed).append(name)
            last_res = getattr(args, "last_result", None)
            if last_res and getattr(last_res, "qa_warnings", None):
                for q_code, q_lbl, q_rsn in last_res.qa_warnings:
                    all_qa_issues.append((name, q_code, q_lbl, q_rsn))
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

    if all_qa_issues:
        print(f"\n{'=' * 64}")
        print(f"🚨 [Auto-QC 종합 품질 검수 리포트: 포즈 수정 권장]")
        print(f"총 {len(all_qa_issues)}개 에셋에서 최대 재시도(3회) 후에도 결함이 감지되었습니다:")
        for char_name, q_code, q_lbl, q_rsn in all_qa_issues:
            print(f"  - [{char_name}] 포즈 {q_code:03d} ({q_lbl}) ➔ {q_rsn}")
        print("\n💡 [권장 조치] 위 이미지들은 시드를 변경하며 3회 재시도하였음에도 인체/인원/얼굴 결함이 지속되었습니다.")
        print("   안정적인 결과물을 위해 해당 포즈의 구도(프롬프트), 화각, 가중치 조정을 권장합니다.")
        print(f"{'=' * 64}")

    return 1 if failed else 0

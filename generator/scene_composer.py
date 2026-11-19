"""
generator.scene_composer
단일 포즈 씬의 배경 적용, 의상 스왑/탈의 스트리핑, 2인 상호작용/솔로/사후여운/오프스크린 분기,
동적 포지티브·네거티브 변조, LoRA/Lightning 및 ControlNet 가중치 조정을 수행하는 순수 도메인 모듈.
(네트워크 및 파일 I/O가 전혀 없으며 dry-run과 실생성이 동일 로직을 공유한다.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from generator.config import IMAGE_SIZE, LORA_STRING
from generator.models import PoseEntry
from generator.prompt import (
    NEG_MAX_WEIGHT,
    POS_MAX_WEIGHT,
    assemble_prompt,
    clamp_prompt_weights,
    harmonize_character_expression,
    join_tags,
    normalize_tag,
    remove_conflicting_tags_from_negative,
    resolve_theme_outfit,
    strip_outfit_tags,
)

INTRINSIC_BG_KEYWORDS: tuple[str, ...] = (
    "bed",
    "shower",
    "bath",
    "sofa",
    "classroom",
    "cafe",
    "library",
    "festival",
    "cathedral",
    "desk",
    "wall",
)

NON_BG_SECTIONS: frozenset[str] = frozenset({
    "h_scenes",
    "special_h",
    "scenes_otokonoko",
    "special_otokonoko",
    "adult",
})

PARTNER_KEYWORDS: tuple[str, ...] = (
    "partner",
    "faceless male",
    "penetration",
    "kiss",
    "fellatio",
    "hetero",
    "missionary",
    "cowgirl",
    "doggystyle",
    "cunnilingus",
    "fingering",
    "groping",
    "ass grab",
)

MALE_NEG_TOKENS: frozenset[str] = frozenset({
    "1boy",
    "2boys",
    "male",
    "masculine",
    "man",
    "men",
    "guy",
    "boy",
    "boys",
    "yaoi",
    "multiple characters",
    "beard",
    "mustache",
    "facial hair",
})

WEDDING_CONFLICT_NEG: frozenset[str] = frozenset({
    "white dress",
    "white clothes",
    "white sweater",
    "white skirt",
    "white bodice",
    "white chest",
    "two-tone dress",
    "bicolored clothes",
    "half white",
    "white robe",
    "white vest",
    "white blouse",
    "long skirt",
    "maxi dress",
    "train",
    "cape",
    "sleeves",
    "long sleeves",
    "puffy sleeves",
    "frills",
    "frilled skirt",
    "pleated skirt",
    "tiara",
    "circlet",
    "hair ornament",
    "headwear",
})

_COMPOSITION_ANTI_DRIFT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("tight bust portrait", "close-up bust shot", "intimate close-up"),
        "(cowboy shot, full body, lower body, wide shot, knees, feet:1.25)",
    ),
    (
        ("cowboy shot",),
        "(tight bust portrait, close-up face, full body, wide shot, dutch angle, fisheye:1.2)",
    ),
    (
        ("full body",),
        "(cropped legs, tight bust portrait, close-up face, dutch angle, fisheye:1.2)",
    ),
)


def apply_composition_anti_drift(pose_prompt: str, negative_prompt: str) -> str:
    """샷 사이즈별 반대 구도 이탈 방지 네거티브를 자동 주입한다 (순수 함수)."""
    pose_low = (pose_prompt or "").lower()
    if not pose_low:
        return negative_prompt
    for shot_kws, anti_neg in _COMPOSITION_ANTI_DRIFT_RULES:
        if "full body" in shot_kws and "portrait" in pose_low:
            continue
        if any(kw in pose_low for kw in shot_kws):
            return f"{negative_prompt}, {anti_neg}" if negative_prompt else anti_neg
    return negative_prompt


@dataclass(frozen=True, slots=True)
class SceneComposition:
    """단일 포즈 코드에 대해 조립된 프롬프트·네거티브·생성 파라미터 및 씬 분류 결과."""

    pose_prompt: str
    apply_bg: bool
    bg_applied: str | None
    active_base_pos: str
    active_char_prompt: str
    full_prompt: str
    actual_negative: str
    actual_sampler: str
    actual_steps: int | None
    actual_cfg: float | None
    actual_scheduler: str | None
    actual_freeu: bool
    actual_width: int
    actual_height: int
    is_clothed_scene: bool
    is_nude_scene: bool
    is_aftermath: bool
    is_offscreen_partner: bool
    is_solo_scene: bool
    is_interactive: bool
    is_kiss_scene: bool
    controlnet_units: list[dict[str, Any]]
    applied_global_lora: str | None = None


def should_apply_background(entry: PoseEntry, pose_prompt: str) -> bool:
    """해당 포즈에 감정/로스터 배경(active_bg)을 주입할지 여부를 판별한다 (순수 함수)."""
    p_low = pose_prompt.lower()
    has_intrinsic_bg = any(
        re.search(r"\b" + re.escape(kw) + r"\b", p_low)
        for kw in INTRINSIC_BG_KEYWORDS
    )
    return (
        entry.section in ("emotions", "genit_emotions")
        or (not has_intrinsic_bg and entry.section not in NON_BG_SECTIONS)
    )


def compose_scene_prompt(
    *,
    entry: PoseEntry,
    prefix: str,
    tag: str,
    base_positive: str,
    char_prompt: str,
    negative_prompt: str,
    sampler_name: str,
    gender: str = "female",
    active_bg: str | None = None,
    nude_positive: str | None = None,
    outfits: dict[str, str] | None = None,
    steps: int | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    cfg_scale: float | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    enable_freeu: bool = False,
    enable_lightning: bool = False,
    lightning_steps: int = 2,
    lightning_cfg: float = 1.0,
    lightning_lora_name: str = "sdxl_lightning_2step_lora",
    base_controlnet_unit: dict[str, Any] | None = None,
    enable_clamp_weights: bool = False,
    global_lora_string: str | None = None,
) -> SceneComposition:
    """단일 포즈 엔트리에 대한 최종 포지티브/네거티브 및 실행 파라미터를 조립한다 (순수 함수)."""
    actual_sampler = sampler or sampler_name
    actual_steps = steps
    actual_cfg = cfg_scale
    actual_scheduler = scheduler
    actual_freeu = enable_freeu

    pose_prompt = entry.get_prompt(gender or "female")
    apply_bg = should_apply_background(entry, pose_prompt)
    bg_applied: str | None = None
    if active_bg and apply_bg:
        bg_applied = active_bg
        if "clean background" in pose_prompt:
            pose_prompt = pose_prompt.replace("clean background", active_bg)
        else:
            pose_prompt = join_tags(pose_prompt, active_bg)

    # ── 탈의/H-씬 또는 테마 의상 스왑 분기 ──────────────────
    active_base_pos = base_positive
    active_char_prompt = char_prompt
    is_clothed_scene = entry.required_outfit == "default"
    is_nude_scene = (
        not is_clothed_scene
        and (
            entry.required_outfit == "nude"
            or (
                not entry.required_outfit
                and (
                    entry.section in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko")
                    or any(
                        kw in pose_prompt.lower()
                        for kw in ("nude", "unclothed", "bare skin", "bare body")
                    )
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
        theme_outfit = resolve_theme_outfit(outfits, entry.required_outfit)
        if nude_positive and nude_positive.strip():
            cleaned_nude = strip_outfit_tags(nude_positive.strip())
            if not char_prompt and base_positive:
                active_base_pos = (
                    f"{cleaned_nude}, {theme_outfit}" if theme_outfit else cleaned_nude
                )
                active_char_prompt = ""
            else:
                active_base_pos = strip_outfit_tags(base_positive)
                active_char_prompt = (
                    f"{cleaned_nude}, {theme_outfit}" if theme_outfit else cleaned_nude
                )
        else:
            active_base_pos = strip_outfit_tags(base_positive)
            stripped_char = strip_outfit_tags(char_prompt)
            active_char_prompt = (
                f"{stripped_char}, {theme_outfit}".strip(", ")
                if theme_outfit
                else stripped_char
            )

    # 캐릭터 기본 외형 표정과 포즈 표정 간 충돌 자동 조화
    if " BREAK " in active_base_pos:
        q_part, c_part = active_base_pos.split(" BREAK ", 1)
        active_base_pos = f"{q_part} BREAK {harmonize_character_expression(c_part, pose_prompt)}"
    elif not active_char_prompt and active_base_pos:
        active_base_pos = harmonize_character_expression(active_base_pos, pose_prompt)
    if active_char_prompt:
        active_char_prompt = harmonize_character_expression(active_char_prompt, pose_prompt)

    full_prompt = assemble_prompt(
        active_base_pos, active_char_prompt, pose_prompt, f"{prefix}_{tag}"
    )

    applied_global_lora: str | None = None
    active_lora = LORA_STRING if global_lora_string is None else global_lora_string
    if enable_lightning:
        actual_sampler = "DPM++ SDE"
        actual_steps = lightning_steps
        actual_cfg = lightning_cfg
        actual_scheduler = "Karras"
        lora_tag = f"<lora:{lightning_lora_name}:1.0>"
        full_prompt = f"{lora_tag}, {full_prompt}"
        if enable_freeu:
            actual_freeu = False
    elif active_lora:
        full_prompt = f"{active_lora}, {full_prompt}"
        applied_global_lora = active_lora

    actual_width = entry.width if entry.width else (image_width or IMAGE_SIZE[0])
    actual_height = entry.height if entry.height else (image_height or IMAGE_SIZE[1])

    # ── 씬 유형 판별 및 동적 포지티브/네거티브 변조 ──────────
    actual_negative = negative_prompt
    if entry.negative_add and entry.negative_add.strip():
        actual_negative = f"{actual_negative}, {entry.negative_add.strip()}"

    p_low = pose_prompt.lower()
    is_aftermath = any(kw in p_low for kw in ("aftermath", "aftersex", "afterglow"))
    is_offscreen_partner = any(
        kw in p_low for kw in ("off-screen", "offscreen", "completely off-screen")
    )
    is_solo_scene = (
        "solo focus" in p_low
        or "solo, " in p_low
        or ", solo" in p_low
        or ("spreading own legs" in p_low and "partner" not in p_low)
    )
    is_interactive = (
        (
            any(k in p_low for k in PARTNER_KEYWORDS)
            or entry.section in ("h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko")
        )
        and not is_aftermath
        and not is_solo_scene
    )

    if is_interactive:
        neg_tags = [t.strip() for t in actual_negative.split(",") if t.strip()]
        filtered_neg = [
            t for t in neg_tags if normalize_tag(t) not in MALE_NEG_TOKENS
        ]
        actual_negative = ", ".join(filtered_neg)

        if not is_offscreen_partner:
            actual_negative = (
                f"{actual_negative}, (standing apart, separated, distance between characters:1.3)"
            )

        actual_negative = (
            f"{actual_negative}, ((male face, detailed male face, handsome male, male eyes, "
            f"visible male face, male expression, male blush:1.55)), "
            f"((2boys, 2males, multiple males, extra male, extra head, two heads:1.4))"
        )

        if is_offscreen_partner:
            actual_negative = (
                f"{actual_negative}, ((male body, male torso, male lower body, male legs, "
                f"pants, trousers, black pants, jeans, lap, sitting between legs, legs of partner, 1boy:1.5))"
            )

        if "shower" in p_low:
            actual_negative = (
                f"{actual_negative}, (bars, vertical bars, fence, lattice, cage, blinds, grating:1.3)"
            )

        if is_nude_scene:
            actual_negative = (
                f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, "
                f"rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, "
                f"crop top, halter, straps:1.35)"
            )

        if is_nude_scene or "on bed" in p_low:
            actual_negative = (
                f"{actual_negative}, (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"
            )

        if not is_offscreen_partner:
            for solo_pat in (", solo", "solo,", " solo "):
                full_prompt = full_prompt.replace(solo_pat, " ")
            full_prompt = " ".join(full_prompt.split())

            is_otokonoko_char = (
                "otokonoko" in active_base_pos.lower()
                or "otokonoko" in active_char_prompt.lower()
            )
            if entry.section in ("scenes_otokonoko", "special_otokonoko") or (
                is_otokonoko_char and is_interactive
            ):
                if "yaoi" not in full_prompt.lower() and "2boys" not in full_prompt.lower():
                    full_prompt = f"yaoi, 2boys, faceless male, {full_prompt}"
                full_prompt = full_prompt.replace("hetero,", "").replace("hetero", "")
            elif (
                entry.section in ("h_scenes", "special_h")
                or "hetero" in p_low
                or is_interactive
            ):
                if "hetero" not in full_prompt.lower():
                    full_prompt = f"hetero, 1boy, faceless male, {full_prompt}"
                elif "1boy" not in full_prompt.lower():
                    full_prompt = f"1boy, faceless male, {full_prompt}"

            is_head_hug_scene = any(
                kw in p_low
                for kw in (
                    "head on chest",
                    "burying face",
                    "face buried",
                    "holding head",
                    "hugging head",
                    "patting head",
                    "kissing head",
                    "hair from behind",
                    "back of male",
                    "back of head",
                )
            )
            is_rear_scene = (
                any(
                    kw in p_low
                    for kw in (
                        "back hug",
                        "doggystyle",
                        "sumata",
                        "rear",
                        "prone",
                        "grabbed from behind",
                        "hugging from behind",
                    )
                )
                or (
                    "behind" in p_low
                    and not any(
                        f in p_low
                        for f in (
                            "head from behind",
                            "hair from behind",
                            "face buried",
                            "cleavage",
                            "buried between breasts",
                            "nestled in cleavage",
                        )
                    )
                )
            )
            rear_direction = "(from behind:1.15), " if is_rear_scene else ""
            male_face_suppression = (
                "(detailed male face:1.4), (detailed eyes on male:1.35), (handsome male:1.4), "
                "(attractive male face:1.3), (expressive male face:1.3), (male portrait:1.3), "
                "male hair, male bangs, male haircut"
            )

            if is_head_hug_scene:
                mob_positive = ""
                mob_negative = (
                    f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), "
                    f"(standing male:1.5), (tall male:1.5), (standing beside:1.5), "
                    f"(2boys:1.8), (multiple boys:1.8), (duplicate male:1.8)"
                )
            elif is_nude_scene:
                mob_positive = (
                    f"BREAK (single faceless male partner:1.15), (unseen eyes:1.15), "
                    f"(obscured male face:1.15), (male head out of frame:1.15), "
                    f"{rear_direction}(naked male:1.15), (bottomless male:1.15), muscular build"
                )
                mob_negative = (
                    f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), "
                    f"male clothes, male shirt, pants, trousers, jeans, shorts, underwear, "
                    f"(mask, white mask, noh mask, blank face, pupil-less:1.3), "
                    f"(2boys:1.6), (multiple boys:1.6), (multiple males:1.6), (extra male:1.6)"
                )
            else:
                mob_positive = (
                    f"BREAK (single faceless male partner:1.15), (unseen eyes:1.15), "
                    f"(obscured male face:1.15), (male head out of frame:1.15), "
                    f"{rear_direction}(shirtless male:1.1), (bare shoulders:1.1), "
                    f"(solid black pants:1.2), muscular build"
                )
                mob_negative = (
                    f"{male_face_suppression}, (shading eyes:1.5), (hand on forehead:1.4), "
                    f"male clothes, male shirt, male t-shirt, male jacket, male suit, "
                    f"(naked male:1.2), (bottomless:1.2), (male underwear:1.2), jeans, blue pants, "
                    f"(mask, white mask, noh mask, blank face, pupil-less:1.3), "
                    f"(2boys:1.6), (multiple boys:1.6), (multiple males:1.6), (extra male:1.6)"
                )

            if mob_positive:
                full_prompt = f"{full_prompt} {mob_positive}"
            actual_negative = f"{actual_negative}, {mob_negative}"

            is_horizontal_scene = any(
                kw in p_low
                for kw in ("lying", "reclining", "on bed", "on back", "missionary")
            )
            if is_horizontal_scene:
                actual_negative = (
                    f"{actual_negative}, (spitroast, duplicate torso, extra legs at bottom, extra male lower body:1.4)"
                )

    if is_aftermath:
        actual_negative = (
            f"{actual_negative}, ((1boy, 2boys, male, masculine, partner, faceless male, multiple characters, extra face:1.5))"
        )
        if is_nude_scene:
            actual_negative = (
                f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, "
                f"rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, "
                f"crop top, halter, straps:1.35), (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"
            )

    if is_solo_scene:
        actual_negative = (
            f"{actual_negative}, ((1boy, 2boys, male, masculine, partner, faceless male, multiple characters, extra face:1.5))"
        )
        if is_nude_scene:
            actual_negative = (
                f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, "
                f"rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, "
                f"crop top, halter, straps:1.35), (wine, wine glass, champagne, glass, bottle, cup, drink, beverage:1.4)"
            )

    if "bath" in p_low:
        actual_negative = (
            f"{actual_negative}, ((marbles, glass marbles, balls, floating balls, spheres, orbs, "
            f"bath bombs, rubber duck, toys, floating objects, petals, bubbles, animal, cat, dog, pet, "
            f"fire, flame, flames, torch, white border, picture frame, "
            f"narrow bathtub, cramped tub, small plastic tub:1.45))"
        )

    if entry.required_outfit == "wedding":
        neg_parts = [t.strip() for t in actual_negative.split(",") if t.strip()]
        cleaned_neg_parts = [
            t for t in neg_parts if normalize_tag(t) not in WEDDING_CONFLICT_NEG
        ]
        actual_negative = ", ".join(cleaned_neg_parts)
        actual_negative = (
            f"{actual_negative}, ((cleavage, deep cleavage, exposed breasts, plunging neckline, "
            f"strapless, low-cut, bare breasts, nipples, lingerie, bikini, micro bikini, corset, "
            f"garter belt, garter straps, thighhighs, black stockings, cutout, chest cutout, "
            f"side slit, pelvic curtain, navel, midriff, short dress, micro skirt, mini skirt, "
            f"see-through breasts:1.4))"
        )

    # 샷 사이즈별 반대 구도 이탈 방지 네거티브 주입 (CompositionAntiDrift)
    actual_negative = apply_composition_anti_drift(pose_prompt, actual_negative)

    # 최종 정제: 모브 BREAK 블록을 격리한 상태에서 포지티브/네거티브 충돌 태그 자동 제거
    actual_negative, _removed_conflicts = remove_conflicting_tags_from_negative(
        full_prompt,
        actual_negative,
        isolate_mob_break=True,
    )

    # 가중치 클램핑은 기본 비활성(opt-in): 활성화 시 긍정(<=1.15)과 부정(<=1.25) 분리 클램핑
    if enable_clamp_weights:
        full_prompt, actual_negative = clamp_prompt_weights(
            full_prompt,
            actual_negative,
            pos_max=POS_MAX_WEIGHT,
            neg_max=NEG_MAX_WEIGHT,
            normalize_brackets=True,
        )
        full_prompt = re.sub(r"\(\s*,\s*", "(", full_prompt)
        full_prompt = re.sub(r"\s*,\s*", ", ", full_prompt).strip().strip(",")
        full_prompt = re.sub(r",?\s*BREAK\s*,?", " BREAK ", full_prompt).strip()
        actual_negative = re.sub(r"\(\s*,\s*", "(", actual_negative)
        actual_negative = re.sub(r",\s*,+", ", ", actual_negative)
        actual_negative = re.sub(r"\s*,\s*", ", ", actual_negative).strip().strip(",")

    controlnet_units: list[dict[str, Any]] = []
    if base_controlnet_unit is not None:
        unit = dict(base_controlnet_unit)
        is_theme_swap = bool(
            entry.required_outfit and entry.required_outfit not in ("default", "nude")
        )
        if (is_nude_scene or is_theme_swap) and unit.get("weight", 0) > 0.5:
            unit["weight"] = 0.5
        controlnet_units.append(unit)

    is_kiss_scene = "kiss" in p_low or entry.code in (60, 61, 198, 199, 19)

    return SceneComposition(
        pose_prompt=pose_prompt,
        apply_bg=apply_bg,
        bg_applied=bg_applied,
        active_base_pos=active_base_pos,
        active_char_prompt=active_char_prompt,
        full_prompt=full_prompt,
        actual_negative=actual_negative,
        actual_sampler=actual_sampler,
        actual_steps=actual_steps,
        actual_cfg=actual_cfg,
        actual_scheduler=actual_scheduler,
        actual_freeu=actual_freeu,
        actual_width=actual_width,
        actual_height=actual_height,
        is_clothed_scene=is_clothed_scene,
        is_nude_scene=is_nude_scene,
        is_aftermath=is_aftermath,
        is_offscreen_partner=is_offscreen_partner,
        is_solo_scene=is_solo_scene,
        is_interactive=is_interactive,
        is_kiss_scene=is_kiss_scene,
        controlnet_units=controlnet_units,
        applied_global_lora=applied_global_lora,
    )

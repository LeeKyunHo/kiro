"""
generator.scene_composer
단일 포즈 씬의 배경 적용, 의상 스왑/탈의 스트리핑, 2인 상호작용/솔로/사후여운/오프스크린 분기,
동적 포지티브·네거티브 변조, LoRA/Lightning 및 ControlNet 가중치 조정을 수행하는 순수 도메인 모듈.
(네트워크 및 파일 I/O가 전혀 없으며 dry-run과 실생성이 동일 로직을 공유한다.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

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


# ── Phase S7 4-Tier 모브 표준 (ADR-100/101/105) 및 고유 소품 (ADR-106) ──
_MOB_SHADOW_EYES_POS: str = (
    "(dark-haired male mob:1.15), (short black hair, shaded upper face, "
    "shadow over male eyes, eyes obscured by shadow, no visible male eyes:1.2)"
)
_MOB_EYELESS_BANGS_NEG: str = (
    "(visible male eyes, detailed male eyes, visible male pupils, detailed male face, "
    "male side profile, handsome male face, attractive male face, expressive male face, "
    "silver-haired male, blonde male:1.25)"
)

_PARTNER_POS_SPECS: dict[str, str] = {
    "above": "male hands pressing beside shoulders and gripping waist tightly, chest pressed close, partner shoulders at top edge, partner head out of frame",
    "below": "male hands gripping her waist and hips from below, intimate close embrace, partner head out of frame",
    "behind": "male torso tightly embracing from behind, male hands gripping her hips and waist firmly, lower body occluded, partner head out of frame",
    "side_kiss": "cropped male torso and shoulder at edge, partner head out of frame, partner arms around waist",
    "offscreen": "partner head out of frame, cropped male hands at frame edge",
    "carry": "carried in dark-haired male partner's arms, male torso and shoulders supporting her",
    "bury_chest": "back of dark-haired male head buried in her chest, male face completely obscured, partner arms around waist",
}

_MOB_TIER_SPECS: dict[int, dict[str, Any]] = {
    0: {
        "is_two_person": False,
        "is_offscreen_partner": True,
        "is_solo_scene": True,
        "is_interactive": False,
        "break_chunk": False,
        "positive_tags": "",
        "negative_tags": "1boy, male body, penis, disembodied limb",
    },
    1: {
        "is_two_person": True,
        "is_offscreen_partner": True,
        "is_solo_scene": False,
        "is_interactive": True,
        "break_chunk": False,
        "positive_tags": "(pov, disembodied male hands:1.05), cropped male arms, female focus",
        "negative_tags": "(detailed male face, male eyes, full male head:1.25), male torso, muscular chest, full male body, lower body focus, crotch focus",
    },
    2: {
        "is_two_person": True,
        "is_offscreen_partner": True,
        "is_solo_scene": False,
        "is_interactive": True,
        "break_chunk": False,
        "positive_tags": "",
        "negative_tags": "(detailed male face, male eyes, full male head:1.25), dark silhouette, heavy shadow, full body, wide shot, crotch focus",
    },
    3: {
        "is_two_person": True,
        "is_offscreen_partner": True,
        "is_solo_scene": False,
        "is_interactive": True,
        "break_chunk": True,
        "positive_tags": "BREAK (pov crotch:1.05), disembodied penis, (partially visible glans, compact shaft:1.05), cropped male lower torso",
        "negative_tags": "(giant penis, hyper penis, huge penis:1.25), full male body, (detailed male face, male eyes, full male head:1.25), muscular chest",
    },
}

_FACE_HAIR_ACCESSORY_KEYWORDS: tuple[str, ...] = (
    "glasses", "eyeglasses", "sunglasses", "monocle",
    "ribbon", "hair ribbon", "hairpin", "hair clip",
    "beret", "hat", "cap", "headband", "earring", "earrings", "hair ornament",
)

_ACCESSORY_BASE_NOUNS: tuple[str, ...] = _FACE_HAIR_ACCESSORY_KEYWORDS + (
    "necktie", "tie", "bowtie", "belt", "leather belt", "choker", "necklace",
    "gloves", "stockings", "thighhighs", "pantyhose",
)


def _strip_tags_preserving_parens(text: str, inner_pattern: str) -> str:
    """중첩/가중치 괄호 균형을 훼손하지 않고 일치하는 태그 토큰만 안전하게 제거한다."""
    cleaned = re.sub(inner_pattern, "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"(:\d+(?:\.\d+)?)\s*,\s*:\d+(?:\.\d+)?", r"\1", cleaned)
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(r"\(\s*,\s*", "(", cleaned)
        cleaned = re.sub(r",\s*(?=[:\)])", "", cleaned)
        cleaned = re.sub(r"\(\s*(?::[0-9\.]+)?\s*\)", "", cleaned)
    cleaned = re.sub(r"(,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\bBREAK\s*,\s*", "BREAK ", cleaned)
    cleaned = re.sub(r",\s*\bBREAK\b", " BREAK", cleaned)
    return cleaned.strip(" ,")


def _is_face_hair_accessory(tag: str) -> bool:
    """탈의(nude) 상태에서도 유지할 얼굴/헤어 소품인지 판별한다."""
    low = (tag or "").strip().lower()
    return any(kw in low for kw in _FACE_HAIR_ACCESSORY_KEYWORDS)


def _select_active_signature_accessories(
    accessories: Sequence[str] | None,
    is_nude: bool,
) -> list[str]:
    """현재 슬롯(착의/탈의)에 맞춰 활성화할 캐릭터 고유 소품 목록을 반환한다."""
    if not accessories:
        return []
    items = [str(item).strip() for item in accessories if str(item).strip()]
    if is_nude:
        return [item for item in items if _is_face_hair_accessory(item)]
    return items


def _inject_signature_accessories(
    base_pos: str,
    accessories: Sequence[str] | None,
    is_nude: bool = False,
) -> str:
    """signature_accessories를 캐릭터 외형 청크 선두(앵커 태그 직후)에 중복 없이 우선 배치한다."""
    active = _select_active_signature_accessories(accessories, is_nude)
    if not active:
        return base_pos

    if " BREAK " in base_pos:
        head, char_part = base_pos.split(" BREAK ", 1)
    else:
        head, char_part = "", base_pos

    cleaned_char = char_part
    formatted_tags: list[str] = []
    for acc in active:
        core = normalize_tag(acc)
        if not core:
            continue
        cleaned_char = _strip_tags_preserving_parens(
            cleaned_char,
            rf"\(?{re.escape(core)}(?::[0-9\.]+)?\)?",
        )
        formatted_tags.append(acc if ":" in acc else f"({core}:1.10)")

    if not formatted_tags:
        return base_pos

    char_tokens = [t.strip() for t in cleaned_char.split(",") if t.strip()]
    anchor_kws = ("1girl", "1boy", "solo", "androgynous", "otokonoko", "femboy")
    insert_idx = 0
    for idx, tok in enumerate(char_tokens[:4]):
        tok_low = normalize_tag(tok)
        if any(k in tok_low for k in anchor_kws):
            insert_idx = idx + 1
    new_char_tokens = char_tokens[:insert_idx] + formatted_tags + char_tokens[insert_idx:]
    new_char_part = ", ".join(new_char_tokens)
    return f"{head} BREAK {new_char_part}" if head else new_char_part


def _strip_accessory_negative_conflicts(
    neg: str,
    accessories: Sequence[str] | None,
) -> str:
    """활성화된 signature_accessories와 충돌하는 네거티브 전체구문 및 핵심 소품 명사를 함께 제거한다."""
    if not accessories or not neg:
        return neg
    patterns: list[str] = []
    for sig_acc in accessories:
        acc_core = normalize_tag(sig_acc)
        if not acc_core:
            continue
        patterns.append(re.escape(acc_core))
        for noun in _ACCESSORY_BASE_NOUNS:
            if re.search(rf"\b{re.escape(noun)}\b", acc_core):
                patterns.append(re.escape(noun))
    if not patterns:
        return neg
    combined = r"\b(?:" + "|".join(sorted(set(patterns), key=len, reverse=True)) + r")\b(?::[0-9\.]+)?"
    return _strip_tags_preserving_parens(neg, combined)


def _inject_partner_pos_inline(
    pos: str,
    partner_pos: str | None,
    pose_tag: str = "",
) -> str:
    """2인 구도 시 첫 청크 후미(BREAK 앞)에 방향성 파트너 엣지 크롭 태그를 인라인 주입한다."""
    pose_low = (pose_tag or "").lower()
    if partner_pos == "side_kiss" and any(kw in pose_low for kw in ("kiss", "french kiss")):
        tags = "cropped dark-haired male jaw and lips at extreme frame edge, male eyes out of frame, partner arms around waist"
    else:
        tags = _PARTNER_POS_SPECS.get(partner_pos or "", "partner head out of frame, cropped male shoulders at frame edge")
    if not tags:
        return pos
    if " BREAK " in pos:
        head, tail = pos.split(" BREAK ", 1)
        return f"{head}, {tags} BREAK {tail}"
    return f"{pos}, {tags}"


def _resolve_mob_tier(entry: PoseEntry) -> int | None:
    """PoseEntry.mob_tier 명시값(0~3)을 검증해 반환한다. 미지정 시 None(기존 V2 동작 유지)."""
    raw_tier = getattr(entry, "mob_tier", None)
    if raw_tier in _MOB_TIER_SPECS:
        return int(raw_tier)
    return None


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
    mob_tier: int | None = None
    partner_pos: str | None = None


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
    signature_accessories: Sequence[str] | None = None,
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

    # ADR-106: 캐릭터 고유 소품(signature_accessories)을 외형 전단에 중복 없이 우선 배치
    active_accessories = _select_active_signature_accessories(signature_accessories, is_nude_scene)
    if active_accessories:
        if active_char_prompt:
            active_char_prompt = _inject_signature_accessories(
                active_char_prompt, active_accessories, is_nude=is_nude_scene
            )
        elif active_base_pos:
            active_base_pos = _inject_signature_accessories(
                active_base_pos, active_accessories, is_nude=is_nude_scene
            )

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
    resolved_mob_tier = _resolve_mob_tier(entry)
    resolved_partner_pos = entry.partner_pos
    is_otokonoko_char = (
        (gender or "").strip().lower() in ("otokonoko", "femboy")
        or entry.section in ("scenes_otokonoko", "special_otokonoko")
        or "otokonoko" in active_base_pos.lower()
        or "otokonoko" in active_char_prompt.lower()
    )

    if resolved_mob_tier in _MOB_TIER_SPECS:
        tier_spec = _MOB_TIER_SPECS[resolved_mob_tier]
        is_offscreen_partner = bool(tier_spec["is_offscreen_partner"])
        is_solo_scene = bool(tier_spec["is_solo_scene"])
        is_interactive = bool(tier_spec["is_interactive"]) and not is_aftermath

        if resolved_mob_tier == 0:
            tier0_neg = (
                "1boy, male body, disembodied limb"
                if is_otokonoko_char
                else str(tier_spec["negative_tags"])
            )
            actual_negative = f"{actual_negative}, {tier0_neg}"
        elif is_interactive:
            neg_tags = [t.strip() for t in actual_negative.split(",") if t.strip()]
            actual_negative = ", ".join(
                t for t in neg_tags if normalize_tag(t) not in MALE_NEG_TOKENS
            )
            for solo_pat in (", solo", "solo,", " solo "):
                full_prompt = full_prompt.replace(solo_pat, " ")
            full_prompt = " ".join(full_prompt.split())

            tier_neg = str(tier_spec["negative_tags"])
            if resolved_mob_tier == 2 and resolved_partner_pos == "carry":
                tier_neg = _strip_tags_preserving_parens(tier_neg, r"\bfull male head\b")
            actual_negative = f"{actual_negative}, {tier_neg}, {_MOB_EYELESS_BANGS_NEG}"

            if is_nude_scene:
                actual_negative = (
                    f"{actual_negative}, (clothes, clothing, dress, sleeves, collar, cuffs, fabric, "
                    f"rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, "
                    f"crop top, halter, straps:1.35)"
                )

            if resolved_mob_tier == 1:
                if tier_spec["positive_tags"]:
                    full_prompt = f"{full_prompt}, {tier_spec['positive_tags']}"
                if resolved_partner_pos and resolved_partner_pos != "offscreen":
                    full_prompt = _inject_partner_pos_inline(
                        full_prompt, resolved_partner_pos, pose_prompt
                    )
            elif resolved_mob_tier == 2:
                full_prompt = _inject_partner_pos_inline(
                    full_prompt, resolved_partner_pos, pose_prompt
                )
                if resolved_partner_pos == "carry":
                    full_prompt = re.sub(
                        r",?\s*\bpartner head out of frame\b",
                        "",
                        full_prompt,
                        flags=re.IGNORECASE,
                    )
                if resolved_partner_pos != "bury_chest":
                    has_shirtless_male = bool(
                        re.search(r"\bshirtless male\b", full_prompt, flags=re.IGNORECASE)
                    )
                    if has_shirtless_male and not is_nude_scene:
                        full_prompt = _strip_tags_preserving_parens(
                            full_prompt, r"\bshirtless male\b"
                        )
                    if "closed eyes" not in p_low and not is_otokonoko_char:
                        full_prompt = f"{full_prompt}, (clear visible female eyes:1.15)"
                    mob_tail = (
                        f"{_MOB_SHADOW_EYES_POS}, shirtless male"
                        if (has_shirtless_male and not is_nude_scene)
                        else _MOB_SHADOW_EYES_POS
                    )
                    full_prompt = f"{full_prompt} BREAK (tall dark-haired male mob:1.15), {mob_tail}"
            elif resolved_mob_tier == 3:
                if resolved_partner_pos:
                    full_prompt = _inject_partner_pos_inline(
                        full_prompt, resolved_partner_pos, pose_prompt
                    )
                if tier_spec["positive_tags"]:
                    full_prompt = f"{full_prompt} {tier_spec['positive_tags']}"
                if str(entry.code).strip().zfill(3) in ("126", "127") or any(
                    kw in p_low for kw in ("under desk", "under table", "책상밑")
                ):
                    full_prompt = (
                        f"{full_prompt}, ((clothed male partner:1.20)), "
                        f"((wearing collared shirt, white shirt:1.20))"
                    )
                    actual_negative = (
                        f"{actual_negative}, (shirtless male:1.35), (naked male:1.35), "
                        f"(bare chest:1.35), (bare shoulders:1.35)"
                    )
    else:
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

    if resolved_mob_tier is None and is_interactive:
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

    # ADR-106: 활성화된 캐릭터 고유 소품과 충돌하는 네거티브 태그 자동 해제
    if active_accessories:
        actual_negative = _strip_accessory_negative_conflicts(actual_negative, active_accessories)

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
        mob_tier=resolved_mob_tier,
        partner_pos=resolved_partner_pos,
    )

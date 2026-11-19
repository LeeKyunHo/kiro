"""
generator.prompt
프롬프트 조립, 태그 정규화/충돌 검출·제거, 표정 조화, 의상 스트리핑·테마 폴백,
가중치 분리 클램핑, 배경 프롬프트 해석 및 입력 검증.
"""

from __future__ import annotations

import json
import logging
import re
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)

from generator.config import (
    _BRACKET_TABLE,
    REF_WEIGHT_MAX,
    REF_WEIGHT_MIN,
    SAFE_PREFIX_PATTERN,
    WEIGHT_SUFFIX_PATTERN,
    ConfigError,
)

POS_MAX_WEIGHT: float = 1.15
NEG_MAX_WEIGHT: float = 1.25

# 파이프라인이 구도/시선/인원수 제어를 위해 의도적으로 제어하는 보호 태그 (자동 충돌 제거 제외)
_PROTECTED_CONTROL_TAGS: frozenset[str] = frozenset({
    "1boy",
    "2boys",
    "1girl",
    "2girls",
    "male",
    "masculine",
    "solo",
    "looking at viewer",
    "looking straight at camera",
})

# 남성 모브 전용 BREAK 블록 (충돌 검사 시 여주인공 본체 네거티브 보호를 위해 격리)
_MOB_BREAK_PATTERN = re.compile(
    r"\bBREAK\s*\((?:(?:single|multiple)\s+)?(?:faceless(?:\s+shadowed)?|cropped)\s+male.*$"
    r"|\bBREAK\s*\(pov\s+crotch.*$"
    r"|\bBREAK\s*\(tall\s+dark-haired\s+male\s+mob.*$",
    flags=re.IGNORECASE,
)

# 공용 테마 의상 폴백 DB (캐릭터 JSON의 outfits에 해당 슬롯이 없을 때 안전 폴백)
DEFAULT_THEME_OUTFITS: dict[str, str] = {
    "swimsuit": "micro bikini, halterneck bikini top, side-tie bikini bottom",
    "bunny": "black glossy bunny suit, fishnet pantyhose, bunny ears, white collar, bow tie",
    "maid": "classic maid outfit, black maid dress, white frilled apron, maid headdress, white thighhighs",
    "bdsm": "black leather strap harness, black choker, leather arm bands",
    "combat": "tactical battle attire, torn clothes, fingerless gloves, combat boots",
    "gym": "bloomers, gym uniform, white t-shirt",
    "school": "school uniform, sailor collar, pleated skirt",
    "underwear": "white lace bra, matching white lace panties",
    "wedding": "pure white bridal wedding gown, sheer bridal veil, floral lace bridal bodice, white opera gloves",
}

_EXPLICIT_POSE_EXPRESSION_KEYWORDS: tuple[str, ...] = (
    "smile", "smiling", "laugh", "grin", "happy", "blush", "flushed",
    "crying", "tears", "ecstasy", "screaming", "angry", "moaning",
    "pleasure", "winking", "embarrassed", "pout", "flustered", "intoxicated",
    "shock", "surprised", "panic", "comic", "wide eyes", "constricted pupils", "blank stare",
    "sad", "frown", "scowl", "smirk", "ahegao", "shy", "nervous",
)

_NON_SMILE_EXPRESSION_KEYWORDS: tuple[str, ...] = (
    "angry", "crying", "tears", "sad", "frown", "scowl",
    "shock", "surprised", "panic", "embarrassed", "pout", "flustered",
    "moaning", "ecstasy", "screaming", "blank stare", "expressionless",
)

_SMILE_EXPRESSION_KEYWORDS: tuple[str, ...] = (
    "smile", "smiling", "laugh", "grin", "happy", "smirk",
)

_NEUTRAL_CHAR_EXPRESSION_PATTERN = (
    r"\b(?:cool calm expression|stoic expression|calm expression|expressionless|"
    r"emotionless|indifferent gaze|neutral expression|stoic)\b(?::[0-9\.]+)?"
)

_SMILE_CHAR_EXPRESSION_PATTERN = (
    r"\b(?:gentle smile|soft smile|warm smile|serene smile|calm smile|slight smile|smile)\b(?::[0-9\.]+)?"
)


def clamp_weights(
    prompt_text: str,
    max_weight: float = POS_MAX_WEIGHT,
    normalize_brackets: bool = False,
) -> str:
    """
    SDXL/Illustrious CLIP 인코더 최적화:
    1. 지정된 max_weight 를 초과하는 과도한 가중치를 안전 한계치로 클램핑하여 색상 과포화/번아웃 방지.
    2. normalize_brackets=True 시 다중 괄호((...)) 누적으로 인한 비정상 가중치 증폭을 단일화.
    """
    if not prompt_text:
        return ""

    def _clamp_match(m: re.Match[str]) -> str:
        w = float(m.group(1))
        return f":{min(w, max_weight):.2f}"

    text = re.sub(r":([0-9]+\.[0-9]+)", _clamp_match, prompt_text)
    if normalize_brackets:
        text = re.sub(r"\({2,}", "(", text)
        text = re.sub(r"\){2,}", ")", text)
    return text


def clamp_prompt_weights(
    positive: str,
    negative: str,
    *,
    pos_max: float = POS_MAX_WEIGHT,
    neg_max: float = NEG_MAX_WEIGHT,
    normalize_brackets: bool = True,
) -> tuple[str, str]:
    """긍정(기본 1.15)과 부정(기본 1.25) 프롬프트 가중치를 분리 상한으로 클램핑한다."""
    pos_clamped = clamp_weights(positive, max_weight=pos_max, normalize_brackets=normalize_brackets)
    neg_clamped = clamp_weights(negative, max_weight=neg_max, normalize_brackets=normalize_brackets)
    return pos_clamped, neg_clamped


def normalize_tag(raw: str) -> str:
    """
    비교 가능한 형태로 태그를 정규화한다.
    괄호 강조와 가중치 표기를 제거하고 소문자·단일 공백으로 맞춘다.
    """
    tag = raw.strip().lower().translate(_BRACKET_TABLE)
    tag = WEIGHT_SUFFIX_PATTERN.sub("", tag)
    return " ".join(tag.split())


def split_tags(text: str) -> list[str]:
    """쉼표 및 BREAK 구분자로 분리해 정규화한 태그 목록. 빈 토큰은 버린다."""
    cleaned = re.sub(r"\bBREAK\b", ",", text)
    return [tag for tag in map(normalize_tag, cleaned.split(",")) if tag]


def find_tag_conflicts(positive: str, negative: str) -> list[str]:
    """포지티브와 네거티브에 동시에 존재하는 태그를 찾는다."""
    return sorted(set(split_tags(positive)) & set(split_tags(negative)))


def _strip_tags_preserving_parens(text: str, inner_pattern: str) -> str:
    """중첩/가중치 괄호 균형을 훼손하지 않고 일치하는 태그 토큰만 안전하게 제거한다."""
    if not text:
        return ""
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


def remove_conflicting_tags_from_negative(
    positive: str,
    negative: str,
    *,
    isolate_mob_break: bool = True,
) -> tuple[str, list[str]]:
    """
    긍정과 부정 사이에 충돌하는 태그를 감지하고, 네거티브 프롬프트에서 해당 태그를 안전하게 제거한다.
    다중 태그 괄호 그룹(((tag1, tag2:1.5))) 내부 조각도 괄호 쌍과 그룹 가중치를 보존하면서 정제한다.
    - isolate_mob_break=True: 모브 전용 BREAK 블록의 태그가 여주인공 본체 네거티브를 제거하지 않도록 격리.
    - _PROTECTED_CONTROL_TAGS: 구도/인원수 제어용 보호 태그는 가드 무력화 방지를 위해 보존.
    """
    if not positive or not negative:
        return negative, []

    effective_pos = _MOB_BREAK_PATTERN.sub("", positive) if isolate_mob_break else positive
    conflicts = [
        t for t in find_tag_conflicts(effective_pos, negative)
        if t not in _PROTECTED_CONTROL_TAGS
    ]
    if not conflicts:
        return negative, []

    conflict_set = set(conflicts)
    neg_chunks = [c.strip() for c in negative.split(",") if c.strip()]
    kept_chunks: list[str] = []
    removed_tags: list[str] = []
    pending_open_parens = ""

    for chunk in neg_chunks:
        norm = normalize_tag(chunk)
        open_parens = chunk.count("(")
        close_parens = chunk.count(")")

        if norm in conflict_set:
            removed_tags.append(norm)
            if open_parens > close_parens:
                pending_open_parens += "(" * (open_parens - close_parens)
            elif close_parens > open_parens:
                needed_close = ")" * (close_parens - open_parens)
                weight_suffix = ""
                w_match = re.search(r"(:[0-9]+(?:\.[0-9]+)?)\s*\)+$", chunk)
                if w_match:
                    weight_suffix = w_match.group(1)
                if pending_open_parens:
                    cancel_count = min(len(pending_open_parens), len(needed_close))
                    pending_open_parens = pending_open_parens[:-cancel_count] if cancel_count else ""
                    needed_close = needed_close[cancel_count:]
                if needed_close and kept_chunks:
                    open_balance = sum(c.count("(") - c.count(")") for c in kept_chunks)
                    if open_balance > 0:
                        close_slice = needed_close[:open_balance]
                        if weight_suffix and not re.search(r":[0-9]+(?:\.[0-9]+)?\)*$", kept_chunks[-1]):
                            kept_chunks[-1] = kept_chunks[-1] + weight_suffix + close_slice
                        else:
                            kept_chunks[-1] = kept_chunks[-1] + close_slice
            continue

        if pending_open_parens:
            chunk = pending_open_parens + chunk
            pending_open_parens = ""

        kept_chunks.append(chunk)

    new_neg = ", ".join(kept_chunks)
    return new_neg, sorted(set(removed_tags))


def harmonize_character_expression(char_part: str, pose_tag: str) -> str:
    """
    포즈에 구체적인 표정/감정 태그가 있을 경우, 캐릭터 기본 외형 정의의
    상반된 고정 표정(무표정/stoic, 또는 비미소 감정 씬의 기본 smile)을 억제하여 표정 충돌을 방지한다.
    """
    if not char_part or not pose_tag:
        return char_part

    pose_low = pose_tag.lower()
    has_explicit_pose_expression = any(
        kw in pose_low for kw in _EXPLICIT_POSE_EXPRESSION_KEYWORDS
    )
    if not has_explicit_pose_expression:
        return char_part

    result = _strip_tags_preserving_parens(char_part, _NEUTRAL_CHAR_EXPRESSION_PATTERN)

    has_non_smile = any(kw in pose_low for kw in _NON_SMILE_EXPRESSION_KEYWORDS)
    has_smile = any(kw in pose_low for kw in _SMILE_EXPRESSION_KEYWORDS)
    if has_non_smile and not has_smile:
        result = _strip_tags_preserving_parens(result, _SMILE_CHAR_EXPRESSION_PATTERN)

    return result


def join_tags(*parts: str) -> str:
    """빈 조각을 건너뛰고 쉼표로 이어붙인다."""
    return ", ".join(part.strip() for part in parts if part and part.strip())


def resolve_theme_outfit(outfits: dict[str, str] | None, slot: str) -> str:
    """캐릭터 맞춤 의상 슬롯을 조회하고, 없으면 공용 테마 의상(DEFAULT_THEME_OUTFITS)으로 폴백한다."""
    clean_slot = (slot or "").strip().lower()
    if clean_slot in ("", "default", "nude"):
        return ""
    if outfits and clean_slot in outfits and str(outfits[clean_slot]).strip():
        return str(outfits[clean_slot]).strip()
    return DEFAULT_THEME_OUTFITS.get(clean_slot, "")


_OUTFIT_KEYWORDS: frozenset[str] = frozenset({
    "dress", "skirt", "bodycon", "knit", "high-neck", "turtleneck", "sleeves", "sleeved", "sleeveless",
    "cutout", "shirt", "blouse", "pants", "jeans", "trousers", "slacks", "jacket", "coat", "sweater", "cardigan",
    "blazer", "vest", "waistcoat", "overcoat", "outerwear", "miniskirt", "mini-skirt",
    "uniform", "suit", "collar", "cuffs", "tie", "necktie", "bowtie", "necklace", "pendant", "choker",
    "bracelet", "gloves", "socks", "stockings", "pantyhose", "shoes", "boots", "heels",
    "bra", "panties", "underwear", "swimwear", "bikini", "swimsuit", "leotard", "one-piece",
    "apron", "shorts", "robe", "robes", "hanfu", "kimono", "hoodie", "top", "camisole", "bracers", "scabbard",
    "underboob", "underbust", "corset", "bodice", "bustier", "strap", "straps", "suspender", "suspenders",
    "garter", "garters", "belt", "buckle", "sash", "shawl", "fabric", "slit", "slits", "wrap", "wristwatch", "watch",
    "neckline", "scoop", "plunging", "v-neck", "halter-neck", "halterneck", "off-shoulder", "strapless", "backless",
    "slipping", "clinging", "contouring", "leather", "denim", "lace", "silk", "satin", "velvet", "cashmere",
    "headband", "headdress", "maid", "pinafore", "capelet", "cape", "petticoat", "brooch", "cross",
    "bandage", "bandages", "lanyard", "badge", "holster", "syringe", "holder", "folder", "bolero",
    "bunny", "tights", "qipao", "cheongsam", "bodysuit", "fishnet", "fishnets", "stiletto",
    "lingerie", "curtain", "cleavage", "latex", "harness", "pasties", "thong", "legwear",
})

_OUTFIT_PHRASES: tuple[str, ...] = (
    "through dress", "contouring dress", "through clothes", "underboob", "underbust",
    "side slit", "thigh slit", "off shoulder", "off-shoulder", "over shoulder",
    "slipping off", "snug fabric", "clinging tightly", "waist fit", "snug fit",
    "cashmere shawl", "leather belt", "black strap", "white strap", "silk strap",
    "metallic strap", "thin strap", "open back", "bare shoulders", "no jewelry",
    "maid headband", "maid cap", "maid dress", "ruffled apron", "frilled apron",
    "lace choker", "ribbon choker", "cross pendant", "lace cuffs", "puffy sleeves",
    "sweetheart neckline", "scoop neckline", "high-neck", "corset bodice",
)

_HAIR_KEYWORDS: tuple[str, ...] = (
    "hair", "ponytail", "bun", "bangs", "strands", "sidelocks",
    "twintails", "braid", "updo", "ahoge", "curls",
)


def strip_outfit_tags(prompt_text: str) -> str:
    """
    캐릭터 프롬프트에서 헤어/체형/얼굴 태그는 보존하고 의상 및 착용 액세서리 태그를 제거한다.
    완전 탈의(H-씬)에서 목이나 팔 등에 의상 파편(하이넥, 롱슬리브, 벨트, 숄, 언더붑 등)이 잔류하는 현상을 방지한다.
    """
    if not prompt_text:
        return ""

    chunks = []
    parts = prompt_text.split(" BREAK ")
    for part in parts:
        tags = [t.strip() for t in part.split(",") if t.strip()]
        cleaned_tags = []
        for t in tags:
            clean = t.lower().replace("(", "").replace(")", "").split(":")[0].strip()
            is_hair = any(h in clean for h in _HAIR_KEYWORDS)
            words = clean.split()
            is_outfit = not is_hair and any(w in _OUTFIT_KEYWORDS for w in words)
            if not is_outfit and not is_hair and any(kw in clean for kw in _OUTFIT_PHRASES):
                is_outfit = True

            if not is_outfit:
                cleaned_tags.append(t)
        chunks.append(", ".join(cleaned_tags))

    return " BREAK ".join(chunks)


def assemble_prompt(
    base_positive: str,
    char_prompt: str,
    pose_prompt: str,
    trigger_tag: str = "",
) -> str:
    """
    포즈와 캐릭터 프롬프트를 최적의 CLIP 순서로 결합한다.
    base_positive 에 ' BREAK '가 포함되어 있으면:
      [품질 태그], [포즈 태그] BREAK [캐릭터 외형 태그], [트리거 태그]
    순으로 조립하여 포즈/구도가 긴 외형 묘사에 밀려 후순위 청크로 넘어가는 것을 방지한다.
    ' BREAK '가 없으면 기존 방식(base_positive, char_prompt, pose_prompt, trigger_tag)을 유지한다.
    """
    if " BREAK " in base_positive:
        quality_part, char_part = base_positive.split(" BREAK ", 1)
        first_chunk = join_tags(quality_part, pose_prompt)
        second_chunk = join_tags(char_part, char_prompt, trigger_tag)
        return f"{first_chunk} BREAK {second_chunk}"
    return join_tags(base_positive, char_prompt, pose_prompt, trigger_tag)


def resolve_background(
    cli_bg: str | None,
    bg_file: Path,
    preset: str = "default",
) -> str | None:
    """
    감정(emotions) 씬에 적용할 배경 프롬프트를 해석한다.

    1. CLI 또는 캐릭터 JSON에서 전달된 cli_bg 가 있으면 최우선 반환.
    2. bg_file (projects/{roster}/background.json) 이 존재하면:
       JSON 내에서 preset(기본 "default")에 해당하는 문자열 반환.
    3. 일치하는 프리셋이 없거나 파일이 없으면 None 반환 (기존 clean background 유지).
    """
    if cli_bg and cli_bg.strip():
        return cli_bg.strip()

    if bg_file.exists():
        try:
            with open(bg_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                bg_val = data.get(preset)
                if isinstance(bg_val, str) and bg_val.strip():
                    return bg_val.strip()
                if preset != "default" and "default" in data:
                    logger.warning(
                        f"[WARN] 배경 프리셋 '{preset}' 이(가) 없어 'default' 프리셋으로 폴백합니다."
                    )
                    default_val = data.get("default")
                    if isinstance(default_val, str) and default_val.strip():
                        return default_val.strip()
        except Exception as e:
            logger.warning(f"[WARN] 배경 설정 파일 로드 실패 ({bg_file}): {e}")

    return None


def validate_ref_weight(value: float) -> float:
    """IP-Adapter 적용 강도를 검증한다."""
    if not REF_WEIGHT_MIN <= value <= REF_WEIGHT_MAX:
        raise ConfigError(
            f"--ref_weight 는 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} 범위여야 합니다: {value}",
            "0.5~0.8 이 실무 범위입니다. 1.0 이상은 참조 이미지의 포즈까지 전이됩니다.",
        )
    return value


def validate_prefix(prefix: str) -> str:
    """prefix 를 안전한 단일 경로 세그먼트로 제한한다."""
    candidate = prefix.strip()
    if not SAFE_PREFIX_PATTERN.match(candidate):
        raise ConfigError(
            f"prefix '{prefix}' 를 사용할 수 없습니다.",
            "영문·숫자·밑줄·하이픈 1~64자만 허용합니다. (예: mika, test_01)",
        )
    return candidate


def compute_deterministic_pose_seed(pose_code: str | int, char_prefix: str) -> int:
    """포즈 코드 및 캐릭터 식별자 기반의 프로세스 독립적 결정론적 시드(CRC32)를 산출한다."""
    code_digits = re.sub(r"\D", "", str(pose_code or "").strip())
    numeric_code = (
        int(code_digits)
        if code_digits
        else (zlib.crc32(str(pose_code or "").encode("utf-8")) % 1000)
    )
    char_offset = zlib.crc32(str(char_prefix or "").encode("utf-8")) % 100000
    return numeric_code * 1000 + char_offset

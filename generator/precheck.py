"""
generator.precheck
이미지 배치 생산 전 캐릭터 JSON, 포즈 DB 제작 규칙, 배경 매핑 정합성 및
전 포즈 조립 시뮬레이션(착의 고정 / 탈의 의상 누수 / 테마 의상 스왑)을 전수 검증하는 모듈.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from generator.config import COMMON_NEG, POS_BASE
from generator.models import CharacterConfig, PoseDatabase, RosterPaths
from generator.pose_db import expand_v2_mode, load_pose_db, resolve_targets
from generator.prompt import normalize_tag, resolve_theme_outfit, strip_outfit_tags
from generator.roster import load_character
from generator.scene_composer import compose_scene_prompt


@dataclass
class PrecheckReport:
    roster_name: str
    total_characters: int = 0
    total_poses: int = 0
    total_combinations_checked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


_SWEAT_LIQUID_PATTERN = re.compile(
    r"\b(?:sweat|sweating|sweaty|liquid|liquids|bodily fluids?|body fluids?)\b"
)
_FORBIDDEN_POSE_TAGS = (
    "long slender limbs",
    "introduction pose",
    "blank face",
    "featureless face",
)

# 탈의(nude) 씬의 캐릭터 본체 파트에 잔류하면 안 되는 주요 의상 키워드 (범용)
_NUDE_OUTFIT_LEAK_TOKENS: tuple[str, ...] = (
    "school uniform",
    "nurse uniform",
    "doctor coat",
    "lab coat",
    "trench coat",
    "cropped jacket",
    "leather jacket",
    "blazer",
    "pleated skirt",
    "pencil skirt",
    "miniskirt",
    "qipao",
    "cheongsam",
    "hoodie",
    "cardigan",
    "business suit",
    "wedding dress",
    "evening gown",
    "necktie",
    "trousers",
    "jeans",
)

_EXCLUDED_SIGNATURE_FRAGMENTS: tuple[str, ...] = (
    "unbuttoned",
    "open shirt",
    "clothes open",
    "garter",
    "thighhigh",
    "stocking",
    "pantyhose",
    "panties",
    "underwear",
)


def _check_positive_rules(text: str, scope_label: str) -> list[str]:
    """포즈/외형 포지티브 프롬프트의 구강 왜곡 및 금지 태그를 검사한다."""
    errors: list[str] = []
    low = text.lower()
    if "parted lips" in low and "puckered lips" in low:
        errors.append(f"{scope_label} 구강 왜곡 유발 동시 사용 (parted lips + puckered lips)")
    for bad in _FORBIDDEN_POSE_TAGS:
        if bad in low:
            errors.append(f"{scope_label} 금지/결함 태그 포함: '{bad}'")
    return errors


def audit_pose_db(db: PoseDatabase) -> list[str]:
    """포즈 DB 무결성 및 V2 라벨/프롬프트/의상 슬롯 규칙 점검."""
    errors = []
    for code, entry in db.entries.items():
        label = entry.label
        # 1. 라벨 규격 검사 (슬래시 금지, 1~20자)
        if "/" in label:
            errors.append(f"포즈 {code} 라벨 슬래시 금지 위반: '{label}'")
        if not (1 <= len(label) <= 20):
            errors.append(f"포즈 {code} 라벨 길이 비정상: '{label}' ({len(label)}자)")

        # 2. 기본 + V2 성별 분기 프롬프트(prompt, prompt_female, prompt_otokonoko) 검사
        for field_name, prompt_val in (
            ("prompt", entry.prompt),
            ("prompt_female", entry.prompt_female),
            ("prompt_otokonoko", entry.prompt_otokonoko),
        ):
            if prompt_val and prompt_val.strip():
                scope = f"포즈 {code}" if field_name == "prompt" else f"포즈 {code} ({field_name})"
                errors.extend(_check_positive_rules(prompt_val, scope))

        # 3. negative_add 내 금지된 sweat/liquid 계열 태그 혼입 점검
        if entry.negative_add and entry.negative_add.strip():
            m = _SWEAT_LIQUID_PATTERN.search(entry.negative_add.lower())
            if m:
                errors.append(
                    f"포즈 {code} negative_add 금지 태그 포함: '{m.group(0)}'"
                )

        # 4. required_outfit 단일 슬롯 키 형식 검증
        if entry.required_outfit and re.search(r"[,\s]", entry.required_outfit.strip()):
            errors.append(
                f"포즈 {code} required_outfit 형식 비정상 (단일 키만 허용): '{entry.required_outfit}'"
            )
    return errors


def audit_character_safety(char_data: dict, name: str) -> list[str]:
    """캐릭터 JSON 외형, 네거티브 금지 태그(sweat/liquid) 및 outfits 스키마 점검."""
    if not isinstance(char_data, dict):
        return [f"캐릭터 '{name}' JSON 최상위가 딕셔너리가 아님"]

    errors = []
    pfx = char_data.get("prefix", name)
    pos = (char_data.get("char_prompt") or char_data.get("positive") or char_data.get("sdxl_positive") or "").lower()
    nude = (char_data.get("nude_positive") or char_data.get("sdxl_nude_positive") or "").lower()

    for target_text, field_name in [(pos, "외형 프롬프트"), (nude, "탈의 프롬프트")]:
        if "long slender limbs" in target_text:
            errors.append(f"캐릭터 '{pfx}' {field_name}에 기형 유발 태그 'long slender limbs' 잔류")

    for neg_key in ("negative", "sdxl_negative", "custom_neg"):
        raw_neg = char_data.get(neg_key)
        if isinstance(raw_neg, str) and raw_neg.strip():
            m = _SWEAT_LIQUID_PATTERN.search(raw_neg.lower())
            if m:
                errors.append(
                    f"캐릭터 '{pfx}' {neg_key}에 금지 태그 '{m.group(0)}' 포함"
                )

    if "outfits" in char_data:
        raw_outfits = char_data["outfits"]
        if not isinstance(raw_outfits, dict):
            errors.append(f"캐릭터 '{pfx}' outfits 필드가 딕셔너리가 아님")
        else:
            for o_key, o_val in raw_outfits.items():
                if not isinstance(o_key, str) or not isinstance(o_val, str):
                    errors.append(f"캐릭터 '{pfx}' outfits['{o_key}'] 값이 문자열이 아님")

    return errors


def _extract_signature_outfit_tokens(clothed_prompt: str) -> tuple[str, ...]:
    """캐릭터 착의 프롬프트에서 strip_outfit_tags 차분으로 대표 의상 토큰(최대 3개)을 자동 추출한다."""
    if not clothed_prompt or not clothed_prompt.strip():
        return ()
    body = clothed_prompt.split(" BREAK ", 1)[1] if " BREAK " in clothed_prompt else clothed_prompt
    stripped_body = strip_outfit_tags(body)
    kept_tags = {normalize_tag(t) for t in stripped_body.split(",") if normalize_tag(t)}

    extracted: list[str] = []
    for raw_t in body.split(","):
        norm_t = normalize_tag(raw_t)
        if (
            len(norm_t) > 3
            and norm_t not in kept_tags
            and not any(frag in norm_t for frag in _EXCLUDED_SIGNATURE_FRAGMENTS)
        ):
            extracted.append(norm_t)
    return tuple(extracted[:3])


def _extract_valid_bg_keys(bg_data: Any) -> set[str]:
    """플랫 background.json 및 선택적 presets 딕셔너리에서 유효한 프리셋 키 집합을 추출한다."""
    if not isinstance(bg_data, dict):
        return set()
    valid = {
        str(k).strip()
        for k, v in bg_data.items()
        if not str(k).startswith("_") and isinstance(v, str) and v.strip()
    }
    presets = bg_data.get("presets")
    if isinstance(presets, dict):
        valid.update(
            str(k).strip()
            for k in presets.keys()
            if not str(k).startswith("_")
        )
    return valid


def audit_pose_background_mapping(
    roster: RosterPaths,
    bg_data: Any = None,
) -> tuple[list[str], list[str]]:
    """로스터의 pose_background_mapping.json 매핑 키가 background.json 에 실존하는지 검증한다."""
    errors: list[str] = []
    warnings: list[str] = []
    mapping_file = roster.assets_dir.parent / "pose_background_mapping.json"
    if not mapping_file.is_file():
        return errors, warnings

    try:
        map_data = json.loads(mapping_file.read_text(encoding="utf-8-sig"))
    except Exception as e:
        errors.append(f"[배경매핑규칙] {roster.roster_name}/pose_background_mapping.json 파싱 실패: {e}")
        return errors, warnings

    if not isinstance(map_data, dict):
        errors.append(f"[배경매핑규칙] {roster.roster_name}/pose_background_mapping.json 최상위가 딕셔너리가 아님")
        return errors, warnings

    if bg_data is None and roster.background_file.is_file():
        try:
            bg_data = json.loads(roster.background_file.read_text(encoding="utf-8-sig"))
        except Exception:
            bg_data = None

    valid_bg_keys = _extract_valid_bg_keys(bg_data)
    if not valid_bg_keys:
        errors.append(
            f"[배경매핑규칙] {roster.roster_name}/pose_background_mapping.json 이 존재하지만 유효한 background.json 프리셋이 없습니다."
        )
        return errors, warnings

    default_bg_key = str(map_data.get("default_background", "default") or "").strip()
    if default_bg_key and default_bg_key not in ("none", "auto") and default_bg_key not in valid_bg_keys:
        errors.append(
            f"[배경매핑규칙] {roster.roster_name}/pose_background_mapping.json 의 default_background "
            f"'{default_bg_key}' 가 background.json 에 존재하지 않습니다."
        )

    pose_maps = map_data.get("pose_mappings", {})
    if isinstance(pose_maps, dict):
        for code_pattern, mapped_bg in pose_maps.items():
            bg_key = str(mapped_bg or "").strip()
            if bg_key and bg_key not in ("none", "auto") and bg_key not in valid_bg_keys:
                errors.append(
                    f"[배경매핑규칙] {roster.roster_name} 포즈 대역 '{code_pattern}' 에 매핑된 배경 "
                    f"'{bg_key}' 가 background.json 에 존재하지 않습니다."
                )

    return errors, warnings


def simulate_character_poses(
    cfg: CharacterConfig,
    db: PoseDatabase,
) -> tuple[int, list[str], list[str]]:
    """단일 캐릭터의 활성 포즈 전체를 compose_scene_prompt()로 오프라인 조립 시뮬레이션한다."""
    errors: list[str] = []
    warnings: list[str] = []

    if cfg.positive:
        base_pos = cfg.positive
        char_pos = ""
        clothed_source = cfg.positive
        base_neg = cfg.negative or ""
    else:
        base_pos = POS_BASE
        char_pos = cfg.char_prompt
        clothed_source = cfg.char_prompt
        base_neg = f"{COMMON_NEG}, {cfg.custom_neg}".strip(", ")

    sig_outfit_tokens = _extract_signature_outfit_tokens(clothed_source)
    sig_acc_norm = {normalize_tag(a) for a in cfg.signature_accessories if normalize_tag(a)}

    active_mode = expand_v2_mode("all", db, expand_all_by_gender=cfg.gender or "female")
    target_codes = resolve_targets(db, active_mode)

    checked = 0
    for code in target_codes:
        entry = db.entries[code]
        checked += 1
        scene = compose_scene_prompt(
            entry=entry,
            prefix=cfg.prefix,
            tag=f"{code:03d}",
            base_positive=base_pos,
            char_prompt=char_pos,
            negative_prompt=base_neg,
            sampler_name="DPM++ 2M",
            gender=cfg.gender or "female",
            nude_positive=cfg.nude_positive,
            outfits=cfg.outfits,
            signature_accessories=cfg.signature_accessories,
        )
        full_low = scene.full_prompt.lower()
        req_outfit = (entry.required_outfit or "").strip().lower()

        # 1) 착의 포즈(required_outfit == "default") 의상 유지 검증
        if req_outfit == "default":
            if scene.is_nude_scene:
                errors.append(f"[착의고정] {cfg.prefix}:{code:03d} default 슬롯인데 is_nude_scene=True 반환")
            if "completely nude" in full_low:
                errors.append(f"[착의고정] {cfg.prefix}:{code:03d} 착의 씬에 'completely nude' 오주입")
            for tok in sig_outfit_tokens:
                if tok not in full_low:
                    errors.append(
                        f"[착의고정] {cfg.prefix}:{code:03d} 착의 씬에서 대표 의상 토큰 누락 ({tok!r})"
                    )

        # 2) 탈의 포즈(required_outfit == "nude") 의상 잔류/누수 감지
        elif req_outfit == "nude":
            if not scene.is_nude_scene:
                errors.append(f"[알몸전환] {cfg.prefix}:{code:03d} nude 슬롯인데 is_nude_scene=False 반환")
            char_body = f"{scene.active_base_pos}, {scene.active_char_prompt}".lower()
            for leak in _NUDE_OUTFIT_LEAK_TOKENS:
                if any(leak in acc for acc in sig_acc_norm):
                    continue
                if re.search(r"\b" + re.escape(leak) + r"\b", char_body):
                    errors.append(
                        f"[알몸전환] {cfg.prefix}:{code:03d} 탈의 캐릭터 본체에 주요 의상 잔류 ({leak!r})"
                    )

        # 3) 테마 의상 스왑(swimsuit, maid, wedding 등) 유효성 검증
        elif req_outfit:
            if scene.is_nude_scene:
                errors.append(
                    f"[테마스왑] {cfg.prefix}:{code:03d} 테마 슬롯({req_outfit})인데 is_nude_scene=True 반환"
                )
            resolved_theme = resolve_theme_outfit(cfg.outfits, req_outfit)
            if not resolved_theme or not resolved_theme.strip():
                errors.append(
                    f"[테마스왑] {cfg.prefix}:{code:03d} 테마 슬롯 '{req_outfit}' 의상 해석 실패"
                )
            else:
                first_theme_token = normalize_tag(resolved_theme.split(",")[0])
                if first_theme_token and first_theme_token not in full_low:
                    errors.append(
                        f"[테마스왑] {cfg.prefix}:{code:03d} 테마 의상('{first_theme_token}')이 조립 프롬프트에 미반영"
                    )

    return checked, errors, warnings


def run_precheck(base_dir: Path, roster: RosterPaths) -> PrecheckReport:
    """로스터 및 포즈 DB 종합 사전 점검 (정적 검증 + 전 포즈 조립 시뮬레이션 + 배경 매핑 정합성)."""
    report = PrecheckReport(roster_name=roster.roster_name)
    db = load_pose_db(base_dir, roster.events_file)
    report.total_poses = len(db.entries)

    # 1. 포즈 DB 점검
    report.errors.extend(audit_pose_db(db))

    # 2. 캐릭터 JSON 점검 + 전 포즈 조립 시뮬레이션
    chars_dir = roster.characters_dir
    required_theme_outfits = {
        entry.required_outfit.strip()
        for entry in db.entries.values()
        if entry.required_outfit and entry.required_outfit.strip() not in ("default", "nude")
    }
    if chars_dir.is_dir():
        char_files = sorted(chars_dir.glob("*.json"))
        report.total_characters = len(char_files)
        for cf in char_files:
            try:
                data = json.loads(cf.read_text(encoding="utf-8-sig"))
                char_errors = audit_character_safety(data, cf.stem)
                report.errors.extend(char_errors)
                if isinstance(data, dict):
                    if "outfits" not in data:
                        report.warnings.append(f"캐릭터 '{cf.stem}' JSON에 outfits 키가 없습니다.")
                    elif isinstance(data.get("outfits"), dict) and required_theme_outfits:
                        missing_outfits = sorted(k for k in required_theme_outfits if k not in data["outfits"])
                        if missing_outfits:
                            report.warnings.append(
                                f"캐릭터 '{cf.stem}' outfits에 포즈 DB 요구 테마 키 누락: {missing_outfits}"
                            )
                # 정적 구조가 유효한 캐릭터에 대해 전 포즈 조립 시뮬레이션 수행
                try:
                    cfg = load_character(chars_dir, cf.stem)
                    checked, sim_errs, sim_warns = simulate_character_poses(cfg, db)
                    report.total_combinations_checked += checked
                    report.errors.extend(sim_errs)
                    report.warnings.extend(sim_warns)
                except Exception as sim_exc:
                    report.warnings.append(f"캐릭터 '{cf.stem}' 조립 시뮬레이션 건너뜀: {sim_exc}")
            except Exception as e:
                report.errors.append(f"캐릭터 파일 {cf.name} 읽기 오류: {e}")

    # 3. 배경 설정 전체 프리셋 점검
    bg_file = roster.background_file
    bg_data: Any = None
    if bg_file.exists():
        try:
            bg_data = json.loads(bg_file.read_text(encoding="utf-8-sig"))
            if not isinstance(bg_data, dict):
                report.warnings.append(f"배경 파일 {bg_file.name} 최상위가 딕셔너리가 아님")
            else:
                if "default" not in bg_data:
                    report.warnings.append(f"배경 파일 {bg_file.name}에 'default' 프리셋이 없습니다.")
                for preset_name, preset_val in bg_data.items():
                    if str(preset_name).startswith("_"):
                        continue
                    if not isinstance(preset_val, str) or not preset_val.strip():
                        report.warnings.append(f"배경 '{preset_name}' 프리셋 값이 비어 있거나 문자열이 아님")
                        continue
                    preset_low = preset_val.lower()
                    for bad_bg in ("chandelier", "ceiling"):
                        if re.search(r"\b" + re.escape(bad_bg) + r"\b", preset_low):
                            report.warnings.append(
                                f"배경 '{preset_name}' 프리셋에 전신 왜곡 우려 태그 포함: '{bad_bg}'"
                            )
        except Exception as e:
            report.warnings.append(f"배경 파일 {bg_file.name} 읽기 오류: {e}")

    # 4. pose_background_mapping.json 정합성 검증
    map_errs, map_warns = audit_pose_background_mapping(roster, bg_data=bg_data)
    report.errors.extend(map_errs)
    report.warnings.extend(map_warns)

    return report

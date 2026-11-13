"""
generator.precheck
이미지 배치 생산 전 캐릭터 JSON, 포즈 DB 제작 규칙 및 무결성을 전수 검증하는 모듈.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from generator.models import PoseDatabase, RosterPaths
from generator.pose_db import load_pose_db


@dataclass
class PrecheckReport:
    roster_name: str
    total_characters: int = 0
    total_poses: int = 0
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


def run_precheck(base_dir: Path, roster: RosterPaths) -> PrecheckReport:
    """로스터 및 포즈 DB 종합 사전 점검."""
    report = PrecheckReport(roster_name=roster.roster_name)
    db = load_pose_db(base_dir, roster.events_file)
    report.total_poses = len(db.entries)

    # 1. 포즈 DB 점검
    report.errors.extend(audit_pose_db(db))

    # 2. 캐릭터 JSON 점검
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
                report.errors.extend(audit_character_safety(data, cf.stem))
                if isinstance(data, dict):
                    if "outfits" not in data:
                        report.warnings.append(f"캐릭터 '{cf.stem}' JSON에 outfits 키가 없습니다.")
                    elif isinstance(data.get("outfits"), dict) and required_theme_outfits:
                        missing_outfits = sorted(k for k in required_theme_outfits if k not in data["outfits"])
                        if missing_outfits:
                            report.warnings.append(
                                f"캐릭터 '{cf.stem}' outfits에 포즈 DB 요구 테마 키 누락: {missing_outfits}"
                            )
            except Exception as e:
                report.errors.append(f"캐릭터 파일 {cf.name} 읽기 오류: {e}")

    # 3. 배경 설정 전체 프리셋 점검
    bg_file = roster.background_file
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

    return report

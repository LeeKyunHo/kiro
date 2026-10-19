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


def audit_pose_db(db: PoseDatabase) -> list[str]:
    """포즈 DB 무결성 및 라벨/프롬프트 규칙 점검."""
    errors = []
    for code, entry in db.entries.items():
        label = entry.label
        # 1. 라벨 규격 검사 (슬래시 금지, 1~20자)
        if "/" in label:
            errors.append(f"포즈 {code} 라벨 슬래시 금지 위반: '{label}'")
        if not (1 <= len(label) <= 20):
            errors.append(f"포즈 {code} 라벨 길이 비정상: '{label}' ({len(label)}자)")

        low = entry.prompt.lower()
        # 2. 구강 왜곡 동시 사용 검사
        if "parted lips" in low and "puckered lips" in low:
            errors.append(f"포즈 {code} 구강 왜곡 유발 동시 사용 (parted lips + puckered lips)")

        # 3. 기형/금지 태그 검사
        for bad in ("long slender limbs", "introduction pose", "blank face", "featureless face"):
            if bad in low:
                errors.append(f"포즈 {code} 금지/결함 태그 포함: '{bad}'")
    return errors


def audit_character_safety(char_data: dict, name: str) -> list[str]:
    """캐릭터 JSON 외형 및 금지 태그 점검."""
    errors = []
    pfx = char_data.get("prefix", name)
    pos = (char_data.get("char_prompt") or char_data.get("positive") or char_data.get("sdxl_positive") or "").lower()
    nude = (char_data.get("nude_positive") or char_data.get("sdxl_nude_positive") or "").lower()

    for target_text, field_name in [(pos, "외형 프롬프트"), (nude, "탈의 프롬프트")]:
        if "long slender limbs" in target_text:
            errors.append(f"캐릭터 '{pfx}' {field_name}에 기형 유발 태그 'long slender limbs' 잔류")

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
    if chars_dir.is_dir():
        char_files = sorted(chars_dir.glob("*.json"))
        report.total_characters = len(char_files)
        for cf in char_files:
            try:
                data = json.loads(cf.read_text(encoding="utf-8-sig"))
                report.errors.extend(audit_character_safety(data, cf.stem))
            except Exception as e:
                report.errors.append(f"캐릭터 파일 {cf.name} 읽기 오류: {e}")

    # 3. 배경 설정 점검
    bg_file = roster.background_file
    if bg_file.exists():
        try:
            bg_data = json.loads(bg_file.read_text(encoding="utf-8-sig"))
            default_bg = str(bg_data.get("default", "")).lower()
            for bad_bg in ("chandelier", "ceiling"):
                if re.search(r"\b" + re.escape(bad_bg) + r"\b", default_bg):
                    report.warnings.append(f"배경 default 프리셋에 전신 왜곡 우려 태그 포함: '{bad_bg}'")
        except Exception as e:
            report.warnings.append(f"배경 파일 {bg_file.name} 읽기 오류: {e}")

    return report

"""
generator.checkpoint
체크포인트 프로필 로드, 패턴 매칭 및 파라미터 적용 모듈.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from generator.config import ConfigError


@dataclass(frozen=True)
class CheckpointProfile:
    key: str
    name: str
    engine: str = "sdxl"
    width: int = 832
    height: int = 1216
    steps: int = 25
    cfg: float = 6.0
    sampler: str = "DPM++ 2M"
    scheduler: str = "Karras"
    clip_skip: int = 2
    quality_positive: str = ""
    quality_negative: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    match_patterns: list[str] = field(default_factory=list)


def load_checkpoint_profiles(base_dir: Path) -> dict[str, CheckpointProfile]:
    """checkpoint_profiles.json 을 읽어 프로필 딕셔너리로 반환한다."""
    path = base_dir / "checkpoint_profiles.json"
    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ConfigError(f"checkpoint_profiles.json 로드 실패: {e}") from None

    profiles_data = data.get("profiles", {})
    profiles: dict[str, CheckpointProfile] = {}

    for key, p in profiles_data.items():
        patterns = [pat.lower() for pat in p.get("match_patterns", [])]
        profiles[key] = CheckpointProfile(
            key=key,
            name=p.get("name", key),
            engine=p.get("engine", "sdxl"),
            width=int(p.get("width", 832)),
            height=int(p.get("height", 1216)),
            steps=int(p.get("steps", 25)),
            cfg=float(p.get("cfg", 6.0)),
            sampler=str(p.get("sampler", "DPM++ 2M")),
            scheduler=str(p.get("scheduler", "Karras")),
            clip_skip=int(p.get("clip_skip", 2)),
            quality_positive=str(p.get("quality_positive", "")),
            quality_negative=str(p.get("quality_negative", "")),
            description=str(p.get("description", "")),
            tags=list(p.get("tags", [])),
            match_patterns=patterns,
        )

    if "default_sdxl" in data:
        p = data["default_sdxl"]
        profiles["default_sdxl"] = CheckpointProfile(
            key="default_sdxl",
            name=p.get("name", "Default SDXL"),
            engine=p.get("engine", "sdxl"),
            width=int(p.get("width", 832)),
            height=int(p.get("height", 1216)),
            steps=int(p.get("steps", 25)),
            cfg=float(p.get("cfg", 6.5)),
            sampler=str(p.get("sampler", "dpmpp_sde")),
            scheduler=str(p.get("scheduler", "karras")),
            clip_skip=int(p.get("clip_skip", 2)),
            quality_positive=str(p.get("quality_positive", "")),
            quality_negative=str(p.get("quality_negative", "")),
            description=str(p.get("description", "")),
            tags=list(p.get("tags", ["default"])),
            match_patterns=["default", "sdxl"],
        )

    return profiles


def match_checkpoint_profile(
    profiles: dict[str, CheckpointProfile], query: str
) -> CheckpointProfile | None:
    """이름, 키, 또는 match_patterns 로 체크포인트 프로필을 검색한다."""
    if not query:
        return None
    q = query.lower().strip()

    # 1. 완전 일치
    if q in profiles:
        return profiles[q]

    for key, prof in profiles.items():
        if prof.name.lower() == q or key.lower() == q:
            return prof

    # 2. match_patterns 검색
    for prof in profiles.values():
        for pat in prof.match_patterns:
            if pat in q or q in pat:
                return prof

    # 3. 이름 부분 일치
    for prof in profiles.values():
        if q in prof.name.lower():
            return prof

    return None

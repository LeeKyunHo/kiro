"""
generator.checkpoint
체크포인트 프로필 로드, 패턴 매칭 및 파라미터 적용 모듈.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from generator.config import CFG_SCALE, IMAGE_SIZE, STEPS, ConfigError


_SAMPLER_ALIAS_MAP: dict[str, str] = {
    "dpmpp_2m": "DPM++ 2M",
    "dpmpp_2m_sde": "DPM++ 2M SDE",
    "dpmpp_sde": "DPM++ SDE",
    "dpmpp_3m_sde": "DPM++ 3M SDE",
    "euler_ancestral": "Euler a",
    "euler": "Euler",
    "ddim": "DDIM",
}
_SCHEDULER_ALIAS_MAP: dict[str, str] = {
    "karras": "Karras",
    "normal": "Automatic",
    "automatic": "Automatic",
    "exponential": "Exponential",
    "sgm_uniform": "SGM Uniform",
    "simple": "Simple",
    "uniform": "Uniform",
}


def normalize_webui_sampler(name: str) -> str:
    """ComfyUI/프로필 샘플러 식별자를 WebUI API 표준 샘플러명으로 정규화한다."""
    key = name.strip().lower()
    return _SAMPLER_ALIAS_MAP.get(key, name.strip())


def normalize_webui_scheduler(name: str) -> str:
    """ComfyUI/프로필 스케줄러 식별자를 WebUI API 표준 스케줄러명으로 정규화한다."""
    key = name.strip().lower()
    return _SCHEDULER_ALIAS_MAP.get(key, name.strip())


@dataclass(frozen=True, slots=True)
class ResolvedGenerationParams:
    """우선순위(Config < CheckpointProfile < CharacterConfig < CLI/GUI) 적용 결과."""

    steps: int
    cfg_scale: float
    sampler: str
    scheduler: str
    width: int
    height: int
    clip_skip: int | None = None


def resolve_generation_params(
    *,
    fallback_sampler: str,
    ckpt_profile: CheckpointProfile | None = None,
    char_overrides: dict[str, Any] | None = None,
    explicit_steps: int | None = None,
    explicit_cfg_scale: float | None = None,
    explicit_sampler: str | None = None,
    explicit_scheduler: str | None = None,
    explicit_width: int | None = None,
    explicit_height: int | None = None,
    explicit_clip_skip: int | None = None,
) -> ResolvedGenerationParams:
    """수치 파라미터만 우선순위(Config < CheckpointProfile < CharacterConfig < CLI/GUI)로 병합한다.

    quality_positive / quality_negative 는 과포화 방지를 위해 자동 병합하지 않는다.
    """
    steps: int = STEPS
    cfg_scale: float = float(CFG_SCALE)
    sampler: str = fallback_sampler
    scheduler: str = "Automatic"
    width: int = IMAGE_SIZE[0]
    height: int = IMAGE_SIZE[1]
    clip_skip: int | None = None

    # Layer 2: CheckpointProfile (수치 파라미터만 적용)
    if ckpt_profile is not None:
        steps = int(ckpt_profile.steps)
        cfg_scale = float(ckpt_profile.cfg)
        if ckpt_profile.sampler:
            sampler = normalize_webui_sampler(ckpt_profile.sampler)
        if ckpt_profile.scheduler:
            scheduler = normalize_webui_scheduler(ckpt_profile.scheduler)
        width = int(ckpt_profile.width)
        height = int(ckpt_profile.height)
        clip_skip = int(ckpt_profile.clip_skip) if ckpt_profile.clip_skip else None

    # Layer 3: CharacterConfig optional numeric overrides
    if char_overrides:
        if char_overrides.get("steps") is not None:
            steps = int(char_overrides["steps"])
        char_cfg = char_overrides.get("cfg_scale")
        if char_cfg is None:
            char_cfg = char_overrides.get("cfg")
        if char_cfg is not None:
            cfg_scale = float(char_cfg)
        if char_overrides.get("sampler"):
            sampler = normalize_webui_sampler(str(char_overrides["sampler"]))
        if char_overrides.get("scheduler"):
            scheduler = normalize_webui_scheduler(str(char_overrides["scheduler"]))
        if char_overrides.get("width") is not None:
            width = int(char_overrides["width"])
        if char_overrides.get("height") is not None:
            height = int(char_overrides["height"])
        if char_overrides.get("clip_skip") is not None:
            clip_skip = int(char_overrides["clip_skip"])

    # Layer 4: CLI / GUI 명시 인자
    if explicit_steps is not None:
        steps = int(explicit_steps)
    if explicit_cfg_scale is not None:
        cfg_scale = float(explicit_cfg_scale)
    if explicit_sampler:
        sampler = normalize_webui_sampler(str(explicit_sampler))
    if explicit_scheduler:
        scheduler = normalize_webui_scheduler(str(explicit_scheduler))
    if explicit_width is not None:
        width = int(explicit_width)
    if explicit_height is not None:
        height = int(explicit_height)
    if explicit_clip_skip is not None:
        clip_skip = int(explicit_clip_skip)

    return ResolvedGenerationParams(
        steps=steps,
        cfg_scale=cfg_scale,
        sampler=sampler,
        scheduler=scheduler,
        width=width,
        height=height,
        clip_skip=clip_skip,
    )


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

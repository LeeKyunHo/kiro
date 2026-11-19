"""
generator 패키지 루트.
모듈화된 SD 캐릭터 에셋 배치 생성 파이프라인.
"""

import importlib
from typing import Any

from generator.config import ConfigError, configure_logging, configure_stdio
from generator.models import (
    AllCharsResult,
    BatchResult,
    CharacterConfig,
    ControlNetSpec,
    GenerationOptions,
    PoseDatabase,
    PoseEntry,
    Profile,
    ReferenceImage,
    RosterPaths,
    RunHooks,
    TestReport,
    TimingStats,
    summarize_durations,
)
from generator.pose_db import (
    V2_BUILTIN_SECTIONS,
    V2_FEMALE_SECTIONS,
    V2_OTOKONOKO_SECTIONS,
    expand_v2_mode,
    load_pose_db,
    parse_pose_db,
    peek_choices,
    print_warnings,
    read_pose_json,
    resolve_profile,
    resolve_targets,
)
from generator.prompt import (
    assemble_prompt,
    clamp_weights,
    find_tag_conflicts,
    join_tags,
    normalize_tag,
    resolve_background,
    split_tags,
    validate_prefix,
    validate_ref_weight,
)
from generator.precheck import PrecheckReport, run_precheck
from generator.checkpoint import (
    CheckpointProfile,
    load_checkpoint_profiles,
    match_checkpoint_profile,
)
from generator.reference import build_controlnet_unit, resolve_controlnet_spec, resolve_reference_image
from generator.reporter import (
    asset_filename,
    build_genit_block,
    build_section_guide,
    code_width,
    format_code,
    print_summary,
)
from generator.roster import RosterPathManager, apply_character_to_args, list_characters, load_character
from generator.runner import execute, run_all_chars, run_batch, run_interrogate
from generator.scene_composer import (
    SceneComposition,
    compose_scene_prompt,
    should_apply_background,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "censor_file": ("generator.censor", "censor_file"),
    "censor_image": ("generator.censor", "censor_image"),
    "detect_genitals": ("generator.censor", "detect_genitals"),
    "extract_pose_code": ("generator.censor", "extract_pose_code"),
    "get_origin_backup_path": ("generator.censor", "get_origin_backup_path"),
    "get_pose_anchor_box": ("generator.censor", "get_pose_anchor_box"),
    "get_pose_category": ("generator.censor", "get_pose_category"),
    "is_censor_target": ("generator.censor", "is_censor_target"),
    "run_batch_censor": ("generator.censor", "run_batch_censor"),
    "POSE_SAFE_ANCHORS": ("generator.censor", "POSE_SAFE_ANCHORS"),
    "validate_generated_image": ("generator.qa_validator", "validate_generated_image"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    # config
    "ConfigError",
    "configure_logging",
    "configure_stdio",
    # models
    "RosterPaths",
    "PoseEntry",
    "Profile",
    "PoseDatabase",
    "CharacterConfig",
    "ReferenceImage",
    "ControlNetSpec",
    "TimingStats",
    "BatchResult",
    "AllCharsResult",
    "GenerationOptions",
    "RunHooks",
    "TestReport",
    "summarize_durations",
    # pose_db
    "V2_FEMALE_SECTIONS",
    "V2_OTOKONOKO_SECTIONS",
    "V2_BUILTIN_SECTIONS",
    "expand_v2_mode",
    "load_pose_db",
    "parse_pose_db",
    "peek_choices",
    "print_warnings",
    "read_pose_json",
    "resolve_profile",
    "resolve_targets",
    # prompt
    "assemble_prompt",
    "clamp_weights",
    "find_tag_conflicts",
    "join_tags",
    "normalize_tag",
    "resolve_background",
    "split_tags",
    "validate_prefix",
    "validate_ref_weight",
    # reference
    "build_controlnet_unit",
    "resolve_controlnet_spec",
    "resolve_reference_image",
    # reporter
    "asset_filename",
    "build_genit_block",
    "build_section_guide",
    "code_width",
    "format_code",
    "print_summary",
    # roster
    "RosterPathManager",
    "apply_character_to_args",
    "list_characters",
    "load_character",
    # scene_composer
    "SceneComposition",
    "compose_scene_prompt",
    "should_apply_background",
    # runner
    "execute",
    "run_all_chars",
    "run_batch",
    "run_interrogate",
    # censor
    "censor_file",
    "censor_image",
    "detect_genitals",
    "extract_pose_code",
    "get_origin_backup_path",
    "get_pose_anchor_box",
    "get_pose_category",
    "is_censor_target",
    "run_batch_censor",
    "POSE_SAFE_ANCHORS",
    # qa_validator
    "validate_generated_image",
]

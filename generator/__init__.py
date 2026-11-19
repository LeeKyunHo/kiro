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
from generator.pose_background import PoseBackgroundService, default_pose_bg_service
from generator.pose_db import (
    POSE_SETS_FILE,
    V2_BUILTIN_SECTIONS,
    V2_FEMALE_SECTIONS,
    V2_OTOKONOKO_SECTIONS,
    expand_v2_mode,
    load_pose_db,
    load_pose_sets,
    parse_pose_db,
    peek_choices,
    print_warnings,
    read_pose_json,
    resolve_pose_set_codes,
    resolve_targets,
)
from generator.prompt import (
    DEFAULT_THEME_OUTFITS,
    NEG_MAX_WEIGHT,
    POS_MAX_WEIGHT,
    assemble_prompt,
    clamp_prompt_weights,
    clamp_weights,
    compute_deterministic_pose_seed,
    find_tag_conflicts,
    harmonize_character_expression,
    join_tags,
    normalize_tag,
    remove_conflicting_tags_from_negative,
    resolve_background,
    resolve_theme_outfit,
    split_tags,
    strip_outfit_tags,
    validate_prefix,
    validate_ref_weight,
)
from generator.precheck import PrecheckReport, run_precheck
from generator.option_specs import (
    GENERATION_OPTION_SPECS,
    OPTION_SPEC_OBJECTS,
    OptionSpec,
    get_gui_option_specs,
    get_option_spec,
)
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
    apply_composition_anti_drift,
    compose_scene_prompt,
    should_apply_background,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AssetManager": ("generator.asset_manager", "AssetManager"),
    "_atomic_replace_with_retry": ("generator.censor", "_atomic_replace_with_retry"),
    "_is_otokonoko_context": ("generator.censor", "_is_otokonoko_context"),
    "apply_debug_bbox": ("generator.censor", "apply_debug_bbox"),
    "apply_manga_bar": ("generator.censor", "apply_manga_bar"),
    "apply_manga_glow": ("generator.censor", "apply_manga_glow"),
    "censor_file": ("generator.censor", "censor_file"),
    "censor_image": ("generator.censor", "censor_image"),
    "detect_genitals": ("generator.censor", "detect_genitals"),
    "extract_pose_code": ("generator.censor", "extract_pose_code"),
    "get_custom_censor_seg_model": ("generator.censor", "get_custom_censor_seg_model"),
    "get_origin_backup_path": ("generator.censor", "get_origin_backup_path"),
    "get_pose_anchor_box": ("generator.censor", "get_pose_anchor_box"),
    "get_pose_category": ("generator.censor", "get_pose_category"),
    "get_sam_model": ("generator.censor", "get_sam_model"),
    "is_censor_target": ("generator.censor", "is_censor_target"),
    "run_batch_censor": ("generator.censor", "run_batch_censor"),
    "unload_censor_models": ("generator.censor", "unload_censor_models"),
    "verify_and_heal_censor": ("generator.censor", "verify_and_heal_censor"),
    "POSE_SAFE_ANCHORS": ("generator.censor", "POSE_SAFE_ANCHORS"),
    "GenitExporter": ("generator.genit_exporter", "GenitExporter"),
    "default_genit_exporter": ("generator.genit_exporter", "default_genit_exporter"),
    "denormalize_genit_code": ("generator.genit_exporter", "denormalize_genit_code"),
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
    "resolve_targets",
    # prompt
    "DEFAULT_THEME_OUTFITS",
    "POS_MAX_WEIGHT",
    "NEG_MAX_WEIGHT",
    "assemble_prompt",
    "clamp_prompt_weights",
    "clamp_weights",
    "find_tag_conflicts",
    "harmonize_character_expression",
    "join_tags",
    "normalize_tag",
    "remove_conflicting_tags_from_negative",
    "resolve_background",
    "resolve_theme_outfit",
    "split_tags",
    "strip_outfit_tags",
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
    "apply_composition_anti_drift",
    "compose_scene_prompt",
    "should_apply_background",
    # option_specs
    "GENERATION_OPTION_SPECS",
    "OPTION_SPEC_OBJECTS",
    "OptionSpec",
    "get_gui_option_specs",
    "get_option_spec",
    # runner
    "execute",
    "run_all_chars",
    "run_batch",
    "run_interrogate",
    # censor
    "AssetManager",
    "_is_otokonoko_context",
    "censor_file",
    "censor_image",
    "detect_genitals",
    "extract_pose_code",
    "get_custom_censor_seg_model",
    "get_origin_backup_path",
    "get_pose_anchor_box",
    "get_pose_category",
    "get_sam_model",
    "is_censor_target",
    "run_batch_censor",
    "POSE_SAFE_ANCHORS",
    # qa_validator
    "validate_generated_image",
]

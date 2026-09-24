"""
generator 패키지 루트.
모듈화된 SD 캐릭터 에셋 배치 생성 파이프라인.
"""

from generator.config import ConfigError, configure_stdio
from generator.models import (
    BatchResult,
    CharacterConfig,
    ControlNetSpec,
    PoseDatabase,
    PoseEntry,
    Profile,
    ReferenceImage,
    RosterPaths,
    TestReport,
    TimingStats,
    summarize_durations,
)
from generator.pose_db import (
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
    find_tag_conflicts,
    join_tags,
    normalize_tag,
    resolve_background,
    split_tags,
    validate_prefix,
    validate_ref_weight,
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
from generator.runner import execute, run_all_chars, run_batch

__all__ = [
    # config
    "ConfigError",
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
    "TestReport",
    "summarize_durations",
    # pose_db
    "load_pose_db",
    "parse_pose_db",
    "peek_choices",
    "print_warnings",
    "read_pose_json",
    "resolve_profile",
    "resolve_targets",
    # prompt
    "assemble_prompt",
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
    # runner
    "execute",
    "run_all_chars",
    "run_batch",
]

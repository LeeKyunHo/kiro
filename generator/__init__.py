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
)
from generator.pose_db import load_pose_db, parse_pose_db, read_pose_json, resolve_profile, resolve_targets
from generator.prompt import assemble_prompt, normalize_tag, resolve_background, validate_prefix, validate_ref_weight
from generator.reference import build_controlnet_unit, resolve_controlnet_spec, resolve_reference_image
from generator.reporter import asset_filename, build_genit_block, code_width, format_code, print_summary
from generator.roster import RosterPathManager, apply_character_to_args, list_characters, load_character
from generator.runner import execute, run_all_chars, run_batch

__all__ = [
    "ConfigError",
    "configure_stdio",
    "RosterPaths",
    "PoseEntry",
    "Profile",
    "PoseDatabase",
    "CharacterConfig",
    "ReferenceImage",
    "ControlNetSpec",
    "BatchResult",
    "TestReport",
    "load_pose_db",
    "parse_pose_db",
    "read_pose_json",
    "resolve_profile",
    "resolve_targets",
    "assemble_prompt",
    "normalize_tag",
    "resolve_background",
    "validate_prefix",
    "validate_ref_weight",
    "build_controlnet_unit",
    "resolve_controlnet_spec",
    "resolve_reference_image",
    "asset_filename",
    "build_genit_block",
    "code_width",
    "format_code",
    "print_summary",
    "RosterPathManager",
    "apply_character_to_args",
    "list_characters",
    "load_character",
    "execute",
    "run_all_chars",
    "run_batch",
]

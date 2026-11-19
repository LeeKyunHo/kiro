"""
gui/runner_bridge.py
GUI 설정값을 코어 GenerationOptions로 조립하고 ZenIt 마크다운·Auto-QC 리포트를 생성하는 브릿지.
"""
from __future__ import annotations

from typing import Any, Sequence

from generator.config import REF_WEIGHT_DEFAULT
from generator.models import CharacterConfig, GenerationOptions, PoseDatabase
from generator.pose_db import expand_v2_mode, resolve_targets
from generator.reporter import build_genit_block, code_width
from generator.roster import apply_character_to_args
from gui.state import GuiRuntimeState, resolve_gui_mode_and_codes


def expand_gui_target_mode(
    target_mode: str,
    codes_arg: str | None,
    char_gender: str,
    db: PoseDatabase,
) -> str:
    """GUI target_mode가 'all'이고 명시 코드가 없을 때 V2 성별 활성 섹션으로 확장한다."""
    if target_mode == "all" and not codes_arg:
        return expand_v2_mode("all", db, expand_all_by_gender=char_gender or "female")
    return target_mode


def build_base_options(
    state: GuiRuntimeState,
    current_cfg: dict[str, Any],
    *,
    mode_val: str,
    codes_val: str | None,
    ckpt_name: str | None,
    mock: bool = False,
) -> GenerationOptions:
    """GUI 공통 실행 설정을 GenerationOptions로 조립한다."""
    active_ckpt = state.ckpt_profiles.get(ckpt_name) if ckpt_name else None
    use_ref = bool(current_cfg.get("use_reference", True))
    return GenerationOptions(
        mode=mode_val,
        codes=codes_val,
        bg_preset="default",
        ref_weight=REF_WEIGHT_DEFAULT,
        no_ref=not use_ref,
        dry_run=False,
        mock=bool(mock),
        steps=int(current_cfg.get("steps", 25)),
        sampler=str(current_cfg.get("sampler", "DPM++ 2M")),
        scheduler=str(current_cfg.get("scheduler", "Karras")),
        cfg_scale=float(current_cfg.get("cfg_scale", 6.0)),
        naming=str(current_cfg.get("naming", "hybrid")),
        overwrite=bool(current_cfg.get("overwrite", False)),
        auto_qc=bool(current_cfg.get("auto_qc", False)),
        strict_censor_qc=bool(current_cfg.get("strict_censor_qc", False)),
        max_retries=int(current_cfg.get("max_retries", 3)),
        censor=bool(current_cfg.get("censor", False)),
        censor_style=str(current_cfg.get("censor_style", "white_silhouette")),
        ckpt_profile=ckpt_name,
        active_ckpt=active_ckpt,
    )


def build_char_options(
    state: GuiRuntimeState,
    cfg: CharacterConfig,
    raw_expr: str,
    current_cfg: dict[str, Any],
    *,
    ckpt_name: str | None,
    mock: bool = False,
) -> GenerationOptions:
    """개별 캐릭터 설정(cfg)과 GUI 범위 입력(raw_expr)을 결합해 GenerationOptions를 생성한다."""
    mode_val, codes_val = resolve_gui_mode_and_codes(
        raw_expr,
        cfg.gender,
        available_sections=set(state.db.sections.keys()),
    )
    opts = build_base_options(
        state,
        current_cfg,
        mode_val=mode_val,
        codes_val=codes_val,
        ckpt_name=ckpt_name,
        mock=mock,
    )
    apply_character_to_args(cfg, opts, state.db)
    return opts


def build_char_zenit_block(
    state: GuiRuntimeState,
    cfg: CharacterConfig,
    opts: GenerationOptions,
    *,
    naming: str,
) -> str:
    """단일 캐릭터의 생성 범위 기준 ZenIt 마크다운 블록을 생성한다."""
    target_mode = expand_gui_target_mode(
        opts.mode,
        opts.codes,
        cfg.gender or "female",
        state.db,
    )
    targets = resolve_targets(state.db, target_mode, opts.codes)
    w = code_width(state.db.all_codes)
    return build_genit_block(cfg.prefix, targets, state.db, w, naming=naming)


def format_qa_warnings_markdown(
    qa_warnings: Sequence[tuple[int, str, str]],
) -> str:
    """단일 캐릭터 Auto-QC 품질 경고 목록을 마크다운 섹션으로 포맷한다."""
    if not qa_warnings:
        return ""
    warn_text = "\n".join(f"- [{c:03d} {lbl}] {rsn}" for c, lbl, rsn in qa_warnings)
    return f"### ⚠️ Auto-QC 품질 경고 ({len(qa_warnings)}건)\n{warn_text}"


def format_all_chars_qa_markdown(
    all_qa_warnings: Sequence[tuple[str, int, str, str]],
) -> str:
    """전체 캐릭터 Auto-QC 품질 경고 요약을 마크다운 섹션으로 포맷한다."""
    if not all_qa_warnings:
        return ""
    qa_lines = [f"### ⚠️ Auto-QC 품질 경고 요약 ({len(all_qa_warnings)}건)"]
    for q_char, q_code, q_lbl, q_rsn in all_qa_warnings:
        qa_lines.append(f"- [{q_char}] 포즈 {q_code:03d} ({q_lbl}): {q_rsn}")
    return "\n".join(qa_lines)

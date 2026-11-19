"""
Wave P3 통합 검증 테스트:
1. 4-Tier 모브 파이프라인(ADR-100/101/105) · Phase S7 상안부 그림자 표준 · strict_censor_qc Tier 0~2 스킵
2. signature_accessories(ADR-106) 전단 우선 배치 및 네거티브 충돌 해제
3. precheck.py 전 포즈 조립 시뮬레이션(착의 고정·탈의 누수·테마 스왑·배경 매핑 정합성)
4. GENERATION_OPTION_SPECS 삼위일체(GenerationOptions ↔ CLI ↔ GUI) 동기화 계약
5. scripts/audit_asset_alignment.py 및 scripts/export_context.py 독립 도구 검증
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image
import pytest

import generator.censor as censor_mod
from generator.models import BatchResult, CharacterConfig, GenerationOptions, PoseEntry, RosterPaths
from generator.option_specs import (
    GENERATION_OPTION_SPECS,
    OPTION_SPEC_OBJECTS,
    get_gui_option_specs,
)
from generator.pose_db import parse_pose_db
from generator.precheck import run_precheck
from generator.roster import apply_character_to_args, load_character
import generator.runner as runner_mod
from generator.scene_composer import (
    _MOB_EYELESS_BANGS_NEG,
    _MOB_SHADOW_EYES_POS,
    _PARTNER_POS_SPECS,
    SceneComposition,
    compose_scene_prompt,
)
from gui.runner_bridge import build_base_options
from gui.settings import DEFAULT_SETTINGS
import sd_batch_generator
from scripts.audit_asset_alignment import audit_roster_alignment
from scripts.export_context import export_context_snapshot


def _make_png_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (220, 180, 160)).save(buf, format="PNG")
    return buf.getvalue()


# ── 1. 4-Tier 모브 파이프라인 & Phase S7 상안부 그림자 & strict_censor_qc Tier 연동 ──


def test_pose_db_parses_optional_mob_tier_and_partner_pos() -> None:
    """parse_pose_db 가 선택적 mob_tier(0~3) 및 partner_pos 를 안전하게 파싱하고 미지정 시 None 을 유지해야 한다."""
    db = parse_pose_db({
        "poses": {
            "20": {"label": "기본", "prompt": "standing, smile"},
            "27": {
                "label": "백허그",
                "prompt": "hugging from behind, warm smile",
                "mob_tier": 2,
                "partner_pos": "behind",
            },
            "28": {"label": "잘못된티어", "prompt": "standing", "mob_tier": 99},
        }
    })
    assert db.entries[20].mob_tier is None
    assert db.entries[20].partner_pos is None
    assert db.entries[27].mob_tier == 2
    assert db.entries[27].partner_pos == "behind"
    assert db.entries[28].mob_tier is None


def test_four_tier_mob_pipeline_and_phase_s7_shadow_eyes() -> None:
    """4-Tier 모브(0~3), 방향성 partner_pos, Phase S7 상안부 그림자 음영 및 여성 눈 보호 규칙을 검증한다."""
    # Tier 0: 단독 구도 보존 (모브 블록 미주입)
    e0 = PoseEntry(80, "lying on bed, blushing, solo focus", "h_scenes", mob_tier=0)
    sc0 = compose_scene_prompt(
        entry=e0, prefix="rei", tag="080",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair", negative_prompt="lowres", sampler_name="DPM++ 2M",
    )
    assert sc0.mob_tier == 0
    assert sc0.is_solo_scene is True
    assert sc0.is_interactive is False
    assert "dark-haired male mob" not in sc0.full_prompt
    assert "disembodied penis" not in sc0.full_prompt
    assert "disembodied limb" in sc0.actual_negative

    # Tier 1 + partner_pos="above": 손/팔 POV + 방향성 인라인
    e1 = PoseEntry(81, "lying on back, flushed face", "h_scenes", mob_tier=1, partner_pos="above")
    sc1 = compose_scene_prompt(
        entry=e1, prefix="rei", tag="081",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair", negative_prompt="lowres, 1boy", sampler_name="DPM++ 2M",
    )
    assert sc1.is_interactive is True
    assert "disembodied male hands" in sc1.full_prompt
    assert _PARTNER_POS_SPECS["above"] in sc1.full_prompt
    assert "male side profile" in sc1.actual_negative

    # Tier 2 + partner_pos="side_kiss" (여성): Phase S7 상안부 그림자 + 여성 눈 보호 + 마네킹/이염 태그 배제
    e2 = PoseEntry(60, "deep kiss, romantic mood", "romance", mob_tier=2, partner_pos="side_kiss")
    sc2 = compose_scene_prompt(
        entry=e2, prefix="rei", tag="060",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair", negative_prompt="lowres, 1boy", sampler_name="DPM++ 2M",
        gender="female",
    )
    assert sc2.is_interactive is True
    assert _MOB_SHADOW_EYES_POS in sc2.full_prompt
    assert "(clear visible female eyes:1.15)" in sc2.full_prompt
    assert "cropped dark-haired male jaw and lips" in sc2.full_prompt
    assert "faceless" not in sc2.full_prompt.lower()
    assert "bald" not in sc2.full_prompt.lower()
    assert "hair over eyes" not in sc2.full_prompt.lower()
    assert "bangs covering eyes" not in sc2.full_prompt.lower()
    assert "visible male eyes" in sc2.actual_negative

    # Tier 2 (오토코노코): 여성 한정 clear visible female eyes 미주입
    sc2_otk = compose_scene_prompt(
        entry=e2, prefix="ren", tag="060",
        base_positive="masterpiece BREAK 1boy, otokonoko, solo",
        char_prompt="pink hair", negative_prompt="lowres", sampler_name="DPM++ 2M",
        gender="otokonoko",
    )
    assert _MOB_SHADOW_EYES_POS in sc2_otk.full_prompt
    assert "clear visible female eyes" not in sc2_otk.full_prompt

    # Tier 2 + partner_pos="bury_chest": 뒤통수 밀착이므로 상안부 그림자 블록 중복 미주입
    e2_bury = PoseEntry(62, "chest hug, intimate", "romance", mob_tier=2, partner_pos="bury_chest")
    sc2_bury = compose_scene_prompt(
        entry=e2_bury, prefix="rei", tag="062",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair", negative_prompt="lowres", sampler_name="DPM++ 2M",
    )
    assert _PARTNER_POS_SPECS["bury_chest"] in sc2_bury.full_prompt
    assert _MOB_SHADOW_EYES_POS not in sc2_bury.full_prompt

    # Tier 3 + partner_pos="behind": 풀 인터랙션 POV crotch BREAK 블록 + 방향성 인라인
    e3 = PoseEntry(88, "doggystyle on bed", "h_scenes", mob_tier=3, partner_pos="behind")
    sc3 = compose_scene_prompt(
        entry=e3, prefix="rei", tag="088",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair", negative_prompt="lowres, 1boy", sampler_name="DPM++ 2M",
    )
    assert sc3.mob_tier == 3
    assert _PARTNER_POS_SPECS["behind"] in sc3.full_prompt
    assert "BREAK (pov crotch:1.05)" in sc3.full_prompt
    assert "giant penis" in sc3.actual_negative


def test_strict_censor_qc_skips_mob_tiers_0_1_2_and_checks_tier_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-100: strict_censor_qc 는 mob_tier 0~2 포즈에서 불필요한 리롤을 스킵하고 Tier 3 / None 에서만 검사한다."""
    png_bytes = _make_png_bytes()
    monkeypatch.setattr(runner_mod, "_request_image_with_fallback", lambda *_a, **_kw: png_bytes)

    detect_calls = {"n": 0}
    monkeypatch.setattr(
        censor_mod,
        "detect_genitals",
        lambda _img, **_kw: detect_calls.__setitem__("n", detect_calls["n"] + 1) or [],
    )

    scene_base = SceneComposition(
        pose_prompt="sex, intimate", apply_bg=False, bg_applied=None,
        active_base_pos="masterpiece", active_char_prompt="1girl",
        full_prompt="masterpiece, 1girl, sex", actual_negative="lowres",
        actual_sampler="DPM++ 2M", actual_steps=20, actual_cfg=6.0,
        actual_scheduler="Karras", actual_freeu=False,
        actual_width=832, actual_height=1216,
        is_clothed_scene=False, is_nude_scene=True,
        is_aftermath=False, is_offscreen_partner=True,
        is_solo_scene=False, is_interactive=True, is_kiss_scene=False,
        controlnet_units=[],
    )

    # mob_tier 0, 1, 2 -> detect_genitals 미호출
    for tier in (0, 1, 2):
        entry = PoseEntry(80 + tier, "intimate scene", "h_scenes", mob_tier=tier)
        res = BatchResult()
        runner_mod._generate_and_validate_image(
            payload={"seed": 1}, scene=scene_base, entry=entry,
            code=entry.code, tag=f"{entry.code:03d}", clip_skip=None,
            auto_qc=False, strict_censor_qc=True, max_retries=2, result=res,
        )
        assert res.qa_warnings == []
    assert detect_calls["n"] == 0

    # mob_tier 3 -> detect_genitals 호출 및 max_retries 리롤 후 경고 기록
    entry3 = PoseEntry(88, "doggystyle", "h_scenes", mob_tier=3)
    res3 = BatchResult()
    runner_mod._generate_and_validate_image(
        payload={"seed": 1}, scene=scene_base, entry=entry3,
        code=88, tag="088", clip_skip=None,
        auto_qc=False, strict_censor_qc=True, max_retries=1, result=res3,
    )
    assert detect_calls["n"] == 2
    assert len(res3.qa_warnings) == 1
    assert "strict_censor_qc" in res3.qa_warnings[0][2]


# ── 2. signature_accessories (ADR-106) ──


def test_signature_accessories_front_injection_and_negative_conflict_removal(tmp_path: Path) -> None:
    """signature_accessories 가 캐릭터 JSON 로드·전단 배치·탈의 필터링·네거티브 충돌 해제를 정확히 수행해야 한다."""
    chars_dir = tmp_path / "characters"
    chars_dir.mkdir(parents=True)
    (chars_dir / "rina.json").write_text(
        json.dumps({
            "prefix": "rina",
            "char_prompt": "1girl, solo, silver hair, school uniform, round glasses",
            "nude_positive": "1girl, silver hair, bare skin",
            "signature_accessories": ["round glasses", "red silk choker"],
        }),
        encoding="utf-8",
    )
    cfg = load_character(chars_dir, "rina")
    assert cfg.signature_accessories == ["round glasses", "red silk choker"]

    opts = GenerationOptions()
    apply_character_to_args(cfg, opts)
    assert opts.signature_accessories == ["round glasses", "red silk choker"]

    # 1) 착의 씬: 두 소품 모두 앵커(1girl, solo) 직후 전단 우선 배치 & 네거티브에서 glasses/choker 해제
    e_clothed = PoseEntry(0, "standing, gentle smile", "emotions", required_outfit="default")
    sc_clothed = compose_scene_prompt(
        entry=e_clothed, prefix="rina", tag="000",
        base_positive="masterpiece", char_prompt=cfg.char_prompt,
        negative_prompt="lowres, (glasses, choker, bad anatomy:1.3)",
        sampler_name="DPM++ 2M",
        signature_accessories=cfg.signature_accessories,
    )
    assert "(round glasses:1.10)" in sc_clothed.active_char_prompt
    assert "(red silk choker:1.10)" in sc_clothed.active_char_prompt
    # 전단 배치 확인 (silver hair 보다 앞)
    assert sc_clothed.active_char_prompt.index("round glasses") < sc_clothed.active_char_prompt.index("silver hair")
    # 네거티브 충돌 해제 & 괄호 무결성 확인
    assert "glasses" not in sc_clothed.actual_negative.lower()
    assert "choker" not in sc_clothed.actual_negative.lower()
    assert "bad anatomy" in sc_clothed.actual_negative.lower()
    assert sc_clothed.actual_negative.count("(") == sc_clothed.actual_negative.count(")")

    # 2) 탈의 씬: 얼굴/헤어 소품(round glasses)만 유지하고 의상성 소품(red silk choker)은 제외
    e_nude = PoseEntry(80, "lying on bed, nude, solo focus", "h_scenes", required_outfit="nude")
    sc_nude = compose_scene_prompt(
        entry=e_nude, prefix="rina", tag="080",
        base_positive="masterpiece", char_prompt=cfg.char_prompt,
        nude_positive=cfg.nude_positive,
        negative_prompt="lowres, glasses, choker",
        sampler_name="DPM++ 2M",
        signature_accessories=cfg.signature_accessories,
    )
    assert "round glasses" in sc_nude.active_char_prompt
    assert "red silk choker" not in sc_nude.active_char_prompt
    assert "glasses" not in sc_nude.actual_negative.lower()


# ── 3. precheck.py 전 포즈 조립 시뮬레이션 + 배경 매핑 정합성 ──


def test_precheck_pose_assembly_simulation_and_background_mapping(tmp_path: Path) -> None:
    """precheck 가 착의 고정, 탈의 의상 누수, 테마 스왑, pose_background_mapping.json 정합성을 검증해야 한다."""
    pose_db_raw = {
        "emotions": {
            "0": {"label": "미소", "prompt": "standing, gentle smile, clean background", "required_outfit": "default"},
        },
        "events": {
            "41": {"label": "수영복", "prompt": "at beach, smiling", "required_outfit": "swimsuit"},
        },
        "h_scenes": {
            "80": {"label": "정상위", "prompt": "missionary on bed", "required_outfit": "nude"},
        },
    }
    (tmp_path / "pose_database.json").write_text(json.dumps(pose_db_raw), encoding="utf-8")

    roster_dir = tmp_path / "projects" / "demo"
    chars_dir = roster_dir / "characters"
    assets_dir = roster_dir / "assets"
    refs_dir = roster_dir / "references"
    chars_dir.mkdir(parents=True)
    assets_dir.mkdir(parents=True)

    (roster_dir / "background.json").write_text(
        json.dumps({"default": "sunny campus courtyard", "bedroom": "warm cozy bedroom"}),
        encoding="utf-8",
    )
    (roster_dir / "pose_background_mapping.json").write_text(
        json.dumps({"default_background": "default", "pose_mappings": {"000": "default", "080": "bedroom"}}),
        encoding="utf-8",
    )
    (chars_dir / "hana.json").write_text(
        json.dumps({
            "prefix": "hana",
            "char_prompt": "1girl, solo, black hair, school uniform, pleated skirt",
            "nude_positive": "1girl, black hair, slender body, bare shoulders",
            "outfits": {"swimsuit": "blue two-piece bikini swimsuit"},
        }),
        encoding="utf-8",
    )

    roster = RosterPaths("demo", chars_dir, assets_dir, refs_dir)
    rep_ok = run_precheck(tmp_path, roster)
    assert rep_ok.passed is True, rep_ok.errors
    assert rep_ok.total_combinations_checked == 3

    # 1) 탈의 프롬프트에 주요 의상('school uniform') 누수 주입 -> 오류 감지
    (chars_dir / "hana.json").write_text(
        json.dumps({
            "prefix": "hana",
            "char_prompt": "1girl, solo, black hair, school uniform, pleated skirt",
            "nude_positive": "1girl, black hair, school uniform, bare shoulders",
            "outfits": {"swimsuit": "blue two-piece bikini swimsuit"},
        }),
        encoding="utf-8",
    )
    rep_leak = run_precheck(tmp_path, roster)
    assert rep_leak.passed is False
    assert any("알몸전환" in err and "school uniform" in err for err in rep_leak.errors)

    # 2) pose_background_mapping.json 에 미존재 프리셋('ghost_room') 매핑 -> 오류 감지
    (roster_dir / "pose_background_mapping.json").write_text(
        json.dumps({"default_background": "default", "pose_mappings": {"080": "ghost_room"}}),
        encoding="utf-8",
    )
    rep_bg = run_precheck(tmp_path, roster)
    assert rep_bg.passed is False
    assert any("배경매핑규칙" in err and "ghost_room" in err for err in rep_bg.errors)


# ── 4. GENERATION_OPTION_SPECS 삼위일체(Trinity Sync) 계약 테스트 ──


def test_trinity_sync_option_specs_models_cli_gui() -> None:
    """GENERATION_OPTION_SPECS ↔ GenerationOptions ↔ CLI 파서 ↔ GUI 설정/브릿지 간 100% 정합성을 보장한다."""
    assert len(GENERATION_OPTION_SPECS) == len(OPTION_SPEC_OBJECTS) >= 15

    # 1) GenerationOptions 필드 및 기본값 동기화
    opt_defaults = {f.name: f.default for f in dataclass_fields(GenerationOptions)}
    for spec in OPTION_SPEC_OBJECTS:
        assert spec.field in opt_defaults, f"GenerationOptions 누락 필드: {spec.field}"
        assert opt_defaults[spec.field] == spec.default, (
            f"GenerationOptions.{spec.field} 기본값 불일치: "
            f"model={opt_defaults[spec.field]!r} vs spec={spec.default!r}"
        )

    # 2) CLI 파서(sd_batch_generator.build_parser) 플래그·dest·기본값·choices 동기화
    parser = sd_batch_generator.build_parser(add_help=False)
    actions_by_flag: dict[str, Any] = {}
    for action in parser._actions:
        for opt_str in action.option_strings:
            actions_by_flag[opt_str] = action

    for spec in OPTION_SPEC_OBJECTS:
        assert spec.cli_flag in actions_by_flag, f"CLI 플래그 누락: {spec.cli_flag}"
        action = actions_by_flag[spec.cli_flag]
        assert action.dest == spec.field, (
            f"CLI {spec.cli_flag} dest 불일치: {action.dest!r} != {spec.field!r}"
        )
        assert action.default == spec.default, (
            f"CLI {spec.cli_flag} 기본값 불일치: {action.default!r} != {spec.default!r}"
        )
        if spec.choices:
            assert tuple(action.choices or ()) == spec.choices, (
                f"CLI {spec.cli_flag} choices 불일치: {action.choices!r} != {spec.choices!r}"
            )

    # 3) GUI DEFAULT_SETTINGS ↔ OPTION_SPEC_OBJECTS 양방향 키·기본값 동기화
    gui_specs = get_gui_option_specs()
    spec_gui_keys = {s.gui_key for s in gui_specs}
    assert spec_gui_keys == set(DEFAULT_SETTINGS.keys()), (
        f"GUI DEFAULT_SETTINGS 드리프트 감지: "
        f"only_in_gui={set(DEFAULT_SETTINGS.keys()) - spec_gui_keys}, "
        f"only_in_spec={spec_gui_keys - set(DEFAULT_SETTINGS.keys())}"
    )
    for spec in gui_specs:
        assert DEFAULT_SETTINGS[spec.gui_key] == spec.effective_gui_default, (
            f"GUI DEFAULT_SETTINGS[{spec.gui_key!r}] 기본값 불일치"
        )

    # 4) gui.runner_bridge.build_base_options() 비기본값 전파 검증
    custom_gui_cfg = {
        "steps": 42,
        "sampler": "Euler a",
        "scheduler": "SGM Uniform",
        "cfg_scale": 7.5,
        "use_reference": False,  # inverted -> no_ref=True
        "naming": "label",
        "overwrite": False,
        "auto_qc": True,
        "strict_censor_qc": True,
        "max_retries": 5,
        "censor": True,
        "censor_style": "manga_bar",
    }
    built = build_base_options(
        SimpleNamespace(ckpt_profiles={}),  # type: ignore[arg-type]
        custom_gui_cfg,
        mode_val="all",
        codes_val=None,
        ckpt_name=None,
    )
    assert built.steps == 42
    assert built.sampler == "Euler a"
    assert built.scheduler == "SGM Uniform"
    assert built.cfg_scale == 7.5
    assert built.no_ref is True
    assert built.naming == "label"
    assert built.overwrite is False
    assert built.auto_qc is True
    assert built.strict_censor_qc is True
    assert built.max_retries == 5
    assert built.censor is True
    assert built.censor_style == "manga_bar"


# ── 5. Kiro 독립 도구 2종 (audit_asset_alignment.py & export_context.py) ──


def test_audit_asset_alignment_report_and_two_pass_safe_rename(tmp_path: Path) -> None:
    """audit_asset_alignment.py 가 완비율/누락/불일치를 산출하고 --apply-rename 시 2-Pass 리네임을 수행해야 한다."""
    pose_db_raw = {
        "emotions": {
            "0": {"label": "평상", "prompt": "standing, neutral"},
            "1": {"label": "미소", "prompt": "standing, smile"},
        },
        "h_scenes": {
            "80": {"label": "정상위", "prompt": "missionary on bed"},
        },
    }
    (tmp_path / "pose_database.json").write_text(json.dumps(pose_db_raw), encoding="utf-8")

    roster_dir = tmp_path / "projects" / "demo"
    chars_dir = roster_dir / "characters"
    char_assets = roster_dir / "assets" / "rei"
    origin_assets = roster_dir / "assets" / "rei_origin"
    chars_dir.mkdir(parents=True)
    char_assets.mkdir(parents=True)
    origin_assets.mkdir(parents=True)

    (chars_dir / "rei.json").write_text(
        json.dumps({"prefix": "rei", "char_prompt": "1girl, silver hair", "gender": "female"}),
        encoding="utf-8",
    )

    # 00: 정상 일치, 80: 구 라벨 불일치 (+ _origin 백업 존재), 01: 누락, 999: 고아 파일
    (char_assets / "rei_00_평상.webp").write_bytes(b"ok00")
    (char_assets / "rei_80_구라벨정상위.webp").write_bytes(b"censored80")
    (origin_assets / "rei_80_구라벨정상위.webp").write_bytes(b"origin80")
    (char_assets / "rei_999_고아.webp").write_bytes(b"orphan")

    # 1) 기본 읽기 전용 dry-run
    dry_reports = audit_roster_alignment("demo", apply_rename=False, base_dir=tmp_path)
    assert len(dry_reports) == 1
    rep = dry_reports[0]
    assert rep["present_count"] == 2
    assert rep["expected_count"] == 3
    assert rep["completion_rate"] == pytest.approx(66.7)
    assert rep["origin_count"] == 1
    assert len(rep["mismatches"]) == 1
    assert rep["mismatches"][0]["expected_label"] == "정상위"
    assert [m["code"] for m in rep["missing_poses"]] == ["01"]
    assert len(rep["orphan_files"]) == 1
    assert (char_assets / "rei_80_구라벨정상위.webp").is_file()  # dry-run 이므로 미변경

    # 2) apply_rename=True (2-Pass 안전 리네임: assets/{char} 및 {char}_origin 동시 동기화)
    fix_reports = audit_roster_alignment("demo", apply_rename=True, base_dir=tmp_path)
    rep_fix = fix_reports[0]
    assert len(rep_fix["fixed_files"]) == 1
    assert len(rep_fix["fixed_origin_files"]) == 1
    assert (char_assets / "rei_80_정상위.webp").read_bytes() == b"censored80"
    assert (origin_assets / "rei_80_정상위.webp").read_bytes() == b"origin80"
    assert not (char_assets / "rei_80_구라벨정상위.webp").exists()

    # 3) CLI --roster all 종합 요약 모드 정상 동작 확인
    from scripts.audit_asset_alignment import main as audit_main

    assert audit_main(["--roster", "all", "--base-dir", str(tmp_path)]) == 0


def test_export_context_generates_three_tier_snapshot_standalone(tmp_path: Path) -> None:
    """scripts/export_context.py 가 plepa 의존 없이 Kiro 3-Tier 스냅샷을 생성하고 .gitignore 에 등록되어 있어야 한다."""
    out_dir = tmp_path / "snapshot_out"
    info = export_context_snapshot(root=Path.cwd(), out_dir=out_dir, run_tests=False, full_code=False)

    expected_files = (
        "00_README_FOR_AI.md",
        "10_TIER1_CONSTITUTION.md",
        "20_TIER2_CORE_LOGIC.md",
        "30_TIER3_RUNTIME.md",
    )
    assert tuple(info["files"]) == expected_files
    for fname in expected_files:
        fpath = out_dir / fname
        assert fpath.is_file()
        text = fpath.read_text(encoding="utf-8")
        assert info["snapshot_id"] in text
        assert len(text) > 100
    assert (out_dir / ".hashes.json").is_file()

    # Tier 2 에 Kiro GenerationOptions 및 option_specs 반영 확인
    t2_text = (out_dir / "20_TIER2_CORE_LOGIC.md").read_text(encoding="utf-8")
    assert "GenerationOptions" in t2_text
    assert "generator/option_specs.py" in t2_text

    # .gitignore 에 context_snapshot/ 등록 확인
    gitignore_text = Path(".gitignore").read_text(encoding="utf-8")
    assert "context_snapshot/" in gitignore_text

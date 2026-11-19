"""Wave 2 코어 분해 검증: scene_composer 순수 도메인 규칙, GenerationOptions/RunHooks, V2 섹션 SSOT, HTTP 폴백·Auto-QC 리롤 및 로깅."""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import requests
from PIL import Image

from generator.config import configure_logging
from generator.models import (
    ControlNetSpec,
    GenerationOptions,
    PoseDatabase,
    PoseEntry,
    ReferenceImage,
    RosterPaths,
    RunHooks,
)
from generator.pose_db import (
    V2_BUILTIN_SECTIONS,
    expand_v2_mode,
    parse_pose_db,
)
from generator.roster import _resolve_default_mode
from generator.scene_composer import compose_scene_prompt, should_apply_background


def test_compose_scene_emotion_and_intrinsic_background() -> None:
    """감정 1인 씬 배경 치환 및 고유 배경(bed) 보유 이벤트 씬 배경 보존 검증."""
    emotion_entry = PoseEntry(0, "standing, smile, clean background", "emotions", label_override="미소")
    sc_emotion = compose_scene_prompt(
        entry=emotion_entry,
        prefix="rei",
        tag="00",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair, black dress",
        negative_prompt="lowres, 1boy, male",
        sampler_name="DPM++ 2M",
        active_bg="sunset classroom",
    )
    assert sc_emotion.apply_bg is True
    assert sc_emotion.bg_applied == "sunset classroom"
    assert "sunset classroom" in sc_emotion.full_prompt
    assert "clean background" not in sc_emotion.full_prompt
    assert sc_emotion.is_interactive is False

    bed_event = PoseEntry(40, "sitting on bed, reading book", "events", required_outfit="default")
    assert should_apply_background(bed_event, bed_event.prompt) is False
    sc_bed = compose_scene_prompt(
        entry=bed_event,
        prefix="rei",
        tag="40",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair, black dress",
        negative_prompt="lowres",
        sampler_name="DPM++ 2M",
        active_bg="sunset classroom",
    )
    assert sc_bed.apply_bg is False
    assert sc_bed.bg_applied is None
    assert "sunset classroom" not in sc_bed.full_prompt
    assert "black dress" in sc_bed.full_prompt


def test_compose_scene_nude_bath_and_wedding_theme_and_cn_weight() -> None:
    """탈의/목욕 씬 의상 제거·욕조 네거티브, 웨딩 테마 스왑 및 ControlNet 가중치(0.5) 완화 검증."""
    base_cn = {"enabled": True, "weight": 0.75, "model": "ip-adapter"}

    bath_entry = PoseEntry(
        45,
        "soaking in bath, wet skin, solo",
        "events",
        required_outfit="nude",
    )
    sc_bath = compose_scene_prompt(
        entry=bath_entry,
        prefix="rei",
        tag="45",
        base_positive="masterpiece BREAK 1girl, solo, pleated skirt",
        char_prompt="silver hair, school uniform, blue necktie",
        negative_prompt="lowres",
        sampler_name="DPM++ 2M",
        nude_positive="1girl, silver hair, bare shoulders",
        base_controlnet_unit=base_cn,
    )
    assert sc_bath.is_nude_scene is True
    assert sc_bath.is_solo_scene is True
    assert "school uniform" not in sc_bath.full_prompt
    assert "pleated skirt" not in sc_bath.full_prompt
    assert "completely nude" in sc_bath.full_prompt
    assert "rubber duck" in sc_bath.actual_negative
    assert sc_bath.controlnet_units[0]["weight"] == 0.5

    wedding_entry = PoseEntry(
        46,
        "holding bouquet, cathedral",
        "events",
        required_outfit="wedding",
    )
    sc_wed = compose_scene_prompt(
        entry=wedding_entry,
        prefix="rei",
        tag="46",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair, black jacket",
        negative_prompt="lowres, white dress, long skirt, tiara",
        sampler_name="DPM++ 2M",
        outfits={"wedding": "ornate white wedding dress, bridal veil"},
        base_controlnet_unit=base_cn,
    )
    assert "ornate white wedding dress" in sc_wed.full_prompt
    assert "black jacket" not in sc_wed.full_prompt
    assert "white dress" not in sc_wed.actual_negative.split(",")[1]
    assert "plunging neckline" in sc_wed.actual_negative
    assert sc_wed.controlnet_units[0]["weight"] == 0.5


def test_compose_scene_interactive_rear_horizontal_offscreen_aftermath_otokonoko() -> None:
    """2인 상호작용(후면/가로/오프스크린/사후여운/오토코노코) 분기 규칙 검증."""
    # 1. 후면 + 가로 침대 2인 성인 씬
    rear_h_entry = PoseEntry(
        88,
        "doggystyle on bed, lying on stomach, partner from behind",
        "h_scenes",
    )
    sc_rear = compose_scene_prompt(
        entry=rear_h_entry,
        prefix="rei",
        tag="88",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair",
        negative_prompt="lowres, 1boy, male, multiple characters",
        sampler_name="DPM++ 2M",
    )
    assert sc_rear.is_interactive is True
    assert sc_rear.is_nude_scene is True
    assert "hetero, 1boy, faceless male" in sc_rear.full_prompt
    assert "BREAK (single faceless male partner:1.15)" in sc_rear.full_prompt
    assert "(from behind:1.15)" in sc_rear.full_prompt
    assert "spitroast" in sc_rear.actual_negative
    assert "standing apart" in sc_rear.actual_negative

    # 2. 오프스크린 파트너 POV 씬
    pov_entry = PoseEntry(
        35,
        "lying on back, hands pressed on mattress, off-screen partner",
        "poses",
        required_outfit="default",
    )
    sc_pov = compose_scene_prompt(
        entry=pov_entry,
        prefix="rei",
        tag="35",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair",
        negative_prompt="lowres, 1boy",
        sampler_name="DPM++ 2M",
    )
    assert sc_pov.is_interactive is True
    assert sc_pov.is_offscreen_partner is True
    assert "BREAK (single faceless male partner:1.15)" not in sc_pov.full_prompt
    assert "male lower body" in sc_pov.actual_negative

    # 3. 사후여운 씬 (솔로 보장)
    after_entry = PoseEntry(
        101,
        "aftermath, lying on bed, exhausted, panting",
        "h_scenes",
    )
    sc_after = compose_scene_prompt(
        entry=after_entry,
        prefix="rei",
        tag="101",
        base_positive="masterpiece BREAK 1girl, solo",
        char_prompt="silver hair",
        negative_prompt="lowres",
        sampler_name="DPM++ 2M",
    )
    assert sc_after.is_aftermath is True
    assert sc_after.is_interactive is False
    assert "BREAK (single faceless male partner:1.15)" not in sc_after.full_prompt
    assert "faceless male, multiple characters, extra face:1.5" in sc_after.actual_negative

    # 4. 오토코노코 2인 씬
    otk_entry = PoseEntry(
        150,
        "missionary on bed, legs wrapped",
        "scenes_otokonoko",
        prompt_otokonoko="otokonoko missionary on bed",
    )
    sc_otk = compose_scene_prompt(
        entry=otk_entry,
        prefix="ast",
        tag="150",
        base_positive="masterpiece BREAK 1boy, otokonoko, solo",
        char_prompt="pink hair",
        negative_prompt="lowres, 2boys",
        sampler_name="DPM++ 2M",
        gender="otokonoko",
    )
    assert sc_otk.is_interactive is True
    assert "yaoi, 2boys, faceless male" in sc_otk.full_prompt
    assert "hetero" not in sc_otk.full_prompt
    assert "otokonoko missionary on bed" in sc_otk.full_prompt


def test_compose_scene_clamp_weights_opt_in_vs_default() -> None:
    """clamp_weights 기본값(False)은 강조 가중치(1.35)를 보존하고, True 일 때만 1.15로 제한한다."""
    entry = PoseEntry(0, "standing, smile", "emotions")
    sc_default = compose_scene_prompt(
        entry=entry,
        prefix="rei",
        tag="00",
        base_positive="masterpiece",
        char_prompt="(deep burgundy hair:1.35)",
        negative_prompt="lowres",
        sampler_name="DPM++ 2M",
        enable_clamp_weights=False,
    )
    assert "(deep burgundy hair:1.35)" in sc_default.full_prompt

    sc_clamped = compose_scene_prompt(
        entry=entry,
        prefix="rei",
        tag="00",
        base_positive="masterpiece",
        char_prompt="(deep burgundy hair:1.35)",
        negative_prompt="lowres",
        sampler_name="DPM++ 2M",
        enable_clamp_weights=True,
    )
    assert "(deep burgundy hair:1.15)" in sc_clamped.full_prompt


def test_v2_section_expansion_ssot_excludes_legacy_genit_actions() -> None:
    """expand_v2_mode 와 _resolve_default_mode 가 단일 SSOT로 동작하고 genit_actions 를 배제함을 검증."""
    assert "genit_actions" not in V2_BUILTIN_SECTIONS
    raw_db = parse_pose_db({
        "emotions": {"0": "smile"},
        "poses": {"20": "standing"},
        "events": {"40": "reading"},
        "romance": {"60": "holding hands"},
        "h_scenes": {"80": "bed"},
        "special_h": {"130": "special"},
        "scenes_otokonoko": {"150": "otk"},
        "special_otokonoko": {"133": "otk_sp"},
        "event_custom": {"201": "custom"},
    })
    fem_mode = expand_v2_mode("all", raw_db, expand_all_by_gender="female")
    otk_mode = expand_v2_mode("all", raw_db, expand_all_by_gender="otokonoko")
    assert fem_mode == "emotions,poses,events,romance,h_scenes,special_h,event_custom"
    assert otk_mode == "emotions,poses,events,romance,scenes_otokonoko,special_otokonoko,event_custom"
    assert _resolve_default_mode("female", raw_db) == fem_mode
    assert _resolve_default_mode("otokonoko", raw_db) == otk_mode


def test_run_batch_dry_run_equivalence_http_fallback_and_auto_qc_reroll(
    tmp_path: Path, monkeypatch
) -> None:
    """run_batch 의 dry_run 동등성, HTTP 422 폴백(actual_negative 유지) 및 Auto-QC 리롤/경고 집계를 검증."""
    import generator.qa_validator as qv
    import generator.runner as runner

    db = parse_pose_db({
        "emotions": {"0": {"label": "미소", "prompt": "standing, smile, clean background"}},
    })
    entry = db.entries[0]
    dummy_png = runner.make_dummy_png("rei", 0, 2, entry)

    # 1. dry_run=True
    dry_res = runner.run_batch(
        prefix="rei",
        base_positive="masterpiece",
        char_prompt="silver hair",
        negative_prompt="lowres",
        targets=[0],
        db=db,
        save_dir=tmp_path,
        width=2,
        sampler_name="DPM++ 2M",
        active_bg="sunset room",
        dry_run=True,
    )
    assert dry_res.planned == [0]
    assert not (tmp_path / "rei_00.webp").exists()

    # 2. HTTP 422 첫 호출 실패 -> 순정 폴백 성공 + Auto-QC 1회 불량 후 2회차 통과
    calls: list[dict] = []

    def _fake_generate(payload: dict) -> bytes:
        calls.append(dict(payload))
        if len(calls) == 1:
            resp = requests.Response()
            resp.status_code = 422
            raise requests.exceptions.HTTPError(response=resp)
        return dummy_png

    qc_calls = {"count": 0}

    def _fake_qc(**kwargs):
        qc_calls["count"] += 1
        assert "sunset room" in (kwargs.get("prompt") or "")
        if qc_calls["count"] == 1:
            return False, "1인 씬 손 증식(3개 이상) 감지", {"hands": 3}
        return True, "", {"hands": 2}

    monkeypatch.setattr(runner, "generate_image", _fake_generate)
    monkeypatch.setattr(qv, "validate_generated_image", _fake_qc)

    real_res = runner.run_batch(
        prefix="rei",
        base_positive="masterpiece",
        char_prompt="silver hair",
        negative_prompt="lowres",
        targets=[0],
        db=db,
        save_dir=tmp_path,
        width=2,
        sampler_name="DPM++ 2M",
        active_bg="sunset room",
        enable_freeu=True,
        auto_qc=True,
        max_retries=2,
        overwrite=True,
    )
    assert real_res.success == [0]
    assert real_res.qa_warnings == []
    # 첫 시도(422) + 폴백 성공(QC 1차 불량) + 리롤 시도(QC 2차 통과) = 총 3회 generate_image 호출
    assert len(calls) == 3
    assert calls[1]["negative_prompt"] == calls[0]["negative_prompt"]
    assert "FreeU Integrated" not in calls[1].get("alwayson_scripts", {})
    assert (tmp_path / "rei_00.webp").is_file()


def test_run_all_chars_hooks_failure_cancellation_and_qa_aggregation(
    tmp_path: Path, monkeypatch
) -> None:
    """run_all_chars 의 GenerationOptions/RunHooks 연동, 실패/QA 경고 집계 및 중도 취소 검증."""
    import generator.qa_validator as qv
    import generator.runner as runner

    project_dir = tmp_path / "proj"
    chars_dir = project_dir / "characters"
    assets_dir = project_dir / "assets"
    refs_dir = project_dir / "references"
    chars_dir.mkdir(parents=True)
    assets_dir.mkdir(parents=True)

    (chars_dir / "a_ok.json").write_text(
        json.dumps({"prefix": "aok", "char_prompt": "silver hair"}), encoding="utf-8"
    )
    (chars_dir / "b_bad.json").write_text("{invalid_json", encoding="utf-8")
    (chars_dir / "c_second.json").write_text(
        json.dumps({"prefix": "csc", "char_prompt": "blue hair"}), encoding="utf-8"
    )

    roster = RosterPaths("proj", chars_dir, assets_dir, refs_dir)
    monkeypatch.setattr(runner, "open_in_explorer", lambda _p: None)
    monkeypatch.setattr(runner, "fetch_vram_peak", lambda: None)
    monkeypatch.setattr(
        qv,
        "validate_generated_image",
        lambda **_kw: (False, "테스트 QC 불량", {"hands": 5}),
    )
    dummy_png = runner.make_dummy_png("aok", 0, 3, runner.load_pose_db(Path.cwd()).entries[0])
    monkeypatch.setattr(runner, "generate_image", lambda _p: dummy_png)
    monkeypatch.setattr(runner, "_setup_sampler", lambda _o, _d, _m: "DPM++ 2M")

    started_chars: list[str] = []
    completed_chars: list[tuple[str, int]] = []
    pose_events: list[tuple[str, int]] = []

    hooks = RunHooks(
        on_char_start=lambda _i, _t, name, _cfg: started_chars.append(name),
        char_progress_callback=lambda _i, _t, name, _cfg, code, _lbl, _p, _c, _tot: pose_events.append((name, code)),
        on_char_complete=lambda _i, _t, name, _cfg, _res, rc: completed_chars.append((name, rc)),
    )
    opts = GenerationOptions(
        mode="all",
        codes="0",
        no_ref=True,
        auto_qc=True,
        max_retries=1,
        overwrite=True,
    )

    rc = runner.run_all_chars(roster, options=opts, hooks=hooks)
    assert rc == 1  # b_bad 실패로 인해 1 반환
    summary = hooks.last_all_result
    assert summary is not None
    assert summary.succeeded == ["a_ok", "c_second"]
    assert summary.failed == ["b_bad"]
    assert len(summary.qa_issues) == 2
    assert started_chars == ["a_ok", "b_bad", "c_second"][::2] or started_chars == ["a_ok", "c_second"]
    assert ("a_ok", 0) in pose_events and ("c_second", 0) in pose_events
    assert completed_chars == [("a_ok", 0), ("c_second", 0)]

    # 취소 동작 검증: 첫 캐릭터 완료 즉시 취소
    cancel_flag = {"stop": False}
    cancel_hooks = RunHooks(
        is_cancelled=lambda: cancel_flag["stop"],
        on_char_complete=lambda *_a: cancel_flag.__setitem__("stop", True),
    )
    rc_cancel = runner.run_all_chars(
        roster,
        options=GenerationOptions(mode="all", codes="0", mock=True, overwrite=True),
        hooks=cancel_hooks,
    )
    assert rc_cancel == 1
    assert cancel_hooks.last_all_result is not None
    assert cancel_hooks.last_all_result.aborted is True
    assert cancel_hooks.last_all_result.succeeded == ["a_ok"]


def test_runner_structural_invariants_and_logging_handler() -> None:
    """run_batch 함수 길이가 150줄 이하이고 runner.py 내 직접 print() 호출이 0회임을 검증."""
    runner_path = Path("generator/runner.py")
    tree = ast.parse(runner_path.read_text(encoding="utf-8"))

    run_batch_lines = 0
    print_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_batch":
            run_batch_lines = (node.end_lineno or node.lineno) - node.lineno + 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            print_calls += 1

    assert 0 < run_batch_lines <= 150, f"run_batch lines={run_batch_lines} (> 150)"
    assert print_calls == 0, f"runner.py still has {print_calls} print() calls"

    # configure_logging 중복 호출 시 핸들러가 중복 등록되지 않고 커스텀 핸들러가 정상 수신하는지 검증
    records: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    custom_h = _ListHandler()
    pkg_logger = configure_logging(extra_handler=custom_h)
    count_before = len(pkg_logger.handlers)
    configure_logging(extra_handler=custom_h)
    assert len(pkg_logger.handlers) == count_before
    try:
        logging.getLogger("generator.runner").info("WAVE2_LOG_TEST")
        assert "WAVE2_LOG_TEST" in records
    finally:
        pkg_logger.removeHandler(custom_h)

"""Wave 1 정합성 검증: 체크포인트 수치 우선순위, 검열 SSOT·경로 보안, GUI V2 및 동적 URL/BOM."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


def test_checkpoint_priority_order_and_no_quality_merge() -> None:
    """Config < CheckpointProfile < CharacterConfig < CLI/GUI 4계층 우선순위 및 quality 비병합 검증."""
    from generator.checkpoint import (
        CheckpointProfile,
        resolve_generation_params,
    )
    from generator.config import CFG_SCALE, IMAGE_SIZE, STEPS

    prof = CheckpointProfile(
        key="waiIllustriousSDXL_v170.safetensors",
        name="WAI-Illustrious-SDXL v17",
        width=1024,
        height=1344,
        steps=25,
        cfg=6.0,
        sampler="euler_ancestral",
        scheduler="normal",
        clip_skip=2,
        quality_positive="SHOULD_NOT_BE_MERGED_POS",
        quality_negative="SHOULD_NOT_BE_MERGED_NEG",
    )

    # Layer 1: Config fallback
    p1 = resolve_generation_params(fallback_sampler="DPM++ 2M Karras")
    assert p1.steps == STEPS
    assert p1.cfg_scale == float(CFG_SCALE)
    assert p1.sampler == "DPM++ 2M Karras"
    assert (p1.width, p1.height) == IMAGE_SIZE
    assert p1.clip_skip is None

    # Layer 2: CheckpointProfile 적용 + 샘플러/스케줄러 정규화 + quality 비포함
    p2 = resolve_generation_params(fallback_sampler="DPM++ 2M Karras", ckpt_profile=prof)
    assert p2.steps == 25
    assert p2.cfg_scale == 6.0
    assert p2.sampler == "Euler a"
    assert p2.scheduler == "Automatic"
    assert (p2.width, p2.height) == (1024, 1344)
    assert p2.clip_skip == 2
    assert not hasattr(p2, "quality_positive") and not hasattr(p2, "quality_negative")

    # Layer 3: CharacterConfig 오버라이드
    p3 = resolve_generation_params(
        fallback_sampler="DPM++ 2M Karras",
        ckpt_profile=prof,
        char_overrides={"steps": 28, "cfg_scale": 5.5, "scheduler": "karras", "clip_skip": 1},
    )
    assert p3.steps == 28
    assert p3.cfg_scale == 5.5
    assert p3.scheduler == "Karras"
    assert p3.sampler == "Euler a"  # prof 유지
    assert p3.clip_skip == 1

    # Layer 4: CLI / GUI 명시 인자 최우선
    p4 = resolve_generation_params(
        fallback_sampler="DPM++ 2M Karras",
        ckpt_profile=prof,
        char_overrides={"steps": 28, "cfg_scale": 5.5, "sampler": "euler", "scheduler": "karras"},
        explicit_steps=40,
        explicit_cfg_scale=7.5,
        explicit_sampler="dpmpp_2m_sde",
        explicit_clip_skip=3,
    )
    assert p4.steps == 40
    assert p4.cfg_scale == 7.5
    assert p4.sampler == "DPM++ 2M SDE"
    assert p4.scheduler == "Karras"
    assert p4.clip_skip == 3


def test_txt2img_payload_clip_skip_override_and_default_ten_keys() -> None:
    """clip_skip 미지정 시 기존 10개 키를 유지하고, 지정 시 CLIP_stop_at_last_layers 를 주입한다."""
    from generator.webui_client import build_txt2img_payload

    plain = build_txt2img_payload(prompt="p", negative_prompt="n", sampler_name="Euler a")
    assert len(plain) == 10
    assert "override_settings" not in plain

    with_clip = build_txt2img_payload(
        prompt="p", negative_prompt="n", sampler_name="Euler a", clip_skip=2
    )
    assert with_clip["override_settings"] == {"CLIP_stop_at_last_layers": 2}
    assert with_clip["override_settings_restore_afterwards"] is True


def test_execute_wires_ckpt_profile_numerical_layer_without_merging_quality(tmp_path: Path, monkeypatch) -> None:
    """runner.execute() 가 active_ckpt 수치를 실제 payload 로 전달하되 quality 태그는 병합하지 않음을 검증."""
    import generator.runner as runner
    from generator.checkpoint import CheckpointProfile
    from generator.models import RosterPaths

    chars_dir = tmp_path / "chars"
    assets_dir = tmp_path / "assets"
    refs_dir = tmp_path / "refs"
    chars_dir.mkdir()
    assets_dir.mkdir()
    roster = RosterPaths("test_roster", chars_dir, assets_dir, refs_dir)

    prof = CheckpointProfile(
        key="test_wai",
        name="Test WAI",
        width=1024,
        height=1344,
        steps=22,
        cfg=5.5,
        sampler="euler_ancestral",
        scheduler="karras",
        clip_skip=2,
        quality_positive="UNIQUE_CKPT_QUALITY_POS",
        quality_negative="UNIQUE_CKPT_QUALITY_NEG",
    )

    captured_payloads: list[dict] = []
    png_buf = runner.make_dummy_png("tst", 0, 3, runner.load_pose_db(Path.cwd()).entries[0])

    monkeypatch.setattr(runner, "generate_image", lambda p: captured_payloads.append(p) or png_buf)
    monkeypatch.setattr(runner, "fetch_vram_peak", lambda: None)
    monkeypatch.setattr(runner, "open_in_explorer", lambda _p: None)
    monkeypatch.setattr(runner, "_setup_sampler", lambda _a, _d, _m: "DPM++ 2M Karras")

    args = argparse.Namespace(
        prefix="tst",
        char_prompt="silver hair",
        profile=None,
        custom_neg="",
        mode="all",
        codes="0",
        dry_run=False,
        mock=False,
        ref_image=None,
        no_ref=True,
        ref_weight=0.7,
        cn_module=None,
        cn_model=None,
        enable_freeu=False,
        freeu_b1=1.1,
        freeu_b2=1.2,
        freeu_s1=0.9,
        freeu_s2=0.2,
        enable_adetailer=False,
        enable_lightning=False,
        lightning_steps=2,
        lightning_cfg=1.0,
        steps=None,
        sampler=None,
        scheduler=None,
        cfg_scale=None,
        active_ckpt=prof,
    )
    rc = runner.execute(args, roster)
    assert rc == 0
    assert len(captured_payloads) == 1
    sent = captured_payloads[0]
    assert sent["steps"] == 22
    assert sent["cfg_scale"] == 5.5
    assert sent["sampler_name"] == "Euler a"
    assert sent["scheduler"] == "Karras"
    assert (sent["width"], sent["height"]) == (1024, 1344)
    assert sent["override_settings"] == {"CLIP_stop_at_last_layers": 2}
    assert "UNIQUE_CKPT_QUALITY_POS" not in sent["prompt"]
    assert "UNIQUE_CKPT_QUALITY_NEG" not in sent["negative_prompt"]


def test_is_censor_target_boundaries_and_non_exposure() -> None:
    """검열 대상 SSOT 경계값 및 비노출 씬 예외 처리 검증."""
    from generator.censor import NON_EXPOSURE_H_POSES, is_censor_target

    assert is_censor_target(79) is False
    assert is_censor_target(80) is True
    assert is_censor_target(135) is True
    assert is_censor_target(136) is False
    assert is_censor_target(149) is False
    assert is_censor_target(150) is True
    assert is_censor_target(200) is True
    assert is_censor_target(201) is False

    for code_str in NON_EXPOSURE_H_POSES:
        assert is_censor_target(int(code_str), section="h_scenes", is_nude_scene=True) is False

    assert is_censor_target(201, is_nude_scene=True) is True
    assert is_censor_target(45, section="events", is_nude_scene=False) is False


def test_censor_studio_path_traversal_and_v2_and_origin_backup(tmp_path: Path) -> None:
    """censor_studio 의 Path Traversal 차단, V2 H-씬 필터링, _origin 백업 경로 통일 검증."""
    from generator.censor import get_origin_backup_path
    from tools.censor_studio import get_image_list, resolve_safe_target_path

    assets_dir = tmp_path / "assets"
    char_dir = assets_dir / "hero"
    origin_dir = assets_dir / "hero_origin"
    char_dir.mkdir(parents=True)
    origin_dir.mkdir(parents=True)
    outside = tmp_path / "secret.webp"
    outside.write_bytes(b"secret")

    def _touch_img(p: Path) -> None:
        Image.new("RGB", (32, 32), (255, 255, 255)).save(p, "WEBP")

    _touch_img(char_dir / "hero_045_이벤트.webp")
    _touch_img(char_dir / "hero_062_로맨스.webp")
    _touch_img(char_dir / "hero_080_정상위.webp")
    _touch_img(char_dir / "hero_101_여운.webp")
    _touch_img(char_dir / "hero_130_특수.webp")
    _touch_img(origin_dir / "hero_080_정상위.webp")

    # 경로 탈출 차단
    valid_child = char_dir / "hero_080_정상위.webp"
    assert resolve_safe_target_path(assets_dir, str(valid_child)) == valid_child.resolve()
    assert resolve_safe_target_path(assets_dir, str(assets_dir / ".." / "secret.webp")) is None
    assert resolve_safe_target_path(assets_dir, f"{assets_dir}%2f..%2fsecret.webp") is None
    assert resolve_safe_target_path(assets_dir, str(outside)) is None

    # V2 H-씬 필터링 및 _origin 제외
    items = get_image_list(str(assets_dir), filter_h=True)
    names = [item["filename"] for item in items]
    assert names == ["hero_080_정상위.webp", "hero_130_특수.webp"]

    # _origin SSOT 경로
    assert get_origin_backup_path(valid_child) == origin_dir / "hero_080_정상위.webp"


def test_gui_v2_mode_resolution_and_bom_settings(tmp_path: Path) -> None:
    """GUI V2 섹션 해석 및 UTF-8 BOM 포함 설정/배경 JSON 로드 검증."""
    import kiro_gui
    from generator.prompt import resolve_background

    secs = {"emotions", "poses", "events", "romance", "h_scenes", "special_h", "scenes_otokonoko", "special_otokonoko"}
    assert kiro_gui.resolve_gui_mode_and_codes("h_scenes,special_h", "female", secs) == ("h_scenes,special_h", None)
    assert kiro_gui.resolve_gui_mode_and_codes("h_scenes,special_h", "otokonoko", secs) == ("scenes_otokonoko,special_otokonoko", None)
    assert kiro_gui.resolve_gui_mode_and_codes("48-99", "female", secs) == ("h_scenes,special_h", None)
    assert kiro_gui.resolve_gui_mode_and_codes("40-52", "female", secs) == ("all", "40-52")

    # BOM 포함 gui_settings.json 및 background.json
    (tmp_path / "gui_settings.json").write_text('{"steps": 33}', encoding="utf-8-sig")
    loaded_cfg = kiro_gui.load_gui_settings(tmp_path)
    assert loaded_cfg["steps"] == 33

    bg_file = tmp_path / "background.json"
    bg_file.write_text('{"default": "warm cafe interior"}', encoding="utf-8-sig")
    assert resolve_background(None, bg_file) == "warm cafe interior"


def test_reference_controlnet_uses_dynamic_api_url(monkeypatch) -> None:
    """resolve_controlnet_spec() 가 config.API_HOST 변경에 따라 동적 URL 을 호출함을 검증."""
    import generator.config as cfg
    from generator.reference import resolve_controlnet_spec

    monkeypatch.setattr(cfg, "API_HOST", "http://127.0.0.1:9999")
    requested_urls: list[str] = []

    class _MockSess:
        def get(self, url: str, timeout: int = 10):
            requested_urls.append(url)
            if url.endswith("/module_list"):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"module_list": ["ip-adapter_clip_sdxl"]})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"model_list": ["ip-adapter_xl [test]"]})

    spec = resolve_controlnet_spec(session=_MockSess())
    assert spec is not None
    assert requested_urls == [
        "http://127.0.0.1:9999/controlnet/module_list",
        "http://127.0.0.1:9999/controlnet/model_list",
    ]

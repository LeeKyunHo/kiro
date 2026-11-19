"""
Wave P2-B 통합 검증 테스트 (Phase P2.5 하이브리드 검열 엔진 분리 이식 + ADR-99/ADR-103 + strict_censor_qc).
모든 ML 추론 및 파일 시스템 가중치 접근은 오프라인 mock 기반으로 검증한다.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
from PIL import Image
import pytest

import generator.censor as censor_mod
import generator.censor_detect as censor_detect_mod
import generator.censor_seg as censor_seg_mod
from generator.models import BatchResult, GenerationOptions, PoseEntry
import generator.runner as runner_mod
from generator.scene_composer import SceneComposition
from gui.runner_bridge import build_base_options
from gui.settings import DEFAULT_SETTINGS
import sd_batch_generator


def _make_png_bytes(size: tuple[int, int] = (128, 128), color: tuple[int, int, int] = (230, 190, 170)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_skin_image(size: tuple[int, int] = (400, 400)) -> Image.Image:
    """HSV 피부톤 필터를 통과하는 합성 이미지."""
    return Image.new("RGB", size, (235, 195, 175))


def _make_dark_backlight_image(size: tuple[int, int] = (400, 400)) -> Image.Image:
    """역광/야경(HSV 피부율 0%) 합성 이미지."""
    return Image.new("RGB", size, (15, 25, 45))


def _make_scene_stub(
    *,
    is_nude_scene: bool = True,
    is_aftermath: bool = False,
    is_solo_scene: bool = False,
    is_interactive: bool = True,
) -> SceneComposition:
    return SceneComposition(
        pose_prompt="sex, missionary, 1girl, 1boy",
        apply_bg=False,
        bg_applied=None,
        active_base_pos="masterpiece",
        active_char_prompt="1girl",
        full_prompt="masterpiece, 1girl, sex, missionary",
        actual_negative="lowres",
        actual_sampler="DPM++ 2M",
        actual_steps=20,
        actual_cfg=6.0,
        actual_scheduler="Karras",
        actual_freeu=False,
        actual_width=832,
        actual_height=1216,
        is_clothed_scene=False,
        is_nude_scene=is_nude_scene,
        is_aftermath=is_aftermath,
        is_offscreen_partner=False,
        is_solo_scene=is_solo_scene,
        is_interactive=is_interactive,
        is_kiss_scene=False,
        controlnet_units=[],
    )


# ── 1. SAM 2.1 우선 로드 -> MobileSAM 폴백 + YOLO11s-seg 미보유 폴백 + Unload ──


def test_sam21_priority_and_mobilesam_fallback_and_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAM 2.1 실패 시 MobileSAM 으로 폴백하고, unload_censor_models() 가 양쪽 싱글톤을 해제해야 한다."""
    censor_mod.unload_censor_models()

    loaded_paths: list[str] = []

    class FakeSAMCls:
        def __init__(self, model_path: str) -> None:
            loaded_paths.append(model_path)
            if "sam2.1_s.pt" in model_path:
                raise RuntimeError("sam2.1 unavailable offline")
            self.model_path = model_path

    fake_ultralytics = ModuleType("ultralytics")
    fake_ultralytics.SAM = FakeSAMCls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ultralytics", fake_ultralytics)
    monkeypatch.setattr(censor_seg_mod, "_find_existing_weight", lambda *_a, **_kw: None)

    sam = censor_mod.get_sam_model()
    assert sam is not None
    assert sam.model_path == "mobile_sam.pt"
    assert any("sam2.1_s.pt" in p for p in loaded_paths)
    assert any("mobile_sam.pt" in p for p in loaded_paths)

    # 커스텀 YOLO11s-seg 가중치 미보유 시 안전하게 None 폴백
    seg = censor_mod.get_custom_censor_seg_model()
    assert seg is None

    # unload_censor_models() 가 censor 및 censor_seg 싱글톤을 모두 초기화
    censor_mod.unload_censor_models()
    assert censor_mod._SAM_MODEL is None
    assert censor_mod._CENSOR_SEG_MODEL is None
    assert censor_seg_mod._SAM_MODEL is None
    assert censor_seg_mod._CENSOR_SEG_MODEL is None


def test_sam21_succeeds_without_mobilesam_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sam2.1_s.pt 로드 성공 시 MobileSAM 폴백 없이 SAM 2.1 을 그대로 사용해야 한다."""
    censor_mod.unload_censor_models()
    fake_weight = tmp_path / "sam2.1_s.pt"
    fake_weight.write_bytes(b"stub")

    loaded_paths: list[str] = []

    class FakeSAMCls:
        def __init__(self, model_path: str) -> None:
            loaded_paths.append(model_path)
            self.model_path = model_path

    fake_ultralytics = ModuleType("ultralytics")
    fake_ultralytics.SAM = FakeSAMCls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ultralytics", fake_ultralytics)
    monkeypatch.setattr(
        censor_seg_mod,
        "_find_existing_weight",
        lambda fname, **_kw: fake_weight if fname == "sam2.1_s.pt" else None,
    )

    sam = censor_mod.get_sam_model()
    assert sam is not None
    assert len(loaded_paths) == 1
    assert loaded_paths[0] == str(fake_weight)
    censor_mod.unload_censor_models()


# ── 2. ADR-99 다중 스케일 탐지 병합 / 역광 고신뢰 면제 / 하단 엣지 오탐 필터 ──


def test_multiscale_merge_penis_priority_and_backlight_skin_exemption() -> None:
    """중첩 박스(iou>=0.35 or iomin>=0.55)는 penis 라벨 우선으로 병합되고, 고신뢰(>=0.50)는 저피부율 면제되어야 한다."""
    # 1) 중첩 병합 & penis 우선
    skin_img = _make_skin_image((400, 400))

    def fake_detect_overlap(im: Image.Image, conf_threshold: float = 0.16) -> list[Any]:
        if im.size == (400, 400):
            return [
                ((140, 200, 240, 300), "pussy", 0.72),
                ((155, 210, 245, 295), "penis", 0.65),
            ]
        return []

    merged = censor_detect_mod._detect_target_candidates(
        skin_img,
        targets=("penis", "pussy"),
        min_confidence=0.16,
        detect_fn=fake_detect_overlap,
        has_imgutils=True,
    )
    assert len(merged) == 1
    box, lbl, score = merged[0]
    assert lbl == "penis"
    assert score == pytest.approx(0.72)
    assert box == (140, 200, 245, 300)

    # 2) 역광/야경(피부율 0%): score >= 0.50 생존, score < 0.50 기각
    dark_img = _make_dark_backlight_image((400, 400))

    def fake_detect_backlight(im: Image.Image, conf_threshold: float = 0.16) -> list[Any]:
        if im.size == (400, 400):
            return [
                ((120, 200, 220, 300), "penis", 0.62),  # 고신뢰 면제 -> 생존
                ((260, 200, 340, 280), "penis", 0.24),  # 저신뢰 + 저피부율 -> 기각
            ]
        return []

    backlight_res = censor_detect_mod._detect_target_candidates(
        dark_img,
        targets=("penis", "pussy"),
        min_confidence=0.16,
        detect_fn=fake_detect_backlight,
        has_imgutils=True,
    )
    assert len(backlight_res) == 1
    assert backlight_res[0][0] == (120, 200, 220, 300)
    assert backlight_res[0][2] == pytest.approx(0.62)


def test_filter_bottom_edge_false_positives() -> None:
    """하단 프레임 끝단에 걸친 저신뢰 얇은 오탐 박스는 제거되고 상단 주 타겟은 보존되어야 한다."""
    img_np = np.full((500, 400, 3), (230, 190, 170), dtype=np.uint8)
    skin_np = censor_mod.get_skin_mask(img_np)
    cands = [
        ((140, 200, 240, 320), "penis", 0.85),  # 주 타겟 (생존)
        ((160, 465, 230, 498), "penis", 0.22),  # y2/H=0.996 >= 0.88, 저신뢰(<0.35) 하단 오탐 (제거)
    ]
    filtered = censor_detect_mod._filter_bottom_edge_false_positives(
        cands, img_w=400, img_h=500, img_rgb_np=img_np, skin_mask_np=skin_np
    )
    assert len(filtered) == 1
    assert filtered[0][0] == (140, 200, 240, 320)


# ── 3. P2.5 Dual Consensus + Residual Span 2-Way 복원 + protection_mask 0px 보장 ──


def test_hybrid_residual_span_branch_b_union_and_branch_a_hand_split() -> None:
    """YOLO prior 가 DeepGHS 하단을 미커버 시, 손 미가로질림(Branch B Union) vs 손 가로질림(Branch A Split)으로 정확히 분기한다."""
    img = _make_skin_image((400, 500))
    skin_np = censor_mod.get_skin_mask(np.array(img))

    # YOLO11s-seg 모의 모델: 상단 (150, 140, 240, 230) 폴리곤 탐지 (신뢰도 0.55, fast-path 0.92 미만)
    yolo_poly = np.array(
        [[150.0, 140.0], [240.0, 140.0], [240.0, 230.0], [150.0, 230.0]],
        dtype=np.float32,
    )

    class FakeYoloSeg:
        names = {0: "penis"}

        def __call__(self, _im: Image.Image, conf: float = 0.16, verbose: bool = False) -> list[Any]:
            boxes = SimpleNamespace(
                xyxy=np.array([[150.0, 140.0, 240.0, 230.0]], dtype=np.float32),
                cls=np.array([0], dtype=np.int32),
                conf=np.array([0.55], dtype=np.float32),
            )
            masks = SimpleNamespace(xy=[yolo_poly])
            return [SimpleNamespace(boxes=boxes, masks=masks, names=self.names)]

    # DeepGHS 모의 탐지: 상단~하단 전체 (145, 140, 245, 390) 탐지 -> bot_res = 160 >= y_res_thresh
    def fake_dghs(im: Image.Image, conf_threshold: float = 0.16) -> list[Any]:
        if im.size == (400, 500):
            return [((145, 140, 245, 390), "penis", 0.78)]
        return []

    # (1) Branch B: 손이 가로지르지 않음 -> 단일 Union Span 박스 + prior + context_box
    boxes_b, seg_mask_b, fb_b, priors_b, ctx_b = censor_detect_mod._resolve_hybrid_targets_with_priors(
        img,
        targets=("penis", "pussy"),
        min_confidence=0.16,
        skin_np=skin_np,
        hand_boxes=[],
        detect_fn=fake_dghs,
        has_imgutils=True,
        seg_model_provider=lambda: FakeYoloSeg(),
    )
    assert seg_mask_b is None
    assert len(boxes_b) == 1
    assert boxes_b[0][0] == (145, 140, 245, 390)
    assert priors_b is not None and priors_b[0] is not None
    assert ctx_b is not None and ctx_b[0] == (145, 140, 245, 390)

    # 중심축 회랑(corridor)이 하단 미커버 구간(y=230..390)으로 뻗어 나가는지 검증
    corridor = censor_seg_mod._compute_prior_extension_corridor(
        (priors_b[0] > 0).astype(np.uint8), boxes_b[0][0]
    )
    assert corridor is not None
    assert np.count_nonzero(corridor[230:390, 145:245]) > 0

    # (2) Branch A: 손 박스가 상단 prior(y=140..230)와 하단 잔여(y=230..390) 사이를 가로지름 -> 2개 Sub-BBox 분할
    crossing_hand = [(120, 215, 270, 275)]
    boxes_a, _, _, priors_a, _ = censor_detect_mod._resolve_hybrid_targets_with_priors(
        img,
        targets=("penis", "pussy"),
        min_confidence=0.16,
        skin_np=skin_np,
        hand_boxes=crossing_hand,
        detect_fn=fake_dghs,
        has_imgutils=True,
        seg_model_provider=lambda: FakeYoloSeg(),
    )
    assert len(boxes_a) == 2
    assert boxes_a[0][0] == (150, 140, 240, 230)  # 상단 YOLO prior 유지
    assert boxes_a[1][0][1] >= 230 and boxes_a[1][0][3] == 390  # 하단 독립 Sub-BBox 추가
    assert priors_a is not None and priors_a[0] is not None and priors_a[1] is None


def test_protection_mask_zero_px_non_encroachment_and_lineart_snap(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_silhouette 은 AA 블러 및 선화 스냅 이후에도 protection_mask 영역 픽셀을 단 1px 도 침범하지 않아야 한다."""
    img_np = np.full((256, 256, 3), (225, 185, 165), dtype=np.uint8)
    # 검열 박스 (80, 80, 176, 176) 바깥쪽 1px 어두운 선화 테두리 추가
    img_np[79:177, 79:177] = (30, 25, 25)
    img_np[81:175, 81:175] = (230, 190, 170)
    # 보호 영역 (110, 100, 150, 160)에 고유 색 마킹
    img_np[100:160, 110:150] = (40, 180, 240)
    img = Image.fromarray(img_np, mode="RGB")

    protect = np.zeros((256, 256), dtype=np.uint8)
    protect[100:160, 110:150] = 255

    seg_mask = np.zeros((256, 256), dtype=np.uint8)
    seg_mask[81:175, 81:175] = 255

    monkeypatch.setattr(censor_mod, "HAS_IMGUTILS", False)
    monkeypatch.setattr(censor_mod, "get_sam_model", lambda: None)

    out = censor_mod.apply_white_silhouette(
        img,
        [((80, 80, 176, 176), "penis", 0.90)],
        custom_seg_mask=seg_mask,
        fallback_boxes=[],
        extra_protection_mask=protect,
    )
    out_np = np.array(out)

    # 1) 보호 마스크 영역 내부 모든 픽셀은 원본과 바이트 단위 100% 일치 (0px 침범 보장)
    assert np.array_equal(out_np[protect > 0], img_np[protect > 0])
    # 2) 비보호 검열 코어는 순백색으로 마스킹
    assert tuple(int(v) for v in out_np[130, 95]) == (255, 255, 255)

    # 3) _snap_to_outer_lineart 단독 검증: 보호 영역 외 어두운 외곽선은 흡수, 보호 영역은 0 유지
    snapped = censor_seg_mod._snap_to_outer_lineart(
        seg_mask, img_np, scale=1.0, protection_mask=protect, max_steps=2
    )
    assert int(snapped[80, 95]) == 255  # 어두운 외곽 선화(y=80) 흡수 확인
    assert np.count_nonzero(snapped[protect > 0]) == 0


# ── 4. ADR-103 오토코노코 컨텍스트 및 is_censor_target / runner 전파 ──


def test_adr103_otokonoko_censor_target_and_runner_propagation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-103: 여성 비노출(101,102,104,105)만 제외되고 오토코노코(+70: 171~175 포함)는 전수 검열 대상이어야 한다."""
    assert censor_mod.NON_EXPOSURE_H_POSES == frozenset({"101", "102", "104", "105"})

    for fem_non_exp in (101, 102, 104, 105):
        assert censor_mod.is_censor_target(fem_non_exp, section="h_scenes", is_otokonoko=False) is False
        assert censor_mod.is_censor_target(fem_non_exp, section="h_scenes", is_otokonoko=True) is True

    for otk_code in (150, 171, 172, 174, 175, 197):
        assert censor_mod.is_censor_target(otk_code) is True

    assert censor_mod._is_otokonoko_context(roster="oto") is True
    assert censor_mod._is_otokonoko_context(section="scenes_otokonoko") is True
    assert censor_mod._is_otokonoko_context(gender="otokonoko") is True
    assert censor_mod._is_otokonoko_context(file_path=Path("projects/oto/assets/ren/ren_101.webp")) is True
    assert censor_mod._is_otokonoko_context(roster="dark_generals", gender="female", pose_code=80) is False

    # runner._apply_post_censor 에서 is_nude / is_otokonoko / roster 전파 확인
    captured: dict[str, Any] = {}
    asset_dir = tmp_path / "assets" / "ren"
    asset_dir.mkdir(parents=True)
    save_path = asset_dir / "ren_171.webp"
    png_bytes = _make_png_bytes((64, 64))
    Image.open(io.BytesIO(png_bytes)).save(save_path, format="WEBP")

    def fake_censor_file(*args: Any, **kwargs: Any) -> Path:
        captured.update(kwargs)
        dst = Path(kwargs["dst_path"])
        dst.write_bytes(save_path.read_bytes())
        return dst

    monkeypatch.setattr(censor_mod, "censor_file", fake_censor_file)
    entry = PoseEntry(code=171, prompt="afterglow, nude", section="scenes_otokonoko")
    runner_mod._apply_post_censor(
        png_bytes=png_bytes,
        save_path=save_path,
        prefix="ren",
        filename="ren_171.webp",
        code=171,
        entry=entry,
        is_nude_scene=True,
        censor_style="white_silhouette",
        gender="otokonoko",
        roster_name="oto",
    )
    assert captured.get("is_nude") is True
    assert captured.get("is_otokonoko") is True
    assert captured.get("roster") == "oto"


# ── 5. strict_censor_qc 옵션 독립 연동 (runner + CLI + GUI) ──


def test_strict_censor_qc_rerolls_independently_of_censor_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """strict_censor_qc=True 는 censor=False 에서도 성인 노출 필수 씬 타겟 미탐지 시 max_retries 까지 리롤해야 한다."""
    png_bytes = _make_png_bytes((128, 128))
    gen_calls = {"n": 0}
    detect_calls = {"n": 0}

    def fake_request(payload: dict[str, Any], scene: SceneComposition, clip_skip: Any, tag: str) -> bytes:
        gen_calls["n"] += 1
        return png_bytes

    def fake_detect_pass_on_3rd(img: Any, **_kw: Any) -> list[Any]:
        detect_calls["n"] += 1
        if detect_calls["n"] < 3:
            return []
        return [((30, 50, 90, 110), "penis", 0.82)]

    monkeypatch.setattr(runner_mod, "_request_image_with_fallback", fake_request)
    monkeypatch.setattr(censor_mod, "detect_genitals", fake_detect_pass_on_3rd)

    # Case 1: 2회 미탐지 -> 3회차 탐지 성공 (auto_qc=False, censor=False 독립 동작)
    res1 = BatchResult()
    entry_h = PoseEntry(code=80, prompt="sex, missionary", section="h_scenes")
    out1 = runner_mod._generate_and_validate_image(
        payload={"seed": 42},
        scene=_make_scene_stub(is_nude_scene=True, is_aftermath=False, is_solo_scene=False),
        entry=entry_h,
        code=80,
        tag="080",
        clip_skip=None,
        auto_qc=False,
        strict_censor_qc=True,
        max_retries=3,
        result=res1,
    )
    assert out1 == png_bytes
    assert gen_calls["n"] == 3
    assert detect_calls["n"] == 3
    assert res1.qa_warnings == []

    # Case 2: 모든 시도 미탐지 -> max_retries=2 초과 후 qa_warnings 에 사유 기록
    gen_calls["n"] = 0
    monkeypatch.setattr(censor_mod, "detect_genitals", lambda _img, **_kw: [])
    res2 = BatchResult()
    runner_mod._generate_and_validate_image(
        payload={"seed": 42},
        scene=_make_scene_stub(is_nude_scene=True, is_aftermath=False, is_solo_scene=False),
        entry=entry_h,
        code=80,
        tag="080",
        clip_skip=None,
        auto_qc=False,
        strict_censor_qc=True,
        max_retries=2,
        result=res2,
    )
    assert gen_calls["n"] == 3
    assert len(res2.qa_warnings) == 1
    assert "strict_censor_qc" in res2.qa_warnings[0][2]

    # Case 3: 감정 씬 / 여운(aftermath) / 솔로 씬은 detect_genitals 미호출
    skip_detect_calls = {"n": 0}
    monkeypatch.setattr(
        censor_mod,
        "detect_genitals",
        lambda _img, **_kw: skip_detect_calls.__setitem__("n", skip_detect_calls["n"] + 1) or [],
    )
    for sc, ent in (
        (_make_scene_stub(is_nude_scene=False, is_aftermath=False, is_solo_scene=False), PoseEntry(code=5, prompt="happy", section="emotions")),
        (_make_scene_stub(is_nude_scene=True, is_aftermath=True, is_solo_scene=False), PoseEntry(code=101, prompt="afterglow", section="h_scenes")),
        (_make_scene_stub(is_nude_scene=True, is_aftermath=False, is_solo_scene=True), PoseEntry(code=95, prompt="solo", section="h_scenes")),
    ):
        runner_mod._generate_and_validate_image(
            payload={"seed": 1},
            scene=sc,
            entry=ent,
            code=ent.code,
            tag=f"{ent.code:03d}",
            clip_skip=None,
            auto_qc=False,
            strict_censor_qc=True,
            max_retries=2,
            result=BatchResult(),
        )
    assert skip_detect_calls["n"] == 0


def test_strict_censor_qc_cli_and_gui_wiring() -> None:
    """CLI --strict-censor-qc 및 GUI 설정/옵션 브릿지가 GenerationOptions.strict_censor_qc 와 연동되어야 한다."""
    parser = sd_batch_generator.build_parser(add_help=False)
    ns = parser.parse_args(["--prefix", "demo", "--char_prompt", "1girl", "--strict-censor-qc"])
    opts = GenerationOptions.from_namespace(ns)
    assert opts.strict_censor_qc is True
    assert opts.censor is False

    assert "strict_censor_qc" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["strict_censor_qc"] is False

    fake_state = SimpleNamespace(ckpt_profiles={})
    gui_opts = build_base_options(
        fake_state,  # type: ignore[arg-type]
        {**DEFAULT_SETTINGS, "strict_censor_qc": True, "censor": False},
        mode_val="h_scenes",
        codes_val=None,
        ckpt_name=None,
    )
    assert gui_opts.strict_censor_qc is True
    assert gui_opts.censor is False


# ── 6. Subprocess Lazy ML Load 불변식 검증 ──


def test_lazy_ml_load_invariant_on_import_generator() -> None:
    """import generator 시 torch / ultralytics / imgutils / censor 엔진이 즉시 로드되지 않아야 한다."""
    code = (
        "import json, sys; "
        "import generator; "
        "loaded = [m for m in ('torch', 'ultralytics', 'imgutils', 'imgutils.detect', "
        "'generator.censor', 'generator.censor_seg', 'generator.censor_detect') if m in sys.modules]; "
        "print(json.dumps(loaded))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(proc.stdout.strip())
    assert loaded == [], f"Lazy Load 위반 - 즉시 로드된 모듈: {loaded}"

"""
tests/test_port_p1_prompt_qa.py
Wave P1 (plepa 프롬프트 품질 안정화 + Auto-QC 정밀화 이식) 오프라인 단위·통합 테스트.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from generator import qa_validator as qa_mod
from generator.models import PoseEntry
from generator.prompt import (
    DEFAULT_THEME_OUTFITS,
    _strip_tags_preserving_parens,
    harmonize_character_expression,
    remove_conflicting_tags_from_negative,
    resolve_theme_outfit,
    strip_outfit_tags,
)
from generator.scene_composer import apply_composition_anti_drift, compose_scene_prompt


# ── 1. 표정 조화 (harmonize_character_expression) ──


def test_harmonize_character_expression_resolves_conflicts() -> None:
    """포즈에 명시적 표정이 있으면 캐릭터 기본 외형의 상반된 고정 표정을 안전하게 억제해야 한다."""
    char_stoic = "1girl, silver hair, (stoic expression:1.1), cool calm expression, red eyes"
    out_smile = harmonize_character_expression(char_stoic, "happy, bright smile, looking at viewer")
    assert "stoic expression" not in out_smile.lower()
    assert "cool calm expression" not in out_smile.lower()
    assert "silver hair" in out_smile and "red eyes" in out_smile
    assert out_smile.count("(") == out_smile.count(")")

    # 부정 감정 포즈에서는 캐릭터 기본 미소도 억제
    char_gentle = "1girl, blonde hair, gentle smile, blue eyes"
    out_angry = harmonize_character_expression(char_gentle, "angry, scowl, glaring")
    assert "gentle smile" not in out_angry.lower()
    assert "blonde hair" in out_angry

    # 포즈에 표정 태그가 없으면 캐릭터 원본 그대로 보존
    out_neutral_pose = harmonize_character_expression(char_stoic, "standing, hands on hips")
    assert out_neutral_pose == char_stoic


# ── 2. 충돌 태그 제거 및 모브 BREAK 격리 ──


def test_remove_conflicting_tags_preserves_nested_parens_and_isolates_mob_break() -> None:
    """중첩 가중치 괄호 균형과 보호 제어 태그를 유지하고, 모브 BREAK 블록은 격리해야 한다."""
    # 앞/뒤 위치 충돌 태그 제거 시 괄호 및 그룹 가중치 보존
    neg_front = "worst quality, ((choker, bad hands:1.5)), blurry"
    cleaned_front, removed_front = remove_conflicting_tags_from_negative("1girl, choker", neg_front)
    assert removed_front == ["choker"]
    assert cleaned_front.count("(") == cleaned_front.count(")")
    assert "((bad hands:1.5))" in cleaned_front

    neg_back = "worst quality, ((bad hands, choker:1.5)), blurry"
    cleaned_back, removed_back = remove_conflicting_tags_from_negative("1girl, choker", neg_back)
    assert removed_back == ["choker"]
    assert cleaned_back.count("(") == cleaned_back.count(")")
    assert "((bad hands:1.5))" in cleaned_back

    # 모브 전용 BREAK 블록의 태그(muscular build, naked male)가 여주인공 본체 네거티브를 삭제하면 안 됨
    pos_with_mob = (
        "masterpiece, missionary BREAK 1girl, silver hair, choker "
        "BREAK (single faceless male partner:1.15), (naked male:1.15), muscular build"
    )
    neg_base = "worst quality, choker, (naked male:1.2), muscular build, (1boy:1.4), solo"
    cleaned_mob, removed_mob = remove_conflicting_tags_from_negative(
        pos_with_mob, neg_base, isolate_mob_break=True
    )
    assert removed_mob == ["choker"]
    assert "muscular build" in cleaned_mob
    assert "(naked male:1.2)" in cleaned_mob

    # 보호 제어 태그(solo, 1boy, looking at viewer)는 충돌해도 제거 금지
    cleaned_prot, removed_prot = remove_conflicting_tags_from_negative(
        "1girl, solo, looking at viewer", "solo, looking at viewer, blurry"
    )
    assert removed_prot == []
    assert "solo" in cleaned_prot and "looking at viewer" in cleaned_prot

    # _strip_tags_preserving_parens 빈 괄호 정리 확인
    assert _strip_tags_preserving_parens("((choker:1.3)), bad hands", r"\bchoker\b(?::[0-9\.]+)?") == "bad hands"


# ── 3. CompositionAntiDrift 및 테마 의상 폴백 / 확장 의상 스트리핑 ──


def test_composition_anti_drift_rules_and_scene_composer() -> None:
    """샷 사이즈별 반대 구도 이탈 방지 네거티브가 정확히 주입되어야 한다."""
    neg_bust = apply_composition_anti_drift("tight bust portrait, smiling", "worst quality")
    assert "cowboy shot, full body, lower body, wide shot, knees, feet:1.25" in neg_bust

    neg_cowboy = apply_composition_anti_drift("cowboy shot, standing", "worst quality")
    assert "tight bust portrait, close-up face, full body, wide shot" in neg_cowboy

    neg_full = apply_composition_anti_drift("full body, standing outdoors", "worst quality")
    assert "cropped legs, tight bust portrait, close-up face" in neg_full

    # portrait가 함께 있으면 full body 규칙은 스킵
    neg_skip = apply_composition_anti_drift("full body portrait, standing", "worst quality")
    assert neg_skip == "worst quality"


def test_default_theme_outfits_fallback_and_extended_outfit_stripping() -> None:
    """캐릭터 outfits 미지정 시 DEFAULT_THEME_OUTFITS 폴백과 확장 의상 스트리핑이 동작해야 한다."""
    assert resolve_theme_outfit(None, "swimsuit") == DEFAULT_THEME_OUTFITS["swimsuit"]
    assert resolve_theme_outfit({"maid": "custom gothic maid dress"}, "maid") == "custom gothic maid dress"
    assert resolve_theme_outfit({}, "wedding") == DEFAULT_THEME_OUTFITS["wedding"]

    # 확장 의상 키워드/구문 제거 + 헤어/신체 태그 보존
    char_pos = (
        "masterpiece BREAK 1girl, solo, silver hair, hair ribbon, crimson eyes, slender body, "
        "black blazer, cashmere shawl, leather belt, ruffled apron, cross pendant, puffy sleeves"
    )
    stripped = strip_outfit_tags(char_pos)
    assert "silver hair" in stripped
    assert "hair ribbon" in stripped
    assert "crimson eyes" in stripped
    assert "slender body" in stripped
    for outfit_tok in ("blazer", "cashmere shawl", "leather belt", "ruffled apron", "cross pendant", "puffy sleeves"):
        assert outfit_tok not in stripped.lower()

    # scene_composer 연동: outfits 없이 required_outfit="bunny" 지정 시 공용 바니걸 의상 자동 폴백
    entry_bunny = PoseEntry(code=48, prompt="cowboy shot, posing", section="events", required_outfit="bunny")
    sc = compose_scene_prompt(
        entry=entry_bunny,
        prefix="ignis",
        tag="048",
        base_positive=char_pos,
        char_prompt="",
        negative_prompt="worst quality, blurry",
        sampler_name="DPM++ 2M",
        outfits=None,
    )
    assert "bunny suit" in sc.full_prompt.lower()
    assert "blazer" not in sc.full_prompt.lower()
    assert "tight bust portrait" in sc.actual_negative.lower()


# ── 4. 가중치 클램핑 기본 비활성(False) vs opt-in 분리 클램핑(True) ──


def test_scene_composer_clamp_weights_opt_in_separate_caps() -> None:
    """enable_clamp_weights=False는 강조 가중치(1.35~1.55)를 보존하고, True는 긍정<=1.15·부정<=1.25로 분리 클램핑해야 한다."""
    entry = PoseEntry(code=5, prompt="tight bust portrait, smiling", section="emotions")
    base_pos = "masterpiece BREAK 1girl, solo, ((burgundy hair:1.45)), (stoic expression:1.35)"
    base_neg = "worst quality, ((extra limbs:1.55)), (bad hands:1.40)"

    sc_default = compose_scene_prompt(
        entry=entry,
        prefix="ignis",
        tag="005",
        base_positive=base_pos,
        char_prompt="",
        negative_prompt=base_neg,
        sampler_name="DPM++ 2M",
        enable_clamp_weights=False,
    )
    assert "((burgundy hair:1.45))" in sc_default.full_prompt
    assert "((extra limbs:1.55))" in sc_default.actual_negative
    assert "stoic expression" not in sc_default.full_prompt.lower()

    sc_clamped = compose_scene_prompt(
        entry=entry,
        prefix="ignis",
        tag="005",
        base_positive=base_pos,
        char_prompt="",
        negative_prompt=base_neg,
        sampler_name="DPM++ 2M",
        enable_clamp_weights=True,
    )
    assert "(burgundy hair:1.15)" in sc_clamped.full_prompt
    assert "1.45" not in sc_clamped.full_prompt
    assert "(extra limbs:1.25)" in sc_clamped.actual_negative
    assert "(bad hands:1.25)" in sc_clamped.actual_negative
    assert "1.55" not in sc_clamped.actual_negative
    assert "((" not in sc_clamped.full_prompt and "((" not in sc_clamped.actual_negative


# ── 5. Auto-QC 정밀화 (Color Burn, 화각 드리프트, Short-Circuit, 증발/headless/오프스크린/faceless, Lazy Load) ──


def _make_burned_image(burn_fraction: float) -> Image.Image:
    """상단 burn_fraction 비율을 극단 네온 마젠타(255, 0, 255), 나머지를 중성 회색으로 채운 테스트 이미지."""
    img = Image.new("RGB", (100, 100), (140, 140, 140))
    burn_rows = int(round(100 * burn_fraction))
    for y in range(burn_rows):
        for x in range(100):
            img.putpixel((x, y), (255, 0, 255))
    return img


def test_qa_validator_color_burn_normal_vs_night_and_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Color Burn은 일반 씬(0.22)에서 차단하고 야간 씬(0.35)에서 완화하며, 실패 시 하위 ONNX 감지기를 호출하지 않아야 한다."""
    calls = {"person": 0, "head": 0, "face": 0, "hand": 0}

    def _p(_img: Any) -> list[Any]:
        calls["person"] += 1
        return [((10, 10, 90, 95), "person", 0.9)]

    def _hd(_img: Any) -> list[Any]:
        calls["head"] += 1
        return [((30, 10, 70, 45), "head", 0.9)]

    def _fc(_img: Any) -> list[Any]:
        calls["face"] += 1
        return [((35, 15, 65, 40), "face", 0.9)]

    def _hn(_img: Any) -> list[Any]:
        calls["hand"] += 1
        return [((20, 50, 35, 65), "hand", 0.9)]

    monkeypatch.setattr(qa_mod, "HAS_IMGUTILS", True)
    monkeypatch.setattr(qa_mod, "detect_person", _p)
    monkeypatch.setattr(qa_mod, "detect_heads", _hd)
    monkeypatch.setattr(qa_mod, "detect_faces", _fc)
    monkeypatch.setattr(qa_mod, "detect_hands", _hn)

    # 28% 과포화 이미지: 일반 주간 씬(>=22%) -> 실패 + Short-Circuit (검출기 0회 호출)
    img_28 = _make_burned_image(0.28)
    ok, reason, metrics = qa_mod.validate_generated_image(img_28, is_solo_scene=True, prompt="standing, dark hair, smile")
    assert ok is False
    assert "Color Burn" in reason
    assert metrics["color_burn_ratio"] >= 0.22
    assert calls == {"person": 0, "head": 0, "face": 0, "hand": 0}

    # 동일 28% 과포화 이미지: 야간/네온 씬(<=35% 완화) -> 통과
    ok_night, reason_night, _ = qa_mod.validate_generated_image(
        img_28, is_solo_scene=True, prompt="standing, neon city lights at night, smile"
    )
    assert ok_night is True and reason_night == ""
    assert calls["person"] == 1 and calls["hand"] == 1

    # 45% 극단 과포화 이미지: 야간 씬에서도 차단
    img_45 = _make_burned_image(0.45)
    ok_night_fail, reason_night_fail, _ = qa_mod.validate_generated_image(
        img_45, is_solo_scene=True, prompt="neon night scene"
    )
    assert ok_night_fail is False and "Color Burn" in reason_night_fail


def test_qa_validator_aspect_drift_evaporation_headless_offscreen_faceless_and_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """화각 드리프트, 피사체 증발/headless 예외, 오프스크린/faceless 분기 및 단계별 Short-Circuit을 검증한다."""
    sample = Image.new("RGB", (200, 200), (180, 180, 180))
    calls = {"head": 0, "face": 0, "hand": 0}

    def _setup(persons: list[Any], heads: list[Any], faces: list[Any], hands: list[Any]) -> None:
        calls["head"] = calls["face"] = calls["hand"] = 0
        monkeypatch.setattr(qa_mod, "HAS_IMGUTILS", True)
        monkeypatch.setattr(qa_mod, "detect_person", lambda _im: persons)

        def _hd(_im: Any) -> list[Any]:
            calls["head"] += 1
            return heads

        def _fc(_im: Any) -> list[Any]:
            calls["face"] += 1
            return faces

        def _hn(_im: Any) -> list[Any]:
            calls["hand"] += 1
            return hands

        monkeypatch.setattr(qa_mod, "detect_heads", _hd)
        monkeypatch.setattr(qa_mod, "detect_faces", _fc)
        monkeypatch.setattr(qa_mod, "detect_hands", _hn)

    # 1) 피사체 증발 (persons == 0) -> detect_heads/faces/hands Short-Circuit
    _setup([], [], [], [])
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True, prompt="standing, smile")
    assert ok is False and "피사체 증발" in reason
    assert calls == {"head": 0, "face": 0, "hand": 0}

    # 2) headless / crotch focus 예외 -> persons=0, heads=0, faces=0 이어도 통과
    _setup([], [], [], [])
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True, prompt="crotch focus, lower body")
    assert ok is True and reason == ""

    # 3) 화각 드리프트: tight bust portrait에서 얼굴 높이 10px / 200px = 5% < 8% -> 불량 + detect_hands Short-Circuit
    _setup(
        [((40, 20, 160, 190), "person", 0.9)],
        [((80, 20, 110, 40), "head", 0.9)],
        [((85, 22, 100, 32), "face", 0.9)],
        [((50, 80, 70, 100), "hand", 0.9)],
    )
    ok, reason, metrics = qa_mod.validate_generated_image(
        sample, is_solo_scene=True, prompt="tight bust portrait, smiling"
    )
    assert ok is False and "화각 드리프트" in reason
    assert metrics["face_height_ratio"] == 0.05
    assert calls["hand"] == 0

    # 4) 오프스크린 파트너: 머리 2개 불량(허용 1개) + detect_faces/hands Short-Circuit
    _setup(
        [((10, 10, 100, 180), "person", 0.9), ((110, 10, 190, 180), "person", 0.9)],
        [((30, 10, 70, 50), "head", 0.9), ((130, 10, 170, 50), "head", 0.9)],
        [((35, 15, 65, 45), "face", 0.9)],
        [],
    )
    ok, reason, _ = qa_mod.validate_generated_image(
        sample, is_solo_scene=False, is_two_person=True, is_offscreen_partner=True, prompt="pov, off-screen partner"
    )
    assert ok is False and "오프스크린 파트너" in reason
    assert calls["face"] == 0 and calls["hand"] == 0

    # 5) 2인 씬 faceless 위반 (얼굴 2개 -> 불량) vs 키스 씬 예외 (얼굴 2개 -> 통과)
    _setup(
        [((10, 10, 100, 180), "person", 0.9), ((100, 10, 190, 180), "person", 0.9)],
        [((30, 10, 70, 50), "head", 0.9), ((130, 10, 170, 50), "head", 0.9)],
        [((35, 15, 65, 55), "face", 0.9), ((135, 15, 165, 55), "face", 0.9)],
        [],
    )
    ok_fl, reason_fl, _ = qa_mod.validate_generated_image(
        sample, is_solo_scene=False, is_two_person=True, prompt="missionary, faceless male"
    )
    assert ok_fl is False and "faceless 위반" in reason_fl
    assert calls["hand"] == 0

    ok_kiss, reason_kiss, _ = qa_mod.validate_generated_image(
        sample, is_solo_scene=False, is_two_person=True, is_kiss_scene=True, prompt="deep kiss, faceless male"
    )
    assert ok_kiss is True and reason_kiss == ""


def test_qa_validator_opt_in_blur_and_aesthetic_and_lazy_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opt-in check_blur/check_aesthetic 동작 및 import generator 시 torch/imgutils Lazy Load 유지를 검증한다."""
    sample = Image.new("RGB", (64, 64), (160, 160, 160))
    monkeypatch.setattr(qa_mod, "HAS_IMGUTILS", True)
    monkeypatch.setattr(qa_mod, "detect_person", lambda _im: [((0, 0, 60, 60), "person", 0.9)])
    monkeypatch.setattr(qa_mod, "detect_heads", lambda _im: [((10, 5, 40, 30), "head", 0.9)])
    monkeypatch.setattr(qa_mod, "detect_faces", lambda _im: [((15, 8, 35, 28), "face", 0.9)])
    monkeypatch.setattr(qa_mod, "detect_hands", lambda _im: [])
    monkeypatch.setattr(qa_mod, "HAS_LAPLACIAN", True)
    monkeypatch.setattr(qa_mod, "laplacian_score", lambda _im: 25.0)
    monkeypatch.setattr(qa_mod, "HAS_AESTHETIC", True)
    monkeypatch.setattr(qa_mod, "anime_dbaesthetic", lambda _im: 3.1)

    ok_blur, reason_blur, m_blur = qa_mod.validate_generated_image(sample, check_blur=True, min_blur_score=60.0)
    assert ok_blur is False and "블러" in reason_blur and m_blur["blur_score"] == 25.0

    ok_aes, reason_aes, m_aes = qa_mod.validate_generated_image(sample, check_aesthetic=True, min_aesthetic_score=4.0)
    assert ok_aes is False and "미적 완성도" in reason_aes and m_aes["aesthetic_score"] == 3.1

    # 격리된 서브프로세스에서 import generator 및 import generator.qa_validator 시 torch/imgutils 미로드 확인
    repo_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; "
        "import generator; "
        "import generator.qa_validator; "
        "loaded = [m for m in ('torch', 'imgutils', 'imgutils.detect', 'imgutils.metrics') if m in sys.modules]; "
        "assert not loaded, f'Lazy load broken: {loaded}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

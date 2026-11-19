"""Wave 0 안전망 검증: Lazy 임포트, Auto-QC 모의 판정, 검열 코드 추출/배치 및 기존 진단 회귀 확인."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image


def test_import_generator_avoids_heavy_ml_imports() -> None:
    """`import generator` 시 torch/cv2/imgutils가 즉시 로드되지 않아야 한다."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, generator; "
                "assert 'torch' not in sys.modules, 'torch loaded eagerly'; "
                "assert 'cv2' not in sys.modules, 'cv2 loaded eagerly'; "
                "assert 'imgutils' not in sys.modules, 'imgutils loaded eagerly'"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_import_qa_validator_has_no_import_side_effects() -> None:
    """`import generator.qa_validator` 만으로는 torch 로드나 런타임 초기화가 일어나지 않는다."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, generator.qa_validator as qv; "
                "assert 'torch' not in sys.modules, 'torch loaded on qa_validator import'; "
                "assert qv.HAS_IMGUTILS is None"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_generator_lazy_exports_backward_compatible() -> None:
    """PEP 562 Lazy Export 심볼이 기존과 동일하게 임포트 및 호출 가능해야 한다."""
    from generator import (
        POSE_SAFE_ANCHORS,
        censor_file,
        extract_pose_code,
        validate_generated_image,
    )

    assert isinstance(POSE_SAFE_ANCHORS, dict)
    assert callable(censor_file)
    assert callable(extract_pose_code)
    assert callable(validate_generated_image)


def test_qa_validator_rules_offline_with_mocked_detectors(monkeypatch) -> None:
    """외부 감지기 모킹으로 1인/POV/2인 Auto-QC 판정 규칙을 오프라인 검증한다."""
    import generator.qa_validator as qv

    dummy_img = Image.new("RGB", (64, 64), (200, 200, 200))

    def _configure_counts(persons: int, heads: int, hands: int, faces: int) -> None:
        monkeypatch.setattr(qv, "HAS_IMGUTILS", True)
        monkeypatch.setattr(qv, "detect_person", lambda _im: [((0, 0, 10, 10), "person", 0.9)] * persons)
        monkeypatch.setattr(qv, "detect_heads", lambda _im: [((0, 0, 10, 10), "head", 0.9)] * heads)
        monkeypatch.setattr(qv, "detect_hands", lambda _im: [((0, 0, 10, 10), "hand", 0.9)] * hands)
        monkeypatch.setattr(qv, "detect_faces", lambda _im: [((0, 0, 10, 10), "face", 0.9)] * faces)

    # 1인 씬: person=1, head=1, hand=2 -> 통과
    _configure_counts(1, 1, 2, 1)
    ok, reason, _ = qv.validate_generated_image(dummy_img, is_solo_scene=True, is_two_person=False)
    assert ok is True and reason == ""

    # 1인 씬: person=2 -> 실패
    _configure_counts(2, 1, 2, 1)
    ok, _, _ = qv.validate_generated_image(dummy_img, is_solo_scene=True, is_two_person=False)
    assert ok is False

    # 1인 씬(비-POV): hand=3 -> 실패
    _configure_counts(1, 1, 3, 1)
    ok, _, _ = qv.validate_generated_image(dummy_img, is_solo_scene=True, is_two_person=False, prompt="standing")
    assert ok is False

    # 1인 씬(POV): hand=4 -> 통과, hand=5 -> 실패
    _configure_counts(1, 1, 4, 1)
    ok, _, _ = qv.validate_generated_image(
        dummy_img, is_solo_scene=True, is_two_person=False, prompt="headpat, looking up"
    )
    assert ok is True

    _configure_counts(1, 1, 5, 1)
    ok, _, _ = qv.validate_generated_image(
        dummy_img, is_solo_scene=True, is_two_person=False, prompt="headpat, looking up"
    )
    assert ok is False

    # 2인 씬: person=2, head=2, hand=4, face=2 -> 통과, face=3 -> 실패
    _configure_counts(2, 2, 4, 2)
    ok, _, _ = qv.validate_generated_image(
        dummy_img, is_solo_scene=False, is_two_person=True, is_offscreen_partner=False
    )
    assert ok is True

    _configure_counts(2, 2, 4, 3)
    ok, _, _ = qv.validate_generated_image(
        dummy_img, is_solo_scene=False, is_two_person=True, is_offscreen_partner=False
    )
    assert ok is False


def test_censor_extract_pose_code() -> None:
    """code 및 hybrid 파일명 모두에서 포즈 코드 정수를 정확히 추출한다."""
    from generator.censor import extract_pose_code

    assert extract_pose_code("abc_080.webp") == 80
    assert extract_pose_code("abc_080_정상위.webp") == 80
    assert extract_pose_code("abc_135_특수.png") == 135
    assert extract_pose_code("abc_05.webp") == 5
    assert extract_pose_code("no_code.webp") is None


def test_censor_run_batch_censor_no_nameerror_and_excludes_origin(tmp_path: Path, monkeypatch) -> None:
    """run_batch_censor()가 hybrid 파일명을 처리하고 _origin/_censored/비H씬을 제외한다."""
    import generator.censor as censor

    char_dir = tmp_path / "char"
    origin_dir = tmp_path / "char_origin"
    char_dir.mkdir()
    origin_dir.mkdir()

    def _save_dummy(path: Path) -> None:
        Image.new("RGB", (64, 64), (240, 220, 210)).save(path, "WEBP")

    _save_dummy(char_dir / "char_080_정상위.webp")
    _save_dummy(char_dir / "char_010_평상.webp")
    _save_dummy(origin_dir / "char_080_정상위.webp")
    _save_dummy(char_dir / "char_080_censored.webp")

    monkeypatch.setattr(censor, "HAS_IMGUTILS", True)
    monkeypatch.setattr(
        censor,
        "detect_censors",
        lambda _im: [((10, 10, 40, 40), "penis", 0.9)],
    )

    processed = censor.run_batch_censor(tmp_path, style="wide_bar", overwrite=False)
    assert processed == 1
    assert (char_dir / "char_080_정상위_censored.webp").is_file()
    assert not (char_dir / "char_010_평상_censored.webp").exists()
    assert not (origin_dir / "char_080_정상위_censored.webp").exists()


def test_self_test_suite_passes() -> None:
    """기존 --test 자체 진단 64개 항목이 모두 통과해야 한다."""
    from generator.diagnostics.self_test import run_self_test

    assert run_self_test(Path.cwd()) == 0

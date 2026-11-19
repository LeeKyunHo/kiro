"""
tests/test_wave4_coverage.py
Wave 4 리팩토링(WebUI 클라이언트 재시도/WebP/Mock, Auto-QC 판정·Lazy Load, 검열 스타일 분기, Censor Studio V2/백업) 커버리지 보강 테스트.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pytest
import requests

from generator import censor as censor_mod
from generator import qa_validator as qa_mod
from generator import webui_client as wc
from generator.models import PoseEntry
from tools import censor_studio


def _make_png_bytes(color: tuple[int, int, int] = (120, 160, 200), mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    if mode == "RGBA":
        img = Image.new("RGBA", (64, 64), (*color, 255))
    else:
        img = Image.new("RGB", (64, 64), color)
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 1. generator/webui_client.py ──


def test_generate_image_retries_on_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_image()가 일시적 ConnectionError 후 백오프 재시도하여 PNG 바이트를 정상 반환해야 한다."""
    png_bytes = _make_png_bytes((10, 20, 30))
    b64_png = base64.b64encode(png_bytes).decode("ascii")
    calls = {"count": 0}
    sleeps: list[float] = []

    class FakeResponse:
        text = "{}"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"images": [b64_png]}

    class FakeSession:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> FakeResponse:
            calls["count"] += 1
            if calls["count"] < 2:
                raise requests.exceptions.ConnectionError("temporary reset")
            return FakeResponse()

    monkeypatch.setattr(wc, "get_session", lambda: FakeSession())
    monkeypatch.setattr(wc.time, "sleep", sleeps.append)

    out = wc.generate_image({"prompt": "test"}, host="http://127.0.0.1:7860", max_retries=2)
    assert out == png_bytes
    assert calls["count"] == 2
    assert sleeps == [0.5]


def test_generate_image_raises_on_exhausted_retries_and_invalid_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재시도 한도 초과, 비정상 JSON, 빈 images 응답 시 각각 적절한 예외를 발생시켜야 한다."""
    monkeypatch.setattr(wc.time, "sleep", lambda _s: None)

    class AlwaysTimeoutSession:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> Any:
            raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(wc, "get_session", lambda: AlwaysTimeoutSession())
    with pytest.raises(requests.exceptions.Timeout):
        wc.generate_image({"prompt": "test"}, max_retries=1)

    class BadJsonResponse:
        text = "<html>502 Bad Gateway</html>"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            raise ValueError("Expecting value")

    class BadJsonSession:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> Any:
            return BadJsonResponse()

    monkeypatch.setattr(wc, "get_session", lambda: BadJsonSession())
    with pytest.raises(RuntimeError, match="유효한 JSON이 아닙니다"):
        wc.generate_image({"prompt": "test"}, max_retries=0)

    class EmptyImagesResponse:
        text = '{"images": []}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"images": []}

    class EmptyImagesSession:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> Any:
            return EmptyImagesResponse()

    monkeypatch.setattr(wc, "get_session", lambda: EmptyImagesSession())
    with pytest.raises(RuntimeError, match="images 가 없습니다"):
        wc.generate_image({"prompt": "test"}, max_retries=0)


def test_save_as_webp_and_make_dummy_png_pipeline(tmp_path: Path) -> None:
    """save_as_webp()가 RGBA PNG를 원자적으로 WebP 저장하고 make_dummy_png()가 유효한 PNG 바이트를 생성해야 한다."""
    entry = PoseEntry(code=12, section="emotions", prompt="smile, looking at viewer", label_override="미소")
    dummy_png = wc.make_dummy_png("ignis", 12, 3, entry, reference=None)
    with Image.open(io.BytesIO(dummy_png)) as im:
        assert im.format == "PNG"
        assert im.size == wc.MOCK_SIZE

    rgba_png = _make_png_bytes((200, 100, 50), mode="RGBA")
    out_webp = tmp_path / "ignis_012_미소.webp"
    wc.save_as_webp(rgba_png, out_webp)

    assert out_webp.is_file()
    assert not out_webp.with_name(out_webp.name + ".part").exists()
    with Image.open(out_webp) as saved:
        assert saved.format == "WEBP"
        assert saved.mode == "RGB"
        assert saved.size == (64, 64)


# ── 2. generator/qa_validator.py ──


def _mock_qa_detectors(
    monkeypatch: pytest.MonkeyPatch,
    *,
    persons: int = 1,
    heads: int = 1,
    hands: int = 2,
    faces: int = 1,
) -> None:
    monkeypatch.setattr(qa_mod, "HAS_IMGUTILS", True)
    monkeypatch.setattr(qa_mod, "detect_person", lambda _img: [((0, 0, 10, 10), "person", 0.9)] * persons)
    monkeypatch.setattr(qa_mod, "detect_heads", lambda _img: [((0, 0, 10, 10), "head", 0.9)] * heads)
    monkeypatch.setattr(qa_mod, "detect_hands", lambda _img: [((0, 0, 10, 10), "hand", 0.9)] * hands)
    monkeypatch.setattr(qa_mod, "detect_faces", lambda _img: [((0, 0, 10, 10), "face", 0.9)] * faces)


def test_qa_validator_solo_two_person_and_pov_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_generated_image()가 인원 초과, 머리 복제, 손 증식, 모브 얼굴 노출 및 POV 손 예외를 정확히 판정해야 한다."""
    sample = Image.new("RGB", (64, 64), (180, 180, 180))

    # 1인 씬: 정상 통과
    _mock_qa_detectors(monkeypatch, persons=1, heads=1, hands=2, faces=1)
    ok, reason, metrics = qa_mod.validate_generated_image(sample, is_solo_scene=True)
    assert ok is True and reason == "" and metrics["hands"] == 2

    # 1인 씬: 인원/머리 복제 불량
    _mock_qa_detectors(monkeypatch, persons=1, heads=2, hands=2, faces=1)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True)
    assert ok is False and "인원/머리 초과" in reason

    # 1인 씬: 일반 포즈 손 3개 불량 vs POV 손 개입(headpat) 4개 통과 / 5개 불량
    _mock_qa_detectors(monkeypatch, persons=1, heads=1, hands=3, faces=1)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True, prompt="standing, smile")
    assert ok is False and "손 증식" in reason

    _mock_qa_detectors(monkeypatch, persons=1, heads=1, hands=4, faces=1)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True, prompt="pov, headpat, smiling")
    assert ok is True and reason == ""

    _mock_qa_detectors(monkeypatch, persons=1, heads=1, hands=5, faces=1)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=True, prompt="pov, headpat, smiling")
    assert ok is False and "손 증식" in reason

    # 2인 씬: 제3자 인원 초과, 손 5개 초과, 모브 얼굴 3개 노출 불량
    _mock_qa_detectors(monkeypatch, persons=3, heads=2, hands=4, faces=2)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=False, is_two_person=True)
    assert ok is False and "제3자/남성 증식" in reason

    _mock_qa_detectors(monkeypatch, persons=2, heads=2, hands=5, faces=2)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=False, is_two_person=True)
    assert ok is False and "손 증식" in reason

    _mock_qa_detectors(monkeypatch, persons=2, heads=2, hands=4, faces=3)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=False, is_two_person=True)
    assert ok is False and "얼굴 과다 노출" in reason

    # 2인 씬: 정상 통과
    _mock_qa_detectors(monkeypatch, persons=2, heads=2, hands=4, faces=2)
    ok, reason, _ = qa_mod.validate_generated_image(sample, is_solo_scene=False, is_two_person=True)
    assert ok is True and reason == ""


# ── 3. generator/censor.py ──


@pytest.mark.parametrize(
    "style",
    [
        "white_silhouette",
        "black_silhouette",
        "wide_bar",
        "slim_bar",
        "shadow",
        "mosaic",
    ],
)
def test_censor_image_all_styles_render_offline(
    style: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """censor_image()의 모든 검열 스타일 분기가 오프라인 OpenCV 폴백 환경에서 정상 렌더링되어야 한다."""
    # 전경/배경 대비가 뚜렷한 합성 이미지 생성 (GrabCut 실루엣 및 모자이크 픽셀 변화 검증용)
    img = Image.new("RGB", (128, 128), (30, 50, 90))
    for y in range(44, 100):
        for x in range(36, 92):
            img.putpixel((x, y), (220 if (x + y) % 4 < 2 else 180, 140, 120))

    monkeypatch.setattr(censor_mod, "HAS_IMGUTILS", True)
    monkeypatch.setattr(
        censor_mod,
        "detect_censors",
        lambda _im: [((36, 44, 92, 100), "penis", 0.92)],
    )
    monkeypatch.setattr(censor_mod, "get_sam_model", lambda: None)

    censored, count = censor_mod.censor_image(img, style=style, targets=("penis", "pussy"))
    assert count == 1
    assert censored.size == img.size
    assert censored.tobytes() != img.tobytes()


# ── 4. tools/censor_studio.py ──


def test_censor_studio_v2_filtering_and_origin_backup_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """censor_studio가 V2 H-씬만 필터링하고 /api/save 시 assets/{char}_origin/ 백업 후 솔리드 바를 저장해야 한다."""
    char_dir = tmp_path / "ignis"
    char_dir.mkdir(parents=True)
    origin_ignored = tmp_path / "ignis_origin"
    origin_ignored.mkdir(parents=True)

    h_img = char_dir / "ignis_080_missionary.webp"
    otk_img = char_dir / "ignis_150.webp"
    skip_non_exp = char_dir / "ignis_101_afterglow.webp"
    skip_emotion = char_dir / "ignis_005_happy.webp"
    origin_dummy = origin_ignored / "ignis_080_missionary.webp"

    orig_color = (40, 120, 200)
    for p in (h_img, otk_img, skip_non_exp, skip_emotion, origin_dummy):
        Image.new("RGB", (64, 64), orig_color).save(p, format="WEBP")
    # _origin 파일은 기존 테스트용이 아니라 /api/save의 최초 백업 검증을 위해 제거
    origin_dummy.unlink()

    filtered = censor_studio.get_image_list(str(tmp_path), filter_h=True)
    names = [item["filename"] for item in filtered]
    assert names == ["ignis_080_missionary.webp", "ignis_150.webp"]
    assert all(item["is_h"] is True for item in filtered)

    if censor_studio.HAS_FLASK and censor_studio.app is not None:
        monkeypatch.setattr(censor_studio, "TARGET_DIR", str(tmp_path))
        monkeypatch.setattr(censor_studio, "BACKUP_ENABLED", True)
        client = censor_studio.app.test_client()

        # Path Traversal 차단 확인
        res_forbidden = client.get("/api/image", query_string={"path": str(tmp_path.parent / "secret.webp")})
        assert res_forbidden.status_code == 403

        # 정상 저장 + _origin 최초 1회 백업 생성 확인
        res_save = client.post(
            "/api/save",
            data=json.dumps({
                "path": str(h_img),
                "bars": [{"cx": 32, "cy": 32, "width": 30, "height": 16, "angle": 0}],
            }),
            content_type="application/json",
        )
        assert res_save.status_code == 200
        assert res_save.get_json()["success"] is True

        backup_path = tmp_path / "ignis_origin" / h_img.name
        assert backup_path.is_file()
        with Image.open(backup_path) as bak:
            r, g, b = bak.convert("RGB").getpixel((32, 32))
            assert abs(b - orig_color[2]) <= 15
        with Image.open(h_img) as edited:
            r, g, b = edited.convert("RGB").getpixel((32, 32))
            assert r < 20 and g < 20 and b < 20

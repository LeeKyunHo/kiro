"""
tests/test_port_p2a_censor_utils.py
Wave P2-A (검열 코어 안정화 · 스타일 모듈 분리 + 실무 유틸 4종 이식) 오프라인 단위·통합 테스트.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from PIL import Image
import pytest

from generator import censor as censor_mod
from generator import censor_styles as styles_mod
from generator.config import ConfigError
from generator.genit_exporter import GenitExporter, denormalize_genit_code
from generator.models import PoseEntry, RosterPaths
from generator.pose_background import PoseBackgroundService
from generator.pose_db import load_pose_db, load_pose_sets, resolve_pose_set_codes
from generator.prompt import compute_deterministic_pose_seed


def _make_contrast_image(size: tuple[int, int] = (200, 200)) -> Image.Image:
    """전경/배경 대비가 뚜렷한 합성 테스트 이미지를 생성한다."""
    w, h = size
    img = Image.new("RGB", (w, h), (30, 60, 120))
    x0, y0, x1, y1 = int(w * 0.30), int(h * 0.25), int(w * 0.70), int(h * 0.75)
    for y in range(y0, y1):
        for x in range(x0, x1):
            img.putpixel((x, y), (235 if (x + y) % 4 < 2 else 195, 165, 145))
    return img


# ── 1. censor_styles: 동적 스케일 커널, 스타일 최적화 및 신규 스타일, re-export 호환 ──


def test_censor_styles_dynamic_scale_kernels_and_reexports() -> None:
    """832px 기준 vs 4K 업스케일 동적 커널 비례 계산 및 censor.py 하위 호환 re-export를 검증한다."""
    s_base = styles_mod._compute_scale_factor(832, 1216)
    s_4k = styles_mod._compute_scale_factor(3328, 4864)
    assert s_base == pytest.approx(1.0)
    assert s_4k == pytest.approx(4.0)

    assert styles_mod._scale_int(14, s_base) == 14
    assert styles_mod._scale_int(14, s_4k) == 56

    k_base = styles_mod._odd_kernel(9, s_base)
    k_4k = styles_mod._odd_kernel(9, s_4k)
    assert k_base % 2 == 1 and k_4k % 2 == 1 and k_4k > k_base * 3

    # generator.censor 하위 호환 re-export 확인
    for sym in (
        "_compute_scale_factor",
        "_scale_int",
        "_odd_kernel",
        "apply_wide_solid_bar",
        "apply_slim_black_bar",
        "apply_shadow_silhouette",
        "apply_mosaic",
        "apply_manga_bar",
        "apply_manga_glow",
        "apply_debug_bbox",
        "_render_censored_style",
    ):
        assert hasattr(censor_mod, sym)


def test_censor_styles_shadow_mosaic_manga_and_debug_rendering() -> None:
    """shadow 페더링, mosaic ROI 국소화, manga_bar, manga_glow, debug_bbox가 정상 렌더링되어야 한다."""
    img = _make_contrast_image((200, 200))
    box = (60, 50, 140, 150)

    # 1) shadow: 타원 소프트 페더링 -> 중앙은 어두워지고, 바깥 모서리는 원본 유지
    shadow_out = styles_mod.apply_shadow_silhouette(img, [box])
    assert shadow_out.size == img.size
    center_orig = img.getpixel((100, 100))
    center_shad = shadow_out.getpixel((100, 100))
    assert sum(center_shad) < sum(center_orig) * 0.5
    assert shadow_out.getpixel((5, 5)) == img.getpixel((5, 5))

    # 2) mosaic: ROI 국소 리사이즈 -> ROI 내부는 변형되고, 외부 원거리 픽셀은 완전 보존
    mosaic_out = styles_mod.apply_mosaic(img, [box])
    assert mosaic_out.tobytes() != img.tobytes()
    assert mosaic_out.getpixel((5, 5)) == img.getpixel((5, 5))

    # 3) manga_bar: 중앙 기둥 구간이 순수 검정(0,0,0)으로 덮임
    manga_bar_out = censor_mod._render_censored_style(img, [(box, "penis", 0.9)], style="manga_bar")
    assert manga_bar_out.getpixel((100, 100)) == (0, 0, 0)
    assert manga_bar_out.getpixel((5, 5)) == img.getpixel((5, 5))

    # 4) manga_glow: 중앙 코어가 순백(255,255,255)으로 발광
    manga_glow_out = censor_mod._render_censored_style(img, [(box, "penis", 0.9)], style="manga_glow")
    assert manga_glow_out.getpixel((100, 100)) == (255, 255, 255)
    assert manga_glow_out.getpixel((5, 5)) == img.getpixel((5, 5))

    # 5) debug_bbox: 하단 테두리에 라임 그린(0, 255, 64) 표시
    debug_out = censor_mod._render_censored_style(img, [(box, "penis", 0.87)], style="debug_bbox")
    assert debug_out.getpixel((box[0], box[3])) == (0, 255, 64)


# ── 2. censor 안정화: SAM Lock / ROI 크롭 / Unload / verify_and_heal / _atomic_replace_with_retry ──


def test_censor_sam_roi_crop_lock_and_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    """MobileSAM 추론이 전체 이미지 대신 패딩 ROI 크롭만 입력받고 unload_censor_models()가 싱글톤을 해제해야 한다."""
    img = _make_contrast_image((400, 400))
    seen_sizes: list[tuple[int, int]] = []

    class FakeSam:
        def __call__(self, crop_pil: Image.Image, bboxes: list[list[int]], verbose: bool = False) -> list[Any]:
            seen_sizes.append(crop_pil.size)
            cw, ch = crop_pil.size
            mask = np.ones((1, ch, cw), dtype=np.float32)
            return [SimpleNamespace(masks=SimpleNamespace(data=mask))]

    assert hasattr(censor_mod._SAM_MODEL_LOCK, "acquire")
    monkeypatch.setattr(censor_mod, "_SAM_MODEL", FakeSam())
    monkeypatch.setattr(censor_mod, "HAS_IMGUTILS", False)

    out = censor_mod.apply_white_silhouette(img, [((160, 160, 220, 220), "penis")])
    assert seen_sizes, "FakeSam이 호출되어야 합니다."
    assert seen_sizes[0][0] < 400 and seen_sizes[0][1] < 400
    assert out.getpixel((190, 190)) == (255, 255, 255)

    censor_mod.unload_censor_models()
    assert censor_mod._SAM_MODEL is None


def test_verify_and_heal_censor_and_atomic_replace_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_and_heal_censor 2차 보정 및 _atomic_replace_with_retry 일시 PermissionError 재시도를 검증한다."""
    img = _make_contrast_image((128, 128))
    monkeypatch.setattr(censor_mod, "HAS_IMGUTILS", True)

    # 1) 잔여 노출 감지 시 2차 보정 발동
    monkeypatch.setattr(
        censor_mod,
        "detect_censors",
        lambda _im, conf_threshold=0.32: [((40, 40, 88, 88), "penis", 0.55)],
    )
    healed_img, healed = censor_mod.verify_and_heal_censor(img, style="wide_bar", min_confidence=0.32)
    assert healed is True
    assert healed_img.getpixel((64, 64)) == (0, 0, 0)

    # 2) 임계치 미만일 때는 보정 미발동
    monkeypatch.setattr(
        censor_mod,
        "detect_censors",
        lambda _im, conf_threshold=0.32: [((40, 40, 88, 88), "penis", 0.18)],
    )
    unchanged_img, healed2 = censor_mod.verify_and_heal_censor(img, style="wide_bar", min_confidence=0.32)
    assert healed2 is False
    assert unchanged_img is img

    # 3) _atomic_replace_with_retry: 2회 PermissionError 후 3회째 성공
    src = tmp_path / ".tmp_asset.webp"
    dst = tmp_path / "asset.webp"
    src.write_bytes(b"new-content")
    dst.write_bytes(b"old-content")

    attempts = {"n": 0}
    real_replace = os.replace

    def flaky_replace(a: Any, b: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("file locked by explorer")
        real_replace(a, b)

    monkeypatch.setattr(censor_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(censor_mod.time, "sleep", lambda _s: None)

    censor_mod._atomic_replace_with_retry(src, dst, retries=4, delay=0.01)
    assert attempts["n"] == 3
    assert dst.read_bytes() == b"new-content"
    assert not src.exists()


# ── 3. PoseBackgroundService: 단일 코드/범위 매핑, 플랫 background.json, 명시 우선순위, 폴백 ──


def test_pose_background_service_mapping_priority_and_fallback(tmp_path: Path) -> None:
    """PoseBackgroundService가 단일 코드>범위>기본 배경 우선순위 및 플랫 background.json 연동을 정확히 수행해야 한다."""
    roster_dir = tmp_path / "demo_roster"
    roster_dir.mkdir(parents=True)

    (roster_dir / "background.json").write_text(
        json.dumps(
            {
                "default": "sunny campus hallway",
                "bedroom": "warm private bedroom",
                "bath": "steamy wooden bathhouse",
                "night": "moonlit terrace",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (roster_dir / "pose_background_mapping.json").write_text(
        json.dumps(
            {
                "default_background": "default",
                "pose_mappings": {
                    "044": "bath",
                    "000-059": "default",
                    "080-129": "bedroom",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    svc = PoseBackgroundService(projects_dir=tmp_path)
    assert svc.has_mapping(roster_dir) is True

    # 단일 코드("044")가 범위("000-059")보다 우선
    assert svc.get_background_for_pose(roster_dir, 44) == "bath"
    assert svc.get_background_for_pose(roster_dir, 10) == "default"
    assert svc.get_background_for_pose(roster_dir, 88) == "bedroom"

    # 플랫 background.json 프롬프트 해석
    assert svc.resolve_pose_background(roster_dir, 44) == "steamy wooden bathhouse"
    assert svc.resolve_pose_background(roster_dir, 88) == "warm private bedroom"

    # 명시 CLI --bg 및 명시 프리셋(--bg-preset night) 최우선
    assert svc.resolve_pose_background(roster_dir, 88, cli_bg="custom studio") == "custom studio"
    assert svc.resolve_pose_background(roster_dir, 88, explicit_preset="night") == "moonlit terrace"

    # 매핑 파일 없는 로스터 -> 기존 플랫 default 폴백
    plain_dir = tmp_path / "plain_roster"
    plain_dir.mkdir()
    (plain_dir / "background.json").write_text(
        json.dumps({"default": "plain default bg"}), encoding="utf-8"
    )
    assert svc.has_mapping(plain_dir) is False
    assert svc.resolve_pose_background(plain_dir, 88) == "plain default bg"


# ── 4. pose_sets.json V2 정합성(Contract) & 결정론적 포즈 시드 ──


def test_pose_sets_v2_contract_and_deterministic_pose_seed() -> None:
    """pose_sets.json의 모든 코드가 실제 pose_database.json에 존재하고 결정론적 시드가 재현 가능해야 한다."""
    repo_root = Path(__file__).resolve().parent.parent
    db = load_pose_db(repo_root)
    sets = load_pose_sets(repo_root)

    assert {"sample10", "daily_all", "romance_all", "h_female_all", "h_otokonoko_all", "female_full", "otokonoko_full"} <= set(sets.keys())

    for set_id, entry in sets.items():
        codes = entry["codes"]
        assert codes, f"{set_id} 코드 목록이 비어 있습니다."
        missing = [int(c) for c in codes if int(c) not in db.entries]
        assert not missing, f"pose_sets.json '{set_id}'에 DB 미존재 코드 포함: {missing}"

    sample_expr = resolve_pose_set_codes(repo_root, "sample10")
    assert len(sample_expr.split(",")) == 10

    with pytest.raises(ConfigError):
        resolve_pose_set_codes(repo_root, "nonexistent_pose_set")

    # CRC32 결정론적 시드 재현성 및 구별성 검증
    s1 = compute_deterministic_pose_seed(80, "ignis")
    s2 = compute_deterministic_pose_seed("080", "ignis")
    s_other_pose = compute_deterministic_pose_seed(81, "ignis")
    s_other_char = compute_deterministic_pose_seed(80, "rei")
    assert s1 == s2
    assert s1 != s_other_pose
    assert s1 != s_other_char
    assert s1 >= 0


# ── 5. GenitExporter: 임시 로스터 + genit_map.json + _origin 제외 ──


def test_genit_exporter_with_genit_map_and_origin_exclusion(tmp_path: Path) -> None:
    """GenitExporter가 genit_map.json 매핑, code/hybrid 파일명 파싱, _origin 제외 및 매핑 시트를 정상 생성해야 한다."""
    repo_root = Path(__file__).resolve().parent.parent
    db = load_pose_db(repo_root)

    roster_dir = tmp_path / "projects" / "demo"
    chars_dir = roster_dir / "characters"
    refs_dir = roster_dir / "references"
    assets_dir = roster_dir / "assets"
    ignis_dir = assets_dir / "ignis"
    ignis_origin = assets_dir / "ignis_origin"
    for d in (chars_dir, refs_dir, ignis_dir, ignis_origin):
        d.mkdir(parents=True)

    (roster_dir / "genit_map.json").write_text(
        json.dumps({"ignis": "AA"}, ensure_ascii=False),
        encoding="utf-8",
    )

    # hybrid + code 파일명 에셋 생성 & _origin 내 동일/추가 파일 생성
    Image.new("RGB", (32, 32), (100, 120, 140)).save(ignis_dir / "ignis_000_평상.webp", format="WEBP")
    Image.new("RGB", (32, 32), (150, 160, 170)).save(ignis_dir / "ignis_080.webp", format="WEBP")
    Image.new("RGB", (32, 32), (10, 10, 10)).save(ignis_origin / "ignis_080.webp", format="WEBP")
    Image.new("RGB", (32, 32), (10, 10, 10)).save(ignis_origin / "ignis_081.webp", format="WEBP")

    roster = RosterPaths(
        roster_name="demo",
        characters_dir=chars_dir,
        references_dir=refs_dir,
        assets_dir=assets_dir,
    )

    assert denormalize_genit_code("A00") == "A0"
    assert denormalize_genit_code(7) == "007"

    exporter = GenitExporter(base_dir=repo_root)
    out_root = tmp_path / "dist_genit"
    results = exporter.export_roster(roster, output_dir=out_root, db=db)

    # ignis_origin 폴더는 제외되어 ignis 1명만 내보내기 되어야 함
    assert len(results) == 1
    char_prefix, count, target_dir, export_code = results[0]
    assert char_prefix == "ignis"
    assert export_code == "AA"
    assert count == 2
    assert (target_dir / "000.webp").is_file()
    assert (target_dir / "080.webp").is_file()
    assert not (target_dir / "081.webp").exists()

    sheet = target_dir / "_genit_prompt_sheet.md"
    assert sheet.is_file()
    sheet_text = sheet.read_text(encoding="utf-8")
    assert "`000`" in sheet_text and "`080`" in sheet_text and "`AA`" in sheet_text

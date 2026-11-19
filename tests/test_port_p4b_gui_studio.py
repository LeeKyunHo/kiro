"""
Wave P4-B 통합 검증 테스트:
1. gui/retouch_editor.py: 캔버스 도구(화이트/블랙 브러시·크기·지우개·Undo·확대/패닝)·터치/마우스·/api/retouch/save 연동
2. gui/gallery.py & gui/mobile_viewer.py & gui/app.py: V2 8섹션 필터·매트릭스 바인딩·/inspect 서빙·라우트 등록·kiro_gui 호환
3. scripts/export_context.py CLI 스모크 실행 검증
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generator.asset_manager import AssetManager
from generator.pose_db import V2_BUILTIN_SECTIONS
import gui
from gui.gallery import (
    GRID_CHUNK_SIZE,
    MATRIX_ROW_CHUNK_SIZE,
    V2_SECTION_LABELS,
    build_image_api_url,
    filter_gallery_matrix_by_section,
    pick_default_character,
)
from gui.mobile_viewer import generate_mobile_viewer_html, register_mobile_viewer_route
from gui.retouch_editor import (
    RETOUCH_EDITOR_CSS,
    RETOUCH_EDITOR_HTML,
    build_retouch_editor_script,
)
import kiro_gui


def test_retouch_editor_html_and_script_contract() -> None:
    """공용 리터치 캔버스 에디터가 필수 도구·터치/마우스 핸들러·/api/retouch/save·캐시버스팅을 포함해야 한다."""
    assert "#kiroEditorCanvas" in RETOUCH_EDITOR_CSS
    for required_id in (
        "kiroRetouchModal",
        "kiroEditorCanvas",
        "kiroBtnWhiteBrush",
        "kiroBtnBlackBrush",
        "kiroBtnEraser",
        "kiroBtnPan",
        "kiroBrushSize",
        "kiroBtnLoadOrigin",
        "kiroBtnRetouchSave",
    ):
        assert required_id in RETOUCH_EDITOR_HTML

    script = build_retouch_editor_script(on_saved_js="window._onSavedTest(savedPayload);")
    for token in (
        "pointerdown",
        "pointermove",
        "touchstart",
        "touchmove",
        "white_brush",
        "black_brush",
        "eraser",
        "undoKiroRetouchStroke",
        "adjustKiroRetouchZoom",
        "/api/retouch/save",
        "X-Kiro-Token",
        "sessionStorage",
        "Date.now()",
        "window._onSavedTest(savedPayload);",
    ):
        assert token in script, f"리터치 스크립트 필수 토큰 누락: {token}"


def test_gallery_v2_section_filter_and_matrix_binding(tmp_path: Path) -> None:
    """PC 갤러리 V2 8개 섹션 필터·매트릭스 데이터 바인딩·/api/image URL 생성을 검증한다."""
    for sec in V2_BUILTIN_SECTIONS:
        assert sec in V2_SECTION_LABELS

    pose_db = {
        "emotions": {"0": {"label": "평상", "prompt": "standing, neutral"}},
        "events": {"40": {"label": "침대", "prompt": "sitting on bed"}},
        "h_scenes": {"80": {"label": "정상위", "prompt": "missionary on bed"}},
    }
    (tmp_path / "pose_database.json").write_text(json.dumps(pose_db), encoding="utf-8")

    rdir = tmp_path / "projects" / "demo"
    (rdir / "characters").mkdir(parents=True)
    (rdir / "assets" / "rei").mkdir(parents=True)
    (rdir / "assets" / "rei_origin").mkdir(parents=True)
    (rdir / "characters" / "rei.json").write_text(
        json.dumps({"prefix": "rei", "char_prompt": "1girl, silver hair", "gender": "female"}),
        encoding="utf-8",
    )
    (rdir / "assets" / "rei" / "rei_00_평상.webp").write_bytes(b"e00")
    (rdir / "assets" / "rei" / "rei_80_정상위.webp").write_bytes(b"h80")
    (rdir / "assets" / "rei_origin" / "rei_80_정상위.webp").write_bytes(b"orig80")

    matrix = AssetManager(tmp_path).build_gallery_matrix("demo")
    poses_all, chars_all, items_all = filter_gallery_matrix_by_section(matrix, "all")
    assert len(poses_all) == 3
    assert len(chars_all) == 1
    assert len(items_all) == 2

    poses_h, _, items_h = filter_gallery_matrix_by_section(matrix, "h_scenes")
    assert [p["code"] for p in poses_h] == [80]
    assert len(items_h) == 1
    assert items_h[0]["has_origin"] is True

    url_cur = build_image_api_url("demo/assets/rei/rei_80_정상위.webp", origin=False, version=12345)
    url_orig = build_image_api_url("demo/assets/rei/rei_80_정상위.webp", origin=True, version=12345)
    assert url_cur.startswith("/api/image?path=") and "origin=1" not in url_cur and "t=12345" in url_cur
    assert "origin=1" in url_orig and "t=12345" in url_orig

    # 첫 캐릭터 기본 선택 및 순차 청크 크기 검증
    assert GRID_CHUNK_SIZE == 24
    assert MATRIX_ROW_CHUNK_SIZE == 16
    assert pick_default_character([]) == "all"
    assert (
        pick_default_character(
            [
                {"prefix": "empty_char", "total_assets": 0},
                {"prefix": "active_char", "total_assets": 75},
            ]
        )
        == "active_char"
    )
    assert pick_default_character([{"prefix": "zero_only", "total_assets": 0}]) == "zero_only"


def test_mobile_viewer_inspect_route_and_kiro_gui_exports() -> None:
    """/inspect 모바일 검수 라우트 서빙, 중복 등록 방지 및 kiro_gui / gui 공개 심볼 호환성을 검증한다."""
    app = FastAPI()
    assert register_mobile_viewer_route(app) is True
    assert register_mobile_viewer_route(app) is False  # idempotent

    client = TestClient(app)
    resp = client.get("/inspect", params={"roster": "dark_generals"})
    assert resp.status_code == 200
    body = resp.text
    for expected in (
        "/api/rosters",
        "/api/gallery/assets",
        "/api/image",
        "/api/recensor",
        "/api/restore_origin",
        "/api/retouch/save",
        "/api/asset/delete",
        "/api/asset/restore",
        "/api/reroll",
        "kiro_api_token",
        "openKiroRetouchEditor",
    ):
        assert expected in body, f"/inspect HTML 누락 요소: {expected}"

    # gui & kiro_gui 공개 심볼 호환 확인
    assert callable(gui.render_gallery_page)
    assert callable(gui.register_studio_pages)
    assert callable(gui.register_mobile_viewer_route)
    assert callable(gui.build_retouch_editor_script)
    assert kiro_gui.build_gui is gui.build_gui
    assert kiro_gui.KiroGuiState is gui.GuiRuntimeState


def test_export_context_cli_smoke(tmp_path: Path) -> None:
    """python scripts/export_context.py CLI 실행이 Kiro 단독으로 4개 스냅샷 문서를 오류 없이 생성해야 한다."""
    out_dir = tmp_path / "ctx_smoke"
    proc = subprocess.run(
        [sys.executable, "scripts/export_context.py", "--out", str(out_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert "[export_context] snapshot_id:" in proc.stdout
    for fname in (
        "00_README_FOR_AI.md",
        "10_TIER1_CONSTITUTION.md",
        "20_TIER2_CORE_LOGIC.md",
        "30_TIER3_RUNTIME.md",
        ".hashes.json",
    ):
        assert (out_dir / fname).is_file()

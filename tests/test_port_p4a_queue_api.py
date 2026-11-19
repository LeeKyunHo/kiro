"""
Wave P4-A 통합 검증 테스트:
1. generator/asset_manager.py: 플랫 에셋 탐색·매트릭스·휴지통/Undo·_origin 복원·리터치 원자 저장·Path Traversal 차단
2. gui/job_manager.py: FIFO 순차 직렬 실행(동시성 0), 개별/실행 중 취소, 실패 격리
3. gui/api_routes.py: FastAPI TestClient 기반 전 엔드포인트, Idempotent 마운트, Path Traversal 403, KIRO_API_TOKEN 인증
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

import generator.censor as censor_mod
from generator.asset_manager import AssetManager
from gui.api_routes import register_api_routes
from gui.job_manager import GlobalJobManager, JobExecutionContext


def _save_solid_webp(path: Path, rgb: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, rgb).save(path, format="WEBP", quality=95)


def _encode_png_data_url(rgb: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_temp_kiro_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    """임시 Kiro 플랫 로스터(demo/rei)와 pose_database.json 을 생성한다."""
    pose_db = {
        "emotions": {
            "0": {"label": "평상", "prompt": "standing, neutral"},
            "1": {"label": "미소", "prompt": "standing, smile"},
        },
        "h_scenes": {
            "80": {"label": "정상위", "prompt": "missionary on bed", "required_outfit": "nude"},
        },
    }
    (tmp_path / "pose_database.json").write_text(json.dumps(pose_db), encoding="utf-8")

    roster_dir = tmp_path / "projects" / "demo"
    chars_dir = roster_dir / "characters"
    char_dir = roster_dir / "assets" / "rei"
    origin_dir = roster_dir / "assets" / "rei_origin"
    chars_dir.mkdir(parents=True)
    char_dir.mkdir(parents=True)
    origin_dir.mkdir(parents=True)

    (chars_dir / "rei.json").write_text(
        json.dumps({"prefix": "rei", "char_prompt": "1girl, silver hair", "gender": "female"}),
        encoding="utf-8",
    )

    img_000 = char_dir / "rei_000_평상.webp"
    img_080 = char_dir / "rei_080_정상위.webp"
    orig_080 = origin_dir / "rei_080_정상위.webp"

    _save_solid_webp(img_000, (30, 80, 200))
    _save_solid_webp(img_080, (245, 245, 245))   # 검열본 (흰색)
    _save_solid_webp(orig_080, (200, 50, 60))    # 무검열 원본 (빨강)

    return tmp_path, img_000, img_080


# ── 1. AssetManager 단위 테스트 ──


def test_asset_manager_matrix_trash_undo_origin_restore_retouch_and_traversal(tmp_path: Path) -> None:
    """AssetManager 의 플랫 스캔, 휴지통/Undo, _origin 복원, 리터치 백업/저장 및 경로 탈출 차단을 검증한다."""
    root, img_000, img_080 = _build_temp_kiro_root(tmp_path)
    orig_080 = img_080.parent.parent / "rei_origin" / img_080.name
    mgr = AssetManager(root)

    # 1) 로스터/매트릭스 조회
    assert mgr.list_rosters() == ["demo"]
    matrix = mgr.build_gallery_matrix("demo")
    assert matrix["roster"] == "demo"
    assert len(matrix["characters"]) == 1
    ch_row = matrix["characters"][0]
    assert ch_row["prefix"] == "rei"
    assert ch_row["total_assets"] == 2
    assert ch_row["origin_count"] == 1
    assert ch_row["assets_by_code"]["80"]["has_origin"] is True
    assert ch_row["assets_by_code"]["00"]["has_origin"] is False

    # 2) Path Traversal 및 _origin 직접 수정 차단
    with pytest.raises(PermissionError):
        mgr.resolve_safe_path("../pose_database.json")
    with pytest.raises(PermissionError):
        mgr.delete_asset_bundle(orig_080)

    # 3) 휴지통 번들 삭제(본체 + _origin + manifest.json) -> Undo 100% 복원
    orig_main_bytes = img_080.read_bytes()
    orig_bak_bytes = orig_080.read_bytes()
    del_res = mgr.delete_asset_bundle("demo/assets/rei/rei_080_정상위.webp")
    assert del_res["success"] is True
    assert del_res["moved_count"] == 2
    assert not img_080.exists()
    assert not orig_080.exists()
    trash_manifest = root / "projects" / "demo" / "assets" / "_trash" / del_res["trash_id"] / "manifest.json"
    assert trash_manifest.is_file()

    undo_res = mgr.restore_asset_bundle(del_res["trash_id"], roster="demo")
    assert undo_res["success"] is True
    assert img_080.read_bytes() == orig_main_bytes
    assert orig_080.read_bytes() == orig_bak_bytes
    assert not trash_manifest.exists()

    # 4) 무검열 원본 1클릭 복원 (_origin 자체는 무손실 유지)
    restore_res = mgr.restore_from_origin("demo/assets/rei/rei_080_정상위.webp")
    assert restore_res["success"] is True
    assert orig_080.is_file()
    with Image.open(img_080) as im:
        r, g, b = im.convert("RGB").getpixel((16, 16))
        assert r > 170 and g < 80

    # 5) 리터치 Base64 저장: 최초 리터치 시 _origin 선제 백업 생성 후 WebP 원자 덮어쓰기
    orig_000 = img_000.parent.parent / "rei_origin" / img_000.name
    assert not orig_000.exists()
    before_000_bytes = img_000.read_bytes()
    retouch_res = mgr.save_retouched_image(
        "demo/assets/rei/rei_000_평상.webp",
        _encode_png_data_url((20, 210, 80)),
    )
    assert retouch_res["success"] is True
    assert retouch_res["origin_backed_up"] is True
    assert orig_000.read_bytes() == before_000_bytes
    with Image.open(img_000) as im_ret:
        r, g, b = im_ret.convert("RGB").getpixel((16, 16))
        assert g > 180 and r < 50


# ── 2. GlobalJobManager FIFO 직렬화 / 취소 / 실패 격리 테스트 ──


def test_global_job_manager_fifo_serial_cancel_and_failure_isolation() -> None:
    """GlobalJobManager 가 FIFO 순차 직렬 실행(동시 1건), 개별/실행 중 취소, 실패 격리를 보장해야 한다."""
    interrupted = {"count": 0}
    jm = GlobalJobManager(interrupt_fn=lambda: interrupted.__setitem__("count", interrupted["count"] + 1))

    order: list[str] = []
    active_now = {"n": 0, "max": 0}
    lock = threading.Lock()
    gate1 = threading.Event()

    def _task_template(name: str, *, wait_ev: threading.Event | None = None, fail: bool = False) -> Any:
        def _run(ctx: JobExecutionContext) -> None:
            with lock:
                active_now["n"] += 1
                active_now["max"] = max(active_now["max"], active_now["n"])
                order.append(f"start:{name}")
            try:
                if wait_ev is not None:
                    wait_ev.wait(timeout=3.0)
                ctx.report_progress(1, 2, f"{name} half")
                if fail:
                    raise RuntimeError(f"boom:{name}")
                time.sleep(0.02)
                order.append(f"end:{name}")
            finally:
                with lock:
                    active_now["n"] -= 1
        return _run

    # 작업 1(게이트 대기), 작업 2(대기 중 취소할 대상), 작업 3(실패), 작업 4(실패 이후에도 정상 실행되어야 함)
    j1 = jm.enqueue_task(kind="batch", label="job1", fn=_task_template("j1", wait_ev=gate1))
    j2 = jm.enqueue_task(kind="reroll", label="job2", fn=_task_template("j2"))
    j3 = jm.enqueue_task(kind="reroll", label="job3", fn=_task_template("j3", fail=True))
    j4 = jm.enqueue_task(kind="reroll", label="job4", fn=_task_template("j4"))

    # j2 가 대기열에 있는 동안 개별 취소
    cancel_res = jm.cancel_job(j2["job_id"])
    assert cancel_res["cancelled"] is True
    assert cancel_res["job"]["status"] == "cancelled"

    # 게이트 개방 후 전체 완료 대기
    gate1.set()
    assert jm.wait_idle(timeout=5.0) is True

    assert active_now["max"] == 1, f"동시 실행 발생: max={active_now['max']}"
    assert order == ["start:j1", "end:j1", "start:j3", "start:j4", "end:j4"]
    assert jm.get_job(j1["job_id"])["status"] == "completed"
    assert jm.get_job(j2["job_id"])["status"] == "cancelled"
    assert jm.get_job(j3["job_id"])["status"] == "failed"
    assert "boom:j3" in (jm.get_job(j3["job_id"])["error"] or "")
    assert jm.get_job(j4["job_id"])["status"] == "completed"

    # 실행 중 작업 취소(request_cancel) 검증
    started_ev = threading.Event()

    def _cancellable_task(ctx: JobExecutionContext) -> None:
        started_ev.set()
        for _ in range(100):
            if ctx.is_cancelled():
                return
            time.sleep(0.01)

    jc = jm.enqueue_task(kind="batch", label="cancel-me", fn=_cancellable_task)
    assert started_ev.wait(timeout=2.0) is True
    cancelled_ids = jm.request_cancel()
    assert jc["job_id"] in cancelled_ids
    assert interrupted["count"] >= 1
    assert jm.wait_idle(timeout=3.0) is True
    assert jm.get_job(jc["job_id"])["status"] == "cancelled"


# ── 3. api_routes FastAPI TestClient 통합 및 보안 테스트 ──


def test_api_routes_endpoints_security_and_idempotent_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api_routes 의 전체 엔드포인트, 중복 마운트 방지, Path Traversal 차단 및 KIRO_API_TOKEN 인증을 검증한다."""
    root, img_000, img_080 = _build_temp_kiro_root(tmp_path)
    orig_080 = img_080.parent.parent / "rei_origin" / img_080.name
    monkeypatch.delenv("KIRO_API_TOKEN", raising=False)

    reroll_calls: list[tuple[str, str]] = []

    def fake_reroll_executor(opts: Any, roster: Any, hooks: Any) -> int:
        reroll_calls.append((opts.prefix, str(opts.codes)))
        return 0

    jm = GlobalJobManager(interrupt_fn=lambda: None)
    app = FastAPI()
    r1 = register_api_routes(app, root, job_manager=jm, reroll_executor=fake_reroll_executor)
    route_count_after_first = len(app.routes)
    r2 = register_api_routes(app, root, job_manager=jm, reroll_executor=fake_reroll_executor)
    assert r1 is not None
    assert r2 is None
    assert len(app.routes) == route_count_after_first

    client = TestClient(app)

    # 1) GET /api/rosters & /api/gallery/assets
    res_rosters = client.get("/api/rosters")
    assert res_rosters.status_code == 200
    assert res_rosters.json()["rosters"] == ["demo"]

    res_gallery = client.get("/api/gallery/assets", params={"roster": "demo"})
    assert res_gallery.status_code == 200
    assert res_gallery.json()["characters"][0]["total_assets"] == 2

    # 2) GET /api/image (정상, origin=1, Path Traversal 403)
    res_img = client.get("/api/image", params={"path": "demo/assets/rei/rei_080_정상위.webp"})
    assert res_img.status_code == 200
    assert "no-store" in res_img.headers.get("cache-control", "")
    res_orig = client.get("/api/image", params={"path": "demo/assets/rei/rei_080_정상위.webp", "origin": 1})
    assert res_orig.status_code == 200
    assert res_orig.content == orig_080.read_bytes()

    res_trav = client.get("/api/image", params={"path": "../../pose_database.json"})
    assert res_trav.status_code == 403

    # 3) POST /api/restore_origin
    res_restore = client.post("/api/restore_origin", json={"path": "demo/assets/rei/rei_080_정상위.webp"})
    assert res_restore.status_code == 200
    assert res_restore.json()["success"] is True

    # 4) POST /api/recensor (censor_file mock)
    def fake_censor_file(**kwargs: Any) -> Path:
        dst = Path(kwargs["dst_path"])
        _save_solid_webp(dst, (250, 250, 250))
        return dst

    monkeypatch.setattr(censor_mod, "censor_file", fake_censor_file)
    res_recensor = client.post(
        "/api/recensor",
        json={"path": "demo/assets/rei/rei_080_정상위.webp", "style": "manga_bar"},
    )
    assert res_recensor.status_code == 200
    assert res_recensor.json()["censored"] is True
    with Image.open(img_080) as im_c:
        assert im_c.convert("RGB").getpixel((16, 16))[0] > 230

    # 5) POST /api/retouch/save
    res_retouch = client.post(
        "/api/retouch/save",
        json={
            "path": "demo/assets/rei/rei_000_평상.webp",
            "image_base64": _encode_png_data_url((10, 200, 100)),
        },
    )
    assert res_retouch.status_code == 200
    assert res_retouch.json()["origin_backed_up"] is True

    # 6) POST /api/asset/delete -> POST /api/asset/restore
    res_del = client.post("/api/asset/delete", json={"path": "demo/assets/rei/rei_080_정상위.webp"})
    assert res_del.status_code == 200
    trash_id = res_del.json()["trash_id"]
    assert not img_080.exists()

    res_undo = client.post("/api/asset/restore", json={"trash_id": trash_id, "roster": "demo"})
    assert res_undo.status_code == 200
    assert img_080.is_file() and orig_080.is_file()

    # 7) POST /api/reroll & GET /api/jobs
    res_reroll = client.post(
        "/api/reroll",
        json={"roster": "demo", "char": "rei", "code": "080", "mock": True},
    )
    assert res_reroll.status_code == 200
    job_id = res_reroll.json()["job"]["job_id"]
    assert jm.wait_idle(timeout=5.0) is True
    assert reroll_calls == [("rei", "080")]

    res_jobs = client.get("/api/jobs")
    assert res_jobs.status_code == 200
    assert any(h["job_id"] == job_id and h["status"] == "completed" for h in res_jobs.json()["history"])

    # 8) KIRO_API_TOKEN 보안 가드: 미인증 401, 오토큰 401, X-Kiro-Token / Bearer 통과
    monkeypatch.setenv("KIRO_API_TOKEN", "secret-kiro-token-999")
    assert client.post("/api/restore_origin", json={"path": "demo/assets/rei/rei_080_정상위.webp"}).status_code == 401
    assert (
        client.post(
            "/api/restore_origin",
            json={"path": "demo/assets/rei/rei_080_정상위.webp"},
            headers={"X-Kiro-Token": "wrong"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/restore_origin",
            json={"path": "demo/assets/rei/rei_080_정상위.webp"},
            headers={"X-Kiro-Token": "secret-kiro-token-999"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/restore_origin",
            json={"path": "demo/assets/rei/rei_080_정상위.webp"},
            headers={"Authorization": "Bearer secret-kiro-token-999"},
        ).status_code
        == 200
    )

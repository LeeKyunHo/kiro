"""
gui/api_routes.py
Kiro Studio 검수·리터치·재검열·휴지통/Undo·FIFO 리롤 REST API (FastAPI / NiceGUI 공용).

보안 정책:
- 모든 파일 접근은 AssetManager.resolve_safe_path() 로 Path Traversal(is_relative_to) 차단.
- 모든 쓰기/실행(POST) 엔드포인트는 로컬호스트 기본 허용 + KIRO_API_TOKEN 환경변수 설정 시
  X-Kiro-Token 또는 Authorization: Bearer 헤더 상수시간(hmac.compare_digest) 인증 강제.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from generator.asset_manager import AssetManager, _atomic_copy_file, _atomic_replace_with_retry
from gui.job_manager import GlobalJobManager, global_job_manager


def get_optional_api_token() -> str:
    """환경변수 KIRO_API_TOKEN 반환 (미설정 시 빈 문자열 — 로컬 기본 허용)."""
    return os.environ.get("KIRO_API_TOKEN", "").strip()


async def require_write_auth(request: Request) -> None:
    """POST(쓰기/실행) 엔드포인트용 선택적 토큰 인증 의존성."""
    expected = get_optional_api_token()
    if not expected:
        return

    token = (request.headers.get("X-Kiro-Token") or "").strip()
    if not token:
        auth_header = (request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="유효하지 않은 API 인증 토큰입니다 (X-Kiro-Token 또는 Authorization: Bearer 필요).",
        )


# ── Pydantic 요청 스키마 ──────────────────────────────────────────

class PathRequest(BaseModel):
    path: str | None = None
    rel_path: str | None = None

    def resolved_path_input(self) -> str:
        val = (self.path or self.rel_path or "").strip()
        if not val:
            raise HTTPException(status_code=400, detail="path 필드가 필요합니다.")
        return val


class RecensorRequest(PathRequest):
    style: str = "white_silhouette"
    min_confidence: float = 0.16
    targets: list[str] = ["penis", "pussy"]
    enable_anchor_fallback: bool = False
    verify_heal: bool = False


class RetouchSaveRequest(PathRequest):
    image_base64: str
    quality: int = 95


class RestoreTrashRequest(BaseModel):
    trash_id: str
    roster: str | None = None


class RerollRequest(BaseModel):
    roster: str
    char: str
    code: str | int
    censor: bool = False
    censor_style: str = "white_silhouette"
    auto_qc: bool = False
    strict_censor_qc: bool = False
    max_retries: int = 3
    naming: str = "hybrid"
    seed: int | None = None
    mock: bool = False
    no_ref: bool = False
    steps: int | None = None
    cfg_scale: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    ckpt_profile: str | None = None


def _translate_fs_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def create_api_router(
    base_dir: Path,
    *,
    asset_manager: AssetManager | None = None,
    job_manager: GlobalJobManager | None = None,
    reroll_executor: Any = None,
) -> APIRouter:
    """검수·리터치·리롤·작업큐 REST API 라우터를 생성한다."""
    resolved_base = Path(base_dir).resolve()
    assets = asset_manager or AssetManager(resolved_base)
    jobs = job_manager or global_job_manager

    router = APIRouter(prefix="/api", tags=["Kiro Studio API"])
    write_deps = [Depends(require_write_auth)]

    # 1. 로스터 목록 & 갤러리 에셋 매트릭스
    @router.get("/rosters")
    async def api_list_rosters() -> dict[str, Any]:
        rosters = assets.list_rosters()
        return {"rosters": rosters, "count": len(rosters)}

    @router.get("/gallery/assets")
    async def api_gallery_assets(
        roster: str = Query(..., description="조회할 로스터명"),
        char: str | None = Query(None, description="특정 캐릭터 필터 (선택)"),
    ) -> dict[str, Any]:
        try:
            return assets.build_gallery_matrix(roster, char_filter=char)
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    # 2. 현재 이미지 또는 origin=1 무검열 원본 이미지 서빙 (Path Traversal 차단 + 버전별 스마트 캐시)
    @router.get("/image")
    async def api_get_image(
        path: str = Query(..., description="projects/ 기준 상대경로 또는 절대경로"),
        origin: int = Query(0, description="1 지정 시 _origin 무검열 원본 반환"),
        t: int | None = Query(None, description="캐시 버스팅 타임스탬프 버전 (지정 시 브라우저 캐시 허용)"),
    ) -> FileResponse:
        try:
            target = assets.resolve_safe_path(path)
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

        if origin == 1:
            origin_file = assets.find_origin_file(target)
            if origin_file is None or not origin_file.is_file():
                raise HTTPException(status_code=404, detail=f"무검열 원본(_origin) 파일이 없습니다: {path}")
            serve_path = origin_file
        else:
            if not target.is_file():
                raise HTTPException(status_code=404, detail=f"이미지 파일을 찾을 수 없습니다: {path}")
            serve_path = target

        cache_header = (
            "private, max-age=3600"
            if t is not None
            else "no-store, no-cache, must-revalidate, max-age=0"
        )
        return FileResponse(
            path=str(serve_path),
            headers={"Cache-Control": cache_header},
        )

    # 3. 무검열 원본 1클릭 복원
    @router.post("/restore_origin", dependencies=write_deps)
    async def api_restore_origin(req: PathRequest) -> dict[str, Any]:
        try:
            return assets.restore_from_origin(req.resolved_path_input())
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    # 4. 단일 에셋 즉시 재검열 (선제 _origin 백업 유지 + 원자적 교체)
    @router.post("/recensor", dependencies=write_deps)
    async def api_recensor(req: RecensorRequest) -> dict[str, Any]:
        raw_path = req.resolved_path_input()
        try:
            target = assets.resolve_safe_path(raw_path)
            assets._ensure_not_origin_path(target)
            assets._ensure_not_trash_path(target)
            if not target.is_file():
                raise FileNotFoundError(f"재검열 대상 에셋을 찾을 수 없습니다: {raw_path}")

            # 선제 _origin 백업 보장 및 클린 원본 소스 선택
            origin_path = assets.find_origin_file(target)
            if origin_path is None:
                origin_dst = assets.get_origin_path(target).resolve()
                if not origin_dst.is_relative_to(assets.projects_dir):
                    raise PermissionError("허용되지 않은 _origin 백업 경로입니다.")
                _atomic_copy_file(target, origin_dst)
                origin_path = origin_dst

            from generator.censor import censor_file, extract_pose_code

            pose_code = extract_pose_code(target.name)
            roster_name = target.relative_to(assets.projects_dir).parts[0]
            tmp_dst = target.with_name(f".recensor_{uuid.uuid4().hex[:8]}{target.suffix or '.webp'}")
            try:
                saved = censor_file(
                    src_path=origin_path,
                    dst_path=tmp_dst,
                    style=req.style,
                    targets=tuple(req.targets or ["penis", "pussy"]),
                    min_confidence=float(req.min_confidence),
                    pose_code=pose_code,
                    enable_anchor_fallback=bool(req.enable_anchor_fallback),
                    roster=roster_name,
                    verify_heal=bool(req.verify_heal),
                )
                censored_applied = saved is not None and Path(saved).is_file()
                if censored_applied:
                    _atomic_replace_with_retry(Path(saved), target)
                elif tmp_dst.exists():
                    tmp_dst.unlink(missing_ok=True)
            finally:
                if tmp_dst.exists():
                    tmp_dst.unlink(missing_ok=True)

            return {
                "success": True,
                "censored": censored_applied,
                "style": req.style,
                "rel_path": assets._to_projects_rel(target),
                "origin_rel_path": assets._to_projects_rel(origin_path),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    # 5. 인라인 리터치 캔버스 Base64 저장
    @router.post("/retouch/save", dependencies=write_deps)
    async def api_retouch_save(req: RetouchSaveRequest) -> dict[str, Any]:
        try:
            return assets.save_retouched_image(
                req.resolved_path_input(),
                req.image_base64,
                quality=req.quality,
            )
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    # 6. 안전 휴지통 삭제 및 Undo 복원
    @router.post("/asset/delete", dependencies=write_deps)
    async def api_asset_delete(req: PathRequest) -> dict[str, Any]:
        try:
            return assets.delete_asset_bundle(req.resolved_path_input())
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    @router.post("/asset/restore", dependencies=write_deps)
    async def api_asset_restore(req: RestoreTrashRequest) -> dict[str, Any]:
        try:
            return assets.restore_asset_bundle(req.trash_id, roster=req.roster)
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    # 7. FIFO 작업 큐: 단건 리롤 등록, 큐 상태 조회, 개별 작업 취소
    @router.post("/reroll", dependencies=write_deps)
    async def api_reroll(req: RerollRequest) -> dict[str, Any]:
        try:
            safe_roster = assets._validate_safe_token(req.roster, "로스터명")
            safe_char = assets._validate_safe_token(req.char, "캐릭터명")
            job = jobs.enqueue_reroll(
                resolved_base,
                safe_roster,
                safe_char,
                req.code,
                options_overrides={
                    "censor": req.censor,
                    "censor_style": req.censor_style,
                    "auto_qc": req.auto_qc,
                    "strict_censor_qc": req.strict_censor_qc,
                    "max_retries": req.max_retries,
                    "naming": req.naming,
                    "seed": req.seed,
                    "mock": req.mock,
                    "no_ref": req.no_ref,
                    "steps": req.steps,
                    "cfg_scale": req.cfg_scale,
                    "sampler": req.sampler,
                    "scheduler": req.scheduler,
                    "ckpt_profile": req.ckpt_profile,
                },
                executor=reroll_executor,
            )
            return {"success": True, "job": job}
        except Exception as exc:
            raise _translate_fs_error(exc) from exc

    @router.get("/jobs")
    async def api_get_jobs() -> dict[str, Any]:
        return jobs.get_status()

    @router.post("/jobs/{job_id}/cancel", dependencies=write_deps)
    async def api_cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router


def register_api_routes(
    app: Any,
    base_dir: Path,
    *,
    asset_manager: AssetManager | None = None,
    job_manager: GlobalJobManager | None = None,
    reroll_executor: Any = None,
) -> APIRouter | None:
    """FastAPI / NiceGUI 앱에 Kiro REST API 라우터를 중복 없이 1회 안전 마운트한다."""
    state_obj = getattr(app, "state", None)
    if state_obj is not None and getattr(state_obj, "_kiro_api_routes_registered", False):
        return None
    if getattr(app, "_kiro_api_routes_registered", False):
        return None

    router = create_api_router(
        base_dir,
        asset_manager=asset_manager,
        job_manager=job_manager,
        reroll_executor=reroll_executor,
    )
    app.include_router(router)
    if state_obj is not None:
        setattr(state_obj, "_kiro_api_routes_registered", True)
    else:
        setattr(app, "_kiro_api_routes_registered", True)
    return router


__all__ = [
    "create_api_router",
    "get_optional_api_token",
    "register_api_routes",
    "require_write_auth",
]

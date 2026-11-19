"""
generator.asset_manager
Kiro 플랫 에셋 구조(projects/{roster}/assets/{char}/ + {char}_origin/) 전용
에셋 인덱싱·매트릭스 조회·안전 휴지통(Trash/Undo)·무검열 원본 복원·리터치 원자 저장 서비스.

- Path Traversal 방지: 모든 파일 경로는 Path.resolve().is_relative_to(projects_dir) 강제 검증.
- 원자적 교체: 임시 파일 생성 후 백오프 재시도 os.replace(_atomic_replace_with_retry) 적용.
- Lazy ML Load 보장: torch / ultralytics / imgutils 를 절대 임포트하지 않음.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
import uuid

from PIL import Image

from generator.config import PROJECTS_DIRNAME
from generator.pose_db import expand_v2_mode, load_pose_db, resolve_targets
from generator.reporter import code_width, format_code
from generator.roster import RosterPathManager, load_character

logger = logging.getLogger(__name__)

_IMAGE_EXTS: frozenset[str] = frozenset({".webp", ".png", ".jpg", ".jpeg"})
_POSE_CODE_PATTERN = re.compile(r"_(\d{2,4})(?:_|\.)")
_SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TRASH_DIRNAME = "_trash"


def _atomic_replace_with_retry(
    temp_dst: Path,
    target_dst: Path,
    retries: int = 5,
    delay: float = 0.08,
) -> None:
    """Windows 파일 락 일시 충돌 시 지수 백오프로 원자적 os.replace 를 수행한다."""
    last_err: OSError | None = None
    attempts = max(1, int(retries))
    for idx in range(attempts):
        try:
            os.replace(temp_dst, target_dst)
            return
        except (PermissionError, OSError) as err:
            last_err = err
            if idx + 1 < attempts:
                time.sleep(delay * (2 ** idx))
    if last_err is not None:
        raise last_err


def _atomic_copy_file(src: Path, dst: Path) -> None:
    """원본 파일을 임시 파일로 복사한 뒤 원자적으로 대상 경로에 교체한다."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.stem}_{uuid.uuid4().hex[:8]}.tmp{dst.suffix}")
    try:
        shutil.copy2(src, tmp)
        _atomic_replace_with_retry(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_move_file(src: Path, dst: Path) -> None:
    """동일 볼륨에서는 원자적 os.replace, 교차 볼륨에서는 임시 복사+원자 교체+원본 삭제로 이동한다."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_replace_with_retry(src, dst)
    except OSError:
        _atomic_copy_file(src, dst)
        src.unlink(missing_ok=True)


def _extract_pose_code(filename: str) -> int | None:
    m = _POSE_CODE_PATTERN.search(filename or "")
    return int(m.group(1)) if m else None


class AssetManager:
    """Kiro 플랫 에셋 탐색, 매트릭스 생성, 휴지통/Undo, _origin 복원, 리터치 저장 관리자."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir: Path = (
            Path(base_dir).resolve()
            if base_dir is not None
            else Path(__file__).resolve().parent.parent
        )
        self.projects_dir: Path = (self.base_dir / PROJECTS_DIRNAME).resolve()
        self.rpm = RosterPathManager(self.base_dir)

    # ── 경로 보안 검증 ─────────────────────────────────────────────

    def resolve_safe_path(self, path_or_rel: Path | str) -> Path:
        """
        입력 경로(projects/ 상대경로 또는 절대경로)를 해석하고 projects_dir 내부인지 강제 검증한다.
        경로 탈출(Path Traversal) 시도 시 PermissionError 발생.
        """
        raw_str = str(path_or_rel or "").strip()
        if not raw_str:
            raise ValueError("경로가 비어 있습니다.")

        raw_path = Path(raw_str)
        if raw_path.is_absolute():
            candidate = raw_path.resolve()
        else:
            norm_rel = raw_str.replace("\\", "/").lstrip("/")
            if norm_rel.startswith(f"{PROJECTS_DIRNAME}/"):
                candidate = (self.base_dir / norm_rel).resolve()
            else:
                candidate = (self.projects_dir / norm_rel).resolve()

        if not candidate.is_relative_to(self.projects_dir) or candidate == self.projects_dir:
            raise PermissionError(f"허용되지 않은 경로 접근입니다: {path_or_rel!r}")
        return candidate

    def _to_projects_rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.projects_dir).as_posix()

    @staticmethod
    def _ensure_not_origin_path(target: Path) -> None:
        if any(part == "_origin" or part.endswith("_origin") for part in target.parts):
            raise PermissionError("무검열 원본(_origin) 경로는 직접 수정/삭제할 수 없습니다.")

    @staticmethod
    def _ensure_not_trash_path(target: Path) -> None:
        if _TRASH_DIRNAME in target.parts:
            raise PermissionError("휴지통(_trash) 내부 경로는 직접 삭제/덮어쓰기할 수 없습니다.")

    def _validate_safe_token(self, token: str, label: str = "식별자") -> str:
        clean = (token or "").strip()
        if not clean or not _SAFE_TOKEN_PATTERN.match(clean):
            raise ValueError(f"유효하지 않은 {label}입니다: {token!r}")
        return clean

    # ── _origin 매핑 유틸 ──────────────────────────────────────────

    @staticmethod
    def get_origin_path(asset_path: Path) -> Path:
        """Kiro 플랫 규칙: assets/{char}/{filename} -> assets/{char}_origin/{filename}."""
        src = Path(asset_path)
        return src.parent.parent / f"{src.parent.name}_origin" / src.name

    def find_origin_file(self, asset_path: Path) -> Path | None:
        """본체 에셋에 대응하는 _origin 무검열 원본 파일을 탐색한다 (동일 파일명 우선, 코드 폴백)."""
        target = self.resolve_safe_path(asset_path)
        exact = self.get_origin_path(target).resolve()
        if exact.is_relative_to(self.projects_dir) and exact.is_file():
            return exact

        origin_dir = exact.parent
        if not origin_dir.is_dir():
            return None

        pose_code = _extract_pose_code(target.name)
        prefix = target.parent.name
        if pose_code is not None:
            for cand in sorted(origin_dir.iterdir()):
                if not cand.is_file() or cand.suffix.lower() not in _IMAGE_EXTS:
                    continue
                if cand.name.startswith(f"{prefix}_") and _extract_pose_code(cand.name) == pose_code:
                    resolved = cand.resolve()
                    if resolved.is_relative_to(self.projects_dir):
                        return resolved
        return None

    # ── 에셋 탐색 및 매트릭스 조회 ────────────────────────────────

    def list_rosters(self) -> list[str]:
        """projects/ 하위 유효 로스터 목록을 반환한다."""
        return self.rpm.list_available_rosters()

    def list_characters(self, roster: str) -> list[dict[str, Any]]:
        """지정 로스터의 캐릭터 메타 목록(name, prefix, gender)을 반환한다."""
        safe_roster = self._validate_safe_token(roster, "로스터명")
        rpaths = self.rpm.resolve_roster(safe_roster)
        if not rpaths.characters_dir.is_dir():
            return []

        items: list[dict[str, Any]] = []
        for cfile in sorted(rpaths.characters_dir.glob("*.json")):
            try:
                cfg = load_character(rpaths.characters_dir, cfile.stem)
                items.append({
                    "name": cfg.name,
                    "prefix": cfg.prefix,
                    "gender": cfg.gender or "female",
                })
            except Exception as exc:
                logger.warning("[AssetManager] 캐릭터 로드 건너뜀 (%s): %s", cfile.name, exc)
        return items

    def list_character_assets(self, roster: str, prefix: str) -> list[dict[str, Any]]:
        """
        projects/{roster}/assets/{prefix}/ 의 플랫 에셋 목록을 코드 오름차순으로 반환한다.
        각 항목은 code, code_str, label, filename, rel_path, has_origin, origin_rel_path, size_bytes, mtime 포함.
        """
        safe_roster = self._validate_safe_token(roster, "로스터명")
        safe_prefix = self._validate_safe_token(prefix, "캐릭터 prefix")
        rpaths = self.rpm.resolve_roster(safe_roster)
        char_dir = (rpaths.assets_dir / safe_prefix).resolve()
        if not char_dir.is_relative_to(self.projects_dir) or not char_dir.is_dir():
            return []

        name_re = re.compile(
            rf"^{re.escape(safe_prefix)}_(\d{{2,4}})(?:_(.*?))?(?:_censored)?\.(webp|png|jpg|jpeg)$",
            re.IGNORECASE,
        )

        items: list[dict[str, Any]] = []
        for fpath in sorted(char_dir.iterdir()):
            if not fpath.is_file() or fpath.suffix.lower() not in _IMAGE_EXTS:
                continue
            if fpath.name.startswith(".tmp_") or fpath.name.startswith("__tmp_"):
                continue
            if fpath.stem.lower().endswith("_censored"):
                continue

            m = name_re.match(fpath.name)
            code_int = int(m.group(1)) if m else _extract_pose_code(fpath.name)
            code_str = m.group(1) if m else (f"{code_int:03d}" if code_int is not None else "")
            label = (m.group(2) or "").strip() if m else ""

            origin_file = self.find_origin_file(fpath)
            stat = fpath.stat()
            items.append({
                "code": code_int,
                "code_str": code_str,
                "label": label,
                "filename": fpath.name,
                "rel_path": self._to_projects_rel(fpath),
                "has_origin": origin_file is not None,
                "origin_rel_path": self._to_projects_rel(origin_file) if origin_file else None,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            })

        items.sort(key=lambda it: (it["code"] is None, it["code"] if it["code"] is not None else 999999, it["filename"]))
        return items

    def build_gallery_matrix(
        self,
        roster: str,
        *,
        char_filter: str | None = None,
    ) -> dict[str, Any]:
        """로스터 -> 캐릭터 -> 포즈 코드별 에셋 및 _origin 보유 매트릭스를 조립한다."""
        safe_roster = self._validate_safe_token(roster, "로스터명")
        rpaths = self.rpm.resolve_roster(safe_roster)
        db = load_pose_db(self.base_dir, rpaths.events_file)
        width = code_width(db.all_codes)

        poses_meta = [
            {
                "code": c,
                "code_str": format_code(c, width),
                "label": db.entries[c].label,
                "section": db.entries[c].section,
                "required_outfit": db.entries[c].required_outfit,
            }
            for c in db.all_codes
        ]

        chars = self.list_characters(safe_roster)
        if char_filter:
            clean_f = char_filter.strip().lower()
            chars = [
                c for c in chars
                if c["name"].lower() == clean_f or c["prefix"].lower() == clean_f
            ]

        char_rows: list[dict[str, Any]] = []
        for ch in chars:
            assets = self.list_character_assets(safe_roster, ch["prefix"])
            assets_by_code: dict[str, dict[str, Any]] = {}
            for a in assets:
                if a["code"] is not None:
                    key = format_code(a["code"], width)
                    assets_by_code[key] = a
            active_mode = expand_v2_mode("all", db, expand_all_by_gender=ch["gender"])
            expected_codes = resolve_targets(db, active_mode)
            char_rows.append({
                "name": ch["name"],
                "prefix": ch["prefix"],
                "gender": ch["gender"],
                "expected_count": len(expected_codes),
                "total_assets": len(assets),
                "origin_count": sum(1 for a in assets if a["has_origin"]),
                "assets_by_code": assets_by_code,
                "assets": assets,
            })

        return {
            "roster": rpaths.roster_name,
            "code_width": width,
            "poses": poses_meta,
            "characters": char_rows,
        }

    # ── 안전 휴지통 삭제 & Undo 복원 ───────────────────────────────

    def _infer_roster_from_path(self, resolved_path: Path) -> str:
        rel_parts = resolved_path.relative_to(self.projects_dir).parts
        if not rel_parts:
            raise PermissionError("로스터 경로를 판별할 수 없습니다.")
        return rel_parts[0]

    def delete_asset_bundle(self, file_path: Path | str) -> dict[str, Any]:
        """
        본체 이미지와 대응하는 _origin 백업(존재 시)을 영구 삭제 대신
        projects/{roster}/assets/_trash/{trash_id}/ 번들로 안전 이동하고 manifest.json 을 기록한다.
        """
        target = self.resolve_safe_path(file_path)
        self._ensure_not_origin_path(target)
        self._ensure_not_trash_path(target)
        if not target.is_file():
            raise FileNotFoundError(f"삭제할 에셋 파일을 찾을 수 없습니다: {file_path}")

        roster_name = self._infer_roster_from_path(target)
        trash_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
        trash_bundle_dir = (self.projects_dir / roster_name / "assets" / _TRASH_DIRNAME / trash_id).resolve()
        if not trash_bundle_dir.is_relative_to(self.projects_dir):
            raise PermissionError("허용되지 않은 휴지통 경로입니다.")
        trash_bundle_dir.mkdir(parents=True, exist_ok=True)

        origin_file = self.find_origin_file(target)
        moved_items: list[dict[str, str]] = []

        # 1) 본체 이동
        main_dst = trash_bundle_dir / "main" / target.name
        _atomic_move_file(target, main_dst)
        moved_items.append({
            "role": "main",
            "original_rel": self._to_projects_rel(target),
            "trash_rel": self._to_projects_rel(main_dst),
        })

        # 2) _origin 백업 이동 (존재 시)
        if origin_file is not None and origin_file.is_file():
            origin_dst = trash_bundle_dir / "origin" / origin_file.name
            _atomic_move_file(origin_file, origin_dst)
            moved_items.append({
                "role": "origin",
                "original_rel": self._to_projects_rel(origin_file),
                "trash_rel": self._to_projects_rel(origin_dst),
            })

        manifest = {
            "trash_id": trash_id,
            "roster": roster_name,
            "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "filename": target.name,
            "prefix": target.parent.name,
            "code": _extract_pose_code(target.name),
            "items": moved_items,
        }
        manifest_path = trash_bundle_dir / "manifest.json"
        tmp_manifest = trash_bundle_dir / f".manifest_{uuid.uuid4().hex[:6]}.tmp"
        try:
            tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            _atomic_replace_with_retry(tmp_manifest, manifest_path)
        finally:
            if tmp_manifest.exists():
                tmp_manifest.unlink(missing_ok=True)

        return {
            "success": True,
            "trash_id": trash_id,
            "roster": roster_name,
            "filename": target.name,
            "prefix": target.parent.name,
            "code": manifest["code"],
            "moved_count": len(moved_items),
            "items": moved_items,
        }

    def _locate_trash_manifest(self, trash_id: str, roster: str | None = None) -> Path:
        safe_id = self._validate_safe_token(trash_id, "trash_id")
        if roster:
            safe_roster = self._validate_safe_token(roster, "로스터명")
            cand = (self.projects_dir / safe_roster / "assets" / _TRASH_DIRNAME / safe_id / "manifest.json").resolve()
            if cand.is_relative_to(self.projects_dir) and cand.is_file():
                return cand
            raise FileNotFoundError(f"휴지통 번들을 찾을 수 없습니다: {safe_roster}/{safe_id}")

        if self.projects_dir.is_dir():
            for rdir in sorted(self.projects_dir.iterdir()):
                if not rdir.is_dir():
                    continue
                cand = (rdir / "assets" / _TRASH_DIRNAME / safe_id / "manifest.json").resolve()
                if cand.is_relative_to(self.projects_dir) and cand.is_file():
                    return cand
        raise FileNotFoundError(f"휴지통 번들을 찾을 수 없습니다: {safe_id}")

    def restore_asset_bundle(
        self,
        trash_id: str,
        *,
        roster: str | None = None,
    ) -> dict[str, Any]:
        """휴지통 번들(trash_id)의 본체와 _origin 파일을 원래 위치로 100% 무손실 복원(Undo)한다."""
        manifest_path = self._locate_trash_manifest(trash_id, roster=roster)
        trash_bundle_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

        restored: list[dict[str, str]] = []
        for item in manifest.get("items", []):
            trash_file = self.resolve_safe_path(item["trash_rel"])
            orig_target = self.resolve_safe_path(item["original_rel"])
            if not trash_file.is_file():
                raise FileNotFoundError(f"휴지통 내부 파일이 누락되었습니다: {item['trash_rel']}")
            _atomic_move_file(trash_file, orig_target)
            restored.append({
                "role": str(item.get("role", "main")),
                "restored_rel": self._to_projects_rel(orig_target),
            })

        shutil.rmtree(trash_bundle_dir, ignore_errors=True)
        return {
            "success": True,
            "trash_id": manifest.get("trash_id", trash_id),
            "roster": manifest.get("roster"),
            "filename": manifest.get("filename"),
            "restored": restored,
        }

    # ── 무검열 원본 1클릭 복원 ─────────────────────────────────────

    def restore_from_origin(self, file_path: Path | str) -> dict[str, Any]:
        """
        {char}_origin/ 의 무검열 원본을 assets/{char}/ 현재 파일로 원자적 복원한다.
        (_origin 백업 파일 자체는 절대 삭제하거나 훼손하지 않는다.)
        """
        target = self.resolve_safe_path(file_path)
        self._ensure_not_origin_path(target)
        self._ensure_not_trash_path(target)

        origin_file = self.find_origin_file(target)
        if origin_file is None or not origin_file.is_file():
            raise FileNotFoundError(f"대응하는 무검열 원본(_origin) 파일이 없습니다: {file_path}")

        _atomic_copy_file(origin_file, target)
        return {
            "success": True,
            "rel_path": self._to_projects_rel(target),
            "origin_rel_path": self._to_projects_rel(origin_file),
        }

    # ── 리터치 Base64 이미지 원자적 저장 ───────────────────────────

    def save_retouched_image(
        self,
        file_path: Path | str,
        image_base64: str,
        *,
        quality: int = 95,
    ) -> dict[str, Any]:
        """
        브라우저 캔버스 Base64 PNG/WebP 데이터를 디코딩해 대상 에셋에 WebP(quality=95)로 원자 저장한다.
        최초 리터치 시 {char}_origin/ 에 백업이 없으면 원본을 선제 백업한다.
        """
        target = self.resolve_safe_path(file_path)
        self._ensure_not_origin_path(target)
        self._ensure_not_trash_path(target)
        if not target.is_file():
            raise FileNotFoundError(f"리터치 대상 에셋 파일을 찾을 수 없습니다: {file_path}")

        raw_b64 = (image_base64 or "").strip()
        if not raw_b64:
            raise ValueError("이미지 Base64 데이터가 비어 있습니다.")
        if "," in raw_b64 and raw_b64.lower().startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]

        try:
            raw_bytes = base64.b64decode(raw_b64, validate=False)
            with Image.open(io.BytesIO(raw_bytes)) as opened:
                img_rgb = opened.convert("RGB")
        except Exception as exc:
            raise ValueError(f"유효하지 않은 Base64 이미지 데이터입니다: {exc}") from exc

        # 최초 리터치 시 _origin 백업이 없다면 현재 원본을 선제 복사 보존
        origin_path = self.get_origin_path(target).resolve()
        if not origin_path.is_relative_to(self.projects_dir):
            raise PermissionError("허용되지 않은 _origin 백업 경로입니다.")
        origin_backed_up = False
        if not origin_path.is_file():
            _atomic_copy_file(target, origin_path)
            origin_backed_up = True

        # 대상 경로에 WebP(quality=95)로 임시 저장 후 원자 교체
        ext = target.suffix.lower()
        tmp_dst = target.with_name(f".{target.stem}_{uuid.uuid4().hex[:8]}.tmp{target.suffix or '.webp'}")
        try:
            if ext == ".png":
                img_rgb.save(tmp_dst, "PNG", optimize=True)
            elif ext in (".jpg", ".jpeg"):
                img_rgb.save(tmp_dst, "JPEG", quality=int(quality), subsampling=0)
            else:
                img_rgb.save(tmp_dst, "WEBP", quality=int(quality))
            _atomic_replace_with_retry(tmp_dst, target)
        finally:
            if tmp_dst.exists():
                tmp_dst.unlink(missing_ok=True)

        return {
            "success": True,
            "rel_path": self._to_projects_rel(target),
            "origin_rel_path": self._to_projects_rel(origin_path),
            "origin_backed_up": origin_backed_up,
            "width": img_rgb.width,
            "height": img_rgb.height,
        }


__all__ = [
    "AssetManager",
    "_atomic_copy_file",
    "_atomic_move_file",
    "_atomic_replace_with_retry",
]

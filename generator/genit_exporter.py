"""
generator.genit_exporter
Kiro 플랫 에셋 폴더(projects/{roster}/assets/{char}/) 및 code/hybrid 파일명을
Genit 플랫폼 규격({output_dir}/{export_code}/{상황코드}.webp + _genit_prompt_sheet.md)으로
안전하게 패키징·내보내기하는 익스포터.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from generator.censor import _atomic_replace_with_retry, extract_pose_code
from generator.models import PoseDatabase, RosterPaths
from generator.pose_db import load_pose_db

logger = logging.getLogger(__name__)


def denormalize_genit_code(code: str | int) -> str:
    """
    내부 포즈 코드를 Genit 플랫폼 출력 코드 문자열로 정규화한다.
    - 영문 접두사 코드('A00', 'N01') -> 'A0', 'N1'
    - 정수/숫자 코드(0, '007', '080') -> '000', '007', '080' (3자리 제로 패딩 유지)
    """
    code_s = str(code).strip().upper()
    if code_s.isdigit():
        return f"{int(code_s):03d}"
    if len(code_s) >= 2 and code_s[0].isalpha() and code_s[1:].isdigit():
        return f"{code_s[0]}{int(code_s[1:]):d}"
    return code_s


class GenitExporter:
    """Kiro 로스터 에셋을 Genit 규격으로 내보내는 패키징 서비스."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir if base_dir is not None else Path.cwd()

    @staticmethod
    def _load_roster_export_map(roster_dir: Optional[Path]) -> Dict[str, str]:
        """로스터 디렉토리 내 genit_map.json 설정을 UTF-8-SIG로 로드한다."""
        if roster_dir is None:
            return {}
        map_path = roster_dir / "genit_map.json"
        if not map_path.is_file():
            return {}
        try:
            data = json.loads(map_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return {str(k): str(v).strip().upper() for k, v in data.items() if str(v).strip()}
        except Exception as e:
            logger.warning(f"[WARN] genit_map.json 로드 실패 ({map_path}): {e}")
        return {}

    @classmethod
    def resolve_export_code(
        cls,
        char_name: str,
        char_prefix: str,
        *,
        custom_code: Optional[str] = None,
        roster_dir: Optional[Path] = None,
    ) -> str:
        """캐릭터 이름/prefix 및 로스터 genit_map.json 을 기반으로 Genit 출력 코드를 결정한다."""
        if custom_code and custom_code.strip():
            return custom_code.strip().upper()

        roster_map = cls._load_roster_export_map(roster_dir)
        if char_prefix in roster_map:
            return roster_map[char_prefix]
        if char_name in roster_map:
            return roster_map[char_name]
        for name_key, code_val in roster_map.items():
            if name_key and char_name and name_key in char_name:
                return code_val

        clean_prefix = re.sub(r"[^A-Za-z0-9]", "", char_prefix)
        if len(clean_prefix) >= 2:
            return clean_prefix[:2].upper()
        return (clean_prefix + "X")[:2].upper()

    def export_character(
        self,
        roster: RosterPaths,
        char_prefix: str,
        char_name: Optional[str] = None,
        *,
        output_dir: Optional[Path] = None,
        custom_export_code: Optional[str] = None,
        db: Optional[PoseDatabase] = None,
    ) -> Tuple[int, Path, str]:
        """
        단일 캐릭터의 Kiro 플랫 에셋 폴더(assets/{char_prefix}/)를 Genit 규격으로 내보낸다.
        - `_origin` 백업 폴더 및 숨김/임시 파일(`.tmp_` 등)은 자동 제외.
        - 완료 시 `_genit_prompt_sheet.md` 매핑 시트를 원자적으로 생성.
        반환값: (내보낸 파일 수, 대상 폴더 경로, 결정된 캐릭터 코드)
        """
        display_name = (char_name or char_prefix).strip()
        roster_dir = roster.assets_dir.parent
        export_code = self.resolve_export_code(
            display_name,
            char_prefix,
            custom_code=custom_export_code,
            roster_dir=roster_dir,
        )
        target_root = output_dir if output_dir is not None else (self.base_dir / "dist" / "genit" / roster.roster_name)
        target_dir = target_root / export_code
        target_dir.mkdir(parents=True, exist_ok=True)

        char_assets_dir = roster.assets_dir / char_prefix
        if not char_assets_dir.is_dir():
            return 0, target_dir, export_code

        pose_db = db if db is not None else load_pose_db(self.base_dir, roster.events_file)
        exported_count = 0
        sheet_rows: List[str] = []

        for webp_file in sorted(char_assets_dir.rglob("*.webp")):
            if any("_origin" in part for part in webp_file.parts) or webp_file.name.startswith("."):
                continue
            code_int = extract_pose_code(webp_file.name)
            if code_int is None:
                continue

            genit_code = denormalize_genit_code(code_int)
            dest_file = target_dir / f"{genit_code}.webp"
            temp_dest = target_dir / f".{dest_file.name}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                shutil.copy2(webp_file, temp_dest)
                _atomic_replace_with_retry(temp_dest, dest_file)
            finally:
                if temp_dest.exists():
                    temp_dest.unlink(missing_ok=True)
            exported_count += 1

            label = "미상"
            desc = ""
            if code_int in pose_db.entries:
                entry = pose_db.entries[code_int]
                label = entry.label
                desc = entry.description or entry.section
            sheet_rows.append(f"| `{genit_code}` | `{dest_file.name}` | **{label}** | {desc} |")

        sheet_path = target_dir / "_genit_prompt_sheet.md"
        sheet_content = [
            f"# 젠잇(Genit) 프롬프트 매핑 시트 - {display_name} (`{export_code}`)",
            "",
            f"- **캐릭터 식별자**: `{char_prefix}`",
            f"- **젠잇 코드**: `{export_code}`",
            f"- **내보낸 에셋 수**: {exported_count}개",
            f"- **원본 로스터**: `{roster.roster_name}`",
            "",
            "| 젠잇 포즈코드 | 출력 파일명 | 한글 라벨 | 상황 및 연출 설명 |",
            "| :---: | :--- | :--- | :--- |",
        ] + sheet_rows

        temp_sheet = target_dir / f".{sheet_path.name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(temp_sheet, "w", encoding="utf-8") as sf:
                sf.write("\n".join(sheet_content) + "\n")
                sf.flush()
                os.fsync(sf.fileno())
            _atomic_replace_with_retry(temp_sheet, sheet_path)
        finally:
            if temp_sheet.exists():
                temp_sheet.unlink(missing_ok=True)

        return exported_count, target_dir, export_code

    def export_roster(
        self,
        roster: RosterPaths,
        *,
        output_dir: Optional[Path] = None,
        char_prefixes: Optional[Sequence[str]] = None,
        db: Optional[PoseDatabase] = None,
    ) -> List[Tuple[str, int, Path, str]]:
        """로스터 내 모든(또는 지정된) 캐릭터 에셋 폴더를 일괄 내보낸다 (`*_origin` 폴더 자동 제외)."""
        if not roster.assets_dir.is_dir():
            return []

        allowed = {c.strip() for c in char_prefixes if c.strip()} if char_prefixes else None
        pose_db = db if db is not None else load_pose_db(self.base_dir, roster.events_file)
        results: List[Tuple[str, int, Path, str]] = []

        for child in sorted(roster.assets_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.endswith("_origin") or "backup" in child.name.lower() or child.name.startswith("."):
                continue
            if allowed is not None and child.name not in allowed:
                continue
            count, out_dir, code = self.export_character(
                roster,
                child.name,
                child.name,
                output_dir=output_dir,
                db=pose_db,
            )
            results.append((child.name, count, out_dir, code))
        return results


default_genit_exporter = GenitExporter()

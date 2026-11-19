"""
scripts/audit_asset_alignment.py
Kiro 플랫 에셋(projects/{roster}/assets/{char}/ + {char}_origin/)과
V2 포즈 DB(pose_database.json + 로스터 pose_overrides.json / events.json) 간의 정합성을 전수 검사하는 도구.

주요 기능:
1. 로스터별·캐릭터별 에셋 완비율(present / expected, %), _origin 백업 현황 산출
2. 구 라벨 / 라벨 불일치(mismatches) 및 누락 코드(missing_poses) 리포트
3. DB 미정의 고아 파일(orphan_files) 및 동일 코드 중복(duplicates) 탐지
4. --apply-rename (별칭 --fix): 2-Pass 임시 파일 기반 안전 리네임 (기본은 읽기 전용 dry-run)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# 프로젝트 루트 경로 추가
KIRO_ROOT = Path(__file__).resolve().parent.parent
if str(KIRO_ROOT) not in sys.path:
    sys.path.insert(0, str(KIRO_ROOT))

if getattr(sys.stdout, "encoding", None) and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from generator.config import CHARACTERS_DIRNAME, PROJECTS_DIRNAME
from generator.pose_db import (
    V2_FEMALE_SECTIONS,
    V2_OTOKONOKO_SECTIONS,
    expand_v2_mode,
    load_pose_db,
    resolve_targets,
)
from generator.reporter import code_width, format_code
from generator.roster import RosterPathManager, load_character

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("KiroAssetAlignmentAuditor")

_IMAGE_EXTS = {".webp", ".png", ".jpg", ".jpeg"}


def normalize_label_for_comparison(label: str) -> str:
    """공백·언더스코어·괄호를 제거하고 소문자로 정규화해 라벨 비교에 사용한다."""
    return re.sub(r"[\s_\-\(\)\[\]]", "", (label or "")).lower()


def load_kiro_pose_catalog(
    roster: str,
    *,
    gender: str = "female",
    base_dir: Path | None = None,
) -> tuple[dict[int, dict[str, Any]], int]:
    """
    Kiro V2 pose_database.json 과 로스터의 pose_overrides.json / events.json 을 병합해
    해당 성별에 기대되는 활성 포즈 카탈로그 `{code_int: {...}}` 와 표준 코드 폭(`width`)을 반환한다.
    """
    root = (base_dir if base_dir is not None else KIRO_ROOT).resolve()
    mgr = RosterPathManager(root)
    roster_paths = mgr.resolve_roster(roster)
    db = load_pose_db(root, roster_paths.events_file)
    width = code_width(db.all_codes)

    active_mode = expand_v2_mode("all", db, expand_all_by_gender=gender or "female")
    active_codes = resolve_targets(db, active_mode)

    catalog: dict[int, dict[str, Any]] = {}
    for code in active_codes:
        entry = db.entries[code]
        catalog[code] = {
            "code": code,
            "code_str": format_code(code, width),
            "label": entry.label,
            "section": entry.section,
            "required_outfit": entry.required_outfit,
        }
    return catalog, width


def _execute_two_pass_safe_rename(
    fpath: Path,
    char_dir: Path,
    origin_dir: Path,
    new_name: str,
    report: dict[str, Any],
) -> None:
    """Windows 대소문자 무시 파일시스템에서도 충돌 없는 2-Pass 임시 파일 기반 안전 리네임을 수행한다."""
    new_path = fpath.parent / new_name
    if fpath == new_path:
        return
    if new_path.exists() and new_path.resolve() != fpath.resolve():
        logger.warning(f"대상 파일 이미 존재하여 리네임 건너뜀: {new_path.name}")
        return

    tmp_path = fpath.parent / f"__tmp_align_{fpath.name}"
    os.rename(fpath, tmp_path)
    os.rename(tmp_path, new_path)
    report["fixed_files"].append(f"{fpath.name} -> {new_name}")

    if origin_dir.is_dir():
        try:
            orig_target = origin_dir / fpath.relative_to(char_dir)
            if orig_target.is_file():
                orig_new = orig_target.parent / new_name
                if not orig_new.exists() or orig_new.resolve() == orig_target.resolve():
                    orig_tmp = orig_target.parent / f"__tmp_align_{orig_target.name}"
                    os.rename(orig_target, orig_tmp)
                    os.rename(orig_tmp, orig_new)
                    report["fixed_origin_files"].append(f"{orig_target.name} -> {new_name}")
        except Exception as e:
            logger.warning(f"_origin 리네임 예외 ({fpath.name}): {e}")


def audit_character_alignment(
    roster: str,
    prefix: str,
    *,
    gender: str = "female",
    apply_rename: bool = False,
    base_dir: Path | None = None,
    catalog: dict[int, dict[str, Any]] | None = None,
    width: int | None = None,
) -> dict[str, Any]:
    """단일 캐릭터 에셋 폴더(assets/{prefix}/ 및 {prefix}_origin/)와 포즈 DB 정합성을 검사한다."""
    root = (base_dir if base_dir is not None else KIRO_ROOT).resolve()
    mgr = RosterPathManager(root)
    roster_paths = mgr.resolve_roster(roster)
    char_dir = roster_paths.assets_dir / prefix
    origin_dir = roster_paths.assets_dir / f"{prefix}_origin"

    if catalog is None or width is None:
        catalog, width = load_kiro_pose_catalog(roster, gender=gender, base_dir=root)

    origin_files = (
        [f for f in origin_dir.iterdir() if f.is_file() and f.suffix.lower() in _IMAGE_EXTS]
        if origin_dir.is_dir()
        else []
    )

    report: dict[str, Any] = {
        "roster": roster_paths.roster_name,
        "prefix": prefix,
        "gender": gender,
        "total_files": 0,
        "expected_count": len(catalog),
        "present_count": 0,
        "completion_rate": 0.0,
        "origin_count": len(origin_files),
        "matched_count": 0,
        "code_only_count": 0,
        "mismatches": [],
        "missing_poses": [],
        "orphan_files": [],
        "duplicates": [],
        "fixed_files": [],
        "fixed_origin_files": [],
    }

    if not char_dir.is_dir():
        for code in sorted(catalog):
            info = catalog[code]
            report["missing_poses"].append(
                {"code": info["code_str"], "label": info["label"], "section": info["section"]}
            )
        return report

    # Kiro 파일명 규칙: {prefix}_{code}.ext 또는 {prefix}_{code}_{label}[_censored].ext
    pattern = re.compile(
        rf"^{re.escape(prefix)}_(\d{{2,4}})(?:_(.*?))?(?:_censored)?\.(webp|png|jpg|jpeg)$",
        re.IGNORECASE,
    )

    all_files = sorted(
        f
        for f in char_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() in _IMAGE_EXTS
        and not f.name.startswith(".tmp_")
        and not f.name.startswith("__tmp_align_")
    )
    report["total_files"] = len(all_files)

    files_by_code: dict[int, list[tuple[Path, str, str]]] = {}
    for fpath in all_files:
        m = pattern.match(fpath.name)
        if not m:
            report["orphan_files"].append(
                {
                    "code": "UNKNOWN",
                    "filename": fpath.name,
                    "path": str(fpath),
                    "reason": f"파일명이 '{prefix}_{{code}}[_label].ext' 규격과 불일치",
                }
            )
            continue
        code_int = int(m.group(1))
        raw_code_str = m.group(1)
        raw_label = (m.group(2) or "").strip()
        files_by_code.setdefault(code_int, []).append((fpath, raw_code_str, raw_label))

    for code_int, flist in sorted(files_by_code.items()):
        if code_int not in catalog:
            for fpath, raw_code_str, _ in flist:
                report["orphan_files"].append(
                    {
                        "code": raw_code_str,
                        "filename": fpath.name,
                        "path": str(fpath),
                        "reason": "현재 성별/로스터 활성 포즈 DB에 정의되지 않은 코드",
                    }
                )
            continue

        expected_info = catalog[code_int]
        expected_code_str = expected_info["code_str"]
        expected_label = expected_info["label"]
        norm_exp = normalize_label_for_comparison(expected_label)

        if len(flist) > 1:
            report["duplicates"].append(
                {"code": expected_code_str, "files": [f[0].name for f in flist]}
            )

        for fpath, _raw_code_str, raw_label in flist:
            if not raw_label:
                # code 명명 모드 ({prefix}_{code}.webp)
                report["code_only_count"] += 1
                report["matched_count"] += 1
                continue

            norm_raw = normalize_label_for_comparison(raw_label)
            if norm_raw == norm_exp:
                report["matched_count"] += 1
            else:
                report["mismatches"].append(
                    {
                        "code": expected_code_str,
                        "filename": fpath.name,
                        "path": str(fpath),
                        "actual_label": raw_label,
                        "expected_label": expected_label,
                        "section": expected_info["section"],
                    }
                )
                if apply_rename:
                    is_censored = fpath.stem.lower().endswith("_censored")
                    censor_suffix = "_censored" if is_censored else ""
                    new_name = f"{prefix}_{expected_code_str}_{expected_label}{censor_suffix}{fpath.suffix}"
                    _execute_two_pass_safe_rename(fpath, char_dir, origin_dir, new_name, report)

    present_codes = [c for c in catalog if c in files_by_code]
    report["present_count"] = len(present_codes)
    report["completion_rate"] = (
        round((len(present_codes) / len(catalog)) * 100.0, 1) if catalog else 0.0
    )

    for code in sorted(catalog):
        if code not in files_by_code:
            info = catalog[code]
            report["missing_poses"].append(
                {"code": info["code_str"], "label": info["label"], "section": info["section"]}
            )

    return report


def audit_roster_alignment(
    roster: str,
    *,
    apply_rename: bool = False,
    base_dir: Path | None = None,
    char_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    """로스터 내 모든(또는 지정된) 캐릭터의 에셋 정합성 리포트 리스트를 반환한다."""
    root = (base_dir if base_dir is not None else KIRO_ROOT).resolve()
    mgr = RosterPathManager(root)
    roster_paths = mgr.resolve_roster(roster)
    if not roster_paths.characters_dir.is_dir():
        raise FileNotFoundError(f"로스터 캐릭터 폴더를 찾을 수 없습니다: {roster_paths.characters_dir}")

    catalog_cache: dict[str, tuple[dict[int, dict[str, Any]], int]] = {}
    reports: list[dict[str, Any]] = []

    for cfile in sorted(roster_paths.characters_dir.glob("*.json")):
        if char_filter and cfile.stem not in char_filter:
            continue
        try:
            cfg = load_character(roster_paths.characters_dir, cfile.stem)
            prefix = cfg.prefix
            gender = cfg.gender or "female"
        except Exception as e:
            logger.warning(f"캐릭터 JSON 로드 실패({cfile.name}), 파일명 기준 진행: {e}")
            prefix = cfile.stem
            gender = "female"

        if gender not in catalog_cache:
            catalog_cache[gender] = load_kiro_pose_catalog(roster, gender=gender, base_dir=root)
        cat, width = catalog_cache[gender]
        reports.append(
            audit_character_alignment(
                roster,
                prefix,
                gender=gender,
                apply_rename=apply_rename,
                base_dir=root,
                catalog=cat,
                width=width,
            )
        )

    return reports


def print_roster_alignment_report(roster: str, char_reports: list[dict[str, Any]]) -> None:
    """로스터 정합성 리포트를 터미널에 출력한다."""
    print("=" * 80)
    print(f"📊 [KIRO · {roster.upper()}] 에셋 - V2 포즈 DB 정합성 감사 결과")
    print("=" * 80)

    total_mismatches = sum(len(r["mismatches"]) for r in char_reports)
    total_orphans = sum(len(r["orphan_files"]) for r in char_reports)
    total_duplicates = sum(len(r["duplicates"]) for r in char_reports)
    total_missing = sum(len(r["missing_poses"]) for r in char_reports)
    total_fixed = sum(len(r["fixed_files"]) for r in char_reports)

    for r in char_reports:
        print(
            f"\n👤 [{r['prefix']}] ({r['gender']}) "
            f"완비율 {r['present_count']}/{r['expected_count']} ({r['completion_rate']}%) | "
            f"_origin {r['origin_count']}장"
        )
        if r["mismatches"]:
            print(f"  🚨 [라벨 불일치] {len(r['mismatches'])}건:")
            for m in r["mismatches"][:10]:
                print(
                    f"    - {m['code']} [{m['section']}]: '{m['actual_label']}' -> '{m['expected_label']}' ({m['filename']})"
                )
        else:
            print("  ✅ [라벨 일치] 불일치 0건")

        if r["duplicates"]:
            print(f"  ⚠️ [중복 코드] {len(r['duplicates'])}건")
        if r["orphan_files"]:
            print(f"  ⚠️ [고아/비규격 파일] {len(r['orphan_files'])}건")
        if r["missing_poses"]:
            sample = ", ".join(f"{m['code']}_{m['label']}" for m in r["missing_poses"][:10])
            suffix = f" 외 {len(r['missing_poses']) - 10}종" if len(r["missing_poses"]) > 10 else ""
            print(f"  ℹ️ [누락 코드] {len(r['missing_poses'])}종: {sample}{suffix}")
        else:
            print("  ✅ [누락 코드] 전체 포즈 100% 완비")

        if r["fixed_files"]:
            print(f"  🔧 [안전 리네임 완료] 에셋 {len(r['fixed_files'])}건 / _origin {len(r['fixed_origin_files'])}건")

    print("\n" + "=" * 80)
    print(f"🏁 종합 요약 ({roster.upper()} {len(char_reports)}명):")
    print(f"  - 라벨 불일치: {total_mismatches}건 | 중복: {total_duplicates}건 | 고아: {total_orphans}건")
    print(f"  - 누락 포즈 총합: {total_missing}건")
    if total_fixed:
        print(f"  - 2-Pass 안전 리네임 적용: {total_fixed}건")
    print("=" * 80)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kiro 에셋 폴더와 V2 포즈 DB 간 완비율·라벨 불일치·누락 코드 정합성 감사 도구"
    )
    parser.add_argument(
        "-r",
        "--roster",
        default="dark_generals",
        help="검사할 로스터명, 별칭 또는 'all' 전체 로스터 (기본: dark_generals)",
    )
    parser.add_argument("--char", default=None, help="특정 캐릭터 필터 (콤마 구분 가능)")
    parser.add_argument(
        "--apply-rename",
        "--fix",
        dest="apply_rename",
        action="store_true",
        help="라벨 불일치 파일을 2-Pass 안전 리네임으로 공식 라벨 동기화 (미지정 시 읽기 전용 dry-run)",
    )
    parser.add_argument("--base-dir", default=str(KIRO_ROOT), help="Kiro 프로젝트 루트 경로")
    parser.add_argument("--json", dest="as_json", action="store_true", help="결과를 JSON으로 출력")
    args = parser.parse_args(argv)

    root = Path(args.base_dir).resolve()
    char_filter = (
        {c.strip() for c in args.char.split(",") if c.strip()} if args.char else None
    )

    if str(args.roster).strip().lower() == "all":
        target_rosters = RosterPathManager(root).list_available_rosters()
        if not target_rosters:
            logger.error("유효한 로스터 폴더를 찾을 수 없습니다.")
            return 1
        all_reports: dict[str, list[dict[str, Any]]] = {}
        for r_name in target_rosters:
            all_reports[r_name] = audit_roster_alignment(
                r_name,
                apply_rename=args.apply_rename,
                base_dir=root,
                char_filter=char_filter,
            )
        if args.as_json:
            print(json.dumps(all_reports, ensure_ascii=False, indent=2))
        else:
            print("=" * 80)
            print(f"📊 [KIRO · ALL] 전체 {len(target_rosters)}개 로스터 에셋 정합성 종합 요약")
            print("=" * 80)
            grand_chars = 0
            grand_present = 0
            grand_expected = 0
            grand_mismatches = 0
            grand_orphans = 0
            grand_origins = 0
            for r_name, reps in all_reports.items():
                c_cnt = len(reps)
                p_cnt = sum(r["present_count"] for r in reps)
                e_cnt = sum(r["expected_count"] for r in reps)
                m_cnt = sum(len(r["mismatches"]) for r in reps)
                o_cnt = sum(len(r["orphan_files"]) for r in reps)
                orig_cnt = sum(r["origin_count"] for r in reps)
                rate = round((p_cnt / e_cnt) * 100.0, 1) if e_cnt else 0.0
                grand_chars += c_cnt
                grand_present += p_cnt
                grand_expected += e_cnt
                grand_mismatches += m_cnt
                grand_orphans += o_cnt
                grand_origins += orig_cnt
                print(
                    f"  - {r_name:<15} | 캐릭터 {c_cnt:>2}명 | 완비 {p_cnt:>4}/{e_cnt:<4} ({rate:>5.1f}%) | "
                    f"_origin {orig_cnt:>4} | 구라벨 {m_cnt:>3} | 고아 {o_cnt:>3}"
                )
            grand_rate = round((grand_present / grand_expected) * 100.0, 1) if grand_expected else 0.0
            print("-" * 80)
            print(
                f"🏁 전체 합계: {len(target_rosters)}개 로스터 · {grand_chars}명 | "
                f"완비 {grand_present}/{grand_expected} ({grand_rate}%) | "
                f"_origin {grand_origins}장 | 구라벨 {grand_mismatches}건 | 고아 {grand_orphans}건"
            )
            print("=" * 80)
        return 0

    try:
        reports = audit_roster_alignment(
            args.roster,
            apply_rename=args.apply_rename,
            base_dir=root,
            char_filter=char_filter,
        )
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1

    if args.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        print_roster_alignment_report(args.roster, reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())

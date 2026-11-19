"""
auto_censor.py
키로(kiro) 고정밀 일러스트 자동 검열 경량 CLI 진입점.
내부 배치·백업·원본 복원(_origin) 로직은 모두 `generator.censor.run_batch_censor` 단일 코어에 위임합니다.

지원 옵션:
- `-d / --dir`: 검열할 폴더 경로
- `-r / --roster`: 로스터 이름(예: dar, spy, sea)으로 `projects/{roster}/assets` 자동 지정
- `-c / --char`: 특정 캐릭터(들)만 필터링 (예: `--char cla evl` 또는 `--char cla,evl`)
- `--restore-origin`: 기존 `assets/{char}_origin/` 원본을 선제 복원한 뒤 클린 재검열 수행
- `-f / --file`: 단일 이미지 파일 검열
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from generator.censor import (
    censor_file,
    extract_pose_code,
    get_origin_backup_path,
    is_censor_target,
    run_batch_censor,
)
from generator.config import configure_stdio
from generator.roster import RosterPathManager

__all__ = [
    "censor_file",
    "extract_pose_code",
    "get_origin_backup_path",
    "is_censor_target",
    "run_batch_censor",
    "run_batch_censor_with_backup",
    "main",
]


def run_batch_censor_with_backup(
    folder_path: str | Path,
    style: str = "white_silhouette",
    overwrite: bool = True,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    enable_anchor_fallback: bool = False,
    *,
    restore_origin: bool = False,
    char_names: Sequence[str] | None = None,
) -> int:
    """하위 호환 래퍼: `generator.censor.run_batch_censor` 단일 코어로 위임합니다."""
    return run_batch_censor(
        folder_path=folder_path,
        style=style,
        overwrite=overwrite,
        targets=targets,
        min_confidence=min_confidence,
        enable_anchor_fallback=enable_anchor_fallback,
        backup_origin=True,
        restore_origin=restore_origin,
        char_names=char_names,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="키로(kiro) 2D 애니메이션 일러스트 고정밀 성기 자동 검열 도구"
    )
    parser.add_argument(
        "-d", "--dir",
        type=str,
        help="검열을 수행할 일러스트 폴더 경로",
    )
    parser.add_argument(
        "-r", "--roster",
        type=str,
        default=None,
        help="검열을 수행할 로스터 이름 (예: dar, spy, sea -> projects/{roster}/assets)",
    )
    parser.add_argument(
        "-c", "--char",
        nargs="+",
        default=None,
        help="특정 캐릭터 prefix 만 필터링 (예: --char cla evl 또는 --char cla,evl)",
    )
    parser.add_argument(
        "--restore-origin",
        action="store_true",
        help="검열 전 assets/{char}_origin/ 무검열 원본을 먼저 복원한 뒤 클린 재검열 수행",
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        help="검열을 수행할 단일 이미지 파일 경로",
    )
    parser.add_argument(
        "-m", "--mode", "--style",
        dest="style",
        type=str,
        default="white_silhouette",
        choices=[
            "white_silhouette",
            "black_silhouette",
            "wide_bar",
            "slim_bar",
            "shadow",
            "mosaic",
            "black_bar",
            "manga_bar",
            "manga_glow",
            "debug_bbox",
        ],
        help="검열 렌더링 스타일 (기본값: white_silhouette)",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=["penis", "pussy"],
        help="검열 대상 라벨 (기본값: penis pussy)",
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=0.15,
        help="검출 신뢰도 임계치 (기본값: 0.15)",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="원본 파일을 덮어쓰지 않고 _censored 파일로 별도 저장",
    )
    parser.add_argument(
        "--enable-anchor",
        action="store_true",
        help="AI 미탐지 시 체위별 세이프 앵커 폴백 활성화 (주의: 엉덩이/골반 과대 검열 가능)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    style = "wide_bar" if args.style == "black_bar" else args.style
    anchor_fallback = getattr(args, "enable_anchor", False)

    if args.file:
        file_path = Path(args.file).resolve()
        if not file_path.exists():
            print(f"[오류] 파일을 찾을 수 없습니다: {file_path}")
            return 1

        dst = (
            None
            if not args.no_overwrite
            else file_path.with_name(f"{file_path.stem}_censored{file_path.suffix}")
        )
        out = censor_file(
            src_path=file_path,
            dst_path=dst,
            style=style,
            targets=args.targets,
            min_confidence=args.conf,
            pose_code=extract_pose_code(file_path.name),
            enable_anchor_fallback=anchor_fallback,
        )
        if out:
            print(f"[완료] 검열본 저장 완료: {out}")
        else:
            print("[통과] 검열 대상 부위가 감지되지 않았습니다.")
        return 0

    target_dir: Path | None = None
    if args.dir:
        target_dir = Path(args.dir).resolve()
    elif args.roster:
        base_dir = Path(__file__).resolve().parent
        rpm = RosterPathManager(base_dir)
        roster_paths = rpm.resolve_roster(args.roster)
        target_dir = roster_paths.assets_dir

    if target_dir is not None:
        run_batch_censor(
            folder_path=target_dir,
            style=style,
            overwrite=not args.no_overwrite,
            targets=args.targets,
            min_confidence=args.conf,
            enable_anchor_fallback=anchor_fallback,
            backup_origin=True,
            restore_origin=bool(args.restore_origin),
            char_names=args.char,
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

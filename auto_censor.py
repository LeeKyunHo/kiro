"""
auto_censor.py
키로(kiro) 고정밀 일러스트 자동 검열 CLI 진입점 스크립트.
내부적으로 `generator.censor` 엔진을 호출하며, 레거시 인자(-d, -m)와 신규 스타일, _origin 안전 백업을 지원합니다.

지원 스타일:
- white_silhouette (기본값): 화이트 외곽선 실루엣 (MobileSAM/Contour)
- black_silhouette: 블랙 외곽선 실루엣
- wide_bar (black_bar 호환): 라운드 캡슐 와이드 바
- slim_bar: 사선 미니멀 블랙 띠
- shadow: 선화 디테일 차단 그림자 음영
- mosaic: 클래식 픽셀 격자 모자이크
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Sequence

from generator.censor import (
    censor_file,
    run_batch_censor,
)


def run_batch_censor_with_backup(
    folder_path: str | Path,
    style: str = "white_silhouette",
    overwrite: bool = True,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    enable_anchor_fallback: bool = True,
) -> int:
    """_origin 무검열 원본 격리 백업과 함께 일괄 검열을 수행합니다."""
    base_dir = Path(folder_path).resolve()
    if not base_dir.exists():
        print(f"[오류] 경로를 찾을 수 없습니다: {base_dir}")
        return 0

    extensions = ["*.webp", "*.png", "*.jpg", "*.jpeg"]
    image_files: list[Path] = []
    for ext in extensions:
        image_files.extend(base_dir.rglob(ext))

    target_files = [
        f for f in image_files
        if not f.stem.endswith("_censored") and "_origin" not in f.parts and not f.name.startswith(".tmp_")
    ]
    total = len(target_files)

    print(f"\n[스캔 시작] 총 {total}장의 일러스트 에셋을 검사합니다.")
    print(f"[설정] 스타일: {style} | 원본 덮어쓰기: {overwrite} | 안전 앵커: {enable_anchor_fallback}")

    processed_count = 0
    for idx, file_path in enumerate(target_files, 1):
        match = re.search(r"_(\d{3})\.", file_path.name)
        pose_code = int(match.group(1)) if match else None

        # V2 체계: 080~149, 150~219 또는 레거시 H-씬
        is_h = False
        if pose_code is not None:
            is_h = (80 <= pose_code <= 149) or (150 <= pose_code <= 219) or (40 <= pose_code <= 75) or (140 <= pose_code <= 170)
        if not is_h:
            continue

        try:
            # 1. 무검열 원본 assets/{char}_origin/ 격리 백업
            if overwrite:
                parent_dir = file_path.parent
                origin_dir = parent_dir.parent / f"{parent_dir.name}_origin"
                origin_dir.mkdir(parents=True, exist_ok=True)
                origin_path = origin_dir / file_path.name
                if not origin_path.exists():
                    shutil.copy2(file_path, origin_path)

            dst = file_path if overwrite else file_path.with_name(f"{file_path.stem}_censored{file_path.suffix}")
            tmp_censored = file_path.parent / f".tmp_{file_path.name}"
            saved = censor_file(
                src_path=file_path,
                dst_path=tmp_censored if overwrite else dst,
                style=style,
                targets=targets,
                min_confidence=min_confidence,
                pose_code=pose_code,
                enable_anchor_fallback=enable_anchor_fallback,
            )
            if saved is not None:
                if overwrite:
                    if file_path.exists():
                        file_path.unlink()
                    tmp_censored.rename(file_path)
                processed_count += 1
                print(f"[{idx}/{total}] 검열 완료 (백업: _origin/): {file_path.name}")
            else:
                if overwrite and tmp_censored.exists():
                    tmp_censored.unlink()
        except Exception as e:
            print(f"[{idx}/{total}] 처리 실패: {file_path.name} ({e})")

    print(f"\n[작업 완료] 총 {processed_count}장의 에셋에 정밀 검열을 적용했습니다.\n")
    return processed_count


def main():
    parser = argparse.ArgumentParser(
        description="키로(kiro) 2D 애니메이션 일러스트 고정밀 성기 자동 검열 도구"
    )
    parser.add_argument(
        "-d", "--dir",
        type=str,
        help="검열을 수행할 일러스트 폴더 경로"
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        help="검열을 수행할 단일 이미지 파일 경로"
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
            "black_bar",  # 레거시 호환
        ],
        help="검열 렌더링 스타일 (기본값: white_silhouette)"
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=["penis", "pussy"],
        help="검열 대상 라벨 (기본값: penis pussy)"
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=0.15,
        help="검출 신뢰도 임계치 (기본값: 0.15)"
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="원본 파일을 덮어쓰지 않고 _censored 파일로 별도 저장"
    )
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="AI 미탐지 시 체위별 세이프 앵커 폴백 비활성화"
    )

    args = parser.parse_args()

    # 레거시 스타일명 매핑
    style = "wide_bar" if args.style == "black_bar" else args.style

    if args.file:
        file_path = Path(args.file).resolve()
        if not file_path.exists():
            print(f"[오류] 파일을 찾을 수 없습니다: {file_path}")
            sys.exit(1)

        dst = None if not args.no_overwrite else file_path.with_name(f"{file_path.stem}_censored{file_path.suffix}")
        out = censor_file(
            src_path=file_path,
            dst_path=dst,
            style=style,
            targets=args.targets,
            min_confidence=args.conf,
            enable_anchor_fallback=not args.no_anchor,
        )
        if out:
            print(f"[완료] 검열본 저장 완료: {out}")
        else:
            print("[통과] 검열 대상 부위가 감지되지 않았습니다.")

    elif args.dir:
        run_batch_censor_with_backup(
            folder_path=args.dir,
            style=style,
            overwrite=not args.no_overwrite,
            targets=args.targets,
            min_confidence=args.conf,
            enable_anchor_fallback=not args.no_anchor,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

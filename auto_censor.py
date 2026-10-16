"""
auto_censor.py
키로(kiro) 고정밀 일러스트 자동 검열 CLI 진입점 스크립트.
내부적으로 `generator.censor` 엔진을 호출하며, 레거시 인자(-d, -m)와 신규 스타일을 완전 지원합니다.

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
import sys
from pathlib import Path

from generator.censor import (
    censor_file,
    run_batch_censor,
)


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
        run_batch_censor(
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
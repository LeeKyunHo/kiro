"""
tools/apply_precise_censor.py
스파이 로스터 10명 전 캐릭터의 H-씬을 _origin 무검열 원본에서 복원한 뒤,
안전 앵커(safe_anchor) 없이 AI 순수 정밀 감지 기반으로 슬림 화이트 실루엣 검열을 재적용합니다.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import time

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator.censor import censor_file

CHARS = ["cla", "evl", "kzk", "rvn", "scl", "srh", "vic", "viv", "chl", "dom"]


def main():
    base_dir = Path("projects/spy/assets")
    print(f"\n[정밀 검열 시작] 총 {len(CHARS)}명 캐릭터 H-씬 재검열 착수\n{'=' * 60}")
    
    total_processed = 0
    total_censored = 0
    total_bypassed = 0
    start_total = time.perf_counter()

    for idx, char in enumerate(CHARS, 1):
        asset_dir = base_dir / char
        origin_dir = base_dir / f"{char}_origin"

        if not origin_dir.exists():
            print(f"[{idx}/{len(CHARS)}] {char}: origin 폴더 없음, 건너뜀")
            continue

        orig_files = sorted(origin_dir.glob("*.webp"))
        print(f"\n[{idx}/{len(CHARS)}] {char} (총 {len(orig_files)}개 H-씬):")

        char_censored = 0
        char_bypassed = 0
        c_start = time.perf_counter()

        for f in orig_files:
            target_f = asset_dir / f.name
            # 1. 무검열 원본 복원 (과도한 앵커 검열 제거)
            shutil.copy2(f, target_f)

            # 2. AI 순수 정밀 검열 (안전 앵커 완전 해제)
            tmp_censored = asset_dir / f".tmp_{f.name}"
            saved = censor_file(
                src_path=target_f,
                dst_path=tmp_censored,
                style="white_silhouette",
                enable_anchor_fallback=False,
            )

            if saved is not None and tmp_censored.exists():
                if target_f.exists():
                    target_f.unlink()
                tmp_censored.rename(target_f)
                char_censored += 1
            else:
                if tmp_censored.exists():
                    tmp_censored.unlink()
                char_bypassed += 1

            total_processed += 1

        c_elapsed = time.perf_counter() - c_start
        total_censored += char_censored
        total_bypassed += char_bypassed
        print(f"  -> 완료 ({c_elapsed:.1f}초): 정밀 검열 {char_censored}장 / 무검열 원본 보존 {char_bypassed}장")

    total_time = time.perf_counter() - start_total
    print("\n" + "=" * 60)
    print(f"[전체 완료] 총 소요 시간: {total_time:.1f}초 ({total_time/60:.1f}분)")
    print(f"  - 처리 파일 수: {total_processed}장")
    print(f"  - 실제 성기 노출 정밀 검열 적용: {total_censored}장")
    print(f"  - 성기 미노출 원본 바디 보존: {total_bypassed}장")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
젠잇이 지목한 이미지 삭제 후 재생성
- 모자이크 -> 흰색 완전 차단 censor 적용된 새 버전으로 교체
"""
import subprocess, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROSTER = "dark_generals"
ASSETS = BASE / f"generated_assets_{ROSTER}"

# 젠잇 지목 목록
FLAGGED = {
    "bel": [24, 27, 140, 141, 142, 144, 145, 146, 148, 149,
            151, 153, 154, 155, 156, 157, 158, 159, 160, 161,
            162, 163, 164, 165, 166, 167, 170],
    "cam": [40, 41, 43, 44, 45, 46, 47, 48, 49, 50,
            53, 54, 55, 58, 61, 62, 63, 64, 65, 67],
    "cha": [40, 41, 42, 43, 44, 45, 47, 49, 50, 53,
            54, 56, 59, 60, 64, 65, 66, 67, 69, 70],
    "dem": [40, 41, 42, 43, 49, 50, 54, 57, 62, 64, 70],
    "ele": [41, 42, 43, 44, 49, 53, 54, 59, 61, 64, 67],
    "lil": [40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
            52, 54, 60, 61, 63, 64, 65, 69, 70],
    "mai": [40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
            53, 55, 59, 61, 62, 64, 65],
    "mic": [140, 141, 143, 145, 146, 147, 148, 149,
            153, 155, 158, 160, 162],
    "rog": [140, 141, 142, 144, 145, 146, 147, 149, 151, 152,
            153, 154, 157, 158, 160, 161, 162, 163, 164, 165,
            166, 167, 168, 169, 170],
    "ser": [40, 41, 42, 44, 46, 47, 48, 52, 53, 61, 64],
}

# 코드 폭 결정 (3자리)
WIDTH = 3

def fmt(code: int) -> str:
    return str(code).zfill(WIDTH)

# 1. 지목된 파일 삭제
deleted = 0
for prefix, codes in FLAGGED.items():
    char_dir = ASSETS / prefix
    if not char_dir.exists():
        print(f"[SKIP] {prefix}/ 폴더 없음")
        continue
    for code in codes:
        path = char_dir / f"{prefix}_{fmt(code)}.webp"
        if path.exists():
            path.unlink()
            deleted += 1

print(f"✅ {deleted}개 파일 삭제 완료\n")

# 2. 캐릭터별로 재생성 (삭제된 것만 새로 생성됨 - 스킵 로직)
failed = []
for prefix in FLAGGED:
    char_dir = ASSETS / prefix
    if not char_dir.exists():
        continue

    # 재생성할 코드만 mode로 지정
    codes = FLAGGED[prefix]
    codes_expr = ",".join(str(c) for c in sorted(codes))

    print(f"[GEN] {prefix} ({len(codes)}장) ...")
    cmd = [
        sys.executable, "sd_batch_generator.py",
        "--char", prefix,
        "-r", "dar",
        "--mode", codes_expr,
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[FAIL] {prefix}")
        failed.append(prefix)

print(f"\n{'='*50}")
if failed:
    print(f"실패: {failed}")
    print("같은 명령 재실행하면 실패한 것만 이어서 생성됩니다.")
else:
    print("모든 재생성 완료!")

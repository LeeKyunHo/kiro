"""
generator.reporter
결과 요약 출력, 윈도우 탐색기 연동 및 젠잇(GenIt) 호출 마크다운 생성.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from generator.config import (
    GENIT_STATUS_TEMPLATE,
    MIN_CODE_WIDTH,
    SEPARATOR,
    URL_PLACEHOLDER,
)
from generator.models import BatchResult, PoseDatabase


def code_width(codes: Sequence[int]) -> int:
    """최대 코드 자릿수에 맞춘 패딩 폭 (최소 2자리)."""
    if not codes:
        return MIN_CODE_WIDTH
    return max(MIN_CODE_WIDTH, len(str(max(codes))))


def format_code(code: int, width: int) -> str:
    """코드 제로 패딩 단일 진입점."""
    return f"{code:0{width}d}"


def asset_filename(prefix: str, code: int, width: int) -> str:
    """파일명 조립 단일 진입점."""
    return f"{prefix}_{format_code(code, width)}.webp"


def mode_badge(dry_run: bool, mock: bool) -> str:
    if dry_run:
        return " [DRY-RUN]"
    if mock:
        return " [MOCK]"
    return ""


def build_section_guide(
    db: PoseDatabase, codes: Sequence[int], prefix: str, width: int
) -> str:
    """JSON 실제 구성에서 상태 매핑 가이드를 유도한다."""
    present = set(codes)
    blocks: list[str] = []

    for section, section_codes in db.sections.items():
        available = [code for code in section_codes if code in present]
        if not available:
            continue
        rows = "\n".join(
            f"  {db.entries[code].label:<26} -> {asset_filename(prefix, code, width)}"
            for code in available
        )
        blocks.append(f"[{section}]\n{rows}")

    return "\n\n".join(blocks)


def build_genit_block(
    prefix: str,
    codes: Sequence[int],
    db: PoseDatabase,
    width: int,
    badge: str = "",
) -> str:
    """
    젠잇 복사용 마크다운 블록을 조립해 문자열로 반환한다.
    - {{url}}prefix/prefix_NNN.webp 단독 줄 출력
    """
    urls = [f"{URL_PLACEHOLDER}{prefix}/{asset_filename(prefix, c, width)}" for c in codes]
    calls = "\n".join(url for url in urls)
    files = "\n".join(
        f"- `{url}` ({db.entries[code].label})" for code, url in zip(codes, urls)
    )
    guide = build_section_guide(db, codes, prefix, width)
    status = GENIT_STATUS_TEMPLATE.format(
        name=prefix, title="직책입력", status="현재상태", desc="대사한줄"
    )

    return f"""
{SEPARATOR}
  젠잇(ZenIt) 복사용 에셋 블록 | {prefix}   (총 {len(codes)}개){badge}
{SEPARATOR}

### {prefix} 이미지 호출 코드
{calls}

### {prefix} 파일 목록
{files}

### {prefix} 상태 매핑 가이드
{guide}

### {prefix} 상태창 템플릿
{status}
{SEPARATOR}
"""


def open_in_explorer(path: Path) -> None:
    """결과 폴더를 윈도우 파일 탐색기로 연다."""
    if os.name != "nt":
        return
    try:
        os.startfile(path)
    except OSError as e:
        print(f"[WARN] 탐색기를 열지 못했습니다: {e}")


def print_summary(
    result: BatchResult,
    save_dir: Path,
    badge: str,
    vram: tuple[float, float] | None = None,
) -> None:
    """실행 요약 리포트."""
    print(
        f"\n[작업 완료]{badge} 성공 {len(result.success)} / "
        f"건너뜀 {len(result.skipped)} / 실패 {len(result.failed)}"
    )
    if result.planned:
        print(f"           계획 {len(result.planned)}건 (파일 미생성)")
    if result.failed:
        print(f"           실패 코드: {result.failed_codes}")
    if result.aborted:
        print("           WebUI 연결이 끊겨 중단되었습니다.")

    if timing := result.timing:
        print(f"[측정] {timing.format()}")
    if vram:
        peak, total = vram
        ratio = peak / total * 100 if total else 0
        print(f"[VRAM] 피크 {peak:.2f} / {total:.2f} GiB ({ratio:.0f}%)")

    print(f"           폴더: {save_dir}")

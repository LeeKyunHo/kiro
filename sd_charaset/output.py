"""
젠잇 마크다운 조립 및 콘솔 리포트.

**조립은 문자열을 반환하고, 출력은 별도 함수가 한다.**

이 분리가 진단에서 stdout 캡처 없이 라인 수와 리터럴 포함 여부를 직접
검사할 수 있게 만든다. print 가 섞여 있으면 테스트가 출력을 가로채야 한다.

마크다운은 `logging_setup.emit` 으로 **stdout** 에 나가고 진행 로그는
stderr 로 나간다. 그래서 `> assets.md` 리다이렉트로 순수 마크다운만
추출할 수 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import config
from .codes import CodeFormatter
from .logging_setup import emit, get_logger
from .models import BatchResult, PoseDatabase, VramSnapshot

_logger = get_logger("output")


# ─────────────────────────────────────────────
# 젠잇 마크다운
# ─────────────────────────────────────────────
def build_asset_urls(
    prefix: str, codes: Sequence[int], formatter: CodeFormatter
) -> tuple[str, ...]:
    """
    젠잇 호출 URL 목록.

    `URL_PLACEHOLDER` 를 상수로 둔 이유: f-string 안에서 리터럴 `{{url}}`
    을 출력하려면 `{{{{url}}}}` 로 4중 작성해야 한다. 가독성이 나쁘고
    실수가 나기 쉬워 일반 문자열 상수로 분리했다.
    """
    return tuple(
        f"{config.URL_PLACEHOLDER}{prefix}/{formatter.filename(prefix, code)}"
        for code in codes
    )


def build_section_guide(
    database: PoseDatabase,
    codes: Sequence[int],
    prefix: str,
    formatter: CodeFormatter,
) -> str:
    """
    상태 매핑 가이드를 JSON 실제 구성에서 유도한다.

    하드코딩된 예시가 아니라 DB 에서 만들므로, JSON 을 고치면 가이드도
    자동으로 따라 바뀐다.
    """
    present = set(codes)
    blocks: list[str] = []

    for section, section_codes in database.sections.items():
        available = [code for code in section_codes if code in present]
        if not available:
            continue
        rows = "\n".join(
            f"  {database.entry(code).label:<26} -> "
            f"{formatter.filename(prefix, code)}"
            for code in available
        )
        blocks.append(f"[{section}]\n{rows}")

    return "\n\n".join(blocks)


def build_genit_block(
    *,
    prefix: str,
    codes: Sequence[int],
    database: PoseDatabase,
    formatter: CodeFormatter,
    badge: str = "",
) -> str:
    """
    젠잇 복사용 마크다운 블록을 조립해 문자열로 반환한다.

    호출 라인 수가 `codes` 개수와 정확히 일치해야 한다. 진단에서 이
    불변식을 검사한다.
    """
    urls = build_asset_urls(prefix, codes, formatter)

    calls = "\n".join(f"![image]({url})" for url in urls)
    # strict=True 로 길이 불일치를 조용히 넘기지 않는다.
    # urls 는 codes 에서 만들었으므로 길이가 다르면 조립 버그다.
    files = "\n".join(
        f"- `{url}` ({database.entry(code).label})"
        for code, url in zip(codes, urls, strict=True)
    )
    guide = build_section_guide(database, codes, prefix, formatter)
    status = config.GENIT_STATUS_TEMPLATE.format(
        name=prefix, **config.GENIT_STATUS_DEFAULTS
    )

    return f"""
{config.SEPARATOR}
  젠잇(Genit) 복사용 에셋 블록 | {prefix}   (총 {len(codes)}개){badge}
{config.SEPARATOR}

### {prefix} 이미지 호출 코드
{calls}

### {prefix} 파일 목록
{files}

### {prefix} 상태 매핑 가이드
{guide}

### {prefix} 상태창 템플릿
{status}
{config.SEPARATOR}
"""


# ─────────────────────────────────────────────
# 콘솔 리포트
# ─────────────────────────────────────────────
def log_batch_header(
    *,
    prefix: str,
    profile_name: str,
    mode: str,
    count: int,
    formatter: CodeFormatter,
    output_dir: Path,
    positive: str,
    negative: str,
    badge: str,
) -> None:
    """배치 시작 정보를 로그로 남긴다."""
    _logger.info(
        "\n[작업 시작]%s 캐릭터: %s | 프로필: %s | 모드: %s (%d장) | 폭: %d",
        badge,
        prefix,
        profile_name,
        mode,
        count,
        formatter.width,
    )
    _logger.info("[저장] %s", output_dir)
    _logger.info("[POS]  %s", positive)
    _logger.info("[NEG]  %s", negative)


def log_summary(
    result: BatchResult,
    output_dir: Path,
    badge: str,
    vram: VramSnapshot | None = None,
) -> None:
    """실행 요약을 로그로 남긴다."""
    _logger.info(
        "\n[작업 완료]%s 성공 %d / 건너뜀 %d / 실패 %d",
        badge,
        len(result.succeeded),
        len(result.skipped),
        len(result.failures),
    )
    if result.planned:
        _logger.info("           계획 %d건 (파일 미생성)", len(result.planned))
    if result.failures:
        _logger.warning("           실패 코드: %s", list(result.failed_codes))
    if result.aborted:
        _logger.error("           WebUI 연결이 끊겨 중단되었습니다.")

    # 설정 비교(--medvram-sdxl, xFormers 등)의 판단 근거가 된다.
    if timing := result.timing:
        _logger.info("[측정] %s", timing.format())
    if vram is not None:
        _logger.info("[VRAM] %s", vram.format())

    _logger.info("           폴더: %s", output_dir)


def emit_genit_block(
    *,
    prefix: str,
    codes: Sequence[int],
    database: PoseDatabase,
    formatter: CodeFormatter,
    badge: str = "",
) -> None:
    """젠잇 블록을 stdout 으로 출력한다."""
    emit(
        build_genit_block(
            prefix=prefix,
            codes=codes,
            database=database,
            formatter=formatter,
            badge=badge,
        )
    )


def emit_interrogate_header(*, source_label: str, model: str) -> None:
    """
    태그 역추출 대상을 먼저 출력한다.

    API 호출 전에 내보내는 이유: 호출이 실패해도 무엇을 어떤 모델로
    시도했는지 남아야 사용자가 원인을 판단할 수 있다.
    """
    emit(f"\n{config.SEPARATOR}")
    emit(f"  태그 추출 | {source_label} | 모델: {model}")
    emit(config.SEPARATOR)


def emit_interrogate_report(
    *,
    raw: str,
    tags: Sequence[str],
    removed: Sequence[str],
    filtered: str,
    program: str,
) -> None:
    """
    태그 역추출 결과를 출력한다.

    `[원본]` 이 아니라 `[권장]` 을 쓰도록 유도한다. 원본에는 성별·인원
    태그가 있어 프로필과 충돌하기 때문이다.

    명령 예시의 prefix 를 `PREFIX` 대문자 자리표시자로 두는 이유:
    `--from_image` 는 `--prefix` 를 요구하지 않으므로 실제 값을 알 수 없다.
    사용자가 채워야 함을 시각적으로 드러낸다.
    """
    emit(f"\n[원본] ({len(tags)}개 태그)")
    emit(raw)

    if removed:
        _logger.warning("성별·인원 태그가 감지되었습니다: %s", list(removed))
        _logger.warning(
            "프로필(%s)에서 이미 다루므로 --char_prompt 에는 넣지 마세요.",
            config.PROFILES_KEY,
        )

    kept_count = len(tags) - len(removed)
    emit(f"\n[권장] ({kept_count}개 태그)")
    emit(filtered)

    emit("\n[그대로 실행하려면]")
    emit(f'{program} --prefix PREFIX --char_prompt "{filtered}"')
    emit(f"{config.SEPARATOR}\n")

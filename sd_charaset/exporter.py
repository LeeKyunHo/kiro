"""
젠잇(Genit) 캐릭터 카드 파일 내보내기.

콘솔 출력은 터미널을 닫으면 사라진다. 40장 세트를 뽑고 나서 마크다운을
복사하지 않고 창을 닫으면 다시 실행해야 하고, 재실행은 전부 "건너뜀" 이
되어 마크다운은 나오지만 그걸 또 복사해야 한다. 카드를 파일로 남기면
에셋 폴더를 열어 바로 집어갈 수 있다.

**설계 결정 3가지**

1. **카드는 이번 실행의 결과가 아니라 폴더의 현재 상태를 기술한다.**

   `--mode emotions` 로 10장만 돌렸을 때 기존 40항목 카드가 10항목으로
   덮이면 데이터 손실이다. 그래서 배치 결과를 쓰지 않고 디스크를 스캔해
   실존 파일에서 코드를 역파싱한다. 어떤 `--mode` 로 몇 번을 나눠
   돌리든 카드는 항상 폴더 전체를 반영한다.

2. **조립(`build_card`)과 쓰기(`write_card`)를 분리한다.**

   `output.py` 와 같은 정책이다. 진단이 파일 없이 내용을 검사할 수 있다.

3. **`output.py` 의 순수 조립 함수를 재사용한다.**

   `build_asset_urls` 와 `build_section_guide` 를 그대로 쓴다. 카드 전용
   조립을 새로 쓰면 콘솔 블록과 카드의 형식이 갈라져 한쪽만 고쳐지는
   사고가 난다. 마크다운 골격만 이 모듈이 만든다.

**원자적 쓰기를 쓰지 않는 이유**

이미지는 `.part` 를 경유해 원자적으로 쓴다. 재개 로직이 반쪽 파일을
완성품으로 보고 영구히 건너뛰기 때문이다. 카드에는 그런 소비자가 없다.
매 실행마다 통째로 다시 쓰므로 중단된 카드는 다음 실행에서 교정된다.
같은 안전장치를 필요 없는 곳에 복제하면 유지 대상만 늘어난다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from . import config, output
from .codes import CodeFormatter
from .errors import StorageError
from .logging_setup import get_logger
from .models import PoseDatabase, ResolvedCharacter
from .storage import AssetPaths

_logger = get_logger("exporter")

_ASSET_STEM: Final = re.compile(config.ASSET_FILENAME_PATTERN_SOURCE)


# ─────────────────────────────────────────────
# 카드 메타데이터
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CardMeta:
    """
    카드 머리말에 실을 정보.

    `benchmark.BenchmarkReport` 와 같은 위치에 둔다. 기능 전용 표현 데이터는
    `models.py` 가 아니라 해당 모듈에 두는 것이 이 패키지의 기존 관례다.

    `command` 는 이 세트를 재생성하는 명령이다. 카드만 보고 같은 결과를
    다시 만들 수 있어야 카드가 자기완결적인 문서가 된다.
    """

    prefix: str
    profile_name: str
    char_prompt: str
    negative: str
    command: str
    kind_label: str
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            # 타임존 없는 로컬 시각을 쓴다. 사람이 "언제 뽑았는지" 를
            # 확인하는 용도이고, 기계 판독은 벤치마크 매니페스트가 담당한다.
            object.__setattr__(
                self,
                "created_at",
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            )


def build_command_hint(
    resolved: ResolvedCharacter | None, program: str, prefix: str
) -> str:
    """
    카드에 실을 재생성 명령을 만든다.

    프리셋으로 실행했다면 `--char mika` 한 줄이면 되므로 그것을 보여준다.
    긴 명령을 그대로 옮겨 적는 것보다 짧고, 프리셋 사용을 자연히 학습한다.
    """
    if resolved is not None and resolved.from_preset and not resolved.overridden:
        return f"{program} --char {resolved.preset_name}"

    if resolved is not None and resolved.char_prompt:
        parts = [program, "--prefix", prefix, "--char_prompt", f'"{resolved.char_prompt}"']
        if resolved.profile:
            parts += ["--profile", resolved.profile]
        if resolved.custom_neg:
            parts += ["--custom_neg", f'"{resolved.custom_neg}"']
        return " ".join(parts)

    return f"{program} --prefix {prefix} --char_prompt \"...\""


# ─────────────────────────────────────────────
# 디스크 스캔
# ─────────────────────────────────────────────
def scan_asset_codes(directory: Path, prefix: str) -> tuple[int, ...]:
    """
    폴더에서 `{prefix}_{code}.webp` 파일을 찾아 코드를 역파싱한다.

    `prefix` 를 정확히 대조하는 이유: 한 폴더에 여러 접두어가 섞이는 것은
    정상 경로에서 일어나지 않지만, 사용자가 파일을 옮겼을 때 남의 코드가
    카드에 실리면 안 된다.

    자릿수가 다른 파일(`mika_07` 과 `mika_007`)이 함께 있어도 정수로
    환산해 중복을 제거한다. DB 가 100항목을 넘어가며 패딩 폭이 늘어난
    뒤에도 과거 파일이 남아 있을 수 있다.

    Returns:
        오름차순 정렬된 코드 튜플. 폴더가 없으면 빈 튜플.
    """
    if not directory.is_dir():
        return ()

    codes: set[int] = set()
    for path in directory.glob(f"*{config.ASSET_SUFFIX}"):
        matched = _ASSET_STEM.match(path.stem)
        if matched is None or matched.group("prefix") != prefix:
            continue
        codes.add(int(matched.group("code")))
    return tuple(sorted(codes))


def select_card_codes(
    directory: Path, prefix: str, database: PoseDatabase
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """
    카드에 실을 코드와 DB 에 없어 제외된 코드를 나눈다.

    라벨과 섹션은 DB 에서 온다. DB 에 없는 코드는 이름을 붙일 수 없으므로
    카드에 실을 수 없다. 조용히 버리지 않고 함께 반환해 호출부가 경고할 수
    있게 한다. JSON 에서 항목을 지웠는데 이미지가 남아 있는 상태다.

    Returns:
        (카드에 실을 코드, 제외된 코드)
    """
    found = scan_asset_codes(directory, prefix)
    known = tuple(code for code in found if code in database.entries)
    unknown = tuple(code for code in found if code not in database.entries)
    return known, unknown


# ─────────────────────────────────────────────
# 조립 (순수)
# ─────────────────────────────────────────────
def _fence(body: str, language: str = "") -> str:
    """코드 펜스로 감싼다. 빈 본문은 자리표시자로 채운다."""
    return f"```{language}\n{body or '(없음)'}\n```"


def build_card(
    *,
    meta: CardMeta,
    codes: Sequence[int],
    database: PoseDatabase,
    formatter: CodeFormatter,
) -> str:
    """
    젠잇 캐릭터 카드 마크다운을 조립해 문자열로 반환한다.

    파일을 쓰지 않는 순수 함수다. 진단이 내용을 직접 검사한다.

    호출 코드 줄 수가 `codes` 개수와 정확히 일치해야 한다. 이 불변식을
    진단에서 검사한다.

    호출 코드와 상태 매핑을 코드 펜스로 감싸는 이유: 젠잇에 붙여넣을
    원문이므로 마크다운 렌더러가 이미지를 실제로 표시해버리면 복사할 수
    없다. `{{url}}` 자리표시자도 그대로 보존해야 한다.
    """
    urls = output.build_asset_urls(meta.prefix, codes, formatter)
    calls = "\n".join(f"![image]({url})" for url in urls)
    guide = output.build_section_guide(database, codes, meta.prefix, formatter)
    status = config.GENIT_STATUS_TEMPLATE.format(
        name=meta.prefix, **config.GENIT_STATUS_DEFAULTS
    )

    files = "\n".join(
        f"| `{formatter.filename(meta.prefix, code)}` "
        f"| {database.entry(code).section} "
        f"| {database.entry(code).label} |"
        for code in codes
    )

    return f"""# {meta.prefix} 젠잇 캐릭터 카드

> 자동 생성 문서입니다. 다시 실행하면 덮어써집니다.
> 직접 메모를 남기려면 별도 파일에 적으세요.

| 항목 | 값 |
|---|---|
| 약칭 | `{meta.prefix}` |
| 프로필 | {meta.profile_name} |
| 외형 태그 | {meta.char_prompt or "(없음)"} |
| 이미지 수 | {len(codes)}장 |
| 산출물 | {meta.kind_label} |
| 생성 시각 | {meta.created_at} |

## 1. 이미지 호출 코드

젠잇 캐릭터 설정에 그대로 붙여넣습니다. `{config.URL_PLACEHOLDER}` 는
젠잇이 실제 주소로 바꿔주는 자리이므로 고치지 않습니다.

{_fence(calls)}

## 2. 상태 매핑 가이드

어떤 감정·상황일 때 어떤 파일을 부를지 정리한 표입니다. 챗봇 프롬프트에
감정별 이미지를 연결할 때 참고합니다.

{_fence(guide)}

## 3. 상태창 템플릿

`title` / `status` / `desc` 를 캐릭터에 맞게 채웁니다.

{_fence(status)}

## 4. 파일 목록

| 파일 | 섹션 | 라벨 |
|---|---|---|
{files or "| (없음) | - | - |"}

## 5. 재생성

이미지를 다시 뽑거나 추가할 때 쓰는 명령입니다. 이미 있는 파일은
건너뛰므로 몇 번 실행해도 안전합니다.

```powershell
{meta.command}
```

네거티브 프롬프트(참고용):

{_fence(meta.negative)}
"""


# ─────────────────────────────────────────────
# 쓰기
# ─────────────────────────────────────────────
def card_path(paths: AssetPaths) -> Path:
    """
    카드 파일 경로.

    `output_dir` 을 쓰므로 실제 실행은 `generated_assets/{prefix}/`,
    mock 은 `mock_assets/{prefix}/` 에 놓인다. mock 이 실제 폴더에 쓰면
    출력 격리가 깨지므로 경로를 따로 잡지 않는다.
    """
    return paths.output_dir / config.GENIT_CARD_TEMPLATE.format(prefix=paths.prefix)


def write_card(
    paths: AssetPaths,
    *,
    meta: CardMeta,
    codes: Sequence[int],
    database: PoseDatabase,
    formatter: CodeFormatter,
) -> Path:
    """
    카드를 파일로 저장한다.

    Returns:
        저장된 경로.

    Raises:
        StorageError: 쓰기 실패.
    """
    destination = card_path(paths)
    content = build_card(
        meta=meta, codes=codes, database=database, formatter=formatter
    )
    try:
        destination.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"카드를 저장할 수 없습니다: {destination}", str(exc)) from None
    return destination


def export_card(
    paths: AssetPaths,
    *,
    database: PoseDatabase,
    formatter: CodeFormatter,
    resolved: ResolvedCharacter | None,
    profile_name: str,
    negative: str,
    program: str,
    kind_label: str,
) -> Path | None:
    """
    디스크를 스캔해 카드를 만들고 저장한다.

    쓰기 실패를 예외로 올리지 않는다. 이미지 40장을 다 뽑은 뒤 카드
    저장만 실패했을 때 종료 코드를 1로 만들면 "실패했다" 는 신호가
    과장된다. 경고만 남기고 `None` 을 반환한다.

    Returns:
        저장된 경로. 실을 코드가 없거나 실패하면 `None`.
    """
    codes, unknown = select_card_codes(paths.output_dir, paths.prefix, database)

    if unknown:
        _logger.warning(
            "%s 에 DB 에 없는 코드의 이미지가 있어 카드에서 제외합니다: %s",
            paths.output_dir.name,
            list(unknown),
        )

    if not codes:
        _logger.info("카드에 실을 이미지가 없어 생성하지 않았습니다.")
        return None

    meta = CardMeta(
        prefix=paths.prefix,
        profile_name=profile_name,
        char_prompt=(resolved.char_prompt if resolved else "") or "",
        negative=negative,
        command=build_command_hint(resolved, program, paths.prefix),
        kind_label=kind_label,
    )

    try:
        destination = write_card(
            paths,
            meta=meta,
            codes=codes,
            database=database,
            formatter=formatter,
        )
    except StorageError as exc:
        _logger.warning("%s", exc.message)
        return None

    _logger.info("[카드]  %s (%d장)", destination, len(codes))
    return destination

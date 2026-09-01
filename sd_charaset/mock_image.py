"""
모의 이미지 생성.

**핵심 설계: API 반환값과 동일한 형태(PNG 바이트열)를 만든다.**

이렇게 하면 `AtomicImageWriter` 를 우회하지 않으므로 WebP 변환, 원자적
쓰기, 파일명 조립까지 검증 범위에 들어온다. mock 이 "파일명만 흉내내는
시뮬레이션" 이 아니라 종단 테스트가 되는 이유다.

        실제:  client.generate()   -> PNG bytes ─┐
                                                  ├→ AtomicImageWriter → .webp
        mock:  render_mock_png()   -> PNG bytes ─┘
"""

from __future__ import annotations

import colorsys
import io
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from . import config
from .models import PoseEntry, ReferenceImage

FontLike = Any


def _hue_color(code: int) -> tuple[int, int, int]:
    """코드값으로 배경색을 분산시켜 이미지 구분이 육안으로 가능하게 한다."""
    hue = ((code * config.MOCK_HUE_STEP) % 360) / 360.0
    red, green, blue = colorsys.hsv_to_rgb(
        hue, config.MOCK_SATURATION, config.MOCK_VALUE
    )
    return int(red * 255), int(green * 255), int(blue * 255)


@lru_cache(maxsize=1)
def _load_fonts() -> tuple[FontLike, FontLike]:
    """
    폰트를 한 번만 로드해 재사용한다.

    캐시가 없으면 배치 장수만큼 truetype 파일을 반복해서 읽는다.
    폰트가 없는 환경에서도 예외 없이 완주해야 한다.
    """
    try:
        return (
            ImageFont.truetype(config.MOCK_FONT_NAME, config.MOCK_FONT_BIG_SIZE),
            ImageFont.truetype(config.MOCK_FONT_NAME, config.MOCK_FONT_SMALL_SIZE),
        )
    except OSError:
        fallback = ImageFont.load_default()
        return fallback, fallback


def render_mock_png(
    *,
    prefix: str,
    code_tag: str,
    entry: PoseEntry,
    reference: ReferenceImage | None = None,
) -> bytes:
    """
    더미 이미지를 PNG 바이트열로 만든다.

    Args:
        prefix: 에셋 약칭.
        code_tag: 제로 패딩된 코드 문자열. `CodeFormatter.tag()` 결과.
        entry: 포즈 엔트리. 섹션과 라벨을 표시한다.
        reference: 참조 이미지. 있으면 `MOCK +REF` 로 표기한다.

    해상도는 실제의 1/4 이며 종횡비는 동일하다. 장당 1초 이내로 끝나야
    검증 도구로서 의미가 있다.
    """
    font_big, font_small = _load_fonts()

    image = Image.new(
        "RGB", (config.MOCK_WIDTH, config.MOCK_HEIGHT), _hue_color_from_tag(code_tag)
    )
    draw = ImageDraw.Draw(image)

    # 줄 간격을 폰트 객체 동일성으로 판단하면, 폴백 시 두 폰트가 같은
    # 객체가 되어 모든 줄이 큰 간격을 쓰고 캔버스를 벗어난다.
    # 간격을 데이터로 명시해 그 결합을 끊는다.
    rows: tuple[tuple[str, FontLike, int], ...] = (
        (code_tag, font_big, config.MOCK_GAP_BIG),
        (prefix, font_small, config.MOCK_GAP_SMALL),
        (entry.section, font_small, config.MOCK_GAP_SMALL),
        (entry.label[: config.MOCK_LABEL_MAXLEN], font_small, config.MOCK_GAP_SMALL),
        (
            "MOCK +REF" if reference else "MOCK",
            font_small,
            config.MOCK_GAP_SMALL,
        ),
    )

    y = config.MOCK_TEXT_TOP
    for text, font, gap in rows:
        draw.text(
            (config.MOCK_TEXT_X, y), text, fill=config.MOCK_TEXT_COLOR, font=font
        )
        y += gap

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image.close()
    return buffer.getvalue()


def _hue_color_from_tag(code_tag: str) -> tuple[int, int, int]:
    """코드 태그 문자열에서 색을 산출한다. 비수치 태그는 0으로 취급한다."""
    try:
        return _hue_color(int(code_tag))
    except ValueError:
        return _hue_color(0)

"""
로깅 구성 및 표준 스트림 인코딩.

**스트림 분리 정책**

    stderr  진행 로그, 경고, 에러 (logging)
    stdout  젠잇 마크다운 블록 등 사용자가 가져다 쓰는 데이터 (print)

이렇게 나누면 아래가 가능해진다.

    python -m sd_charaset --prefix mika --char_prompt "..." > assets.md

로그는 터미널에 그대로 보이고 마크다운만 파일로 떨어진다. Unix 관행이며
파이프라인 도구로서 조합 가능성을 확보한다.
"""

from __future__ import annotations

import logging
import sys
from typing import Final, TextIO

LOGGER_NAME: Final = "sd_charaset"

# 레벨 접두어를 기존 출력 형식과 맞춘다. 사용자가 이미 익숙한 표기다.
_LEVEL_PREFIX: Final = {
    logging.DEBUG: "[DEBUG]",
    logging.INFO: "",
    logging.WARNING: "[WARN]",
    logging.ERROR: "[ERROR]",
    logging.CRITICAL: "[FATAL]",
}


class PrefixFormatter(logging.Formatter):
    """
    레벨별 접두어를 붙이는 포매터.

    INFO 는 접두어 없이 본문만 출력한다. 진행 로그가 대부분 INFO 이고,
    매 줄에 [INFO] 가 붙으면 시각적 소음이 된다.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        prefix = _LEVEL_PREFIX.get(record.levelno, f"[{record.levelname}]")
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{prefix} {message}" if prefix else message


def configure_stdio() -> None:
    """
    표준 스트림을 UTF-8 로 고정한다.

    Windows 에서 출력을 파이프/파일로 리다이렉트하면 Python 이 콘솔 UTF-8
    대신 로케일 인코딩(cp949)으로 폴백해 비-ASCII 기호에서
    UnicodeEncodeError 가 발생한다. 직접 실행 시에는 재현되지 않아
    놓치기 쉬운 경로다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 비표준 스트림이나 이미 닫힌 스트림은 그대로 둔다.
            pass


def configure_logging(verbose: bool = False, stream: TextIO | None = None) -> logging.Logger:
    """
    패키지 로거를 구성해 반환한다.

    Args:
        verbose: True 면 DEBUG 레벨까지 출력.
        stream: 핸들러가 쓸 스트림. 기본은 stderr.

    Returns:
        구성된 로거. 재호출 시 핸들러가 중복 추가되지 않는다.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # 라이브러리로 import 된 경우 상위 로거로 전파되면 중복 출력된다.
    logger.propagate = False

    target = stream if stream is not None else sys.stderr

    # 재호출 시 기존 핸들러를 교체한다. 누적되면 같은 줄이 여러 번 찍힌다.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(target)
    handler.setFormatter(PrefixFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(suffix: str | None = None) -> logging.Logger:
    """패키지 로거 또는 그 자식 로거를 반환한다."""
    if suffix:
        return logging.getLogger(f"{LOGGER_NAME}.{suffix}")
    return logging.getLogger(LOGGER_NAME)


def emit(text: str = "") -> None:
    """
    사용자가 가져다 쓰는 데이터를 stdout 으로 출력한다.

    로그가 아니라 산출물이다. 젠잇 마크다운 블록처럼 복사·리다이렉트
    대상이 되는 내용만 이 함수를 쓴다.
    """
    print(text, file=sys.stdout)

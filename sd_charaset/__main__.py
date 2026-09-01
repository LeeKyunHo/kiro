"""
`python -m sd_charaset` 진입점.

스크립트 파일 실행과 모듈 실행의 이중성을 없앤다. 패키지 어디에서
실행하든 동일한 경로 해석과 동일한 동작을 보장한다.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(prog="python -m sd_charaset"))

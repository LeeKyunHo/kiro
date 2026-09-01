#!/usr/bin/env python3
"""
하위 호환 진입점.

실제 구현은 `sd_charaset` 패키지에 있다. 이 파일은 기존 명령과 문서
(사용법.txt, WEDNESDAY_CHECKLIST.md, .kiro/steering/)를 깨지 않기 위해
유지한다.

    python sd_batch_generator.py --prefix mika --char_prompt "..."   (기존)
    python -m sd_charaset --prefix mika --char_prompt "..."          (권장)

두 명령은 완전히 동일하게 동작한다.

신규 코드는 패키지를 직접 쓰는 것을 권한다.

    from sd_charaset.cli import main
"""

from __future__ import annotations

import sys
from pathlib import Path

# 저장소를 클론한 위치에서 바로 실행할 수 있게 한다.
# pip install 없이 `python sd_batch_generator.py` 만으로 동작해야 한다.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sd_charaset.cli import main  # noqa: E402  (sys.path 조정 후 import 필요)

if __name__ == "__main__":
    sys.exit(main(prog="python sd_batch_generator.py"))

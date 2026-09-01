"""
sd_charaset — 캐릭터 챗봇용 이미지 에셋 배치 생성 파이프라인.

로컬 SD WebUI REST API 를 호출해 `pose_database.json` 항목 수에 맞춰
이미지를 일괄 생성하고, 젠잇(Genit) 규격 마크다운 호출 코드를 조립한다.

핵심 설계 원칙
--------------
프롬프트 데이터는 JSON 에, 로직은 Python 에 둔다. JSON 항목을 늘리면
코드 수정 없이 생성 장수가 늘어난다.

계층 구조
---------
순수 계층(tags, codes, validators, prompt, payload)은 네트워크·파일·전역
상태에 접근하지 않는다. 그 덕분에 GPU 없는 환경에서 페이로드 구조와
도메인 로직을 전량 검증할 수 있다.

    cli → commands → render → { api, storage, mock_image }
                            ↘ { payload, prompt, codes }
           ↘ diagnostics ────↗
           ↘ output ─────────↗

사용 예
-------
    python -m sd_charaset --prefix mika --char_prompt "silver hair"
    python -m sd_charaset --test

라이브러리로 쓸 때는 `main()` 을 직접 호출할 수 있다.

    from sd_charaset import main
    exit_code = main(["--test"])
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> int:
    """
    CLI 진입점의 얇은 재노출.

    `cli` 모듈을 여기서 즉시 import 하지 않는 이유: `sd_charaset` 를
    import 하는 것만으로 argparse 와 requests 가 로드되면 라이브러리
    용도로 쓸 때 시작 비용이 커진다.
    """
    from .cli import main as cli_main

    return cli_main(argv)

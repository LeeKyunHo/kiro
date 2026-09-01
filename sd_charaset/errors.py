"""
예외 계층 및 종료 코드 정책.

종료 코드를 예외 클래스 속성으로 두는 이유: 어떤 실패가 어떤 코드를
반환하는지가 예외 정의 옆에 붙어 한 곳에서 관리된다. 호출부가
`except CharasetError as e: return e.exit_code` 한 줄로 처리할 수 있어
종료 정책이 여러 파일에 흩어지지 않는다.
"""

from __future__ import annotations

from typing import Final

# argparse 표준: 인자 오류는 2
EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
EXIT_INTERRUPTED: Final = 130


class CharasetError(Exception):
    """
    이 패키지가 의도적으로 발생시키는 모든 예외의 루트.

    Attributes:
        message: 사용자에게 보여줄 주 메시지.
        hint: 해결 방법 안내. 없으면 빈 문자열.
        exit_code: 프로세스 종료 코드.
    """

    exit_code: int = EXIT_ERROR

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message

    def render(self) -> list[str]:
        """콘솔 출력용 줄 목록. 힌트가 있으면 들여쓰기해 덧붙인다."""
        lines = [f"[ERROR] {self.message}"]
        if self.hint:
            lines.append(f"        {self.hint}")
        return lines


class ConfigError(CharasetError):
    """설정 파일, CLI 인자, 입력 데이터의 오류."""


class DatabaseError(ConfigError):
    """pose_database.json 의 부재·문법·스키마 오류."""


class ValidationError(ConfigError):
    """prefix, weight 등 입력값 검증 실패."""


class StorageError(CharasetError):
    """파일 읽기·쓰기 실패."""


class ApiError(CharasetError):
    """WebUI API 호출 실패."""


class ApiUnavailableError(ApiError):
    """
    WebUI 에 연결할 수 없음.

    배치 중 이 예외가 발생하면 남은 코드를 계속 시도해도 무의미하므로
    루프를 중단시킨다. 개별 생성 실패(ApiError)와 구분하는 이유다.
    """


class UserAbort(CharasetError):
    """Ctrl+C 등 사용자 중단."""

    exit_code = EXIT_INTERRUPTED

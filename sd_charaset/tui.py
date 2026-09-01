"""
대화형 선택 UI — 표준 라이브러리만 사용.

**외부 의존성을 쓰지 않는 이유**

이 프로젝트는 의존성을 `requests`, `Pillow` 로 한정한다는 원칙이 있다.
`inquirer` / `rich` / `questionary` 는 편리하지만 편의 기능 하나로 그
원칙을 깨는 것은 비용 대비 이득이 없다. 화살표 키 입력은
`msvcrt`(Windows) 와 `termios`+`tty`(POSIX) 로 충분히 구현된다.

**비대화형 폴백이 필수다**

`stdin` 이 tty 가 아니면(파이프, CI, IDE 내장 터미널 일부) raw 모드
전환이 실패하거나 키 입력이 오지 않아 무한 대기한다. 그런 환경에서는
번호 입력 방식으로 자동 전환한다. 이것이 없으면 CI 에서 멈춘다.

**결과를 argv 로 조립한다**

선택 결과로 `Namespace` 를 직접 만들지 않고 argv 를 만들어 기존 파서에
다시 넣는다. 검증 경로가 수동 CLI 와 완전히 동일해져 규칙이 두 곳에
중복되지 않는다. 조립된 명령을 화면에 보여주므로 사용자가 CLI 사용법도
자연히 익히게 된다.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import config
from .errors import UserAbort

# ─────────────────────────────────────────────
# ANSI 제어
# ─────────────────────────────────────────────
_CSI = "\x1b["
_HIDE_CURSOR = f"{_CSI}?25l"
_SHOW_CURSOR = f"{_CSI}?25h"
_CLEAR_LINE = f"{_CSI}2K"

KEY_UP = "up"
KEY_DOWN = "down"
KEY_ENTER = "enter"
KEY_ESCAPE = "escape"
KEY_QUIT = "quit"
KEY_OTHER = "other"


def supports_interactive() -> bool:
    """
    화살표 키 방식이 가능한 환경인지 판단한다.

    stdin 과 stdout 이 모두 tty 여야 한다. 하나라도 리다이렉트되면
    커서 제어와 키 입력이 정상 동작하지 않는다.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# ─────────────────────────────────────────────
# 키 입력
# ─────────────────────────────────────────────
def _read_key_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    # 방향키는 접두어(0x00 또는 0xE0) 뒤에 실제 코드가 온다.
    if char in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {"H": KEY_UP, "P": KEY_DOWN}.get(code, KEY_OTHER)
    if char in ("\r", "\n"):
        return KEY_ENTER
    if char == "\x1b":
        return KEY_ESCAPE
    if char == "\x03":
        raise KeyboardInterrupt
    if char.lower() == "q":
        return KEY_QUIT
    if char == "k":
        return KEY_UP
    if char == "j":
        return KEY_DOWN
    return KEY_OTHER


def _read_key_posix() -> str:
    # POSIX 전용 모듈이라 Windows 에서는 존재하지 않는다.
    # 정적 분석기가 플랫폼을 알 수 없으므로 이 지점에서만 검사를 끈다.
    import termios  # type: ignore[import-not-found, unused-ignore]
    import tty  # type: ignore[import-not-found, unused-ignore]

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)  # type: ignore[attr-defined, unused-ignore]
    try:
        tty.setraw(fd)  # type: ignore[attr-defined, unused-ignore]
        char = sys.stdin.read(1)
        if char == "\x1b":
            # ESC 시퀀스. 방향키는 "\x1b[A" 형태다.
            following = sys.stdin.read(2)
            if following == "[A":
                return KEY_UP
            if following == "[B":
                return KEY_DOWN
            return KEY_ESCAPE
        if char in ("\r", "\n"):
            return KEY_ENTER
        if char == "\x03":
            raise KeyboardInterrupt
        if char.lower() == "q":
            return KEY_QUIT
        if char == "k":
            return KEY_UP
        if char == "j":
            return KEY_DOWN
        return KEY_OTHER
    finally:
        termios.tcsetattr(  # type: ignore[attr-defined, unused-ignore]
            fd,
            termios.TCSADRAIN,  # type: ignore[attr-defined, unused-ignore]
            saved,
        )


def read_key() -> str:
    """단일 키를 읽어 논리 이름으로 반환한다."""
    if os.name == "nt":
        return _read_key_windows()
    return _read_key_posix()


# ─────────────────────────────────────────────
# 선택 항목
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Choice:
    """선택지 하나."""

    value: str
    label: str
    hint: str = ""

    def render(self) -> str:
        return f"{self.label}  —  {self.hint}" if self.hint else self.label


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _draw(title: str, choices: Sequence[Choice], cursor: int) -> None:
    _write(f"\n{title}\n")
    for index, choice in enumerate(choices):
        marker = "›" if index == cursor else " "
        style = f"{_CSI}1;36m" if index == cursor else ""
        reset = f"{_CSI}0m" if style else ""
        _write(f"  {marker} {style}{choice.render()}{reset}\n")
    _write(f"{_CSI}2m  ↑↓ 이동 · Enter 선택 · q 취소{_CSI}0m\n")


def _erase(line_count: int) -> None:
    for _ in range(line_count):
        _write(f"{_CSI}1A{_CLEAR_LINE}")


def select(title: str, choices: Sequence[Choice], default: int = 0) -> str:
    """
    항목 하나를 선택한다.

    tty 환경에서는 화살표 키, 아니면 번호 입력으로 자동 전환한다.

    Raises:
        UserAbort: 사용자가 취소했을 때.
        ValueError: 선택지가 비었을 때.
    """
    if not choices:
        raise ValueError("선택지가 비어 있습니다")

    if not supports_interactive():
        return _select_numbered(title, choices, default)

    cursor = max(0, min(default, len(choices) - 1))
    total_lines = len(choices) + 3  # 제목 앞 개행 + 제목 + 항목 + 안내

    _write(_HIDE_CURSOR)
    try:
        while True:
            _draw(title, choices, cursor)
            try:
                key = read_key()
            except KeyboardInterrupt:
                raise UserAbort("선택이 취소되었습니다.") from None

            if key == KEY_UP:
                cursor = (cursor - 1) % len(choices)
            elif key == KEY_DOWN:
                cursor = (cursor + 1) % len(choices)
            elif key == KEY_ENTER:
                _erase(total_lines)
                _write(f"  {title}: {choices[cursor].label}\n")
                return choices[cursor].value
            elif key in (KEY_QUIT, KEY_ESCAPE):
                raise UserAbort("선택이 취소되었습니다.")

            _erase(total_lines)
    finally:
        _write(_SHOW_CURSOR)


def _select_numbered(
    title: str, choices: Sequence[Choice], default: int
) -> str:
    """
    번호 입력 폴백.

    tty 가 아닌 환경에서 쓴다. 빈 입력은 기본값을 택한다. EOF(파이프
    종료)는 기본값으로 처리해 자동화에서 멈추지 않게 한다.
    """
    print(f"\n{title}")
    for index, choice in enumerate(choices, start=1):
        mark = "*" if index - 1 == default else " "
        print(f" {mark}{index:>2}. {choice.render()}")

    while True:
        try:
            raw = input(f"번호 입력 [{default + 1}]: ").strip()
        except EOFError:
            print(f"(기본값 {choices[default].label})")
            return choices[default].value
        except KeyboardInterrupt:
            raise UserAbort("선택이 취소되었습니다.") from None

        if not raw:
            return choices[default].value
        if raw.lower() in ("q", "quit", "exit"):
            raise UserAbort("선택이 취소되었습니다.")
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1].value
        print(f"1~{len(choices)} 사이의 번호를 입력하세요.")


def ask_text(
    prompt: str,
    default: str = "",
    validate: Callable[[str], str] | None = None,
) -> str:
    """
    자유 입력을 받는다.

    Args:
        prompt: 표시할 질문.
        default: 빈 입력 시 사용할 값.
        validate: 검증 함수. 예외를 던지면 재입력을 요구한다.

    Raises:
        UserAbort: 사용자가 취소했을 때.
    """
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            if default:
                print(f"(기본값 {default})")
                return default
            raise UserAbort("입력이 중단되었습니다.") from None
        except KeyboardInterrupt:
            raise UserAbort("입력이 취소되었습니다.") from None

        value = raw or default
        if not value:
            print("값을 입력하세요.")
            continue
        if value.lower() in ("q", "quit", "exit"):
            raise UserAbort("입력이 취소되었습니다.")

        if validate is None:
            return value
        try:
            return validate(value)
        except Exception as exc:  # noqa: BLE001 — 검증 실패 메시지를 보여주고 재입력
            print(f"  {exc}")


def confirm(prompt: str, default: bool = True) -> bool:
    """예/아니오를 묻는다."""
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{prompt} [{hint}]: ").strip().lower()
        except EOFError:
            return default
        except KeyboardInterrupt:
            raise UserAbort("입력이 취소되었습니다.") from None

        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("y 또는 n 을 입력하세요.")


def banner(text: str) -> None:
    """구획 제목을 출력한다."""
    print(f"\n{config.SEPARATOR}")
    print(f"  {text}")
    print(config.SEPARATOR)

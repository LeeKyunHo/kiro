"""
렌더링 전략과 배치 실행기.

**Strategy 패턴 적용 범위**

`--test` 와 `--from_image` 는 이미지를 만들지 않는다. 이것들을
RenderStrategy 로 취급하면 "렌더링하지 않는 렌더러" 라는 모순된 구현이
생기고 인터페이스가 오염된다. 따라서 두 층으로 나눈다.

    Command  (commands.py)  생성 / 역추출 / 진단  ← 서로 다른 작업
    Strategy (이 모듈)      API / mock / 계획만   ← 같은 작업의 다른 방식

`typing.Protocol` 을 쓰는 이유는 구조적 서브타이핑이다. 구현체가 특정
기반 클래스를 상속하도록 강요하지 않으므로 결합도가 낮고, 테스트용
가짜 전략을 만들 때 import 가 필요 없다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import payload as payload_module
from .api import WebUiClient
from .codes import CodeFormatter
from .errors import ApiError, ApiUnavailableError, StorageError
from .logging_setup import get_logger
from .mock_image import render_mock_png
from .models import (
    BatchResult,
    Failure,
    PoseDatabase,
    PoseEntry,
    ReferenceContext,
)
from .prompt import PromptComposer
from .storage import AssetPaths, AtomicImageWriter, write_mock_manifest

_logger = get_logger("render")


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """전략에 넘기는 단일 코드 렌더링 요청."""

    entry: PoseEntry
    prefix: str
    formatter: CodeFormatter
    destination: Path
    composer: PromptComposer
    reference: ReferenceContext
    sampler: str

    @property
    def code(self) -> int:
        return self.entry.code

    @property
    def tag(self) -> str:
        return self.formatter.tag(self.entry.code)


@runtime_checkable
class RenderStrategy(Protocol):
    """
    단일 코드를 처리하는 방식.

    `produces_files` 는 호출부가 폴더 생성·탐색기 오픈 여부를 판단하는 데
    쓴다. 전략이 자기 성질을 스스로 알려주므로 호출부에 `if mode == ...`
    분기가 생기지 않는다.
    """

    name: str
    badge: str
    produces_files: bool
    plan_only: bool

    def render(self, request: RenderRequest) -> None:
        """
        Raises:
            ApiUnavailableError: 배치를 중단해야 하는 실패.
            ApiError / StorageError: 이 코드만 실패. 다음으로 진행 가능.
        """
        ...


class ApiRenderStrategy:
    """WebUI API 를 호출해 실제 이미지를 생성한다."""

    name = "api"
    badge = ""
    produces_files = True
    plan_only = False

    def __init__(self, client: WebUiClient, writer: AtomicImageWriter) -> None:
        self._client = client
        self._writer = writer

    def render(self, request: RenderRequest) -> None:
        pair = request.composer.compose(
            request.entry, request.prefix, request.formatter
        )
        body = payload_module.build_generation_payload(
            positive=pair.positive,
            negative=pair.negative,
            sampler=request.sampler,
            reference=request.reference,
        )
        png_bytes = self._client.generate(body)
        self._writer.write(png_bytes, request.destination)


class MockRenderStrategy:
    """
    API 없이 더미 이미지를 실제로 저장한다.

    페이로드 조립도 수행하고 전송만 생략한다. 조립 오류는 mock 에서
    잡아야 할 결함이므로 이 단계를 건너뛰면 검증 범위가 줄어든다.

    출력 경로는 `AssetPaths(is_mock=True)` 가 `mock_assets/` 로 격리한다.
    """

    name = "mock"
    badge = " [MOCK]"
    produces_files = True
    plan_only = False

    def __init__(self, writer: AtomicImageWriter) -> None:
        self._writer = writer
        self.written: list[str] = []

    def render(self, request: RenderRequest) -> None:
        pair = request.composer.compose(
            request.entry, request.prefix, request.formatter
        )
        # 전송하지 않지만 조립은 실제 경로와 동일하게 수행한다.
        payload_module.build_generation_payload(
            positive=pair.positive,
            negative=pair.negative,
            sampler=request.sampler,
            reference=request.reference,
        )
        png_bytes = render_mock_png(
            prefix=request.prefix,
            code_tag=request.tag,
            entry=request.entry,
            reference=request.reference.image,
        )
        self._writer.write(png_bytes, request.destination)
        self.written.append(request.destination.name)


class PlanOnlyStrategy:
    """
    파일을 쓰지 않고 계획만 기록한다 (`--dry-run`).

    파일 I/O 를 하지 않는 것이 이 모드의 계약이다. 참조 이미지의 존재
    여부와 경로는 호출부가 출력하지만 내용을 읽지 않는다.
    """

    name = "plan"
    badge = " [DRY-RUN]"
    produces_files = False
    plan_only = True

    def render(self, request: RenderRequest) -> None:
        # 의도적으로 아무것도 하지 않는다. 기록은 BatchRunner 가 담당한다.
        return None


class BatchRunner:
    """
    코드 목록을 순회하며 전략에 위임한다.

    전략이 무엇이든 순회·스킵 판정·집계·측정은 동일하므로 여기에 한 번만
    구현한다. 모드별로 루프를 복제하면 스킵 로직이 갈라진다.
    """

    def __init__(self, strategy: RenderStrategy) -> None:
        self._strategy = strategy

    def run(
        self,
        *,
        codes: tuple[int, ...],
        database: PoseDatabase,
        prefix: str,
        formatter: CodeFormatter,
        composer: PromptComposer,
        reference: ReferenceContext,
        sampler: str,
        paths: AssetPaths,
    ) -> BatchResult:
        result = BatchResult(plan_only=self._strategy.plan_only)

        for code in codes:
            entry = database.entry(code)
            tag = formatter.tag(code)
            filename = formatter.filename(prefix, code)
            destination = paths.file(filename)

            # plan-only 검사를 존재 확인보다 **먼저** 둔다.
            # 순서가 뒤바뀌면 이미 생성된 파일이 skipped 로 빠져
            # planned 가 불완전해지고 마크다운이 누락된다.
            if self._strategy.plan_only:
                result.planned.append(code)
                _logger.info("  [%s] (계획) %s  <- %s", tag, filename, entry.label)
                continue

            if destination.exists():
                _logger.info("  [%s] 이미 존재 (건너뜀) -> %s", tag, filename)
                result.skipped.append(code)
                continue

            request = RenderRequest(
                entry=entry,
                prefix=prefix,
                formatter=formatter,
                destination=destination,
                composer=composer,
                reference=reference,
                sampler=sampler,
            )

            label = "모의 생성" if self._strategy.name == "mock" else "생성"
            started = time.perf_counter()
            try:
                self._strategy.render(request)
            except ApiUnavailableError as exc:
                # WebUI 가 죽은 상태에서 남은 코드를 계속 시도하면 대기만
                # 누적된다. 즉시 중단한다.
                _logger.error("  [%s] %s 실패: %s", tag, label, exc)
                result.failures.append(Failure(code, "connection"))
                result.aborted = True
                break
            except (ApiError, StorageError) as exc:
                _logger.warning("  [%s] %s 실패: %s", tag, label, exc)
                result.failures.append(Failure(code, str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 — 개별 실패가 배치를 막지 않는다
                _logger.warning("  [%s] %s 실패(예상치 못한 오류): %s", tag, label, exc)
                result.failures.append(Failure(code, repr(exc)))
                continue

            elapsed = time.perf_counter() - started
            result.durations.append(elapsed)
            _logger.info("  [%s] 완료 (%.1f초) -> %s", tag, elapsed, filename)
            result.succeeded.append(code)

        self._finalize(paths)
        return result

    def _finalize(self, paths: AssetPaths) -> None:
        """mock 산출물이면 매니페스트를 남긴다."""
        strategy = self._strategy
        if isinstance(strategy, MockRenderStrategy) and strategy.written:
            write_mock_manifest(paths, strategy.written)

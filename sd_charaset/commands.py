"""
Command 계층.

**Command 와 Strategy 를 나눈 이유**

    Command   생성 / 역추출 / 진단  ← 서로 다른 작업
    Strategy  API / mock / 계획만   ← 같은 작업의 다른 방식

`--test` 와 `--from_image` 는 이미지를 만들지 않는다. 이것들을
RenderStrategy 로 취급하면 "렌더링하지 않는 렌더러" 라는 모순된 구현이
생기고 인터페이스가 오염된다.

각 Command 는 `run()` 에서 종료 코드를 반환한다. 예외를 삼키지 않고
`CharasetError` 를 그대로 올려 `cli` 가 한 곳에서 종료 코드로 변환한다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import config, output
from .api import WebUiClient
from .codes import CodeFormatter, resolve_codes
from .database import (
    load_pose_database,
    log_warnings,
    resolve_profile,
)
from .diagnostics import run_diagnostics
from .errors import ApiError, ConfigError, ValidationError
from .logging_setup import get_logger
from .models import InterrogateResult, ReferenceContext
from .prompt import PromptComposer
from .render import (
    ApiRenderStrategy,
    BatchRunner,
    MockRenderStrategy,
    PlanOnlyStrategy,
    RenderStrategy,
)
from .storage import (
    AssetPaths,
    AtomicImageWriter,
    find_reference_candidates,
    guard_real_output,
    load_reference,
    open_in_file_manager,
    resolve_reference_image,
)
from .tags import partition_gender_tags
from .validators import (
    audit_prompt_conflicts,
    validate_interrogator,
    validate_prefix,
    validate_ref_weight,
)

_logger = get_logger("commands")


class Command(Protocol):
    """실행 가능한 작업."""

    def run(self) -> int:
        """Returns: 종료 코드."""
        ...


# ─────────────────────────────────────────────
# 진단
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DiagnoseCommand:
    """`--test`. 데이터·로직 자체 진단."""

    base_dir: Path

    def run(self) -> int:
        return run_diagnostics(self.base_dir)


# ─────────────────────────────────────────────
# 태그 역추출
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class InterrogateCommand:
    """
    `--from_image`. 이미지에서 태그를 역추출한다.

    `--prefix` / `--char_prompt` 를 요구하지 않는다. 태그를 얻는 것이
    목적이며 생성을 하지 않는다.
    """

    base_dir: Path
    image_path: str
    model: str
    program: str

    def run(self) -> int:
        model = validate_interrogator(self.model)
        path = Path(self.image_path).expanduser()
        if not path.is_absolute():
            path = (self.base_dir / path).resolve()
        if path.is_dir():
            raise ConfigError(
                f"--from_image 에 디렉터리가 지정되었습니다: {path}",
                "이미지 파일 경로를 지정하세요.",
            )
        if not path.is_file():
            raise ConfigError(f"이미지를 찾을 수 없습니다: {path}")

        reference = load_reference(path)

        # 헤더를 API 호출 **전에** 출력한다. 호출이 실패해도 무엇을 어떤
        # 모델로 시도했는지 남아야 원인을 판단할 수 있다.
        output.emit_interrogate_header(
            source_label=reference.label, model=model
        )

        with WebUiClient() as client:
            try:
                raw = client.interrogate(reference.b64, model)
            except ApiError as exc:
                _logger.error("%s", exc.message)
                if exc.hint:
                    _logger.error("%s", exc.hint)
                if model == config.INTERROGATOR_DEFAULT:
                    _logger.error(
                        "DeepBooru 모델이 없으면 --interrogator clip 을 시도하세요."
                    )
                return 1

        tags = tuple(part.strip() for part in raw.split(",") if part.strip())
        _kept, removed = partition_gender_tags(tags)
        result = InterrogateResult(raw=raw, tags=tags, gender_tags=removed)

        output.emit_interrogate_report(
            raw=result.raw,
            tags=result.tags,
            removed=result.gender_tags,
            filtered=result.filtered,
            program=self.program,
        )
        return 0


# ─────────────────────────────────────────────
# 생성
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GenerateCommand:
    """
    기본 / `--mock` / `--dry-run`. 에셋 배치 생성.

    세 모드는 `RenderStrategy` 로 갈라지고, 그 외 흐름(코드 선택, 프롬프트
    조립, 집계, 출력)은 공유한다.
    """

    base_dir: Path
    args: argparse.Namespace

    # ── 모드 결정 ────────────────────────────────
    @property
    def plan_only(self) -> bool:
        return bool(self.args.dry_run)

    @property
    def use_mock(self) -> bool:
        # dry-run 이 우선한다. 부작용이 적은 쪽을 택한다.
        return bool(self.args.mock) and not self.plan_only

    def run(self) -> int:
        args = self.args

        if args.mock and args.dry_run:
            _logger.warning("--dry-run 이 우선합니다. --mock 무시됨")

        prefix = validate_prefix(args.prefix)
        char_prompt = (args.char_prompt or "").strip()
        ref_weight = validate_ref_weight(args.ref_weight)

        database = load_pose_database(self.base_dir)
        log_warnings(database)

        profile = resolve_profile(database, args.profile)
        if args.profile:
            _logger.info("[PROFILE] '%s' 적용", profile.name)
        else:
            _logger.info("[PROFILE] 미지정 - 기본값 '%s' 적용", profile.name)

        composer = PromptComposer(
            profile=profile,
            char_prompt=char_prompt,
            custom_negative=(args.custom_neg or "").strip(),
        )

        for message in audit_prompt_conflicts(
            composer.positive_preview, composer.negative, database.exclusive_groups
        ):
            _logger.warning(message)

        selection = resolve_codes(
            available=dict(database.entries),
            section_map=dict(database.sections),
            mode=args.mode,
            explicit_expression=args.codes,
            on_warning=_logger.warning,
        )
        if not selection:
            raise ValidationError(
                "생성 대상 코드가 없습니다.", "--mode / --codes 값을 확인하세요."
            )

        # 폭은 전체 DB 기준으로 계산한다. 선택된 코드만 기준으로 하면
        # --mode 0,1 실행과 --mode all 실행이 다른 파일명을 만들어
        # 스킵 판정이 깨진다.
        formatter = CodeFormatter.for_codes(database.all_codes)

        paths = AssetPaths(self.base_dir, prefix, is_mock=self.use_mock)
        if not self.plan_only:
            if not self.use_mock:
                guard_real_output(paths)
            paths.ensure()
            AtomicImageWriter.cleanup_partials(paths.output_dir)

        reference = self._resolve_reference(prefix, ref_weight)

        with WebUiClient() as client:
            sampler = (
                config.SAMPLER_PLACEHOLDER
                if (self.use_mock or self.plan_only)
                else client.resolve_sampler()
            )
            reference = self._attach_controlnet(client, reference)
            strategy = self._build_strategy(client)

            output.log_batch_header(
                prefix=prefix,
                profile_name=profile.name,
                mode=args.mode if not args.codes else f"codes:{args.codes}",
                count=len(selection),
                formatter=formatter,
                output_dir=paths.output_dir,
                positive=profile.base_positive,
                negative=composer.negative,
                badge=strategy.badge,
            )

            result = BatchRunner(strategy).run(
                codes=selection.codes,
                database=database,
                prefix=prefix,
                formatter=formatter,
                composer=composer,
                reference=reference,
                sampler=sampler,
                paths=paths,
            )

            vram = (
                None
                if (self.use_mock or self.plan_only)
                else client.vram_snapshot()
            )

        output.log_summary(result, paths.output_dir, strategy.badge, vram)

        deliverables = result.deliverable_codes
        if not deliverables:
            _logger.info("생성된 파일이 없어 마크다운을 출력하지 않습니다.")
            return 1 if result.failures else 0

        if strategy.produces_files:
            open_in_file_manager(paths.output_dir)

        output.emit_genit_block(
            prefix=prefix,
            codes=deliverables,
            database=database,
            formatter=formatter,
            badge=strategy.badge,
        )
        return 1 if result.aborted else 0

    # ── 내부 단계 ────────────────────────────────
    def _resolve_reference(self, prefix: str, weight: float) -> ReferenceContext:
        """
        참조 이미지를 해석한다.

        dry-run 은 "무엇을 할 계획인가" 만 보여주는 모드라 파일 내용을
        읽지 않는다. 존재 여부와 경로만 확인한다.
        """
        args = self.args

        if self.plan_only:
            if args.no_ref:
                _logger.info("[REF]  --no_ref 지정 - 참조 이미지 사용 안 함")
            elif args.ref_image:
                # 존재 확인은 파일 내용을 읽지 않으므로 dry-run 계약을 깨지
                # 않는다. 사전 점검 모드가 잘못된 경로를 통과시키면 "괜찮다"
                # 고 한 뒤 실제 실행에서 실패해 점검의 의미가 없어진다.
                probe = Path(args.ref_image).expanduser()
                if not probe.is_absolute():
                    probe = (self.base_dir / probe).resolve()
                if probe.is_dir():
                    raise ConfigError(
                        f"--ref_image 에 디렉터리가 지정되었습니다: {probe}",
                        "이미지 파일 경로를 지정하세요.",
                    )
                if not probe.is_file():
                    raise ConfigError(
                        f"--ref_image 경로를 찾을 수 없습니다: {probe}",
                        f"자동 탐색을 쓰려면 {config.REFERENCES_DIRNAME}/{prefix}.png "
                        "로 두고 --ref_image 를 생략하세요.",
                    )
                _logger.info("[REF]  %s (지정) weight %s", probe.name, weight)
            elif found := find_reference_candidates(self.base_dir, prefix):
                _logger.info("[REF]  %s 발견 weight %s", found[0].name, weight)
            else:
                _logger.info(
                    "[REF]  없음 (%s/%s.*) - 텍스트만 사용",
                    config.REFERENCES_DIRNAME,
                    prefix,
                )
            return ReferenceContext(weight=weight)

        image = resolve_reference_image(
            self.base_dir, prefix, args.ref_image, disabled=args.no_ref
        )
        if image is None and not args.no_ref:
            _logger.warning(
                "참조 이미지 없음 (%s/%s.*) - 텍스트 프롬프트만 사용",
                config.REFERENCES_DIRNAME,
                prefix,
            )
        return ReferenceContext(image=image, weight=weight)

    def _attach_controlnet(
        self, client: WebUiClient, reference: ReferenceContext
    ) -> ReferenceContext:
        """
        ControlNet spec 을 해석해 붙인다.

        mock 에서는 HTTP 조회를 하지 않는다. 대신 `--cn_module` 과
        `--cn_model` 을 둘 다 주면 주입 경로를 GPU 없이 검증할 수 있다.
        """
        if reference.image is None:
            return reference

        args = self.args
        if self.use_mock:
            spec = (
                None
                if not (args.cn_module and args.cn_model)
                else client.resolve_controlnet(args.cn_module, args.cn_model)
            )
        else:
            spec = client.resolve_controlnet(args.cn_module, args.cn_model)

        attached = ReferenceContext(
            image=reference.image, spec=spec, weight=reference.weight
        )
        if attached.active:
            assert attached.spec is not None
            _logger.info(
                "[REF]  %s weight %s", reference.image.label, reference.weight
            )
            _logger.info("[CN]   %s", attached.spec.label)
        else:
            _logger.info(
                "[REF]  %s - ControlNet 미해석, 텍스트만 사용",
                reference.image.label,
            )
        return attached

    def _build_strategy(self, client: WebUiClient) -> RenderStrategy:
        if self.plan_only:
            return PlanOnlyStrategy()
        writer = AtomicImageWriter()
        if self.use_mock:
            return MockRenderStrategy(writer)
        return ApiRenderStrategy(client, writer)

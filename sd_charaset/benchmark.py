"""
IP-Adapter 가중치 벤치마크 및 정적 HTML 뷰어 생성.

**목적**

`--ref_weight` 를 바꿔가며 같은 코드를 생성해 비교한다. 가중치를 올리면
캐릭터 일관성은 오르지만 참조 이미지의 포즈까지 전이되어 JSON 포즈 지시를
무시하기 시작한다. 그 임계점 직전이 최적값이며, 이미지를 나란히 봐야
판단할 수 있다.

**설계 결정 3가지**

1. **서브프로세스가 아니라 패키지를 직접 쓴다.**
   CLI 를 셸로 호출하면 인자 파싱이 중복되고 예외가 문자열로 뭉개진다.
   `BatchRunner` 와 전략을 그대로 재사용한다.

2. **접두어를 바꾸지 않는다.**
   가중치별 구분을 접두어(`bench_w05` 등)로 하면 트리거 태그가 달라져
   프롬프트 자체가 변한다. 비교의 전제가 깨진다.
   `AssetPaths.variant` 로 하위 폴더만 나눈다.

3. **`--mock` 을 지원한다.**
   GPU 없는 환경에서 HTML 생성과 매트릭스 조립 경로를 검증할 수 있다.
   더미 이미지라 비교 자체는 의미 없지만, 파이프라인은 완전히 동일하다.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .api import WebUiClient
from .codes import CodeFormatter, resolve_codes
from .database import load_pose_database, log_warnings, resolve_profile
from .errors import ValidationError
from .logging_setup import emit, get_logger
from .models import (
    BatchResult,
    PoseDatabase,
    ReferenceContext,
    TimingStats,
    VramSnapshot,
)
from .prompt import PromptComposer
from .render import (
    ApiRenderStrategy,
    BatchRunner,
    MockRenderStrategy,
    RenderStrategy,
)
from .storage import (
    AssetPaths,
    AtomicImageWriter,
    OutputKind,
    resolve_reference_image,
)
from .validators import validate_prefix, validate_ref_weight

_logger = get_logger("benchmark")


# ─────────────────────────────────────────────
# 결과 모델
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class WeightRun:
    """단일 가중치에 대한 실행 결과."""

    weight: float
    variant: str
    output_dir: Path
    succeeded: tuple[int, ...]
    skipped: tuple[int, ...]
    failed: tuple[int, ...]
    timing: TimingStats | None
    vram: VramSnapshot | None

    @property
    def available(self) -> tuple[int, ...]:
        """디스크에 파일이 있는 코드."""
        return tuple(sorted(self.succeeded + self.skipped))

    @property
    def label(self) -> str:
        return f"{self.weight:.2f}"

    def to_dict(self) -> dict[str, object]:
        return {
            "weight": self.weight,
            "variant": self.variant,
            "output_dir": str(self.output_dir),
            "succeeded": list(self.succeeded),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "timing": (
                {
                    "count": self.timing.count,
                    "total": round(self.timing.total, 2),
                    "average": round(self.timing.average, 2),
                    "fastest": round(self.timing.fastest, 2),
                    "slowest": round(self.timing.slowest, 2),
                }
                if self.timing
                else None
            ),
            "vram": (
                {
                    "peak_gib": round(self.vram.peak, 2),
                    "total_gib": round(self.vram.total, 2),
                    "ratio_percent": round(self.vram.ratio, 1),
                }
                if self.vram
                else None
            ),
        }


@dataclass(slots=True)
class BenchmarkReport:
    """벤치마크 전체 결과."""

    prefix: str
    profile_name: str
    char_prompt: str
    reference_label: str
    codes: tuple[int, ...]
    formatter: CodeFormatter
    runs: list[WeightRun] = field(default_factory=list)
    mock: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @property
    def weights(self) -> tuple[float, ...]:
        return tuple(run.weight for run in self.runs)

    def to_dict(self) -> dict[str, object]:
        return {
            "prefix": self.prefix,
            "profile": self.profile_name,
            "char_prompt": self.char_prompt,
            "reference": self.reference_label,
            "codes": list(self.codes),
            "code_width": self.formatter.width,
            "mock": self.mock,
            "created_at": self.created_at,
            "runs": [run.to_dict() for run in self.runs],
        }


# ─────────────────────────────────────────────
# 가중치 파싱
# ─────────────────────────────────────────────
def parse_weights(expression: str | None) -> tuple[float, ...]:
    """
    `--bench_weights` 값을 가중치 튜플로 변환한다.

    형식: "0.3,0.5,0.7,0.9". 중복은 제거하고 오름차순 정렬한다.

    Raises:
        ValidationError: 실수로 해석할 수 없거나 허용 범위를 벗어날 때.
    """
    if not expression:
        return config.BENCHMARK_WEIGHTS_DEFAULT

    values: set[float] = set()
    for raw in expression.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            weight = float(token)
        except ValueError:
            raise ValidationError(
                f"가중치를 해석할 수 없습니다: '{token}'",
                "예: --bench_weights 0.3,0.5,0.7,0.9",
            ) from None
        values.add(validate_ref_weight(weight))

    if not values:
        raise ValidationError(
            "비교할 가중치가 없습니다.", "예: --bench_weights 0.5,0.7"
        )
    return tuple(sorted(values))


def variant_name(weight: float) -> str:
    """가중치를 폴더명으로 변환한다."""
    return config.BENCHMARK_VARIANT_TEMPLATE.format(weight=weight)


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
def _build_strategy(
    client: WebUiClient, writer: AtomicImageWriter, mock: bool
) -> RenderStrategy:
    return MockRenderStrategy(writer) if mock else ApiRenderStrategy(client, writer)


def _summarize(result: BatchResult) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    return (
        tuple(sorted(result.succeeded)),
        tuple(sorted(result.skipped)),
        result.failed_codes,
    )


def run_benchmark(
    *,
    base_dir: Path,
    prefix: str,
    char_prompt: str,
    profile_name: str | None,
    custom_negative: str,
    mode: str,
    codes_expression: str | None,
    weights: Sequence[float],
    reference_path: str | None,
    cn_module: str | None,
    cn_model: str | None,
    mock: bool,
) -> BenchmarkReport:
    """
    가중치를 순회하며 배치를 실행하고 결과를 모은다.

    Raises:
        ValidationError: 참조 이미지가 없을 때. 가중치 비교의 전제가 없다.
    """
    safe_prefix = validate_prefix(prefix)
    database = load_pose_database(base_dir)
    log_warnings(database)

    profile = resolve_profile(database, profile_name)
    composer = PromptComposer(
        profile=profile,
        char_prompt=char_prompt.strip(),
        custom_negative=custom_negative.strip(),
    )

    selection = resolve_codes(
        available=dict(database.entries),
        section_map=dict(database.sections),
        mode=mode,
        explicit_expression=codes_expression,
        on_warning=_logger.warning,
    )
    if not selection:
        raise ValidationError(
            "비교할 대상 코드가 없습니다.", "--mode / --codes 값을 확인하세요."
        )

    formatter = CodeFormatter.for_codes(database.all_codes)

    image = resolve_reference_image(base_dir, safe_prefix, reference_path)
    if image is None:
        raise ValidationError(
            "참조 이미지가 없어 가중치 비교를 할 수 없습니다.",
            f"{config.REFERENCES_DIRNAME}/{safe_prefix}.png 를 두거나 "
            "--ref_image 로 지정하세요.",
        )

    report = BenchmarkReport(
        prefix=safe_prefix,
        profile_name=profile.name,
        char_prompt=composer.char_prompt,
        reference_label=image.label,
        codes=selection.codes,
        formatter=formatter,
        mock=mock,
    )

    emit(f"\n{config.SEPARATOR}")
    emit(f"  가중치 벤치마크 | {safe_prefix} | {len(selection)}장 x {len(weights)}종")
    emit(config.SEPARATOR)

    with WebUiClient() as client:
        sampler = config.SAMPLER_PLACEHOLDER if mock else client.resolve_sampler()

        # ControlNet 해석은 배치 전체에서 한 번만 한다.
        # mock 에서는 HTTP 를 쓰지 않으므로 수동 지정이 있을 때만 해석한다.
        if mock:
            spec = (
                client.resolve_controlnet(cn_module, cn_model)
                if (cn_module and cn_model)
                else None
            )
        else:
            spec = client.resolve_controlnet(cn_module, cn_model)

        if spec is None:
            _logger.warning(
                "ControlNet 미해석 - 가중치가 결과에 반영되지 않습니다. "
                "비교 의미가 없으므로 --cn_module / --cn_model 확인이 필요합니다."
            )
        else:
            _logger.info("[CN]   %s", spec.label)

        writer = AtomicImageWriter()

        for weight in weights:
            variant = variant_name(weight)
            paths = AssetPaths(
                base_dir, safe_prefix, kind=OutputKind.BENCHMARK, variant=variant
            )
            paths.ensure()
            AtomicImageWriter.cleanup_partials(paths.output_dir)

            reference = ReferenceContext(image=image, spec=spec, weight=weight)
            strategy = _build_strategy(client, writer, mock)

            _logger.info(
                "\n[가중치 %s] %s (%d장)", variant, paths.output_dir, len(selection)
            )

            result = BatchRunner(strategy).run(
                codes=selection.codes,
                database=database,
                prefix=safe_prefix,
                formatter=formatter,
                composer=composer,
                reference=reference,
                sampler=sampler,
                paths=paths,
            )

            succeeded, skipped, failed = _summarize(result)
            vram = None if mock else client.vram_snapshot()

            report.runs.append(
                WeightRun(
                    weight=weight,
                    variant=variant,
                    output_dir=paths.output_dir,
                    succeeded=succeeded,
                    skipped=skipped,
                    failed=failed,
                    timing=result.timing,
                    vram=vram,
                )
            )

            if timing := result.timing:
                _logger.info("[측정] %s", timing.format())
            if vram is not None:
                _logger.info("[VRAM] %s", vram.format())

            if result.aborted:
                _logger.error("연결이 끊겨 남은 가중치를 건너뜁니다.")
                break

    return report


# ─────────────────────────────────────────────
# HTML 뷰어
# ─────────────────────────────────────────────
_VIEWER_CSS = """\
:root {
  color-scheme: light dark;
  --bg: #14161a;
  --panel: #1d2027;
  --line: #2c313b;
  --text: #e6e8ec;
  --muted: #9aa1ad;
  --accent: #6ea8fe;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 "Segoe UI", "Malgun Gothic", system-ui, sans-serif;
}
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 32px 0 12px; color: var(--muted); font-weight: 600; }
.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px 24px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 8px;
}
.meta div { display: flex; gap: 8px; }
.meta dt { color: var(--muted); min-width: 92px; }
.meta dd { margin: 0; word-break: break-all; }
.note {
  border-left: 3px solid var(--accent);
  background: var(--panel);
  padding: 12px 16px;
  margin: 16px 0;
  border-radius: 0 6px 6px 0;
  color: var(--muted);
}
table { border-collapse: collapse; width: 100%; }
th, td {
  border: 1px solid var(--line);
  padding: 8px;
  text-align: center;
  vertical-align: top;
}
th { background: var(--panel); position: sticky; top: 0; z-index: 1; }
th.code-col, td.code-col {
  text-align: left;
  background: var(--panel);
  position: sticky;
  left: 0;
  min-width: 190px;
  z-index: 2;
}
td img {
  width: 100%;
  max-width: 208px;
  height: auto;
  display: block;
  margin: 0 auto;
  border-radius: 4px;
  background: #0d0f12;
  cursor: zoom-in;
}
.missing { color: #c96b6b; font-size: 12px; }
.code-label { font-weight: 600; }
.code-sub { color: var(--muted); font-size: 12px; }
.stats td, .stats th { text-align: right; }
.stats td:first-child, .stats th:first-child { text-align: left; }
.wrap { overflow: auto; max-height: 82vh; border: 1px solid var(--line); border-radius: 8px; }
dialog {
  border: none;
  background: transparent;
  padding: 0;
  max-width: 96vw;
  max-height: 96vh;
}
dialog::backdrop { background: rgba(0, 0, 0, 0.86); }
dialog img { max-width: 96vw; max-height: 92vh; border-radius: 6px; }
dialog p { color: #e6e8ec; text-align: center; margin: 8px 0 0; font-size: 13px; }
footer { margin-top: 28px; color: var(--muted); font-size: 12px; }
"""

_VIEWER_JS = """\
(function () {
  var dialog = document.getElementById('zoom');
  var target = document.getElementById('zoom-image');
  var caption = document.getElementById('zoom-caption');
  if (!dialog || !target) { return; }

  document.querySelectorAll('img[data-zoom]').forEach(function (node) {
    node.addEventListener('click', function () {
      target.src = node.getAttribute('src');
      caption.textContent = node.getAttribute('alt') || '';
      if (typeof dialog.showModal === 'function') { dialog.showModal(); }
    });
  });

  dialog.addEventListener('click', function () { dialog.close(); });
})();
"""


def _esc(value: object) -> str:
    """HTML 이스케이프. 사용자 입력이 마크업으로 해석되는 것을 막는다."""
    return html.escape(str(value), quote=True)


def _relative(path: Path, start: Path) -> str:
    """
    뷰어 기준 상대 경로를 슬래시 표기로 만든다.

    Windows 역슬래시는 HTML 에서 경로 구분자로 동작하지 않으므로 변환한다.
    """
    try:
        return path.relative_to(start).as_posix()
    except ValueError:
        return path.as_posix()


def _render_meta(report: BenchmarkReport) -> str:
    rows = [
        ("약칭", report.prefix),
        ("프로필", report.profile_name),
        ("외형 태그", report.char_prompt or "(없음)"),
        ("참조 이미지", report.reference_label),
        ("대상 코드", f"{len(report.codes)}종"),
        ("가중치", ", ".join(f"{w:.2f}" for w in report.weights) or "(없음)"),
        ("생성 시각", report.created_at),
        ("모드", "모의 생성(--mock)" if report.mock else "실제 렌더링"),
    ]
    items = "\n".join(
        f"    <div><dt>{_esc(key)}</dt><dd>{_esc(value)}</dd></div>"
        for key, value in rows
    )
    return f'  <dl class="meta">\n{items}\n  </dl>'


def _render_matrix(
    report: BenchmarkReport, database: PoseDatabase, viewer_dir: Path
) -> str:
    header = "".join(
        f'<th>weight {_esc(run.label)}</th>' for run in report.runs
    )
    body_rows: list[str] = []

    for code in report.codes:
        entry = database.entries.get(code)
        label = entry.label if entry else "(unknown)"
        section = entry.section if entry else "-"
        tag = report.formatter.tag(code)

        cells: list[str] = []
        for run in report.runs:
            filename = report.formatter.filename(report.prefix, code)
            image_path = run.output_dir / filename
            if image_path.is_file():
                src = _relative(image_path, viewer_dir)
                alt = f"{report.prefix}_{tag} @ weight {run.label}"
                cells.append(
                    f'<td><img data-zoom src="{_esc(src)}" alt="{_esc(alt)}" '
                    f'loading="lazy"></td>'
                )
            else:
                cells.append('<td><span class="missing">없음</span></td>')

        body_rows.append(
            "        <tr>"
            f'<td class="code-col">'
            f'<div class="code-label">{_esc(tag)} {_esc(label)}</div>'
            f'<div class="code-sub">{_esc(section)}</div>'
            "</td>"
            + "".join(cells)
            + "</tr>"
        )

    return (
        '  <div class="wrap">\n'
        "    <table>\n"
        f"      <thead><tr><th class=\"code-col\">코드</th>{header}</tr></thead>\n"
        "      <tbody>\n" + "\n".join(body_rows) + "\n      </tbody>\n"
        "    </table>\n"
        "  </div>"
    )


def _render_stats(report: BenchmarkReport) -> str:
    rows: list[str] = []
    for run in report.runs:
        timing = run.timing
        vram = run.vram
        rows.append(
            "        <tr>"
            f"<td>weight {_esc(run.label)}</td>"
            f"<td>{len(run.succeeded)}</td>"
            f"<td>{len(run.skipped)}</td>"
            f"<td>{len(run.failed)}</td>"
            f"<td>{f'{timing.average:.1f}s' if timing else '-'}</td>"
            f"<td>{f'{timing.total:.1f}s' if timing else '-'}</td>"
            f"<td>{f'{vram.peak:.2f} / {vram.total:.2f} GiB' if vram else '-'}</td>"
            "</tr>"
        )
    return (
        '  <table class="stats">\n'
        "    <thead><tr><th>가중치</th><th>성공</th><th>건너뜀</th><th>실패</th>"
        "<th>장당 평균</th><th>총 소요</th><th>VRAM 피크</th></tr></thead>\n"
        "    <tbody>\n" + "\n".join(rows) + "\n    </tbody>\n"
        "  </table>"
    )


def build_viewer_html(
    report: BenchmarkReport, database: PoseDatabase, viewer_dir: Path
) -> str:
    """
    비교 매트릭스 HTML 을 문자열로 조립한다.

    문자열을 반환하고 쓰기는 호출부가 하는 이유: 진단에서 파일 없이
    내용을 검사할 수 있다.

    이미지는 상대 경로로 참조한다. base64 로 내장하면 파일이 자기완결적이
    되지만 수십 장이면 수 MB 가 되고 브라우저 로딩이 느려진다. 뷰어를
    이미지 옆에 두는 것이 이 용도에는 맞다.
    """
    mock_note = (
        '  <p class="note"><strong>모의 생성 결과입니다.</strong> '
        "더미 이미지이므로 화질·일관성 비교에 쓸 수 없습니다. "
        "매트릭스 구성과 뷰어 동작을 확인하는 용도입니다.</p>"
        if report.mock
        else '  <p class="note">가중치를 올리면 캐릭터 일관성은 오르지만 '
        "참조 이미지의 포즈까지 전이되어 표정·포즈 지시가 무시되기 시작합니다. "
        "<strong>표정이 바뀌지 않기 시작하는 지점 직전</strong>이 최적값입니다. "
        "이미지를 클릭하면 확대됩니다.</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>가중치 벤치마크 — {_esc(report.prefix)}</title>
<style>
{_VIEWER_CSS}</style>
</head>
<body>
  <h1>IP-Adapter 가중치 벤치마크 — {_esc(report.prefix)}</h1>
{_render_meta(report)}
{mock_note}

  <h2>비교 매트릭스 (행: 코드 / 열: 가중치)</h2>
{_render_matrix(report, database, viewer_dir)}

  <h2>실행 통계</h2>
{_render_stats(report)}

  <dialog id="zoom">
    <img id="zoom-image" alt="">
    <p id="zoom-caption"></p>
  </dialog>

  <footer>
    sd_charaset benchmark · {_esc(report.created_at)}
  </footer>
<script>
{_VIEWER_JS}</script>
</body>
</html>
"""


def write_report(
    report: BenchmarkReport, database: PoseDatabase, base_dir: Path
) -> tuple[Path, Path]:
    """
    뷰어 HTML 과 JSON 매니페스트를 저장한다.

    Returns:
        (뷰어 경로, 매니페스트 경로)
    """
    prefix_dir = AssetPaths(
        base_dir, report.prefix, kind=OutputKind.BENCHMARK
    ).prefix_dir
    prefix_dir.mkdir(parents=True, exist_ok=True)

    viewer_path = prefix_dir / config.BENCHMARK_VIEWER_FILENAME
    manifest_path = prefix_dir / config.BENCHMARK_MANIFEST_FILENAME

    viewer_path.write_text(
        build_viewer_html(report, database, prefix_dir), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return viewer_path, manifest_path


def log_conclusion(report: BenchmarkReport, viewer_path: Path) -> None:
    """요약과 다음 행동을 안내한다."""
    _logger.info("\n%s", config.SEPARATOR)
    _logger.info("  벤치마크 완료 | %d종 가중치", len(report.runs))
    for run in report.runs:
        timing = f"{run.timing.average:.1f}s/장" if run.timing else "-"
        vram = f"{run.vram.peak:.2f}GiB" if run.vram else "-"
        _logger.info(
            "    weight %s  성공 %d  %s  %s",
            run.label,
            len(run.succeeded),
            timing,
            vram,
        )
    _logger.info("%s", config.SEPARATOR)

    emit(f"\n뷰어: {viewer_path}")
    emit("브라우저로 열어 가중치별 결과를 비교하세요.")
    if not report.mock:
        emit(
            "최적값을 정하면 config.py 의 REF_WEIGHT_DEFAULT 를 그 값으로 갱신합니다."
        )

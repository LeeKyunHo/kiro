"""
파일 입출력, 경로 정책, 참조 이미지 탐색.

세 가지 안전장치가 여기 모여 있다.

1. **원자적 쓰기** — `.part` 임시 파일에 쓴 뒤 `os.replace()` 로 교체.
   중단 시 반쪽 파일이 남으면 재개 로직이 그것을 완성품으로 보고 영구히
   건너뛴다. 재개 지원을 설계에 넣은 이상 실제 데이터 손실 경로다.

2. **mock 출력 격리** — mock 은 `mock_assets/` 에만 쓴다. 실제 렌더링의
   스킵 판정이 mock 파일을 볼 경로 자체가 없어진다.

3. **매니페스트 오염 감지** — 실제 출력 폴더에 mock 매니페스트가 있으면
   경고 후 중단한다. 사용자가 손으로 복사한 경우를 잡는다.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from PIL import Image

from . import config
from .errors import ConfigError, StorageError
from .logging_setup import get_logger
from .models import ReferenceImage

_logger = get_logger("storage")


# ─────────────────────────────────────────────
# 경로 정책
# ─────────────────────────────────────────────
class OutputKind(str, Enum):
    """
    산출물 종류.

    이전에는 `is_mock: bool` 이었다. 벤치마크가 추가되어 상태가 3개가 된
    시점부터 불리언 플래그는 코드 냄새다. 조건이 `if is_mock` 에서
    `if is_mock else if is_benchmark` 로 번져 호출부마다 분기가 늘어난다.

    `str` 을 함께 상속해 JSON 직렬화와 로그 출력이 그대로 된다.
    """

    REAL = "real"
    MOCK = "mock"
    BENCHMARK = "benchmark"

    @property
    def dirname(self) -> str:
        return {
            OutputKind.REAL: config.ASSETS_DIRNAME,
            OutputKind.MOCK: config.MOCK_ASSETS_DIRNAME,
            OutputKind.BENCHMARK: config.BENCHMARK_ASSETS_DIRNAME,
        }[self]

    @property
    def label(self) -> str:
        """사람이 읽는 산출물 설명. 카드 머리말과 로그에 쓴다."""
        return {
            OutputKind.REAL: "실제 렌더링",
            OutputKind.MOCK: "모의 생성 (--mock) — 더미 이미지입니다",
            OutputKind.BENCHMARK: "가중치 벤치마크",
        }[self]


@dataclass(frozen=True, slots=True)
class AssetPaths:
    """
    출력 경로를 한 곳에서 결정한다.

    종류별로 루트를 분리하는 것이 핵심이다. 같은 폴더를 쓰면
    `--mock --prefix mika` 로 검증한 뒤 실제 렌더링을 돌렸을 때 재개
    로직이 더미 파일을 완성품으로 보고 전부 건너뛴다. 최종 에셋이 더미
    이미지가 되는 사고가 난다.

    `variant` 는 같은 종류 안에서 다시 구획을 나눈다. 벤치마크가 가중치별
    결과를 분리할 때 쓴다. **접두어를 바꾸지 않는 것이 중요하다.**
    접두어는 트리거 태그로 프롬프트에 들어가므로, 접두어를 바꿔 구분하면
    프롬프트 자체가 달라져 비교가 무의미해진다.
    """

    base_dir: Path
    prefix: str
    kind: OutputKind = OutputKind.REAL
    variant: str | None = None

    @property
    def root(self) -> Path:
        return self.base_dir / self.kind.dirname

    @property
    def prefix_dir(self) -> Path:
        """variant 를 제외한 접두어 단위 폴더. 벤치마크 뷰어가 여기 놓인다."""
        return self.root / self.prefix

    @property
    def output_dir(self) -> Path:
        if self.variant:
            return self.prefix_dir / self.variant
        return self.prefix_dir

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / config.MOCK_MANIFEST_FILENAME

    @property
    def real_output_dir(self) -> Path:
        """종류와 무관한 실제 출력 경로. 오염 검사용."""
        return self.base_dir / config.ASSETS_DIRNAME / self.prefix

    @property
    def is_mock(self) -> bool:
        return self.kind is OutputKind.MOCK

    def file(self, filename: str) -> Path:
        return self.output_dir / filename

    def ensure(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def with_variant(self, variant: str | None) -> AssetPaths:
        """같은 설정에 구획만 바꾼 사본을 만든다."""
        return AssetPaths(self.base_dir, self.prefix, self.kind, variant)


def guard_real_output(paths: AssetPaths) -> None:
    """
    실제 출력 폴더에 mock 산출물이 섞였는지 검사한다.

    출력 루트를 분리했으므로 정상 경로에서는 발생하지 않는다. 사용자가
    mock 파일을 손으로 복사한 경우를 잡는 마지막 방어선이다.

    Raises:
        ConfigError: mock 매니페스트가 실제 출력 폴더에 존재.
    """
    marker = paths.real_output_dir / config.MOCK_MANIFEST_FILENAME
    if marker.is_file():
        raise ConfigError(
            f"실제 출력 폴더에 mock 산출물이 있습니다: {paths.real_output_dir}",
            f"{marker.name} 과 함께 있는 더미 이미지를 삭제한 뒤 다시 실행하세요.",
        )


def write_mock_manifest(paths: AssetPaths, filenames: list[str]) -> None:
    """
    mock 산출물 목록을 기록한다.

    나중에 이 폴더가 무엇인지 사람이 확인할 수 있고, 실제 출력 폴더로
    복사됐을 때 `guard_real_output` 이 감지할 수 있다.
    """
    if not filenames:
        return
    manifest = {
        "generated_by": "sd_charaset --mock",
        "warning": "더미 이미지입니다. 실제 에셋으로 사용하지 마세요.",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prefix": paths.prefix,
        "files": sorted(filenames),
    }
    try:
        paths.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _logger.warning("mock 매니페스트를 쓰지 못했습니다: %s", exc)


# ─────────────────────────────────────────────
# 원자적 이미지 쓰기
# ─────────────────────────────────────────────
class AtomicImageWriter:
    """
    PNG 바이트를 WebP 로 변환해 원자적으로 저장한다.

    변환 파라미터(RGBA/P -> RGB, quality, method)는 검증된 값이므로
    생성자 기본값으로 고정한다.

    쓰기는 `.part` 임시 파일을 경유한 뒤 `os.replace()` 로 교체한다.
    `os.replace()` 는 같은 볼륨 내에서 원자적이므로, 중간에 프로세스가
    죽어도 최종 경로에는 완전한 파일 또는 아무것도 없다.
    """

    def __init__(
        self,
        quality: int = config.WEBP_QUALITY,
        method: int = config.WEBP_METHOD,
    ) -> None:
        self.quality = quality
        self.method = method

    def write(self, png_bytes: bytes, destination: Path) -> None:
        """
        Raises:
            StorageError: 변환 또는 쓰기 실패.
        """
        partial = destination.with_name(destination.name + config.PARTIAL_SUFFIX)

        try:
            image = Image.open(io.BytesIO(png_bytes))
        except Exception as exc:  # noqa: BLE001 — Pillow 가 다양한 예외를 던진다
            raise StorageError(f"이미지를 열 수 없습니다: {exc}") from None

        # 팔레트/알파 이미지는 WebP 저장 시 경고가 나므로 RGB 로 맞춘다.
        if image.mode in ("RGBA", "P", "LA"):
            image = image.convert("RGB")

        try:
            image.save(
                partial, format="WEBP", quality=self.quality, method=self.method
            )
            # Path.replace() 는 os.replace() 와 동일하게 같은 볼륨 내에서
            # 원자적이다. 중간에 프로세스가 죽어도 최종 경로에는 완전한
            # 파일 또는 아무것도 없다.
            partial.replace(destination)
        except Exception as exc:  # noqa: BLE001
            partial.unlink(missing_ok=True)
            raise StorageError(f"저장 실패 ({destination.name}): {exc}") from None
        finally:
            image.close()

    @staticmethod
    def cleanup_partials(directory: Path) -> int:
        """
        남은 `.part` 파일을 정리한다.

        Returns:
            삭제한 파일 수.
        """
        if not directory.is_dir():
            return 0
        removed = 0
        for leftover in directory.glob(f"*{config.PARTIAL_SUFFIX}"):
            try:
                leftover.unlink()
                removed += 1
            except OSError:
                pass
        return removed


# ─────────────────────────────────────────────
# 참조 이미지 탐색
# ─────────────────────────────────────────────
def load_reference(path: Path) -> ReferenceImage:
    """
    참조 이미지를 읽어 검증하고 base64 인코딩한다.

    원본 바이트를 그대로 인코딩한다. Pillow 로 재인코딩하지 않는 이유는
    불필요한 손실과 시간이 발생하고, WebUI 가 PNG/JPEG/WebP 를 모두
    받기 때문이다.

    Raises:
        ConfigError: 읽을 수 없거나 유효한 이미지가 아닐 때.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"참조 이미지를 읽을 수 없습니다: {path}", str(exc)
        ) from None

    try:
        # verify() 이후에는 이미지 객체를 재사용할 수 없고 크기도 얻을 수
        # 없어 두 번 열어야 한다. Pillow 의 알려진 특성이다.
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"유효한 이미지 파일이 아닙니다: {path}", str(exc)
        ) from None

    return ReferenceImage(
        path=path,
        b64=base64.b64encode(data).decode("ascii"),
        width=width,
        height=height,
    )


def find_reference_candidates(base_dir: Path, prefix: str) -> tuple[Path, ...]:
    """
    `references/{prefix}.{ext}` 를 우선순위 순서로 찾는다.

    존재하는 것만 반환하므로 빈 튜플이면 참조 없음이다.
    """
    ref_dir = base_dir / config.REFERENCES_DIRNAME
    if not ref_dir.is_dir():
        return ()
    return tuple(
        candidate
        for ext in config.REFERENCE_EXTENSIONS
        if (candidate := ref_dir / f"{prefix}{ext}").is_file()
    )


def resolve_reference_image(
    base_dir: Path,
    prefix: str,
    explicit_path: str | None = None,
    disabled: bool = False,
) -> ReferenceImage | None:
    """
    참조 이미지를 해석한다. 없으면 None 을 반환한다.

    명시 지정과 자동 탐색의 실패 처리를 **다르게** 한다.

    `--ref_image` 를 적었다면 그 파일을 쓰겠다는 명확한 의사표시이므로
    조용히 무시하지 않는다. 자동 탐색은 "있으면 쓰고 없으면 넘어가는"
    편의 기능이며, 00번을 먼저 생성해 참조로 쓰는 부트스트랩
    워크플로우에서는 부재가 정상 상태다.

    Raises:
        ConfigError: `--ref_image` 로 명시한 경로가 없거나 유효하지 않을 때.
    """
    if disabled:
        return None

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if path.is_dir():
            raise ConfigError(
                f"--ref_image 에 디렉터리가 지정되었습니다: {path}",
                "이미지 파일 경로를 지정하세요.",
            )
        if not path.is_file():
            raise ConfigError(
                f"--ref_image 경로를 찾을 수 없습니다: {path}",
                f"자동 탐색을 쓰려면 {config.REFERENCES_DIRNAME}/{prefix}.png 로 "
                "두고 --ref_image 를 생략하세요.",
            )
        return load_reference(path)

    candidates = find_reference_candidates(base_dir, prefix)
    if not candidates:
        return None

    if len(candidates) > 1:
        ignored = [path.name for path in candidates[1:]]
        _logger.warning(
            "참조 이미지가 여러 개입니다. '%s' 사용, 무시됨: %s",
            candidates[0].name,
            ignored,
        )
    return load_reference(candidates[0])


# ─────────────────────────────────────────────
# 탐색기
# ─────────────────────────────────────────────
def open_in_file_manager(path: Path) -> None:
    """
    결과 폴더를 파일 관리자로 연다.

    `os.system()` 을 쓰지 않는다. 셸을 거치면 경로에 특수문자가 있을 때
    명령이 조립되어 임의 실행 위험이 있다. prefix 검증으로 이미 막고
    있지만, 두 번째 방어선을 둔다.

    탐색기는 비동기로 열린다. 이 함수가 반환된 뒤에 창이 뜨므로, 그 사이에
    폴더를 삭제하면 OS 가 "위치를 사용할 수 없습니다" 경고를 띄운다.
    기능 결함이 아니라 삭제와의 경쟁 조건이다.
    """
    if not path.is_dir():
        return

    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except (OSError, FileNotFoundError) as exc:
        _logger.warning("파일 관리자를 열지 못했습니다: %s", exc)

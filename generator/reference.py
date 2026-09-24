"""
generator.reference
IP-Adapter 참조 이미지 로드, 인코딩 및 ControlNet 유닛/스펙 조립.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from generator.config import (
    CN_MODELS_URL,
    CN_MODULES_URL,
    CONTROLNET_LIST_TIMEOUT,
    IP_ADAPTER_MODEL_PATTERNS,
    IP_ADAPTER_MODULE_PATTERNS,
    REFERENCE_EXTENSIONS,
    REFERENCES_DIRNAME,
    ConfigError,
)
from generator.models import ControlNetSpec, ReferenceImage


def load_reference(path: Path) -> ReferenceImage:
    """참조 이미지를 읽어 검증하고 base64 인코딩한다."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"참조 이미지를 읽을 수 없습니다: {path}", str(e)) from None

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as e:
        raise ConfigError(
            f"유효한 이미지 파일이 아닙니다: {path}", str(e)
        ) from None

    return ReferenceImage(
        path=path,
        b64=base64.b64encode(data).decode("ascii"),
        width=width,
        height=height,
    )


def find_reference_candidates(ref_dir: Path, prefix: str) -> list[Path]:
    """references/{prefix}.{ext} 를 우선순위 순서로 찾아 존재하는 것만 반환한다."""
    if not ref_dir.is_dir():
        return []
    return [
        candidate
        for ext in REFERENCE_EXTENSIONS
        if (candidate := ref_dir / f"{prefix}{ext}").is_file()
    ]


def resolve_reference_image(
    ref_dir: Path,
    prefix: str,
    explicit_path: str | None = None,
    disabled: bool = False,
) -> ReferenceImage | None:
    """참조 이미지를 해석한다. 없으면 None 을 반환한다."""
    if disabled:
        return None

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_absolute():
            path = (ref_dir / path).resolve()
        if path.is_dir():
            raise ConfigError(
                f"--ref_image 에 디렉터리가 지정되었습니다: {path}",
                "이미지 파일 경로를 지정하세요.",
            )
        if not path.is_file():
            raise ConfigError(
                f"--ref_image 경로를 찾을 수 없습니다: {path}",
                f"자동 탐색을 쓰려면 {REFERENCES_DIRNAME}/{prefix}.png 로 두고 "
                "--ref_image 를 생략하세요.",
            )
        return load_reference(path)

    candidates = find_reference_candidates(ref_dir, prefix)
    if not candidates:
        return None

    if len(candidates) > 1:
        ignored = [p.name for p in candidates[1:]]
        print(f"[WARN] 참조 이미지가 여러 개입니다. '{candidates[0].name}' 사용, "
              f"무시됨: {ignored}")

    return load_reference(candidates[0])


def build_controlnet_unit(
    reference: ReferenceImage, spec: ControlNetSpec, weight: float
) -> dict[str, Any]:
    """ControlNet 단일 유닛을 조립한다 (순수 함수)."""
    return {
        "enabled": True,
        "input_image": reference.b64,
        "module": spec.module,
        "model": spec.model,
        "weight": weight,
        "resize_mode": "Crop and Resize",
        "control_mode": "Balanced",
        "pixel_perfect": True,
    }


def match_model_name(
    available: Sequence[str], patterns: Sequence[str]
) -> str | None:
    """사용 가능 목록에서 패턴을 부분 문자열로 찾는다 (순수 함수)."""
    lowered = [(name, name.lower()) for name in available]
    for pattern in patterns:
        needle = pattern.lower()
        for original, low in lowered:
            if needle in low:
                return original
    return None


def _fetch_controlnet_list(session: Any, url: str, key: str) -> list[str]:
    """ControlNet 목록 엔드포인트를 조회한다."""
    response = session.get(url, timeout=CONTROLNET_LIST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    return list(data.get(key) or [])


def resolve_controlnet_spec(
    manual_module: str | None = None,
    manual_model: str | None = None,
    session: Any | None = None,
) -> ControlNetSpec | None:
    """ControlNet 전처리기와 모델을 해석한다. 실패하면 None 을 반환한다."""
    if manual_module and manual_model:
        return ControlNetSpec(manual_module, manual_model, "manual")

    if session is None:
        import requests
        session = requests.Session()

    try:
        modules = _fetch_controlnet_list(session, CN_MODULES_URL, "module_list")
        models = _fetch_controlnet_list(session, CN_MODELS_URL, "model_list")
    except Exception:
        print("[WARN] ControlNet 목록 조회 실패 - 참조 이미지 없이 생성합니다")
        print("       ControlNet 확장이 설치되어 있는지 확인하세요.")
        return None

    module = match_model_name(modules, IP_ADAPTER_MODULE_PATTERNS)
    model = match_model_name(models, IP_ADAPTER_MODEL_PATTERNS)

    if not module or not model:
        print(f"[WARN] IP-Adapter 매칭 실패: module={module}, model={model}")
        return None

    return ControlNetSpec(module, model, "auto")

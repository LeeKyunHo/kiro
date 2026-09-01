"""
API 페이로드 조립 — 순수 계층.

**이 모듈이 GPU 없는 환경에서 검증이 가능한 이유다.**

조립과 전송이 한 함수에 섞여 있으면 페이로드 구조를 확인하려고 HTTP 를
가로채야 한다. 분리하면 `--test` 에서 딕셔너리를 직접 검사할 수 있다.

전송은 `api.WebUiClient` 가 담당하며, 이 모듈은 네트워크를 모른다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from . import config
from .models import ControlNetSpec, ReferenceContext, ReferenceImage

# ControlNet 유닛에 반드시 있어야 하는 키. 진단에서 이 집합으로 검사한다.
CONTROLNET_REQUIRED_KEYS: Final = frozenset({
    "enabled",
    "input_image",
    "module",
    "model",
    "weight",
    "resize_mode",
    "control_mode",
    "pixel_perfect",
})

ALWAYSON_KEY: Final = "alwayson_scripts"
CONTROLNET_KEY: Final = "controlnet"


def build_txt2img_payload(
    *, positive: str, negative: str, sampler: str
) -> dict[str, Any]:
    """
    txt2img 페이로드를 조립한다.

    해상도·스텝·CFG 는 config 에서 가져온다. 여기서 리터럴로 적으면
    상수의 단일 진실 공급원이 깨진다.
    """
    return {
        "prompt": positive,
        "negative_prompt": negative,
        "width": config.IMAGE_WIDTH,
        "height": config.IMAGE_HEIGHT,
        "steps": config.STEPS,
        "batch_size": 1,
        "n_iter": 1,
        "cfg_scale": config.CFG_SCALE,
        "sampler_name": sampler,
    }


def build_controlnet_unit(
    reference: ReferenceImage, spec: ControlNetSpec, weight: float
) -> dict[str, Any]:
    """
    ControlNet 단일 유닛을 조립한다.

    resize_mode / control_mode / pixel_perfect 는 WebUI 기본값과 같지만
    명시한다. 버전에 따라 기본값이 바뀌어도 동작이 흔들리지 않게 하려는
    의도다.
    """
    return {
        "enabled": True,
        "input_image": reference.b64,
        "module": spec.module,
        "model": spec.model,
        "weight": weight,
        "resize_mode": config.CN_RESIZE_MODE,
        "control_mode": config.CN_CONTROL_MODE,
        "pixel_perfect": True,
    }


def inject_controlnet(
    payload: dict[str, Any], unit: dict[str, Any]
) -> dict[str, Any]:
    """
    ControlNet 유닛을 주입한 **새 딕셔너리**를 반환한다.

    원본을 변경하지 않는 것이 계약이다. 루프에서 페이로드를 재사용할 때
    상태가 누적되는 것을 막는다. 얕은 복사로 충분한 이유는 최상위에
    키 하나만 추가하고 기존 값은 교체하지 않기 때문이다.
    """
    merged = dict(payload)
    merged[ALWAYSON_KEY] = {CONTROLNET_KEY: {"args": [unit]}}
    return merged


def build_generation_payload(
    *, positive: str, negative: str, sampler: str, reference: ReferenceContext
) -> dict[str, Any]:
    """
    참조 이미지 유무를 반영한 최종 페이로드를 만든다.

    참조 이미지와 ControlNet 해석이 **둘 다** 성공했을 때만 주입한다.
    참조는 있는데 ControlNet 이 없으면(미설치 등) `alwayson_scripts` 키
    자체를 넣지 않는다. 빈 딕셔너리를 넣으면 WebUI 가 "ControlNet 비활성"
    이 아니라 "인자 부족" 으로 해석할 수 있다.
    """
    payload = build_txt2img_payload(
        positive=positive, negative=negative, sampler=sampler
    )
    if not reference.active:
        return payload

    assert reference.image is not None and reference.spec is not None
    unit = build_controlnet_unit(reference.image, reference.spec, reference.weight)
    return inject_controlnet(payload, unit)


def build_interrogate_payload(image_b64: str, model: str) -> dict[str, str]:
    """
    interrogate 페이로드를 조립한다.

    단순하지만 순수 함수로 떼어내 진단에서 구조를 검증한다.
    """
    return {"image": image_b64, "model": model}


def match_pattern(
    available: Sequence[str], patterns: Sequence[str]
) -> str | None:
    """
    사용 가능 목록에서 패턴을 부분 문자열로 찾는다.

    모델명이 `ip-adapter_xl [4209e9f7]` 처럼 해시를 포함해 완전 일치가
    불가능하므로 부분 매칭을 쓴다. 패턴 순서가 우선순위다.

    대소문자를 무시한다. WebUI 버전에 따라 표기가 다를 수 있다.
    """
    lowered = [(name, name.lower()) for name in available]
    for pattern in patterns:
        needle = pattern.lower()
        for original, low in lowered:
            if needle in low:
                return original
    return None


def extract_vram_peak(payload: dict[str, Any]) -> tuple[float, float] | None:
    """
    `/sdapi/v1/memory` 응답에서 (피크 GiB, 전체 GiB) 를 뽑는다.

    응답 구조가 WebUI 버전과 Forge 여부에 따라 다르므로 여러 키를 순차
    탐색한다.

        1. 최상위 스칼라: reserved_peak -> active_peak
        2. 중첩 딕셔너리: reserved.peak -> active.peak -> allocated.peak

    어느 것도 찾지 못하면 None 을 반환하고 호출부는 조용히 넘어간다.
    VRAM 표시는 부가 정보이며 이것 때문에 배치가 실패해서는 안 된다.
    """
    cuda = payload.get("cuda")
    if not isinstance(cuda, dict):
        return None

    system = cuda.get("system")
    total = system.get("total") if isinstance(system, dict) else None
    if not isinstance(total, (int, float)) or total <= 0:
        return None

    peak: float | None = None
    for key in ("reserved_peak", "active_peak"):
        value = cuda.get(key)
        if isinstance(value, (int, float)):
            peak = float(value)
            break

    if peak is None:
        for key in ("reserved", "active", "allocated"):
            node = cuda.get(key)
            if isinstance(node, dict):
                candidate = node.get("peak")
                if isinstance(candidate, (int, float)):
                    peak = float(candidate)
                    break

    if peak is None:
        return None
    return peak / config.GIB, total / config.GIB

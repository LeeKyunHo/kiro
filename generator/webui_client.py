"""
generator.webui_client
SD WebUI API 통신, 페이로드 조립, WebP 변환, VRAM 측정, 모의 이미지 생성.
"""

from __future__ import annotations

import base64
import colorsys
import io
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import requests
from PIL import Image, ImageDraw, ImageFont

from generator.config import (
    API_URL,
    CFG_SCALE,
    GENDER_TAGS,
    GIB,
    IMAGE_SIZE,
    INTERROGATE_TIMEOUT,
    INTERROGATE_URL,
    MEMORY_TIMEOUT,
    MEMORY_URL,
    MOCK_FONT_BIG_SIZE,
    MOCK_FONT_SMALL_SIZE,
    MOCK_GAP_BIG,
    MOCK_GAP_SMALL,
    MOCK_LABEL_MAXLEN,
    MOCK_SIZE,
    MOCK_TEXT_COLOR,
    MOCK_TEXT_TOP,
    MOCK_TEXT_X,
    PARTIAL_SUFFIX,
    SAMPLER_CANDIDATES,
    SAMPLERS_TIMEOUT,
    SAMPLERS_URL,
    SEPARATOR,
    STEPS,
    TXT2IMG_TIMEOUT,
    WEBP_METHOD,
    WEBP_QUALITY,
    ConfigError,
    FontLike,
)
from generator.models import InterrogateResult, PoseEntry, ReferenceImage
from generator.prompt import normalize_tag
from generator.reference import resolve_reference_image


@lru_cache(maxsize=1)
def get_session() -> requests.Session:
    """HTTP 세션을 재사용한다."""
    return requests.Session()


def resolve_sampler() -> str:
    """WebUI가 지원하는 샘플러 목록과 대조해 사용 가능한 첫 후보를 반환한다."""
    try:
        response = get_session().get(SAMPLERS_URL, timeout=SAMPLERS_TIMEOUT)
        response.raise_for_status()
        available = {item["name"] for item in response.json()}
    except Exception:
        print("[SAMPLER] 목록 조회 실패 - 기본값 사용")
        return SAMPLER_CANDIDATES[0]

    for candidate in SAMPLER_CANDIDATES:
        if candidate in available:
            print(f"[SAMPLER] '{candidate}' 감지됨")
            return candidate

    print(f"[SAMPLER] 후보 미발견 - '{SAMPLER_CANDIDATES[0]}' 로 전달")
    return SAMPLER_CANDIDATES[0]


def save_as_webp(
    png_bytes: bytes, save_path: Path, quality: int = WEBP_QUALITY
) -> None:
    """PNG 바이트를 Pillow로 열어 실제 WebP로 인코딩 저장한다 (원자적 교체)."""
    partial = save_path.with_name(save_path.name + PARTIAL_SUFFIX)

    image = Image.open(io.BytesIO(png_bytes))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    try:
        image.save(partial, format="WEBP", quality=quality, method=WEBP_METHOD)
        os.replace(partial, save_path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def build_txt2img_payload(
    *, prompt: str, negative_prompt: str, sampler_name: str,
    width: int = IMAGE_SIZE[0], height: int = IMAGE_SIZE[1],
    steps: int | None = None, cfg_scale: float | None = None,
    scheduler: str | None = None
) -> dict[str, Any]:
    """txt2img 페이로드를 조립한다 (순수 함수)."""
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps if steps is not None else STEPS,
        "batch_size": 1,
        "n_iter": 1,
        "cfg_scale": cfg_scale if cfg_scale is not None else CFG_SCALE,
        "scheduler": "Automatic",
        "sampler_name": sampler_name,
    }
    
    if scheduler:
        payload["scheduler"] = scheduler
    
    return payload


def _build_forge_scripts(
    enable_freeu: bool = False,
    freeu_b1: float = 1.1,
    freeu_b2: float = 1.2,
    freeu_s1: float = 0.9,
    freeu_s2: float = 0.2,
    enable_adetailer: bool = False,
) -> dict[str, Any]:
    """Forge 내장 기능 스크립트를 조립한다 (순수 함수)."""
    scripts: dict[str, Any] = {}

    if enable_freeu:
        scripts["FreeU Integrated"] = {
            "args": [True, freeu_b1, freeu_b2, freeu_s1, freeu_s2]
        }

    if enable_adetailer:
        scripts["ADetailer"] = {
            "args": [
                True,
                False,
                {
                    "ad_model": "face_yolov8n.pt",
                    "ad_prompt": "",
                    "ad_negative_prompt": "",
                    "ad_confidence": 0.3,
                    "ad_mask_k_largest": 0,
                    "ad_mask_min_ratio": 0.0,
                    "ad_mask_max_ratio": 1.0,
                    "ad_dilate_erode": 4,
                    "ad_x_offset": 0,
                    "ad_y_offset": 0,
                    "ad_mask_merge_invert": "None",
                    "ad_mask_blur": 4,
                    "ad_denoising_strength": 0.4,
                    "ad_inpaint_only_masked": True,
                    "ad_inpaint_only_masked_padding": 32,
                    "ad_use_inpaint_width_height": False,
                    "ad_inpaint_width": 512,
                    "ad_inpaint_height": 512,
                    "ad_use_steps": False,
                    "ad_steps": 28,
                    "ad_use_cfg_scale": False,
                    "ad_cfg_scale": 7.0,
                    "ad_use_checkpoint": False,
                    "ad_checkpoint": "Use same checkpoint",
                    "ad_use_vae": False,
                    "ad_vae": "Use same VAE",
                    "ad_use_sampler": False,
                    "ad_sampler": "DPM++ 2M",
                    "ad_use_noise_multiplier": False,
                    "ad_noise_multiplier": 1.0,
                    "ad_use_clip_skip": False,
                    "ad_clip_skip": 1,
                    "ad_restore_face": False,
                    "ad_controlnet_model": "None",
                    "ad_controlnet_weight": 1.0,
                    "ad_controlnet_guidance_start": 0.0,
                    "ad_controlnet_guidance_end": 1.0,
                },
            ]
        }

    return scripts


def inject_alwayson_scripts(
    payload: dict[str, Any],
    controlnet_units: list[dict[str, Any]],
    enable_freeu: bool = False,
    freeu_b1: float = 1.1,
    freeu_b2: float = 1.2,
    freeu_s1: float = 0.9,
    freeu_s2: float = 0.2,
    enable_adetailer: bool = False,
) -> dict[str, Any]:
    """페이로드의 alwayson_scripts 에 Forge 기능 + ControlNet 유닛 주입 (원본 불변)."""
    merged = dict(payload)
    scripts = _build_forge_scripts(
        enable_freeu, freeu_b1, freeu_b2, freeu_s1, freeu_s2, enable_adetailer
    )
    if controlnet_units:
        scripts["controlnet"] = {"args": controlnet_units}
    merged["alwayson_scripts"] = scripts
    return merged


def inject_controlnet(
    payload: dict[str, Any], unit: dict[str, Any]
) -> dict[str, Any]:
    """단일 유닛 주입 래퍼 (하위 호환용)."""
    return inject_alwayson_scripts(payload, [unit])


def extract_vram_peak(payload: dict[str, Any]) -> tuple[float, float] | None:
    """/sdapi/v1/memory 응답에서 (피크 사용량 GiB, 전체 GiB) 추출."""
    cuda = payload.get("cuda")
    if not isinstance(cuda, dict):
        return None

    total = (cuda.get("system") or {}).get("total")
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
            if isinstance(node, dict) and isinstance(node.get("peak"), (int, float)):
                peak = float(node["peak"])
                break

    if peak is None:
        return None
    return peak / GIB, total / GIB


def fetch_vram_peak() -> tuple[float, float] | None:
    """VRAM 피크를 조회한다. 실패하면 None."""
    try:
        response = get_session().get(MEMORY_URL, timeout=MEMORY_TIMEOUT)
        response.raise_for_status()
        return extract_vram_peak(response.json())
    except Exception:
        return None


def generate_image(payload: dict[str, Any]) -> bytes:
    """조립된 페이로드를 전송해 PNG 바이트를 받는다."""
    response = get_session().post(API_URL, json=payload, timeout=TXT2IMG_TIMEOUT)
    response.raise_for_status()

    images = response.json().get("images") or []
    if not images:
        raise RuntimeError("API 응답에 images 가 없습니다")

    return base64.b64decode(images[0])


def build_interrogate_payload(b64: str, model: str) -> dict[str, str]:
    """interrogate 페이로드를 조립한다."""
    return {"image": b64, "model": model}


def filter_gender_tags(tags: Sequence[str]) -> tuple[list[str], list[str]]:
    """성별·인원 태그를 분리한다 (순수 함수)."""
    kept: list[str] = []
    removed: list[str] = []
    for tag in tags:
        (removed if normalize_tag(tag) in GENDER_TAGS else kept).append(tag)
    return kept, removed


def run_interrogate(image_path: str, model: str) -> int:
    """참조 이미지에서 태그를 역추출해 출력한다."""
    ref_dir = Path.cwd()
    reference = resolve_reference_image(ref_dir, "_", explicit_path=image_path)
    if reference is None:
        raise ConfigError(f"이미지를 찾을 수 없습니다: {image_path}")

    payload = build_interrogate_payload(reference.b64, model)

    print(f"\n{SEPARATOR}")
    print(f"  태그 추출 | {reference.label} | 모델: {model}")
    print(SEPARATOR)

    try:
        response = get_session().post(
            INTERROGATE_URL, json=payload, timeout=INTERROGATE_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        print("\n[ERROR] WebUI 에 연결할 수 없습니다.", file=sys.stderr)
        print("        webui-user.bat 에 --api 를 넣고 실행했는지 확인하세요.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\n[ERROR] interrogate 실패: {e}", file=sys.stderr)
        if model == "deepdanbooru":
            print("        DeepBooru 모델이 없으면 --interrogator clip 을 시도하세요.", file=sys.stderr)
        return 1

    raw = (response.json().get("caption") or "").strip()
    if not raw:
        print("\n[ERROR] 추출된 태그가 없습니다.", file=sys.stderr)
        return 1

    tags = [t.strip() for t in raw.split(",") if t.strip()]
    kept, removed = filter_gender_tags(tags)
    result = InterrogateResult(raw=raw, tags=tags, gender_tags=removed)

    print(f"\n[원본] ({len(tags)}개 태그)")
    print(f"{result.raw}")

    if removed:
        print(f"\n[WARN] 성별·인원 태그가 감지되었습니다: {removed}")
        print("       프로필(_profiles)에서 이미 다루므로 --char_prompt 에는 넣지 마세요.")

    print(f"\n[권장] ({len(kept)}개 태그)")
    print(f"{result.filtered}")

    print("\n[그대로 실행하려면]")
    print(f'python sd_batch_generator.py --prefix PREFIX --char_prompt "{result.filtered}"')
    print(f"{SEPARATOR}\n")
    return 0


def _hue_color(code: int) -> tuple[int, int, int]:
    """코드값으로 배경색을 분산시켜 이미지 구분이 육안으로 가능하게 한다."""
    red, green, blue = colorsys.hsv_to_rgb(((code * 37) % 360) / 360.0, 0.35, 0.90)
    return int(red * 255), int(green * 255), int(blue * 255)


@lru_cache(maxsize=1)
def _mock_fonts() -> tuple[FontLike, FontLike]:
    """더미 이미지용 폰트를 한 번만 로드해 재사용한다."""
    try:
        return (
            ImageFont.truetype("arial.ttf", MOCK_FONT_BIG_SIZE),
            ImageFont.truetype("arial.ttf", MOCK_FONT_SMALL_SIZE),
        )
    except OSError:
        fallback = ImageFont.load_default()
        return fallback, fallback


def make_dummy_png(
    prefix: str,
    code: int,
    width: int,
    entry: PoseEntry,
    reference: ReferenceImage | None = None,
) -> bytes:
    """API 반환값과 동일한 형태(PNG 바이트열)의 더미 이미지를 즉석 생성한다."""
    from generator.reporter import format_code

    font_big, font_small = _mock_fonts()

    image = Image.new("RGB", MOCK_SIZE, _hue_color(code))
    draw = ImageDraw.Draw(image)

    rows: tuple[tuple[str, FontLike, int], ...] = (
        (format_code(code, width), font_big, MOCK_GAP_BIG),
        (prefix, font_small, MOCK_GAP_SMALL),
        (entry.section, font_small, MOCK_GAP_SMALL),
        (entry.label[:MOCK_LABEL_MAXLEN], font_small, MOCK_GAP_SMALL),
        ("MOCK +REF" if reference else "MOCK", font_small, MOCK_GAP_SMALL),
    )

    y = MOCK_TEXT_TOP
    for text, font, gap in rows:
        draw.text((MOCK_TEXT_X, y), text, fill=MOCK_TEXT_COLOR, font=font)
        y += gap

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

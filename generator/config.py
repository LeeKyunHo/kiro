"""
generator.config
전역 상수, URL, 기본 생성 파라미터 및 예외 클래스 정의.
"""

from __future__ import annotations

import re
import sys
from typing import Any

# ─────────────────────────────────────────────
# 1. API 및 네트워크 엔드포인트
# ─────────────────────────────────────────────
DEFAULT_HOST = "http://127.0.0.1:7860"
API_HOST = DEFAULT_HOST
API_URL = f"{API_HOST}/sdapi/v1/txt2img"
SAMPLERS_URL = f"{API_HOST}/sdapi/v1/samplers"

CN_MODULES_URL = f"{API_HOST}/controlnet/module_list"
CN_MODELS_URL = f"{API_HOST}/controlnet/model_list"

MEMORY_URL = f"{API_HOST}/sdapi/v1/memory"
MEMORY_TIMEOUT = 5
GIB = 1024 ** 3

SAMPLER_CANDIDATES = ("Euler a", "Euler", "DPM++ 2M SDE Karras", "DPM++ 2M Karras", "DPM++ 2M")

SAMPLERS_TIMEOUT = 5
TXT2IMG_TIMEOUT = 300
CONTROLNET_LIST_TIMEOUT = 10

# ─────────────────────────────────────────────
# 2. 프롬프트 및 파일 기본값
# ─────────────────────────────────────────────
POS_BASE = (
    "masterpiece, best quality, highly detailed, "
    "1girl, solo, clean background, soft lighting, character portrait"
)

COMMON_NEG = (
    "worst quality, low quality, blurry, bad anatomy, bad hands, "
    "extra fingers, extra limbs, deformed, disfigured, watermark, "
    "signature, text, jpeg artifacts, cropped"
)

POSE_DB_FILE = "pose_database.json"
ASSETS_DIRNAME = "assets"
CHARACTERS_DIRNAME = "characters"
PROJECTS_DIRNAME = "projects"

ROSTER_ALIAS = {
    "dar": "dark_generals",
    "sea": "sea",
    "oto": "oto",
    "default": "dark_generals",
}

REFERENCES_DIRNAME = "references"
REFERENCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

REF_WEIGHT_DEFAULT = 0.7
REF_WEIGHT_MIN = 0.0
REF_WEIGHT_MAX = 2.0

IP_ADAPTER_MODULE_PATTERNS = ("ip-adapter", "ipadapter")
IP_ADAPTER_MODEL_PATTERNS = ("ip-adapter", "ipadapter")

DEFAULT_PROFILE = "female"
FALLBACK_PROFILE = "(built-in)"

BRACKET_CHARS = "()[]{}"
_BRACKET_TABLE = str.maketrans("", "", BRACKET_CHARS)
WEIGHT_SUFFIX_PATTERN = re.compile(r":\s*-?\d+(?:\.\d+)?\s*$")

# ─────────────────────────────────────────────
# 3. 실제 생성 파라미터 (SDXL / Illustrious)
# ─────────────────────────────────────────────
IMAGE_SIZE = (832, 1216)
STEPS = 30
CFG_SCALE = 7
LORA_STRING = ""
WEBP_QUALITY = 95
WEBP_METHOD = 6

MOCK_SIZE = (208, 304)
MOCK_TEXT_X = 12
MOCK_TEXT_TOP = 24
MOCK_TEXT_COLOR = (30, 30, 30)
MOCK_FONT_BIG_SIZE = 44
MOCK_FONT_SMALL_SIZE = 13
MOCK_GAP_BIG = 52
MOCK_GAP_SMALL = 20
MOCK_LABEL_MAXLEN = 26

URL_PLACEHOLDER = "{{url}}"
GENIT_STATUS_TEMPLATE = (
    "[@id=상태창|name={name}|title={title}|status={status}|desc={desc}]"
)

CODE_EXPR_PATTERN = re.compile(r"^[\s\d,\-]+$")
SAFE_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_CODE = 9_999
MIN_CODE_WIDTH = 2
SECTION_COMMENT_PREFIX = "_"
PARTIAL_SUFFIX = ".part"
SEPARATOR = "=" * 64

FontLike = Any


class ConfigError(Exception):
    """
    설정·입력 오류.
    헬퍼가 직접 sys.exit() 하지 않고 이 예외를 올리면,
    종료 정책은 main 한 곳에만 남는다.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def configure_stdio() -> None:
    """표준 출력을 UTF-8 + unbuffered 로 고정한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, OSError):
            pass

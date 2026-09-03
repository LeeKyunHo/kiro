"""
sd_batch_generator.py
캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI API 연동)

프롬프트는 pose_database.json 에서 불러오며, 항목 수에 맞춰 동적으로 순회한다.
결과는 Pillow로 실제 WebP 로 인코딩 변환해 저장한다.

실행 모드
    (기본)      실제 생성. WebUI API 호출.
    --mock      API 없이 더미 이미지를 생성해 전체 파이프라인 검증.
    --dry-run   파일 쓰기 없이 대상·파일명·마크다운만 출력.
    --test      데이터/로직 자체 진단 후 종료 코드 반환.

Usage:
    python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes"
    python sd_batch_generator.py --prefix mika --char_prompt "..." --mode emotions
    python sd_batch_generator.py --prefix mika --char_prompt "..." --mode 0,5,12
    python sd_batch_generator.py --prefix mika --char_prompt "..." --mode 10-14
    python sd_batch_generator.py --prefix test --char_prompt "none" --mock
    python sd_batch_generator.py --test
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import requests
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
# 1. 상수
# ─────────────────────────────────────────────
API_HOST = "http://127.0.0.1:7860"
API_URL = f"{API_HOST}/sdapi/v1/txt2img"
SAMPLERS_URL = f"{API_HOST}/sdapi/v1/samplers"
INTERROGATE_URL = f"{API_HOST}/sdapi/v1/interrogate"

# ControlNet 확장이 제공하는 조회 엔드포인트.
# 모델명에 해시가 붙어 환경마다 다르므로 조회가 필수다.
CN_MODULES_URL = f"{API_HOST}/controlnet/module_list"
CN_MODELS_URL = f"{API_HOST}/controlnet/model_list"

# VRAM 사용량 조회. 응답 구조가 버전마다 달라 방어적으로 파싱한다.
MEMORY_URL = f"{API_HOST}/sdapi/v1/memory"
MEMORY_TIMEOUT = 5
GIB = 1024 ** 3

SAMPLER_CANDIDATES = ("DPM++ 2M Karras", "DPM++ 2M", "Euler a")

SAMPLERS_TIMEOUT = 5
TXT2IMG_TIMEOUT = 300
CONTROLNET_LIST_TIMEOUT = 10
INTERROGATE_TIMEOUT = 120

# 챗봇 초상화용 공통 품질 태그
POS_BASE = (
    "masterpiece, best quality, highly detailed, "
    "1girl, solo, clean background, soft lighting, character portrait"
)

# 공통 네거티브 (품질/해부학 관련)
COMMON_NEG = (
    "worst quality, low quality, blurry, bad anatomy, bad hands, "
    "extra fingers, extra limbs, deformed, disfigured, watermark, "
    "signature, text, jpeg artifacts, cropped"
)

POSE_DB_FILE = "pose_database.json"
ASSETS_DIRNAME = "generated_assets"
CHARACTERS_DIRNAME = "characters"

# ── 참조 이미지 (IP-Adapter) ─────────────────
REFERENCES_DIRNAME = "references"
# 탐색 우선순위. 여러 확장자가 공존하면 앞의 것을 쓴다.
REFERENCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# IP-Adapter 적용 강도.
# 1.0 이상은 참조 이미지의 포즈까지 전이되어 JSON 포즈 지시를 무시한다.
# 0.7 은 실무 관행에 기반한 출발점이며 실제 최적값은 GPU 환경에서 튜닝한다.
REF_WEIGHT_DEFAULT = 0.7
REF_WEIGHT_MIN = 0.0
REF_WEIGHT_MAX = 2.0

# ControlNet 모듈·모델 탐색 패턴. 부분 문자열로 매칭한다.
# 모델명이 "ip-adapter_xl [4209e9f7]" 형태라 완전 일치가 불가능하다.
IP_ADAPTER_MODULE_PATTERNS = ("ip-adapter", "ipadapter")
IP_ADAPTER_MODEL_PATTERNS = ("ip-adapter", "ipadapter")

# ── 태그 역추출 (--from_image) ───────────────
INTERROGATORS = ("deepdanbooru", "clip")
INTERROGATE_DEFAULT = "deepdanbooru"

# --char_prompt 에 들어가면 프로필(_profiles)과 충돌하는 태그.
# DeepBooru 는 거의 항상 성별 태그를 반환하므로 실질적으로 매번 걸린다.
# 'solo' 는 프로필 base_positive 에 이미 있어 중복이다.
GENDER_TAGS = frozenset({
    "1girl", "2girls", "3girls", "multiple girls", "girl",
    "1boy", "2boys", "3boys", "multiple boys", "boy",
    "male", "female", "male focus", "female focus",
    "solo", "solo focus",
})

# _profiles 섹션. 프로필의 base_positive/base_negative 가 위 POS_BASE/COMMON_NEG 를
# 완전히 대체한다. 품질 태그도 프로필 쪽에 포함되어야 한다.
PROFILES_KEY = "_profiles"
PROFILE_POSITIVE_KEY = "base_positive"
PROFILE_NEGATIVE_KEY = "base_negative"

# --profile 생략 시 이 프로필을 쓴다. 기존 명령어를 깨지 않기 위한 기본값이며,
# 무엇이 적용됐는지 콘솔에 명시적으로 출력한다.
DEFAULT_PROFILE = "female"

# 프로필이 정의되지 않은 JSON 과의 하위 호환용 가상 프로필 이름
FALLBACK_PROFILE = "(built-in)"

# 태그 정규화용. "(tag:1.3)" -> "tag", "((tag))" -> "tag"
BRACKET_CHARS = "()[]{}"
_BRACKET_TABLE = str.maketrans("", "", BRACKET_CHARS)
WEIGHT_SUFFIX_PATTERN = re.compile(r":\s*-?\d+(?:\.\d+)?\s*$")

# 실제 생성 파라미터
IMAGE_SIZE = (832, 1216)
STEPS = 28
CFG_SCALE = 7
WEBP_QUALITY = 90
WEBP_METHOD = 6

# 모의 생성 (실제의 1/4, 종횡비 동일 — R7.5)
MOCK_SIZE = (208, 304)
MOCK_TEXT_X = 12
MOCK_TEXT_TOP = 24
MOCK_TEXT_COLOR = (30, 30, 30)
MOCK_FONT_BIG_SIZE = 44
MOCK_FONT_SMALL_SIZE = 13
MOCK_GAP_BIG = 52
MOCK_GAP_SMALL = 20
MOCK_LABEL_MAXLEN = 26

# 젠잇 규격: 치환되지 않는 리터럴 플레이스홀더 (R4.3)
# 일반 문자열로 두면 f-string 4중 이스케이프가 불필요하다.
URL_PLACEHOLDER = "{{url}}"

GENIT_STATUS_TEMPLATE = (
    "[@id=상태창|name={name}|title={title}|status={status}|desc={desc}]"
)

# --mode 값이 섹션명인지 코드 표현식인지 판별 (R2.7)
# 숫자·콤마·하이픈·공백만으로 구성되면 코드 표현식.
CODE_EXPR_PATTERN = re.compile(r"^[\s\d,\-]+$")

# prefix 는 경로 세그먼트와 (구버전 호환용) 셸 인자로 쓰이므로 화이트리스트로 제한한다.
# 이 검사가 경로 이탈(../)과 인용부호 주입을 입구에서 함께 차단한다.
SAFE_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 코드 범위 상한. 없으면 "0-999999999" 같은 입력이 메모리를 폭주시킨다.
MAX_CODE = 9_999

MIN_CODE_WIDTH = 2
SECTION_COMMENT_PREFIX = "_"
PARTIAL_SUFFIX = ".part"
SEPARATOR = "=" * 64

FontLike = Any


class ConfigError(Exception):
    """
    설정·입력 오류.

    헬퍼가 직접 sys.exit() 하지 않고 이 예외를 올리면, 단위 검증에서
    함수를 그대로 호출할 수 있고 종료 정책은 main 한 곳에만 남는다.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _configure_stdio() -> None:
    """
    표준 출력을 UTF-8 로 고정한다.

    Windows에서 출력을 파이프/파일로 리다이렉트하면 Python이 콘솔 UTF-8 대신
    로케일 인코딩(cp949)으로 폴백해 비-ASCII 기호에서 UnicodeEncodeError 가
    발생한다. 직접 실행 시에는 재현되지 않아 놓치기 쉬운 경로다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 구버전 파이썬이나 비표준 스트림은 그대로 둔다


# ─────────────────────────────────────────────
# 2. 데이터 모델
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PoseEntry:
    """포즈/표정 단일 항목."""

    code: int
    prompt: str
    section: str

    @property
    def label(self) -> str:
        """프롬프트 첫 태그를 사람이 읽을 라벨로 사용."""
        return self.prompt.split(",")[0].strip()


@dataclass(frozen=True, slots=True)
class Profile:
    """
    성별 등 캐릭터 축 프리셋.

    포즈·표정과 직교하는 축이므로 포즈 섹션에 섞지 않고 별도로 둔다.
    섹션에 성별을 넣으면 emotions_female / emotions_male 처럼 조합이
    곱셈으로 늘어나 같은 감정을 여러 곳에서 관리해야 한다.
    """

    name: str
    base_positive: str
    base_negative: str


@dataclass(slots=True)
class PoseDatabase:
    entries: dict[int, PoseEntry] = field(default_factory=dict)
    sections: dict[str, list[int]] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_codes(self) -> list[int]:
        return sorted(self.entries)

    @property
    def section_names(self) -> list[str]:
        return list(self.sections)

    @property
    def profile_names(self) -> list[str]:
        return list(self.profiles)


@dataclass(frozen=True, slots=True)
class CharacterConfig:
    """
    characters/{name}.json 에서 로드한 캐릭터 프리셋.

    --char name 으로 지정하면 이 값들이 커맨드라인 기본값으로 쓰인다.
    커맨드라인에 같은 인자가 있으면 커맨드라인 쪽이 우선한다.

    prefix: 생략 시 파일명(name)을 사용한다.
    profile / custom_neg: 생략 가능. 생략 시 기존 기본값 동작.
    """

    name: str          # 파일명 (확장자 제외). bel.json -> "bel"
    char_prompt: str
    prefix: str        # json 의 "prefix" 또는 name 으로 채워진다
    profile: str | None = None
    custom_neg: str = ""
    ref_weight: float | None = None  # None 이면 REF_WEIGHT_DEFAULT 사용
    # positive/negative 가 있으면 profile+char_prompt 조합을 완전히 대체한다.
    # 없으면 기존 방식(profile + char_prompt + custom_neg) 폴백.
    positive: str | None = None
    negative: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """
    IP-Adapter 참조 이미지.

    b64 를 프로퍼티가 아닌 필드로 갖는다. 20~50장을 생성하는 배치에서
    프로퍼티로 두면 호출마다 재인코딩되므로 생성 시점에 한 번만 계산한다.

    "참조 없음" 은 이 클래스의 특수 인스턴스가 아니라 None 으로 표현한다.
    """

    path: Path
    b64: str
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.path.name} ({self.width}x{self.height})"


@dataclass(frozen=True, slots=True)
class ControlNetSpec:
    """
    ControlNet 전처리기와 모델 조합.

    source 는 자동 탐지("auto")인지 사용자 지정("manual")인지 구분한다.
    GPU 환경에서 어느 경로가 동작했는지 로그로 판단하기 위한 것이다.
    """

    module: str
    model: str
    source: str = "auto"


@dataclass(frozen=True, slots=True)
class InterrogateResult:
    """태그 역추출 결과."""

    raw: str
    tags: list[str]
    gender_tags: list[str]

    @property
    def filtered(self) -> str:
        """성별·인원 태그를 제거한 프롬프트 문자열."""
        excluded = set(self.gender_tags)
        return ", ".join(tag for tag in self.tags if tag not in excluded)


@dataclass(frozen=True, slots=True)
class TimingStats:
    """
    생성 시간 집계.

    VRAM 설정(--medvram-sdxl, xFormers 등)을 비교할 때 판단 근거가 된다.
    에파는 한 배치에 수십 장을 순차 생성하므로 장당 손실이 누적된다.
    """

    count: int
    total: float
    average: float
    fastest: float
    slowest: float

    def format(self) -> str:
        return (
            f"{self.count}장 / 총 {self.total:.1f}초 / "
            f"장당 평균 {self.average:.1f}초 "
            f"(최속 {self.fastest:.1f} ~ 최저 {self.slowest:.1f})"
        )


def summarize_durations(seconds: Sequence[float]) -> TimingStats | None:
    """측정값을 집계한다 (순수 함수). 빈 입력은 None."""
    if not seconds:
        return None
    total = sum(seconds)
    return TimingStats(
        count=len(seconds),
        total=total,
        average=total / len(seconds),
        fastest=min(seconds),
        slowest=max(seconds),
    )


@dataclass(slots=True)
class BatchResult:
    success: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)
    # (코드, 소요 초). 실제로 생성한 것만 기록한다. 건너뛴 것은 제외.
    durations: list[tuple[int, float]] = field(default_factory=list)
    aborted: bool = False
    dry_run: bool = False

    @property
    def existing(self) -> list[int]:
        """
        마크다운 대상 코드.

        기본/mock: 디스크에 실제 파일이 있는 코드만 (R4.6)
        dry-run:   시뮬레이션 대상 전체 (R4.8)

        분기를 프로퍼티 내부에 두어 호출부가 실행 모드를 알 필요가 없게 한다.
        """
        if self.dry_run:
            return sorted(self.planned)
        return sorted(self.success + self.skipped)

    @property
    def failed_codes(self) -> list[int]:
        return [code for code, _ in self.failed]

    @property
    def timing(self) -> TimingStats | None:
        """생성 시간 집계. 측정값이 없으면 None."""
        return summarize_durations([sec for _, sec in self.durations])


@dataclass(slots=True)
class TestReport:
    """--test 결과 수집기."""

    passed: int = 0
    failed: int = 0
    warned: int = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.passed += ok
        self.failed += not ok
        self._emit("[PASS]" if ok else "[FAIL]", name, detail)
        return ok

    def warn(self, name: str, detail: str = "") -> None:
        self.warned += 1
        self._emit("[WARN]", name, detail)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        self._emit("[PASS]", name, detail)

    @staticmethod
    def _emit(tag: str, name: str, detail: str) -> None:
        print(f"  {tag} {name}" + (f" - {detail}" if detail else ""))

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


# ─────────────────────────────────────────────
# 3. 태그 유틸리티
# ─────────────────────────────────────────────
def normalize_tag(raw: str) -> str:
    """
    비교 가능한 형태로 태그를 정규화한다.

    괄호 강조와 가중치 표기를 제거하고 소문자·단일 공백으로 맞춘다.
        "(huge:1.3)"      -> "huge"
        "((tag))"         -> "tag"
        " Bad   Hands "   -> "bad hands"
    """
    tag = raw.strip().lower().translate(_BRACKET_TABLE)
    tag = WEIGHT_SUFFIX_PATTERN.sub("", tag)
    return " ".join(tag.split())


def split_tags(text: str) -> list[str]:
    """쉼표로 분리해 정규화한 태그 목록. 빈 토큰은 버린다."""
    return [tag for tag in map(normalize_tag, text.split(",")) if tag]


def find_tag_conflicts(positive: str, negative: str) -> list[str]:
    """
    포지티브와 네거티브에 동시에 존재하는 태그를 찾는다.

    같은 문자열만 잡는다. '1girl' 과 '1boy' 처럼 의미가 상충하지만
    문자열이 다른 경우는 검출되지 않는다.
    """
    return sorted(set(split_tags(positive)) & set(split_tags(negative)))


def join_tags(*parts: str) -> str:
    """빈 조각을 건너뛰고 쉼표로 이어붙인다."""
    return ", ".join(part.strip() for part in parts if part and part.strip())


# ─────────────────────────────────────────────
# 4. 입력 검증
# ─────────────────────────────────────────────
def validate_ref_weight(value: float) -> float:
    """IP-Adapter 적용 강도를 검증한다."""
    if not REF_WEIGHT_MIN <= value <= REF_WEIGHT_MAX:
        raise ConfigError(
            f"--ref_weight 는 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} 범위여야 합니다: {value}",
            "0.5~0.8 이 실무 범위입니다. 1.0 이상은 참조 이미지의 포즈까지 전이됩니다.",
        )
    return value


def validate_prefix(prefix: str) -> str:
    """
    prefix 를 안전한 단일 경로 세그먼트로 제한한다.

    prefix 는 저장 경로와 파일명에 그대로 들어가므로, 검증하지 않으면
    '../' 로 대상 폴더를 벗어나거나 인용부호로 셸 인자를 깨뜨릴 수 있다.
    화이트리스트 방식이라 새 위험 문자가 생겨도 자동으로 막힌다.
    """
    candidate = prefix.strip()
    if not SAFE_PREFIX_PATTERN.match(candidate):
        raise ConfigError(
            f"prefix '{prefix}' 를 사용할 수 없습니다.",
            "영문·숫자·밑줄·하이픈 1~64자만 허용합니다. (예: mika, test_01)",
        )
    return candidate


# ─────────────────────────────────────────────
# 4A. 캐릭터 프리셋 (characters/*.json)
# ─────────────────────────────────────────────
_CHAR_REQUIRED_KEYS = frozenset({"char_prompt"})
_CHAR_OPTIONAL_KEYS = frozenset({"prefix", "profile", "custom_neg", "ref_weight", "positive", "negative"})
_CHAR_ALL_KEYS = _CHAR_REQUIRED_KEYS | _CHAR_OPTIONAL_KEYS


def _characters_dir(base_dir: Path) -> Path:
    return base_dir / CHARACTERS_DIRNAME


def load_character(base_dir: Path, name: str) -> CharacterConfig:
    """
    characters/{name}.json 을 읽어 CharacterConfig 로 변환한다.

    Raises:
        ConfigError: 파일 없음, JSON 문법 오류, 필수 키 누락.
    """
    path = _characters_dir(base_dir) / f"{name}.json"
    if not path.is_file():
        chars_dir = _characters_dir(base_dir)
        available = sorted(p.stem for p in chars_dir.glob("*.json")) if chars_dir.is_dir() else []
        hint = (
            f"사용 가능: {available}" if available
            else f"{CHARACTERS_DIRNAME}/ 폴더에 json 파일이 없습니다"
        )
        raise ConfigError(f"캐릭터 '{name}' 을 찾을 수 없습니다 ({path})", hint)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} JSON 문법 오류: {e}") from None

    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} 최상위가 딕셔너리가 아닙니다")

    missing = _CHAR_REQUIRED_KEYS - raw.keys()
    # positive 가 있으면 char_prompt 없이도 동작 가능하므로 필수 검사에서 제외한다.
    if "positive" in raw:
        missing -= {"char_prompt"}
    if missing:
        raise ConfigError(
            f"{path.name} 필수 키 누락: {sorted(missing)}",
            f"'positive'+'negative' 직접 기재 또는 'char_prompt' 중 하나가 필요합니다",
        )

    unknown = set(raw.keys()) - _CHAR_ALL_KEYS
    if unknown:
        print(f"[WARN] {path.name} 알 수 없는 키 무시: {sorted(unknown)}")

    # positive 직접 기재 방식: char_prompt 불필요
    raw_positive = raw.get("positive")
    raw_negative = raw.get("negative")
    positive = raw_positive.strip() if isinstance(raw_positive, str) and raw_positive.strip() else None
    negative = raw_negative.strip() if isinstance(raw_negative, str) and raw_negative.strip() else None

    # 기존 방식: char_prompt 필요
    raw_char_prompt = raw.get("char_prompt", "")
    char_prompt = raw_char_prompt.strip() if isinstance(raw_char_prompt, str) else ""
    if not positive and not char_prompt:
        raise ConfigError(
            f"{path.name} 'char_prompt' 또는 'positive' 중 하나는 있어야 합니다"
        )

    # prefix 미지정 시 파일명을 사용한다.
    raw_prefix = raw.get("prefix") or name
    prefix = validate_prefix(str(raw_prefix))

    return CharacterConfig(
        name=name,
        char_prompt=char_prompt,
        prefix=prefix,
        profile=raw.get("profile") or None,
        custom_neg=str(raw.get("custom_neg") or "").strip(),
        ref_weight=float(raw["ref_weight"]) if "ref_weight" in raw else None,
        positive=positive,
        negative=negative,
    )


def list_characters(base_dir: Path) -> int:
    """
    characters/ 폴더의 캐릭터 목록을 출력한다.

    Returns:
        종료 코드.
    """
    chars_dir = _characters_dir(base_dir)
    if not chars_dir.is_dir():
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 폴더가 없습니다. 캐릭터를 추가하세요.")
        return 0

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 에 json 파일이 없습니다.")
        return 0

    print(f"\n{'캐릭터':16}  {'프로필':20}  {'prefix'}")
    print("-" * 56)
    errors: list[str] = []
    for path in files:
        try:
            cfg = load_character(base_dir, path.stem)
            profile_display = cfg.profile or f"(기본값: {DEFAULT_PROFILE})"
            prefix_display = cfg.prefix if cfg.prefix != path.stem else "(파일명과 동일)"
            print(f"  {path.stem:<14}  {profile_display:<20}  {prefix_display}")
        except ConfigError as e:
            errors.append(f"  [ERROR] {path.name}: {e}")

    if errors:
        print()
        for msg in errors:
            print(msg)
        return 1

    print()
    return 0


def run_all_chars(base_dir: Path, mode: str, codes_expr: str | None,
                  dry_run: bool, mock: bool) -> int:
    """
    characters/ 의 모든 캐릭터를 순서대로 생성한다.

    각 캐릭터마다 execute() 를 호출한다. 이미 있는 파일은 기존 스킵 로직이 처리한다.
    한 캐릭터가 실패해도 나머지는 계속 진행하고, 마지막에 전체 요약을 출력한다.

    Returns:
        0: 전체 성공 (부분 스킵 포함)
        1: 1개 이상 실패
    """
    chars_dir = _characters_dir(base_dir)
    if not chars_dir.is_dir():
        print(f"[ERROR] {CHARACTERS_DIRNAME}/ 폴더가 없습니다.", file=sys.stderr)
        return 1

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {CHARACTERS_DIRNAME}/ 에 json 파일이 없습니다.")
        return 0

    total = len(files)
    succeeded: list[str] = []
    failed: list[str] = []

    print(f"\n[ALL-CHARS] {total}명 순차 생성 시작\n{'=' * 64}")

    for idx, path in enumerate(files, 1):
        name = path.stem
        print(f"\n[ALL-CHARS] ({idx}/{total}) {name}")
        print("-" * 40)

        try:
            cfg = load_character(base_dir, name)
        except ConfigError as e:
            print(f"[ERROR] {name} 로드 실패: {e}", file=sys.stderr)
            failed.append(name)
            continue

        # execute()가 요구하는 최소 args 를 조립한다.
        # --all-chars 와 함께 넘어온 --mode / --codes / --dry-run / --mock 은
        # 모든 캐릭터에 동일하게 적용된다.
        args = argparse.Namespace(
            prefix=cfg.prefix,
            char_prompt=cfg.char_prompt,
            profile=cfg.profile,
            custom_neg=cfg.custom_neg,
            positive=cfg.positive,
            negative=cfg.negative,
            mode=mode,
            codes=codes_expr,
            ref_image=None,
            ref_weight=cfg.ref_weight if cfg.ref_weight is not None else REF_WEIGHT_DEFAULT,
            no_ref=False,
            cn_module=None,
            cn_model=None,
            dry_run=dry_run,
            mock=mock,
        )

        try:
            code = execute(args, base_dir)
            (succeeded if code == 0 else failed).append(name)
        except ConfigError as e:
            print(f"[ERROR] {name}: {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            failed.append(name)
        except KeyboardInterrupt:
            print(f"\n[중단] {name} 처리 중 취소되었습니다.", file=sys.stderr)
            failed.append(name)
            break

    # 전체 요약
    print(f"\n{'=' * 64}")
    print(f"[ALL-CHARS] 완료: {len(succeeded)}명 성공 / {len(failed)}명 실패 (총 {total}명)")
    if succeeded:
        print(f"  성공: {succeeded}")
    if failed:
        print(f"  실패: {failed}")
    print()

    return 1 if failed else 0


def apply_character_to_args(cfg: CharacterConfig, args: argparse.Namespace) -> None:
    """
    CharacterConfig 값을 args 에 채운다. 커맨드라인 명시값이 있으면 건드리지 않는다.

    args 를 직접 변경하는 이유: argparse.Namespace 는 setattr 로 조작하는 것이
    공식 패턴이며, 새 객체를 만들면 모든 필드를 복사해야 해 유지보수가 어렵다.
    """
    # prefix: --prefix 가 없으면 캐릭터 파일의 값으로 채운다
    if not args.prefix:
        args.prefix = cfg.prefix

    # char_prompt: --char_prompt 가 없으면 캐릭터 파일의 값으로 채운다
    if not args.char_prompt:
        args.char_prompt = cfg.char_prompt

    # profile: --profile 이 없으면 캐릭터 파일의 값으로 채운다
    if args.profile is None and cfg.profile:
        args.profile = cfg.profile

    # custom_neg: 양쪽을 합친다. 중복이 있어도 WebUI 가 알아서 처리한다.
    if cfg.custom_neg:
        existing = (args.custom_neg or "").strip()
        args.custom_neg = join_tags(existing, cfg.custom_neg) if existing else cfg.custom_neg

    # ref_weight: --ref_weight 가 기본값 그대로이고 캐릭터에 지정값이 있으면 채운다.
    if cfg.ref_weight is not None and args.ref_weight == REF_WEIGHT_DEFAULT:
        args.ref_weight = cfg.ref_weight

    # positive/negative: 캐릭터 json 에 있으면 args 에 심는다.
    # execute() 는 args.positive 유무로 분기한다.
    if cfg.positive and not getattr(args, "positive", None):
        args.positive = cfg.positive
    if cfg.negative and not getattr(args, "negative", None):
        args.negative = cfg.negative


# ─────────────────────────────────────────────
# 4. 프롬프트 DB 파싱
# ─────────────────────────────────────────────
def _iter_sections(raw: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    """주석 섹션(_ 시작)을 걸러 순회한다 (R2.6)."""
    for name, body in raw.items():
        if not name.startswith(SECTION_COMMENT_PREFIX):
            yield name, body


def _parse_profiles(raw: dict[str, Any], warnings: list[str]) -> dict[str, Profile]:
    """_profiles 섹션을 Profile 매핑으로 변환한다. 문제 항목은 경고 후 건너뛴다."""
    section = raw.get(PROFILES_KEY)
    if section is None:
        return {}
    if not isinstance(section, dict):
        warnings.append(f"'{PROFILES_KEY}' 가 딕셔너리가 아님 - 프로필 무시")
        return {}

    profiles: dict[str, Profile] = {}
    for name, body in section.items():
        if not isinstance(body, dict):
            warnings.append(f"프로필 '{name}' 이 딕셔너리가 아님 - 무시")
            continue

        positive = body.get(PROFILE_POSITIVE_KEY)
        negative = body.get(PROFILE_NEGATIVE_KEY, "")

        if not isinstance(positive, str) or not positive.strip():
            warnings.append(
                f"프로필 '{name}' 에 {PROFILE_POSITIVE_KEY} 가 없거나 비어 있음 - 무시"
            )
            continue
        if not isinstance(negative, str):
            warnings.append(f"프로필 '{name}' 의 {PROFILE_NEGATIVE_KEY} 가 문자열이 아님 - 빈 값 사용")
            negative = ""

        profiles[name] = Profile(name, positive.strip(), negative.strip())

    return profiles


def parse_pose_db(raw: dict[str, Any]) -> PoseDatabase:
    """
    최상위 섹션 딕셔너리를 PoseDatabase 로 정규화한다.

    파일 I/O 도 프로세스 종료도 하지 않는 순수 함수. --test 에서 직접 호출한다.
    """
    db = PoseDatabase()
    db.profiles = _parse_profiles(raw, db.warnings)

    for section, body in _iter_sections(raw):
        if not isinstance(body, dict):
            db.warnings.append(f"섹션 '{section}' 이 딕셔너리가 아님 - 무시")
            continue

        section_codes: list[int] = []
        for key, value in body.items():
            try:
                code = int(key)
            except (TypeError, ValueError):
                db.warnings.append(
                    f"섹션 '{section}' 의 키 '{key}' 는 정수가 아님 - 무시"
                )
                continue

            if not isinstance(value, str) or not value.strip():
                db.warnings.append(f"코드 {key} 의 프롬프트가 비어 있음 - 무시")
                continue

            if code in db.entries:
                previous = db.entries[code].section
                db.warnings.append(
                    f"코드 {code} 중복 정의 ('{previous}' -> '{section}') - 나중 값 사용"
                )

            db.entries[code] = PoseEntry(code, value.strip(), section)
            section_codes.append(code)

        db.sections[section] = sorted(section_codes)

    return db


def read_pose_json(base_dir: Path) -> dict[str, Any]:
    """
    pose_database.json 을 읽어 원본 딕셔너리를 반환한다.

    Raises:
        ConfigError: 파일 부재, 문법 오류, 최상위 타입 불일치.
    """
    db_path = base_dir / POSE_DB_FILE

    try:
        text = db_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"프롬프트 DB 파일을 찾을 수 없습니다: {db_path}",
            f"{POSE_DB_FILE} 을 스크립트와 같은 폴더에 두세요.",
        ) from None
    except OSError as e:
        raise ConfigError(f"프롬프트 DB 파일을 읽을 수 없습니다: {e}") from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"JSON 문법 오류: {db_path}",
            f"line {e.lineno}, column {e.colno}: {e.msg}",
        ) from None

    if not isinstance(raw, dict):
        raise ConfigError(
            "JSON 최상위는 섹션 딕셔너리여야 합니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )

    return raw


def load_pose_db(base_dir: Path) -> PoseDatabase:
    """JSON 을 읽고 검증까지 완료한 PoseDatabase 를 반환한다."""
    db = parse_pose_db(read_pose_json(base_dir))

    if not db.entries:
        raise ConfigError(
            "유효한 프롬프트 항목이 없습니다.",
            '예: {"emotions": {"00": "standing, smile"}}',
        )

    return db


def peek_choices(base_dir: Path) -> tuple[list[str], list[str]]:
    """
    --help 문구용 (섹션명, 프로필명) 목록.

    argparse 의 choices 는 파서 생성 시점에 확정되므로 쓸 수 없지만,
    help 문구는 JSON 을 먼저 읽어 동적으로 채울 수 있다. 읽기에 실패해도
    --help 자체는 동작해야 하므로 예외를 내지 않는다.
    """
    try:
        raw = read_pose_json(base_dir)
    except ConfigError:
        return [], []

    sections = [name for name, body in _iter_sections(raw) if isinstance(body, dict)]
    profiles = list(_parse_profiles(raw, []))
    return sections, profiles


def print_warnings(db: PoseDatabase) -> None:
    """로드 경고를 생성 로그 시작 전에 한 번에 출력한다."""
    if not db.warnings:
        return
    for message in db.warnings:
        print(f"[WARN] {message}")
    print()


def resolve_profile(db: PoseDatabase, requested: str | None) -> Profile:
    """
    --profile 값을 Profile 로 해석한다.

    _profiles 가 없는 JSON 에서는 스크립트 하드코딩값을 가상 프로필로 감싸
    기존 동작을 그대로 유지한다.

    Raises:
        ConfigError: 요청한 프로필이 정의되지 않았을 때.
    """
    if not db.profiles:
        if requested:
            raise ConfigError(
                f"프로필 '{requested}' 을 쓸 수 없습니다. "
                f"{POSE_DB_FILE} 에 '{PROFILES_KEY}' 섹션이 없습니다.",
                f"'{PROFILES_KEY}' 를 추가하거나 --profile 을 생략하세요.",
            )
        return Profile(FALLBACK_PROFILE, POS_BASE, COMMON_NEG)

    if requested:
        if requested not in db.profiles:
            raise ConfigError(
                f"알 수 없는 프로필 '{requested}'. 사용 가능: {db.profile_names}",
                f"{POSE_DB_FILE} 의 '{PROFILES_KEY}' 섹션을 확인하세요.",
            )
        return db.profiles[requested]

    if DEFAULT_PROFILE in db.profiles:
        return db.profiles[DEFAULT_PROFILE]

    # 기본 프로필명이 없으면 정의 순서상 첫 프로필로 폴백한다.
    return next(iter(db.profiles.values()))


# ─────────────────────────────────────────────
# 5. 코드 표현식 파싱 및 대상 결정
# ─────────────────────────────────────────────
def _parse_code_token(token: str) -> Iterable[int]:
    """단일 토큰('7' 또는 '10-14')을 코드로 확장한다."""
    if "-" in token:
        start_text, _, end_text = token.partition("-")
        start, end = int(start_text.strip()), int(end_text.strip())
        if start > end:
            start, end = end, start  # 역순 입력 허용
        if start < 0:
            raise ValueError(f"음수 코드는 허용되지 않습니다: {token}")
        if end > MAX_CODE:
            raise ValueError(f"코드 상한({MAX_CODE})을 초과했습니다: {token}")
        return range(start, end + 1)

    code = int(token)
    if not 0 <= code <= MAX_CODE:
        raise ValueError(f"코드는 0~{MAX_CODE} 범위여야 합니다: {token}")
    return (code,)


def parse_codes_expr(expr: str) -> list[int]:
    """
    코드 표현식을 정수 리스트로 변환한다.

    지원: "20-29"(범위), "0,3,7"(열거), "0-5,10,20-22"(혼합)
    역순 범위는 교정하고, 중복은 정규화한다.

    Raises:
        ValueError: 정수로 해석할 수 없거나 0~MAX_CODE 범위를 벗어날 때.
    """
    codes: set[int] = set()
    for raw_token in expr.split(","):
        token = raw_token.strip()
        if token:
            codes.update(_parse_code_token(token))
    return sorted(codes)


def looks_like_code_expr(value: str) -> bool:
    """숫자·콤마·하이픈·공백만으로 구성되면 코드 표현식으로 간주한다 (R2.7)."""
    return bool(value) and CODE_EXPR_PATTERN.match(value) is not None


def _pick_code_expr(mode: str, codes_expr: str | None) -> str | None:
    """--codes 와 --mode 중 코드 표현식으로 쓸 값을 고른다 (R2.9)."""
    if codes_expr:
        if mode and mode != "all" and looks_like_code_expr(mode):
            print(f"[WARN] --codes 가 우선합니다. --mode '{mode}' 무시됨")
        return codes_expr
    if mode and looks_like_code_expr(mode):
        return mode  # R2.7 — 코드 리스트 직접 지정
    return None


def resolve_targets(
    db: PoseDatabase, mode: str, codes_expr: str | None = None
) -> list[int]:
    """
    --codes / --mode 를 해석해 순회 대상 코드를 반환한다.

    Raises:
        ConfigError: 표현식 구문 오류 또는 미등록 모드.
    """
    expr = _pick_code_expr(mode, codes_expr)

    if expr is not None:
        try:
            requested = parse_codes_expr(expr)
        except ValueError as e:
            raise ConfigError(
                f"코드 표현식을 해석할 수 없습니다: '{expr}' ({e})",
                "예: 20-29 / 0,3,7 / 0-5,10,20-22",
            ) from None

        if missing := [code for code in requested if code not in db.entries]:
            print(f"[WARN] DB에 없는 코드 무시: {missing}")
        return [code for code in requested if code in db.entries]

    if mode == "all":
        return db.all_codes
    if mode in db.sections:
        return db.sections[mode]

    raise ConfigError(
        f"알 수 없는 모드 '{mode}'. 사용 가능: {['all'] + sorted(db.sections)}",
        "코드 리스트 직접 지정도 가능합니다. 예: --mode 0,5,12 / --mode 10-14",
    )


# ─────────────────────────────────────────────
# 6. 가변 폭 코드 포맷팅
# ─────────────────────────────────────────────
def code_width(codes: Sequence[int]) -> int:
    """최대 코드 자릿수에 맞춘 패딩 폭. 최소 2자리로 하위 호환 유지 (R3.1)."""
    if not codes:
        return MIN_CODE_WIDTH
    return max(MIN_CODE_WIDTH, len(str(max(codes))))


def format_code(code: int, width: int) -> str:
    """코드 제로 패딩 단일 진입점."""
    return f"{code:0{width}d}"


def asset_filename(prefix: str, code: int, width: int) -> str:
    """파일명 조립 단일 진입점. 루프·스킵 판정·마크다운이 모두 이걸 쓴다 (R3.3)."""
    return f"{prefix}_{format_code(code, width)}.webp"


# ─────────────────────────────────────────────
# 6A. 참조 이미지 해석
# ─────────────────────────────────────────────
def load_reference(path: Path) -> ReferenceImage:
    """
    참조 이미지를 읽어 검증하고 base64 인코딩한다.

    원본 바이트를 그대로 인코딩한다. Pillow 로 재인코딩하지 않는 이유는
    불필요한 손실과 시간이 발생하고, WebUI 가 PNG/JPEG/WebP 를 모두 받기 때문이다.

    Raises:
        ConfigError: 읽을 수 없거나 유효한 이미지가 아닐 때.
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"참조 이미지를 읽을 수 없습니다: {path}", str(e)) from None

    try:
        # verify() 이후에는 이미지 객체를 재사용할 수 없고 크기도 얻을 수 없어
        # 두 번 열어야 한다. Pillow 의 알려진 특성이다.
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as e:  # noqa: BLE001 — Pillow 가 다양한 예외를 던진다
        raise ConfigError(
            f"유효한 이미지 파일이 아닙니다: {path}", str(e)
        ) from None

    return ReferenceImage(
        path=path,
        b64=base64.b64encode(data).decode("ascii"),
        width=width,
        height=height,
    )


def find_reference_candidates(base_dir: Path, prefix: str) -> list[Path]:
    """references/{prefix}.{ext} 를 우선순위 순서로 찾아 존재하는 것만 반환한다."""
    ref_dir = base_dir / REFERENCES_DIRNAME
    if not ref_dir.is_dir():
        return []
    return [
        candidate
        for ext in REFERENCE_EXTENSIONS
        if (candidate := ref_dir / f"{prefix}{ext}").is_file()
    ]


def resolve_reference_image(
    base_dir: Path,
    prefix: str,
    explicit_path: str | None = None,
    disabled: bool = False,
) -> ReferenceImage | None:
    """
    참조 이미지를 해석한다. 없으면 None 을 반환한다.

    명시 지정과 자동 탐색의 실패 처리를 다르게 한다.
    --ref_image 를 적었다면 그 파일을 쓰겠다는 명확한 의사표시이므로 조용히
    무시하지 않는다. 자동 탐색은 "있으면 쓰고 없으면 넘어가는" 편의 기능이며,
    00번을 먼저 생성해 참조로 쓰는 워크플로우에서는 부재가 정상 상태다.

    Raises:
        ConfigError: --ref_image 로 명시한 경로가 없거나 유효하지 않을 때.
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
                f"자동 탐색을 쓰려면 {REFERENCES_DIRNAME}/{prefix}.png 로 두고 "
                "--ref_image 를 생략하세요.",
            )
        return load_reference(path)

    candidates = find_reference_candidates(base_dir, prefix)
    if not candidates:
        return None

    if len(candidates) > 1:
        ignored = [p.name for p in candidates[1:]]
        print(f"[WARN] 참조 이미지가 여러 개입니다. '{candidates[0].name}' 사용, "
              f"무시됨: {ignored}")

    return load_reference(candidates[0])


# ─────────────────────────────────────────────
# 7. API 유틸리티
# ─────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_session() -> requests.Session:
    """
    HTTP 세션을 재사용한다.

    배치당 수십 건을 순차 요청하므로 커넥션을 유지하면 매 요청의
    TCP 핸드셰이크가 사라진다.
    """
    return requests.Session()


def resolve_sampler() -> str:
    """
    WebUI가 지원하는 샘플러 목록과 대조해 사용 가능한 첫 후보를 반환한다.

    후보 목록과 폴백 순서는 검증된 동작이므로 변경하지 않는다 (R5.2).
    """
    try:
        response = get_session().get(SAMPLERS_URL, timeout=SAMPLERS_TIMEOUT)
        response.raise_for_status()
        available = {item["name"] for item in response.json()}
    except Exception:  # noqa: BLE001 — 조회 실패는 기본값으로 진행한다
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
    """
    PNG 바이트를 Pillow로 열어 실제 WebP로 인코딩 저장한다.

    변환 파라미터(RGBA/P -> RGB, quality, method)는 검증된 값이라 그대로 둔다 (R5.1).
    쓰기만 임시 파일 경유로 바꿨다. 중단 시 반쪽 파일이 남으면 재개 로직이
    그것을 '완성된 파일'로 보고 건너뛰기 때문이다.
    """
    partial = save_path.with_name(save_path.name + PARTIAL_SUFFIX)

    image = Image.open(io.BytesIO(png_bytes))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    try:
        image.save(partial, format="WEBP", quality=quality, method=WEBP_METHOD)
        os.replace(partial, save_path)  # 같은 볼륨 내 원자적 교체
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def build_txt2img_payload(
    *, prompt: str, negative_prompt: str, sampler_name: str
) -> dict[str, Any]:
    """
    txt2img 페이로드를 조립한다 (순수 함수).

    조립과 전송을 분리한 이유: 전송이 섞여 있으면 페이로드 구조를 검증하려고
    HTTP 를 가로채야 한다. 분리하면 --test 에서 딕셔너리를 직접 검사할 수 있다.
    """
    return {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": IMAGE_SIZE[0],
        "height": IMAGE_SIZE[1],
        "steps": STEPS,
        "batch_size": 1,
        "n_iter": 1,
        "cfg_scale": CFG_SCALE,
        "sampler_name": sampler_name,
    }


def build_controlnet_unit(
    reference: ReferenceImage, spec: ControlNetSpec, weight: float
) -> dict[str, Any]:
    """
    ControlNet 단일 유닛을 조립한다 (순수 함수).

    resize_mode / control_mode / pixel_perfect 는 WebUI 기본값과 같지만
    명시한다. 버전에 따라 기본값이 바뀌어도 동작이 흔들리지 않게 하려는 의도다.
    """
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


def inject_controlnet(
    payload: dict[str, Any], unit: dict[str, Any]
) -> dict[str, Any]:
    """
    페이로드에 ControlNet 유닛을 주입한 새 딕셔너리를 반환한다 (순수 함수).

    원본을 변경하지 않는다. 루프에서 페이로드를 재사용할 때 상태가 누적되는
    것을 막기 위한 계약이다.

    참조 이미지나 spec 이 없을 때는 이 함수를 호출하지 않는다. 빈
    alwayson_scripts 를 넣으면 WebUI 가 "ControlNet 비활성" 이 아니라
    "인자 부족" 으로 해석할 수 있다.
    """
    merged = dict(payload)
    merged["alwayson_scripts"] = {"controlnet": {"args": [unit]}}
    return merged


def match_model_name(
    available: Sequence[str], patterns: Sequence[str]
) -> str | None:
    """
    사용 가능 목록에서 패턴을 부분 문자열로 찾는다 (순수 함수).

    모델명이 "ip-adapter_xl [4209e9f7]" 처럼 해시를 포함해 완전 일치가
    불가능하므로 부분 매칭을 쓴다. 패턴 순서가 우선순위다.
    """
    lowered = [(name, name.lower()) for name in available]
    for pattern in patterns:
        needle = pattern.lower()
        for original, low in lowered:
            if needle in low:
                return original
    return None


def _fetch_controlnet_list(url: str, key: str) -> list[str]:
    """ControlNet 목록 엔드포인트를 조회한다."""
    response = get_session().get(url, timeout=CONTROLNET_LIST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    return list(data.get(key) or [])


def resolve_controlnet_spec(
    manual_module: str | None = None, manual_model: str | None = None
) -> ControlNetSpec | None:
    """
    ControlNet 전처리기와 모델을 해석한다. 실패하면 None 을 반환한다.

    둘 다 수동 지정되면 조회를 생략한다. GPU 가 없는 환경에서 주입 경로를
    검증할 때의 우회로이기도 하다.

    조회나 매칭이 실패해도 예외를 올리지 않는다. ControlNet 미설치 환경에서도
    텍스트 프롬프트만으로 생성이 계속되어야 한다.
    """
    if manual_module and manual_model:
        return ControlNetSpec(manual_module, manual_model, "manual")

    try:
        modules = _fetch_controlnet_list(CN_MODULES_URL, "module_list")
        models = _fetch_controlnet_list(CN_MODELS_URL, "model_list")
    except Exception:  # noqa: BLE001 — 미설치/미실행 모두 동일하게 처리
        print("[WARN] ControlNet 목록 조회 실패 - 참조 이미지 없이 생성합니다")
        print("       ControlNet 확장이 설치되어 있는지 확인하세요.")
        return None

    module = manual_module or match_model_name(modules, IP_ADAPTER_MODULE_PATTERNS)
    model = manual_model or match_model_name(models, IP_ADAPTER_MODEL_PATTERNS)

    if not module or not model:
        # 목록 전체를 출력한다. GPU 환경에서 이 출력만 보고 바로
        # --cn_module / --cn_model 을 지정할 수 있게 하려는 것이다.
        print("[WARN] IP-Adapter 모듈/모델을 찾지 못했습니다.")
        print(f"       모듈 후보: {IP_ADAPTER_MODULE_PATTERNS} -> {module or '없음'}")
        print(f"       모델 후보: {IP_ADAPTER_MODEL_PATTERNS} -> {model or '없음'}")
        print(f"       사용 가능 모듈 ({len(modules)}): {modules}")
        print(f"       사용 가능 모델 ({len(models)}): {models}")
        print("       --cn_module / --cn_model 로 직접 지정하세요.")
        return None

    return ControlNetSpec(module, model, "auto")


def extract_vram_peak(payload: dict[str, Any]) -> tuple[float, float] | None:
    """
    /sdapi/v1/memory 응답에서 (피크 사용량 GiB, 전체 GiB) 를 뽑는다 (순수 함수).

    응답 구조가 WebUI 버전과 Forge 여부에 따라 다르므로 여러 키를 순차
    탐색한다. 어느 것도 찾지 못하면 None 을 반환하고 호출부는 조용히 넘어간다.
    VRAM 표시는 부가 정보이며 이것 때문에 배치가 실패하면 안 된다.
    """
    cuda = payload.get("cuda")
    if not isinstance(cuda, dict):
        return None

    total = (cuda.get("system") or {}).get("total")
    if not isinstance(total, (int, float)) or total <= 0:
        return None

    peak: float | None = None
    # 1) 최상위 스칼라 형태
    for key in ("reserved_peak", "active_peak"):
        value = cuda.get(key)
        if isinstance(value, (int, float)):
            peak = float(value)
            break
    # 2) 중첩 딕셔너리 형태
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
    """VRAM 피크를 조회한다. 실패하면 None (배치에 영향 없음)."""
    try:
        response = get_session().get(MEMORY_URL, timeout=MEMORY_TIMEOUT)
        response.raise_for_status()
        return extract_vram_peak(response.json())
    except Exception:  # noqa: BLE001 — 부가 정보이므로 조용히 포기한다
        return None


def generate_image(payload: dict[str, Any]) -> bytes:
    """조립된 페이로드를 전송해 PNG 바이트를 받는다."""
    response = get_session().post(API_URL, json=payload, timeout=TXT2IMG_TIMEOUT)
    response.raise_for_status()

    images = response.json().get("images") or []
    if not images:
        # 명시적으로 걸러야 KeyError/IndexError 대신 읽을 수 있는 메시지가 남는다.
        raise RuntimeError("API 응답에 images 가 없습니다")

    return base64.b64decode(images[0])


# ─────────────────────────────────────────────
# 7A. 태그 역추출 (--from_image)
# ─────────────────────────────────────────────
def build_interrogate_payload(b64: str, model: str) -> dict[str, str]:
    """interrogate 페이로드를 조립한다 (순수 함수)."""
    return {"image": b64, "model": model}


def filter_gender_tags(tags: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    성별·인원 태그를 분리한다 (순수 함수).

    Returns:
        (유지할 태그, 제거된 태그)

    성별은 _profiles 축에서 결정되므로 --char_prompt 에 들어가면 프로필과
    충돌한다. 경고만 하지 않고 필터링된 버전을 함께 제시하는 이유는,
    사용자가 출력을 그대로 복사해 쓸 가능성이 높기 때문이다.
    """
    kept: list[str] = []
    removed: list[str] = []
    for tag in tags:
        (removed if normalize_tag(tag) in GENDER_TAGS else kept).append(tag)
    return kept, removed


def run_interrogate(base_dir: Path, image_path: str, model: str) -> int:
    """
    참조 이미지에서 태그를 역추출해 출력한다. 생성은 하지 않는다.

    Returns:
        종료 코드.
    """
    reference = resolve_reference_image(base_dir, "_", explicit_path=image_path)
    if reference is None:  # explicit_path 가 있으면 도달하지 않는 경로
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
        print("        webui-user.bat 에 --api 를 넣고 실행했는지 확인하세요.",
              file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n[ERROR] interrogate 실패: {e}", file=sys.stderr)
        if model == "deepdanbooru":
            print("        DeepBooru 모델이 없으면 --interrogator clip 을 시도하세요.",
                  file=sys.stderr)
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
    print(f'python {Path(__file__).name} --prefix PREFIX '
          f'--char_prompt "{result.filtered}"')
    print(f"{SEPARATOR}\n")
    return 0


# ─────────────────────────────────────────────
# 8. 모의 이미지 생성 (--mock)
# ─────────────────────────────────────────────
def _hue_color(code: int) -> tuple[int, int, int]:
    """코드값으로 배경색을 분산시켜 이미지 구분이 육안으로 가능하게 한다."""
    red, green, blue = colorsys.hsv_to_rgb(((code * 37) % 360) / 360.0, 0.35, 0.90)
    return int(red * 255), int(green * 255), int(blue * 255)


@lru_cache(maxsize=1)
def _mock_fonts() -> tuple[FontLike, FontLike]:
    """
    더미 이미지용 폰트를 한 번만 로드해 재사용한다.

    캐시가 없으면 배치 장수만큼 truetype 파일을 반복해서 읽는다.
    폰트가 없는 환경에서도 예외 없이 완주해야 한다 (R7.3).
    """
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
    """
    API 반환값과 동일한 형태(PNG 바이트열)의 더미 이미지를 즉석 생성한다.

    반환 형식을 PNG로 맞추는 것이 핵심이다. 이렇게 하면 save_as_webp() 를
    우회하지 않으므로 Pillow 변환 로직까지 검증 범위에 들어온다 (R7.4).
    """
    font_big, font_small = _mock_fonts()

    image = Image.new("RGB", MOCK_SIZE, _hue_color(code))
    draw = ImageDraw.Draw(image)

    # 줄 간격을 폰트 객체 동일성으로 판단하면, 폴백 시 두 폰트가 같은 객체가 되어
    # 모든 줄이 큰 간격을 쓰게 된다. 간격을 데이터로 명시해 그 결합을 끊는다.
    rows: tuple[tuple[str, FontLike, int], ...] = (
        (format_code(code, width), font_big, MOCK_GAP_BIG),
        (prefix, font_small, MOCK_GAP_SMALL),
        (entry.section, font_small, MOCK_GAP_SMALL),
        (entry.label[:MOCK_LABEL_MAXLEN], font_small, MOCK_GAP_SMALL),
        # 참조가 적용된 mock 산출물을 육안으로 구분할 수 있게 한다.
        ("MOCK +REF" if reference else "MOCK", font_small, MOCK_GAP_SMALL),
    )

    y = MOCK_TEXT_TOP
    for text, font, gap in rows:
        draw.text((MOCK_TEXT_X, y), text, fill=MOCK_TEXT_COLOR, font=font)
        y += gap

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ─────────────────────────────────────────────
# 9. 배치 실행
# ─────────────────────────────────────────────
def run_batch(
    *,
    prefix: str,
    base_positive: str,
    char_prompt: str,
    negative_prompt: str,
    targets: Sequence[int],
    db: PoseDatabase,
    save_dir: Path,
    width: int,
    sampler_name: str,
    reference: ReferenceImage | None = None,
    cn_spec: ControlNetSpec | None = None,
    ref_weight: float = REF_WEIGHT_DEFAULT,
    dry_run: bool = False,
    mock: bool = False,
) -> BatchResult:
    result = BatchResult(dry_run=dry_run)

    # 참조 이미지와 ControlNet 해석이 둘 다 성공했을 때만 주입한다.
    # 참조는 있는데 ControlNet 이 없으면(미설치 등) 텍스트만으로 생성한다.
    controlnet_unit = (
        build_controlnet_unit(reference, cn_spec, ref_weight)
        if reference and cn_spec
        else None
    )

    for code in targets:
        entry = db.entries[code]
        tag = format_code(code, width)
        filename = asset_filename(prefix, code, width)
        save_path = save_dir / filename

        # dry-run 검사를 존재 확인보다 먼저 둔다.
        # 순서가 뒤바뀌면 이미 생성된 파일이 skipped 로 빠져 planned 가 불완전해진다.
        if dry_run:
            result.planned.append(code)
            print(f"  [{tag}] (계획) {filename}  <- {entry.label}")
            continue

        if save_path.exists():
            print(f"  [{tag}] 이미 존재 (건너뜀) -> {filename}")
            result.skipped.append(code)
            continue

        print(f"  [{tag}] {'모의 생성' if mock else '생성'} 중... ", end="", flush=True)

        started = time.perf_counter()
        try:
            full_prompt = join_tags(
                base_positive, char_prompt, entry.prompt, f"{prefix}_{tag}"
            )
            # 페이로드는 mock 에서도 조립한다. 조립 오류는 mock 에서 잡아야
            # 할 결함이므로 전송만 생략한다.
            payload = build_txt2img_payload(
                prompt=full_prompt,
                negative_prompt=negative_prompt,
                sampler_name=sampler_name,
            )
            if controlnet_unit is not None:
                payload = inject_controlnet(payload, controlnet_unit)

            if mock:
                png_bytes = make_dummy_png(prefix, code, width, entry, reference)
            else:
                png_bytes = generate_image(payload)
            save_as_webp(png_bytes, save_path)
        except requests.exceptions.ConnectionError:
            # WebUI 가 죽은 상태에서 남은 코드를 계속 시도하면 대기만 누적된다.
            print("실패: WebUI 연결 불가 (--api 옵션 실행 여부 확인)")
            result.failed.append((code, "connection"))
            result.aborted = True
            break
        except Exception as e:  # noqa: BLE001 — 개별 실패는 배치를 막지 않는다
            print(f"실패: {e}")
            result.failed.append((code, str(e)))
            continue

        elapsed = time.perf_counter() - started
        result.durations.append((code, elapsed))
        print(f"완료 ({elapsed:.1f}초) -> {filename}")
        result.success.append(code)

    return result


# ─────────────────────────────────────────────
# 10. 젠잇 마크다운 조립
# ─────────────────────────────────────────────
def mode_badge(dry_run: bool, mock: bool) -> str:
    if dry_run:
        return " [DRY-RUN]"
    if mock:
        return " [MOCK]"
    return ""


def build_section_guide(
    db: PoseDatabase, codes: Sequence[int], prefix: str, width: int
) -> str:
    """JSON 실제 구성에서 상태 매핑 가이드를 유도한다 (R4.7)."""
    present = set(codes)
    blocks: list[str] = []

    for section, section_codes in db.sections.items():
        available = [code for code in section_codes if code in present]
        if not available:
            continue
        rows = "\n".join(
            f"  {db.entries[code].label:<26} -> {asset_filename(prefix, code, width)}"
            for code in available
        )
        blocks.append(f"[{section}]\n{rows}")

    return "\n\n".join(blocks)


def build_genit_block(
    prefix: str,
    codes: Sequence[int],
    db: PoseDatabase,
    width: int,
    badge: str = "",
) -> str:
    """
    젠잇 복사용 마크다운 블록을 조립해 문자열로 반환한다.

    출력이 아니라 반환으로 둔 이유: --test 에서 stdout 캡처 없이
    라인 수와 리터럴 포함 여부를 직접 검사할 수 있어야 한다 (R8.7).
    """
    urls = [f"{URL_PLACEHOLDER}{prefix}/{asset_filename(prefix, c, width)}" for c in codes]

    calls = "\n".join(f"![image]({url})" for url in urls)
    files = "\n".join(
        f"- `{url}` ({db.entries[code].label})" for code, url in zip(codes, urls)
    )
    guide = build_section_guide(db, codes, prefix, width)
    status = GENIT_STATUS_TEMPLATE.format(
        name=prefix, title="직책입력", status="현재상태", desc="대사한줄"
    )

    return f"""
{SEPARATOR}
  젠잇(Genit) 복사용 에셋 블록 | {prefix}   (총 {len(codes)}개){badge}
{SEPARATOR}

### {prefix} 이미지 호출 코드
{calls}

### {prefix} 파일 목록
{files}

### {prefix} 상태 매핑 가이드
{guide}

### {prefix} 상태창 템플릿
{status}
{SEPARATOR}
"""


# ─────────────────────────────────────────────
# 11. 자체 검증 (--test)
# ─────────────────────────────────────────────
PARSER_CASES: tuple[tuple[str, list[int]], ...] = (
    ("20-29", list(range(20, 30))),
    ("0,3,7", [0, 3, 7]),
    ("0-5,10,20-22", [0, 1, 2, 3, 4, 5, 10, 20, 21, 22]),
    ("29-20", list(range(20, 30))),  # 역순 교정
    ("0-5,3", [0, 1, 2, 3, 4, 5]),  # 중복 정규화
    (" 1 , 2 ", [1, 2]),  # 공백 허용
)

REJECT_EXPRS: tuple[str, ...] = (
    "abc",
    "1-",
    f"0-{MAX_CODE + 1}",  # 상한 초과
    "-5",
)

UNSAFE_PREFIXES: tuple[str, ...] = (
    "..",
    "../evil",
    "a/b",
    "a\\b",
    'a" & calc & "',
    "",
    "x" * 65,
)

WIDTH_CASES: tuple[tuple[list[int], int], ...] = (
    ([0, 19], 2),
    ([0, 99], 2),
    ([0, 100], 3),
    ([7], 2),
    ([], 2),
)

REF_WEIGHT_REJECT: tuple[float, ...] = (-0.1, 2.1, -1.0, 99.0)
REF_WEIGHT_ACCEPT: tuple[float, ...] = (0.0, 0.5, 0.7, 1.0, 2.0)

# ControlNet 유닛에 반드시 있어야 하는 키
CN_UNIT_REQUIRED_KEYS = frozenset({
    "enabled", "input_image", "module", "model",
    "weight", "resize_mode", "control_mode", "pixel_perfect",
})

# 해시가 붙은 실제 모델명 형태를 모사한 픽스처
CN_MODEL_FIXTURE = (
    "control_v11p_sd15_openpose [cab727d4]",
    "ip-adapter_xl [4209e9f7]",
    "t2iadapter_style_sd14v1 [202e85cc]",
)
CN_MODULE_FIXTURE = ("none", "canny", "openpose_full", "ip-adapter_clip_sdxl")

SYNTHETIC_RAW: dict[str, Any] = {
    "_comment": "self-test fixture",
    "alpha": {"5": "five, tag", "12": "twelve, tag", "03": "three, tag"},
    "beta": {"7": "seven, tag"},
}

# 태그 정규화 케이스: (입력, 기대값)
NORMALIZE_CASES: tuple[tuple[str, str], ...] = (
    ("(huge:1.3)", "huge"),
    ("((tag))", "tag"),
    ("[soft]", "soft"),
    (" Bad   Hands ", "bad hands"),
    ("(masterpiece:1.2)", "masterpiece"),
    ("plain", "plain"),
    ("(weight:-0.5)", "weight"),
)

# 충돌 감지 케이스: (포지티브, 네거티브, 기대 충돌 목록)
CONFLICT_CASES: tuple[tuple[str, str, list[str]], ...] = (
    ("1girl, solo, smile", "1boy, male", []),
    ("1girl, (breasts:1.2)", "breasts, muscular", ["breasts"]),
    ("a, b, c", "C, B", ["b", "c"]),
    ("", "anything", []),
)


def _audit_data_quality(report: TestReport, raw: dict[str, Any]) -> None:
    """T4~T6: 데이터 품질. 런타임과 동일하게 경고로만 처리한다."""
    bad_keys: list[str] = []
    empty_values: list[str] = []
    duplicates: list[str] = []
    seen: dict[int, str] = {}

    for section, body in _iter_sections(raw):
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            try:
                code = int(key)
            except (TypeError, ValueError):
                bad_keys.append(f"{section}/{key}")
                continue
            if not isinstance(value, str) or not value.strip():
                empty_values.append(f"{section}/{key}")
                continue
            if code in seen:
                duplicates.append(f"{code}({seen[code]}->{section})")
            seen[code] = section

    for name, findings in (
        ("T4 비정수 키", bad_keys),
        ("T5 빈 프롬프트", empty_values),
        ("T6 중복 코드", duplicates),
    ):
        if findings:
            report.warn(name, str(findings))
        else:
            report.ok(f"{name} 없음")


def _test_logic(report: TestReport, db: PoseDatabase) -> None:
    """T8~T15: 순수 함수 검증. 실제 구현을 직접 호출한다 (R8.7)."""
    print("\n[로직 검사]")
    synthetic = parse_pose_db(SYNTHETIC_RAW)

    # T8 — 문자열 사전순으로 정렬하면 "10" < "2" 가 되어 순서가 깨진다.
    lexicographic = [int(k) for k in sorted(["5", "12", "03"])]
    report.check(
        "T8 정수 정렬 (사전순 아님)",
        synthetic.all_codes == [3, 5, 7, 12] and lexicographic != [3, 5, 12],
        f"{synthetic.all_codes}, 사전순={lexicographic}",
    )
    report.check(
        "T8b 실제 DB 정렬", db.all_codes == sorted(db.all_codes), str(db.all_codes)
    )

    # T9
    report.check(
        "T9 code_width 산출",
        all(code_width(codes) == expected for codes, expected in WIDTH_CASES),
        str([(codes, code_width(codes)) for codes, _ in WIDTH_CASES]),
    )

    # T10
    failures = []
    for expr, expected in PARSER_CASES:
        try:
            actual = parse_codes_expr(expr)
            if actual != expected:
                failures.append(f"'{expr}'->{actual}!={expected}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"'{expr}' raised {e}")
    report.check(f"T10 parse_codes_expr {len(PARSER_CASES)}케이스", not failures,
                 str(failures) if failures else "")

    report.check(
        "T10b looks_like_code_expr 판별",
        all(map(looks_like_code_expr, ("0,5,12", "10-14")))
        and not any(map(looks_like_code_expr, ("emotions", "all", ""))),
    )

    # T11
    report.check(
        "T11 asset_filename 조립",
        asset_filename("x", 7, 2) == "x_07.webp"
        and asset_filename("x", 7, 3) == "x_007.webp"
        and asset_filename("x", 123, 3) == "x_123.webp",
        asset_filename("x", 7, 2),
    )

    # T12 / T13
    codes = synthetic.all_codes
    block = build_genit_block("t", codes, synthetic, code_width(codes))
    calls = block.count("![image](")
    report.check("T12 마크다운 라인 수 == 대상 수", calls == len(codes),
                 f"{calls}/{len(codes)}")
    report.check("T13 {{url}} 리터럴 포함", URL_PLACEHOLDER in block)

    # T14 — prefix 화이트리스트가 경로 이탈·인용부호 주입을 막는지
    rejected = []
    for unsafe in UNSAFE_PREFIXES:
        try:
            validate_prefix(unsafe)
        except ConfigError:
            continue
        rejected.append(unsafe)
    report.check("T14 위험 prefix 차단", not rejected,
                 f"통과됨: {rejected}" if rejected else f"{len(UNSAFE_PREFIXES)}종 차단")
    report.check(
        "T14b 정상 prefix 허용",
        validate_prefix(" mika ") == "mika" and validate_prefix("test_01") == "test_01",
    )

    # T15 — 상한/음수 표현식 거부
    accepted = []
    for expr in REJECT_EXPRS:
        try:
            parse_codes_expr(expr)
        except ValueError:
            continue
        accepted.append(expr)
    report.check("T15 잘못된 표현식 거부", not accepted,
                 f"통과됨: {accepted}" if accepted else f"{len(REJECT_EXPRS)}종 거부")

    # T16 — 태그 정규화 (괄호·가중치·대소문자·공백)
    bad_norm = [
        f"'{src}'->'{normalize_tag(src)}'!='{expected}'"
        for src, expected in NORMALIZE_CASES
        if normalize_tag(src) != expected
    ]
    report.check(f"T16 normalize_tag {len(NORMALIZE_CASES)}케이스", not bad_norm,
                 str(bad_norm) if bad_norm else "")

    # T17 — 충돌 감지 함수 동작
    bad_conflict = [
        f"({pos!r},{neg!r})->{find_tag_conflicts(pos, neg)}!={expected}"
        for pos, neg, expected in CONFLICT_CASES
        if find_tag_conflicts(pos, neg) != expected
    ]
    report.check(f"T17 find_tag_conflicts {len(CONFLICT_CASES)}케이스",
                 not bad_conflict, str(bad_conflict) if bad_conflict else "")


@contextmanager
def _temp_reference(extensions: Sequence[str] = (".png",)) -> Iterator[Path]:
    """
    임시 참조 이미지를 만들고 정리한다.

    저장소에 테스트용 바이너리를 커밋하지 않기 위해 Pillow 로 즉석 생성한다.
    contextmanager 를 쓰면 검사 실패로 예외가 나도 임시 폴더가 정리된다.
    """
    tmp = Path(tempfile.mkdtemp(prefix="sdref_"))
    try:
        refs = tmp / REFERENCES_DIRNAME
        refs.mkdir()
        for ext in extensions:
            Image.new("RGB", (64, 96), (128, 128, 200)).save(refs / f"t{ext}")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_reference(report: TestReport) -> None:
    """T21~T30: 참조 이미지 및 페이로드 조립 검증. 네트워크 접근 없음."""
    print("\n[참조 이미지 검사]")

    # ── T21: 확장자 우선순위 ──
    with _temp_reference(REFERENCE_EXTENSIONS) as tmp:
        found = find_reference_candidates(tmp, "t")
        report.check(
            "T21 확장자 우선순위",
            len(found) == 4 and found[0].suffix == ".png",
            f"{[p.suffix for p in found]}",
        )
        picked = resolve_reference_image(tmp, "t")
        report.check(
            "T21b .png 채택",
            picked is not None and picked.path.suffix == ".png",
            picked.path.name if picked else "None",
        )

    # ── T22: 참조 부재 ──
    with _temp_reference(()) as tmp:
        try:
            missing = resolve_reference_image(tmp, "nosuch")
            report.check("T22 참조 부재 시 None", missing is None, repr(missing))
        except Exception as e:  # noqa: BLE001
            report.check("T22 참조 부재 시 None", False, f"예외 발생: {e}")

    empty_dir = Path(tempfile.mkdtemp(prefix="sdref_empty_"))
    try:
        report.check(
            "T22b references/ 폴더 자체 부재",
            resolve_reference_image(empty_dir, "t") is None,
        )
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)

    # ── T23: base64 왕복 ──
    with _temp_reference((".png",)) as tmp:
        ref = resolve_reference_image(tmp, "t")
        ok = False
        detail = "참조 로드 실패"
        if ref is not None:
            decoded = base64.b64decode(ref.b64)
            with Image.open(io.BytesIO(decoded)) as img:
                ok = img.size == (ref.width, ref.height) == (64, 96)
                detail = f"{img.size} == ({ref.width}, {ref.height})"
        report.check("T23 base64 왕복", ok, detail)

        # ── T24~T26: 페이로드 조립 ──
        print("\n[페이로드 조립 검사]")
        spec = ControlNetSpec("ip-adapter_clip_sdxl", "ip-adapter_xl [test]", "manual")
        base_payload = build_txt2img_payload(
            prompt="p", negative_prompt="n", sampler_name="s"
        )

        assert ref is not None
        unit = build_controlnet_unit(ref, spec, 0.7)
        missing_keys = CN_UNIT_REQUIRED_KEYS - unit.keys()
        report.check(
            "T24 유닛 필수 키",
            not missing_keys and unit["enabled"] is True and unit["weight"] == 0.7,
            f"누락: {sorted(missing_keys)}" if missing_keys else f"{len(unit)}개 키",
        )
        report.check(
            "T24b 유닛에 base64 이미지 포함",
            unit["input_image"] == ref.b64 and len(unit["input_image"]) > 0,
        )

        report.check(
            "T25 참조 없을 때 alwayson_scripts 미주입",
            "alwayson_scripts" not in base_payload,
            f"키 {len(base_payload)}개",
        )

        injected = inject_controlnet(base_payload, unit)
        try:
            args_list = injected["alwayson_scripts"]["controlnet"]["args"]
            placed = len(args_list) == 1 and args_list[0] is unit
        except (KeyError, TypeError):
            placed = False
        report.check("T26 주입 위치", placed)
        report.check(
            "T26b 원본 페이로드 불변",
            "alwayson_scripts" not in base_payload,
            "inject_controlnet 이 원본을 변경하지 않음",
        )
        report.check(
            "T26c 기존 키 보존",
            all(injected[k] == v for k, v in base_payload.items()),
        )

    # ── T27: weight 범위 ──
    print("\n[검증 로직 검사]")
    wrongly_accepted = []
    for value in REF_WEIGHT_REJECT:
        try:
            validate_ref_weight(value)
            wrongly_accepted.append(value)
        except ConfigError:
            pass
    wrongly_rejected = []
    for value in REF_WEIGHT_ACCEPT:
        try:
            validate_ref_weight(value)
        except ConfigError:
            wrongly_rejected.append(value)
    report.check(
        "T27 ref_weight 범위",
        not wrongly_accepted and not wrongly_rejected,
        f"오통과 {wrongly_accepted} / 오거부 {wrongly_rejected}"
        if (wrongly_accepted or wrongly_rejected)
        else f"거부 {len(REF_WEIGHT_REJECT)}종 / 허용 {len(REF_WEIGHT_ACCEPT)}종",
    )

    # ── T28: interrogate 페이로드 ──
    payload = build_interrogate_payload("BASE64", INTERROGATE_DEFAULT)
    report.check(
        "T28 interrogate 페이로드",
        set(payload) == {"image", "model"}
        and payload["model"] == "deepdanbooru"
        and payload["image"] == "BASE64",
        str(payload | {"image": "..."}),
    )

    # ── T29: 성별 태그 필터 ──
    sample = ["1girl", "solo", "silver hair", "blue eyes", "1boy", "MALE"]
    kept, removed = filter_gender_tags(sample)
    report.check(
        "T29 성별 태그 필터",
        kept == ["silver hair", "blue eyes"] and len(removed) == 4,
        f"유지 {kept} / 제거 {removed}",
    )
    result = InterrogateResult(raw=", ".join(sample), tags=sample, gender_tags=removed)
    report.check(
        "T29b filtered 프로퍼티",
        result.filtered == "silver hair, blue eyes",
        result.filtered,
    )

    # ── T30: 모델명 부분 매칭 ──
    matched_model = match_model_name(CN_MODEL_FIXTURE, IP_ADAPTER_MODEL_PATTERNS)
    matched_module = match_model_name(CN_MODULE_FIXTURE, IP_ADAPTER_MODULE_PATTERNS)
    report.check(
        "T30 해시 포함 모델명 매칭",
        matched_model == "ip-adapter_xl [4209e9f7]"
        and matched_module == "ip-adapter_clip_sdxl",
        f"{matched_model} / {matched_module}",
    )
    report.check(
        "T30b 매칭 실패 시 None",
        match_model_name(("canny", "openpose"), IP_ADAPTER_MODEL_PATTERNS) is None,
    )

    # ── T31: 시간 집계 ──
    stats = summarize_durations([2.0, 4.0, 6.0])
    report.check(
        "T31 시간 집계",
        stats is not None
        and stats.count == 3
        and stats.total == 12.0
        and stats.average == 4.0
        and stats.fastest == 2.0
        and stats.slowest == 6.0,
        stats.format() if stats else "None",
    )
    report.check("T31b 빈 측정값 None", summarize_durations([]) is None)

    batch = BatchResult(durations=[(0, 1.5), (1, 2.5)])
    report.check(
        "T31c BatchResult.timing",
        batch.timing is not None and batch.timing.total == 4.0,
        batch.timing.format() if batch.timing else "None",
    )

    # ── T32: VRAM 파싱 (버전별 응답 구조 대응) ──
    vram_cases: tuple[tuple[str, dict[str, Any], bool], ...] = (
        ("최상위 reserved_peak",
         {"cuda": {"system": {"total": 8 * GIB}, "reserved_peak": 6 * GIB}}, True),
        ("중첩 reserved.peak",
         {"cuda": {"system": {"total": 8 * GIB}, "reserved": {"peak": 5 * GIB}}}, True),
        ("active_peak 폴백",
         {"cuda": {"system": {"total": 8 * GIB}, "active_peak": 4 * GIB}}, True),
        ("cuda 없음", {"ram": {}}, False),
        ("total 없음", {"cuda": {"reserved_peak": 1}}, False),
        ("peak 키 전무", {"cuda": {"system": {"total": 8 * GIB}}}, False),
        ("total 0", {"cuda": {"system": {"total": 0}, "reserved_peak": 1}}, False),
    )
    vram_fail = [
        name
        for name, payload, expect in vram_cases
        if (extract_vram_peak(payload) is not None) != expect
    ]
    report.check(f"T32 VRAM 파싱 {len(vram_cases)}케이스", not vram_fail,
                 str(vram_fail) if vram_fail else "")

    parsed = extract_vram_peak(
        {"cuda": {"system": {"total": 8 * GIB}, "reserved_peak": 6 * GIB}}
    )
    report.check(
        "T32b GiB 환산",
        parsed is not None and abs(parsed[0] - 6.0) < 0.01 and abs(parsed[1] - 8.0) < 0.01,
        f"{parsed[0]:.2f} / {parsed[1]:.2f} GiB" if parsed else "None",
    )


def _test_profiles(report: TestReport, db: PoseDatabase) -> None:
    """T18~T20: 프로필 정의 및 실제 태그 충돌 검사."""
    print("\n[프로필 검사]")

    if not db.profiles:
        report.warn(
            f"T18 '{PROFILES_KEY}' 섹션 없음",
            f"스크립트 하드코딩값으로 폴백합니다. 성별 전환이 필요하면 {PROFILES_KEY} 를 추가하세요",
        )
        conflicts = find_tag_conflicts(POS_BASE, COMMON_NEG)
        if conflicts:
            report.warn("T19 내장 기본값 태그 충돌", str(conflicts))
        else:
            report.ok("T19 내장 기본값 태그 충돌 없음")
        return

    report.ok(f"T18 프로필 {len(db.profiles)}종 로드", str(db.profile_names))

    # T19 — 프로필별 포지티브/네거티브 동일 태그 충돌
    found_any = False
    for name, profile in db.profiles.items():
        conflicts = find_tag_conflicts(profile.base_negative, profile.base_positive)
        if conflicts:
            found_any = True
            report.warn(f"T19 프로필 '{name}' 태그 충돌", str(conflicts))
    if not found_any:
        report.ok("T19 프로필 태그 충돌 없음", f"{len(db.profiles)}종 검사")

    # T20 — 기본 프로필 존재 여부 (--profile 생략 시 예측 가능성)
    if DEFAULT_PROFILE in db.profiles:
        report.ok(f"T20 기본 프로필 '{DEFAULT_PROFILE}' 존재")
    else:
        fallback = next(iter(db.profiles))
        report.warn(
            f"T20 기본 프로필 '{DEFAULT_PROFILE}' 없음",
            f"--profile 생략 시 '{fallback}' 이 쓰입니다",
        )


def _test_characters(report: TestReport, base_dir: Path) -> None:
    """T33~T36: characters/ 폴더 및 각 json 파일 검증."""
    print("\n[캐릭터 프리셋 검사]")

    chars_dir = _characters_dir(base_dir)

    # T33 — 폴더 존재 여부 (없어도 경고만. 폴더 자체는 선택 사항)
    if not chars_dir.is_dir():
        report.warn("T33 characters/ 폴더 없음", "캐릭터 프리셋 미사용 — 건너뜀")
        return
    report.ok("T33 characters/ 폴더 존재", str(chars_dir))

    files = sorted(chars_dir.glob("*.json"))
    if not files:
        report.warn("T34 json 파일 없음", f"{CHARACTERS_DIRNAME}/ 에 파일을 추가하세요")
        return
    report.ok(f"T34 json 파일 {len(files)}개 발견", str([f.name for f in files]))

    # T35 — 각 파일 로드·필수키·prefix 유효성
    load_errors: list[str] = []
    loaded: list[CharacterConfig] = []
    for path in files:
        try:
            cfg = load_character(base_dir, path.stem)
            loaded.append(cfg)
        except ConfigError as e:
            load_errors.append(f"{path.name}: {e}")

    report.check(
        f"T35 파일 로드 성공 ({len(loaded)}/{len(files)})",
        not load_errors,
        str(load_errors) if load_errors else "",
    )

    # T36 — apply_character_to_args 우선순위: 커맨드라인 명시값 보존 확인
    if loaded:
        cfg = loaded[0]
        # 빈 args — 캐릭터 값으로 채워져야 한다
        empty_args = argparse.Namespace(
            prefix=None, char_prompt=None, profile=None, custom_neg="",
            ref_weight=REF_WEIGHT_DEFAULT,
        )
        apply_character_to_args(cfg, empty_args)
        filled_ok = (
            empty_args.prefix == cfg.prefix
            and empty_args.char_prompt == cfg.char_prompt
        )

        # 이미 채워진 args — 덮어쓰지 않아야 한다
        full_args = argparse.Namespace(
            prefix="override", char_prompt="override prompt",
            profile="male", custom_neg="", ref_weight=REF_WEIGHT_DEFAULT,
        )
        apply_character_to_args(cfg, full_args)
        preserved_ok = (
            full_args.prefix == "override"
            and full_args.char_prompt == "override prompt"
            and full_args.profile == "male"
        )

        report.check(
            f"T36 apply_character_to_args 우선순위 ({cfg.name})",
            filled_ok and preserved_ok,
            "빈 args 채움 OK, 명시값 보존 OK" if (filled_ok and preserved_ok)
            else f"채움={filled_ok} 보존={preserved_ok}",
        )


def run_self_test(base_dir: Path) -> int:
    """데이터·로직 자체 진단. 파일 쓰기와 네트워크 요청을 하지 않는다."""
    report = TestReport()

    print(f"\n{SEPARATOR}")
    print("  자체 검증 (--test)")
    print(SEPARATOR)
    print("\n[데이터 검사]")

    db_path = base_dir / POSE_DB_FILE
    if not report.check("T1 JSON 파일 존재", db_path.exists(), str(db_path)):
        return _finish_test(report)

    try:
        raw = read_pose_json(base_dir)
        report.check("T2 JSON 문법", True)
        report.check("T3 최상위 섹션 딕셔너리", True)
    except ConfigError as e:
        report.check("T2/T3 JSON 로드", False, f"{e} {e.hint}".strip())
        return _finish_test(report)

    if non_dict := [n for n, b in _iter_sections(raw) if not isinstance(b, dict)]:
        report.warn("T3b 비-딕셔너리 섹션", str(non_dict))

    _audit_data_quality(report, raw)

    db = parse_pose_db(raw)
    if not report.check("T7 유효 엔트리 1개 이상", bool(db.entries), f"{len(db.entries)}개"):
        return _finish_test(report)

    _test_logic(report, db)
    _test_profiles(report, db)
    _test_reference(report)
    _test_characters(report, base_dir)
    return _finish_test(report)


def _finish_test(report: TestReport) -> int:
    print(f"\n{SEPARATOR}")
    print(f"  결과: PASS {report.passed} / FAIL {report.failed} / WARN {report.warned}")
    print(f"  종료 코드: {report.exit_code}")
    print(f"{SEPARATOR}\n")
    return report.exit_code


# ─────────────────────────────────────────────
# 12. CLI 파서
# ─────────────────────────────────────────────
def build_parser(
    section_names: Sequence[str] | None = None,
    profile_names: Sequence[str] | None = None,
    add_help: bool = True,
) -> argparse.ArgumentParser:
    if section_names:
        mode_help = (
            f"all | 섹션명({', '.join(section_names)}) | 코드 리스트(0,5,12 / 10-14)"
        )
    else:
        mode_help = "all | JSON 섹션명 | 코드 리스트 (0,5,12 / 10-14)"

    if profile_names:
        profile_help = (
            f"캐릭터 프로필: {', '.join(profile_names)} "
            f"(생략 시 '{DEFAULT_PROFILE}')"
        )
    else:
        profile_help = f"캐릭터 프로필 (_profiles 섹션에서 선택, 생략 시 '{DEFAULT_PROFILE}')"

    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description="캐릭터 챗봇용 이미지 에셋 배치 생성기 (SD WebUI)",
        add_help=add_help,
    )
    parser.add_argument(
        "--char", default=None,
        metavar="NAME",
        help=f"{CHARACTERS_DIRNAME}/NAME.json 을 읽어 프리셋 적용. 커맨드라인 인자가 있으면 그쪽 우선",
    )
    parser.add_argument(
        "--list", dest="list_chars", action="store_true",
        help=f"{CHARACTERS_DIRNAME}/ 폴더의 캐릭터 목록 출력 후 종료",
    )
    parser.add_argument(
        "--all-chars", dest="all_chars", action="store_true",
        help=f"{CHARACTERS_DIRNAME}/ 의 모든 캐릭터를 순서대로 생성. 이미 있는 파일은 건너뜀",
    )
    parser.add_argument("--prefix", help="에셋 식별자 (영문·숫자·_·- 1~64자)")
    parser.add_argument("--char_prompt", help="캐릭터 외형 태그")
    parser.add_argument("--custom_neg", default="", help="추가 네거티브 태그 (선택)")
    parser.add_argument("--profile", default=None, help=profile_help)
    parser.add_argument("--mode", default="all", help=mode_help)
    parser.add_argument("--codes", default=None, help="코드 직접 지정 (20-29 / 0,3,7)")

    ref = parser.add_argument_group("참조 이미지 (IP-Adapter)")
    ref.add_argument(
        "--ref_image", default=None,
        help=f"참조 이미지 경로 직접 지정 (생략 시 {REFERENCES_DIRNAME}/{{prefix}}.png 자동 탐색)",
    )
    ref.add_argument(
        "--ref_weight", type=float, default=REF_WEIGHT_DEFAULT,
        help=f"적용 강도 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} (기본 {REF_WEIGHT_DEFAULT})",
    )
    ref.add_argument(
        "--no_ref", action="store_true",
        help="참조 이미지를 무시하고 텍스트 프롬프트만 사용",
    )
    ref.add_argument(
        "--cn_module", default=None,
        help="ControlNet 전처리기 수동 지정 (자동 탐지 실패 시)",
    )
    ref.add_argument(
        "--cn_model", default=None,
        help="ControlNet 모델 수동 지정 (자동 탐지 실패 시)",
    )

    interrogate = parser.add_argument_group("태그 역추출")
    interrogate.add_argument(
        "--from_image", default=None,
        help="이미지에서 태그를 추출해 출력하고 종료 (생성하지 않음)",
    )
    interrogate.add_argument(
        "--interrogator", default=INTERROGATE_DEFAULT, choices=INTERROGATORS,
        help=f"추출 모델 (기본 {INTERROGATE_DEFAULT})",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="파일 쓰기 없이 대상·파일명·마크다운만 출력",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="WebUI 없이 더미 이미지를 생성해 전체 파이프라인 검증",
    )
    parser.add_argument(
        "--test", action="store_true", help="데이터·로직 자체 진단 후 종료"
    )
    return parser


# ─────────────────────────────────────────────
# 13. 실행 흐름
# ─────────────────────────────────────────────
def open_in_explorer(path: Path) -> None:
    """
    결과 폴더를 파일 탐색기로 연다.

    os.system() 대신 os.startfile() 을 쓴다. 셸을 거치지 않으므로
    경로에 특수문자가 있어도 명령이 조립되지 않는다.
    """
    if os.name != "nt":
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows 전용
    except OSError as e:
        print(f"[WARN] 탐색기를 열지 못했습니다: {e}")


def print_summary(
    result: BatchResult,
    save_dir: Path,
    badge: str,
    vram: tuple[float, float] | None = None,
) -> None:
    """실행 요약 리포트."""
    print(
        f"\n[작업 완료]{badge} 성공 {len(result.success)} / "
        f"건너뜀 {len(result.skipped)} / 실패 {len(result.failed)}"
    )
    if result.planned:
        print(f"           계획 {len(result.planned)}건 (파일 미생성)")
    if result.failed:
        print(f"           실패 코드: {result.failed_codes}")
    if result.aborted:
        print("           WebUI 연결이 끊겨 중단되었습니다.")

    # 설정 비교(--medvram-sdxl, xFormers 등)의 판단 근거가 된다.
    if timing := result.timing:
        print(f"[측정] {timing.format()}")
    if vram:
        peak, total = vram
        ratio = peak / total * 100 if total else 0
        print(f"[VRAM] 피크 {peak:.2f} / {total:.2f} GiB ({ratio:.0f}%)")

    print(f"           폴더: {save_dir}")


def execute(args: argparse.Namespace, base_dir: Path) -> int:
    """생성 파이프라인 본체. ConfigError 는 호출자가 처리한다."""
    # 모드 우선순위: dry-run > mock (부작용이 적은 쪽 우선, R7.8)
    dry_run: bool = args.dry_run
    mock: bool = args.mock and not dry_run
    if args.mock and dry_run:
        print("[WARN] --dry-run 이 우선합니다. --mock 무시됨")

    prefix = validate_prefix(args.prefix)
    char_prompt = (args.char_prompt or "").strip()

    db = load_pose_db(base_dir)
    print_warnings(db)

    # ── positive/negative 결정 ─────────────────────────
    # 캐릭터 json 에 positive/negative 가 직접 기재된 경우 프로필을 완전히 무시한다.
    # 없으면 기존 방식(profile.base_positive + char_prompt / profile.base_negative + custom_neg) 폴백.
    raw_positive: str | None = getattr(args, "positive", None)
    raw_negative: str | None = getattr(args, "negative", None)

    if raw_positive:
        base_positive = raw_positive
        negative_prompt = raw_negative or ""
        print(f"[POSITIVE] 캐릭터 전용 프롬프트 사용")
        if raw_negative:
            print(f"[NEGATIVE] 캐릭터 전용 네거티브 사용")
        else:
            print(f"[WARN] 'negative' 미지정 - 네거티브 없이 생성합니다")
    else:
        # 프로필이 스크립트 하드코딩값(POS_BASE / COMMON_NEG)을 완전히 대체한다.
        profile = resolve_profile(db, args.profile)
        if args.profile:
            print(f"[PROFILE] '{profile.name}' 적용")
        else:
            print(f"[PROFILE] 미지정 - 기본값 '{profile.name}' 적용")
        base_positive = join_tags(profile.base_positive, char_prompt)
        negative_prompt = join_tags(profile.base_negative, args.custom_neg)

    # 같은 태그가 양쪽에 있으면 모델이 모순된 지시를 받는다.
    if conflicts := find_tag_conflicts(base_positive, negative_prompt):
        print(f"[WARN] 태그 충돌: {conflicts} 가 포지티브와 네거티브에 동시 존재")

    targets = resolve_targets(db, args.mode, args.codes)
    if not targets:
        raise ConfigError(
            "생성 대상 코드가 없습니다.", "--mode / --codes 값을 확인하세요."
        )

    width = code_width(db.all_codes)
    save_dir = base_dir / ASSETS_DIRNAME / prefix
    if not dry_run:
        save_dir.mkdir(parents=True, exist_ok=True)

    # ── 참조 이미지 해석 ──────────────────────────────
    # dry-run 은 "무엇을 할 계획인가" 만 보여주는 모드라 파일 내용을 읽지 않는다.
    # 존재 여부와 경로만 확인한다.
    ref_weight = validate_ref_weight(args.ref_weight)
    reference: ReferenceImage | None = None
    cn_spec: ControlNetSpec | None = None

    if dry_run:
        if args.no_ref:
            print("[REF]  --no_ref 지정 - 참조 이미지 사용 안 함")
        elif args.ref_image:
            print(f"[REF]  {args.ref_image} (지정) weight {ref_weight}")
        elif found := find_reference_candidates(base_dir, prefix):
            print(f"[REF]  {found[0].name} 발견 weight {ref_weight}")
        else:
            print(f"[REF]  없음 ({REFERENCES_DIRNAME}/{prefix}.*) - 텍스트만 사용")
    else:
        reference = resolve_reference_image(
            base_dir, prefix, args.ref_image, disabled=args.no_ref
        )
        if reference is None:
            if not args.no_ref:
                print(f"[WARN] 참조 이미지 없음 ({REFERENCES_DIRNAME}/{prefix}.*) "
                      "- 텍스트 프롬프트만 사용")
        else:
            # mock 에서도 ControlNet 조회는 하지 않는다 (HTTP 금지).
            # 대신 수동 지정이 있으면 그것으로 주입 경로를 검증할 수 있다.
            if mock:
                cn_spec = (
                    ControlNetSpec(args.cn_module, args.cn_model, "manual")
                    if args.cn_module and args.cn_model
                    else None
                )
            else:
                cn_spec = resolve_controlnet_spec(args.cn_module, args.cn_model)

            if cn_spec:
                print(f"[REF]  {reference.label} weight {ref_weight}")
                print(f"[CN]   {cn_spec.module} / {cn_spec.model} ({cn_spec.source})")
            else:
                print(f"[REF]  {reference.label} - ControlNet 미해석, 텍스트만 사용")

    # mock/dry-run 에서는 HTTP 요청을 일절 발생시키지 않는다
    sampler_name = "(mock)" if (mock or dry_run) else resolve_sampler()
    badge = mode_badge(dry_run, mock)

    profile_label = "전용" if raw_positive else profile.name
    print(
        f"\n[작업 시작]{badge} 캐릭터: {prefix} | 프로필: {profile_label} | "
        f"모드: {args.mode} ({len(targets)}장) | 폭: {width}"
    )
    print(f"[저장] {save_dir}")
    print(f"[POS]  {base_positive}")
    print(f"[NEG]  {negative_prompt}\n")

    result = run_batch(
        prefix=prefix,
        base_positive=base_positive,
        char_prompt="" if raw_positive else char_prompt,
        negative_prompt=negative_prompt,
        targets=targets,
        db=db,
        save_dir=save_dir,
        width=width,
        sampler_name=sampler_name,
        reference=reference,
        cn_spec=cn_spec,
        ref_weight=ref_weight,
        dry_run=dry_run,
        mock=mock,
    )

    # VRAM 조회는 실제 생성 후에만 의미가 있다. mock/dry-run 은 GPU 를 쓰지 않는다.
    vram = None if (mock or dry_run) else fetch_vram_peak()
    print_summary(result, save_dir, badge, vram)

    existing = result.existing  # 프로퍼티가 매번 정렬하므로 한 번만 계산한다
    if not existing:
        print("\n[INFO] 생성된 파일이 없어 마크다운을 출력하지 않습니다.")
        return 1 if result.failed else 0

    if not dry_run:
        open_in_explorer(save_dir)

    print(build_genit_block(prefix, existing, db, width, badge))
    return 1 if result.aborted else 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    base_dir = Path(__file__).resolve().parent

    # 1차 파싱: help 를 끄고 --test 만 먼저 판별한다.
    # --help 는 여기서 소비되지 않고 2차 파서로 넘어가므로,
    # 도움말 출력 시점에는 이미 JSON 섹션명이 로드되어 있다 (R2.8).
    pre_args, _ = build_parser(add_help=False).parse_known_args(argv)
    if pre_args.test:
        return run_self_test(base_dir)

    sections, profiles = peek_choices(base_dir)
    parser = build_parser(sections, profiles)
    args = parser.parse_args(argv)

    # --list: 캐릭터 목록 출력 후 종료
    if args.list_chars:
        return list_characters(base_dir)

    # --all-chars: characters/ 의 모든 캐릭터를 순서대로 생성
    if args.all_chars:
        return run_all_chars(
            base_dir,
            mode=args.mode,
            codes_expr=args.codes,
            dry_run=args.dry_run,
            mock=args.mock,
        )

    # --from_image 는 생성과 무관한 독립 작업이므로 다른 생성 플래그보다
    # 먼저 분기해 즉시 종료한다. prefix/char_prompt 도 요구하지 않는다.
    if args.from_image:
        try:
            return run_interrogate(base_dir, args.from_image, args.interrogator)
        except ConfigError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            return 1

    # --char: characters/{name}.json 을 읽어 args 빈 필드를 채운다.
    # 커맨드라인 명시값이 있으면 그쪽이 우선한다 (apply_character_to_args 계약).
    if args.char:
        try:
            cfg = load_character(base_dir, args.char)
        except ConfigError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if e.hint:
                print(f"        {e.hint}", file=sys.stderr)
            return 1
        apply_character_to_args(cfg, args)
        print(f"[CHAR]  '{args.char}' 프리셋 로드 ({CHARACTERS_DIRNAME}/{args.char}.json)")

    # positive 직접 기재 방식이면 char_prompt 없어도 통과시킨다
    char_prompt_needed = not getattr(args, "positive", None)
    if missing := [n for n in ("prefix",) + (("char_prompt",) if char_prompt_needed else ()) if not getattr(args, n)]:
        parser.error("다음 인자가 필요합니다: " + ", ".join(f"--{m}" for m in missing))

    try:
        return execute(args, base_dir)
    except ConfigError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if e.hint:
            print(f"        {e.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[중단] 사용자에 의해 취소되었습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

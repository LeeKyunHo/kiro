"""
상수 단일 진실 공급원.

이 모듈은 **아무것도 import 하지 않는다** (stdlib 타입 제외).
의존성 그래프의 최하단이며, 모든 계층이 여기를 참조할 수 있다.
"""

from __future__ import annotations

from typing import Final

# ─────────────────────────────────────────────
# API 엔드포인트
# ─────────────────────────────────────────────
API_HOST: Final = "http://127.0.0.1:7860"

TXT2IMG_PATH: Final = "/sdapi/v1/txt2img"
SAMPLERS_PATH: Final = "/sdapi/v1/samplers"
INTERROGATE_PATH: Final = "/sdapi/v1/interrogate"
MEMORY_PATH: Final = "/sdapi/v1/memory"

# ControlNet 확장이 제공하는 조회 엔드포인트.
# 모델명에 해시가 붙어 환경마다 다르므로 조회가 필수다.
CN_MODULES_PATH: Final = "/controlnet/module_list"
CN_MODELS_PATH: Final = "/controlnet/model_list"

# 초 단위 타임아웃
TIMEOUT_SAMPLERS: Final = 5
TIMEOUT_TXT2IMG: Final = 300
TIMEOUT_INTERROGATE: Final = 120
TIMEOUT_CONTROLNET_LIST: Final = 10
TIMEOUT_MEMORY: Final = 5

# ─────────────────────────────────────────────
# 생성 파라미터
# ─────────────────────────────────────────────
IMAGE_WIDTH: Final = 832
IMAGE_HEIGHT: Final = 1216
STEPS: Final = 28
CFG_SCALE: Final = 7

WEBP_QUALITY: Final = 90
WEBP_METHOD: Final = 6

# 앞에서부터 탐색한다. 최신 WebUI/Forge 는 샘플러와 스케줄러가 분리되어
# "DPM++ 2M Karras" 가 목록에 없을 수 있으므로 폴백이 필요하다.
SAMPLER_CANDIDATES: Final = ("DPM++ 2M Karras", "DPM++ 2M", "Euler a")
SAMPLER_PLACEHOLDER: Final = "(offline)"

# ─────────────────────────────────────────────
# 기본 프롬프트 (프로필 미정의 시 폴백)
# ─────────────────────────────────────────────
FALLBACK_POSITIVE: Final = (
    "masterpiece, best quality, highly detailed, "
    "1girl, solo, clean background, soft lighting, character portrait"
)

FALLBACK_NEGATIVE: Final = (
    "worst quality, low quality, blurry, bad anatomy, bad hands, "
    "extra fingers, extra limbs, deformed, disfigured, watermark, "
    "signature, text, jpeg artifacts, cropped"
)

# ─────────────────────────────────────────────
# 파일 및 디렉터리
# ─────────────────────────────────────────────
POSE_DB_FILENAME: Final = "pose_database.json"

# 캐릭터 프리셋 DB. 없어도 동작한다 (--char 를 쓸 때만 필요).
CHARACTERS_FILENAME: Final = "characters.json"

# 실제 렌더링 산출물
ASSETS_DIRNAME: Final = "generated_assets"

# 모의 생성 산출물. 실제 경로와 분리해 스킵 로직이 mock 파일을
# 완성품으로 오인하는 것을 구조적으로 차단한다.
MOCK_ASSETS_DIRNAME: Final = "mock_assets"
MOCK_MANIFEST_FILENAME: Final = "_mock_manifest.json"

# 가중치 벤치마크 산출물. 가중치별로 하위 구획을 나눈다.
BENCHMARK_ASSETS_DIRNAME: Final = "benchmark_assets"
BENCHMARK_MANIFEST_FILENAME: Final = "_benchmark.json"
BENCHMARK_VIEWER_FILENAME: Final = "benchmark_viewer.html"

# 비교할 기본 가중치. 0.3 은 참조 약함, 0.9 는 포즈 전이가 시작되는 영역.
BENCHMARK_WEIGHTS_DEFAULT: Final = (0.3, 0.5, 0.7, 0.9)

# 가중치를 폴더명으로 쓸 때의 포맷. 소수점을 그대로 쓰면 OS 별 처리가
# 갈릴 수 있어 소수 둘째 자리까지 고정하고 점을 밑줄로 바꾼다.
BENCHMARK_VARIANT_TEMPLATE: Final = "w{weight:.2f}"

REFERENCES_DIRNAME: Final = "references"
REFERENCE_EXTENSIONS: Final = (".png", ".jpg", ".jpeg", ".webp")

ASSET_SUFFIX: Final = ".webp"
PARTIAL_SUFFIX: Final = ".part"

# ─────────────────────────────────────────────
# JSON 스키마 키
# ─────────────────────────────────────────────
SECTION_COMMENT_PREFIX: Final = "_"
PROFILES_KEY: Final = "_profiles"
RULES_KEY: Final = "_rules"
PROFILE_POSITIVE_KEY: Final = "base_positive"
PROFILE_NEGATIVE_KEY: Final = "base_negative"
RULES_EXCLUSIVE_KEY: Final = "mutually_exclusive"

DEFAULT_PROFILE_NAME: Final = "female"
BUILTIN_PROFILE_NAME: Final = "(built-in)"

# ─────────────────────────────────────────────
# characters.json 스키마 키
# ─────────────────────────────────────────────
# pose_database.json 과 같은 규약을 쓴다. 최상위 키가 캐릭터 약칭이고
# '_' 로 시작하는 키는 메타다. 두 파일의 규약을 통일해야 한 쪽을 익히면
# 다른 쪽도 바로 편집할 수 있다.
CHAR_FIELD_PROMPT: Final = "char_prompt"
CHAR_FIELD_PROFILE: Final = "profile"
CHAR_FIELD_NEGATIVE: Final = "custom_neg"
CHAR_FIELD_REF_WEIGHT: Final = "ref_weight"
CHAR_FIELD_REF_IMAGE: Final = "ref_image"
CHAR_FIELD_MODE: Final = "mode"
CHAR_FIELD_NOTE: Final = "note"

# 알려진 필드 집합. 여기 없는 키는 오타로 보고 경고한다.
# 조용히 무시하면 'char_promt' 같은 오타가 "프리셋이 안 먹는다" 로만
# 드러나고 원인을 찾기 어렵다.
CHAR_FIELDS: Final = frozenset({
    CHAR_FIELD_PROMPT,
    CHAR_FIELD_PROFILE,
    CHAR_FIELD_NEGATIVE,
    CHAR_FIELD_REF_WEIGHT,
    CHAR_FIELD_REF_IMAGE,
    CHAR_FIELD_MODE,
    CHAR_FIELD_NOTE,
})

# 프리셋에 담지 않는 것: --mock / --dry-run / --test / --benchmark 는
# 실행 의도이고, --cn_module / --cn_model 은 환경 설정이다. 둘 다
# 캐릭터의 속성이 아니므로 프리셋 축에서 제외한다.

# ─────────────────────────────────────────────
# 생성 범위 기본값
# ─────────────────────────────────────────────
# argparse 기본값으로 두지 않고 여기서 관리한다. argparse 가 기본값을
# 채우면 "사용자가 --mode all 을 명시했다" 와 "기본값이 채워졌다" 를
# 구분할 수 없어 프리셋 오버라이드 판정이 불가능해진다.
MODE_DEFAULT: Final = "all"
MODE_ALL: Final = "all"

# ─────────────────────────────────────────────
# 코드 범위 및 포맷
# ─────────────────────────────────────────────
CODE_MIN: Final = 0
CODE_MAX: Final = 9_999
CODE_MIN_WIDTH: Final = 2

# ─────────────────────────────────────────────
# 참조 이미지 (IP-Adapter)
# ─────────────────────────────────────────────
# 1.0 이상은 참조 이미지의 포즈까지 전이되어 JSON 포즈 지시를 무시한다.
# 0.7 은 실무 관행에 기반한 출발점이며 최적값은 GPU 환경에서 튜닝한다.
REF_WEIGHT_DEFAULT: Final = 0.7
REF_WEIGHT_MIN: Final = 0.0
REF_WEIGHT_MAX: Final = 2.0

# 모델명이 "ip-adapter_xl [4209e9f7]" 형태라 완전 일치가 불가능하다.
# 부분 문자열로 매칭하며 튜플 순서가 우선순위다.
IP_ADAPTER_MODULE_PATTERNS: Final = ("ip-adapter", "ipadapter")
IP_ADAPTER_MODEL_PATTERNS: Final = ("ip-adapter", "ipadapter")

CN_RESIZE_MODE: Final = "Crop and Resize"
CN_CONTROL_MODE: Final = "Balanced"

# ─────────────────────────────────────────────
# 태그 역추출
# ─────────────────────────────────────────────
INTERROGATORS: Final = ("deepdanbooru", "clip")
INTERROGATOR_DEFAULT: Final = "deepdanbooru"

# --char_prompt 에 들어가면 프로필과 충돌하는 태그.
# DeepBooru 는 거의 항상 성별 태그를 반환하므로 실질적으로 매번 걸린다.
# 'solo' 는 프로필 base_positive 에 이미 있어 중복이다.
GENDER_TAGS: Final = frozenset({
    "1girl", "2girls", "3girls", "multiple girls", "girl",
    "1boy", "2boys", "3boys", "multiple boys", "boy",
    "male", "female", "male focus", "female focus",
    "solo", "solo focus",
})

# 의미적으로 상충하는 태그 조합. 같은 문자열이 아니므로 교집합으로는
# 검출되지 않는다. 완전한 목록을 만들 수 없으므로 최소한만 두고,
# pose_database.json 의 _rules.mutually_exclusive 로 덮어쓸 수 있게 한다.
MUTUALLY_EXCLUSIVE_DEFAULT: Final = (
    frozenset({"1girl", "1boy"}),
    frozenset({"solo", "2girls"}),
    frozenset({"solo", "multiple girls"}),
    frozenset({"male focus", "female focus"}),
)

# ─────────────────────────────────────────────
# 젠잇 출력
# ─────────────────────────────────────────────
# 치환되지 않는 리터럴. 일반 문자열로 두면 f-string 4중 이스케이프가 불필요하다.
URL_PLACEHOLDER: Final = "{{url}}"

GENIT_STATUS_TEMPLATE: Final = (
    "[@id=상태창|name={name}|title={title}|status={status}|desc={desc}]"
)
GENIT_STATUS_DEFAULTS: Final = {
    "title": "직책입력",
    "status": "현재상태",
    "desc": "대사한줄",
}

# 캐릭터 카드 파일. 에셋 폴더 안에 함께 두어 이미지와 설정이 같이
# 이동하도록 한다. 별도 폴더에 모으면 캐릭터를 지울 때 한쪽이 남는다.
GENIT_CARD_TEMPLATE: Final = "{prefix}_genit_card.md"

# 파일명에서 코드를 역파싱할 때 쓴다. 카드는 이번 실행의 결과가 아니라
# 폴더의 현재 상태를 기술해야 하므로 디스크를 읽어야 한다.
ASSET_FILENAME_PATTERN_SOURCE: Final = r"^(?P<prefix>.+)_(?P<code>\d{1,4})$"

SEPARATOR_WIDTH: Final = 64
SEPARATOR: Final = "=" * SEPARATOR_WIDTH

# ─────────────────────────────────────────────
# 검증
# ─────────────────────────────────────────────
# prefix 는 경로 세그먼트와 파일명에 그대로 들어간다.
# 화이트리스트 방식이라 새 위험 문자가 생겨도 자동으로 막힌다.
SAFE_PREFIX_PATTERN_SOURCE: Final = r"^[A-Za-z0-9_-]{1,64}$"

# --mode 값이 섹션명인지 코드 표현식인지 판별.
# 숫자·콤마·하이픈·공백만으로 구성되면 코드 표현식.
CODE_EXPR_PATTERN_SOURCE: Final = r"^[\s\d,\-]+$"

# 태그 정규화용
BRACKET_CHARS: Final = "()[]{}"
WEIGHT_SUFFIX_PATTERN_SOURCE: Final = r":\s*-?\d+(?:\.\d+)?\s*$"

# ─────────────────────────────────────────────
# 모의 이미지
# ─────────────────────────────────────────────
# 실제의 1/4, 종횡비 동일
MOCK_WIDTH: Final = IMAGE_WIDTH // 4
MOCK_HEIGHT: Final = IMAGE_HEIGHT // 4
MOCK_TEXT_X: Final = 12
MOCK_TEXT_TOP: Final = 24
MOCK_TEXT_COLOR: Final = (30, 30, 30)
MOCK_FONT_NAME: Final = "arial.ttf"
MOCK_FONT_BIG_SIZE: Final = 44
MOCK_FONT_SMALL_SIZE: Final = 13
MOCK_GAP_BIG: Final = 52
MOCK_GAP_SMALL: Final = 20
MOCK_LABEL_MAXLEN: Final = 26
MOCK_HUE_STEP: Final = 37
MOCK_SATURATION: Final = 0.35
MOCK_VALUE: Final = 0.90

# ─────────────────────────────────────────────
# 기타
# ─────────────────────────────────────────────
GIB: Final = 1024 ** 3

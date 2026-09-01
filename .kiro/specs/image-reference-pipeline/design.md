# Design — 참조 이미지 파이프라인

## 1. 아키텍처 개요

기존 파이프라인에 **참조 이미지 축**을 추가한다. 프로필이 태그 축을 담당하듯,
참조 이미지는 시각 특징 축을 담당한다. 두 축은 독립이다.

```
pose_database.json ──┐
                     ├─▶ PoseDatabase(entries, sections, profiles)
                     │
references/{prefix}.* ┐
   또는 --ref_image   ├─▶ ReferenceImage(path, b64, size)   ← 신규
                     │        (없으면 None)
                     │
/controlnet/*_list ──┤
   또는 --cn_model    ├─▶ ControlNetSpec(module, model)      ← 신규
                     │        (해석 실패 시 None)
                     ▼
              run_batch()
                     │
      ┌──────────────┴──────────────┐
      │  코드별 페이로드 조립         │
      │  build_txt2img_payload()     │  ← 순수 함수 (신규)
      │    + inject_controlnet()     │  ← 순수 함수 (신규)
      ▼                              │
generate_image(payload)  ────────────┘
```

### 1.1 별도 진입점: `--from_image`

태그 추출은 생성 파이프라인을 타지 않는다. `--test` 처럼 독립 분기다.

```
--from_image PATH ─▶ run_interrogate() ─▶ /sdapi/v1/interrogate
                                        ─▶ 태그 출력 + 명령 예시 ─▶ exit
```

### 1.2 핵심 설계 원칙 — 네트워크와 조립의 분리

노트북에서 검증 가능하게 만드는 것이 이 설계의 최우선 제약이다.
따라서 모든 함수를 두 종류로 엄격히 나눈다.

| 분류 | 특징 | 노트북 검증 |
|---|---|---|
| **순수 함수** | 입력 → 출력. 네트워크·전역 상태 없음 | 가능 |
| **I/O 함수** | HTTP 요청 또는 파일 접근 | 파일만 가능 |

```
순수:  build_controlnet_unit()  build_txt2img_payload()  inject_controlnet()
       build_interrogate_payload()  filter_gender_tags()  match_model_name()
       validate_ref_weight()

I/O :  resolve_reference_image()  encode_image_b64()      ← 파일 (검증 가능)
       resolve_controlnet_spec()  run_interrogate()       ← HTTP (집 PC)
```

`resolve_controlnet_spec()` 은 HTTP를 쓰지만, 내부의 매칭 로직
`match_model_name()` 을 순수 함수로 떼어내 그 부분만 노트북에서 검증한다.

---

## 2. 데이터 모델

### 2.1 `ReferenceImage`

```python
@dataclass(frozen=True, slots=True)
class ReferenceImage:
    path: Path
    b64: str          # base64 인코딩 결과. 배치당 1회만 계산 (R1.8)
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.path.name} ({self.width}x{self.height})"
```

`b64` 를 필드로 갖는 이유는 20~50장 생성 시 재인코딩을 막기 위함이다.
프로퍼티로 lazy 계산하면 호출마다 재계산되므로 생성 시점에 확정한다.

**부재는 `None` 으로 표현한다.** 빈 `ReferenceImage` 같은 특수 객체를 만들지
않는다. `Optional` 이 "참조 없음"을 가장 정직하게 표현하고, 호출부에서
`if reference:` 한 줄로 분기된다 (R1.4).

### 2.2 `ControlNetSpec`

```python
@dataclass(frozen=True, slots=True)
class ControlNetSpec:
    module: str       # 전처리기 (예: "ip-adapter_clip_sdxl")
    model: str        # 모델명 + 해시 (예: "ip-adapter_xl [4209e9f7]")
    source: str       # "auto" | "manual" — 로그 표시용
```

`source` 를 두는 이유: 자동 탐지 결과인지 사용자 지정인지 로그에서 구분하면
집 PC 검증 시 어느 경로가 동작했는지 판단할 수 있다.

### 2.3 `InterrogateResult`

```python
@dataclass(frozen=True, slots=True)
class InterrogateResult:
    raw: str                  # API 원본 응답
    tags: list[str]           # 정규화된 태그 목록
    gender_tags: list[str]    # 발견된 성별 태그 (경고 대상)

    @property
    def filtered(self) -> str:
        """성별 태그를 제거한 프롬프트 문자열 (R4.6)."""
        excluded = set(self.gender_tags)
        return ", ".join(t for t in self.tags if t not in excluded)
```

---

## 3. 참조 이미지 해석

### 3.1 탐색 알고리즘

```python
REFERENCES_DIRNAME = "references"
REFERENCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
```

```
resolve_reference_image(base_dir, prefix, explicit_path) -> ReferenceImage | None

  1. explicit_path 가 있으면
       a. 존재하지 않음        → ConfigError        (R1.5)
       b. 디렉터리             → ConfigError        (R1.6)
       c. load_reference() 반환
  2. references/ 폴더가 없으면 None                 (R6.3)
  3. REFERENCE_EXTENSIONS 순서로 references/{prefix}{ext} 탐색
       - 발견된 것이 2개 이상이면 첫 번째 채택 + 무시 목록 경고 (R1.3)
       - 하나도 없으면 None                          (R1.4)
  4. load_reference() 반환
```

명시 지정과 자동 탐색의 실패 처리를 다르게 하는 이유는 사용자 의도가 다르기
때문이다. `--ref_image` 를 적었다면 그 파일을 쓰겠다는 명확한 의사표시이므로
조용히 무시하면 안 된다. 자동 탐색은 "있으면 쓰고 없으면 넘어가는" 편의 기능이다.

### 3.2 로드 및 검증

```python
def load_reference(path: Path) -> ReferenceImage:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"참조 이미지를 읽을 수 없습니다: {path}", str(e))

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()                    # 헤더 검증
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size        # verify() 후엔 재오픈 필요
    except Exception as e:
        raise ConfigError(f"유효한 이미지가 아닙니다: {path}", str(e))

    return ReferenceImage(path, base64.b64encode(data).decode("ascii"), width, height)
```

`img.verify()` 후에는 이미지 객체를 다시 쓸 수 없다는 Pillow 특성 때문에
두 번 열어야 한다. `verify()` 만으로는 크기를 못 얻는다.

**원본 바이트를 그대로 base64 인코딩한다.** Pillow로 재인코딩하지 않는 이유는
불필요한 손실과 시간이 발생하고, WebUI가 PNG/JPEG/WebP를 모두 받기 때문이다.

---

## 4. 페이로드 조립

### 4.1 기존 구조의 문제

현재 `generate_image()` 안에 페이로드가 인라인으로 박혀 있다.

```python
def generate_image(prompt, negative_prompt, sampler_name) -> bytes:
    payload = { "prompt": prompt, ... }        # ← 조립과 전송이 결합
    response = get_session().post(API_URL, json=payload, ...)
```

이 상태로는 페이로드 구조를 검증하려면 HTTP를 가로채야 한다. 노트북에서는
불가능하다.

### 4.2 분리 후 구조

```python
def build_txt2img_payload(
    *, prompt: str, negative_prompt: str, sampler_name: str
) -> dict[str, Any]:
    """네트워크 접근 없이 페이로드를 조립한다 (순수 함수)."""
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


def generate_image(payload: dict[str, Any]) -> bytes:
    """조립된 페이로드를 전송한다."""
    response = get_session().post(API_URL, json=payload, timeout=TXT2IMG_TIMEOUT)
    ...
```

`generate_image()` 의 시그니처가 바뀐다. 기존 spec에서 "불변"으로 선언한
함수지만, **관찰 가능한 동작(전송되는 페이로드 내용과 반환값)은 동일**하다.
호출 규약만 바뀐다. `dynamic-pose-pipeline/design.md` 6장의 "불변" 정의와
일관된 처리다.

### 4.3 ControlNet 유닛

```python
def build_controlnet_unit(
    reference: ReferenceImage, spec: ControlNetSpec, weight: float
) -> dict[str, Any]:
    """ControlNet 단일 유닛 딕셔너리 (순수 함수)."""
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
```

`resize_mode` / `control_mode` / `pixel_perfect` 는 WebUI 기본값과 동일하지만
명시한다. 버전에 따라 기본값이 바뀌어도 동작이 흔들리지 않게 하려는 의도다.

### 4.4 주입

```python
def inject_controlnet(payload: dict, unit: dict) -> dict:
    """페이로드에 ControlNet 유닛을 주입한 새 딕셔너리를 반환한다 (순수 함수)."""
    merged = dict(payload)
    merged["alwayson_scripts"] = {"controlnet": {"args": [unit]}}
    return merged
```

**입력을 변경하지 않고 새 딕셔너리를 반환한다.** 원본을 mutate하면 루프에서
페이로드를 재사용할 때 상태가 누적될 위험이 있다.

**참조가 없으면 이 함수를 호출하지 않는다.** 빈 `alwayson_scripts` 를 넣지
않는 이유는 WebUI가 그것을 "ControlNet 비활성"이 아니라 "인자 부족"으로
해석할 수 있기 때문이다 (R2.4).

```python
payload = build_txt2img_payload(...)
if reference and cn_spec:
    payload = inject_controlnet(payload, build_controlnet_unit(reference, cn_spec, weight))
```

조건이 `reference and cn_spec` 인 점이 중요하다. 참조 이미지는 있는데
ControlNet 해석이 실패한 경우(미설치 등)에는 주입하지 않고 텍스트만으로
생성한다 (R3.4).

### 4.5 weight 검증

```python
REF_WEIGHT_DEFAULT = 0.7
REF_WEIGHT_MIN = 0.0
REF_WEIGHT_MAX = 2.0

def validate_ref_weight(value: float) -> float:
    if not REF_WEIGHT_MIN <= value <= REF_WEIGHT_MAX:
        raise ConfigError(
            f"--ref_weight 는 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} 범위여야 합니다: {value}",
            "0.5~0.8 이 실무 범위입니다. 1.0 이상은 참조 이미지의 포즈까지 전이됩니다.",
        )
    return value
```

기본값 `0.7` 의 근거는 실무 관행이며, **정확한 값은 R7.4에서 집 PC 튜닝으로
확정한다.** 지금은 근거 있는 출발점일 뿐이다.

---

## 5. ControlNet 모델 자동 탐지

### 5.1 왜 자동 탐지가 필수인가

모델명이 `ip-adapter_xl [4209e9f7]` 형태로 **해시를 포함**한다. 해시는 파일
내용에서 계산되므로 다운로드 출처·버전에 따라 다르다. 하드코딩하면 다른 PC에서
반드시 깨진다.

이미 검증된 패턴이 프로젝트에 있다. `resolve_sampler()` 가 `/sdapi/v1/samplers`
를 조회해 후보와 대조하는 방식이다. 같은 구조를 재사용한다.

### 5.2 매칭은 순수 함수로 분리

```python
IP_ADAPTER_MODEL_PATTERNS = ("ip-adapter", "ipadapter")
IP_ADAPTER_MODULE_PATTERNS = ("ip-adapter", "ipadapter")

def match_model_name(available: Sequence[str], patterns: Sequence[str]) -> str | None:
    """
    사용 가능 목록에서 패턴을 부분 문자열로 찾는다 (순수 함수).

    모델명에 해시가 붙어 완전 일치가 불가능하므로 부분 매칭을 쓴다.
    패턴 순서가 우선순위다.
    """
    lowered = [(name, name.lower()) for name in available]
    for pattern in patterns:
        needle = pattern.lower()
        for original, low in lowered:
            if needle in low:
                return original
    return None
```

HTTP 조회는 껍데기가 담당한다.

```python
def resolve_controlnet_spec(
    manual_module: str | None, manual_model: str | None
) -> ControlNetSpec | None:
    if manual_module and manual_model:
        return ControlNetSpec(manual_module, manual_model, "manual")

    try:
        modules = get_session().get(CN_MODULES_URL, timeout=...).json()["module_list"]
        models = get_session().get(CN_MODELS_URL, timeout=...).json()["model_list"]
    except Exception:
        print("[WARN] ControlNet 목록 조회 실패 - 참조 이미지 없이 생성합니다")
        return None                                       # R3.4

    module = manual_module or match_model_name(modules, IP_ADAPTER_MODULE_PATTERNS)
    model = manual_model or match_model_name(models, IP_ADAPTER_MODEL_PATTERNS)

    if not module or not model:
        print("[WARN] IP-Adapter 모듈/모델을 찾지 못했습니다.")
        print(f"       사용 가능 모듈: {modules}")
        print(f"       사용 가능 모델: {models}")
        print("       --cn_module / --cn_model 로 직접 지정하세요.")
        return None                                       # R3.5
    return ControlNetSpec(module, model, "auto")
```

목록을 전부 출력하는 이유는 집 PC에서 자동 탐지가 실패했을 때 **그 출력만
보고 바로 `--cn_model` 을 지정**할 수 있게 하려는 것이다. 수요일 세션을
짧게 만드는 장치다.

### 5.3 노트북에서의 동작

WebUI가 없으므로 조회가 실패하고 `None` 이 반환된다. 즉 노트북에서는
참조 이미지가 있어도 ControlNet이 주입되지 않는다. 이는 정상이며 R3.4에
부합한다.

그래서 **`--cn_module` 과 `--cn_model` 을 둘 다 수동 지정하면 조회를 건너뛴다**
(R3.3). 노트북에서 주입 경로를 검증하려면 이 우회로를 쓴다.

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "x" --mock `
  --cn_module "ip-adapter_clip_sdxl" --cn_model "ip-adapter_xl [test]"
```

---

## 6. 태그 역추출 (`--from_image`)

### 6.1 엔드포인트

```
POST /sdapi/v1/interrogate
{ "image": "<base64>", "model": "deepdanbooru" }
   → { "caption": "1girl, solo, silver hair, ..." }
```

`model` 후보는 `deepdanbooru` 와 `clip` 이다. **기본값은 `deepdanbooru`.**
CLIP은 자연어 문장(`a girl with silver hair standing in a room`)을 반환해
태그 기반 프롬프트로 쓰기 어렵다 (R4.3).

### 6.2 페이로드 조립 (순수 함수)

```python
INTERROGATORS = ("deepdanbooru", "clip")

def build_interrogate_payload(b64: str, model: str) -> dict[str, str]:
    return {"image": b64, "model": model}
```

단순하지만 순수 함수로 떼어내 `--test` 에서 구조를 검증한다 (R4.9).

### 6.3 성별 태그 필터링

추출된 태그를 그대로 `--char_prompt` 에 쓰면 프로필과 충돌한다.
DeepBooru는 거의 항상 `1girl` 또는 `1boy` 를 반환하므로 실질적으로 매번 걸린다.

```python
GENDER_TAGS = frozenset({
    "1girl", "2girls", "multiple girls", "girl",
    "1boy", "2boys", "multiple boys", "boy",
    "male", "female", "male focus", "female focus",
    "solo",
})

def filter_gender_tags(tags: Sequence[str]) -> tuple[list[str], list[str]]:
    """(유지할 태그, 제거된 성별 태그) 를 반환한다 (순수 함수)."""
    kept, removed = [], []
    for tag in tags:
        (removed if tag in GENDER_TAGS else kept).append(tag)
    return kept, removed
```

`solo` 를 포함시킨 이유: 프로필의 `base_positive` 에 이미 `solo` 가 있어
중복된다. 중복 자체가 치명적이진 않지만 태그 충돌 감지(기존 R4)에서 잡히므로
미리 제거한다.

**제거하지 않고 경고만 하지 않는 이유**는, 사용자가 결과를 그대로 복사해 쓸
가능성이 높기 때문이다. 필터링된 버전을 함께 제시하면 올바른 쪽을 고르게 된다.

### 6.4 출력 형식

```
================================================================
  태그 추출 결과 | references/mika.png (768x1024)
================================================================

[원본]
1girl, solo, silver hair, blue eyes, school uniform, looking at viewer

[WARN] 성별·인원 태그가 감지되었습니다: ['1girl', 'solo']
       프로필(_profiles)에서 이미 다루므로 --char_prompt 에는 넣지 마세요.

[권장]
silver hair, blue eyes, school uniform, looking at viewer

[그대로 실행하려면]
python sd_batch_generator.py --prefix PREFIX --char_prompt "silver hair, blue eyes, school uniform, looking at viewer"
================================================================
```

`PREFIX` 를 대문자 자리표시자로 둔 이유: `--from_image` 는 `--prefix` 를
요구하지 않으므로(R4.2) 실제 값을 알 수 없다. 사용자가 채워야 함을 시각적으로
드러낸다.

---

## 7. 실행 모드별 동작

R5의 요구사항을 구현 관점으로 정리한다.

| 단계 | 기본 | `--mock` | `--dry-run` | `--test` |
|---|---|---|---|---|
| 참조 이미지 탐색 | O | **O** | O | 임시 파일로 |
| base64 인코딩 | O | **O** | X | O |
| ControlNet 목록 조회 | O | X | X | X |
| 페이로드 조립 | O | **O** | X | O |
| API 전송 | O | X | X | X |
| 파일 저장 | O | O | X | X |

### 7.1 `--mock` 에서 탐색과 인코딩을 수행하는 이유

파일을 못 찾거나 base64 인코딩이 깨지는 것은 **mock에서 잡아야 할 결함**이다.
이것까지 건너뛰면 mock이 검증하는 범위가 줄어든다 (R5.1).

같은 논리로 페이로드 조립도 수행한다. 조립된 페이로드를 전송만 하지 않는다.

### 7.2 `--dry-run` 에서 인코딩을 생략하는 이유

dry-run은 "무엇을 할 계획인가"만 보여주는 모드다. 파일 I/O를 하지 않는 것이
이 모드의 계약이다. 참조 이미지 **존재 여부와 경로**는 출력하지만 내용을
읽지는 않는다 (R5.3).

```
[REF]  references/mika.png 발견 (weight 0.7)
```

### 7.3 더미 이미지에 참조 표시

```python
rows = (
    ...,
    ("MOCK" + (" +REF" if reference else ""), font_small, MOCK_GAP_SMALL),
)
```

참조가 적용된 mock 산출물을 육안으로 구분할 수 있게 한다 (R5.2).

---

## 8. `--test` 신규 항목 (T21~T30)

### 8.1 임시 이미지 픽스처

저장소에 테스트용 바이너리를 커밋하지 않는다 (R5.6). Pillow로 즉석 생성한다.

```python
@contextmanager
def _temp_reference(extensions: Sequence[str]):
    """지정 확장자로 임시 참조 이미지를 만들고 정리한다."""
    tmp = Path(tempfile.mkdtemp(prefix="sdref_"))
    try:
        refs = tmp / REFERENCES_DIRNAME
        refs.mkdir()
        for ext in extensions:
            img = Image.new("RGB", (64, 96), (128, 128, 200))
            img.save(refs / f"t{ext}")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
```

`contextmanager` 를 쓰면 검사 실패로 예외가 나도 임시 폴더가 정리된다.

### 8.2 검사 항목

| ID | 검사 | 방식 |
|---|---|---|
| T21 | 확장자 우선순위 | 4종을 모두 만들고 `.png` 가 선택되는지 |
| T22 | 참조 부재 | 빈 폴더에서 `None` 반환, 예외 없음 |
| T23 | base64 왕복 | 인코딩 → 디코딩 → Pillow 로 열어 크기 일치 |
| T24 | 유닛 구조 | 필수 키 7개 존재, `enabled is True` |
| T25 | 미주입 | 참조 없을 때 `"alwayson_scripts" not in payload` |
| T26 | 주입 위치 | `payload["alwayson_scripts"]["controlnet"]["args"][0]` |
| T27 | weight 범위 | `-0.1`, `2.1` 거부 / `0.0`, `0.7`, `2.0` 허용 |
| T28 | interrogate 페이로드 | `{"image", "model"}` 키, 기본 모델명 |
| T29 | 성별 태그 필터 | `1girl, solo, silver hair` → 유지 1 / 제거 2 |
| T30 | 모델명 부분 매칭 | 해시 포함 문자열에서 `ip-adapter` 발견 |

T26은 주입 후 **원본 페이로드가 변경되지 않았는지**도 함께 확인한다
(4.4의 불변성 계약).

---

## 9. 함수 목록

### 9.1 신규

| 함수 | 종류 | 시그니처 |
|---|---|---|
| `resolve_reference_image` | 파일 I/O | `(base_dir, prefix, explicit) -> ReferenceImage \| None` |
| `load_reference` | 파일 I/O | `(path) -> ReferenceImage` |
| `validate_ref_weight` | 순수 | `(value: float) -> float` |
| `build_txt2img_payload` | 순수 | `(*, prompt, negative_prompt, sampler_name) -> dict` |
| `build_controlnet_unit` | 순수 | `(reference, spec, weight) -> dict` |
| `inject_controlnet` | 순수 | `(payload, unit) -> dict` |
| `match_model_name` | 순수 | `(available, patterns) -> str \| None` |
| `resolve_controlnet_spec` | HTTP | `(manual_module, manual_model) -> ControlNetSpec \| None` |
| `build_interrogate_payload` | 순수 | `(b64, model) -> dict` |
| `filter_gender_tags` | 순수 | `(tags) -> (kept, removed)` |
| `run_interrogate` | HTTP | `(base_dir, path, model) -> int` |
| `_temp_reference` | 테스트 | contextmanager |

### 9.2 변경

| 함수 | 변경 내용 | 근거 |
|---|---|---|
| `generate_image` | 시그니처가 `(payload)` 로 변경 | 조립/전송 분리 (4.2) |
| `run_batch` | `reference`, `cn_spec`, `ref_weight` 인자 추가 | |
| `make_dummy_png` | `reference` 인자 추가 (`+REF` 표시) | R5.2 |
| `execute` | 참조 해석 및 spec 해석 단계 추가 | |
| `build_parser` | 플래그 6종 추가 | |
| `main` | `--from_image` 분기 추가 | R4.1 |

### 9.3 불변

`save_as_webp()`, `resolve_sampler()`, `parse_pose_db()`, `resolve_targets()`,
`code_width()`, `asset_filename()`, `build_genit_block()`, 프로필 관련 전체.

---

## 10. CLI 추가 플래그

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--ref_image` | `None` | 참조 이미지 경로 직접 지정 |
| `--ref_weight` | `0.7` | IP-Adapter 적용 강도 (0.0~2.0) |
| `--no_ref` | `False` | 참조 이미지를 무시하고 텍스트만 사용 |
| `--cn_module` | `None` | ControlNet 전처리기 수동 지정 |
| `--cn_model` | `None` | ControlNet 모델 수동 지정 |
| `--from_image` | `None` | 태그 역추출 후 종료 |
| `--interrogator` | `deepdanbooru` | `deepdanbooru` \| `clip` |

`--no_ref` 를 추가한 이유: 참조 이미지가 `references/` 에 있는데 이번만
참조 없이 생성하고 싶은 경우가 있다. 파일을 옮기지 않고 끌 수 있어야 한다.
비교 실험(참조 유/무 대조)에도 필요하다 (R7.7).

### 10.1 모드 우선순위 갱신

```
--test  >  --from_image  >  --dry-run  >  --mock  >  기본
```

`--from_image` 를 `--test` 다음에 두는 이유: 태그 추출은 생성과 무관한
독립 작업이므로 다른 생성 관련 플래그보다 먼저 분기해 즉시 종료한다.

---

## 11. 노트북 검증의 한계 (명시)

다음은 오늘 **검증할 수 없다.** 코드가 있어도 "동작한다"고 말할 수 없는 항목이다.

| 항목 | 이유 | 수요일 확인 방법 |
|---|---|---|
| 실제 모델명 매칭 | 설치된 모델 목록을 알 수 없음 | `/controlnet/model_list` 조회 |
| WebUI 가 페이로드를 수락하는지 | 스키마 검증은 서버에서 발생 | 실제 1장 생성 |
| weight 별 결과 차이 | 이미지를 봐야 판단 | 0.5/0.7/0.9 비교 |
| 태그 추출 품질 | DeepBooru 모델 필요 | 실제 이미지로 실행 |
| 일관성 개선 여부 | 육안 비교 필요 | 참조 유/무 세트 대조 |
| ControlNet 확장 유무 | 환경 의존 | 조회 성공 여부 |

오늘 검증되는 것은 **"우리가 만들려던 페이로드가 의도한 구조인가"** 까지다.
"그 페이로드가 WebUI에서 통하는가" 는 수요일 몫이다.

이 구분을 흐리지 않기 위해 `--test` 는 페이로드 **구조만** 검사하고,
"동작 확인"이라는 표현을 쓰지 않는다.

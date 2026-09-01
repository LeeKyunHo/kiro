# Design — 동적 포즈 파이프라인

## 아키텍처 개요

기존 단일 파일 구조를 유지하되, `main()`에 뭉쳐 있던 코드 집합 결정 로직을
독립 함수로 분리한다. 데이터 흐름을 4단계 파이프라인으로 재정의한다.

```
pose_database.json
        │
        ▼
 ┌──────────────────┐   PoseEntry 목록 + 섹션 인덱스
 │ load_pose_db()   │──────────────────────────────┐
 │  (파싱/검증)      │                              │
 └──────────────────┘                              │
        │                                          │
        ▼                                          ▼
 ┌──────────────────┐                    ┌──────────────────┐
 │ resolve_targets()│  대상 코드 리스트     │ code_width()     │
 │ (mode/codes 분기)│───────────────────▶│ (패딩 폭 산출)    │
 └──────────────────┘                    └──────────────────┘
        │                                          │
        ▼                                          ▼
 ┌───────────────────────────────────────────────────────────┐
 │ run_batch(dry_run, mock)                                   │
 │                                                            │
 │  dry_run ─▶ planned 기록만 (파일·네트워크 없음)              │
 │  mock    ─▶ make_dummy_png() ─┐                            │
 │  기본    ─▶ generate_image() ─┴─▶ save_as_webp() ─▶ .webp   │
 └───────────────────────────────────────────────────────────┘
        │  BatchResult(success/skipped/failed/planned)
        ▼
 ┌───────────────────────────────────────────────────────────┐
 │ open_explorer()  +  build_genit_block() → print            │
 │  (dry-run 시 탐색기 생략, 마크다운은 planned 기준)            │
 └───────────────────────────────────────────────────────────┘
```

`--test` 는 이 파이프라인을 타지 않고 별도 진입점으로 분기한다.

```
 --test ─▶ run_self_test() ─▶ load_pose_db() 및 순수 함수 직접 호출
                           ─▶ TestReport [PASS]/[FAIL]/[WARN] ─▶ exit code
```

**변경 없음(그대로 유지):** `resolve_sampler()`, `save_as_webp()`,
`generate_image()`, 탐색기 오픈 호출.
`--mock` 은 `save_as_webp()` 를 우회하지 않고 그대로 경유한다 (R7.4).

---

## 1. 데이터 모델

### 1.1 JSON 스키마

최상위는 섹션 딕셔너리. `_` 로 시작하는 키는 주석으로 무시한다.

```json
{
  "_comment": "주석 섹션 — 파싱 대상 아님",
  "emotions": { "00": "태그...", "01": "태그..." },
  "poses":    { "10": "태그...", "11": "태그..." },
  "outfits":  { "20": "태그..." }
}
```

섹션명은 자유. 섹션을 추가하면 그 이름이 곧 `--mode` 값이 된다 (R2.1).

### 1.2 내부 표현

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PoseEntry:
    code: int          # 정규화된 정수 코드
    prompt: str        # 포즈/표정 태그
    section: str       # 출처 섹션명 (모드 분기 및 리포트용)

    @property
    def label(self) -> str:
        """프롬프트 첫 태그를 사람이 읽을 라벨로 사용."""
        return self.prompt.split(",")[0].strip()
```

```python
@dataclass
class PoseDatabase:
    entries: dict[int, PoseEntry]        # code → entry
    sections: dict[str, list[int]]       # section → 정렬된 code 목록
    warnings: list[str]                  # 파싱 중 수집한 경고

    @property
    def all_codes(self) -> list[int]:
        return sorted(self.entries.keys())
```

`entries`를 `dict[int, ...]`로 두는 이유는 스킵 판정과 라벨 조회가
O(1)이어야 하고, 정렬은 출력 시점에만 필요하기 때문이다.

---

## 2. JSON 키 정렬 및 순회 알고리즘

### 2.1 정렬 문제의 핵심

JSON 객체 키는 항상 문자열이다. 파이썬 `dict`는 삽입 순서를 보존하므로
`json.load()` 결과는 **파일에 적힌 순서** 그대로다. 이를 그대로 쓰면 두 가지가 깨진다.

- 파일에 `"5"`, `"12"`, `"03"` 순으로 적혀 있으면 그 순서로 생성된다.
- 문자열 정렬을 쓰면 `"10" < "2"` 가 되어 `10`이 `2`보다 먼저 온다.

따라서 **정수 변환 후 정렬**이 유일하게 올바른 방식이다 (R1.2).

```python
codes = sorted(int(k) for k in raw_keys)   # 숫자 크기 기준
```

### 2.2 파싱 알고리즘

```
load_pose_db(path) -> PoseDatabase:
  1. 파일 존재 확인. 없으면 명확한 메시지와 함께 exit(1)
  2. json.load() — JSONDecodeError는 라인/컬럼 포함해 재포장 후 exit(1)
  3. entries = {}, sections = {}, warnings = []
  4. for section, body in raw.items():
       a. section.startswith("_") 이면 continue          # 주석 (R2.6)
       b. isinstance(body, dict) 아니면 warn + continue
       c. section_codes = []
       d. for key, value in body.items():
            i.   int(key) 시도 → 실패 시 warn 후 continue   # (R1.4)
            ii.  value가 str 아니거나 strip() 이 빈 값 → warn 후 continue  # (R1.6)
            iii. code가 이미 entries에 있으면 warn (덮어쓰기 명시)      # (R1.5)
            iv.  entries[code] = PoseEntry(code, value.strip(), section)
                 section_codes.append(code)
       e. sections[section] = sorted(section_codes)
  5. entries가 비어 있으면 에러 출력 후 exit(1)                       # (R1.7)
  6. return PoseDatabase(entries, sections, warnings)
```

경고는 즉시 출력하지 않고 `warnings`에 모았다가 로드 직후 한 번에 출력한다.
생성 로그와 섞이지 않게 하려는 의도다.

### 2.3 대상 코드 결정

`--mode` 는 3가지 입력을 받는다 (R2.7).

| 입력 형태 | 예시 | 처리 |
|---|---|---|
| 프리셋 키워드 | `all` | 전체 코드 합집합 |
| 섹션명 | `emotions`, `poses` | 해당 섹션 코드 |
| **코드 표현식** | `00,05,20`, `10-14`, `0-5,12` | `parse_codes_expr()` 경유 |

#### 판별 방식

섹션명과 코드 표현식은 **문자 구성**으로 구분한다. 섹션명에는 알파벳이 있고
코드 표현식에는 숫자·콤마·하이픈·공백만 있으므로 충돌하지 않는다.

```python
CODE_EXPR_PATTERN = re.compile(r"^[\s\d,\-]+$")

def looks_like_code_expr(value: str) -> bool:
    """숫자·콤마·하이픈·공백만으로 구성되면 코드 표현식으로 간주한다."""
    return bool(value) and CODE_EXPR_PATTERN.match(value) is not None
```

`bool(value)` 선행 검사가 필요한 이유: 정규식 `+` 는 빈 문자열에 매칭되지
않지만, 공백만 있는 `"  "` 는 매칭된다. 이 경우 `parse_codes_expr()` 이
빈 리스트를 반환해 "대상 0개" 에러로 자연히 걸러진다.

#### 우선순위 해소

`--codes` 와 `--mode` 코드 표현식이 동시에 오면 `--codes` 를 채택하고
무시된 값을 경고한다 (R2.9).

```python
def resolve_targets(db: PoseDatabase, mode: str, codes_expr: str | None) -> list[int]:
    expr: str | None = None

    if codes_expr:
        expr = codes_expr
        if mode and mode != "all" and looks_like_code_expr(mode):
            print(f"[WARN] --codes 가 우선합니다. --mode '{mode}' 무시됨")
    elif mode and looks_like_code_expr(mode):
        expr = mode                                  # R2.7

    if expr is not None:
        requested = parse_codes_expr(expr)
        missing = [c for c in requested if c not in db.entries]
        if missing:
            print(f"[WARN] DB에 없는 코드 무시: {missing}")
        return [c for c in requested if c in db.entries]

    if mode == "all":
        return db.all_codes
    if mode in db.sections:
        return db.sections[mode]

    available = ["all"] + sorted(db.sections)
    print(f"[ERROR] 알 수 없는 모드 '{mode}'. 사용 가능: {available}")
    print("        코드 리스트 직접 지정도 가능합니다. 예: --mode 0,5,12 / --mode 10-14")
    sys.exit(1)
```

### 2.4 `--codes` 표현식 파서

`20-29`(범위), `0,3,7`(열거), `0-5,10,20-22`(혼합)을 지원한다.

```python
def parse_codes_expr(expr: str) -> list[int]:
    result: set[int] = set()
    for token in expr.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start        # 역순 입력 허용
            result.update(range(start, end + 1))
        else:
            result.add(int(token))
    return sorted(result)
```

`set` 사용으로 중복 입력(`0-5,3`)이 자동 정규화된다.
`int()` 실패는 `ValueError`로 상위에서 잡아 사용법 안내로 전환한다.

### 2.5 동적 모드 등록 (argparse 제약 해소)

`argparse`의 `choices`는 파서 생성 시점에 확정되지만, 모드 목록은 JSON을
읽어야 알 수 있다. 순서 의존성이 있으므로 **`choices`를 쓰지 않고**
자유 문자열로 받은 뒤 `resolve_targets()`에서 검증한다 (R2.3).

```python
parser.add_argument(
    "--mode", default="all",
    help="all | JSON 섹션명 | 코드 리스트 (예: emotions / 0,5,12 / 10-14)",
)
```

에러 메시지에 실제 사용 가능 목록을 담기 때문에 UX 손실은 없다.

#### `--help` 에 실제 섹션명 노출 (R2.8)

`choices` 를 못 쓰더라도 help 문구 자체는 동적으로 채울 수 있다.
`parse_known_args()` 로 1차 파싱한 뒤 JSON을 읽고, 섹션명을 넣어 파서를
재구성하는 2단계 방식을 쓴다.

```python
def build_parser(section_names: list[str] | None = None) -> argparse.ArgumentParser:
    if section_names:
        mode_help = (
            f"all | 섹션명({', '.join(section_names)}) | 코드 리스트(0,5,12 / 10-14)"
        )
    else:
        mode_help = "all | JSON 섹션명 | 코드 리스트 (0,5,12 / 10-14)"
    ...
```

`--help` 는 `parse_known_args()` 단계에서 `SystemExit` 을 일으키므로,
1차 파서에서는 `add_help=False` 로 두고 2차 파서에서만 help를 활성화한다.
이렇게 하면 `--help` 출력 시점에 이미 JSON이 로드되어 실제 섹션명이 보인다.

JSON 로드에 실패하면 정적 문구로 폴백해 `--help` 자체는 항상 동작하게 한다.

---

## 3. 가변 폭 코드 포맷팅

```python
def code_width(codes: list[int]) -> int:
    """최대 코드 자릿수에 맞춘 패딩 폭. 최소 2자리로 하위 호환 유지."""
    if not codes:
        return 2
    return max(2, len(str(max(codes))))
```

산출된 `width`는 **한 번 계산해 전 구간에 전달**한다 (R3.3).
파일명 생성은 단일 함수로 통일해 루프·스킵 판정·마크다운이 갈라지지 않게 한다.

```python
def asset_filename(prefix: str, code: int, width: int) -> str:
    return f"{prefix}_{code:0{width}d}.webp"
```

`f"{code:0{width}d}"` 는 중첩 포맷 스펙으로, `width=2`면 `07`,
`width=3`이면 `007`이 된다. 기존 20개 구성은 `max=19` → `width=2`가 되어
`mika_07.webp` 그대로 유지된다.

---

## 4. 젠잇 마크다운 템플릿 조립 구조

### 4.1 중괄호 이스케이프 문제

젠잇 호출 태그에는 `{{url}}` 리터럴이 들어가야 한다. f-string에서 `{`는
`{{`로 이스케이프되므로, **리터럴 `{{url}}` 을 출력하려면 f-string 안에서
`{{{{url}}}}` 로 4중 작성**해야 한다 (R4.3).

가독성이 떨어지고 실수가 나기 쉬우므로, URL 플레이스홀더를 상수로 분리한다.

```python
URL_PLACEHOLDER = "{{url}}"     # 일반 문자열 — 이스케이프 불필요
```

이후 f-string에서는 `{URL_PLACEHOLDER}` 로 단순 삽입한다.

### 4.2 템플릿 구성 요소

```python
GENIT_STATUS_TEMPLATE = (
    "[@id=상태창|name={name}|title={title}|status={status}|desc={desc}]"
)
```

`str.format()` 기반으로 두어 f-string 이스케이프 지옥을 피한다.

### 4.3 조립 함수

`existing_codes`만 받는 점이 중요하다. 실패한 코드가 마크다운에 들어가면
젠잇에서 깨진 이미지가 되므로, 파일 실존 여부를 통과한 코드만 넘긴다 (R4.6).

```python
def build_genit_block(prefix, existing_codes, db, width) -> str:
    lines = []
    for code in existing_codes:
        fname = asset_filename(prefix, code, width)
        label = db.entries[code].label
        lines.append(f"- `{URL_PLACEHOLDER}{prefix}/{fname}` — {label}")

    calls = []
    for code in existing_codes:
        fname = asset_filename(prefix, code, width)
        calls.append(f"![image]({URL_PLACEHOLDER}{prefix}/{fname})")

    status = GENIT_STATUS_TEMPLATE.format(
        name=prefix, title="직책입력", status="현재상태", desc="대사한줄"
    )

    section_guide = build_section_guide(db, existing_codes, prefix, width)
    ...
```

### 4.4 섹션 가이드 (하드코딩 제거)

기존 `print_asset_reference()`는 `neutral -> 00`, `smile -> 01` 식으로
번호를 박아 두었다. 이를 JSON의 실제 구성에서 유도한다 (R4.7).

```python
def build_section_guide(db, existing_codes, prefix, width) -> str:
    present = set(existing_codes)
    blocks = []
    for section, codes in db.sections.items():
        avail = [c for c in codes if c in present]
        if not avail:
            continue
        rows = [
            f"  {db.entries[c].label:<28} -> {asset_filename(prefix, c, width)}"
            for c in avail
        ]
        blocks.append(f"[{section}]\n" + "\n".join(rows))
    return "\n\n".join(blocks)
```

라벨은 `PoseEntry.label`(프롬프트 첫 태그)에서 오므로 JSON을 수정하면
가이드도 자동 갱신된다.

### 4.5 최종 출력 레이아웃

헤더에는 실행 모드 표식을 붙인다 (R4.9, R7.7). 기본 실행은 표식 없음.

```python
def mode_badge(dry_run: bool, mock: bool) -> str:
    if dry_run:
        return " [DRY-RUN]"
    if mock:
        return " [MOCK]"
    return ""
```

```
================================================================
  젠잇(Genit) 복사용 에셋 블록 — {prefix}   (총 N개)[MOCK]
================================================================

### {prefix} 이미지 호출 코드
![image]({{url}}mika/mika_00.webp)
![image]({{url}}mika/mika_01.webp)
...                              ← N줄, 대상 수와 정확히 일치 (R4.1)

### {prefix} 파일 목록
- `{{url}}mika/mika_00.webp` — standing
- `{{url}}mika/mika_01.webp` — standing
...

### {prefix} 상태 매핑 가이드
[emotions]
  standing                     -> mika_00.webp
  ...

[poses]
  standing                     -> mika_10.webp
  ...

### {prefix} 상태창 템플릿
[@id=상태창|name=mika|title=직책입력|status=현재상태|desc=대사한줄]
================================================================
```

---

## 5. 배치 실행 루프

### 5.1 결과 집계 모델

```python
@dataclass
class BatchResult:
    success: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)   # dry-run 시뮬레이션 대상
    aborted: bool = False                              # 연결 끊김 등으로 중단됨
    dry_run: bool = False

    @property
    def existing(self) -> list[int]:
        """
        마크다운 대상 코드.

        기본/mock: 디스크에 실제 파일이 있는 코드만 (R4.6)
        dry-run:   시뮬레이션 대상 전체 (R4.8)
        """
        if self.dry_run:
            return sorted(self.planned)
        return sorted(self.success + self.skipped)
```

기존 구현은 정수 카운터만 유지해 "어느 코드가 실패했는지" 알 수 없었다.
리스트로 바꿔 R6.2를 충족시킨다.

#### `dry_run` 분기를 `existing` 프로퍼티에 둔 이유

dry-run은 API를 호출하지 않으므로 `success` 가 항상 비어 있다. R4.6의
"실존 파일만" 규칙을 그대로 적용하면 마크다운이 0줄이 되어 검증 목적을
잃는다 (R4.8).

분기 위치를 프로퍼티 내부로 넣으면 호출부(`build_genit_block`)는
`result.existing` 만 읽으면 되고 모드를 알 필요가 없다. 호출부마다
`if dry_run` 을 흩뿌리면 누락이 생기므로 단일 지점으로 모은다.

### 5.2 루프 제어 흐름

```
for code in target_codes:
    path = save_dir / asset_filename(prefix, code, width)

    if dry_run:                            → planned 기록, 계획 출력, continue
    if path.exists():                      → skipped 기록, continue   (R5.4)

    if mock:
        png = make_dummy_png(...)          → API 호출 없음 (R7.1)
    else:
        try:
            png = generate_image(...)
        except ConnectionError:            → failed 기록, aborted=True, break (R5.5)
        except Timeout:                    → failed 기록, continue
        except Exception as e:             → failed 기록, continue      (R5.6)

    save_as_webp(png, path)                → success 기록  (mock도 동일 경로, R7.4)
```

**`dry_run` 검사를 `path.exists()` 보다 먼저 두는 이유:** dry-run에서는
파일 존재 여부와 무관하게 "대상 전체"가 계획 목록에 들어가야 한다 (R4.8).
순서가 뒤바뀌면 이미 생성된 파일이 `skipped` 로 빠져 `planned` 가 불완전해진다.

`ConnectionError`만 `break`하는 이유: WebUI가 죽은 상태에서 남은 코드를
계속 시도하면 무의미한 대기가 누적된다. 반면 개별 생성 오류(OOM, 잘못된
샘플러 등)는 다음 코드에서 성공할 수 있으므로 계속 진행한다.

### 5.3 `--dry-run` 모드

파일 I/O와 네트워크 없이 R1~R4를 검증하는 경로다. 대상 코드·파일명·마크다운만
출력하고 탐색기 오픈도 생략한다.

```python
if dry_run:
    result.planned.append(code)
    print(f"  [{code:0{width}d}] (계획) {filename}  ← {entry.label}")
    continue
```

### 5.4 `--mock` 모드 — 더미 이미지 생성

WebUI 없이 **파일 저장·WebP 변환·탐색기 오픈까지** 포함한 종단 검증 경로다 (R7).

#### 설계 원칙

핵심은 `save_as_webp()` 를 **우회하지 않는다**는 점이다. 저장 경로가 갈라지면
검증하려는 Pillow 변환 로직 자체가 테스트 범위에서 빠진다 (R7.4).

따라서 `make_dummy_png()` 는 실제 API가 반환하는 것과 동일한 형태
(**PNG 바이트열**)를 만들어 반환한다. 이후 흐름은 실제 생성과 100% 동일하다.

```
실제:  generate_image() → PNG bytes ─┐
                                     ├→ save_as_webp() → .webp
mock:  make_dummy_png() → PNG bytes ─┘
```

#### 구현

```python
MOCK_SIZE = (208, 304)          # 832x1216 의 1/4, 종횡비 동일 (R7.5)

def make_dummy_png(prefix: str, code: int, width: int, entry: PoseEntry) -> bytes:
    """API 반환값과 동일한 PNG 바이트열을 즉석 생성한다."""
    from PIL import ImageDraw, ImageFont

    # 코드값으로 배경색을 분산시켜 이미지 구분이 눈으로 가능하게 함
    hue = (code * 37) % 360
    bg = hsv_to_rgb(hue, 0.35, 0.90)

    img = Image.new("RGB", MOCK_SIZE, bg)
    draw = ImageDraw.Draw(img)

    try:
        font_big = ImageFont.truetype("arial.ttf", 44)
        font_sm = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font_big = ImageFont.load_default()      # 폴백 (R7.3)
        font_sm = ImageFont.load_default()

    lines = [
        (f"{code:0{width}d}", font_big),
        (prefix, font_sm),
        (entry.section, font_sm),
        (entry.label[:24], font_sm),
        ("MOCK", font_sm),
    ]
    ...  # 세로로 순차 배치

    buf = io.BytesIO()
    img.save(buf, format="PNG")      # ← PNG로 내보내야 실제 경로와 동일
    return buf.getvalue()
```

#### 폰트 폴백

`ImageFont.truetype("arial.ttf", ...)` 는 Windows에서 시스템 폰트를 찾지만
Linux/macOS나 폰트 누락 환경에서는 `OSError` 를 던진다. 광범위한 `except
Exception` 으로 잡아 `load_default()` 로 폴백한다 (R7.3). 더미 이미지는
검증용이므로 폰트 품질보다 **예외 없이 완주하는 것**이 우선이다.

#### 샘플러 조회 생략

`--mock` 에서는 `resolve_sampler()` 를 호출하지 않는다. 이 함수는
`requests.get()` 을 시도하며 WebUI가 없으면 5초 타임아웃을 소모한다.
R7.1의 "어떠한 HTTP 요청도 발생시키지 않아야" 를 충족시키려면 건너뛰어야 한다.

```python
sampler_name = "(mock)" if (mock or dry_run) else resolve_sampler()
```

### 5.5 모드 우선순위 결정

R7.8에 따라 더 안전한(부작용이 적은) 쪽을 우선한다.

```python
if args.test:
    sys.exit(run_self_test(base_dir))        # 최우선, 즉시 종료

dry_run = args.dry_run
mock = args.mock and not args.dry_run        # dry-run 우선 (R7.8)

if args.mock and args.dry_run:
    print("[WARN] --dry-run 이 우선합니다. --mock 무시됨")
```

`--test` 는 다른 인자를 전혀 필요로 하지 않으므로(R8.4) 인자 검증보다
먼저 분기해 즉시 종료한다.

---

## 5A. 자체 검증 하니스 (`--test`)

### 5A.1 설계 원칙

**실제 구현 함수를 직접 호출한다** (R8.7). 검증 코드에 로직을 복제하면
구현이 바뀔 때 테스트가 함께 틀어져 회귀를 놓친다.

외부 테스트 프레임워크(`pytest` 등)를 도입하지 않는 이유는 제약 조건상
의존성이 `requests`, `Pillow` 로 한정되고, 단독 CLI 실행이 가능해야
하기 때문이다.

### 5A.2 결과 수집 구조

```python
@dataclass
class TestReport:
    passed: int = 0
    failed: int = 0
    warned: int = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        tag = "[PASS]" if ok else "[FAIL]"
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))
        return ok

    def warn(self, name: str, detail: str = "") -> None:
        self.warned += 1
        print(f"  [WARN] {name}" + (f" — {detail}" if detail else ""))

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0        # R8.2, R8.3
```

`check()` 가 `bool` 을 반환하는 이유: 선행 검사가 실패하면 후속 검사를
건너뛰어야 하는 경우(JSON 파싱 실패 → 구조 검사 무의미)가 있다.

### 5A.3 검사 항목 매핑

| ID | 검사 | 방식 |
|---|---|---|
| T1 | JSON 파일 존재 | `os.path.exists()` |
| T2 | JSON 문법 | `json.load()` + `JSONDecodeError.lineno/colno` |
| T3 | 최상위 구조 | 비주석 섹션이 `dict` 인지 |
| T4 | 비정수 키 | 원본 순회하며 `int()` 시도 → 실패 목록 |
| T5 | 빈 프롬프트 | `str.strip()` 이 빈 값인 항목 |
| T6 | 중복 코드 | 섹션 간 코드 교집합 |
| T7 | 유효 엔트리 ≥1 | `len(db.entries) > 0` |
| T8 | 정렬 정확성 | `db.all_codes == sorted(db.all_codes)` 및 사전순과 비교 |
| T9 | 패딩 폭 | `code_width(codes) == max(2, len(str(max(codes))))` |
| T10 | 표현식 파서 | 고정 케이스 왕복 검증 (아래) |
| T11 | 파일명 조립 | `asset_filename("x", 7, 2) == "x_07.webp"` 등 |
| T12 | 마크다운 라인 수 | `build_genit_block()` 결과의 `![image](` 개수 |
| T13 | URL 리터럴 | 결과 문자열에 `{{url}}` 포함 |

T4~T6 은 데이터 품질 문제이므로 `[FAIL]` 이 아닌 `[WARN]` 으로 처리한다.
50개 항목 중 오타 1개로 종료 코드가 1이 되면 CI 게이트로 쓰기 불편하고,
런타임에서는 이미 경고 후 계속 진행하도록 설계했기 때문이다(R1.4~R1.6).

### 5A.4 T10 파서 검증 케이스

```python
CASES = [
    ("20-29",       list(range(20, 30))),
    ("0,3,7",       [0, 3, 7]),
    ("0-5,10,20-22", [0,1,2,3,4,5,10,20,21,22]),
    ("29-20",       list(range(20, 30))),      # 역순 교정
    ("0-5,3",       [0,1,2,3,4,5]),            # 중복 정규화
    (" 1 , 2 ",     [1, 2]),                   # 공백 허용
]
```

### 5A.5 T12 검증 방식

`build_genit_block()` 이 문자열을 **반환**하도록 설계한 이유가 여기서 드러난다.
출력과 조립이 분리되어 있으므로 stdout 캡처 없이 문자열을 직접 검사할 수 있다.

```python
block = build_genit_block(prefix="t", codes=[0, 5, 12], db=db, width=2, badge="")
actual = block.count("![image](")
report.check("T12 마크다운 라인 수", actual == 3, f"{actual}/3")
```

---

## 6. 함수 시그니처 요약

| 함수 | 상태 | 시그니처 |
|---|---|---|
| `load_pose_db` | 재작성 | `(base_dir: str) -> PoseDatabase` |
| `parse_codes_expr` | 신규 | `(expr: str) -> list[int]` |
| `looks_like_code_expr` | 신규 | `(value: str) -> bool` |
| `resolve_targets` | 신규 | `(db, mode, codes_expr) -> list[int]` |
| `code_width` | 신규 | `(codes: list[int]) -> int` |
| `asset_filename` | 신규 | `(prefix, code, width) -> str` |
| `mode_badge` | 신규 | `(dry_run: bool, mock: bool) -> str` |
| `make_dummy_png` | 신규 | `(prefix, code, width, entry) -> bytes` |
| `run_batch` | 신규(분리) | `(..., dry_run=False, mock=False) -> BatchResult` |
| `build_section_guide` | 신규 | `(db, codes, prefix, width) -> str` |
| `build_genit_block` | 재작성 | `(prefix, codes, db, width, badge) -> str` |
| `build_parser` | 신규 | `(section_names=None) -> ArgumentParser` |
| `run_self_test` | 신규 | `(base_dir: str) -> int` (종료 코드) |
| `resolve_sampler` | 동작 불변 (전송만 세션화) | `() -> str` |
| `save_as_webp` | 동작 불변 (쓰기만 원자화) | `(png_bytes, path: Path, quality=90) -> None` |
| `generate_image` | 동작 불변 (전송만 세션화) | `(prompt, neg, sampler) -> bytes` |
| `validate_prefix` | 신규 | `(prefix: str) -> str` |
| `open_in_explorer` | 신규 | `(path: Path) -> None` |
| `print_summary` | 신규 | `(result, save_dir, badge) -> None` |
| `execute` | 신규 | `(args, base_dir) -> int` |

> R5.1/R5.2 의 "불변"은 **관찰 가능한 동작**을 뜻한다. 변환 파라미터
> (RGBA/P→RGB, `quality=90`, `method=6`)와 샘플러 후보·폴백 순서는 그대로다.
> 전송 계층(세션 재사용)과 쓰기 방식(임시 파일 경유)만 바뀌었고, 이는
> 산출물의 바이트를 바꾸지 않는다. 자세한 근거는 9장 참조.

`print_asset_reference()`는 `build_genit_block()`으로 대체되어 제거된다.
조립과 출력을 분리해 문자열 자체를 테스트할 수 있게 만드는 것이 목적이다.

### 6.1 불변 함수 3종의 재사용 관계

| 함수 | 기본 | `--mock` | `--dry-run` | `--test` |
|---|---|---|---|---|
| `resolve_sampler()` | 호출 | 생략 (R7.1) | 생략 | 생략 |
| `generate_image()` | 호출 | 생략 (R7.1) | 생략 | 생략 |
| `save_as_webp()` | 호출 | **호출** (R7.4) | 생략 | 생략 |

`save_as_webp()` 만 mock에서 유지되는 것이 핵심이다. 이것이 mock을
"파일명만 흉내내는 시뮬레이션"이 아니라 실제 WebP 인코딩까지 검증하는
종단 테스트로 만든다.

---

## 7. 오류 처리 전략

| 상황 | 처리 | 종료 코드 |
|---|---|---|
| JSON 파일 없음 | 경로 안내 + 종료 | 1 |
| JSON 파싱 실패 | 라인/컬럼 포함 메시지 | 1 |
| 유효 엔트리 0개 | 스키마 안내 | 1 |
| 알 수 없는 `--mode` | 사용 가능 목록 안내 | 1 |
| `--codes` 구문 오류 | 예시(`20-29`, `0,3,7`) 안내 | 1 |
| 비정수 키 / 빈 값 / 중복 코드 | 경고 후 계속 | 0 |
| WebUI 연결 불가 | 루프 중단, 집계 출력 | 1 |
| 개별 생성 실패 | 경고 후 다음 코드 | 0 |
| `--test` 에서 `[FAIL]` 발생 | 실패 건수 요약 | 1 (R8.3) |
| `--test` 전 항목 통과 | 요약 출력 | 0 (R8.2) |
| `--mock` 폰트 로딩 실패 | 기본 폰트 폴백, 경고 없음 | 0 (R7.3) |
| `--mock` + `--dry-run` 동시 지정 | dry-run 채택, mock 무시 경고 | 0 (R7.8) |
| `--codes` + `--mode` 표현식 충돌 | `--codes` 채택, mode 무시 경고 | 0 (R2.9) |
| `--prefix` 누락 (비 `--test`) | 필수 인자 안내 | 2 (argparse) |

경고성 상황에서 종료하지 않는 이유는, 50개 항목 중 1개 오타로 전체 배치가
막히면 오히려 운영에 불편하기 때문이다. 단 경고는 반드시 눈에 보이게 출력한다.

### 7.1 `--prefix` 필수성의 조건 분기

R8.4에 따라 `--test` 에서는 `--prefix` / `--char_prompt` 가 필요 없다.
`argparse` 의 `required=True` 는 무조건 강제하므로 사용할 수 없다.

```python
parser.add_argument("--prefix")          # required 미지정
parser.add_argument("--char_prompt")

# 파싱 후 수동 검증
if not args.test:
    missing = [n for n in ("prefix", "char_prompt") if not getattr(args, n)]
    if missing:
        parser.error(f"다음 인자가 필요합니다: {', '.join('--' + m for m in missing)}")
```

`parser.error()` 를 쓰면 argparse 표준 형식(usage + 메시지, 종료 코드 2)을
유지할 수 있어 직접 `sys.exit()` 하는 것보다 일관적이다.

---

## 8. 하위 호환성

1. 기존 `pose_database.json`(emotions 10 + poses 10)을 그대로 로드한다.
2. `--mode all/emotions/poses` 명령이 동일하게 동작한다.
3. `width=2` 가 산출되어 파일명이 기존과 완전히 일치한다.
4. `.kiro/steering/sd_char_gen.md` 의 실행 명령 템플릿은 수정 불필요.
   (`--codes`, `--dry-run`, `--mock`, `--test` 는 추가 옵션이며 기본 동작에
   영향 없음)
5. `--mode` 가 코드 표현식을 새로 받아들이지만, 기존 값(`all`, 섹션명)은
   알파벳을 포함하므로 `CODE_EXPR_PATTERN` 에 매칭되지 않는다. 따라서
   기존 명령의 해석이 바뀌지 않는다.

### 8.1 mock 산출물과 실제 산출물의 혼입 방지

`--mock` 은 실제 저장 경로(`generated_assets/{prefix}/`)에 파일을 쓴다.
실제 생성 파일과 섞이면 나중에 구분이 어렵다. 두 가지로 완화한다.

1. 더미 이미지에 `MOCK` 텍스트를 그려 육안 식별이 가능하게 한다 (R7.3).
2. 검증용으로는 `--prefix test` 처럼 전용 접두어를 쓰도록 문서에 안내한다.

파일명 규칙 자체를 바꾸지 않는 이유는, 규칙(R3)이 검증 대상이기 때문이다.
mock 전용 접미사를 붙이면 정작 검증하려는 파일명 조립 로직이 우회된다.

---

## 9. 리팩터링 및 보안 강화 (구현 후 반영)

구현 완료 후 전체 리뷰에서 발견한 결함과 그 처리다. 요구사항의 기능 범위는
바뀌지 않았고, 검증 항목만 T14·T15가 추가되었다.

### 9.1 명령 주입 (심각)

```python
os.system(f'explorer "{save_dir}"')   # save_dir 에 사용자 입력 prefix 포함
```

`prefix` 가 셸 명령 문자열로 보간되었다. `--prefix 'a" & calc & "'` 는
인용부호를 닫고 임의 명령을 실행시킨다.

**처리:** 셸을 거치지 않는 `os.startfile()` 로 교체.

```python
def open_in_explorer(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        os.startfile(path)
    except OSError as e:
        print(f"[WARN] 탐색기를 열지 못했습니다: {e}")
```

### 9.2 경로 이탈

`prefix` 가 경로 세그먼트로 직접 쓰였다. `--prefix ../../..` 는
`generated_assets` 밖에 파일을 쓴다.

**처리:** 입구에서 화이트리스트 검증. 블랙리스트가 아니라 허용 문자만
지정했으므로 새로운 위험 문자가 생겨도 자동으로 막힌다.

```python
SAFE_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
```

이 하나로 9.1의 인용부호 주입과 9.2의 경로 이탈이 함께 차단된다.
`os.startfile()` 교체는 두 번째 방어선이다.

### 9.3 자원 소진

`parse_codes_expr("0-999999999")` 는 `set` 에 10억 개 정수를 넣으려 한다.
DB 필터링은 그 뒤에 일어나므로 아무 소용이 없다.

**처리:** `MAX_CODE = 9_999` 상한과 음수 거부를 파싱 시점에 적용.

### 9.4 폰트 폴백 시 레이아웃 붕괴

```python
y += 52 if font is font_big else 20        # 객체 동일성 비교
```

`ImageFont.load_default()` 를 두 번 호출해도 같은 객체가 반환될 수 있다.
폰트 폴백이 일어나면 `font_big is font_small` 이 참이 되어 모든 줄이
큰 간격을 쓰고 캔버스를 벗어난다. Windows에서는 `arial.ttf` 가 있어
재현되지 않는 경로였다.

**처리:** 간격을 데이터로 명시해 폰트 객체와의 결합을 끊었다.

```python
rows = (
    (format_code(code, width), font_big, MOCK_GAP_BIG),
    (prefix, font_small, MOCK_GAP_SMALL),
    ...
)
```

### 9.5 중단 시 반쪽 파일

`save_as_webp()` 가 최종 경로에 직접 썼다. 쓰는 중 중단되면 잘린 파일이
남고, 재개 로직(R5.4)이 그것을 완성된 파일로 보고 영구히 건너뛴다.
재개 지원을 설계에 넣은 이상 이건 실제 데이터 손실 경로다.

**처리:** `.part` 임시 파일에 쓰고 `os.replace()` 로 원자적 교체.
실패 시 임시 파일을 정리한다. 변환 파라미터는 건드리지 않았다.

### 9.6 종료 정책 분산

`load_pose_db()`, `resolve_targets()` 가 직접 `sys.exit()` 했다.
헬퍼가 프로세스를 죽이면 단위 검증에서 호출할 수 없고, 종료 조건이
여러 파일 위치에 흩어진다.

**처리:** `ConfigError(message, hint)` 를 올리고 `main()` 에서만
종료 코드로 변환. 덕분에 T14·T15가 `validate_prefix()` 와
`parse_codes_expr()` 를 서브프로세스 없이 직접 검증한다.

### 9.7 그 외

| 항목 | 변경 | 이유 |
|---|---|---|
| `requests.Session` | `lru_cache` 로 단일 세션 재사용 | 배치당 수십 요청의 TCP 핸드셰이크 제거 |
| 폰트 로딩 | `lru_cache(maxsize=1)` | 장수만큼 반복 로드하던 것을 1회로 |
| `result.existing` | 지역 변수로 1회 계산 | 프로퍼티가 호출마다 `sorted()` 수행 |
| `os.path` → `pathlib.Path` | 전면 교체 | 경로 조작 표준화, `with_name`/`unlink(missing_ok)` 활용 |
| `main()` | `execute()` / `print_summary()` 분리 | 80줄 함수를 책임 단위로 분할 |
| `main(argv)` | 인자 주입 가능 | 서브프로세스 없이 CLI 경로 검증 가능 |
| `dataclass(slots=True)` | 4개 모델 적용 | 인스턴스 `__dict__` 제거 |
| `generate_image` | `images` 부재 명시 검사 | `KeyError`/`IndexError` 대신 읽을 수 있는 메시지 |
| `KeyboardInterrupt` | 종료 코드 130 | Ctrl+C 시 트레이스백 대신 메시지 |
| 출력 기호 | `—`/`→`/`←` → ASCII | cp949 리다이렉션 환경 호환 |
| `_iter_sections()` | 주석 섹션 필터 공용화 | 3곳에 중복됐던 `startswith("_")` 통합 |

### 9.8 추가 검증 항목

| ID | 검사 | 대응 |
|---|---|---|
| T14 | 위험 `prefix` 7종 차단 (`..`, `a/b`, `a" & calc & "`, 빈 값, 65자 등) | 9.1, 9.2 |
| T14b | 정상 `prefix` 허용 및 공백 트림 | 9.2 |
| T15 | 잘못된 표현식 4종 거부 (`abc`, `1-`, 상한 초과, 음수) | 9.3 |

`--test` 총 검사 수: 15 → 18.

---

## 10. 프로필 축 도입 (성별 충돌 해소)

### 10.1 문제

`POS_BASE` 가 `1girl, solo` 를 하드코딩했다. 남성 캐릭터를 만들려고
`--char_prompt "1boy, ..."` 를 주면 최종 프롬프트가 이렇게 된다.

```
masterpiece, ..., 1girl, solo, ..., 1boy, short black hair, ...
```

`1girl` 과 `1boy` 가 동시에 들어가고 `solo` 까지 있어 모델이 모순된 지시를
받는다. 구조적 원인은 프롬프트가 **덧붙이기만 가능**하다는 점이었다.
`--custom_neg` 도 `COMMON_NEG` 에 append 만 한다. 제거·치환 수단이 없었다.

### 10.2 성별을 포즈 섹션에 넣지 않는 이유

성별은 감정·포즈와 **직교하는 축**이다. `neutral expression` 은 남녀 공통이고
JSON 의 포즈 프롬프트에도 성별 태그가 없다.

섹션에 성별을 섞으면 조합이 곱셈으로 늘어난다.

```
emotions × {female, male, otokonoko} = 30개
poses    × {female, male, otokonoko} = 30개   → 감정 하나 수정에 3곳 변경
```

따라서 별도 축(`_profiles`)으로 분리한다.

### 10.3 데이터 구조

```json
"_profiles": {
  "female": {
    "base_positive": "masterpiece, ..., 1girl, solo",
    "base_negative": "worst quality, ..., 1boy, male, masculine, beard"
  },
  "male": { ... },
  "male_otokonoko": { ... }
}
```

`_` 접두어라 기존 `_iter_sections()` 가 포즈 섹션에서 자동으로 제외한다.
별도 필터가 필요 없었다.

### 10.4 완전 대체 방식과 그 대가

프로필의 `base_positive` / `base_negative` 가 `POS_BASE` / `COMMON_NEG` 를
**완전히 대체**한다. 부분 병합이 아니다.

그 결과 품질 태그(`masterpiece, best quality, ...`)를 각 프로필이 중복해서
갖는다. 프로필이 3개면 품질 태그가 3번 적힌다.

**대안이었던 분할 방식**(`QUALITY_BASE` 는 코드에, 성별만 프로필에)은 중복이
없지만, 품질 태그를 바꾸려면 코드를 고쳐야 한다. 완전 대체를 택한 이유는
프로필별로 품질 태그까지 다르게 쓸 수 있는 자유도가 더 크고, "데이터는 JSON"
원칙에 부합하기 때문이다.

중복이 문제가 되면 `"extends": "base"` 같은 상속 문법을 나중에 추가할 수 있다.
지금은 프로필이 3개라 중복 비용이 크지 않다.

### 10.5 하위 호환

| 상황 | 동작 |
|---|---|
| `_profiles` 있음, `--profile` 생략 | `DEFAULT_PROFILE`(`female`) 적용 + 콘솔 명시 |
| `_profiles` 있음, 기본 프로필명 없음 | 정의 순서상 첫 프로필 폴백 |
| `_profiles` 없음, `--profile` 생략 | `POS_BASE`/`COMMON_NEG` 를 가상 프로필 `(built-in)` 로 감싸 기존 동작 유지 |
| `_profiles` 없음, `--profile` 지정 | `ConfigError` — 조용히 무시하지 않는다 |
| 프로필 정의 불량 (positive 누락, dict 아님) | 경고 후 해당 프로필만 제외 |

`--profile` 을 필수로 만들면 실수가 불가능해지지만 기존 명령어가 전부 깨진다.
기본값 + 명시적 출력을 택해 양쪽을 얻었다.

```
[PROFILE] 미지정 - 기본값 'female' 적용
```

### 10.6 태그 충돌 감지

정규화 후 집합 교집합으로 같은 태그를 찾는다.

```python
def normalize_tag(raw: str) -> str:
    tag = raw.strip().lower().translate(_BRACKET_TABLE)   # ()[]{} 제거
    tag = WEIGHT_SUFFIX_PATTERN.sub("", tag)              # 트레일링 :1.3 제거
    return " ".join(tag.split())                          # 내부 공백 정규화
```

괄호를 먼저 제거하고 가중치 접미사를 나중에 지우는 순서가 중요하다.
`(breasts:1.3)` → `breasts:1.3` → `breasts`. 순서가 반대면 괄호가 남는다.

**검사 시점 2곳**

| 시점 | 대상 | 등급 |
|---|---|---|
| `--test` (T19) | 각 프로필의 `base_positive` vs `base_negative` | `[WARN]` |
| 실행 시작 | `base_positive + char_prompt` vs `base_negative + custom_neg` | `[WARN]` |

`--test` 만으로는 `--char_prompt` 가 프로필 네거티브와 충돌하는 경우를 잡을 수
없어 런타임 검사를 함께 넣었다.

경고에 그치고 중단하지 않는 이유: 의도적으로 같은 태그를 양쪽에 두는
프롬프트 기법이 존재하고, 50개 항목 중 하나로 배치를 막으면 운영에 불편하다.

**한계.** 같은 문자열만 잡는다. `1girl` 과 `1boy` 는 서로 다른 문자열이므로
검출되지 않는다. 의미적 상충까지 잡으려면 상호배타 쌍 목록이 필요하고,
그건 완전하게 만들 수 없어 도입하지 않았다. `male_otokonoko` 프로필이
`feminine face` 를 포지티브에 두면서 네거티브에는 `feminine` 을 넣지 않은 것이
이 한계를 우회하는 실제 예다.

### 10.7 추가 함수 및 검증

| 함수 | 역할 |
|---|---|
| `normalize_tag` | 괄호·가중치·대소문자·공백 정규화 |
| `split_tags` | 쉼표 분리 + 정규화, 빈 토큰 제거 |
| `find_tag_conflicts` | 교집합 반환 |
| `join_tags` | 빈 조각 건너뛰고 쉼표 결합 |
| `_parse_profiles` | `_profiles` → `dict[str, Profile]`, 불량 항목 경고 |
| `resolve_profile` | `--profile` 해석 및 폴백 |
| `peek_choices` | `--help` 용 (섹션명, 프로필명) 동시 조회 |

| ID | 검사 |
|---|---|
| T16 | `normalize_tag` 7케이스 (가중치·괄호·대소문자·공백·음수 가중치) |
| T17 | `find_tag_conflicts` 4케이스 |
| T18 | 프로필 로드 개수 |
| T19 | 프로필별 포지티브/네거티브 충돌 |
| T20 | 기본 프로필 존재 여부 |

`--test` 총 검사 수: 18 → 23.

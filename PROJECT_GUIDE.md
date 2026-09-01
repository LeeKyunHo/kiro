# SD 캐릭터 에셋 배치 생성 파이프라인 — 프로젝트 가이드

> 이 문서는 AI 에이전트가 이 프로젝트를 처음 접했을 때 전체 구조와 사용법을
> 파악하기 위한 단일 참조 문서다. 코드를 읽지 않고도 정확히 조작할 수 있도록
> 실제 구현값을 기준으로 작성했다.

---

## 0. 한 줄 요약

로컬 SD WebUI API를 호출해 캐릭터 챗봇용 이미지 에셋을 일괄 생성하고,
젠잇(Genit) 플랫폼에 붙여넣을 마크다운 호출 코드를 자동 조립하는 CLI 도구.

**핵심 설계 원칙: 프롬프트 데이터는 JSON에, 로직은 Python에.**
JSON 항목을 늘리면 코드 수정 없이 생성 장수가 늘어난다.

---

## 1. 파일 구성

작업 루트: `C:\Users\USER\kiro`

```
kiro/
├── sd_batch_generator.py                    # 메인 실행 스크립트
├── pose_database.json                       # 프롬프트 데이터 (프로필 + 포즈)
├── PROJECT_GUIDE.md                         # 이 문서
├── 사용법.txt                                # 설치·사용 가이드 (사람용)
├── .gitignore / .gitattributes
│
├── references/                              # IP-Adapter 참조 이미지 (git 추적)
│   ├── README.md
│   └── {prefix}.png                         # --prefix 와 같은 이름
│
├── .kiro/
│   ├── steering/
│   │   └── sd_char_gen.md                   # Kiro 자동 실행 규칙
│   └── specs/
│       ├── dynamic-pose-pipeline/            # 동적 순회·프로필 (완료)
│       │   ├── requirements.md               # R1~R8
│       │   ├── design.md                     # 설계 (10장)
│       │   └── tasks.md                      # 13단계
│       └── image-reference-pipeline/          # 참조 이미지 (진행 중)
│           ├── requirements.md               # R1~R7
│           ├── design.md                     # 설계 (11장)
│           └── tasks.md                      # 17단계
│
└── generated_assets/                        # 실행 시 자동 생성 (git 제외)
    └── {prefix}/
        └── {prefix}_{NN}.webp
```

`references/` 는 git 으로 추적한다. `generated_assets/` 는 재생성 가능한
파생물이지만 참조 이미지는 잃으면 같은 캐릭터를 재현할 수 없는 원본 입력이고,
여러 PC 간 동기화에도 필요하다.

### 파일별 역할

| 파일 | 역할 | 수정 빈도 |
|---|---|---|
| `sd_batch_generator.py` | 전체 로직. API 호출, WebP 변환, 마크다운 조립, 자체 검증 | 낮음 |
| `pose_database.json` | 프로필·포즈·표정 프롬프트. **일상 편집 대상** | 높음 |
| `.kiro/steering/sd_char_gen.md` | Kiro가 `"캐릭터 생성:"` 트리거를 감지해 자동 실행하는 규칙 | 낮음 |
| `.kiro/specs/.../requirements.md` | 요구사항 명세 (R1~R8, 수용 기준) | 참조용 |
| `.kiro/specs/.../design.md` | 아키텍처, 설계 결정 근거, 보안 강화 이력 | 참조용 |
| `.kiro/specs/.../tasks.md` | 구현 단계 체크리스트 (전부 완료) | 참조용 |
| `사용법.txt` | WebUI `webui-user.bat` 옵션, Settings 탭 조치 사항 | 참조용 |

### 범위 밖 파일

- `캐챗 정리.txt` — 롤플레잉 시나리오 설정 문서. 이 파이프라인과 기술적 연관이 없다.
  파이프라인 조작 시 참조하지 않는다.

---

## 2. 의존성 및 실행 환경

| 항목 | 값 |
|---|---|
| Python | 3.10+ (`slots=True` dataclass 사용, 검증 환경 3.12.4) |
| 필수 패키지 | `requests`, `Pillow` |
| OS | Windows 전제 (탐색기 자동 오픈). 다른 OS에서도 생성은 동작 |
| 외부 서비스 | SD WebUI (Automatic1111 또는 Forge), `--api` 옵션 필수 |

```powershell
pip install requests pillow
```

---

## 3. 데이터 흐름

```
pose_database.json
        │  read_pose_json() → parse_pose_db()
        ▼
   PoseDatabase(entries, sections, profiles, warnings)
        │
        ├─ resolve_profile()  ── Profile(base_positive, base_negative)
        ├─ resolve_targets()  ── 대상 코드 리스트
        └─ code_width()       ── 파일명 패딩 폭
        │
        ▼
   run_batch()
        │  dry_run  → planned 기록만 (파일·네트워크 없음)
        │  mock     → make_dummy_png() ─┐
        │  기본     → generate_image() ─┴→ save_as_webp() → .webp
        ▼
   BatchResult(success, skipped, failed, planned)
        │
        ├─ print_summary()
        ├─ open_in_explorer()      (Windows, dry-run 제외)
        └─ build_genit_block()     → 젠잇 마크다운 출력
```

`--test`는 이 파이프라인을 타지 않고 `run_self_test()`로 별도 분기한다.

---

## 4. `pose_database.json` 스펙

### 4.1 전체 구조

```json
{
  "_schema":   { "...": "메타/주석. 파싱 제외" },
  "_profiles": { "female": { "base_positive": "...", "base_negative": "..." } },
  "emotions":  { "00": "프롬프트 태그", "01": "..." },
  "poses":     { "10": "...", "11": "..." }
}
```

- 최상위 키가 `_`로 시작하면 **주석/메타로 간주해 포즈 파싱에서 제외**된다.
- `_profiles`는 예외적으로 프로필 파서가 별도로 읽는다.
- 그 외 최상위 키는 모두 **포즈 섹션**이며, 섹션명이 곧 `--mode` 값이 된다.

### 4.2 포즈 섹션 규칙

| 규칙 | 내용 |
|---|---|
| 코드 키 | 문자열로 적되 **정수로 해석**된다. `"00"`, `"7"`, `"105"` 모두 유효 |
| 정렬 | 숫자 크기 기준. 사전순이 아니다 (`"10"` < `"2"` 문제 회피) |
| 범위 | 0 ~ 9999 (`MAX_CODE`) |
| 결번 | 허용. `0, 1, 5, 42`만 있으면 그 4개만 순회 |
| 중복 | 섹션이 달라도 코드가 같으면 경고 후 **나중 값 사용** |
| 빈 값 | 공백뿐인 프롬프트는 경고 후 제외 |
| 비정수 키 | 경고 후 제외 (프로세스는 계속) |
| **첫 태그 = 라벨** | 쉼표 전까지가 콘솔 출력·상태 매핑 가이드의 라벨 |
| 성별 태그 금지 | 포즈에 `1girl`/`1boy`를 넣지 않는다. 성별은 프로필 축 |
| 섹션명 | 숫자만으로 짓지 말 것. 코드 표현식으로 오인됨 |

**첫 태그가 라벨이라는 점이 실무에서 가장 중요하다.**

```json
"00": "neutral expression, standing, front view"   ← 라벨: neutral expression  (좋음)
"00": "standing, front view, neutral expression"   ← 라벨: standing            (구분 불가)
```

모든 항목이 `standing`으로 시작하면 상태 매핑 가이드가 전부 `standing`이 되어
무의미해진다.

### 4.3 `_profiles` 스펙

```json
"_profiles": {
  "female": {
    "base_positive": "masterpiece, best quality, ..., 1girl, solo",
    "base_negative": "worst quality, ..., 1boy, male, masculine, beard"
  }
}
```

| 키 | 필수 | 설명 |
|---|---|---|
| `base_positive` | O | 포지티브 기본 태그. 스크립트 `POS_BASE`를 **완전히 대체** |
| `base_negative` | X | 네거티브 기본 태그. 스크립트 `COMMON_NEG`를 **완전히 대체** |

**완전 대체이므로 품질 태그(`masterpiece, best quality, ...`)도 각 프로필에
포함해야 한다.** 빼먹으면 품질 태그 없이 생성된다.

현재 정의된 프로필 3종:

| 프로필 | 포지티브 성별 축 | 네거티브 성별 축 |
|---|---|---|
| `female` | `1girl, solo` | `1boy, male, masculine, beard, mustache, facial hair, muscular` |
| `male` | `1boy, solo, masculine` | `1girl, female, breasts, feminine` |
| `male_otokonoko` | `1boy, solo, androgynous, feminine face, slender build, delicate features` | `1girl, breasts, muscular, beard, mustache, facial hair` |

`male_otokonoko`는 포지티브에 `feminine face`가 있으므로 네거티브에 `feminine`을
**의도적으로 넣지 않았다.** 넣으면 의미가 상충한다.

### 4.4 확장 예시

의상 세트 10종을 추가하려면 섹션 하나만 붙이면 된다. 스크립트는 손대지 않는다.

```json
  "outfits": {
    "20": "school uniform, standing, full body, neutral pose",
    "21": "casual hoodie, standing, relaxed posture",
    "22": "elegant dress, standing, formal pose"
  }
```

추가 직후 `--mode outfits`가 자동으로 사용 가능해지고, `--help`에도 노출된다.

---

## 5. CLI 레퍼런스

```
python sd_batch_generator.py [옵션]
```

| 플래그 | 기본값 | 필수 | 설명 |
|---|---|---|---|
| `--prefix` | — | O* | 에셋 식별자. `^[A-Za-z0-9_-]{1,64}$` |
| `--char_prompt` | — | O* | 캐릭터 외형 태그 |
| `--custom_neg` | `""` | X | 프로필 네거티브에 **추가**(대체 아님) |
| `--profile` | `female` | X | `_profiles` 중 선택 |
| `--mode` | `all` | X | 대상 범위 (5가지 형태) |
| `--codes` | `None` | X | 코드 직접 지정. `--mode`보다 우선 |
| `--dry-run` | `False` | X | 파일·네트워크 없이 계획만 출력 |
| `--mock` | `False` | X | 더미 이미지를 실제 저장 |
| `--test` | `False` | X | 자체 진단 후 종료 |

**참조 이미지 (IP-Adapter)**

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--ref_image` | `None` | 참조 이미지 경로 직접 지정 (자동 탐색 무시) |
| `--ref_weight` | `0.7` | 적용 강도 0.0~2.0 |
| `--no_ref` | `False` | 참조 이미지 무시 (비교 실험용) |
| `--cn_module` | `None` | ControlNet 전처리기 수동 지정 |
| `--cn_model` | `None` | ControlNet 모델 수동 지정 |

**태그 역추출**

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--from_image` | `None` | 이미지에서 태그 추출 후 종료 (생성 안 함) |
| `--interrogator` | `deepdanbooru` | `deepdanbooru` \| `clip` |

\* `--test` 또는 `--from_image` 사용 시에는 `--prefix`, `--char_prompt`가
불필요하다.

### 5.1 `--mode`가 받는 5가지 형태

| 형태 | 예시 | 결과 |
|---|---|---|
| 프리셋 | `all` | JSON 전체 코드 |
| 섹션명 | `emotions` | 해당 섹션만 |
| 코드 열거 | `0,5,12` | 지정 코드만 |
| 코드 범위 | `10-14` | 범위 내 |
| 혼합 | `0-5,12` | 합집합 |

판별 방식: 값이 정규식 `^[\s\d,\-]+$`에 매칭되면 코드 표현식, 아니면 섹션명.
섹션명에는 알파벳이 있고 코드 표현식에는 없으므로 충돌하지 않는다.

역순 범위(`29-20`)는 자동 교정, 중복(`0-5,3`)은 정규화된다.

### 5.2 우선순위

```
--codes  >  --mode 코드 표현식  >  --mode 섹션명/all
--test   >  --dry-run  >  --mock  >  기본
```

`--codes`와 `--mode` 코드 표현식이 동시에 오면 `--codes`를 채택하고 경고한다.
`--mock`과 `--dry-run`이 동시에 오면 부작용이 적은 `--dry-run`을 채택한다.

---

## 6. 4가지 실행 모드

| 모드 | API 호출 | 파일 쓰기 | 탐색기 | 마크다운 대상 | 배지 |
|---|---|---|---|---|---|
| 기본 | O | O | O | 실존 파일만 | — |
| `--mock` | X | O (더미) | O | 실존 파일만 | `[MOCK]` |
| `--dry-run` | X | X | X | **대상 전체** | `[DRY-RUN]` |
| `--test` | X | X | X | 내부 검증만 | — |

참조 이미지 관련 단계는 모드별로 다르게 동작한다.

| 단계 | 기본 | `--mock` | `--dry-run` | `--test` |
|---|---|---|---|---|
| 참조 이미지 탐색 | O | **O** | O (경로만) | 임시 파일로 |
| base64 인코딩 | O | **O** | X | O |
| ControlNet 목록 조회 | O | X | X | X |
| 페이로드 조립 | O | **O** | X | O |
| API 전송 | O | X | X | X |

`--mock` 에서 탐색·인코딩·조립을 모두 수행하는 이유는, 파일을 못 찾거나
인코딩이 깨지거나 페이로드가 잘못 조립되는 것이 **mock 에서 잡아야 할 결함**
이기 때문이다. 전송만 생략한다.

`--dry-run` 은 파일 I/O 를 하지 않는 것이 계약이므로 존재 여부와 경로만
출력하고 내용을 읽지 않는다.

`--mock` 에서는 ControlNet 조회(HTTP)를 하지 않으므로 자동 탐지가 불가능하다.
주입 경로를 검증하려면 `--cn_module` 과 `--cn_model` 을 둘 다 수동 지정한다.

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "x" --mock `
  --cn_module "ip-adapter_clip_sdxl" --cn_model "ip-adapter_xl [test]"
```

주입된 상태로 생성된 더미 이미지에는 `MOCK +REF` 가 그려진다.

### 6.1 `--dry-run`의 중요한 특성

dry-run은 **파일 존재 여부를 의도적으로 무시**하고 대상 전체를 계획으로 잡는다.
API를 호출하지 않으므로 실존 파일만 필터링하면 마크다운이 0줄이 되어
조립 로직을 검증할 수 없기 때문이다.

**결과적으로 dry-run 장수는 실제 렌더링 장수와 다를 수 있다.**
이미 15장이 있으면 dry-run은 "20장 계획"이라 하지만 실제로는 5장만 생성된다.
장수 예측용이 아니라 대상 집합·파일명 확인용으로 쓴다.

### 6.2 `--mock`의 함정 (중요)

`--mock`은 **실제 에셋 폴더**(`generated_assets/{prefix}/`)에 더미 파일을 쓴다.

`--prefix mika --mock`으로 검증한 뒤 같은 `--prefix mika`로 실제 렌더링을 돌리면,
재개 로직이 더미 파일을 완성품으로 보고 **전부 건너뛴다.** 최종 에셋이 더미
이미지가 된다.

검증 시에는 반드시 전용 접두어를 쓰고 끝나면 지운다.

```powershell
python sd_batch_generator.py --prefix mocktest --char_prompt "none" --mock
Remove-Item -Recurse -Force generated_assets\mocktest
```

더미 이미지에는 코드 번호·prefix·섹션명·라벨·`MOCK` 텍스트가 그려져 육안 식별은
가능하지만, 스킵 로직은 파일 존재만 본다.

`--dry-run`은 폴더를 만들지 않으므로 실제 약칭을 써도 안전하다.

### 6.3 탐색기 오픈은 비동기다

`open_in_explorer()`는 `os.startfile()`로 별도 프로세스를 띄우고 즉시 반환한다.
스크립트가 종료된 뒤에 탐색기가 열리므로, 그 사이에 대상 폴더를 삭제하면
Windows가 "위치를 사용할 수 없습니다" 경고창을 띄운다.

`os.startfile()` 자체는 성공했고 실패는 탐색기 프로세스 안에서 발생하므로
Python 쪽에서 잡을 수 없다. 기능 결함이 아니라 삭제와의 경쟁 조건이다.

자동 검증 스크립트를 작성할 때는 `--mock` 대신 `--dry-run`을 쓰거나
(탐색기를 열지 않음), 임시 폴더 삭제 전에 지연을 두어야 한다.

---

## 7. 프롬프트 조립 순서

### 포지티브

```
{profile.base_positive}, {--char_prompt}, {JSON 포즈 프롬프트}, {prefix}_{코드}
```

실제 예 (`--prefix mika --char_prompt "silver hair, blue eyes" --profile female`, 코드 00):

```
masterpiece, best quality, highly detailed, clean background, soft lighting,
character portrait, 1girl, solo, silver hair, blue eyes,
neutral expression, standing, front view, looking at viewer, calm, mika_00
```

마지막 `mika_00`은 트리거 태그다. LoRA 트리거 워드 용도로 설계된 잔재이며
실제 SD 프롬프트에 그대로 들어간다.

### 네거티브

```
{profile.base_negative}, {--custom_neg}
```

`--custom_neg`는 **추가**된다. 프로필 네거티브를 대체하지 않는다.

---

## 7A. 참조 이미지 축 (IP-Adapter)

프로필이 **태그 축**을 담당하듯, 참조 이미지는 **시각 특징 축**을 담당한다.
두 축은 독립이며 서로에게 영향을 주지 않는다.

### 7A.1 탐색 규칙

```
references/{prefix}.png → .jpg → .jpeg → .webp
```

우선순위 첫 번째를 쓰고, 여러 개가 공존하면 무시된 목록을 경고한다.
`--ref_image` 로 경로를 직접 주면 자동 탐색을 건너뛴다.

### 7A.2 부재는 정상 상태다

참조 이미지가 없으면 **경고만 출력하고 텍스트 프롬프트만으로 생성한다.**
에러가 아니다.

```
[WARN] 참조 이미지 없음 (references/mika.*) - 텍스트 프롬프트만 사용
```

00번을 먼저 생성해 참조로 쓰는 부트스트랩 워크플로우에서는 1단계에
참조가 없는 것이 정상이므로, 여기서 중단하면 안 된다.

단 `--ref_image` 로 **명시한** 경로가 없으면 `ConfigError` 로 중단한다.
명시적 지정 실패는 사용자 실수이므로 조용히 무시하지 않는다.

### 7A.3 페이로드 주입

엔드포인트는 기존 `/sdapi/v1/txt2img` 를 그대로 쓰고, 페이로드에
`alwayson_scripts` 를 추가한다.

```json
{
  "prompt": "...",
  "alwayson_scripts": {
    "controlnet": {
      "args": [{
        "enabled": true,
        "input_image": "<base64>",
        "module": "ip-adapter_clip_sdxl",
        "model": "ip-adapter_xl [4209e9f7]",
        "weight": 0.7,
        "resize_mode": "Crop and Resize",
        "control_mode": "Balanced",
        "pixel_perfect": true
      }]
    }
  }
}
```

**참조 이미지와 ControlNet 해석이 둘 다 성공했을 때만 주입한다.**
참조는 있는데 ControlNet 이 없으면(미설치 등) `alwayson_scripts` 키 자체를
넣지 않는다. 빈 딕셔너리를 넣으면 WebUI 가 "비활성" 이 아니라 "인자 부족" 으로
해석할 수 있다.

`inject_controlnet()` 은 원본 페이로드를 변경하지 않고 새 딕셔너리를 반환한다.
루프에서 페이로드를 재사용할 때 상태가 누적되는 것을 막는 계약이다.

### 7A.4 ControlNet 모델 자동 탐지

모델명이 `ip-adapter_xl [4209e9f7]` 처럼 **해시를 포함**하며 환경마다 다르다.
하드코딩하면 다른 PC 에서 반드시 깨진다.

`/controlnet/module_list` 와 `/controlnet/model_list` 를 조회해
`IP_ADAPTER_*_PATTERNS` 와 **부분 문자열 매칭**한다. `resolve_sampler()` 와
같은 패턴이다.

매칭에 실패하면 조회된 목록 **전체를 출력**한다. 그 출력만 보고 바로
`--cn_module` / `--cn_model` 을 지정할 수 있게 하려는 의도다.

조회는 배치당 1회이며, `--mock` / `--dry-run` / `--test` 에서는 수행하지 않는다.

### 7A.5 weight 조정

| 값 | 효과 |
|---|---|
| 0.0 | 참조 무시 (텍스트만) |
| 0.5~0.8 | 실무 범위 |
| 1.0 이상 | 참조 이미지의 **포즈까지 전이**되어 JSON 포즈 지시를 무시 |

기본값 `0.7` 은 근거 있는 출발점이며 정확한 값은 GPU 환경 튜닝으로 확정한다.
`REF_WEIGHT_DEFAULT` 상수에 있다.

### 7A.6 태그 역추출 (`--from_image`)

`/sdapi/v1/interrogate` 를 호출해 이미지에서 프롬프트를 역추출한다.
생성 파이프라인을 타지 않고 결과를 출력한 뒤 종료한다.

기본 모델은 `deepdanbooru` 다. `clip` 은 자연어 문장을 반환해 태그 기반
프롬프트로 쓰기 어렵다.

추출 결과에 성별·인원 태그(`1girl`, `solo` 등)가 있으면 경고하고 **제거한
버전을 함께 제시**한다. 이 태그들은 `_profiles` 축에서 이미 다루므로
`--char_prompt` 에 들어가면 프로필과 충돌한다. 경고만 하지 않고 필터링된
버전을 주는 이유는 사용자가 출력을 그대로 복사할 가능성이 높기 때문이다.

가중치 표기(`(1girl:1.2)`)도 정규화 후 비교하므로 감지된다.

### 7A.7 부트스트랩 워크플로우

외부에서 그림을 구하지 않고 자체 생성물로 일관성을 확보하는 방법이다.

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "..." --mode 0
Copy-Item generated_assets\mika\mika_00.webp references\mika.webp
python sd_batch_generator.py --prefix mika --char_prompt "..." --mode 1-19
```

00번이 "정면·중립 표정" 이라 참조용으로 적합하다. 표정이나 포즈가 강한
이미지를 참조로 쓰면 그 특성이 다른 번호에도 새어나온다.

---

## 8. 생성 파라미터 (스크립트 상수)

| 상수 | 값 | 비고 |
|---|---|---|
| `IMAGE_SIZE` | `(832, 1216)` | SDXL 계열 세로 초상화. SD1.5엔 과대 |
| `STEPS` | `28` | |
| `CFG_SCALE` | `7` | |
| `batch_size` / `n_iter` | `1` / `1` | 장당 1회 요청 |
| `WEBP_QUALITY` | `90` | |
| `WEBP_METHOD` | `6` | |
| `MOCK_SIZE` | `(208, 304)` | 실제의 1/4, 종횡비 동일 |
| `SAMPLER_CANDIDATES` | `("DPM++ 2M Karras", "DPM++ 2M", "Euler a")` | 앞에서부터 탐색 |
| `SAMPLERS_TIMEOUT` | `5`초 | |
| `TXT2IMG_TIMEOUT` | `300`초 | 장당 |
| `MAX_CODE` | `9999` | |
| `MIN_CODE_WIDTH` | `2` | 하위 호환 |
| API 엔드포인트 | `http://127.0.0.1:7860` | `/sdapi/v1/txt2img`, `/sdapi/v1/samplers` |

**스크립트는 체크포인트(모델)를 지정하지 않는다.** WebUI에 현재 선택된 모델이
그대로 쓰인다. 실행 전 WebUI UI에서 모델을 확인해야 한다.

샘플러는 `/sdapi/v1/samplers`를 조회해 후보 중 첫 번째 사용 가능한 것을 고른다.
최신 WebUI/Forge는 샘플러와 스케줄러가 분리되어 `DPM++ 2M Karras`가 없을 수 있고,
그러면 `DPM++ 2M`으로 폴백하며 스케줄러는 WebUI 기본값을 쓴다.
선택 결과는 `[SAMPLER]` 로그로 출력된다.

---

## 8A. 측정 기능

배치가 끝나면 요약에 두 줄이 추가된다.

```
[측정] 20장 / 총 412.3초 / 장당 평균 20.6초 (최속 19.8 ~ 최저 24.1)
[VRAM] 피크 7.21 / 8.00 GiB (90%)
```

### 8A.1 왜 필요한가

VRAM 설정(`--medvram-sdxl`, xFormers 등)을 비교할 때 판단 근거가 된다.

에파는 한 배치에 수십 장을 **순차** 생성하므로 장당 손실이 누적된다.
장당 5초 차이는 30장이면 2분 30초다. "OOM 없이 돌아간다" 만으로는 부족하고
속도까지 함께 봐야 한다.

`--medvram` 계열은 메모리를 절약하는 대신 속도를 떨어뜨린다.
`--lowvram` 은 [성능에 치명적](https://github.com/AUTOMATIC1111/stable-diffusion-webui/discussions/2532)이라
배치 작업에는 부적합하다.

### 8A.2 시간 측정

`time.perf_counter()` 로 생성 직전부터 저장 완료까지를 측정한다.

**실제로 생성한 것만 기록한다.** 건너뛴 파일은 제외되므로 재개 실행에서도
숫자가 왜곡되지 않는다. 집계는 `summarize_durations()` 순수 함수가 담당해
`--test` 에서 직접 검증한다.

### 8A.3 VRAM 조회

`/sdapi/v1/memory` 를 배치 종료 후 1회 조회한다.

응답 구조가 WebUI 버전과 Forge 여부에 따라 다르므로 여러 키를 순차 탐색한다.

1. 최상위 스칼라: `reserved_peak` → `active_peak`
2. 중첩 딕셔너리: `reserved.peak` → `active.peak` → `allocated.peak`

어느 것도 찾지 못하면 **조용히 생략한다.** 부가 정보이므로 이것 때문에
배치가 실패해서는 안 된다. 파싱은 `extract_vram_peak()` 순수 함수로 분리해
7가지 응답 형태를 `--test` 에서 검증한다.

`--dry-run` 과 `--mock` 에서는 조회하지 않는다. GPU 를 쓰지 않으므로 무의미하다.

### 8A.4 8GB 환경 권장 설정

[A1111 공식 위키의 8GB Nvidia 권장 조합](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Optimum-SDXL-Usage)이다.

```bat
set COMMANDLINE_ARGS=--api --medvram-sdxl --xformers
```

주의할 점 두 가지가 있다.

**`--xformers` 와 `--opt-sdp-attention` 을 같이 쓰지 않는다.** 둘 다 어텐션
최적화라 하나만 적용된다. 함께 적으면 어느 쪽이 쓰이는지 알 수 없어 비교가
불가능해진다.

**참조 이미지(IP-Adapter)는 VRAM 을 추가로 쓴다.** CLIP 비전 인코더가 함께
올라가므로, 참조 없이 되던 설정이 참조를 켜면 OOM 이 날 수 있다.

에파의 재개 기능이 안전망 역할을 한다. 25장에서 OOM 이 나도 같은 명령을
다시 실행하면 26장부터 이어간다. 설정을 완벽히 맞추지 않아도 작업은 진행된다.

---

## 9. 저장 규칙

```
{스크립트 위치}/generated_assets/{prefix}/{prefix}_{코드}.webp
```

- 코드 패딩 폭 = `max(2, 전체 최대 코드의 자릿수)`
  - 최대 19 → 폭 2 → `mika_07.webp`
  - 최대 105 → 폭 3 → `mika_007.webp`, `mika_105.webp`
- 폭은 **전체 DB 기준**으로 한 번 계산해 루프·스킵 판정·마크다운에 동일 적용
- 저장은 `.part` 임시 파일에 쓴 뒤 `os.replace()`로 원자적 교체
  (중단 시 반쪽 파일이 남아 재개 로직을 속이는 것을 방지)
- **기존 파일이 있으면 건너뛴다.** 중단 후 재실행하면 남은 것만 생성
- 강제 재생성은 해당 `.webp`를 삭제하고 재실행

---

## 10. 전체 워크플로우

### 10-A. 최초 1회 준비

**① 패키지 설치**

```powershell
pip install requests pillow
```

**② WebUI `--api` 활성화**

WebUI 설치 폴더의 `webui-user.bat`을 편집.

```bat
set COMMANDLINE_ARGS=--api
```

VRAM 최적화가 필요하면 추가 (8~12GB 기준):

```bat
set COMMANDLINE_ARGS=--api --xformers --medvram --opt-sdp-attention
```

6GB 이하는 `--medvram` 대신 `--lowvram`.

**③ WebUI Settings 조치 — 파일명 패턴 초기화 (필수)**

`Settings → Saving images/grids → Images filename pattern`을 **완전히 비운다.**

`[prompt_words:1]` 같은 값이 남아 있으면 콜론 등 특수문자 때문에
`[WinError 87]`이 발생하고 파일이 0KB로 저장된다.
변경 후 상단 `Apply settings`를 누른다.

**④ 메모리 누수 방지 (권장)**

`User Interface`에서 생성 완료 후 VRAM 비우기 옵션을 켜거나,
`Actions`의 `Clean temp dir`을 주기적으로 활용.

**⑤ Forge 사용 시 권장값** (RTX 4060 Ti 8GB 기준)

| 항목 | 값 |
|---|---|
| Diffusion with Low Bits | `Auto` |
| Swap Method | `Queue` |
| Swap Location | `CPU` |
| Clip skip | `2` |

설정 후 콘솔 창을 완전히 닫고 `webui-user.bat`으로 재실행.

### 10-B. 매 작업 흐름

```powershell
cd "C:\Users\USER\kiro"
```

**1단계 — JSON 편집**

`pose_database.json`에 항목 추가/수정. 첫 태그는 구별 서술어로.

**2단계 — 자체 진단 (WebUI 불필요)**

```powershell
python sd_batch_generator.py --test
```

23항목이 `[PASS]`면 통과. `[WARN]`은 데이터 품질 경고로 실행은 된다.
`[FAIL]`이 있으면 고쳐야 한다. 종료 코드로 자동화에 걸 수 있다.

**3단계 — 계획 확인 (WebUI 불필요)**

```powershell
python sd_batch_generator.py --prefix chk --char_prompt "none" --dry-run --mode all
```

`--char_prompt`는 파서가 필수로 요구하는 자리표시자다. dry-run에서는 사용되지 않는다.

**4단계 — WebUI 실행 및 모델 선택**

`webui-user.bat` 실행 → 콘솔에 `Running on local URL: http://127.0.0.1:7860`
확인 → 브라우저에서 체크포인트 선택. **콘솔 창은 끄지 않는다.**

**5단계 — 실제 생성**

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes, school uniform" --profile female
```

`--mode all`은 기본값이라 생략 가능. 터미널을 점유하며 순차 진행한다.
20장이면 GPU에 따라 수 분 소요. 창을 닫지 않는다.

**6단계 — 결과**

- 요약 리포트 (성공/건너뜀/실패)
- 탐색기 자동 오픈 (Windows)
- 젠잇 마크다운 출력 — 실존 파일 수와 정확히 일치하는 줄 수

### 10-C. 상황별 명령

**남성 캐릭터**

```powershell
python sd_batch_generator.py --prefix ryu --char_prompt "short black hair, sharp eyes" --profile male
```

**중성적 외형 남성 캐릭터**

```powershell
python sd_batch_generator.py --prefix sei --char_prompt "silver bob cut, violet eyes" --profile male_otokonoko
```

**감정 세트만**

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "silver hair" --mode emotions
```

**특정 번호만 재생성**

```powershell
Remove-Item generated_assets\mika\mika_03.webp, generated_assets\mika\mika_07.webp
python sd_batch_generator.py --prefix mika --char_prompt "silver hair" --mode 3,7
```

**전체 재생성**

```powershell
Remove-Item -Recurse -Force generated_assets\mika
python sd_batch_generator.py --prefix mika --char_prompt "silver hair"
```

**캐릭터별 네거티브 추가**

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "silver hair" --custom_neg "glasses, hat"
```

---

## 11. 출력 형식

### 실행 로그

```
[PROFILE] 'male' 적용
[작업 시작] 캐릭터: ryu | 프로필: male | 모드: all (20장) | 폭: 2
[저장] C:\...\kiro\generated_assets\ryu
[POS]  masterpiece, best quality, ..., 1boy, solo, masculine
[NEG]  worst quality, ..., 1girl, female, breasts, feminine

[SAMPLER] 'DPM++ 2M Karras' 감지됨
  [00] 생성 중... 완료 -> ryu_00.webp
  [01] 이미 존재 (건너뜀) -> ryu_01.webp
  ...

[작업 완료] 성공 19 / 건너뜀 1 / 실패 0
           폴더: C:\...\kiro\generated_assets\ryu
```

### 젠잇 마크다운 블록

```
================================================================
  젠잇(Genit) 복사용 에셋 블록 | ryu   (총 20개)
================================================================

### ryu 이미지 호출 코드
![image]({{url}}ryu/ryu_00.webp)
![image]({{url}}ryu/ryu_01.webp)
...

### ryu 파일 목록
- `{{url}}ryu/ryu_00.webp` (neutral expression)
- `{{url}}ryu/ryu_01.webp` (gentle smile)
...

### ryu 상태 매핑 가이드
[emotions]
  neutral expression         -> ryu_00.webp
  gentle smile               -> ryu_01.webp
...

[poses]
  relaxed stance             -> ryu_10.webp
...

### ryu 상태창 템플릿
[@id=상태창|name=ryu|title=직책입력|status=현재상태|desc=대사한줄]
================================================================
```

`{{url}}`은 젠잇이 치환하는 리터럴이므로 **그대로 두고 복사**한다.

---

## 12. 종료 코드

| 코드 | 의미 | 발생 조건 |
|---|---|---|
| 0 | 정상 | 생성 완료, 또는 `--test` 전항목 통과 |
| 1 | 설정·입력 오류 또는 중단 | `ConfigError`, WebUI 연결 끊김, `--test` FAIL 발생 |
| 2 | 필수 인자 누락 | argparse 표준 (`--prefix`/`--char_prompt` 없음) |
| 130 | 사용자 중단 | Ctrl+C |

### `ConfigError`가 발생하는 경우 (모두 종료 코드 1)

- `pose_database.json` 부재 / JSON 문법 오류 / 최상위가 딕셔너리 아님
- 유효 프롬프트 항목 0개
- `--prefix`가 화이트리스트 위반
- 미등록 `--mode` 섹션명
- 코드 표현식 구문 오류 또는 범위 초과
- 미등록 `--profile`
- `_profiles` 없는데 `--profile` 지정
- 대상 코드 0개

`stderr`에 `[ERROR]` 메시지와 힌트가 함께 출력된다.

### 경고로 처리하고 계속하는 경우 (종료 코드 0)

- 비정수 키 / 빈 프롬프트 / 중복 코드 / 비-딕셔너리 섹션
- 프로필 정의 불량 (해당 프로필만 제외)
- 태그 충돌
- DB에 없는 코드 요청
- 개별 이미지 생성 실패 (다음 코드로 계속)

50개 항목 중 오타 하나로 전체 배치가 막히면 운영에 불편하므로 경고로 둔다.

---

## 13. 태그 충돌 감지

포지티브와 네거티브에 **같은 태그**가 있으면 경고한다.

```
[WARN] 태그 충돌: ['breasts'] 가 포지티브와 네거티브에 동시 존재
```

### 검사 시점 2곳

| 시점 | 검사 대상 |
|---|---|
| `--test` (T19) | 각 프로필의 `base_positive` vs `base_negative` |
| 실행 시작 | `base_positive + char_prompt` vs `base_negative + custom_neg` |

`--test`만으로는 `--char_prompt`가 프로필 네거티브와 충돌하는 경우를 못 잡으므로
런타임 검사를 함께 넣었다.

### 정규화 규칙

```
"(huge:1.3)"    → "huge"
"((tag))"       → "tag"
"[soft]"        → "soft"
" Bad   Hands " → "bad hands"
```

괄호를 먼저 제거하고 가중치 접미사를 나중에 지운다. 순서가 반대면 괄호가 남는다.

### 한계

**같은 문자열만 잡는다.** `1girl`과 `1boy`는 서로 다른 문자열이므로 자동
검출되지 않는다. 의미적 상충까지 잡으려면 상호배타 쌍 목록이 필요하고,
그건 완전하게 만들 수 없어 도입하지 않았다.

경고에 그치고 중단하지 않는다. 의도적으로 같은 태그를 양쪽에 두는 프롬프트
기법이 존재하기 때문이다.

---

## 14. `--test` 검사 항목 (45개)

### 데이터 검사

| ID | 검사 | 등급 |
|---|---|---|
| T1 | JSON 파일 존재 | FAIL |
| T2 | JSON 문법 (line/col 포함) | FAIL |
| T3 | 최상위 섹션 딕셔너리 | FAIL |
| T3b | 비-딕셔너리 섹션 | WARN |
| T4 | 비정수 키 | WARN |
| T5 | 빈 프롬프트 | WARN |
| T6 | 중복 코드 | WARN |
| T7 | 유효 엔트리 1개 이상 | FAIL |

### 로직 검사

| ID | 검사 |
|---|---|
| T8 / T8b | 정수 정렬 (사전순과 다름을 실증) / 실제 DB 정렬 |
| T9 | `code_width()` 5케이스 |
| T10 / T10b | `parse_codes_expr()` 6케이스 / `looks_like_code_expr()` 판별 |
| T11 | `asset_filename()` 조립 (폭 2·3) |
| T12 | 마크다운 라인 수 == 대상 코드 수 |
| T13 | `{{url}}` 리터럴 포함 |
| T14 / T14b | 위험 `prefix` 7종 차단 / 정상 `prefix` 허용 |
| T15 | 잘못된 코드 표현식 4종 거부 |
| T16 | `normalize_tag()` 7케이스 |
| T17 | `find_tag_conflicts()` 4케이스 |

### 프로필 검사

| ID | 검사 | 등급 |
|---|---|---|
| T18 | 프로필 로드 개수 | PASS/WARN |
| T19 | 프로필별 태그 충돌 | WARN |
| T20 | 기본 프로필 `female` 존재 | WARN |

### 참조 이미지 검사

| ID | 검사 |
|---|---|
| T21 / T21b | 확장자 우선순위 / `.png` 채택 |
| T22 / T22b | 참조 부재 시 `None` / `references/` 폴더 자체 부재 |
| T23 | base64 왕복 (디코딩 후 Pillow 로 크기 확인) |
| T24 / T24b | 유닛 필수 키 8개 / base64 이미지 포함 |
| T25 | 참조 없을 때 `alwayson_scripts` 미주입 |
| T26 / T26b / T26c | 주입 위치 / **원본 불변성** / 기존 키 보존 |
| T27 | `--ref_weight` 경계값 (거부 4종 / 허용 5종) |
| T28 | interrogate 페이로드 구조 |
| T29 / T29b | 성별 태그 필터 / `filtered` 프로퍼티 |
| T30 / T30b | 해시 포함 모델명 부분 매칭 / 실패 시 `None` |
| T31 / T31b / T31c | 시간 집계 / 빈 측정값 / `BatchResult.timing` |
| T32 / T32b | VRAM 응답 파싱 7케이스 / GiB 환산 |

현재 총 검사 수: **45항목** (데이터 8 + 로직 12 + 프로필 3 + 참조·측정 22).

검사는 **실제 구현 함수를 직접 호출**한다. 로직을 복제하지 않으므로
구현이 바뀌면 검사도 함께 따라간다.

참조 이미지 검사는 Pillow 로 임시 이미지를 만들어 쓰고 `contextmanager` 로
정리한다. 저장소에 테스트용 바이너리를 커밋하지 않기 위함이다.

### 검증 범위의 한계 (중요)

`--test` 는 페이로드 **구조**만 검사한다. "WebUI 가 이 페이로드를 수락하는가"
는 검증 범위 밖이다. 아래는 GPU 환경이 필요하다.

- 실제 ControlNet 모델명 매칭 결과
- WebUI 의 페이로드 스키마 수락 여부
- weight 별 이미지 차이
- DeepBooru 태그 추출 품질
- 캐릭터 일관성 개선 정도

---

## 15. 보안 제약

| 제약 | 이유 |
|---|---|
| `--prefix`는 `^[A-Za-z0-9_-]{1,64}$` | 경로 이탈(`../`)과 인용부호 주입을 입구에서 차단 |
| 탐색기는 `os.startfile()` 사용 | `os.system()`은 셸을 거쳐 명령 주입 가능 |
| 코드 범위 상한 9999 | `0-999999999` 입력 시 메모리 폭주 방지 |

한글 약칭(`--prefix 미카`)은 거부된다. 사용자가 한글 약칭을 주면 **임의로
변환하지 말고** 영문 약칭을 다시 요청한다. 파일명과 젠잇 호출 코드에 그대로
들어가는 값이므로 추측하면 안 된다.

---

## 16. 문제 해결

| 증상 | 원인 / 대응 |
|---|---|
| `WebUI 연결 불가` | `--api` 누락, WebUI 콘솔 종료 |
| 파일이 0KB로 저장 / `[WinError 87]` | WebUI `Images filename pattern`을 비우고 Apply |
| 종료 코드 1 + `prefix` 메시지 | 약칭에 한글·특수문자·공백 |
| 종료 코드 2 | `--prefix` 또는 `--char_prompt` 누락 |
| 전부 `건너뜀` | 이미 파일 존재. 삭제 후 재실행 |
| 남캐인데 여성으로 나옴 | `--profile male` 누락. 콘솔 `[PROFILE]` 확인 |
| 인물 중복 / 뒤틀림 | SD1.5에 832×1216은 과대. `IMAGE_SIZE` 하향 |
| 화풍이 예상과 다름 | WebUI 체크포인트 확인 (스크립트는 모델 미지정) |
| 라벨이 전부 동일 | JSON 첫 태그 중복. 구별 서술어로 교체 |
| `태그 충돌` 경고 | 프로필 선택 오류 또는 외형 태그 부적합 |
| 10장 이상 시 메모리 부족 | VRAM 비우기 옵션, `--medvram`/`--lowvram` |

---

## 17. Kiro 자동 실행 규칙

`.kiro/steering/sd_char_gen.md`가 `inclusion: auto`로 등록되어 있어,
사용자가 아래 형식으로 입력하면 자동 활성화된다.

```
캐릭터 생성: [약칭] / [외형 프롬프트] / [네거티브]
```

슬래시 기준 분리 후 앞뒤 공백 제거. 네거티브는 생략 가능.

에이전트가 지켜야 할 규칙:

1. 작업 디렉터리는 `cwd` 파라미터로 지정한다. `cd`를 쓰지 않는다.
2. 약칭이 영문 규격에 맞지 않으면 임의 변환하지 않고 재요청한다.
3. 트리거 입력에 남성·소년 등의 단서가 있으면 적절한 `--profile`을 붙인다.
4. `태그 충돌` 경고가 뜨면 강행하지 않고 사용자에게 알린다.
5. 실행 후 터미널 출력과 젠잇 마크다운 블록을 그대로 전달한다.

---

## 18. 확장 시 참고

### 안전하게 바꿔도 되는 것

- `pose_database.json`의 모든 내용 (섹션 추가, 코드 추가, 프로필 추가)
- `IMAGE_SIZE`, `STEPS`, `CFG_SCALE` 등 생성 파라미터 상수
- `DEFAULT_PROFILE` 기본값

### 바꿀 때 주의할 것

| 대상 | 주의 |
|---|---|
| `save_as_webp()` 변환 파라미터 | `quality=90`, `method=6`, RGBA→RGB는 검증된 값 |
| `SAMPLER_CANDIDATES` 순서 | 폴백 순서가 화풍에 영향 |
| `asset_filename()` | 파일명 조립 단일 진입점. 우회하면 스킵 판정과 어긋남 |
| `MIN_CODE_WIDTH = 2` | 기존 생성 파일과의 하위 호환 |
| `run_batch()`의 `dry_run` 검사 순서 | `path.exists()`보다 먼저여야 `planned`가 완전해짐 |
| `URL_PLACEHOLDER` | f-string 이스케이프 실수 방지용 상수. 리터럴로 직접 쓰지 말 것 |

### 알려진 개선 여지

- 프로필 3종이 품질 태그를 중복 보유한다. 프로필이 늘어나면
  `"extends"` 상속 문법을 검토할 가치가 있다.
- 의미적 태그 충돌(`1girl` vs `1boy`)은 자동 검출되지 않는다.
  상호배타 쌍 목록을 도입할 수 있으나 완전성을 보장할 수 없다.
- 실제 API 통신 경로(`ConnectionError` 처리, 세션 재사용 효과)는
  WebUI 실행 환경이 필요해 자동 검증 범위에 포함되지 않는다.

---

## 19. 상세 문서 위치

| 알고 싶은 것 | 문서 |
|---|---|
| 요구사항, 수용 기준 (R1~R8) | `.kiro/specs/dynamic-pose-pipeline/requirements.md` |
| 아키텍처, 설계 결정 근거 | `.kiro/specs/dynamic-pose-pipeline/design.md` |
| 정렬 알고리즘, 마크다운 조립 구조 | 같은 문서 2·4장 |
| 보안 강화 이력 (주입·경로 이탈 등) | 같은 문서 9장 |
| 프로필 축 도입 근거 | 같은 문서 10장 |
| 구현 단계 이력 | `.kiro/specs/dynamic-pose-pipeline/tasks.md` |
| WebUI 설치·설정 | `사용법.txt` |
| Kiro 자동 실행 규칙 | `.kiro/steering/sd_char_gen.md` |

# SD 캐릭터 에셋 배치 생성 파이프라인 — 프로젝트 가이드

> AI 에이전트 및 개발자용 단일 참조 문서.
> 코드를 읽지 않고도 정확히 조작할 수 있도록 실제 구현값을 기준으로 작성.
>
> 설치·실행 절차 → `사용법.txt`
> 기능 명세 압축 참조 → `에파_기능명세.md`

---

## 0. 한 줄 요약

로컬 SD WebUI API를 호출해 캐릭터 챗봇용 이미지 에셋을 일괄 생성하고,
젠잇(Genit) 플랫폼에 붙여넣을 마크다운 호출 코드를 자동 조립하는 CLI 도구.

**핵심 설계 원칙: 프롬프트 데이터는 JSON에, 로직은 Python에.**

---

## 1. 파일 구성

작업 루트: `C:\Users\rbsgh\kiro`

```
kiro/
├── sd_batch_generator.py        메인 실행 스크립트
├── pose_database.json           프롬프트 DB (공용 — 모든 로스터 공유)
├── PROJECT_GUIDE.md             이 문서 (개발자/AI 에이전트용)
├── 에파_기능명세.md               기능 명세 압축 (외부 AI 개선용)
├── 사용법.txt                    설치·사용 가이드 (사람용)
├── .gitignore / .gitattributes
│
└── projects/                    ★ 로스터별 프로젝트 루트
    ├── dark_generals/           흑막사천왕 (별칭: dar)
    │   ├── characters/          캐릭터 JSON
    │   ├── references/          IP-Adapter 참조 이미지
    │   └── assets/              생성 결과 (git 제외: projects/*/assets/)
    │       └── {prefix}/{prefix}_{NNN}.webp
    ├── oto/                     오토코노코 (별칭: oto)
    ├── school/                  학원물 (별칭: school)
    └── fth/                     판타지 하렘 (별칭: fth)
```

### 로스터 (Roster) 시스템

| 별칭 | 폴더 | 설명 |
|---|---|---|
| `dar` | `projects/dark_generals/` | 흑막사천왕 |
| `oto` | `projects/oto/` | 오토코노코 |
| `school` | `projects/school/` | 학원물 |
| `fth` | `projects/fth/` | 판타지 하렘 |

OCP 원칙: `projects/xyz/` 폴더를 만들면 `--roster xyz`가 코드 수정 없이 인식됨.
`pose_database.json`과 `references/`는 루트에서 모든 로스터가 공유.

### 파일별 역할

| 파일 | 역할 | 수정 빈도 |
|---|---|---|
| `sd_batch_generator.py` | 전체 로직. API 호출·WebP 변환·마크다운 조립·자체 검증 | 낮음 |
| `pose_database.json` | 프로필·포즈·표정 프롬프트. **일상 편집 대상** | 높음 |
| `projects/{roster}/characters/*.json` | 캐릭터별 프리셋 | 중간 |
| `.kiro/steering/sd_char_gen.md` | Kiro 자동 실행 트리거 규칙 | 낮음 |

---

## 2. 의존성 및 실행 환경

| 항목 | 값 |
|---|---|
| Python | 3.10+ (`slots=True` dataclass, 검증 환경 3.12) |
| 필수 패키지 | `requests`, `Pillow` |
| OS | Windows 전제 (탐색기 자동 오픈). 다른 OS에서도 생성은 동작 |
| 외부 서비스 | SD WebUI (A1111 또는 Forge), `--api` 옵션 필수 |

---

## 3. 데이터 흐름

```
pose_database.json
        │  read_pose_json() → parse_pose_db()
        ▼
   PoseDatabase(entries, sections, profiles, warnings)
        │
        ├─ resolve_profile()   → Profile(base_positive, base_negative)
        ├─ resolve_targets()   → 대상 코드 리스트
        └─ code_width()        → 파일명 패딩 폭
        │
        ▼
   execute(args, roster)
        │  Lightning 모드 활성화 시:
        │    steps/cfg/sampler/scheduler 오버라이드
        │    LoRA 태그 프롬프트 자동 주입
        │    FreeU 충돌 시 자동 비활성화
        ▼
   run_batch()
        │  dry_run  → planned 기록만
        │  mock     → make_dummy_png() ─┐
        │  기본     → generate_image() ─┴→ save_as_webp() → .webp
        │
        │  Graceful Fallback:
        │    422/500 HTTPError → 순정 페이로드 자동 재시도
        ▼
   BatchResult(success, skipped, failed, planned, durations)
        │
        ├─ print_summary()
        ├─ open_in_explorer()       (Windows, dry-run 제외)
        └─ build_genit_block()      → 젠잇 마크다운 출력
```

`--test`는 이 파이프라인을 타지 않고 `run_self_test()`로 별도 분기.

---

## 4. `pose_database.json` 스펙

### 4.1 전체 구조

```json
{
  "_schema":          { "...": "메타/주석. 파싱 제외" },
  "_profiles":        { "female": { "base_positive": "...", "base_negative": "..." } },
  "emotions":         { "00": "프롬프트 태그", "01": "..." },
  "poses":            { "21": "...", "30": "..." },
  "h_scenes":         { "40": "...", "75": "..." },
  "scenes_otokonoko": { "140": "...", "170": "..." }
}
```

- `_`로 시작하는 최상위 키 → 파싱 제외 (단, `_profiles`는 프로필 파서가 별도 읽음)
- 그 외 최상위 키 → 포즈 섹션. 섹션명이 곧 `--mode` 값

### 4.2 포즈 섹션 규칙

| 규칙 | 내용 |
|---|---|
| 코드 키 | 문자열이지만 정수로 해석·정렬. `"00"`, `"7"`, `"105"` 모두 유효 |
| 범위 | 0 ~ 9999 |
| 결번 허용 | `0, 1, 5, 42`만 있으면 그 4개만 순회 |
| **첫 태그 = 라벨** | 쉼표 전까지가 콘솔 출력·상태 매핑 가이드의 라벨 |
| 성별 태그 금지 | `1girl`/`1boy`는 포즈에 넣지 않음. 성별은 프로필 축 |
| 섹션명 | 숫자만으로 짓지 말 것 (코드 표현식으로 오인됨) |

**첫 태그가 라벨이라는 점이 실무에서 가장 중요하다.**

```json
"00": "neutral face, standing, front view"   ← 라벨: neutral face  (올바름)
"00": "standing, front view, neutral face"   ← 라벨: standing      (구분 불가)
```

### 4.3 `_profiles` 스펙

프로필 값은 스크립트 기본값을 **완전히 대체**한다.
품질 태그(`masterpiece, best quality, ...`)를 각 프로필에 포함해야 한다.

현재 정의된 프로필 3종:

| 프로필 | 포지티브 성별 축 | 네거티브 성별 축 |
|---|---|---|
| `female` | `1girl, solo` | `1boy, male, masculine, beard, mustache, facial hair, muscular` |
| `male` | `1boy, solo, masculine` | `1girl, female, breasts, feminine` |
| `male_otokonoko` | `1boy, androgynous, feminine face, slender build, delicate features` | `muscular, manly, beard, mustache, facial hair` |

`male_otokonoko`는 포지티브에 `feminine face`가 있어 네거티브에 `feminine`을
**의도적으로 넣지 않았다.** 넣으면 의미가 상충한다.

### 4.4 섹션 추가 예시

```json
"swimwear": {
  "80": "bikini outfit, standing, summer mood",
  "81": "school swimsuit, standing, shy expression"
}
```

저장 후 `--mode swimwear`로 즉시 사용 가능. 스크립트 수정 불필요.

---

## 5. 캐릭터 JSON 스펙

### positive 방식 (권장)

```json
{
  "prefix": "rei",
  "default_mode": "female",
  "positive": "masterpiece, best quality, ..., 1girl, solo, dark purple hair, crimson eyes, ...",
  "negative": "1boy, male, child, loli, uncensored, no censor, thin censorship",
  "ref_weight": 0.7,
  "seed": 1234567890
}
```

`positive`가 있으면 프로필의 `base_positive`와 `char_prompt`를 **완전히 무시**한다.
표정·포즈 DB 태그는 항상 뒤에 추가된다.

### char_prompt 방식 (레거시)

```json
{
  "prefix": "bel",
  "profile": "male_otokonoko",
  "default_mode": "otokonoko",
  "char_prompt": "platinum blonde bob, golden eyes, military miniskirt",
  "custom_neg": "1girl, female anatomy",
  "ref_weight": 0.7
}
```

### 주요 필드

| 필드 | 설명 |
|---|---|
| `default_mode` | `female` → h_scenes 포함, `otokonoko` → scenes_otokonoko 포함 |
| `ref_weight` | IP-Adapter 강도. 개별 설정이 `REF_WEIGHT_DEFAULT(0.7)` 오버라이드 |
| `seed` | 지정 시 해당 시드로 고정. `None`이면 WebUI 랜덤(-1) |
| `width`/`height` | 동적 해상도. 씬별 JSON 포즈에도 지정 가능 |

**우선순위**: CLI 명시값 > 캐릭터 JSON > 기본값

---

## 6. 프롬프트 조립 순서

### 포지티브 (char_prompt 방식)

```
{profile.base_positive}, {char_prompt}, {포즈 DB 프롬프트}, {prefix}_{코드}
```

### 포지티브 (positive 방식)

```
{positive}, {포즈 DB 프롬프트}, {prefix}_{코드}
```

Lightning 활성화 시: `<lora:{name}:1.0>` 가 맨 앞에 추가됨

### 네거티브

```
{profile.base_negative}, {custom_neg}
```

마지막 `{prefix}_{코드}` 트리거 태그는 LoRA 트리거 워드 용도 잔재이며 실제 프롬프트에 그대로 들어간다.

---

## 7. 참조 이미지 축 (IP-Adapter)

### 탐색 규칙

```
projects/{roster}/references/{prefix}.png → .jpg → .jpeg → .webp
```

`--ref_image`로 직접 지정 시 자동 탐색 건너뜀.

### 부재는 정상 상태

참조 이미지가 없으면 경고 출력 후 텍스트 프롬프트만으로 생성 계속.
`--ref_image`로 **명시한** 경로가 없으면 `ConfigError`로 중단 (의도적 지정 실패).

### ControlNet 자동 탐지

`/controlnet/module_list`, `/controlnet/model_list` 조회 후 `IP_ADAPTER_*_PATTERNS`와 부분 문자열 매칭.
실패 시 사용 가능 목록 전체 출력 → `--cn_module`/`--cn_model` 수동 지정 안내.

### weight 조정 가이드

| 값 | 효과 |
|---|---|
| 0.5~0.8 | 실무 범위. 기본값 0.7 |
| 1.0 이상 | 참조 이미지의 포즈까지 전이. JSON 포즈 지시 무시됨 |

### 페이로드 주입 구조

```json
"alwayson_scripts": {
  "controlnet": {
    "args": [{
      "enabled": true,
      "input_image": "<base64>",
      "module": "ip-adapter_clip_sdxl",
      "model": "ip-adapter_xl [hash]",
      "weight": 0.7,
      "resize_mode": "Crop and Resize",
      "control_mode": "Balanced",
      "pixel_perfect": true
    }]
  }
}
```

참조는 있는데 ControlNet이 없으면 `alwayson_scripts` 키 자체를 넣지 않는다.
`inject_alwayson_scripts()`는 원본 페이로드를 변경하지 않고 새 딕셔너리를 반환 (불변성 계약).

---

## 8. Forge 고급 기능

### FreeU

```python
payload["alwayson_scripts"]["FreeU Integrated"] = {
    "args": [True, b1, b2, s1, s2]
}
```

`--enable-freeu` 활성화 시에만 주입. 기본값: b1=1.1, b2=1.2, s1=0.9, s2=0.2

### ADetailer

```python
payload["alwayson_scripts"]["ADetailer"] = {
    "args": [True, {"ad_model": "face_yolov8n.pt", "ad_confidence": 0.3, "ad_denoising_strength": 0.4}]
}
```

### Graceful Fallback

422/500 HTTPError 발생 시:
1. 경고 출력
2. FreeU/ADetailer 비활성화한 순정 페이로드로 자동 재시도
3. ControlNet 유닛은 유지
4. 404는 WebUI 미실행이므로 즉시 실패

### Lightning LoRA

`--enable-lightning` 활성화 시 `run_batch()` 내부에서:
- `actual_sampler = "DPM++ SDE"`
- `actual_steps = lightning_steps`
- `actual_cfg = lightning_cfg`
- `actual_scheduler = "Karras"`
- `full_prompt = f"<lora:{name}:1.0>, {full_prompt}"`
- `enable_freeu`가 True면 False로 강제 후 경고 출력

---

## 9. 생성 파라미터 상수

| 상수 | 현재값 | 비고 |
|---|---|---|
| `IMAGE_SIZE` | `(832, 1216)` | SDXL 세로 초상화. SD1.5엔 `(512, 768)` |
| `STEPS` | `24` | 일반 모드 |
| `CFG_SCALE` | `6.0` | |
| `WEBP_QUALITY` | `90` | |
| `WEBP_METHOD` | `6` | |
| `LORA_STRING` | `""` | 비어있으면 미적용 |
| `SAMPLER_CANDIDATES` | `DPM++ 2M SDE Karras`, `DPM++ 2M Karras`, `DPM++ 2M`, `Euler a` | 앞부터 탐색 |
| `REF_WEIGHT_DEFAULT` | `0.7` | |
| `TXT2IMG_TIMEOUT` | `300`초 | |
| `MAX_CODE` | `9999` | |
| API | `http://127.0.0.1:7860` | |

**스크립트는 체크포인트를 지정하지 않는다.** WebUI 현재 선택 모델이 그대로 쓰임.

---

## 10. 저장 규칙

```
projects/{roster}/assets/{prefix}/{prefix}_{코드}.webp
```

- 코드 패딩 폭 = `max(2, 전체 최대 코드의 자릿수)`
  - 최대 75 → 폭 2 → `rei_07.webp` ← **주의: 현재 DB 최대가 170이므로 폭 3**
  - 최대 170 → 폭 3 → `rei_007.webp`, `rei_170.webp`
- 저장은 `.part` 임시 파일에 쓴 뒤 `os.replace()`로 원자적 교체
- **기존 파일이 있으면 건너뛴다.** 강제 재생성은 해당 `.webp` 삭제 후 재실행

---

## 11. 4가지 실행 모드

| 모드 | API 호출 | 파일 쓰기 | 탐색기 | 마크다운 대상 |
|---|---|---|---|---|
| 기본 | O | O | O | 실존 파일만 |
| `--mock` | X | O (더미) | O | 실존 파일만 |
| `--dry-run` | X | X | X | 대상 전체 (파일 존재 무시) |
| `--test` | X | X | X | 없음 |

`--mock` 주의: 실제 에셋 폴더에 더미 파일을 쓴다.
같은 prefix로 실제 생성을 돌리면 더미를 완성품으로 보고 전부 건너뜀.
검증 후 반드시 `Remove-Item -Recurse -Force "projects\{roster}\assets\{prefix}"` 로 삭제.

`--dry-run`은 파일 I/O를 하지 않으므로 실제 약칭을 써도 안전하다.

---

## 12. 측정 기능

```
[측정] 20장 / 총 327.4초 / 장당 평균 16.4초 (최속 15.8 ~ 최저 17.2)
[VRAM] 피크 5.98 / 8.00 GiB (75%)
```

- `time.perf_counter()`로 생성 직전~저장 완료 측정. 건너뛴 파일 제외
- VRAM: `/sdapi/v1/memory` 배치 종료 후 1회 조회
  - 응답 구조가 버전마다 달라 여러 키 순차 탐색. 실패 시 조용히 생략
- `--dry-run`, `--mock`에서는 조회 안 함 (GPU 미사용)

---

## 13. `--test` 검사 항목 (45개)

| 그룹 | 항목 |
|---|---|
| 데이터 (T1~T7) | JSON 파일/문법/섹션 타입/비정수 키/빈 프롬프트/중복 코드/유효 엔트리 |
| 로직 (T8~T17) | 정수 정렬/code_width/코드 파싱/파일명 조립/마크다운 줄 수/`{{url}}`/prefix 화이트리스트/태그 정규화/충돌 감지 |
| 프로필 (T18~T20) | 프로필 로드 수/태그 충돌/기본 프로필 `female` 존재 |
| 참조·측정 (T21~T32) | 참조 탐색/base64 왕복/페이로드 조립/원본 불변성/ref_weight/interrogate/성별 태그 필터/모델명 매칭/시간 집계/VRAM 파싱 |

검사는 **실제 구현 함수를 직접 호출**한다. 구현이 바뀌면 검사도 함께 따라감.

---

## 14. 태그 충돌 감지

포지티브·네거티브에 같은 태그가 있으면 경고 (중단하지 않음).

```
[WARN] 태그 충돌: ['breasts'] 가 포지티브와 네거티브에 동시 존재
```

정규화 규칙:
```
"(huge:1.3)"  → "huge"
"((tag))"     → "tag"
" Bad  Hands" → "bad hands"
```

한계: `1girl` vs `1boy` 같은 의미적 상충은 자동 검출 안 됨.

---

## 15. 보안 제약

| 제약 | 이유 |
|---|---|
| `--prefix` → `^[A-Za-z0-9_-]{1,64}$` | 경로 이탈(`../`)·주입 방지 |
| 탐색기 → `os.startfile()` | `os.system()`은 셸 명령 주입 가능 |
| 코드 범위 상한 9999 | `0-999999999` 입력 시 메모리 폭주 방지 |

한글 약칭(`--prefix 미카`)은 거부된다.
**임의로 변환하지 말고** 영문 약칭을 다시 요청한다.

---

## 16. 종료 코드

| 코드 | 의미 | 조건 |
|---|---|---|
| 0 | 정상 | 생성 완료, `--test` 전항목 통과 |
| 1 | 오류 또는 중단 | ConfigError, WebUI 연결 끊김, `--test` FAIL |
| 2 | 필수 인자 누락 | argparse (`--prefix`/`--char_prompt` 없음) |
| 130 | 사용자 중단 | Ctrl+C |

---

## 17. 확장 가이드

### 안전하게 바꿔도 되는 것

- `pose_database.json` 전체 내용 (섹션 추가, 코드 추가, 프로필 추가)
- `IMAGE_SIZE`, `STEPS`, `CFG_SCALE`, `LORA_STRING` 등 생성 파라미터 상수
- `SAMPLER_CANDIDATES` 순서 (화풍에 영향)
- `DEFAULT_PROFILE` 기본값

### 바꿀 때 주의할 것

| 대상 | 주의 |
|---|---|
| `save_as_webp()` 변환 파라미터 | `quality=90`, `method=6`, RGBA→RGB 변환은 검증된 값 |
| `asset_filename()` | 파일명 조립 단일 진입점. 우회하면 스킵 판정과 어긋남 |
| `MIN_CODE_WIDTH = 2` | 기존 생성 파일과의 하위 호환 |
| `inject_alwayson_scripts()` | 원본 불변성 계약 유지 필요 (T26b 검사) |
| `URL_PLACEHOLDER` | f-string 이스케이프 실수 방지용 상수. 리터럴로 직접 쓰지 말 것 |

### 새 로스터 추가

```
projects/
  newroster/
    characters/   JSON 파일들
    references/   참조 이미지들
    assets/       (자동 생성됨)
```

→ 즉시 `--roster newroster`로 사용 가능. 코드 수정 없음.

### Depth ControlNet 확장 예시

`run_batch()` 내부 Multi-ControlNet 조립부:
```python
controlnet_units: list[dict[str, Any]] = []
if controlnet_unit is not None:
    controlnet_units.append(controlnet_unit)
# 확장 시 아래처럼 append만 추가
# if depth_unit is not None:
#     controlnet_units.append(depth_unit)
```

### 알려진 개선 여지

- 프로필 3종이 품질 태그를 중복 보유. 프로필이 늘면 `"extends"` 상속 문법 검토 가능
- 의미적 태그 충돌(`1girl` vs `1boy`) 자동 검출 안 됨
- 실제 API 통신 경로(ConnectionError 처리, 세션 재사용)는 WebUI 환경 없이 자동 검증 불가

---

## 18. 문제 해결

| 증상 | 원인 / 대응 |
|---|---|
| `WebUI 연결 불가` | `--api` 누락, WebUI 콘솔 종료 |
| 파일이 0KB / `[WinError 87]` | WebUI `Images filename pattern` 비우고 Apply |
| 전부 건너뜀 | 이미 파일 존재. 삭제 후 재실행 |
| 출력이 중간에 잘림 | `python -u` 플래그 추가 (실제 생성은 완료됨) |
| `SyntaxError` (한글) | 에디터 포커스 문제로 파이썬 파일에 한글 입력됨. git checkout으로 복구 |
| BOM 에러 (JSON 파싱 실패) | 메모장 저장 시 BOM 발생. `utf-8-sig` 읽기로 자동 처리됨 |
| 남캐인데 여성으로 나옴 | `--profile male` 누락 또는 `positive`에 `1girl` 포함 |
| 인물 중복/뒤틀림 | SD1.5에 832×1216 과대. `IMAGE_SIZE = (512, 768)` 수정 |
| 화풍이 예상과 다름 | WebUI 체크포인트 확인 (스크립트는 모델 미지정) |
| 라벨이 전부 동일 | JSON 첫 태그 중복. 구별 서술어로 교체 |

---

## 19. Kiro 자동 실행 규칙

`.kiro/steering/sd_char_gen.md`가 `inclusion: auto`로 등록되어 있어,
아래 형식 입력 시 자동 활성화된다.

```
캐릭터 생성: [약칭] / [외형 프롬프트] / [네거티브(선택)]
```

에이전트 준수 사항:
1. 작업 디렉터리는 `cwd` 파라미터로 지정. `cd` 사용 금지
2. 약칭이 영문 규격에 맞지 않으면 임의 변환 금지 → 재요청
3. 남성/소년 단서가 있으면 적절한 `--profile` 부착
4. `태그 충돌` 경고 시 강행 금지 → 사용자에게 알림
5. 실행 후 터미널 출력과 젠잇 마크다운 블록을 그대로 전달

---

## 20. 상세 문서 위치

| 목적 | 문서 |
|---|---|
| 설치·실행 절차 (사람용) | `사용법.txt` |
| 기능 명세 압축 (외부 AI용) | `에파_기능명세.md` |
| 요구사항·수용 기준 | `.kiro/specs/dynamic-pose-pipeline/requirements.md` |
| 아키텍처·설계 결정 근거 | `.kiro/specs/dynamic-pose-pipeline/design.md` |
| Kiro 자동 실행 규칙 | `.kiro/steering/sd_char_gen.md` |

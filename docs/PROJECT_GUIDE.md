# SD 캐릭터 에셋 배치 생성 파이프라인 — 프로젝트 가이드

> **문서 목적**: 개발자 및 AI 에이전트용 아키텍처, 내부 모듈 구조(`generator/`, `gui/`), 데이터 흐름, 테스트 및 확장 가이드.  
> **최종 갱신일**: 2026-11-20 (V2 포즈 체제 + Wave 0~4 리팩토링 반영)  
> **다른 문서 바로가기**:  
> - 🧭 AI 에이전트 핵심 인계 가이드 → [`AI_HANDOVER_GUIDE.md`](AI_HANDOVER_GUIDE.md)  
> - 🎨 창작자용 캐릭터 외형/포즈/Danbooru 태그 작성 규칙 → [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md)  
> - 📚 포즈 전체 카탈로그 → [`POSE_CATALOG.md`](POSE_CATALOG.md)  
> - 🤖 에이전트 협업 규칙 → [`GEMINI.md`](../GEMINI.md)

---

## 0. 한 줄 요약

로컬 SD WebUI (Forge / A1111) API를 호출해 캐릭터 챗봇용 이미지 에셋을 일괄 생성하고,
젠잇(Genit) 플랫폼에 붙여넣을 마크다운 호출 코드를 자동 조립하는 모듈화 CLI/GUI 파이프라인.

**핵심 설계 원칙: 프롬프트 데이터는 JSON에, 코어 로직은 `generator/` 패키지에, 스튜디오 UI는 `gui/` 패키지에 격리.**

---

## 1. 디렉터리 및 패키지 구성

```
kiro/
├── sd_batch_generator.py        경량 CLI 엔트리포인트 (인자 파싱 및 실행 위임)
├── kiro_gui.py                  NiceGUI 스튜디오 진입점 래퍼 (gui/ 위임)
├── auto_censor.py               고정밀 자동 검열 경량 CLI 래퍼 (generator.censor 위임)
├── pyproject.toml               패키지 메타데이터, 선택 의존성(gui/studio/ml/dev), pytest 설정
├── checkpoint_profiles.json     체크포인트별 수치 파라미터 프로필 DB
├── pose_database.json           V2 공용 프롬프트 DB (159종: 8개 섹션)
│
├── generator/                   ★ 코어 파이프라인 패키지 (ML Lazy Load 지원)
│   ├── __init__.py              공개 심볼 re-export + ML 모듈(__getattr__) 지연 로딩
│   ├── config.py                전역 상수, get_api_url(), configure_logging(), ConfigError
│   ├── models.py                슬롯 데이터클래스 (GenerationOptions, RunHooks, AllCharsResult 등)
│   ├── checkpoint.py            체크포인트 프로필 로드·매칭 및 4계층 수치 파라미터 해석
│   ├── pose_db.py               pose_database.json & events.json 파싱, V2 섹션 확장 SSOT
│   ├── prompt.py                태그 정규화, 충돌 감지, BREAK 전진 배치, opt-in 가중치 클램핑
│   ├── scene_composer.py        ★ 순수 도메인 씬 프롬프트·네거티브·배경 조합 엔진
│   ├── reference.py             IP-Adapter 참조 이미지 탐색, ControlNet 유닛, 태그 역추출(--from_image)
│   ├── roster.py                RosterPathManager, 캐릭터 프리셋 로드, 이벤트 자동 병합
│   ├── webui_client.py          WebUI API 통신(백오프 재시도), Forge 스크립트, 원자적 WebP 저장
│   ├── reporter.py              파일명 조립, 콘솔 요약, 젠잇 마크다운 블록 조립
│   ├── precheck.py              5대 사전점검(로스터·포즈 무결성)
│   ├── qa_validator.py          Auto-QC 불량 자동 판정 (Lazy Runtime 초기화)
│   ├── censor.py                검열 대상 판정·MobileSAM/OpenCV 렌더링·배치·_origin 백업/복원 SSOT
│   ├── runner.py                단일/전체 캐릭터 배치 실행 오케스트레이터
│   └── diagnostics/
│       └── self_test.py         64개 항목 자체 진단 스위트 (T1~T38d)
│
├── gui/                         ★ NiceGUI 스튜디오 패키지 (6모듈)
│   ├── __init__.py              GUI 공개 심볼 엔트리포인트
│   ├── settings.py              gui_settings.json UTF-8(BOM 허용) 로드/저장
│   ├── state.py                 GuiRuntimeState, 중복 실행 방지 가드, GuiLogBufferHandler
│   ├── runner_bridge.py         GenerationOptions 조립, V2 섹션 확장, ZenIt/Auto-QC 리포트 포맷
│   ├── components.py            헤더(비동기 Forge 헬스 체크), 좌측 제어·우측 모니터링/로그 패널
│   └── app.py                   build_gui(), 다중 탭 공유 상태, 실행/취소 오케스트레이션
│
├── tools/
│   └── censor_studio.py         독립 Flask 수동 블랭크 검열 스튜디오 (V2 SSOT + Path Traversal 방어)
│
├── tests/                       ★ pytest 오프라인 자동 검증 스위트 (Wave 0~4)
│   ├── test_wave0_safety.py     Lazy Load, 검열 파일명 파싱, 기본 안전성
│   ├── test_wave1_integrity.py  --ckpt-profile 4계층 우선순위, 검열 SSOT, 경로 보안
│   ├── test_wave2_core.py       scene_composer, GenerationOptions/RunHooks, V2 섹션 SSOT
│   ├── test_wave3_gui_censor.py gui/ 패키지, 실행 가드, 로그 스트리밍, 검열 배치·복원
│   └── test_wave4_coverage.py   WebUI 재시도/WebP/Mock, Auto-QC 판정, 검열 스타일, Censor Studio
│
├── docs/                        활성 가이드 문서 (docs/archive/ 레거시 제외)
│   ├── AI_HANDOVER_GUIDE.md     에이전트 마스터 인계 가이드
│   ├── PROJECT_GUIDE.md         [이 문서] 시스템 아키텍처 및 확장 가이드
│   ├── 캐릭터_포즈_제작_규칙.md  캐릭터/포즈/화풍 작성 SSOT
│   └── POSE_CATALOG.md          전체 포즈 카탈로그
│
└── projects/                    ★ 20개 로스터(작품)별 격리 루트
    └── {roster}/                (bani, bin, cafe, campus, dark_generals, default, don, hey,
        ├── characters/           home, maid, man, murim, oto, school, sea, sfk, sns, spy, ykn, zip)
        ├── references/          IP-Adapter 참조 이미지 (*.png, *.jpg, *.webp)
        ├── assets/              생성 에셋 (git 제외: {prefix}_{NNN}.webp / {prefix}_origin/)
        ├── events.json          [선택] 로스터 전용 이벤트 포즈 (200번대 자동 병합)
        └── background.json      [선택] 감정(emotions) 씬 전용 배경 프리셋
```

---

## 2. 의존성 및 실행 환경

| 항목 | 값 | 비고 |
|---|---|---|
| Python | 3.10+ | `slots=True` dataclass, `pyproject.toml` 기준 |
| 코어 의존성 | `requests>=2.31.0`, `Pillow>=10.0.0` | 최소 기본 의존성 |
| 선택 의존성 | `[gui]` `nicegui`, `[studio]` `flask`, `[ml]` `opencv/torch/ultralytics/dghs-imgutils`, `[dev]` `pytest` | `pip install -e ".[gui,dev]"` 등 |
| OS | Windows 전제 | `os.startfile()` 탐색기 자동 오픈 |
| 외부 서비스 | SD WebUI (Forge / A1111) | `--api` 필수, 기본 `http://127.0.0.1:7860` |

---

## 3. 데이터 흐름 (Data Flow)

```
[CLI / GUI] → GenerationOptions + RunHooks
       │
       ▼
RosterPathManager.resolve_roster()
       │
       ├─ load_pose_db() ── pose_database.json + (선택) events.json
       ├─ load_character() + apply_character_to_args()
       ├─ resolve_generation_params()  (Config < CheckpointProfile < CharacterConfig < CLI/GUI)
       └─ resolve_targets() (expand_v2_mode SSOT)
              │
              ▼
       runner.execute() / runner.run_all_chars()
              │
              ├─ reference.resolve_reference_image() → ControlNet 유닛 준비
              ├─ prompt.resolve_background() → background.json / CLI 배경 해석
              │
              ▼
       runner.run_batch()
              │  [코드별 순회 루프]
              ├─ 기존 파일 확인 (--overwrite 없을 시 스킵)
              ├─ scene_composer.compose_scene_prompt()
              │    (BREAK 2청크 전진 배치, 배경 치환, 탈의 스트리핑, 2인/POV/솔로 규칙)
              ├─ webui_client.generate_image() (백오프 재시도 + 422/500 Graceful Fallback)
              ├─ [선택] qa_validator.validate_generated_image() (--auto-qc 재시도)
              ├─ webui_client.save_as_webp() (원자적 .part 교체)
              └─ [선택] censor.censor_file() (--censor + assets/{char}_origin/ 백업)
              │
              ▼
       BatchResult / AllCharsResult
              ├─ reporter.print_summary()
              ├─ reporter.open_in_explorer()
              └─ reporter.build_genit_block()
```

---

## 4. `pose_database.json` V2 스펙 (공용 159종)

| 섹션명 | 코드 대역 | 개수 | 용도 |
|---|---|---|---|
| `emotions` | `000~019` | 20종 | 감정 표현 (`clean background` → `background.json` 치환 대상) |
| `poses` | `020~039` | 20종 | 착의 솔로 포즈 및 일상 밀착 |
| `events` | `040~052` | 13종 | 이벤트·상황 씬 |
| `romance` | `060~063` | 4종 | 로맨스 씬 |
| `h_scenes` | `080~127` | 48종 | 여성 성인 상호작용 씬 |
| `special_h` | `130~132` | 3종 | 여성 특수 연출 씬 |
| `special_otokonoko` | `133~135` | 3종 | 오토코노코 특수 연출 씬 |
| `scenes_otokonoko` | `150~197` | 48종 | 오토코노코 성인 상호작용 씬 |

- `_`로 시작하는 최상위 키는 메타/주석으로 파싱에서 제외된다.
- 포즈 항목은 문자열 또는 `{"label": "한국어", "prompt": "...", "prompt_female": "...", "prompt_otokonoko": "...", "outfit_slot": "..."}` 딕셔너리 형식을 지원한다.
- V2 성별 활성 섹션 확장은 `generator/pose_db.py`의 `expand_v2_mode()` 단일 함수로 통일되어 CLI·GUI가 동일하게 동작한다.

---

## 5. 캐릭터 JSON 스펙 및 우선순위

```json
{
  "prefix": "rei",
  "gender": "female",
  "default_mode": "female",
  "positive": "masterpiece, best quality, ... BREAK 1girl, solo, ...",
  "negative": "worst quality, low quality, ...",
  "nude_positive": "1girl, solo, ...",
  "ref_weight": 0.7,
  "seed": 1234567890,
  "gen_overrides": {
    "steps": 28,
    "cfg_scale": 6.0
  }
}
```

- **파라미터 우선순위 (4계층)**: `Config 기본값 < CheckpointProfile (--ckpt-profile) < CharacterConfig (gen_overrides) < CLI/GUI 명시 인자`
- `clip_skip`이 지정된 경우 Forge/A1111 페이로드의 `override_settings["CLIP_stop_at_last_layers"]`에 자동 반영된다.
- 네거티브에 `sweat`, `perspiration`, `liquid`, `splatter` 금지 (`T34b` 자동 검사).

---

## 6. 생성 파라미터 상수 규격 (`generator/config.py`)

| 상수 | 현재값 | 비고 |
|---|---|---|
| `IMAGE_SIZE` | `(832, 1216)` | SDXL 세로 초상화 |
| `STEPS` | `30` | CLI / GUI 기본값 일치 |
| `CFG_SCALE` | `6` (`6.0`) | CLI / GUI 기본값 일치 |
| `WEBP_QUALITY` | `95` | 고화질 WebP 저장 |
| `WEBP_METHOD` | `6` | 최고 압축 효율 |
| `REF_WEIGHT_DEFAULT` | `0.7` | 허용 범위 `0.0 ~ 2.0` |
| `SAMPLER_CANDIDATES` | `DPM++ 2M Karras`, `DPM++ 2M SDE Karras`, `DPM++ 2M`, `Euler`, `Euler a` | 순차 탐색 |
| `TXT2IMG_TIMEOUT` | `300`초 | 일시 오류 시 백오프 재시도 지원 |
| `API_HOST` | `http://127.0.0.1:7860` | `get_api_url()` 동적 조립 |

---

## 7. 주요 CLI 및 도구 실행 명령

```powershell
# 1) 자체 진단 (64개 항목) 및 pytest 전체 스위트
python sd_batch_generator.py --test
python -m pytest -v

# 2) 단일 캐릭터 생성 (체크포인트 프로필 + Auto-QC + 자동 검열)
python sd_batch_generator.py --roster dar --char ignis --ckpt-profile unholy --auto-qc --censor

# 3) 가중치 클램핑 opt-in (기본 비활성)
python sd_batch_generator.py --roster dar --char ignis --clamp-weights

# 4) 이미지 태그 역추출 (--from_image)
python sd_batch_generator.py --from_image projects/dar/references/ignis.png

# 5) NiceGUI 배치 스튜디오 실행
python kiro_gui.py --port 8080

# 6) 고정밀 자동 검열 CLI (로스터/캐릭터 지정 및 원본 복원 후 재검열)
python auto_censor.py --roster dar --char ignis --restore-origin

# 7) 수동 블랭크 검열 스튜디오 (독립 Flask 도구)
python tools/censor_studio.py --dir projects/dark_generals/assets --port 5050
```

---

## 8. 보안 제약 및 파일 저장 규칙

- **Prefix 보안**: `^[A-Za-z0-9_-]{1,64}$` 정규식 검증으로 경로 이탈 차단.
- **Censor Studio 경로 보안**: `resolve_safe_target_path()`에서 `Path.is_relative_to()` 검사로 Path Traversal 차단.
- **원자적 저장**: WebP 저장(`.part` → `os.replace`) 및 배치 검열(`.tmp_` → `os.replace`) 모두 중단 시 파일 손상 방지.
- **원본 보존 SSOT**: 검열 시 `assets/{char}_origin/{filename}`에 최초 1회 무검열 원본 선제 백업 (절대 덮어쓰기 금지).

---

## 9. Git 커밋 메시지 가이드라인 (사무적 규격)

- 모든 커밋 메시지는 민감한 세부 내용을 배제하고 기술적·사무적인 한국어 서술(`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`)을 사용한다.
- 상세 협업 규칙은 [`GEMINI.md`](../GEMINI.md)를 준수한다.

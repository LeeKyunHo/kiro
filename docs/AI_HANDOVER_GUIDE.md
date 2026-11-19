# 키로(Kiro) 파이프라인 마스터 인계 및 AI 개발자 가이드 (Handover Guide)

> **문서 목적**: 새로운 대화 세션이나 에이전트 인스턴스가 투입되었을 때 불필요한 일회성 히스토리나 특정 캐릭터 묘사에 혼선 없이, 시스템 아키텍처, 파이프라인 핵심 메커니즘, 프롬프트 작성 불변식, 검증 규칙을 즉시 파악하고 작업을 연속성 있게 수행하기 위한 핵심 가이드.  
> **최종 갱신일**: 2026-11-20 (V2 포즈 체제 + Wave 0~4 리팩토링 반영)  
> **연계 문서**: [`GEMINI.md`](../GEMINI.md), [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md), [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md), [`pose_database.json`](../pose_database.json), [`POSE_CATALOG.md`](POSE_CATALOG.md)

---

## 1. 시스템 핵심 원칙 및 아키텍처

- **목적**: Stable Diffusion WebUI (Forge/A1111) API 기반 캐릭터 챗봇용 에셋(V2 공용 **159종**: 감정 20종, 일상 포즈 20종, 이벤트 13종, 로맨스 4종, 여성 H-씬 48종 + 스페셜 3종, 오토코노코 H-씬 48종 + 스페셜 3종; 로스터 전용 `events.json` 자동 병합 지원) 배치 생성 및 젠잇(Genit) 마크다운 자동 조립.
- **체크포인트 프로필 (`--ckpt-profile`)**: `checkpoint_profiles.json` 기반 다중 모델 프로필 지원. **수치 파라미터 레이어**(`steps`, `cfg`, `sampler`, `scheduler`, `width`, `height`, `clip_skip`)만 자동 적용하며, 우선순위는 `Config 기본값 < CheckpointProfile < CharacterConfig(gen_overrides) < CLI/GUI 명시 인자`를 따른다. (`quality_positive`/`quality_negative`는 색감 과포화 방지를 위해 자동 병합하지 않음)
- **철저한 관심사 분리 (Separation of Concerns)**:
  - **프롬프트 데이터 격리**: 공용 포즈 DB는 `pose_database.json`에, 캐릭터 외형/의상은 `projects/{roster}/characters/{char}.json`(현재 20개 로스터 운영)에 격리.
  - **파이프라인 로직 격리**: 배치 생성, 씬 조합, API 통신, Auto-QC, 자동 검열 로직은 `generator/` 패키지에 모듈화.
  - **GUI 레이어 격리**: NiceGUI 스튜디오 UI는 `gui/` 패키지(6모듈)로 분리하고, `kiro_gui.py`는 얇은 진입점 래퍼로 유지.
  - **ML 의존성 Lazy Load**: `generator/__init__.py` (`__getattr__`) 및 `qa_validator._ensure_qa_runtime()` 지연 로딩으로 CLI 기본 임포트 시 `torch`/`cv2`/`imgutils` 부수효과 원천 차단.
- **테스트 무결성**: 모든 코드/데이터 수정 후 반드시 아래 두 스위트 PASS 유지:
  - `python sd_batch_generator.py --test` (**64개 항목** 자체 진단)
  - `python -m pytest -v` (**Wave 0~4** 단위·통합 테스트 스위트)

---

## 2. 파이프라인 런타임 핵심 메커니즘 (`generator/`)

### 2.1 실행 계약 (`GenerationOptions` & `RunHooks`)
- `generator/models.py`의 `GenerationOptions`가 순수 실행 옵션을, `RunHooks`가 진행 콜백·취소 판정·결과 집계를 캡슐화한다.
- `runner.run_batch()` / `runner.run_all_chars()`는 CLI와 `gui/`가 동일 계약을 공유하며, 모든 로깅은 `config.configure_logging()` 기반 `logging.getLogger("generator.*")`로 일원화된다.

### 2.2 순수 도메인 씬 조합 (`generator/scene_composer.py` & `generator/prompt.py`)
- **BREAK 2청크 전진 배치 (`assemble_prompt`)**:
  - **Chunk 1 (품질 및 포즈/구도)**: `[마스터피스 품질 태그], [포즈 및 행위 태그]`
  - **Chunk 2 (캐릭터 외형 및 트리거)**: `[인물 외형, 신체, 헤어스타일, 눈동자], [트리거 태그]`
- **선택적 가중치 클램핑 (`--clamp-weights`)**: 의도된 강조 가중치(`1.35~1.55`) 보호를 위해 기본 파이프라인에서는 비활성 유지하며, CLI `--clamp-weights` opt-in 시에만 `clamp_weights()`를 적용.
- **탈의/H-씬 의상 자동 스트리핑 (`strip_outfit_tags`)**:
  - 외형(헤어, 눈동자, 얼굴, 체형, 피부톤)은 100% 보존하고 의상·악세서리·속옷 태그만 자동 적출.
  - 탈의 씬 감지 시 의상 차단 네거티브 자동 주입 및 IP-Adapter 가중치를 `0.5`로 완화.
- **2인 상호작용 씬 페어링 (`compose_scene_prompt`)**:
  - 캐릭터 네거티브의 남성 억제 토큰(`1boy`, `male` 등) 자동 제외 및 포지티브 `solo` 제거.
  - 성별 자동 분기: 여성 씬 `hetero, 1boy, faceless male`, 오토코노코 씬 `yaoi, 2boys, faceless male`.
  - 모브 얼굴 복제 차단(`male face` 계열 가중치 네거티브) 및 거리 분리 방지 자동 주입.
  - `aftermath`, `aftersex` 또는 포즈 내 명시적 `solo` 감지 시 단독 샷 보장.
  - `partner off-screen` / `off-screen partner` 감지 시 모브 하반신/바지 침범 차단 네거티브 주입.

### 2.3 Auto-QC 및 자동 검열 SSOT (`qa_validator.py`, `censor.py`)
- **Auto-QC (`--auto-qc`)**: `validate_generated_image()`로 인원 초과, 머리 복제, 손 증식(POV `headpat` 등 예외 `max_hands=4`), 모브 얼굴 노출을 자동 감지해 최대 `--max-retries`회 재시도.
- **검열 SSOT (`generator/censor.py`)**:
  - `is_censor_target()` (V2 H-씬 대역 `80..135`, `150..200`, `NON_EXPOSURE_H_POSES` 제외)과 `get_origin_backup_path()` (`assets/{char}_origin/{filename}`)를 `runner.py`, `auto_censor.py`, `tools/censor_studio.py`가 단일 진실 공급원으로 공유.
  - `auto_censor.py`는 `--roster`, `--char`, `--restore-origin`(원본 복원 후 클린 재검열)을 지원하는 경량 CLI 래퍼.

---

## 3. 포즈 데이터베이스(`pose_database.json`) 구조 및 연계 규칙

> 📖 **포즈 상세 작성 가이드 및 전체 레퍼런스**: [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md)를 단일 진실 공급원(SSOT)으로 반드시 교차 확인하십시오.

### 3.1 V2 번호 대역 (공용 159종 정예화 체제)
| 섹션명 | 코드 대역 | 개수 | 설명 |
|---|---|---|---|
| `emotions` | `000~019` | 20종 | 감정 표현 (단독 샷, `clean background` → `background.json` 자동 치환) |
| `poses` | `020~039` | 20종 | 착의 솔로 포즈 및 일상 스킨십/밀착 |
| `events` | `040~052` | 13종 | 이벤트·상황 씬 (덮쳐짐, 쓰다듬기, 목욕, 웨딩 등) |
| `romance` | `060~063` | 4종 | 로맨스·밀착 교감 씬 |
| `h_scenes` | `080~127` | 48종 | 여성 2인 결합 및 상호작용 씬 |
| `special_h` | `130~132` | 3종 | 여성 특수 연출 씬 |
| `special_otokonoko` | `133~135` | 3종 | 오토코노코 특수 연출 씬 |
| `scenes_otokonoko` | `150~197` | 48종 | 오토코노코 2인 결합 및 상호작용 씬 (`080~127` 대칭 매핑) |
| `event_*` (선택) | `200+` | 가변 | 로스터별 `projects/{roster}/events.json` 동적 병합 |

### 3.2 시스템 연계 4대 핵심 불변식
1. **명시적 `label` 필수**: `{"label": "한국어라벨", "prompt": "Danbooru 태그"}` 형식 유지.
2. **성별 태그 금지**: 포즈 프롬프트 내에 `1girl`, `1boy` 하드코딩 금지 (성별은 캐릭터 및 파이프라인에서 자동 제어).
3. **손 위치 앵커링(Anchor)**: 손이 허공에 뜨면 기형 손 편향이 발생하므로 물리적 접촉면(상대방 어깨/가슴, 침대 등)을 명시.
4. **구도 충돌 금지**: `close-up`과 `cowboy shot`처럼 상반된 화각 태그 동시 사용 금지 (화면 분할 버그 유발).

---

## 4. 캐릭터 JSON 작성 규칙 (`projects/{roster}/characters/`)

> 📖 **캐릭터 템플릿, 4단계 레이어 공식 및 화풍 적용법**: [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md)를 단일 진실 공급원(SSOT)으로 교차 확인하십시오.

### 4.1 시스템 무결성 5대 불변식
1. **고정 표정/홍조(`blush`, `smile` 등) 절대 금지**: 감정 씬(`000~019`)의 다양한 감정 표현이 무력화됨.
2. **자세/배경(`standing`, `clean background` 등) 절대 금지**: 배경 치환 시스템(`background.json`)이 무력화됨.
3. **복합 괄호 내 쉼표 금지 (고아 괄호 방지)**: `(tag1, tag2:1.2)` 대신 `(tag1:1.2), (tag2:1.2)` 단일 괄호 사용.
4. **IP-Adapter 참조 가중치(`ref_weight`)**: 기본 `0.7` (권장 `0.55~0.7`). `0.8` 이상 시 포즈 제어력 급격히 상실.
5. **네거티브 금지어 준수**: `sweat, perspiration, liquid, splatter`는 파이프라인 단위 검사(T34b) 금지 태그이므로 캐릭터 네거티브에 절대 추가 금지.

---

## 5. 에이전트 작업 및 커뮤니케이션 규칙

1. **상호 교차 체크 원칙**:
   - **파이프라인 로직 / CLI / GUI / 런타임 아키텍처**: 본 문서([`AI_HANDOVER_GUIDE.md`](AI_HANDOVER_GUIDE.md))와 [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md) 최우선 확인.
   - **캐릭터 JSON 생성·수정 / 포즈 DB 개편**: [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md) 최우선 확인.
2. **응답 언어**: 모든 대화, 설명, 코드 분석, 결과 보고는 **항상 한국어**로 작성.
3. **사무적·기술적 용어 사용**: 민감한 표현을 배제하고 공식 기술/구도 용어 사용.
4. **Git 커밋 규격**: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:` 접두어 사용 및 민감 표현 배제.
5. **작업 완료 전 무결성 검증**: 수정 후 `python sd_batch_generator.py --test` (64개) 및 `python -m pytest -v` 전체 PASS 확인.

---

## 6. 핵심 아키텍처 의사결정 기록 (ADR 요약)

1. **`is_offscreen_partner` 도입**: 1인칭 POV 씬(`partner off-screen`)에서 모브 하반신/바지 침범 차단 네거티브 자동 주입.
2. **`is_solo_scene` 도입**: H-씬 대역 내 단독 씬(`solo` 명시)에서 2인 모브 강제 소환 차단.
3. **머메이드 실루엣 금지**: `mermaid` 토큰의 인어 꼬리화 버그 방지를 위해 `high side slit dress, elegant draped maxi dress`로 표준화.
4. **소품 환각 및 색상 이염 방어**: 탈의/침대 씬 음료 소품 전역 차단, 3중 헤어 컬러 앵커링 + 네거티브 차단 적용.
5. **Wave 0~4 리팩토링 (2026-11)**:
   - `runner.py` 씬 조합을 순수 도메인 모듈(`scene_composer.py`)로 분리하고 `GenerationOptions`/`RunHooks` 도입.
   - `kiro_gui.py`를 `gui/` 6모듈 패키지로 분해하고 중복 실행 방지 가드 및 로그 스트리밍 핸들러 연결.
   - `generator/censor.py`를 검열 판정·배치·`_origin` 백업/복원 SSOT로 단일화하고 중복 스크립트(`apply_precise_censor.py`, `.txt` 중복 문서) 제거.
   - `pyproject.toml` 및 `tests/` (`pytest`) 기반 오프라인 자동 검증 체계 구축.

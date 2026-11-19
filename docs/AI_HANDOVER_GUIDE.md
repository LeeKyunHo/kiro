# 키로(Kiro) 파이프라인 마스터 인계 및 AI 개발자 가이드 (Handover Guide)

> **문서 목적**: 새로운 대화 세션이나 에이전트 인스턴스가 투입되었을 때 불필요한 일회성 히스토리나 특정 캐릭터 묘사에 혼선 없이, 시스템 아키텍처, 파이프라인 핵심 메커니즘, 프롬프트 작성 불변식, 검증 규칙을 즉시 파악하고 작업을 연속성 있게 수행하기 위한 핵심 가이드.  
> **최종 갱신일**: 2026-11-20 (V2 포즈 체제 + Wave 0~4 리팩토링 + Wave P1~P4-B 기능 이식 완결 반영)  
> **연계 문서**: [`GEMINI.md`](../GEMINI.md), [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md), [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md), [`pose_database.json`](../pose_database.json), [`POSE_CATALOG.md`](POSE_CATALOG.md)

---

## 1. 시스템 핵심 원칙 및 아키텍처

- **목적**: Stable Diffusion WebUI (Forge/A1111) API 기반 캐릭터 챗봇용 에셋(V2 공용 **159종**: 감정 20종, 일상 포즈 20종, 이벤트 13종, 로맨스 4종, 여성 H-씬 48종 + 스페셜 3종, 오토코노코 H-씬 48종 + 스페셜 3종; 로스터 전용 `events.json`/`pose_overrides.json` 자동 병합 지원) 배치 생성, 자동 검수·검열 및 젠잇(Genit) 패키징·마크다운 조립.
- **체크포인트 프로필 (`--ckpt-profile`)**: `checkpoint_profiles.json` 기반 다중 모델 프로필 지원. **수치 파라미터 레이어**(`steps`, `cfg`, `sampler`, `scheduler`, `width`, `height`, `clip_skip`)만 자동 적용하며, 우선순위는 `Config 기본값 < CheckpointProfile < CharacterConfig(gen_overrides) < CLI/GUI 명시 인자`를 따른다. (`quality_positive`/`quality_negative`는 색감 과포화 방지를 위해 자동 병합하지 않음)
- **철저한 관심사 분리 (Separation of Concerns)**:
  - **프롬프트 데이터 격리**: 공용 포즈 DB는 `pose_database.json`, 포즈 프리셋은 `pose_sets.json`, 캐릭터 외형/의상은 `projects/{roster}/characters/{char}.json`(20개 로스터 운영)에 격리.
  - **파이프라인 코어 (`generator/`)**: 배치 생성, 씬 조합, API 통신, Auto-QC, 4모듈 하이브리드 검열, 전 포즈 사전점검, 옵션 SSOT 레지스트리, 플랫 에셋/휴지통 매니저를 모듈화.
  - **GUI 스튜디오 (`gui/`)**: NiceGUI 통합 스튜디오(`/` 생성, `/gallery` PC 통합 갤러리·매트릭스·라이트박스, `/inspect` 모바일 원격 검수 뷰어), 공용 HTML5 인라인 리터치 캔버스(`retouch_editor.py`), FIFO 비동기 작업 큐(`job_manager.py`), REST API(`api_routes.py`)로 분리하고 `kiro_gui.py`는 얇은 진입점 래퍼 유지.
  - **ML 의존성 Lazy Load**: `generator/__init__.py` (`__getattr__`) 및 `qa_validator._ensure_qa_runtime()` 지연 로딩으로 CLI 기본 임포트 시 `torch`/`ultralytics`/`imgutils` 부수효과 원천 차단.
- **핵심 구조 불변식 및 테스트 무결성**:
  - `runner.run_batch()` **150줄 이하** AST 불변식, `import generator` **Lazy ML Load** 불변식, **`GENERATION_OPTION_SPECS` 삼위일체 동기화**(`GenerationOptions` ↔ CLI ↔ GUI) 유지.
  - 모든 코드/데이터 수정 후 반드시 아래 두 스위트 PASS 유지:
    - `python sd_batch_generator.py --test` (**64개 항목** 자체 진단)
    - `python -m pytest -v` (**Wave 0~4 + Wave P1~P4-B** 전체 오프라인 단위·통합 테스트 스위트)

---

## 2. 파이프라인 런타임 핵심 메커니즘 (`generator/`)

### 2.1 실행 계약 (`GenerationOptions`, `RunHooks`, `GENERATION_OPTION_SPECS`)
- `generator/models.py`의 `GenerationOptions`가 순수 실행 옵션을, `RunHooks`가 진행 콜백·취소 판정·결과 집계를 캡슐화한다.
- `generator/option_specs.py`의 `GENERATION_OPTION_SPECS`가 `GenerationOptions` ↔ `sd_batch_generator.py` CLI ↔ `gui/settings.py` & `gui/runner_bridge.py` 간 필드·기본값·choices 삼위일체 단일 진실 공급원(SSOT)을 보장한다.
- `runner.run_batch()` / `runner.run_all_chars()`는 CLI와 `gui/`가 동일 계약을 공유하며, 모든 로깅은 `config.configure_logging()` 기반 `logging.getLogger("generator.*")`로 일원화된다.

### 2.2 프롬프트 품질 안정화 & 씬 조합 (`generator/prompt.py`, `generator/scene_composer.py`)
- **BREAK 2청크 전진 배치 (`assemble_prompt`)**:
  - **Chunk 1 (품질 및 포즈/구도)**: `[마스터피스 품질 태그], [포즈 및 행위 태그]`
  - **Chunk 2 (캐릭터 외형 및 트리거)**: `[인물 외형, 신체, 헤어스타일, 눈동자], [트리거 태그]`
- **프롬프트 정제 (Wave P1)**:
  - `harmonize_character_expression()`: 포즈에 명시 감정/표정이 있을 때 캐릭터 기본 무표정(`calm expression`, `stoic` 등) 충돌 자동 조화.
  - `remove_conflicting_tags_from_negative()`: 모브 BREAK 블록을 격리하고 괄호 균형을 보존하면서 긍정/부정 충돌 태그 제거(구도/인원 보호 태그 제외).
  - `apply_composition_anti_drift()`: 화각별(`tight bust portrait`, `cowboy shot`, `full body` 등) 반대 구도 이탈 방지 네거티브 자동 주입.
  - `resolve_theme_outfit()`: 캐릭터 `outfits` 미지정 시 `DEFAULT_THEME_OUTFITS` 안전 폴백.
  - `clamp_prompt_weights()`: 긍정 `<= 1.15` / 부정 `<= 1.25` 분리 클램핑(기본 비활성 `clamp_weights=False`, `--clamp-weights` opt-in 시에만 동작해 기존 `1.35~1.55` 강조 가중치 보호).
- **4-Tier 모브 파이프라인(ADR-100/101/105) & `signature_accessories`(ADR-106) (Wave P3)**:
  - `PoseEntry.mob_tier`(선택, `0~3`) 및 `partner_pos`(`above`, `below`, `behind`, `side_kiss`, `offscreen`, `carry`, `bury_chest`) 지원.
  - `mob_tier=0`은 단독 구도 보존, `mob_tier=1~3`은 방향성 인라인 파트너 묘사 및 Phase S7 상안부 그림자 음영(`_MOB_SHADOW_EYES_POS`) + 비오토코노코 여성 한정 `(clear visible female eyes:1.15)` 보호 + 남성 눈/측면 얼굴 차단(`_MOB_EYELESS_BANGS_NEG`) 적용 (`mob_tier=None` 기존 V2 포즈 100% 하위 호환).
  - `CharacterConfig.signature_accessories` 정의 시 캐릭터 외형 전단 우선 배치 및 네거티브 충돌 자동 해제(탈의 씬은 얼굴/헤어 소품만 유지).

### 2.3 Auto-QC & `strict_censor_qc` (`qa_validator.py`, `runner.py`)
- **Auto-QC (`--auto-qc`, Wave P1 정밀화)**:
  - Lazy ML Load(`_ensure_qa_runtime()`) 유지.
  - CPU 스레드 제어, OpenCV 기반 **Color Burn(색감 과포화/소실)** 사전 감지, **화각 드리프트**(`tight bust`/`cowboy shot`/`full body` 불일치) 검사, 인원 초과·머리 복제·손 증식(POV 예외 `max_hands=4`)·모브 얼굴 노출 **Short-Circuit** 판정 지원.
- **`strict_censor_qc` (`--strict-censor-qc`, Wave P2-B & P3)**:
  - `--censor`와 독립 동작. 성인 노출 필수 씬(`is_censor_target` 참 & 비여운 & 비솔로 & `mob_tier not in (0, 1, 2)`)에서 `detect_genitals()` 0건 시 구도 실패로 간주하고 최대 `--max-retries`회 리롤.

### 2.4 4모듈 Phase P2.5 하이브리드 검열 엔진 (`generator/censor*.py`)
- **모듈 분해**:
  - `generator/censor.py`: 오케스트레이션 SSOT(`is_censor_target`, `extract_pose_code`, `get_origin_backup_path`, `censor_image`, `censor_file`, `detect_genitals`, `run_batch_censor`, `verify_and_heal_censor`, `_atomic_replace_with_retry`).
  - `generator/censor_styles.py`: 동적 해상도 스케일(832px 기준) 렌더링 스타일(`white_silhouette`, `black_silhouette`, `manga_bar`, `manga_glow`, `wide_bar`, `slim_bar`, `shadow`, `mosaic`, `debug_bbox`).
  - `generator/censor_seg.py`: **SAM 2.1 (`sam2.1_s.pt`) 우선 로드 → MobileSAM (`mobile_sam.pt`) 자동 폴백**, 커스텀 **YOLO11s-seg (`censor_seg.pt`)** 싱글톤 로더(탐색 순서: `KIRO_*`/`PLEPA_*` 환경변수 → `kiro/models/` → `../plepa/models/` 자동 공유, 미보유 시 `None` 폴백), `unload_censor_models()`, Semantic-Guided SAM 투영, Residual Span 중심축 회랑, 손 가로질림 Sub-BBox 분할, 2D 외곽 선화 스냅, `protection_mask` 0px 비침범 보장.
  - `generator/censor_detect.py`: ADR-99 동결 기준선(`min_confidence=0.16`, `iou>=0.35 or iomin>=0.55` `penis` 우선 병합, 고신뢰 `>=0.50` 역광 피부율 면제, 하단 엣지 오탐 필터, 1.6x Zoom-In ROI 다중 스케일), Dual Consensus Gating + Residual Span 2-Way 복원, ADR-103 컨텍스트 판별(여성 비노출 `101/102/104/105`만 제외, 오토코노코 `150~200` 전수 스캔).
- `auto_censor.py`와 `tools/censor_studio.py`(독립 Flask 도구) 모두 `generator/censor.py` SSOT를 공유한다.

### 2.5 실무 유틸리티 · 사전점검 · 독립 스크립트 (`Wave P2-A · P3`)
- **포즈별 자동 배경 (`generator/pose_background.py`)**: `projects/{roster}/pose_background_mapping.json` 기반 단일 코드 > 대역 > 기본 배경 해석 (플랫 `background.json` 완전 호환).
- **포즈 세트 & 결정론적 시드**: `pose_sets.json` (`--pose-set`) 및 CRC32 기반 `--deterministic-pose-seed`.
- **Genit 패키징 (`generator/genit_exporter.py`)**: `--export-genit` / `--export-dir` / `--export-code` CLI 지원.
- **전 포즈 조립 시뮬레이션 (`generator/precheck.py`)**: 정적 JSON 검사 + 로스터 `캐릭터 × 활성 포즈` 전수 `compose_scene_prompt()` 시뮬레이션(착의 고정, 탈의 의상 누수, 테마 스왑) + `pose_background_mapping.json` 정합성 검증.
- **독립 감사·스냅샷 도구 (`scripts/`)**:
  - `scripts/audit_asset_alignment.py`: 플랫 `assets/{char}/` + `{char}_origin/` 완비율·구 라벨·누락 코드 감사 및 `--apply-rename` 2-Pass 안전 리네임.
  - `scripts/export_context.py`: Kiro 단독 3-Tier 컨텍스트 스냅샷(`context_snapshot/`, `.gitignore` 등록) 자동 추출.

### 2.6 통합 검수 스튜디오 · FIFO 작업 큐 · REST API (`Wave P4-A · P4-B`)
- **`generator/asset_manager.py`**: Kiro 플랫 에셋 매트릭스 조회, 안전 휴지통 번들 삭제·Undo(`assets/_trash/{trash_id}/` + `manifest.json`), `_origin` 1클릭 복원, 리터치 Base64 선제 백업·원자 WebP 저장, `Path.resolve().is_relative_to(projects_dir)` Path Traversal 차단.
- **`gui/job_manager.py`**: 단일 워커 기반 비동기 FIFO 배치/리롤 작업 큐(`GlobalJobManager`) — GPU 경합 원천 차단, 개별 대기/실행 중 취소, 실패 격리.
- **`gui/api_routes.py`**: FastAPI/NiceGUI REST API(`/api/rosters`, `/api/gallery/assets`, `/api/image`, `/api/restore_origin`, `/api/recensor`, `/api/retouch/save`, `/api/asset/delete`, `/api/asset/restore`, `/api/reroll`, `/api/jobs`, `/api/jobs/{job_id}/cancel`) 및 `KIRO_API_TOKEN` 선택 인증 가드.
- **통합 UI 라우트**: `/` (생성 스튜디오 + 실시간 FIFO 큐 패널), `/gallery` (PC 통합 갤러리 · 캐릭터×포즈 매트릭스 · 라이트박스 `Space` 원본 비교 · 원클릭 액션), `/inspect` (경량 모바일 원격 검수 뷰어), `gui/retouch_editor.py` (PC·모바일 공용 마우스/터치 HTML5 Canvas 리터치 에디터).

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
| `event_*` (선택) | `200+` | 가변 | 로스터별 `projects/{roster}/events.json` / `pose_overrides.json` 동적 병합 |

### 3.2 시스템 연계 4대 핵심 불변식
1. **명시적 `label` 필수**: `{"label": "한국어라벨", "prompt": "Danbooru 태그"}` 형식 유지 (선택 필드: `mob_tier`, `partner_pos`, `required_outfit`, `prompt_female`, `prompt_otokonoko`, `negative_add`).
2. **성별 태그 금지**: 포즈 프롬프트 내에 `1girl`, `1boy` 하드코딩 금지 (성별은 캐릭터 및 파이프라인에서 자동 제어).
3. **손 위치 앵커링(Anchor)**: 손이 허공에 뜨면 기형 손 편향이 발생하므로 물리적 접촉면(상대방 어깨/가슴, 침대 등)을 명시.
4. **구도 충돌 금지**: `close-up`과 `cowboy shot`처럼 상반된 화각 태그 동시 사용 금지 (화면 분할 버그 유발).

---

## 4. 캐릭터 JSON 작성 규칙 (`projects/{roster}/characters/`)

> 📖 **캐릭터 템플릿, 4단계 레이어 공식 및 화풍 적용법**: [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md)를 단일 진실 공급원(SSOT)으로 반드시 교차 확인하십시오.

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

1. **`is_offscreen_partner` / `is_solo_scene` 도입**: 1인칭 POV 씬 및 H-씬 내 단독 샷의 모브 침범 차단.
2. **머메이드 실루엣 금지 & 소품 환각 방어**: `high side slit dress, elegant draped maxi dress` 표준화, 탈의/침대 씬 음료 소품 전역 차단.
3. **Wave 0~4 코어 리팩토링**: `scene_composer.py` 분리, `GenerationOptions`/`RunHooks`, `gui/` 모듈화, `censor.py` SSOT 단일화, `pytest` 체계 구축.
4. **Wave P1 (프롬프트 품질 + Auto-QC 정밀화)**: 표정 조화·괄호 보존 충돌 제거·화각 이탈 방지·테마 의상 폴백·분리 클램핑(opt-in), Auto-QC Color Burn·화각 드리프트·Short-Circuit(Lazy Load 유지).
5. **Wave P2-A · P2-B (Phase P2.5 하이브리드 검열 4모듈 + 유틸 4종)**: `censor_styles`/`censor_seg`/`censor_detect` 분리, SAM 2.1 → MobileSAM 폴백, YOLO11s-seg (`../plepa/models/` 공유), ADR-99/103, `strict_censor_qc`, `PoseBackgroundService`, `pose_sets.json`, `--deterministic-pose-seed`, `GenitExporter`.
6. **Wave P3 (4-Tier 모브 · 고유 소품 · 전 포즈 시뮬레이션 · 삼위일체 레지스트리 · 도구)**: `mob_tier`/`partner_pos`/Phase S7, `signature_accessories`, `precheck` 전 포즈 조립 시뮬레이션, `GENERATION_OPTION_SPECS`, `scripts/audit_asset_alignment.py`, `scripts/export_context.py`.
7. **Wave P4-A · P4-B (통합 검수 스튜디오)**: `AssetManager`(플랫 에셋·휴지통/Undo·`_origin` 복원·리터치 원자 저장), `GlobalJobManager`(FIFO 직렬 큐), `gui/api_routes.py`(REST API + `KIRO_API_TOKEN`), `/gallery` 매트릭스·라이트박스, `/inspect` 모바일 뷰어, 공용 HTML5 인라인 리터치 캔버스(`retouch_editor.py`).

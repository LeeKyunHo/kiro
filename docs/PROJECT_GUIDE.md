# SD 캐릭터 에셋 배치 생성 파이프라인 — 프로젝트 가이드

> **문서 목적**: 개발자 및 AI 에이전트용 아키텍처, 내부 모듈 구조(`generator/`, `gui/`, `scripts/`), 데이터 흐름, REST API, 테스트 및 확장 가이드.  
> **최종 갱신일**: 2026-11-20 (V2 포즈 체제 + Wave 0~4 리팩토링 + Wave P1~P4-B 기능 이식 완결 반영)  
> **다른 문서 바로가기**:  
> - 🧭 AI 에이전트 핵심 인계 가이드 → [`AI_HANDOVER_GUIDE.md`](AI_HANDOVER_GUIDE.md)  
> - 🎨 창작자용 캐릭터 외형/포즈/Danbooru 태그 작성 규칙 → [`캐릭터_포즈_제작_규칙.md`](캐릭터_포즈_제작_규칙.md)  
> - 📚 포즈 전체 카탈로그 → [`POSE_CATALOG.md`](POSE_CATALOG.md)  
> - 🤖 에이전트 협업 규칙 → [`GEMINI.md`](../GEMINI.md)

---

## 0. 한 줄 요약

로컬 SD WebUI (Forge / A1111) API를 호출해 캐릭터 챗봇용 이미지 에셋을 일괄 생성하고,
Auto-QC·Phase P2.5 하이브리드 검열·통합 PC/모바일 검수 및 인라인 리터치·젠잇(Genit) 패키징까지 일원화한 CLI/GUI 파이프라인.

**핵심 설계 원칙: 프롬프트 데이터는 JSON에, 코어 로직은 `generator/` 패키지에, 스튜디오 UI·API는 `gui/` 패키지에, 독립 감사·스냅샷 도구는 `scripts/`에 격리.**

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
├── pose_sets.json               V2 포즈 세트 프리셋 (--pose-set)
│
├── generator/                   ★ 코어 파이프라인 패키지 (ML Lazy Load 지원)
│   ├── __init__.py              공개 심볼 re-export + ML/무거운 모듈(__getattr__) 지연 로딩
│   ├── config.py                전역 상수, get_api_url(), configure_logging(), ConfigError
│   ├── models.py                슬롯 데이터클래스 (GenerationOptions, RunHooks, PoseEntry, CharacterConfig 등)
│   ├── option_specs.py          ★ GenerationOptions ↔ CLI ↔ GUI 삼위일체 옵션 메타데이터 SSOT
│   ├── checkpoint.py            체크포인트 프로필 로드·매칭 및 4계층 수치 파라미터 해석
│   ├── pose_db.py               pose_database.json / events.json / pose_sets.json 파싱, V2 섹션 확장 SSOT
│   ├── pose_background.py       포즈별 자동 배경 매핑 서비스 (pose_background_mapping.json + 플랫 background.json)
│   ├── prompt.py                태그 정규화, 표정 조화, 괄호 보존 충돌 제거, 테마 의상 폴백, 결정론적 시드, opt-in 클램핑
│   ├── scene_composer.py        ★ 순수 도메인 씬 조합 엔진 (4-Tier 모브/Phase S7, signature_accessories, 화각 드리프트 방어)
│   ├── reference.py             IP-Adapter 참조 이미지 탐색, ControlNet 유닛, 태그 역추출(--from_image)
│   ├── roster.py                RosterPathManager, 캐릭터 프리셋 로드, 이벤트 자동 병합
│   ├── webui_client.py          WebUI API 통신(백오프 재시도), Forge 스크립트, 원자적 WebP 저장
│   ├── reporter.py              파일명 조립, 콘솔 요약, 젠잇 마크다운 블록 조립
│   ├── genit_exporter.py        Genit 규격 패키징 익스포터 (--export-genit)
│   ├── precheck.py              정적 무결성 + 전 포즈 조립 시뮬레이션 + 배경 매핑 정합성 사전점검
│   ├── qa_validator.py          Auto-QC 정밀 판정 (Color Burn, 화각 드리프트, Short-Circuit, Lazy Load 유지)
│   ├── censor.py                검열 오케스트레이터 SSOT (ADR-99/103, 배치·_origin 백업/복원, 2차 검증/치유)
│   ├── censor_styles.py         동적 해상도 스케일 렌더링 스타일 (실루엣·망가 바/글로우·바·모자이크·디버그)
│   ├── censor_seg.py            Phase P2.5 세그멘테이션 (SAM 2.1→MobileSAM 폴백, YOLO11s-seg, 선화 스냅, 보호 0px)
│   ├── censor_detect.py         1.6x Zoom-In ROI 다중 스케일 탐지, Dual Consensus + Residual Span 2-Way 복원
│   ├── asset_manager.py         ★ 플랫 에셋 매트릭스, 안전 휴지통/Undo, _origin 복원, 리터치 원자 저장, Path 보안
│   ├── runner.py                단일/전체 캐릭터 배치 오케스트레이터 (strict_censor_qc, run_batch <= 150줄 유지)
│   └── diagnostics/
│       └── self_test.py         64개 항목 자체 진단 스위트 (T1~T38d)
│
├── gui/                         ★ NiceGUI 통합 스튜디오 · API · FIFO 큐 패키지 (10모듈)
│   ├── __init__.py              GUI 공개 심볼 엔트리포인트
│   ├── settings.py              gui_settings.json UTF-8(BOM 허용) 로드/저장
│   ├── state.py                 GuiRuntimeState, 중복 실행 방지 가드, GuiLogBufferHandler
│   ├── job_manager.py           ★ 단일 워커 비동기 FIFO 배치/리롤 작업 큐 (GlobalJobManager)
│   ├── api_routes.py            ★ 검수·리터치·재검열·휴지통/Undo·리롤 REST API (/api/* + KIRO_API_TOKEN)
│   ├── retouch_editor.py        ★ PC·모바일 공용 마우스/터치 HTML5 Canvas 인라인 리터치 에디터
│   ├── runner_bridge.py         GenerationOptions 조립, V2 섹션 확장, ZenIt/Auto-QC 리포트 포맷
│   ├── components.py            헤더(네비게이션·FIFO 큐 배지·Forge 헬스), 제어/모니터/큐 패널
│   ├── gallery.py               ★ PC 통합 갤러리·캐릭터×포즈 매트릭스·라이트박스 (/gallery)
│   ├── mobile_viewer.py         ★ 경량 모바일 원격 검수 뷰어 (/inspect)
│   └── app.py                   build_gui(), register_studio_pages(), 실행/취소 오케스트레이션
│
├── scripts/                     ★ 독립 감사·컨텍스트 스냅샷 도구
│   ├── audit_asset_alignment.py 로스터별 완비율·구 라벨·누락 코드 감사 및 2-Pass 안전 리네임
│   └── export_context.py        Kiro 단독 3-Tier 컨텍스트 스냅샷 추출기 (context_snapshot/)
│
├── tools/
│   └── censor_studio.py         독립 Flask 수동 블랭크 검열 스튜디오 (V2 SSOT + Path Traversal 방어)
│
├── tests/                       ★ pytest 오프라인 자동 검증 스위트 (Wave 0~4 + Wave P1~P4-B)
│   ├── test_wave0_safety.py     Lazy Load, 검열 파일명 파싱, 기본 안전성
│   ├── test_wave1_integrity.py  --ckpt-profile 4계층 우선순위, 검열 SSOT, 경로 보안
│   ├── test_wave2_core.py       scene_composer, GenerationOptions/RunHooks, V2 섹션 SSOT, run_batch<=150줄
│   ├── test_wave3_gui_censor.py gui/ 패키지, 실행 가드, 로그 스트리밍, 검열 배치·복원
│   ├── test_wave4_coverage.py   WebUI 재시도/WebP/Mock, Auto-QC 판정, 검열 스타일, Censor Studio
│   ├── test_port_p1_prompt_qa.py          Wave P1 프롬프트 정제 + Auto-QC Color Burn/화각 드리프트
│   ├── test_port_p2a_censor_utils.py      Wave P2-A 검열 스타일/안정화 + 배경매핑/포즈세트/시드/익스포터
│   ├── test_port_p2b_hybrid_censor.py     Wave P2-B Phase P2.5 하이브리드 검열 + strict_censor_qc + Lazy Load
│   ├── test_port_p3_mob_precheck_tools.py Wave P3 4-Tier 모브/소품/precheck 시뮬레이션/삼위일체/감사·스냅샷
│   ├── test_port_p4a_queue_api.py         Wave P4-A AssetManager/GlobalJobManager/REST API·보안
│   └── test_port_p4b_gui_studio.py        Wave P4-B 리터치 에디터·갤러리·모바일 뷰어·스냅샷 스모크
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
        ├── assets/              생성 에셋 (git 제외: {prefix}_{NNN}.webp / {prefix}_origin/ / _trash/)
        ├── events.json          [선택] 로스터 전용 이벤트 포즈 (200번대 자동 병합)
        ├── pose_overrides.json  [선택] 로스터 전용 포즈 오버라이드
        ├── background.json      [선택] 감정(emotions) 씬 전용 배경 프리셋 (플랫 스키마)
        └── pose_background_mapping.json [선택] 포즈 코드/대역별 배경 프리셋 자동 매핑
```

---

## 2. 의존성 및 실행 환경

| 항목 | 값 | 비고 |
|---|---|---|
| Python | 3.10+ | `slots=True` dataclass, `pyproject.toml` 기준 |
| 코어 의존성 | `requests>=2.31.0`, `Pillow>=10.0.0` | 최소 기본 의존성 |
| 선택 의존성 | `[gui]` `nicegui/fastapi`, `[studio]` `flask`, `[ml]` `opencv/torch/ultralytics/dghs-imgutils`, `[dev]` `pytest/httpx` | `pip install -e ".[gui,dev]"` 등 |
| 가중치 공유 | `kiro/models/` → `../plepa/models/` 자동 탐색 | `sam2.1_s.pt`, `mobile_sam.pt`, `censor_seg.pt` |
| OS | Windows 전제 | `os.startfile()` 탐색기 자동 오픈, `_atomic_replace_with_retry()` 락 방어 |
| 외부 서비스 | SD WebUI (Forge / A1111) | `--api` 필수, 기본 `http://127.0.0.1:7860` |

---

## 3. 데이터 흐름 및 웹 스튜디오 구조

### 3.1 생성 파이프라인 흐름
```
[CLI / GUI / FIFO Queue] → GenerationOptions + RunHooks
       │
       ▼
RosterPathManager.resolve_roster()
       ├─ load_pose_db() + resolve_pose_set_codes() (--pose-set)
       ├─ load_character() + apply_character_to_args() (signature_accessories 포함)
       ├─ resolve_generation_params() (Config < CheckpointProfile < CharacterConfig < CLI/GUI)
       └─ resolve_targets() (expand_v2_mode SSOT)
              │
              ▼
       runner.run_batch()  [<= 150줄 불변식]
              ├─ PoseBackgroundService + compute_deterministic_pose_seed()
              ├─ scene_composer.compose_scene_prompt()
              │    (BREAK 2청크, 표정 조화, 4-Tier 모브/Phase S7, signature_accessories, 화각 방어)
              ├─ webui_client.generate_image() (백오프 재시도 + HTTP 422/500 순정 폴백)
              ├─ [선택] qa_validator.validate_generated_image() (--auto-qc)
              ├─ [선택] strict_censor_qc 노출 타겟 미탐지 리롤 (--strict-censor-qc, mob_tier 0~2 면제)
              ├─ webui_client.save_as_webp() (원자적 .part 교체)
              └─ [선택] censor.censor_file() (Phase P2.5 하이브리드 검열 + assets/{char}_origin/ 백업)
```

### 3.2 통합 검수 스튜디오 라우트 및 REST API (`python kiro_gui.py`)
| 경로 | 용도 | 비고 |
|---|---|---|
| `GET /` | 배치 생성 스튜디오 | 단일/전체 캐릭터 생성, 사전점검, 실시간 로그·FIFO 큐 패널 |
| `GET /gallery` | PC 통합 갤러리 · 매트릭스 · 라이트박스 | V2 8개 섹션 필터, `Space` 원본 비교, 인라인 리터치·재검열·복원·리롤·삭제/Undo |
| `GET /inspect` | 모바일 원격 검수 뷰어 | 스마트폰 세로 카드 UI, 터치 리터치 캔버스, `KIRO_API_TOKEN` 세션 인증 |
| `GET /api/rosters`, `GET /api/gallery/assets` | 로스터 목록 및 에셋 매트릭스 JSON | `AssetManager` 플랫 스캔 |
| `GET /api/image` | 현재 에셋 또는 `origin=1` 무검열 원본 서빙 | Path Traversal `403`, `Cache-Control: no-store` |
| `POST /api/restore_origin`, `POST /api/recensor`, `POST /api/retouch/save` | 원본 복원, 즉시 재검열, 리터치 Base64 저장 | 선제 `_origin` 백업 + 원자 교체 + `KIRO_API_TOKEN` |
| `POST /api/asset/delete`, `POST /api/asset/restore` | 휴지통 번들 이동(`_trash/{id}`) 및 100% Undo | 본체 + `_origin` + `manifest.json` 동시 이동/복원 |
| `POST /api/reroll`, `GET /api/jobs`, `POST /api/jobs/{job_id}/cancel` | FIFO 비동기 단건 리롤 큐 등록·조회·취소 | `GlobalJobManager` 단일 워커 직렬 실행 |

---

## 4. 주요 CLI 및 도구 실행 명령

```powershell
# 1) 자체 진단 (64개 항목) 및 pytest 전체 스위트
python sd_batch_generator.py --test
python -m pytest -v

# 2) 단일 캐릭터 생성 (체크포인트 프로필 + 포즈 세트 + 결정론적 시드 + Auto-QC + 엄격 검열 QC + 자동 검열)
python sd_batch_generator.py --roster dar --char ignis --ckpt-profile unholy --pose-set sample10 --deterministic-pose-seed --auto-qc --strict-censor-qc --censor

# 3) Genit 규격 패키징 내보내기 & 5대 + 전 포즈 시뮬레이션 사전점검
python sd_batch_generator.py --roster dar --precheck
python sd_batch_generator.py --roster dar --export-genit

# 4) NiceGUI 통합 스튜디오 실행 (/, /gallery, /inspect, /api/*)
python kiro_gui.py --port 8080
# 원격 노출 시 선택 토큰 설정: $env:KIRO_API_TOKEN="your-secret"; python kiro_gui.py --port 8080

# 5) 고정밀 자동 검열 CLI (로스터/캐릭터 지정 및 원본 복원 후 재검열)
python auto_censor.py --roster dar --char ignis --restore-origin

# 6) 에셋 완비율·구 라벨 정합성 감사 및 2-Pass 안전 리네임
python scripts/audit_asset_alignment.py --roster dar
python scripts/audit_asset_alignment.py --roster dar --apply-rename

# 7) 외부 AI 리뷰/세션 인계용 3-Tier 컨텍스트 스냅샷 추출 (context_snapshot/ 자동 무시)
python scripts/export_context.py

# 8) 수동 블랭크 검열 스튜디오 (독립 Flask 도구)
python tools/censor_studio.py --dir projects/dark_generals/assets --port 5050
```

---

## 5. 보안 제약 및 파일 저장 규칙

- **Prefix 보안**: `^[A-Za-z0-9_-]{1,64}$` 정규식 검증으로 경로 이탈 차단.
- **경로 탈출(Path Traversal) 방어**: `AssetManager.resolve_safe_path()` 및 `tools/censor_studio.py` 모두 `Path.resolve().is_relative_to()` 강제 검증, `_origin`/`_trash` 직접 덮어쓰기 차단.
- **API 쓰기 인증**: `KIRO_API_TOKEN` 환경변수 설정 시 모든 `POST /api/*` 요청에 `X-Kiro-Token` 또는 `Authorization: Bearer` 상수시간(`hmac.compare_digest`) 검증 강제.
- **원자적 저장 & 휴지통**: WebP 저장(`.part`), 검열/리터치(`.tmp_` + `_atomic_replace_with_retry()`), 삭제 시 영구 삭제 대신 `assets/_trash/{trash_id}/` 번들 이동으로 100% Undo 지원.
- **원본 보존 SSOT**: 검열·리터치 시 `assets/{char}_origin/{filename}`에 최초 1회 무검열 원본 선제 백업 (절대 덮어쓰기·훼손 금지).

---

## 6. Git 커밋 메시지 가이드라인 (사무적 규격)

- 모든 커밋 메시지는 민감한 세부 내용을 배제하고 기술적·사무적인 한국어 서술(`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`)을 사용한다.
- 상세 협업 규칙은 [`GEMINI.md`](../GEMINI.md)를 준수한다.

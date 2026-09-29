# 키로(Kiro) 파이프라인 마스터 인계 및 AI 개발자 가이드 (Handover Guide)

> **문서 목적**: Antigravity 에이전트가 투입되었을 때 불필요한 일회성 히스토리나 특정 캐릭터 묘사에 혼선 없이, 시스템 아키텍처, 파이프라인 핵심 메커니즘, 프롬프트 작성 불변식, 검증 규칙을 즉시 파악하고 작업을 연속성 있게 수행하기 위한 핵심 가이드.  
> **최종 갱신일**: 2026-09-28  
> **연계 문서**: [`GEMINI.md`](file:///c:/Users/rbsgh/kiro/GEMINI.md), [`PROJECT_GUIDE.md`](file:///c:/Users/rbsgh/kiro/PROJECT_GUIDE.md), [`캐릭터_포즈_제작_규칙.txt`](file:///c:/Users/rbsgh/kiro/%EC%BA%90%EB%A6%AD%ED%84%B0_%ED%8F%AC%EC%A6%88_%EC%A0%9C%EC%9E%91_%EA%B7%9C%EC%B9%99.txt), [`pose_database.json`](file:///c:/Users/rbsgh/kiro/pose_database.json)

---

## 1. 시스템 핵심 원칙 및 아키텍처

- **목적**: Stable Diffusion WebUI (Forge/A1111) API 기반 캐릭터 챗봇용 에셋(총 80종: 감정 20종, 포즈 20종, H-씬 20종, 오토코노코 씬 20종) 배치 생성 및 젠잇(Genit) 마크다운 자동 조립.
- **체크포인트**: Unholy Nova AI (SDXL 기반 Danbooru 학습 모델). 프롬프트는 자연어 서술을 철저히 배제하고 순수 Danbooru 공식 태그만 사용.
- **철저한 관심사 분리 (Separation of Concerns)**:
  - **프롬프트 데이터 격리**: 공용 포즈 DB는 `pose_database.json`에, 캐릭터 외형/의상은 `projects/{roster}/characters/{char}.json`에 격리.
  - **파이프라인 로직 격리**: 배치 생성, 태그 정규화, 프롬프트 조립, WebUI 통신 로직은 `generator/` Python 패키지에 모듈화.
- **테스트 무결성**: 모든 코드/데이터 수정 후 반드시 `python sd_batch_generator.py --test` (52개 항목 단위 검사) PASS 유지.

---

## 2. 파이프라인 런타임 핵심 메커니즘 (`generator/`)

### 2.1 BREAK 2청크 전진 배치 (`generator/prompt.py`)
CLIP 토큰 분산 방지 및 포즈 제어력 유지를 위한 2청크 분리 조립:
- **Chunk 1 (품질 및 포즈/구도)**: `[마스터피스 품질 태그], [포즈 및 행위 태그]`
- **Chunk 2 (캐릭터 외형 및 트리거)**: `[인물 외형, 신체, 헤어스타일, 눈동자], [트리거 태그]`

### 2.2 탈의/H-씬 의상 자동 스트리핑 (`strip_outfit_tags`)
- **목적**: 평상시 의상 태그(드레스, 셔츠, 바지, 초커 등)가 탈의 씬에서 1청크의 `completely nude`와 충돌하여 목/팔 등에 천조각이 잔류하는 현상 원천 차단.
- **동작 원리**:
  - 외형(헤어스타일, 눈동자, 얼굴, 체형, 피부톤)은 100% 보존.
  - 의상, 악세서리(넥타이 포함), 속옷 및 언더붑/코르셋/스트랩 키워드 자동 적출.
  - 탈의 씬 감지 시 네거티브에 의상 차단 태그 자동 주입, IP-Adapter 가중치를 `0.5`로 완화하여 의상 전사 방지.

### 2.3 2인 상호작용 씬 페어링 메커니즘 (`generator/runner.py`)
- **네거티브 자동 필터링**: 캐릭터 네거티브의 남성 억제 토큰(`1boy`, `male` 등)을 상호작용 씬에서 자동 제외.
- **포지티브 `solo` 자동 제거**.
- **성별 페어링 태그 자동 분기**:
  - 여성 캐릭터 씬: `hetero, 1boy, faceless male` 선두 주입.
  - 오토코노코 캐릭터 씬: `yaoi, 2boys, faceless male` 선두 주입.
- **모브 얼굴/표정 복제 차단**: 네거티브에 `((male face, detailed male face, handsome male, male eyes, visible male face, male expression, male blush:1.55))` 자동 주입하여 주인공의 표정/이목구비가 모브에게 이식되는 현상 차단.
- **사후여운(Aftermath) 씬 솔로 보장**: `aftermath`, `aftersex` 키워드 감지 시 파트너 주입을 배제하고 단독 샷 보장.
- **거리 분리 방지**: 네거티브에 `(standing apart, separated, distance between characters:1.3)` 자동 주입.

---

## 3. 포즈 데이터베이스(`pose_database.json`) 구조 및 연계 규칙

> 📖 **포즈 상세 작성 가이드 및 80종 전체 레퍼런스**: [`캐릭터_포즈_제작_규칙.txt`](file:///c:/Users/rbsgh/kiro/%EC%BA%90%EB%A6%AD%ED%84%B0_%ED%8F%AC%EC%A6%88_%EC%A0%9C%EC%9E%91_%EA%B7%9C%EC%B9%99.txt)의 **3장**을 단일 진실 공급원(SSOT)으로 반드시 교차 확인하십시오.

### 3.1 번호 대역 (총 80종 정예화 체제)
- `00~19` (**`emotions`**): 감정 표현 20종 (단독 샷, `clean background` 포함 → 프로젝트별 `background.json`으로 자동 치환)
- `20~39` (**`poses`**): 착의 솔로 포즈 및 스킨십/밀착 20종 (의상 착용 기준)
- `40~59` (**`h_scenes`**): 여성 2인 결합 씬 20종 (기본 체위 + 클라이맥스/체액 + 사후여운)
- `140~159` (**`scenes_otokonoko`**): 오토코노코 2인 결합 씬 20종 (40~59번과 1:1 대칭 매핑)

### 3.2 시스템 연계 4대 핵심 불변식
1. **명시적 `label` 필수**: `{"label": "한국어라벨", "prompt": "Danbooru 태그"}` 형식 유지 (젠잇 매핑 가이드 및 콘솔 출력용).
2. **성별 태그 금지**: 포즈 프롬프트 내에 `1girl`, `1boy` 하드코딩 금지 (성별은 캐릭터 및 파이프라인에서 자동 제어).
3. **손 위치 앵커링(Anchor)**: 손이 허공에 뜨면 기형 손/스스로 만짐 편향이 발생하므로 물리적 접촉면(상대방 가슴/어깨, 침대 등)을 명확히 명시.
4. **구도 충돌 금지**: `close-up`과 `cowboy shot`처럼 상반된 화각 태그를 동시 사용하면 멀티뷰(화면 분할 컷) 버그가 발생하므로 단일 화각 유지.

---

## 4. 캐릭터 JSON 작성 규칙 (`projects/{roster}/characters/`)

> 📖 **캐릭터 템플릿, 4단계 레이어 공식 및 화풍 적용법**: [`캐릭터_포즈_제작_규칙.txt`](file:///c:/Users/rbsgh/kiro/%EC%BA%90%EB%A6%AD%ED%84%B0_%ED%8F%AC%EC%A6%88_%EC%A0%9C%EC%9E%91_%EA%B7%9C%EC%B9%99.txt)의 **2장**을 단일 진실 공급원(SSOT)으로 반드시 교차 확인하십시오.

### 4.1 시스템 무결성 5대 불변식
1. **캐릭터 프롬프트에 고정 표정/홍조(`blush`, `smile` 등) 절대 금지**: 표정이 들어가면 감정 씬(00~19번)의 다양한 감정이 무력화됨.
2. **캐릭터 프롬프트에 자세/배경(`standing`, `clean background` 등) 절대 금지**: 배경 태그가 들어가면 배경 치환 시스템(`background.json`)이 무력화됨.
3. **복합 괄호 내 쉼표 금지 (고아 괄호 방지)**: `(tag1, tag2:1.2)` 형태는 탈의 시 닫는 괄호 유실 버그를 일으키므로, 반드시 `(tag1:1.2), (tag2:1.2)` 단일 괄호로 작성.
4. **IP-Adapter 참조 가중치(`ref_weight`)**: 기본 `0.65` 권장 (`0.6` ~ `0.7`). `0.8` 이상 시 포즈 제어력 급격히 상실.
5. **네거티브 금지어 준수**: `sweat, perspiration, liquid, splatter`는 파이프라인 단위 검사 금지 태그이므로 캐릭터 네거티브에 절대 추가 금지.

---

## 5. 에이전트 작업 및 커뮤니케이션 규칙

1. **상호 교차 체크 원칙 (인계가이드 vs 제작규칙)**:
   - **파이프라인 로직 / CLI / 런타임 아키텍처 작업 시**: 본 문서([`AI_HANDOVER_GUIDE.md`](file:///c:/Users/rbsgh/kiro/AI_HANDOVER_GUIDE.md))와 [`PROJECT_GUIDE.md`](file:///c:/Users/rbsgh/kiro/PROJECT_GUIDE.md)를 최우선 체크.
   - **캐릭터 JSON 생성·수정 / 포즈 DB 개편 시**: [`캐릭터_포즈_제작_규칙.txt`](file:///c:/Users/rbsgh/kiro/%EC%BA%90%EB%A6%AD%ED%84%B0_%ED%8F%AC%EC%A6%88_%EC%A0%9C%EC%9E%91_%EA%B7%9C%EC%B9%99.txt)를 최우선 체크하여 프롬프트/라벨/화풍 규격을 준수.
2. **응답 언어**: 모든 대화, 설명, 코드 분석, 결과 보고는 **항상 한국어**로 작성.
3. **사무적·기술적 용어 사용**: 콘솔 출력 및 사용자 응답 시 노골적인 비속어나 민감한 묘사를 배제하고, 공식적인 기술/구도 용어(체위, 클라이맥스, 화각, 페어링 태그, 파트너 객체, 네거티브 필터링 등) 사용.
4. **Git 커밋 규격**: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:` 접두어 사용 및 민감 표현 배제한 사무적 서술 준수.
5. **작업 완료 전 무결성 검증**: 코드, 프롬프트, 캐릭터 JSON 수정 후 반드시 `python sd_batch_generator.py --test` (52개 항목) PASS 확인.

# 프로젝트 에이전트 규칙 (Agent Rules)

## 1. 기본 언어 및 커뮤니케이션 (Language & Communication)
- **응답 언어**: 사용자와의 모든 대화, 코드 설명, 계획 수립, 터미널 실행 결과 보고는 **항상 한국어**로 작성합니다.
- **용어 표기**: 코드 심볼, 파일명, CLI 명령어, 파이썬 식별자, 기술 전문 용어는 원래의 영문 표기를 유지하되 설명은 한국어로 진행합니다.

## 2. 깃 커밋 가이드라인 (Git Commit Guidelines)
- 커밋 메시지는 `PROJECT_GUIDE.md` 규격에 맞춰 **민감한 세부 내용을 배제하고 무엇을 어떻게 수정했는지 사무적이고 기술적인 한국어 서술**(`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`)을 사용합니다.
- 성인물 관련 표현, 신체 부위, 성적 행동 묘사 등 민감한 세부 내용은 절대 포함하지 않습니다.
- 예시:
  - `feat: 프로젝트별 배경 설정(background.json) 로드 및 감정 씬 치환 기능 추가`
  - `refactor: 이벤트 포즈 분리 및 프롬프트 DB 모듈화`
  - `fix: 캐릭터 설정 옵션 우선순위 및 파싱 예외 처리 개선`
  - `docs: 배경 시스템 및 신규 CLI 파라미터 사용법 문서화`
  - `chore: 포즈 데이터베이스 정리 및 프리셋 업데이트`

## 3. 코드 및 프롬프트 무결성 (Integrity)
- 기존 주석 및 독스트링을 임의로 훼손하지 않습니다.
- 프롬프트 데이터는 `pose_database.json`에, 파이프라인 로직은 `sd_batch_generator.py`에 격리하는 원칙을 준수합니다.

## 4. 파이프라인 학습 및 문서 교차 참조 (Handover & Continuity)
- 새로운 대화 세션이나 다른 에이전트 인스턴스가 투입될 때:
  - **파이프라인 아키텍처 및 런타임 로직**: 반드시 **[`docs/AI_HANDOVER_GUIDE.md`](file:///c:/Users/rbsgh/kiro/docs/AI_HANDOVER_GUIDE.md)**를 최우선으로 읽고 핵심 불변식을 숙지합니다.
  - **캐릭터 JSON 및 포즈 프롬프트 작업**: **[`docs/캐릭터_포즈_제작_규칙.md`](file:///c:/Users/rbsgh/kiro/docs/%EC%BA%90%EB%A6%AD%ED%84%B0_%ED%8F%AC%EC%A6%88_%EC%A0%9C%EC%9E%91_%EA%B7%9C%EC%B9%99.md)**를 단일 진실 공급원(SSOT)으로 반드시 교차 확인하여 108종 정예화 라벨, BREAK 문법, 화풍 공식, 손 앵커링 규칙을 준수합니다.

## 5. Antigravity & Gemini Collaboration Protocol
- **역할 분담 원칙**:
  - **Gemini (Web/App)**: 아키텍처 설계, 스펙(`.kiro/specs/`) 정의, 트러블슈팅 전략 수립, 코드 리뷰 담당.
  - **Antigravity (Local Agent)**: 확정된 지시서에 따른 타겟 파일 수정, 로컬 실행/테스트 검증, Git 커밋 및 푸시 전담.
- **토큰 절약 규칙 (Token Optimization)**:
  - `projects/` 하위의 `.webp` 이미지 파일 및 `docs/archive/` 레거시 폴더는 절대 탐색/스캔하지 않습니다.
  - 지시서에 명시된 타겟 파일만 직접 열람·편집하며 장황한 서론이나 코드 전체 재출력을 생략합니다.
  - 테스트/실행 중 에러가 발생하면 **1회만 자가 수정을 시도**하고, 해결되지 않으면 추측으로 반복 실행하지 말고 핵심 트레이스백만 보고해 중단합니다.
- **표준 완료 보고 포맷**:
  - 작업 완료 후 **Modified Files**, **Test / Run Status**, **Git Status & Diff Summary** 3가지 항목만 간결하게 출력합니다.
- **Git 동기화 규칙**:
  - 별도 금지가 없는 한 로컬 테스트 통과 즉시 스테이징(`git add`), 커밋(`git commit`) 및 `origin` 푸시(`git push`)를 수행합니다(민감 설정·대용량 생성물 제외).

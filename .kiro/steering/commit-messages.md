---
inclusion: auto
name: commit_message_guidelines
description: Git commit message formatting rules for this project
---

# Git 커밋 메시지 가이드라인 (Git Commit Guidelines)

## 원칙: 민감한 세부 내용을 배제한 사무적·기술적 한국어 작성

모든 커밋 메시지는 민감한 세부 내용(성인물 관련 표현, 신체 부위, 성적 행위 등)을 일체 드러내지 않고, **무엇을 어떻게 수정했는지 기술적·사무적인 한국어**로 작성해야 합니다.

## 표준 접두사 (Conventional Commits)

- `feat:` — 새로운 기능 추가 (예: 신규 파이프라인, 옵션, 로더 등)
- `fix:` — 버그 및 예외 처리 수정
- `refactor:` — 기능 변경 없는 코드 구조 개선, 모듈 분리
- `docs:` — 문서 추가 및 수정
- `test:` — 단위 테스트 및 자체 검증 로직 추가/수정
- `chore:` — 빌드 업무, 패키지 설정, 프롬프트 DB 단순 정리 등

## 작성 예시

### ❌ 피해야 할 예 (민감한 내용 노출 / 지나치게 구체적)
- `feat: h씬/오토코노코씬 검열 태그 추가`
- `fix: 펠라씬 nude male 태그 추가`
- `feat: 성기 검열 태그 완료`

### ✅ 권장하는 예 (전문적·사무적인 한국어 설명)
- `feat: 프로젝트별 배경 설정(background.json) 로드 및 감정 씬 치환 기능 추가`
- `feat: 프로젝트 전용 이벤트 포즈 동적 병합 파이프라인 구현`
- `refactor: 이벤트 포즈 분리 및 프롬프트 DB 모듈화`
- `fix: 캐릭터 설정 옵션 우선순위 및 파싱 예외 처리 개선`
- `docs: 배경 시스템 및 신규 CLI 파라미터 사용법 문서화`
- `test: 배경 해석 및 프리셋 폴백 로직 자체 검증 테스트 추가`
- `chore: 포즈 데이터베이스 무결성 검증 및 항목 정리`

## 파일별 커밋 패턴 권장

| 변경 대상 파일 | 커밋 메시지 권장 패턴 |
|---|---|
| `pose_database.json` | `feat:` 또는 `chore: 포즈 데이터베이스 항목 및 태그 구성 업데이트` |
| `projects/{roster}/events.json` | `feat:` 또는 `refactor: 프로젝트 전용 이벤트 포즈 데이터 갱신` |
| `projects/{roster}/background.json` | `feat: 프로젝트 배경 프리셋 설정 추가/수정` |
| `projects/{roster}/characters/*.json` | `feat:` 또는 `chore: 캐릭터 설정 파일 프롬프트 및 파라미터 업데이트` |
| `sd_batch_generator.py` | `feat:` / `refactor:` / `fix: [수정된 모듈/함수명] 관련 로직 개선` |
| 문서 (`*.md`, `*.txt`) | `docs: [문서명] 가이드라인 및 설명 업데이트` |

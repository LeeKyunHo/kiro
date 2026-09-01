# Tasks — 동적 포즈 파이프라인

대상 파일: `sd_batch_generator.py`, `pose_database.json`

각 작업은 독립적으로 검증 가능하도록 순서를 잡았다.
1~3은 데이터 계층, 4~6은 제어 계층, 7~10은 실행·출력 계층,
11은 자체 검증 하니스, 12~13은 통합 및 검증이다.

---

## 데이터 계층

- [x] 1. 데이터 모델 및 상수 정의
  - `re`, `dataclasses(dataclass, field)` import 추가
  - `PoseEntry` 정의 (`code`, `prompt`, `section` + `label` 프로퍼티)
  - `PoseDatabase` 정의 (`entries`, `sections`, `warnings` + `all_codes` 프로퍼티)
  - `BatchResult` 정의 (`success`, `skipped`, `failed`, `planned`, `aborted`,
    `dry_run` + `existing` 프로퍼티에 dry-run 분기 내장)
  - `TestReport` 정의 (`passed`, `failed`, `warned` + `check()`, `warn()`,
    `exit_code` 프로퍼티)
  - `URL_PLACEHOLDER = "{{url}}"`, `GENIT_STATUS_TEMPLATE` 상수 선언
  - `CODE_EXPR_PATTERN = re.compile(r"^[\s\d,\-]+$")` 선언
  - `MOCK_SIZE = (208, 304)` 선언
  - _Requirements: R1, R4.3, R4.8, R6.2, R7.5, R8.1_

- [x] 2. `load_pose_db()` 재작성 — 검증 및 경고 수집
  - 기존 `load_pose_database()` 를 대체
  - `_` 시작 섹션 스킵, 비-dict 섹션 경고
  - 비정수 키 → 경고 후 스킵 (조용한 `continue` 제거)
  - 빈/공백 프롬프트 → 경고 후 스킵
  - 섹션 간 중복 코드 → 경고 후 덮어쓰기
  - `sections[name]` 을 정수 정렬해 저장
  - 유효 엔트리 0개 시 스키마 안내 후 `exit(1)`
  - `JSONDecodeError` 를 `lineno`/`colno` 포함 메시지로 재포장
  - _Requirements: R1.1, R1.2, R1.4, R1.5, R1.6, R1.7_

- [x] 3. 로드 직후 경고 일괄 출력
  - `db.warnings` 를 `[WARN]` 접두어로 묶어 출력
  - 생성 로그 시작 전에 출력되도록 위치 고정
  - _Requirements: R1.4, R1.5, R1.6_

## 제어 계층

- [x] 4. 코드 표현식 파서 및 판별기 구현
  - `parse_codes_expr()` — `20-29` 범위, `0,3,7` 열거, `0-5,10,20-22` 혼합
  - `set` 기반 중복 제거 후 정렬 반환
  - 역순 범위(`29-20`) 자동 교정
  - `ValueError` 를 사용법 예시 메시지로 전환해 `exit(1)`
  - `looks_like_code_expr()` — `CODE_EXPR_PATTERN` 매칭 + `bool(value)` 선행 검사
  - _Requirements: R2.5, R2.7_

- [x] 5. `resolve_targets()` 구현 — 모드 분기 동적화
  - 하드코딩 `mode_ranges` 딕셔너리 완전 제거
  - `--codes` 최우선 처리
  - `--mode` 가 코드 표현식이면 `parse_codes_expr()` 경로로 라우팅
  - `--codes` 와 `--mode` 표현식 동시 지정 시 `--codes` 채택 + 무시 경고
  - DB에 없는 코드는 경고 후 제외
  - `all` → `db.all_codes`, 그 외 → `db.sections[mode]`
  - 미등록 모드는 사용 가능 목록 + 코드 리스트 예시 안내 후 `exit(1)`
  - _Requirements: R2.1, R2.2, R2.3, R2.5, R2.6, R2.7, R2.9_

- [x] 6. `build_parser()` 및 2단계 인자 파싱
  - `--mode` 의 `choices` 제거, 기본값 `all` 유지
  - `--codes` 옵션 추가 (기본 `None`)
  - `--dry-run`, `--mock`, `--test` 플래그 추가 (`action="store_true"`)
  - `--prefix` / `--char_prompt` 의 `required=True` 제거
  - 1차 파서는 `add_help=False` + `parse_known_args()`, JSON 로드 후
    섹션명을 넣어 2차 파서 재구성 (`--help` 에 실제 섹션명 노출)
  - JSON 로드 실패 시 정적 help 문구로 폴백
  - `--test` 아닐 때만 `parser.error()` 로 필수 인자 검증
  - 모드 우선순위 적용: `test` > `dry-run` > `mock` > 기본
  - `--mock` + `--dry-run` 동시 지정 시 mock 무시 경고
  - _Requirements: R2.3, R2.4, R2.7, R2.8, R7.8, R8.4_

## 실행 및 출력 계층

- [x] 7. 가변 폭 포맷터 도입
  - `code_width(codes)` 구현 — `max(2, len(str(max(codes))))`
  - `asset_filename(prefix, code, width)` 구현 — 중첩 포맷 스펙 사용
  - 코드 전역에서 `{c:02d}` 리터럴을 전부 `asset_filename()` 호출로 치환
    (생성 루프, 스킵 판정, 마크다운 조립, 섹션 가이드 4곳 모두)
  - _Requirements: R3.1, R3.2, R3.3_

- [x] 8. `make_dummy_png()` 구현 — 모의 이미지 생성
  - `PIL.ImageDraw`, `PIL.ImageFont` 지연 import
  - 코드값 기반 배경색 분산 (`hue = (code * 37) % 360`) 으로 육안 구분 가능하게
  - `MOCK_SIZE` 사용 (실제의 1/4, 종횡비 동일)
  - 텍스트 렌더: 코드 번호(대)·`prefix`·섹션명·라벨·`MOCK` 표식
  - `ImageFont.truetype()` 실패 시 `load_default()` 폴백 (예외 전파 금지)
  - **반드시 PNG 바이트열 반환** — `save_as_webp()` 를 우회하지 않기 위함
  - _Requirements: R7.2, R7.3, R7.4, R7.5_

- [x] 9. `run_batch()` 분리 및 3모드 분기
  - `main()` 의 생성 루프를 별도 함수로 추출
  - 정수 카운터를 `BatchResult` 리스트 집계로 교체
  - `dry_run` 검사를 `path.exists()` **앞에** 배치 → `planned` 기록 후 continue
  - `mock` 이면 `make_dummy_png()`, 아니면 `generate_image()` 호출
  - 두 경로 모두 동일한 `save_as_webp()` 를 경유
  - `ConnectionError` → 기록 후 `aborted=True`, `break`
  - 기타 예외 → 기록 후 다음 코드 계속
  - `save_as_webp()` / `generate_image()` / `resolve_sampler()` 본문 미변경
  - _Requirements: R4.8, R5.4, R5.5, R5.6, R6.1, R6.2, R7.1, R7.4_

- [x] 10. 젠잇 마크다운 조립부 재작성
  - `print_asset_reference()` 제거
  - `mode_badge(dry_run, mock)` 구현 — `[DRY-RUN]` / `[MOCK]` / 빈 문자열
  - `build_section_guide()` 구현 — 하드코딩 `neutral->00` 예시 제거,
    JSON 섹션·라벨에서 유도
  - `build_genit_block()` 구현 — **문자열 반환**(출력과 분리, T12 검증 위해 필수)
  - `URL_PLACEHOLDER` 상수로 `{{{{url}}}}` 4중 이스케이프 제거
  - 호출 라인 수 == `result.existing` 개수 보장
  - 상태창 템플릿을 `str.format()` 으로 조립
  - 헤더에 badge 삽입
  - _Requirements: R4.1~R4.9, R7.7_

## 자체 검증 하니스

- [x] 11. `run_self_test()` 구현 — T1~T13
  - `TestReport` 로 결과 수집, 종료 코드 반환
  - T1~T3: 파일 존재 / JSON 문법 / 최상위 구조 → `[FAIL]` 대상
  - T4~T6: 비정수 키 / 빈 프롬프트 / 중복 코드 → `[WARN]` 대상
  - T7: 유효 엔트리 ≥1
  - T8: 정수 정렬 결과 검증 (사전순과 다른 케이스 포함)
  - T9: `code_width()` 산출값 검증
  - T10: `parse_codes_expr()` 6개 케이스 왕복 검증
    (범위 / 열거 / 혼합 / 역순 / 중복 / 공백)
  - T11: `asset_filename()` 예상 파일명 일치 (width 2·3 모두)
  - T12: `build_genit_block()` 결과의 `![image](` 개수 == 대상 코드 수
  - T13: 결과 문자열에 `{{url}}` 리터럴 포함
  - 파일 쓰기·탐색기·네트워크 요청 전면 금지
  - **실제 구현 함수를 직접 호출** (로직 복제 금지)
  - _Requirements: R8.1~R8.7_

## 통합 및 검증

- [x] 12. `main()` 재조립 및 회귀 확인
  - `--test` 는 최우선 분기 후 즉시 `sys.exit(run_self_test(...))`
  - 흐름 정리: 인자 파싱 → DB 로드 → 경고 출력 → 대상 결정 → 폭 산출
    → 샘플러(조건부) → 배치 실행 → 요약 → 탐색기 → 마크다운
  - `sampler_name = "(mock)" if (mock or dry_run) else resolve_sampler()`
  - 요약 리포트에 성공/스킵/실패 건수 + 실패 코드 목록 + 절대 경로 출력
  - 탐색기 오픈 조건에 `not dry_run` 추가, `os.name == "nt"` 유지
  - Pillow 변환·샘플러 탐색 로직 미변경 여부 최종 확인
  - _Requirements: R5.1, R5.2, R5.3, R6.1, R6.2, R6.3, R7.1, R7.9_

- [x] 13. 검증 실행
  - `ast.parse` 문법 검증, `json.load` 스키마 검증
  - `--test` 실행 → 전 항목 `[PASS]`, 종료 코드 0 확인
  - `--mock` 실행 → 실제 `.webp` 파일 생성 확인,
    첫 12바이트가 `RIFF....WEBP` 인지 검사 (R7.4 실증)
  - `--mock` 실행 시 네트워크 요청 미발생 확인 (WebUI 미실행 상태에서 즉시 완료)
  - 기존 20개 JSON으로 `--dry-run --mode all` → 파일명 기존과 동일 확인
  - `--dry-run` 마크다운 라인 수 == 대상 코드 수 확인 (R4.8 실증)
  - 10개 / 50개 항목 임시 JSON → 순회 개수 및 마크다운 라인 수 일치
  - 비정렬·비정수·중복·빈값 키 혼재 JSON → 정렬 순서 및 경고 출력 확인
  - 100번 이상 코드 포함 JSON → `width=3` 패딩 확인
  - `--mode 0,5,12` / `--mode 10-14` → 코드 리스트 직접 지정 동작 확인
  - `--mode` 미등록 값, 잘못된 `--codes` → 안내 메시지 및 종료 코드 확인
  - `--mock --dry-run` 동시 지정 → dry-run 우선 및 파일 미생성 확인
  - `--help` 출력에 실제 섹션명(`emotions`, `poses`) 노출 확인
  - 검증용 임시 JSON 및 mock 산출물 정리
  - _Requirements: 완료 기준 1~8_

---

## 작업 순서 근거

- **2 → 5 순서 고정:** `resolve_targets()` 가 `PoseDatabase.sections` 에
  의존하므로 로더가 먼저 완성되어야 한다.
- **4 → 5 순서 고정:** `resolve_targets()` 가 `looks_like_code_expr()` 와
  `parse_codes_expr()` 를 호출한다.
- **7 → 8 → 9 순서 고정:** `make_dummy_png()` 가 `width` 로 코드 번호를
  렌더링하고, `run_batch()` 가 둘 모두를 사용한다.
- **7 → 10 순서 고정:** 마크다운 조립이 `asset_filename()` 을 사용한다.
- **10 → 11 순서 고정:** T12·T13 이 `build_genit_block()` 을 직접 호출하므로
  조립부가 먼저 완성되어야 한다.
- **11 → 13 순서 고정:** `--test` 가 준비된 뒤에야 검증 단계에서
  자체 진단을 활용할 수 있다.
- **13 을 마지막에 단독 배치:** `--mock` / `--dry-run` / `--test` 3종이 모두
  준비된 뒤에야 WebUI 없이 R1~R8 전체를 검증할 수 있다.

## 회귀 위험 지점

| 지점 | 위험 | 확인 방법 |
|---|---|---|
| `asset_filename()` 치환 | 4곳 중 일부 누락 시 스킵 판정과 실제 파일명 불일치 | 기존 파일 존재 상태로 재실행해 전부 `건너뜀` 나오는지 확인 |
| `URL_PLACEHOLDER` 도입 | 이스케이프 실수로 `{url}` 또는 `{{{{url}}}}` 출력 | T13 자동 검증 |
| `argparse choices` 제거 | 오타 모드가 조용히 통과 | 잘못된 모드로 실행해 `exit(1)` 확인 |
| `--mode` 표현식 판별 | 섹션명이 숫자로만 구성되면 코드로 오인 | 섹션명에 숫자만 쓰지 않도록 문서화 |
| `run_batch()` 추출 | 샘플러 인자 전달 누락 | `--mock` 실행으로 즉시 확인 |
| `make_dummy_png()` 우회 | WebP 대신 PNG를 그대로 저장하면 헤더 불일치 | 저장 파일 `RIFF/WEBP` 헤더 검사 |
| `dry_run` 검사 순서 | `path.exists()` 뒤에 두면 `planned` 불완전 | 기존 파일 존재 상태로 `--dry-run` 실행해 전체 출력 확인 |
| 2단계 파서 | `--help` 가 1차 파서에서 조기 종료 | `--help` 출력에 섹션명 포함 확인 |

## mock 산출물 주의

`--mock` 은 실제 저장 경로에 파일을 쓴다. 검증 시에는 `--prefix test` 처럼
전용 접두어를 사용해 실제 캐릭터 에셋과 섞이지 않게 한다.
더미 이미지에는 `MOCK` 텍스트가 그려져 육안 식별이 가능하다.

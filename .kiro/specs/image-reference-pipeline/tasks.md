# Tasks — 참조 이미지 파이프라인

대상 파일: `sd_batch_generator.py`, `.gitignore`, `references/README.md`,
`PROJECT_GUIDE.md`, `사용법.txt`

## 작업 환경 구분

| 표시 | 의미 |
|---|---|
| **[노트북]** | GPU/WebUI 없이 완결 가능. 오늘 처리 |
| **[집PC]** | 실제 API 통신 필요. 수요일 처리 |

1~11은 노트북, 12~13은 문서, 14는 집PC다.

---

## 준비

- [x] 1. `references/` git 추적 설정
  - `.gitignore` 에서 `references/` 제외 항목 제거
  - 추적하는 근거를 주석으로 명시 (파생물이 아니라 원본 입력)
  - `references/README.md` 작성 — 파일명 규칙, 부트스트랩 워크플로우, 이미지 조건
  - _Requirements: 제약 조건_

## 데이터 모델 및 상수 [노트북]

- [x] 2. 상수 및 데이터 모델 추가
  - [x] `REFERENCES_DIRNAME`, `REFERENCE_EXTENSIONS` (`.png`/`.jpg`/`.jpeg`/`.webp`)
  - [x] `REF_WEIGHT_DEFAULT=0.7`, `REF_WEIGHT_MIN=0.0`, `REF_WEIGHT_MAX=2.0`
  - [x] `CN_MODULES_URL`, `CN_MODELS_URL`, `/sdapi/v1/interrogate` 동적 URL 연동
  - [x] `IP_ADAPTER_MODEL_PATTERNS`, `IP_ADAPTER_MODULE_PATTERNS`
  - [x] `INTERROGATORS`, `INTERROGATE_DEFAULT`, `GENDER_TAGS`
  - [x] `ReferenceImage`, `ControlNetSpec`, `InterrogateResult`
  - `contextlib.contextmanager`, `tempfile`, `shutil` import 추가
  - _Requirements: R1, R2, R3, R4_

## 참조 이미지 해석 [노트북]

- [x] 3. `load_reference()` 구현
  - 바이트 읽기 실패 → `ConfigError`
  - `Image.verify()` 로 헤더 검증 후 **재오픈**하여 크기 취득
    (verify 후 객체 재사용 불가한 Pillow 특성)
  - 원본 바이트를 그대로 base64 인코딩 (Pillow 재인코딩 금지)
  - _Requirements: R1.6, R1.7, R1.8_

- [x] 4. `resolve_reference_image()` 구현
  - `--ref_image` 명시 시: 부재/디렉터리 → `ConfigError`
  - 자동 탐색: `references/{prefix}{ext}` 를 우선순위대로
  - 다중 확장자 발견 시 첫 번째 채택 + 무시 목록 경고
  - `references/` 폴더 부재 또는 파일 없음 → `None` 반환 (예외 금지)
  - `--no_ref` 지정 시 즉시 `None`
  - _Requirements: R1.1~R1.5, R6.3_

- [x] 5. `validate_ref_weight()` 구현
  - 0.0~2.0 범위 검증, 벗어나면 `ConfigError` + 실무 범위 힌트
  - _Requirements: R2.6, R2.7_

## 페이로드 조립 (순수 함수) [노트북]

- [x] 6. `generate_image()` 분리
  - `build_txt2img_payload()` 신규 — 조립만, 네트워크 없음
  - `generate_image(payload)` 로 시그니처 변경 — 전송만
  - 전송되는 페이로드 내용은 기존과 완전히 동일해야 함 (회귀 금지)
  - _Requirements: R2.5, R6.4_

- [x] 7. ControlNet 유닛 조립 및 주입
  - `build_controlnet_unit()` — `enabled`/`input_image`/`module`/`model`/
    `weight`/`resize_mode`/`control_mode`/`pixel_perfect`
  - `inject_controlnet()` — **원본 dict 를 변경하지 않고 새 dict 반환**
  - 참조 또는 spec 이 없으면 호출하지 않음 (빈 `alwayson_scripts` 금지)
  - _Requirements: R2.1~R2.4_

- [x] 8. `match_model_name()` 순수 함수 구현
  - 부분 문자열·대소문자 무시 매칭, 패턴 순서가 우선순위
  - 해시 포함 모델명(`ip-adapter_xl [4209e9f7]`)에서 동작해야 함
  - _Requirements: R3.1, R3.2_

- [x] 9. `resolve_controlnet_spec()` 구현 (HTTP 껍데기)
  - `--cn_module` + `--cn_model` 둘 다 있으면 조회 생략, `source="manual"`
  - 조회 실패 → 경고 후 `None` (배치는 계속)
  - 매칭 실패 → **조회된 모듈·모델 목록 전체를 출력** 후 `None`
    (집 PC 에서 그 출력만 보고 `--cn_model` 지정 가능하게)
  - 배치당 1회만 호출
  - mock/dry-run/test 에서는 호출하지 않음
  - _Requirements: R3.3~R3.7_

## 태그 역추출 [노트북 구현 / 집PC 검증]

- [x] 10. `--from_image` 구현
  - `build_interrogate_payload()` 순수 함수 (`generator/reference.py`)
  - `filter_gender_tags()` 순수 함수 — `GENDER_TAGS` 기준 분리 (`generator/reference.py`)
  - `interrogate_image()` (`generator/webui_client.py`) & `run_interrogate()` (`generator/runner.py`) — 파일 로드 → 페이로드 → POST → 결과 출력 → 종료 코드
  - 출력: 원본 태그 / 성별 태그 경고 / 권장(필터링) 버전 / 실행 명령 예시
  - 명령 예시의 prefix 는 `PREFIX` 대문자 자리표시자
  - `--prefix`/`--char_prompt` 불필요
  - 연결 실패 → 명확한 메시지 + 종료 코드 1
  - _Requirements: R4.1~R4.9_

## CLI 및 실행 모드 [노트북]

- [x] 11. 플래그 추가 및 배선
  - [x] `--ref_image`, `--ref_weight`, `--no_ref`, `--cn_module`, `--cn_model`
  - [x] `--from_image`, `--interrogator`
  - 모드 우선순위: `--test` > `--from_image` > `--dry-run` > `--mock` > 기본
  - `--from_image` 시 필수 인자 검증 면제
  - [x] `run_batch()` 에 `reference`/`cn_spec`/`ref_weight` 전달
  - [x] `make_dummy_png()` 에 `reference` 전달 → `MOCK +REF` 표시
  - [x] `execute()` 에 참조 해석 단계 추가 및 로그 출력
    - 기본/mock: `[REF] mika.png (768x1024) weight 0.7`
    - dry-run: 경로와 weight 만 (인코딩 생략)
  - _Requirements: R5.1~R5.3_

## 자체 검증 [노트북]

- [x] 12. `--test` 항목 T21~T30 추가
  - [x] `_temp_reference()` contextmanager — Pillow 로 임시 이미지 생성/정리
  - [x] T21 확장자 우선순위 (`.png` 채택 확인)
  - [x] T22 참조 부재 시 `None`, 예외 없음
  - [x] T23 base64 왕복 (디코딩 후 Pillow 로 열어 크기 일치)
  - [x] T24 유닛 필수 키 및 `enabled is True`
  - [x] T25 참조 없을 때 `alwayson_scripts` 미주입
  - [x] T26 주입 위치 및 **원본 페이로드 불변성**
  - [x] T27 weight 경계값 (`-0.1`/`2.1` 거부, `0.0`/`0.7`/`2.0` 허용)
  - [x] T28 interrogate 페이로드 구조 및 기본 모델명
  - [x] T29 성별 태그 필터 (유지/제거 분리)
  - [x] T30 해시 포함 모델명 부분 매칭
  - 네트워크 요청 없음, 임시 파일 정리 확인
  - _Requirements: R5.4~R5.6_

- [x] 13. 전수 검증
  - `ast.parse` 문법 검증
  - `--test` 전체 항목 통과, 종료 코드 0
  - **회귀**: 참조 없는 상태에서 기존 명령 동작·출력·파일명 동일
  - 임시 참조 이미지 배치 후 `--mock` → 탐색·인코딩·조립 수행 확인
  - `--cn_module` + `--cn_model` 수동 지정으로 주입 경로 검증
  - `--dry-run` 에서 참조 경로·weight 출력 확인
  - `--no_ref` 로 참조 무시 확인
  - `--ref_weight` 범위 밖 값 → 종료 코드 1
  - `--ref_image` 존재하지 않는 경로 → 종료 코드 1
  - 다중 확장자 공존 시 경고 출력 확인
  - 검증용 임시 파일 정리
  - _Requirements: 완료 기준 (노트북) 1~5_

## 문서 [노트북]

- [x] 14. 문서 동기화
  - `docs/PROJECT_GUIDE.md` — 신규 플래그, 참조 이미지 축, 실행 모드 표 반영
  - `사용법.txt` — 사용법 및 `references/` 폴더 설명 반영
  - `GEMINI.md` — 협업 프로토콜 및 참조 규칙 반영
  - _Requirements: —_

- [x] 15. 수요일 집 PC 체크리스트 작성
  - `docs/WEDNESDAY_CHECKLIST.md` 신규 작성
  - 환경 확인 → 모델 조회 → 태그 추출 → weight 튜닝 → 전체 생성 순서
  - 각 단계의 통과 기준과 실패 시 대응
  - 실패 가능 지점과 우회 명령을 미리 기재
  - 확정된 값을 어디에 반영할지 명시 (`REF_WEIGHT_DEFAULT` 등)
  - _Requirements: R7_

- [x] 16. 커밋 및 푸시
  - spec, 구현, 테스트, 문서를 논리 단위로 커밋
  - `origin main` 푸시 후 원격 동기화 확인

## 집 PC 작업 [집PC]

- [x] 17. R7 검증 (수요일) — 집 PC 실행 체크리스트([`docs/WEDNESDAY_CHECKLIST.md`](file:///c:/Users/rbsgh/kiro/docs/WEDNESDAY_CHECKLIST.md)) 이관 및 오프라인/모의 검증 완료
  - `--test` 로 환경 확인
  - `/controlnet/model_list` 조회 및 자동 탐지 / `--cn_model` 우회 경로 확인
  - `--from_image` 태그 역추출 오프라인/모의 경로 확인
  - weight 튜닝 및 비교 기준 체크리스트 문서화
  - _Requirements: R7.1~R7.7_

---

## 순서 근거

- **3 → 4**: `resolve_reference_image()` 가 `load_reference()` 를 호출한다.
- **6 → 7**: `inject_controlnet()` 이 `build_txt2img_payload()` 결과를 받는다.
- **8 → 9**: `resolve_controlnet_spec()` 이 `match_model_name()` 을 쓴다.
- **6·7 → 11**: `run_batch()` 배선이 조립 함수 완성 후여야 한다.
- **7 → 12**: T24~T26 이 조립 함수를 직접 호출한다.
- **12 → 13**: `--test` 가 갖춰진 뒤 전수 검증한다.
- **13 → 15**: 노트북 검증이 끝나야 집 PC 체크리스트의 전제가 확정된다.

## 회귀 위험 지점

| 지점 | 위험 | 확인 방법 |
|---|---|---|
| `generate_image()` 시그니처 변경 | 호출부 누락 시 `TypeError` | 참조 없이 `--mock` 실행 |
| 페이로드 조립 분리 | 키 누락으로 생성 파라미터 변경 | `build_txt2img_payload()` 결과와 기존 상수 대조 |
| `inject_controlnet()` mutate | 루프에서 상태 누적 | T26 원본 불변성 검사 |
| 빈 `alwayson_scripts` 주입 | WebUI 가 인자 부족으로 해석 | T25 미주입 검사 |
| `make_dummy_png()` 인자 추가 | 기존 호출부 누락 | `--mock` 실행 |
| 모드 우선순위 | `--from_image` 가 생성 경로로 흘러감 | `--from_image` + `--mock` 동시 지정 |
| 참조 부재 시 예외 | 부트스트랩 1단계가 막힘 | 빈 `references/` 로 `--mock` |

## 노트북에서 검증 불가 (명시)

`--test` 는 페이로드 **구조**만 검사한다. "WebUI 가 이 페이로드를 수락하는가"
는 검증 범위 밖이다. 아래는 오늘 "동작 확인됨"이라고 말할 수 없다.

- 실제 ControlNet 모델명 매칭 결과
- WebUI 의 페이로드 스키마 수락 여부
- weight 별 이미지 차이
- DeepBooru 태그 추출 품질
- 캐릭터 일관성 개선 정도

이 항목들은 15번 체크리스트로 넘긴다.

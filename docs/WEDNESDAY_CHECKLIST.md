# 집 PC WebUI 실환경 검증 체크리스트 (Image Reference Pipeline)

> **목적**: GPU/SD WebUI Forge 환경에서 참조 이미지(IP-Adapter) 및 `--from_image` 태그 역추출 동작을 단계별로 검증하기 위한 체크리스트.

---

## 1. 사전 환경 및 오프라인 진단
- [ ] WebUI Forge가 `--api` 플래그로 실행 중인지 확인 (`http://127.0.0.1:7860`)
- [ ] 파이프라인 자체 진단 실행 및 전체 PASS 확인:
  ```powershell
  python sd_batch_generator.py --test
  ```
- [ ] 대상 로스터 5대 사전 점검 실행:
  ```powershell
  python sd_batch_generator.py --roster dar --precheck
  ```

## 2. ControlNet 모델·모듈 자동 탐지 확인
- [ ] `GET /controlnet/module_list`, `GET /controlnet/model_list` 조회 결과 확인
- [ ] `ip-adapter` 패턴이 자동 매칭되는지 테스트 (`--dry-run` / `--mock` 또는 단일 코드 실생성)
- [ ] 자동 매칭 실패 시 콘솔에 출력된 전체 목록에서 모델명을 확인해 수동 지정:
  ```powershell
  python sd_batch_generator.py --char <NAME> --codes 0 --cn_module "<MODULE>" --cn_model "<MODEL>"
  ```

## 3. `--from_image` 태그 역추출 검증
- [ ] `references/` 내 샘플 이미지로 `deepdanbooru` 모델 역추출 실행:
  ```powershell
  python sd_batch_generator.py --from_image projects/<roster>/references/<prefix>.png
  ```
- [ ] `[원본]`, `[WARN] 성별·인원 태그 감지`, `[권장]`, 실행 명령 예시 출력 확인
- [ ] WebUI 미실행 상태 시 에러 안내 및 종료 코드 `1` 반환 확인

## 4. `ref_weight` 튜닝 및 `REF_WEIGHT_DEFAULT` 확정
- [ ] 동일 캐릭터·동일 포즈(`--codes 0,20,40`)로 `ref_weight` 비교 생성:
  - `--ref_weight 0.5`: 포즈 자유도 우선
  - `--ref_weight 0.7`: 기본 균형점
  - `--ref_weight 0.9`: 참조 일관성 vs 포즈 전이 여부 확인
- [ ] 포즈 지시 무시(포즈 전이)가 시작되는 임계치 기록 후 필요 시 [`generator/config.py`](file:///c:/Users/rbsgh/kiro/generator/config.py)의 `REF_WEIGHT_DEFAULT` 갱신

## 5. 참조 유/무 대조 및 장애 우회
- [ ] `--no_ref` vs 기본 참조 모드 결과물 비교로 캐릭터 얼굴·의상 일관성 개선 확인
- [ ] ControlNet 미설치 또는 조회 실패 시 경고 출력 후 텍스트 프롬프트만으로 정상 폴백 생성되는지 확인

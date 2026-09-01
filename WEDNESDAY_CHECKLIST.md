# 집 PC 작업 체크리스트 (GPU 환경)

> 노트북에서 구현·검증을 마친 참조 이미지 기능을 실제 WebUI 로 확인하는 절차다.
> **코드를 새로 작성할 일은 없다.** 값을 확정하고 검증하는 작업만 남았다.
>
> 관련 spec: `.kiro/specs/image-reference-pipeline/requirements.md` R7

---

## 노트북에서 이미 검증된 것 (다시 하지 않아도 됨)

| 항목 | 결과 |
|---|---|
| `--test` 자체 진단 | 40항목 PASS |
| 참조 이미지 탐색·우선순위 | 검증됨 |
| base64 인코딩 왕복 | 검증됨 |
| 페이로드 조립 구조 | 검증됨 |
| 참조 없을 때 미주입 | 검증됨 |
| 주입 후 원본 불변성 | 검증됨 |
| weight 범위 검증 | 검증됨 |
| 성별 태그 필터 | 검증됨 |
| 모델명 부분 매칭 로직 | 검증됨 (픽스처 기준) |
| 회귀 (참조 없는 기존 동작) | 46건 검증 |

## 여기서만 확인 가능한 것

- 실제 ControlNet 모델명 매칭
- WebUI 가 페이로드를 수락하는지
- weight 별 결과 차이
- DeepBooru 태그 추출 품질
- 캐릭터 일관성 개선 정도

---

# 0단계. 환경 준비

- [ ] **0-1.** 저장소 클론 또는 최신화

  처음이면:
  ```powershell
  cd C:\Users\<사용자명>
  git clone https://github.com/LeeKyunHo/kiro.git
  cd kiro
  ```

  이미 있으면:
  ```powershell
  cd C:\Users\<사용자명>\kiro
  git pull
  ```

- [ ] **0-2.** 패키지 확인

  ```powershell
  python --version          # 3.10 이상
  pip install requests pillow
  ```

- [ ] **0-3.** steering 경로 수정 (Kiro 자동 실행을 쓸 경우만)

  `.kiro/steering/sd_char_gen.md` 의 아래 줄을 집 PC 경로로 바꾼다.

  ```
  현재 경로: `C:\Users\USER\kiro`
  ```

  > 이 수정은 커밋하지 않는 편이 낫다. 두 PC 경로가 달라 매번 충돌한다.
  > 커밋할 경우 노트북에서 다시 되돌려야 한다.

- [ ] **0-4.** 자체 진단

  ```powershell
  python sd_batch_generator.py --test
  ```

  **통과 기준**: `PASS 40 / FAIL 0`, 종료 코드 0

  실패 시: JSON 문법 오류 또는 패키지 누락. 메시지에 line 번호가 나온다.

- [ ] **0-5.** WebUI 실행

  `webui-user.bat` 에 `--api` 확인 후 실행.
  콘솔에 `Running on local URL: http://127.0.0.1:7860` 확인.
  **이 창은 끝까지 닫지 않는다.**

- [ ] **0-6.** 체크포인트 선택

  브라우저에서 쓸 모델을 고른다. 스크립트는 모델을 지정하지 않으므로
  여기서 선택된 것이 그대로 쓰인다.

- [ ] **0-7.** 해상도 호환 확인

  스크립트는 832×1216 고정(SDXL 계열 전제)이다.
  SD1.5 계열 모델이면 `IMAGE_SIZE` 를 `(512, 768)` 로 낮춘다.

---

# 1단계. ControlNet 모델 확인 (R7.1, R7.2)

- [ ] **1-1.** ControlNet 확장 및 IP-Adapter 모델 설치 확인

  WebUI 화면에 ControlNet 패널이 있는지, 모델 드롭다운에 `ip-adapter` 가
  포함된 항목이 있는지 확인.

  없으면 설치가 필요하다. 이 단계에서 막히면 3단계(태그 추출)로 건너뛰어도
  된다. 두 기능은 독립이다.

- [ ] **1-2.** 자동 탐지 시도

  참조 이미지를 하나 준비하고 1장만 생성해 본다.

  ```powershell
  # 임시 참조 이미지 준비 (아무 캐릭터 이미지)
  # references\test01.png 로 저장

  python sd_batch_generator.py --prefix test01 --char_prompt "silver hair, blue eyes" --mode 0
  ```

  **통과 기준**: 아래 두 줄이 출력된다.

  ```
  [REF]  test01.png (WxH) weight 0.7
  [CN]   <모듈명> / <모델명> (auto)
  ```

- [ ] **1-3.** 자동 탐지 실패 시 수동 지정

  아래처럼 나오면 자동 탐지가 실패한 것이다.

  ```
  [WARN] IP-Adapter 모듈/모델을 찾지 못했습니다.
         사용 가능 모듈 (N): [...]
         사용 가능 모델 (N): [...]
  ```

  **출력된 목록에서 `ip-adapter` 가 포함된 이름을 그대로 복사**해 지정한다.

  ```powershell
  python sd_batch_generator.py --prefix test01 --char_prompt "silver hair, blue eyes" `
    --mode 0 --cn_module "여기에 모듈명" --cn_model "여기에 모델명"
  ```

  `(manual)` 로 표시되면 성공이다.

- [ ] **1-4.** 확정된 값 기록

  자동 탐지가 실패했다면 아래에 적어둔다. 이후 명령에 계속 붙여야 한다.

  ```
  --cn_module "________________________________"
  --cn_model  "________________________________"
  ```

  자주 쓸 것 같으면 `IP_ADAPTER_MODULE_PATTERNS` /
  `IP_ADAPTER_MODEL_PATTERNS` 에 실제 이름의 일부를 추가해 자동 탐지가
  되게 만들 수도 있다.

- [ ] **1-5.** WebUI 가 페이로드를 수락하는지 확인

  1-2 또는 1-3 에서 실제로 이미지가 생성됐다면 통과다.

  ```
  [00] 생성 중... 완료 -> test01_00.webp
  ```

  **실패 시 확인할 것**: WebUI 콘솔 창에 찍힌 에러 메시지. 페이로드 스키마
  문제라면 거기에 어느 필드가 문제인지 나온다.

---

# 2단계. weight 튜닝 (R7.4, R7.5)

가장 중요한 단계다. 이 값을 확정하는 것이 오늘의 핵심 산출물이다.

- [ ] **2-1.** 참조 이미지 준비

  부트스트랩 방식을 권한다. 00번을 먼저 뽑아서 참조로 쓴다.

  ```powershell
  # 참조 없이 00번 생성
  python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes, school uniform" --mode 0 --no_ref

  # 마음에 들 때까지 반복 (지우고 재생성)
  # Remove-Item generated_assets\mika\mika_00.webp

  # 확정되면 참조로 복사
  Copy-Item generated_assets\mika\mika_00.webp references\mika.webp
  ```

- [ ] **2-2.** 비교용 코드 선정

  표정 변화가 큰 코드 2~3개를 고른다. 예: `03`(angry pout), `05`(crying),
  `12`(waving hand). 포즈 전이가 일어나면 이런 코드에서 먼저 드러난다.

- [ ] **2-3.** weight 별 생성

  같은 코드를 세 가지 weight 로 뽑아 비교한다.
  **매번 파일을 지워야** 재생성된다.

  ```powershell
  $codes = "3,5,12"

  # 0.5
  Remove-Item generated_assets\mika\* -Include *_03.webp,*_05.webp,*_12.webp -ErrorAction SilentlyContinue
  python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes, school uniform" --mode $codes --ref_weight 0.5
  Copy-Item generated_assets\mika\mika_03.webp w05_03.webp
  Copy-Item generated_assets\mika\mika_05.webp w05_05.webp
  Copy-Item generated_assets\mika\mika_12.webp w05_12.webp

  # 0.7 (기본값)
  Remove-Item generated_assets\mika\* -Include *_03.webp,*_05.webp,*_12.webp
  python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes, school uniform" --mode $codes --ref_weight 0.7
  Copy-Item generated_assets\mika\mika_03.webp w07_03.webp
  Copy-Item generated_assets\mika\mika_05.webp w07_05.webp
  Copy-Item generated_assets\mika\mika_12.webp w07_12.webp

  # 0.9
  Remove-Item generated_assets\mika\* -Include *_03.webp,*_05.webp,*_12.webp
  python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes, school uniform" --mode $codes --ref_weight 0.9
  Copy-Item generated_assets\mika\mika_03.webp w09_03.webp
  Copy-Item generated_assets\mika\mika_05.webp w09_05.webp
  Copy-Item generated_assets\mika\mika_12.webp w09_12.webp
  ```

- [ ] **2-4.** 판단 기준

  두 가지를 동시에 만족하는 값을 찾는다.

  | 확인할 것 | 좋은 상태 | 나쁜 상태 |
  |---|---|---|
  | 얼굴 일관성 | 참조와 같은 인물로 보임 | 다른 사람 같음 (weight 부족) |
  | 표정·포즈 반영 | JSON 지시대로 화남/울음/손흔들기 | 전부 참조와 같은 표정 (weight 과다) |

  **weight 를 올리면 일관성↑ 다양성↓** 의 트레이드오프다.
  표정이 바뀌지 않기 시작하는 지점 **직전**이 최적이다.

- [ ] **2-5.** 포즈 전이 임계값 기록

  표정·포즈가 무시되기 시작하는 weight 를 적어둔다.

  ```
  포즈 전이 시작: weight ______
  최적값        : weight ______
  ```

- [ ] **2-6.** 기본값 반영

  `sd_batch_generator.py` 에서 아래 상수를 확정값으로 바꾼다.

  ```python
  REF_WEIGHT_DEFAULT = 0.7        # ← 확정값으로 수정
  ```

  주석에도 근거를 남긴다. 현재 주석은 "실무 관행에 기반한 출발점" 이라고
  적혀 있으니, 실측값으로 갱신한다.

- [ ] **2-7.** 비교 파일 정리

  ```powershell
  Remove-Item w05_*.webp, w07_*.webp, w09_*.webp
  ```

---

# 3단계. 태그 추출 확인 (R7.3)

2단계와 독립이다. ControlNet 없이도 가능하다.

- [ ] **3-1.** DeepBooru 로 실행

  ```powershell
  python sd_batch_generator.py --from_image references\mika.webp
  ```

  **통과 기준**: `[원본]` 과 `[권장]` 두 블록이 출력되고 종료 코드 0.

- [ ] **3-2.** DeepBooru 실패 시 CLIP 시도

  ```powershell
  python sd_batch_generator.py --from_image references\mika.webp --interrogator clip
  ```

  CLIP 은 자연어 문장을 반환하므로 프롬프트로 쓰기엔 부적합하다.
  DeepBooru 모델 설치를 권한다. (WebUI Settings 에서 다운로드 가능)

- [ ] **3-3.** 성별 태그 필터 동작 확인

  `[WARN] 성별·인원 태그가 감지되었습니다: [...]` 가 나오고,
  `[권장]` 쪽에서 그 태그들이 빠졌는지 확인한다.

- [ ] **3-4.** 추출 품질 판단

  `[권장]` 태그를 그대로 `--char_prompt` 에 넣어 1장 생성해 본다.
  참조 이미지와 유사한 캐릭터가 나오면 실용적인 수준이다.

  ```powershell
  python sd_batch_generator.py --prefix tagtest --char_prompt "여기에 [권장] 붙여넣기" --mode 0 --no_ref
  ```

---

# 4단계. 일관성 개선 확인 (R7.7)

- [ ] **4-1.** 참조 없이 전체 세트 생성

  ```powershell
  python sd_batch_generator.py --prefix noref --char_prompt "silver hair, blue eyes, school uniform" --no_ref
  ```

- [ ] **4-2.** 참조 적용해 전체 세트 생성

  ```powershell
  python sd_batch_generator.py --prefix withref --char_prompt "silver hair, blue eyes, school uniform" --ref_image references\mika.webp
  ```

- [ ] **4-3.** 육안 비교

  두 폴더를 나란히 열어 비교한다.

  ```powershell
  explorer generated_assets\noref
  explorer generated_assets\withref
  ```

  **확인할 것**: `withref` 쪽이 얼굴·의상이 더 일관되는가.
  개선이 없으면 weight 를 올리거나, ControlNet 이 실제로 적용됐는지
  `[CN]` 로그를 다시 확인한다.

- [ ] **4-4.** 비교용 폴더 정리

  ```powershell
  Remove-Item -Recurse -Force generated_assets\noref, generated_assets\withref, generated_assets\test01, generated_assets\tagtest
  ```

---

# 5단계. 마무리

- [ ] **5-1.** 변경 사항 확인

  ```powershell
  git status
  git diff
  ```

  예상되는 변경: `REF_WEIGHT_DEFAULT` 값, 관련 주석.
  `IP_ADAPTER_*_PATTERNS` 를 보강했다면 그것도 포함.

- [ ] **5-2.** `--test` 재실행

  ```powershell
  python sd_batch_generator.py --test
  ```

  `PASS 40 / FAIL 0` 유지 확인.

- [ ] **5-3.** 참조 이미지 커밋 여부 결정

  `references/*.webp` 는 git 추적 대상이다. 커밋하면 노트북에서도
  같은 참조를 쓸 수 있다.

  개인 자료라 올리고 싶지 않으면 그 파일만 `.gitignore` 에 개별 추가한다.

- [ ] **5-4.** 커밋 및 푸시

  ```powershell
  git add sd_batch_generator.py
  git add references/mika.webp        # 커밋하기로 했다면
  git commit -m "tune: IP-Adapter weight 기본값을 실측값으로 확정

  weight 0.5/0.7/0.9 비교 생성 결과 기준.
  포즈 전이 시작 임계값: <값>
  최적값: <값>"
  git push
  ```

- [ ] **5-5.** spec 갱신

  `.kiro/specs/image-reference-pipeline/requirements.md` R7 항목에
  실측 결과를 반영한다. 특히 R7.5 의 임계값.

  `tasks.md` 의 17번 항목을 `[x]` 로 표시.

---

# 문제 발생 시

| 증상 | 확인 |
|---|---|
| `--test` FAIL | JSON 문법. 메시지의 line 번호 확인 |
| WebUI 연결 불가 | `--api` 옵션, 콘솔 창 생존 여부 |
| `[CN]` 줄이 안 나옴 | ControlNet 확장 설치 여부. 1-3 으로 |
| 파일이 0KB / WinError 87 | WebUI Settings → Images filename pattern 비우기 |
| 생성은 되는데 참조 효과 없음 | `[CN]` 로그 확인. weight 올려보기 |
| 표정이 전부 같음 | weight 과다. 낮추기 |
| 인물 중복/뒤틀림 | SD1.5 에 832×1216 은 과대. `IMAGE_SIZE` 낮추기 |
| `--from_image` 에러 | DeepBooru 미설치. `--interrogator clip` |

# 시간이 부족하면

우선순위는 이 순서다.

1. **0단계 + 1단계** — 모델명 확정. 이것만 해도 다음에 이어가기 쉽다.
2. **2단계** — weight 확정. 핵심 산출물.
3. **3단계** — 태그 추출. 독립 기능이라 나중에 해도 무관.
4. **4단계** — 일관성 비교. 확인 성격이라 생략 가능.

1단계에서 ControlNet 이 없어 막히면 3단계로 건너뛴다. 두 기능은 독립이다.

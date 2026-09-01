# 집 PC 작업 체크리스트 (GPU 환경)

> 노트북에서 구현·검증을 마친 참조 이미지 기능을 실제 WebUI 로 확인하는 절차다.
> **코드를 새로 작성할 일은 없다.** 값을 확정하고 검증하는 작업만 남았다.
>
> 관련 spec: `.kiro/specs/image-reference-pipeline/requirements.md` R7

## 어느 브랜치에서 할지 (먼저 결정)

`refactor/modular` 브랜치에 모듈형 리팩터링과 추가 기능이 올라가 있다.
**이 판단은 아래 트레이드오프를 보고 직접 정한다.**

| | `main` | `refactor/modular` |
|---|---|---|
| 구조 | 단일 파일 1,842줄 | 패키지 23모듈 + 33줄 shim |
| `--test` | 45항목 | 67항목 |
| 2단계 weight 비교 | 수동. `Copy-Item` 20여 줄 반복 | `--benchmark` 한 줄 + HTML 비교표 |
| `--no-open` | 없음 | 있음 (삭제 반복 시 경고창 방지) |
| `--interactive` | 없음 | 있음 |
| 기본 JSON | 20종 (2섹션) | 40종 (4섹션) |
| 검증 상태 | 실전 검증됨 | 회귀 69건 통과, GPU 미검증 |

### 판단 기준

**`main` 을 고르는 이유**는 목적 분리다. 오늘 세션의 목적은 weight 확정이다.
여기에 새 구조 검증이 겹치면 문제가 생겼을 때 원인이 weight 인지 리팩터링
인지 가려내기 어려워진다.

**`refactor/modular` 을 고르는 이유**는 2단계가 훨씬 짧아진다는 것이다.
수동 절차(2-3)는 `Remove-Item` / `Copy-Item` 을 가중치마다 반복해야 하고,
결과를 파일 이름으로만 구분하므로 비교할 때 파일 탐색기에서 눈으로 짝을
맞춰야 한다. `--benchmark`(2-3A)는 이걸 한 줄로 줄이고 비교표를 만들어 준다.

**권장**: 시간이 넉넉하면 `main` 으로 0~1단계까지 진행해 ControlNet 모델명을
확정한 뒤, 2단계부터 `refactor/modular` 로 넘어간다. 모델명 확정이 끝나면
변수가 하나 줄어들어 원인 판별 문제가 없어진다.

```powershell
git checkout main                 # 0~1단계
git checkout refactor/modular     # 2단계부터
```

시간이 부족하면 처음부터 `refactor/modular` 로 간다. 두 브랜치의 렌더링
동작은 동일하며 명령어도 `sd_batch_generator.py` 로 같다.

`refactor/modular` 에서 알아둘 차이점:

- `--test` 항목 수가 45 → 67
- `--mock` 산출물이 `generated_assets/` 가 아니라 `mock_assets/` 에 저장됨
  (이 문서의 2단계 이후는 실제 렌더링이므로 영향 없음)
- `--benchmark` 산출물은 `benchmark_assets/` 에 저장됨. 실제 결과물 폴더를
  오염시키지 않는다
- 기본 JSON 이 40종이므로 `--mode all` 이 40장이다. 비교용으로는
  `--mode 3,5,12` 처럼 좁히는 편이 낫다

---

## 노트북에서 이미 검증된 것 (다시 하지 않아도 됨)

| 항목 | 결과 |
|---|---|
| `--test` 자체 진단 | 45항목 (main) / 67항목 (refactor) PASS |
| 시간 측정·집계 로직 | 검증됨 |
| VRAM 응답 파싱 (7케이스) | 검증됨 |
| 참조 이미지 탐색·우선순위 | 검증됨 |
| base64 인코딩 왕복 | 검증됨 |
| 페이로드 조립 구조 | 검증됨 |
| 참조 없을 때 미주입 | 검증됨 |
| 주입 후 원본 불변성 | 검증됨 |
| weight 범위 검증 | 검증됨 |
| 성별 태그 필터 | 검증됨 |
| 모델명 부분 매칭 로직 | 검증됨 (픽스처 기준) |
| 회귀 (참조 없는 기존 동작) | 46건 검증 |
| 벤치마크 가중치 파싱·중복 제거 | 검증됨 |
| 벤치마크 HTML 조립·이스케이프 | 검증됨 (`--mock` 종단) |
| 벤치마크 variant 폴더 분리 | 검증됨 |
| 대화형 모드 argv 조립 | 검증됨 |
| 비대화형 폴백 (tty 없음) | 검증됨 |
| ruff / mypy | 통과 (23모듈) |

## 여기서만 확인 가능한 것

- 실제 ControlNet 모델명 매칭
- WebUI 가 페이로드를 수락하는지
- weight 별 결과 차이
- DeepBooru 태그 추출 품질
- 캐릭터 일관성 개선 정도
- **VRAM 설정별 속도·메모리 실측** (2A단계)
- `/sdapi/v1/memory` 응답 구조가 실제로 파싱되는지
- 벤치마크 뷰어가 **실제 이미지로** 판단에 쓸 만한지
  (노트북에서는 더미 이미지로 구조만 확인했다)

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

  **통과 기준**: `FAIL 0`, 종료 코드 0
  (항목 수는 `main` 45개, `refactor/modular` 67개)

  실패 시: JSON 문법 오류 또는 패키지 누락. 메시지에 line 번호가 나온다.

- [ ] **0-5.** WebUI 실행

  `webui-user.bat` 에 `--api` 확인 후 실행.

  > 2A단계에서 이 파일의 `COMMANDLINE_ARGS` 를 두 번 바꿔가며 비교한다.
  > 지금은 `--api` 만 있어도 되고, 기존에 다른 옵션이 있으면 그대로 두고
  > 2A단계에서 정리한다.
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

### 2-3A. `--benchmark` 로 자동 비교 (`refactor/modular` 전용) ← 권장

- [ ] **2-3A-1.** 한 줄로 전 가중치 생성

  `refactor/modular` 브랜치라면 2-3(수동)을 건너뛰고 이것만 하면 된다.

  ```powershell
  python sd_batch_generator.py --prefix mika `
    --char_prompt "silver hair, blue eyes, school uniform" `
    --benchmark --mode 3,5,12
  ```

  기본 비교 가중치는 `0.3 / 0.5 / 0.7 / 0.9` 네 가지다. 총 12장이 생성된다.
  좁은 구간을 보려면 `--bench_weights 0.5,0.6,0.7,0.8` 처럼 지정한다.

  **산출물은 `benchmark_assets/mika/` 에 들어간다.** `generated_assets/`
  를 건드리지 않으므로 실제 세트와 섞이지 않는다. 접두어도 바뀌지 않아
  프롬프트가 동일하다 — 이게 비교의 전제다.

  ```
  benchmark_assets\mika\
   ├─ benchmark_viewer.html    ← 이걸 브라우저로 연다
   ├─ _benchmark.json
   └─ w0.30\ w0.50\ w0.70\ w0.90\
  ```

- [ ] **2-3A-2.** `[CN]` 줄 확인 (중요)

  화면에 아래가 떴다면 **결과가 전부 같게 나온다.** 가중치가 반영되지
  않은 상태이므로 비교가 무의미하다. 1-3 으로 돌아가 모델을 수동 지정한다.

  ```
  [WARN] ControlNet 미해석 - 가중치가 결과에 반영되지 않습니다.
  ```

  정상이면 `[CN] <모듈명> / <모델명>` 이 한 번 출력되고, 가중치별로
  `[가중치 w0.30] ...` 진행 로그가 이어진다.

- [ ] **2-3A-3.** 뷰어로 비교

  ```powershell
  Start-Process benchmark_assets\mika\benchmark_viewer.html
  ```

  행이 코드, 열이 가중치인 표가 열린다. 가로 한 줄을 훑으면 같은 표정이
  가중치에 따라 어떻게 변하는지 보인다. 이미지를 클릭하면 확대된다.

  표 아래에 가중치별 장당 평균 시간과 VRAM 피크도 함께 나온다.
  2A단계 판단에도 쓸 수 있다.

- [ ] **2-3A-4.** 정리

  판단이 끝나면 지운다. git 추적 대상이 아니므로 남겨둬도 무해하다.

  ```powershell
  Remove-Item -Recurse -Force benchmark_assets\mika
  ```

  → 2-4 로 진행한다.

### 2-3. weight 별 생성 (수동, `main` 용)

- [ ] **2-3.** weight 별 생성

  `refactor/modular` 라면 2-3A 를 쓰고 이 절은 건너뛴다.

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

  아래 상수를 확정값으로 바꾼다. 브랜치에 따라 파일이 다르다.

  | 브랜치 | 파일 |
  |---|---|
  | `main` | `sd_batch_generator.py` |
  | `refactor/modular` | `sd_charaset/config.py` |

  ```python
  REF_WEIGHT_DEFAULT = 0.7        # ← 확정값으로 수정
  ```

  주석에도 근거를 남긴다. 현재 주석은 "실무 관행에 기반한 출발점" 이라고
  적혀 있으니, 실측값으로 갱신한다.

  최적 구간이 기본 4종(0.3/0.5/0.7/0.9)의 바깥이나 사이에 있었다면
  `BENCHMARK_WEIGHTS_DEFAULT` 도 함께 조정해 둔다. (`config.py`)

- [ ] **2-7.** 비교 파일 정리

  2-3(수동)을 썼다면:

  ```powershell
  Remove-Item w05_*.webp, w07_*.webp, w09_*.webp
  ```

  2-3A(벤치마크)를 썼다면 2-3A-4 에서 이미 정리했다.

---

# 2A단계. VRAM 설정 A/B 비교 (4060 Ti 8GB)

에파는 한 배치에 20~50장을 순차 생성한다. 장당 손실이 누적되므로
"OOM 없이 돌아간다" 만으로는 부족하고 **속도까지 함께 봐야** 한다.

실행 후 요약에 아래 두 줄이 찍힌다. 이 숫자를 비교한다.

```
[측정] 20장 / 총 412.3초 / 장당 평균 20.6초 (최속 19.8 ~ 최저 24.1)
[VRAM] 피크 7.21 / 8.00 GiB (90%)
```

> `[VRAM]` 줄은 WebUI 버전에 따라 안 나올 수 있다. `/sdapi/v1/memory` 응답
> 구조가 다르면 조용히 생략된다. 그때는 `[측정]` 만 보고 판단한다.

## 비교 대상

| 버전 | `COMMANDLINE_ARGS` |
|---|---|
| **A** | `--api --medvram-sdxl --xformers` |
| **B** | `--api --xformers` |

A는 [A1111 공식 위키가 8GB Nvidia 에 권장하는 조합](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Optimum-SDXL-Usage)이다.
B는 메모리 절약 없이 속도만 본 것이다.

> **중요:** `--xformers` 와 `--opt-sdp-attention` 을 **같이 쓰지 않는다.**
> 둘 다 어텐션 최적화라 하나만 적용된다. 기존 문서에 둘이 함께 적혀 있었는데
> 그건 잘못이다.

## 절차

- [ ] **2A-1.** 조건 통일

  같은 캐릭터, 같은 코드 범위, 같은 참조 설정으로 돌린다.
  체크포인트도 바꾸지 않는다.

  ```powershell
  # 비교 전 기존 결과 삭제 (건너뛰기 방지)
  Remove-Item -Recurse -Force generated_assets\bench -ErrorAction SilentlyContinue
  ```

  > **`--no-open` 을 붙이면 편하다.** 이 단계는 생성과 삭제를 반복하는데,
  > 매번 탐색기가 열리고 그 직후 폴더를 지우면 "위치를 사용할 수 없습니다"
  > 경고창이 뜬다. 무해하지만 방해가 된다.
  >
  > `refactor/modular` 브랜치에만 있는 플래그다. `main` 에서 작업하면
  > 경고창을 그냥 닫거나, 탐색기 창을 먼저 닫고 삭제하면 된다.

  > **2-3A 를 이미 했다면 이 단계가 짧아진다.** 벤치마크 뷰어의 실행 통계
  > 표에 가중치별 장당 평균 시간과 VRAM 피크가 이미 들어 있다. 같은
  > `COMMANDLINE_ARGS` 안에서의 비교라 A/B 비교에 바로 쓸 수는 없지만,
  > 기준선(baseline)으로는 쓸 수 있다.

- [ ] **2A-2.** 버전 A 측정

  `webui-user.bat` 을 A 로 수정 → **콘솔 창을 완전히 닫고 재실행**
  (`COMMANDLINE_ARGS` 는 실행 시점에 읽히므로 재시작이 필수다)

  ```powershell
  python sd_batch_generator.py --prefix bench --char_prompt "silver hair, blue eyes" --mode 0-9
  ```

  기록:
  ```
  A: 장당 평균 ______ 초 / VRAM 피크 ______ GiB / OOM 발생 여부 ______
  ```

- [ ] **2A-3.** 결과 삭제 후 버전 B 측정

  ```powershell
  Remove-Item -Recurse -Force generated_assets\bench
  ```

  `webui-user.bat` 을 B 로 수정 → 콘솔 닫고 재실행

  ```powershell
  python sd_batch_generator.py --prefix bench --char_prompt "silver hair, blue eyes" --mode 0-9
  ```

  기록:
  ```
  B: 장당 평균 ______ 초 / VRAM 피크 ______ GiB / OOM 발생 여부 ______
  ```

- [ ] **2A-4.** 참조 이미지 켠 상태로 재측정 (중요)

  **IP-Adapter 는 CLIP 비전 인코더를 추가로 올린다.** 8GB 에서는 이것이
  OOM 임계점을 넘길 수 있다. 참조 없이 되던 설정이 참조를 켜면 안 될 수 있다.

  ```powershell
  Remove-Item -Recurse -Force generated_assets\bench
  python sd_batch_generator.py --prefix bench --char_prompt "silver hair, blue eyes" --mode 0-9 --ref_image references\mika.webp
  ```

  기록:
  ```
  참조 ON: 장당 평균 ______ 초 / VRAM 피크 ______ GiB / OOM ______
  참조 OFF 대비 증가분: ______ GiB
  ```

- [ ] **2A-5.** 판정

  | 상황 | 선택 |
  |---|---|
  | B 가 빠르고 OOM 없음 | **B** 채택 (메모리 여유 있음) |
  | B 에서 OOM 발생 | **A** 채택 |
  | 참조 ON 에서만 OOM | A 채택. 그래도 나면 `--medvram` (더 강함) |
  | A/B 속도 차이 5% 미만 | **A** 채택 (안전 마진) |

  20장 이상 연속 생성 시 후반부에 OOM 이 나는지도 함께 본다.
  누적 파편화는 초반 몇 장만 돌려서는 드러나지 않는다.

- [ ] **2A-6.** 확정 설정 기록 및 문서 반영

  ```
  확정: set COMMANDLINE_ARGS=________________________________
  근거: 장당 ______초, VRAM 피크 ______GiB
  ```

  `사용법.txt` 3-1 절의 권장 설정을 이 값으로 갱신한다.
  현재는 `--api --xformers --medvram --opt-sdp-attention` 으로 적혀 있고,
  이건 xformers/sdp 중복 문제가 있으므로 반드시 고쳐야 한다.

- [ ] **2A-7.** 정리

  ```powershell
  Remove-Item -Recurse -Force generated_assets\bench
  ```

## OOM 이 계속 날 때

우선순위대로 시도한다. 아래로 갈수록 느려진다.

1. `--medvram-sdxl` (SDXL 일 때만 적용. 속도 손실 적음)
2. `--medvram` (항상 적용)
3. WebUI Settings 에서 생성 후 VRAM 비우기 켜기
4. `IMAGE_SIZE` 를 `(768, 1152)` 로 낮추기
5. `--lowvram` — **최후 수단.** 속도에 치명적이라 배치 작업에는 부적합하다

**참고: 에파의 재개 기능이 안전망이다.** 25장에서 OOM 이 나도 같은 명령을
다시 실행하면 26장부터 이어간다. 설정을 완벽히 맞추지 않아도 작업은 진행되므로,
여기에 과도한 시간을 쓰지 않는다.

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

  `FAIL 0` 유지 확인. (항목 수는 `main` 45개, `refactor/modular` 67개)

- [ ] **5-3.** 참조 이미지 커밋 여부 결정

  `references/*.webp` 는 git 추적 대상이다. 커밋하면 노트북에서도
  같은 참조를 쓸 수 있다.

  개인 자료라 올리고 싶지 않으면 그 파일만 `.gitignore` 에 개별 추가한다.

- [ ] **5-4.** 커밋 및 푸시

  ```powershell
  # main 이면
  git add sd_batch_generator.py
  # refactor/modular 이면
  git add sd_charaset/config.py

  git add references/mika.webp        # 커밋하기로 했다면
  git commit -m "tune: IP-Adapter weight 기본값을 실측값으로 확정

  weight 0.3/0.5/0.7/0.9 비교 생성 결과 기준.
  포즈 전이 시작 임계값: <값>
  최적값: <값>"
  git push
  ```

  > `benchmark_assets/` 는 `.gitignore` 에 있으므로 커밋되지 않는다.
  > 비교표를 남기고 싶으면 폴더째로 따로 백업한다.

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
| CUDA out of memory | 2A단계 "OOM 이 계속 날 때" 참고. 재실행하면 이어서 생성됨 |
| 후반부에서만 OOM | 누적 파편화. VRAM 비우기 옵션 켜기 |
| `[VRAM]` 줄이 안 나옴 | `/sdapi/v1/memory` 응답 구조 차이. 기능 문제 아님 |
| 참조 켜면 OOM | IP-Adapter 가 CLIP 인코더를 추가로 올림. `--medvram-sdxl` 필요 |
| `--benchmark` 가 즉시 거부됨 | 참조 이미지 없음. 2-1 을 먼저 한다 |
| 벤치마크 결과가 전부 동일 | `[WARN] ControlNet 미해석`. 1-3 으로 |
| 벤치마크 뷰어 이미지 깨짐 | HTML 만 옮겼음. `benchmark_assets/{약칭}/` 폴더째로 |
| 벤치마크 중간에 멈춤 | 연결 끊김. 남은 가중치를 건너뛰고 여기까지의 뷰어를 만든다. 재실행하면 이미 만든 장은 건너뛴다 |
| `--benchmark` 없다고 나옴 | `main` 브랜치다. `git checkout refactor/modular` |

# 시간이 부족하면

우선순위는 이 순서다.

1. **0단계 + 1단계** — 모델명 확정. 이것만 해도 다음에 이어가기 쉽다.
2. **2단계** — weight 확정. 핵심 산출물.
   `refactor/modular` 에서 2-3A(`--benchmark`)를 쓰면 이 단계가 크게 짧아진다.
   명령 한 줄과 뷰어 확인으로 끝난다.
3. **2A단계** — VRAM 설정. OOM 이 나고 있다면 2단계보다 먼저.
4. **3단계** — 태그 추출. 독립 기능이라 나중에 해도 무관.
5. **4단계** — 일관성 비교. 확인 성격이라 생략 가능.
   2단계를 `--benchmark` 로 했다면 뷰어에 이미 나란히 비교된 결과가 있어
   이 단계의 가치가 줄어든다. 생략해도 무방하다.

1단계에서 ControlNet 이 없어 막히면 3단계로 건너뛴다. 두 기능은 독립이다.

2A단계는 WebUI 재시작이 두 번 필요해 시간이 걸린다. OOM 이 발생하지 않고
속도도 견딜 만하면 **현재 설정 그대로 두고 나중에 해도 된다.** 단
`--xformers` 와 `--opt-sdp-attention` 이 함께 적혀 있으면 그것만은 지금
하나로 정리한다.

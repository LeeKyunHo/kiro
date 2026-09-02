---
inclusion: auto
name: sd_char_gen
description: 사용자가 "캐릭터 생성:" 형식으로 입력하면 sd_batch_generator.py를 즉시 실행하는 규칙
---

# 챗봇 캐릭터 에셋 자동 생성 규칙

## 트리거 패턴

```
캐릭터 생성: [약칭] / [외형 프롬프트] / [네거티브]
```

네거티브는 생략 가능하다.

```
캐릭터 생성: [약칭] / [외형 프롬프트]
```

슬래시(`/`) 기준으로 나누고 각 조각의 앞뒤 공백을 제거해 파싱한다.

### 약칭만 준 경우 — 프리셋을 먼저 확인한다

```
캐릭터 생성: mika
```

외형 프롬프트 없이 약칭만 오면 `characters.json` 에 등록된 캐릭터일 수
있다. 이때는 외형을 되묻지 말고 프리셋으로 실행한다.

```powershell
python sd_batch_generator.py --char mika
```

등록되지 않은 이름이면 스크립트가 종료 코드 1과 함께 사용 가능한 목록을
출력한다. 그 목록을 사용자에게 전달하고 외형 프롬프트를 요청한다.
**임의로 외형을 만들어 넣지 않는다.**

## 동작 규칙

1. 불필요한 설명이나 확인 질문 없이 터미널 명령을 실행한다.
2. 작업 디렉터리는 워크스페이스 루트(`pose_database.json` 이 있는 폴더)로 한다.
   현재 경로: `C:\Users\USER\kiro`
   `cd` 를 쓰지 말고 `cwd` 파라미터로 지정한다.
   다른 PC에서 클론했다면 이 경로를 그 환경의 클론 위치로 수정한다.

2-1. 실행은 `python sd_batch_generator.py` 형태를 쓴다.
   구현은 `sd_charaset` 패키지에 있고 이 파일은 하위 호환 shim 이다.
   `python -m sd_charaset` 도 완전히 동일하게 동작하므로 어느 쪽을 써도 된다.
3. 아래 템플릿을 조립해 `execute_pwsh` 로 실행한다. 기본 모드는 `all`.
4. 실행 후 터미널 출력을 그대로 전달하고, 스크립트가 출력하는
   젠잇 마크다운 블록을 채팅창에도 그대로 표시한다.

### 네거티브 있을 때

```powershell
python sd_batch_generator.py --prefix "[약칭]" --char_prompt "[외형 프롬프트]" --custom_neg "[네거티브]" --mode all
```

### 네거티브 없을 때

```powershell
python sd_batch_generator.py --prefix "[약칭]" --char_prompt "[외형 프롬프트]" --mode all
```

## 캐릭터 프리셋 (`characters.json` / `--char`)

캐릭터별 외형·프로필·네거티브·참조 강도를 저장한 파일이다. 약칭 하나로
전부 불러온다.

```powershell
python sd_batch_generator.py --char mika
```

| 필드 | 생략 시 |
|---|---|
| `char_prompt` | **필수** |
| `profile` | `female` |
| `custom_neg` | 없음 |
| `ref_weight` | 0.7 |
| `ref_image` | `references/{약칭}` 자동 탐색 |
| `mode` | `all` |
| `note` | 사람용 메모. 생성에 영향 없음 |

### ★ 우선순위: CLI 명시값 > 프리셋 > 기본값

프리셋을 쓰면서 일부만 바꿀 수 있다.

```powershell
python sd_batch_generator.py --char mika --mode emotions      # 범위만 변경
python sd_batch_generator.py --char mika --ref_weight 0.4     # 강도만 변경
python sd_batch_generator.py --char mika --prefix mika_v2     # 다른 폴더에 변형
python sd_batch_generator.py --char mika --custom_neg ""      # 프리셋 네거티브 비우기
```

실행 로그의 `[CHAR]` 줄에 무엇이 적용됐는지 나온다. 사용자에게 전달한다.

```
[CHAR]  preset:mika + cli(ref_weight) | prefix=mika | mode=all |
        profile=female | ref_weight=0.4
        CLI 가 덮어쓴 축: ref_weight
```

### 안내 규칙

- **사용자가 "mika 다시 뽑아줘" 처럼 말하면 `--char mika` 를 먼저 시도한다.**
  긴 외형 프롬프트를 다시 조립하지 않는다. 매번 태그가 조금씩 달라지면
  같은 캐릭터인데 결과물의 일관성이 깨진다.
- 새 캐릭터를 반복해서 뽑을 것 같으면 `characters.json` 등록을 제안한다.
  등록은 사용자 승인 후에 한다. 파일을 임의로 고치지 않는다.
- `--char` 는 `--test` / `--from_image` 에서 쓰이지 않는다. 함께 주면
  경고가 뜨고 무시된다. 그 모드에는 붙이지 않는다.
- 프리셋 파일이 없어도 기존 명령은 정상 동작한다. 선택 기능이다.

### 실패 메시지 대응

| 메시지 | 대응 |
|---|---|
| `등록되지 않은 캐릭터 'X'. 사용 가능: [...]` | 출력된 목록을 전달하고 선택을 받는다. 이름을 추측해 재시도하지 않는다 |
| `characters.json 이 없어 --char 를 쓸 수 없습니다` | `--prefix` / `--char_prompt` 로 실행하거나 파일 생성을 제안한다 |
| `[WARN] 캐릭터 'X' 의 알 수 없는 필드: [...]` | 필드명 오타다. 위 표의 7가지와 대조해 알린다 |
| `[WARN] 프로필 'X' 이 pose_database.json 에 없음` | `_profiles` 에 없는 이름이다. 둘 중 어느 쪽을 고칠지 묻는다 |

## 약칭(prefix) 제약

스크립트가 `^[A-Za-z0-9_-]{1,64}$` 로 검증하고, 위반 시 종료 코드 1로 거부한다.
경로 이탈과 인용부호 주입을 입구에서 막기 위한 화이트리스트다.

| 입력 | 판정 |
|---|---|
| `mika`, `test_01`, `rin-a` | 허용 |
| `미카` (한글) | 거부 |
| `a/b`, `..`, `a b` | 거부 |
| 65자 이상 | 거부 |

사용자가 한글이나 특수문자로 약칭을 주면, **임의로 바꾸지 말고** 영문 약칭을
다시 요청한다. 파일명과 젠잇 호출 코드에 그대로 들어가는 값이므로 추측하면 안 된다.

## 프로필(--profile)

성별 등 캐릭터 축은 `pose_database.json` 의 `_profiles` 섹션에서 결정한다.
프로필의 `base_positive` / `base_negative` 가 스크립트 하드코딩 기본값을
**완전히 대체**하므로, 남캐를 만들 때 `1girl` 이 섞이지 않는다.

| 프로필 | 용도 |
|---|---|
| `female` | 여성 캐릭터 (기본값) |
| `male` | 남성 캐릭터 |
| `male_otokonoko` | 중성적 외형의 남성 캐릭터 |

```powershell
python sd_batch_generator.py --prefix ryu --char_prompt "short black hair, sharp eyes" --profile male
```

- 생략하면 `female` 이 적용되고 콘솔에 `[PROFILE] 미지정 - 기본값 'female' 적용` 이 찍힌다.
- 사용자가 남캐를 요청했는데 `--profile` 을 안 주면 여캐가 나온다.
  트리거 입력에 남성·소년·otokonoko 등의 단서가 있으면 적절한 프로필을 붙인다.
- 포즈/표정 프롬프트에는 성별 태그를 넣지 않는다. 성별은 프로필 축에서만 다룬다.
- `--custom_neg` 는 프로필 네거티브에 덧붙는다 (대체가 아니라 추가).

## 참조 이미지 (IP-Adapter)

`references/{약칭}.png` 가 있으면 자동으로 발견되어 캐릭터 일관성에 사용된다.
확장자는 `.png` → `.jpg` → `.jpeg` → `.webp` 순으로 탐색한다.

**참조 이미지가 없어도 정상이다.** 경고만 출력되고 텍스트 프롬프트만으로
생성된다. 이것을 오류로 보고하지 않는다.

```
[WARN] 참조 이미지 없음 (references/mika.*) - 텍스트 프롬프트만 사용
```

실행 로그에서 아래 두 줄을 확인해 사용자에게 상태를 전달한다.

| 로그 | 의미 |
|---|---|
| `[REF] ... weight 0.7` + `[CN] ... (auto)` | 참조 정상 적용 |
| `[REF] ... - ControlNet 미해석` | 참조 파일은 있으나 미적용 |
| `[WARN] 참조 이미지 없음` | 참조 없이 생성 (정상) |
| `[WARN] IP-Adapter 모듈/모델을 찾지 못했습니다` | 목록이 출력됨 |

마지막 경우에는 출력된 사용 가능 목록에서 `ip-adapter` 가 포함된 이름을 찾아
`--cn_module` / `--cn_model` 로 지정해 재실행할 것을 제안한다. 임의로 값을
추측해 넣지 않는다.

`--ref_weight` 는 기본 0.7 이다. 사용자가 "표정이 다 똑같이 나온다" 고 하면
weight 가 과도한 것이므로 0.6 이하를 제안한다. 다만 값을 추측해 제시하기보다
`--benchmark` 로 비교하도록 안내하는 편이 낫다 (아래 참고).

## 가중치 벤치마크 (`--benchmark`)

적정 `--ref_weight` 를 찾는 도구다. 같은 코드를 여러 가중치로 생성하고
비교 매트릭스 HTML 을 만든다.

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes" --benchmark --mode 3,5,12
```

| 항목 | 내용 |
|---|---|
| 기본 가중치 | `0.3, 0.5, 0.7, 0.9` (`--bench_weights 0.5,0.6,0.7` 로 변경) |
| 산출물 | `benchmark_assets/{약칭}/benchmark_viewer.html`, `_benchmark.json` |
| 이미지 | `benchmark_assets/{약칭}/w0.30/` ~ `w0.90/` |
| 전제 조건 | **참조 이미지 필수.** 없으면 종료 코드 1 로 거부한다 |

### 안내 규칙

- **대상 코드를 좁히도록 권한다.** 기본 JSON 이 40종이므로 `--mode all` 이면
  40 × 4 = 160장이다. 표정 변화가 큰 2~3개(`--mode 3,5,12` 등)로 충분하다.
- **접두어를 바꾸라고 제안하지 않는다.** 접두어는 트리거 태그로 프롬프트에
  들어가므로 가중치별로 바꾸면 비교의 전제가 깨진다. 구분은 `w0.30/` 같은
  하위 폴더로 이미 되어 있다.
- 실행 로그에 `[WARN] ControlNet 미해석 - 가중치가 결과에 반영되지 않습니다`
  가 뜨면 **결과가 전부 같게 나온다.** 비교가 무의미하므로 사용자에게 알리고
  `--cn_module` / `--cn_model` 확인을 먼저 하도록 안내한다.
- 완료 후 뷰어 경로를 사용자에게 전달한다. 이미지를 상대 경로로 참조하므로
  **HTML 만 옮기면 깨진다.** 폴더째로 옮겨야 한다.
- 최적값을 정했으면 `sd_charaset/config.py` 의 `REF_WEIGHT_DEFAULT` 를
  갱신하도록 안내한다. 임의로 수정하지 않는다.
- `--mock` 과 함께 쓰면 WebUI 없이 뷰어 조립 경로를 검증할 수 있다.
  더미 이미지이므로 화질·일관성 비교에는 쓸 수 없고, 뷰어 상단에 그 경고가
  표시된다. 이 경우에도 참조 이미지 파일 자체는 있어야 한다.

`benchmark_assets/` 는 `.gitignore` 대상이다. 커밋 후보로 제시하지 않는다.

## 대화형 모드 (`--interactive` / `-i`)

사용자가 "명령어를 모르겠다", "옵션이 복잡하다" 고 하면 이 모드를 안내한다.

```powershell
python sd_batch_generator.py --interactive
```

- 실행 모드 → 약칭 → 외형 태그 → 프로필 → 범위 → 네거티브 → 참조 → 탐색기
  순서로 묻는다.
- 방향키·Enter·숫자키로 조작하고 `q` 로 취소한다.
- 마지막에 조립된 명령을 보여주고 확인을 받는다. 그 줄을 복사해두면 다음부터
  대화형 없이 쓸 수 있다.
- 내부적으로 argv 를 만들어 **기존 파서에 다시 넣는다.** 검증 규칙과 동작이
  직접 타이핑한 것과 동일하다.

### ★ 에이전트가 직접 실행하지 않는다

이 모드는 사용자의 키 입력을 기다린다. `execute_pwsh` 로 실행하면 tty 가
아니므로 번호 입력 폴백으로 전환되고, 그마저도 stdin 이 없으면 기본값으로
진행되거나 멈춘다.

트리거(`캐릭터 생성:`)에 대응할 때는 항상 인자를 조립한 비대화형 명령을 쓴다.
`--interactive` 는 **사용자가 직접 터미널에 입력할 명령으로만 제안한다.**

## 태그 추출 (`--from_image`)

사용자가 참조 이미지에서 프롬프트를 뽑고 싶다고 하면 이 명령을 쓴다.

```powershell
python sd_batch_generator.py --from_image "경로"
```

`--prefix` / `--char_prompt` 는 필요 없다. WebUI 는 켜져 있어야 한다.

출력의 `[원본]` 이 아니라 **`[권장]`** 을 사용자에게 전달한다. `[원본]` 에는
`1girl`, `solo` 같은 성별·인원 태그가 있어 프로필과 충돌한다.

## 태그 충돌 경고

포지티브와 네거티브에 같은 태그가 있으면 실행 시작 시 경고가 뜬다.

```
[WARN] 태그 충돌: ['breasts'] 가 포지티브와 네거티브에 동시 존재
```

`--char_prompt` 가 선택한 프로필의 네거티브와 겹칠 때 주로 발생한다.
경고가 뜨면 프로필 선택이 잘못됐거나 외형 태그가 맞지 않는 것이므로,
실행을 강행하지 말고 사용자에게 알린다. 가중치 표기(`(breasts:1.3)`)도 감지된다.

같은 문자열만 잡는다. `1girl` 과 `1boy` 처럼 의미가 상충하지만 문자열이 다른
경우는 자동 검출되지 않는다.

## --mode 값

| 형태 | 예시 | 결과 |
|---|---|---|
| 프리셋 | `all` | JSON 전체 코드 |
| 섹션명 | `emotions`, `poses` | 해당 섹션만 |
| 코드 리스트 | `0,5,12` | 지정 코드만 |
| 코드 범위 | `10-14` | 범위 내 코드 |
| 혼합 | `0-5,12` | 합집합 |

섹션명은 `pose_database.json` 최상위 키에서 자동으로 읽는다.
사용 가능 목록은 `python sd_batch_generator.py --help` 로 확인할 수 있다.

현재 기본 DB 는 4섹션 40종이다. `--mode` 를 생략하면 40장을 생성한다.

| 섹션 | 코드 | 내용 |
|---|---|---|
| `emotions` | 00~09 | 감정 / 표정 |
| `poses` | 10~19 | 포즈 / 동작 |
| `outfits` | 20~29 | 의상 |
| `situations` | 30~39 | 상황 / 배경 |

40장은 시간이 걸린다. 사용자가 처음 시도하는 것이면 `--mode emotions` 로
10장만 먼저 뽑아 화풍을 확인하도록 제안한다. 이미 만든 파일은 건너뛰므로
이어서 전체를 돌리면 남은 30장만 생성된다.

## 검증 모드 (WebUI 없이 실행 가능)

사용자가 생성을 요청했는데 WebUI가 꺼져 있거나, 설정 변경 후 점검이 필요할 때 사용한다.

| 명령 | 용도 |
|---|---|
| `--test` | 두 JSON 파일과 로직 자체 진단 (93항목). 인자 불필요 |
| `--dry-run` | 파일 쓰기 없이 대상·파일명·마크다운만 출력 |
| `--mock` | 더미 이미지를 실제로 저장해 종단 검증 |
| `--benchmark --mock` | 벤치마크 뷰어 조립까지 검증 (참조 파일 필요) |

`--test` 는 `pose_database.json` 과 `characters.json` 을 함께 검사한다.
어느 쪽을 편집한 뒤에도 이것만 돌리면 된다.

`--mock` 산출물은 `mock_assets/{약칭}/` 에 저장된다. 실제 결과물이 들어가는
`generated_assets/` 와 경로가 분리되어 있으므로 **실제 약칭을 그대로 써도
안전하다.** 더미 이미지에는 `MOCK` 텍스트가 그려진다.

우선순위는 `--test` > `--from_image` > `--benchmark` > `--dry-run` > `--mock` >
기본이다. 앞의 셋은 별도 Command 이고, 뒤의 둘은 생성 Command 안의 전략이다.

`--interactive` 는 이 우선순위 밖에 있다. 다른 인자를 파싱하기 전에 먼저
처리되어 argv 를 조립한 뒤 같은 파서로 되돌아온다.

`실제 출력 폴더에 mock 산출물이 있습니다` 에러가 나면, 사용자가 더미 이미지를
`generated_assets/` 로 복사한 상태다. `_mock_manifest.json` 과 함께 있는
더미 이미지를 삭제하도록 안내한다. 임의로 삭제하지 않는다.

## 출력 스트림

진행 로그와 경고는 stderr, 젠잇 마크다운은 stdout 으로 나간다.
따라서 마크다운만 파일로 받으려면 리다이렉트를 쓸 수 있다.

```powershell
python sd_batch_generator.py --prefix mika --char_prompt "..." > assets.md
```

사용자가 결과 복사를 번거로워하면 이 방법을 제안한다.

## 종료 코드 해석

| 코드 | 의미 | 대응 |
|---|---|---|
| 0 | 정상 완료 | 마크다운 블록 전달 |
| 1 | 설정·입력 오류 또는 연결 중단 | stderr 의 `[ERROR]` 와 힌트를 그대로 안내 |
| 2 | 필수 인자 누락 | 트리거 파싱 결과를 재확인 |
| 130 | 사용자 중단(Ctrl+C) | 재실행 안내 |

`ConnectionError` 로 중단된 경우 WebUI 를 `--api` 옵션으로 실행했는지 확인하도록 안내한다.

## 프롬프트 편집

- 감정(00~09)·포즈(10~19)·의상(20~29)·상황(30~39) 태그는
  `pose_database.json` 에서 편집한다.
- 섹션을 새로 만들 때는 번호대를 40번대 이상으로 잡아 기존과 겹치지 않게 한다.
  섹션이 달라도 번호가 겹치면 나중에 읽은 것이 이긴다.
- 스크립트 코드 수정 없이 JSON만 바꾸면 반영된다.
- 프롬프트의 **첫 태그가 라벨**이 되어 콘솔 출력과 상태 매핑 가이드에 표시된다.
  항목을 구별하는 서술어를 맨 앞에 둔다.
- 편집 후 `--test` 로 검증한다.

## 재실행 동작

이미 존재하는 파일은 건너뛴다. 중단 후 같은 명령을 다시 실행하면 남은 것만
생성한다. 강제로 다시 만들려면 해당 `.webp` 파일을 지우고 실행한다.

## 입력 파싱 예시

| 사용자 입력 | --prefix | --char_prompt | --custom_neg |
|---|---|---|---|
| `캐릭터 생성: mika / silver hair, blue eyes, school uniform / chibi` | `mika` | `silver hair, blue eyes, school uniform` | `chibi` |
| `캐릭터 생성: rin / long black hair, red eyes` | `rin` | `long black hair, red eyes` | *(생략)* |

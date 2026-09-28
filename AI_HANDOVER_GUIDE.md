# 키로(Kiro) 파이프라인 마스터 인계 및 AI 학습 가이드 (Handover Guide)

> **문서 목적**: 본 문서는 Antigravity(안티그래비티) 에이전트가 프로젝트 인수인계를 받아 즉시 연속성 있게 작업을 수행할 수 있도록, 시스템 아키텍처, 프롬프트 파이프라인의 핵심 수정 사항, 발생했던 주요 트러블슈팅 및 해결 매커니즘을 총정리한 학습 문서입니다.  
> **최종 갱신일**: 2026-09-28  
> **연계 문서**: [`GEMINI.md`](file:///c:/Users/rbsgh/kiro/GEMINI.md), [`PROJECT_GUIDE.md`](file:///c:/Users/rbsgh/kiro/PROJECT_GUIDE.md), [`pose_database.json`](file:///c:/Users/rbsgh/kiro/pose_database.json)

---

## 1. 프로젝트 개요 및 핵심 아키텍처

- **목적**: Stable Diffusion WebUI (Forge/A1111) API를 연동하여 캐릭터 챗봇용 2D 셀화풍 일러스트 에셋(감정 20종, 솔로포즈+스킨십 20종, H-씬 17종, 오토코노코 17종 등 총 74종)을 배치 생성하고 젠잇(Genit) 마크다운 코드를 조립하는 CLI 시스템.
- **체크포인트**: Unholy Nova AI (SDXL 기반 Danbooru 학습). 프롬프트는 자연어 서술을 배제하고 순수 Danbooru 공식 태그만 사용.
- **핵심 분리 원칙**:
  - **프롬프트 데이터 격리**: 공용 포즈 DB는 `pose_database.json`에, 캐릭터 외형/의상은 `projects/{roster}/characters/{char}.json`에 위치.
  - **파이프라인 로직 격리**: 배치 생성, 태그 정규화, 프롬프트 조립, WebUI 통신 로직은 `generator/` Python 패키지에 모듈화.
- **테스트 무결성**: 모든 수정 후 반드시 `python sd_batch_generator.py --test` (52개 항목 단위 검사) PASS 유지.

---

## 2. 프롬프트 조립 파이프라인 (`generator/prompt.py`, `generator/runner.py`)

### 2.1 BREAK 2청크 전진 배치 아키텍처
CLIP 토큰 분산을 방지하기 위해 2개의 청크로 분리 조립합니다:
- **Chunk 1 (품질 및 포즈/구도)**: `[마스터피스 품질 태그], [포즈 및 행위 태그]`
- **Chunk 2 (캐릭터 외형 및 트리거)**: `[인물 외형, 신체, 헤어스타일, 눈동자], [트리거 태그]`

### 2.2 탈의/H-씬 의상 자동 스트리핑 엔진 (`strip_outfit_tags`)
- **배경 문제**: 캐릭터 JSON의 외형 프롬프트에 포함된 평상시 의상(예: `pitch-black ribbed knit high-neck long-sleeved bodycon mini dress`, `choker`, `necklace` 등)이 H-씬에서도 그대로 2번째 청크로 전송되어, 1번째 청크의 `completely nude`와 충돌하여 **목(하이넥/초커)이나 팔(롱슬리브 소매)에 천조각이 감겨서 생성되는 기괴한 의상 잔재 현상** 발생.
- **해결 매커니즘 (`generator/prompt.py`)**:
  - `strip_outfit_tags()` 함수 신설:
    - 헤어스타일(`hair`, `ponytail`, `bun`, `bangs`, `strands` 등), 눈동자, 얼굴, 체형, 피부톤 등 본연의 외형은 **100% 보존**.
    - 드레스, 니트, 소매, 넥라인, 칼라, 목걸이, 스커트, 바지, 속옷 및 **언더붑/언더버스트/코르셋(`underboob`, `underbust`, `corset`, `bodice`, `straps`) 관련 키워드 완벽히 자동 적출**. (BJh처럼 캐릭터 설정에 `underboob exposure`, `bare underbust curve`가 있을 경우 H-씬에서 가슴 위 탑과 허리 코르셋이 강제 생성되는 현상을 완벽 해결)
  - `generator/runner.py`에서 탈의 씬(`h_scenes`, `scenes_otokonoko` 또는 `nude`, `bare skin` 포함 씬) 감지 시:
    - 캐릭터 외형 프롬프트에 `strip_outfit_tags()`를 적용하여 의상 및 언더붑 태그를 배제.
    - 네거티브 프롬프트에 `(clothes, clothing, dress, sleeves, collar, cuffs, fabric, rags, swimsuit, swimwear, bikini, underboob, underbust, corset, bodice, bra, crop top, halter, straps:1.35)` 자동 주입.
    - 참조 이미지 속 평상복(블랙 드레스 등)의 신체 전사를 막기 위해 IP-Adapter 가중치를 `0.5`로 자동 완화.

### 2.3 2인 상호작용 씬 페어링 및 파트너 생성 보장 매커니즘
- **배경 문제**: 상호작용 H-씬에서 상대방 파트너(모브 남성)가 누락되고 여성 캐릭터 단독 샷(`solo`)만 출력되는 문제.
- **해결 매커니즘 (`generator/runner.py`)**:
  - **네거티브 필터링**: 캐릭터 네거티브에 남성/복수인원 억제 토큰(`1boy`, `2boys`, `male`, `masculine`, `man`, `men`, `guy`, `boy`, `boys`, `yaoi`, `multiple characters`, `beard`, `mustache`, `facial hair`)이 있더라도 상호작용 씬에서는 이를 완벽히 필터링 제외.
  - **단독 태그 차단**: 포지티브 프롬프트에서 `solo` 태그 완전 제거.
  - **2인 구도 페어링 태그 주입**:
    - 일반 H-씬(`h_scenes`): 프롬프트 최선두에 `hetero, 1boy` 자동 주입.
    - 오토코노코 씬(`scenes_otokonoko`): 프롬프트 최선두에 `yaoi, 2boys` 자동 주입.
  - **거리 분리 방지**: 네거티브에 `(standing apart, separated, distance between characters:1.3)`를 주입하여 두 캐릭터가 물리적으로 떨어지는 현상 차단.

---

## 3. 포즈 데이터베이스(`pose_database.json`) 핵심 개편 내역

### 3.0 00~39번 전면 개편 (Unholy Nova AI 최적화)
- **배경 문제**: 기존 프롬프트에 자연어 서술과 과도한 손 제스처가 혼재하여 손가락 기형 및 인체 엉킴 발생.
- **해결 매커니즘**: 순수 Danbooru 키워드 전환, 감정 20종(00~19) 통폐합 및 신규 표정 추가, 스킨십 20종(20~39)을 솔로 11종+2인 안정 9종으로 정예화.

### 3.1 40번 이상 H-씬 및 오토코노코 씬 전면 개편 (정예화 및 후처리 검열 연계)
- **인위적 검열 태그 전면 제거**: 프롬프트의 `censored`, `(black censor bar...)` 등 모델 화풍을 훼손하던 키워드를 전면 삭제. 검열은 `censor_studio.py` 후처리 도구로 전담하여 순수 최고 화질 생성 보장.
- **자연스러운 표준 정석 구도 유지**: 억지로 가리려다 인체 비율이 무너지지 않도록, 표준적인 Danbooru 구도 태그(측면 앵글, 대면 구도, 상반신 샷 등)를 적용하여 해부학적 무결성 극대화.
- **정예화 및 1:1 대칭 완비 (총 40종)**:
  - 일반 H-씬(`h_scenes`): 40~59번 (20종 완비: 기본 체위 + 구강 체인 + 사후여운 다각화)
  - 오토코노코 씬(`scenes_otokonoko`): 140~159번 (20종 완비: 애널/핸드잡 특화 1:1 대칭 매핑)
  - 구도별 시각적 변별력 대폭 향상 및 클라이맥스 체액 연출 극대화.

### 3.2 바닥(`floor`) 편향 제거 및 범용 가구/기물 매핑
- **바닥 편향 원인**: 프롬프트 앞단에 `((discarded clothes on floor...:1.35))` 및 `floor sex` 태그가 강하게 걸려 장면 전체가 차가운 맨바닥으로 오염됨.
- **범용 가구 배치 (장르 불문 호환)**:
  - **침대 그룹 (누운 자세 / 위에 올라탄 자세)**: `lying on bed` 또는 `on bed` 등 간결한 침대 태그 배치. (40~50, 53~54, 60~63, 75, 140~150 등)
  - **소파/암체어 그룹 (착석 / 기대기 자세)**: `sitting on sofa` 또는 `reclining on sofa` (51~52, 69~71, 73, 151~152, 169~170 등)
  - **러그 그룹 (무릎 꿇은 자세)**: `kneeling on rug` (55~56 등)
  - **주의 사항**: 가구 수식어를 장황하게 늘어놓으면(`plush leather sofa, soft cushions...`) CLIP 앞단 토큰을 잠식하여 인물 간 상호작용이 뒤로 밀리므로, **간결한 핵심 명사형 태그**를 유지할 것.

### 3.3 주요 포즈별 구도 안정화 조치
1. **31번 (유두 애무)**: 불필요한 떨림(`trembling`) 및 눈 뒤집힘 태그를 제거하여 감정 표정 안정화.
2. **34번 (1인칭 POV 딥키스)**: 분신 발생을 막기 위해 1인칭 남성 시점(`pov, looking down at partner`)으로 구도 단일화.
3. **55번, 56번 (파이즈리 / 절정)**:
   - 두 인물이 멀리 떨어지던 원인이었던 `faceless male standing`의 `standing` 단어 전면 제거.
   - 1인칭 초근접 POV 구도(`pov, looking down, intimate close-up, paizuri, titfuck, upper body close-up`).
   - 상대방 다리 사이에 완전히 파고든 밀착 배치(`kneeling between partner legs, close physical contact, chest pressed against partner`).
   - 55번에서 솔로 핀업을 유발하던 `hands holding own breasts` 및 `looking up at viewer`를 제거하고 `hands resting on partner thighs`, `looking up at partner`로 일치.
4. **57번 (쿠닐링구스)**:
   - 모순되던 `kneeling on rug`(러그에 무릎 꿇음)와 `reclining on sofa`(소파에 누움) 동시 지정 에러 해결.
   - 여성은 소파에 편안히 기댄 자세(`reclining on sofa, leaning back against sofa cushions, legs spread wide, arched back`), 남성은 다리 사이에 무릎 꿇은 위치(`faceless male partner kneeling between her thighs`)로 역할 분리.
5. **67번, 68번 (샤워 씬 / 절정)**:
   - 배경에 쇠창살/블라인드 격자가 생기던 원인인 `bathroom setting`, `steam` 제거.
   - 화풍이 수채화처럼 번지던 원인인 `steam`, `water streaming down`, `wet skin glistening` 전면 제거하여 선명한 2D 셀화풍 유지.
   - 투명 유리벽 밀착 구도 강화 (`standing against clear glass wall, pinned against glass`).
   - 네거티브에 `(bars, vertical bars, fence, lattice, cage, blinds, grating:1.3)` 자동 주입.
   - 완전 탈의 태그 전진 배치 (`((completely nude, full nudity, unclothed, bare skin:1.25))`).

### 3.4 유혹 포즈 및 샤워 구도 고도화 (26~29, 38~39)
- **26번 (무릎 유혹)**: 침대 위에 무릎을 꿇고 허리를 꺾어 정면의 유저를 올려다보는 유혹 포즈.
- **27번 (펠라 시늉)**: 입술에 손가락을 얹고 혀를 살짝 내밀어 구강 봉사를 암시하는 도발적 제스처 (`finger on lips, parted lips, tongue out, suggestive mouth gesture, teasing`).
- **28번 (대딸 시늉)**: 손으로 스트로킹 모션을 흉내 내며 원을 만들어 유혹하는 상징적 제스처 (`suggestive hand gesture, hand mimicking stroking motion, hand forming circle, teasing seductive smile`).
- **29번 (뒤치기 유혹)**: 사지에 엎드려 엉덩이를 치켜들고 어깨 너머로 뒤돌아보는 후배위 유혹 구도 (`on all fours, ass up, arched back, looking back over shoulder, turned back, buttocks focus`).
- **38번 (샤워 알몸 뒤태)**: 투명 유리 샤워부스 안에서 젖은 몸으로 등과 엉덩이를 강조하는 알몸 단독 뒤태 (`completely nude, back focus, from behind, standing in shower, clear glass shower stall, water droplets, wet hair, buttocks focus`).
- **39번 (샤워장 유리 밀착 유혹)**: 투명 유리벽에 가슴과 손을 완전히 밀착하고 너머의 유저를 유혹하는 상반신 클로즈업 (`completely nude, breasts pressed against glass, hands pressed on glass, looking at viewer through glass, condensation, water droplets, upper body focus`).

### 3.5 H-씬 구강 체인 완비 및 사후여운/클라이맥스 체액 강화 (52~59)
- **단계별 구강 체인 구축**:
  - `52` (구강 기본 봉사): `kneeling, oral, looking up at partner`
  - `53` (구강 절정): `facial cum, thick cum on face, mouth, lips`
  - `54` (이라마치오 / 딥스로트): `deepthroat irrumatio, shaft deep in throat, throat bulge, tears streaming, gagging pleasure`
  - `55` (구강 사후여운): `oral aftermath, excessive cum overflowing from mouth, dripping from lips to chest, completely dazed expression`
- **사후여운 구도 다각화**:
  - `58` (침대 사후여운): 침대에 똑바로 누워 탈진한 채 절정의 여운을 즐김, 시트에 고인 풍성한 체액 웅덩이 연출.
  - `59` (엎드린 사후여운): 침대에 앞으로 엎어져 베개에 얼굴을 묻은 채 탈진, 허벅지 사이로 흘러내리는 짙은 체액 연출 (`lying prone on bed, face down on pillow, exhausted aftersex aftermath, thick cum dripping between thighs`).
- **클라이맥스 체액 연출 강화**:
  - 절정 및 사후여운 씬(`41, 43, 45, 48, 51, 53, 54, 55, 58, 59`)에 `((excessive cum:1.35~1.4))`, `thick cum dripping...`, `cum pool` 등 Danbooru 모델에 직관적인 강조 태그를 전진 배치하여 시각적 몰입도 극대화.

### 3.6 오토코노코 씬 1:1 대칭 완비 및 특화 연출 (140~159)
- **일반 H-씬과 1:1 대응 (`+100` 오프셋)**:
  - `140~149`: 애널 선교/절정, 후배위/절정, 기승위/절정, 역기승위, 대면좌위, 매팅프레스(절정), 스푸닝.
  - `150 / 151` (상호자극 / 절정): 가슴밀착(파이즈리) 대신 침대 위 핸드잡 상호작용 및 사정 절정 연출 (`handjob receiving`, `handjob climax`, 손과 복부에 쏟아지는 풍성한 사정 연출).
  - `152 / 153` (구강 체인): 펠라치오 기본 봉사 및 얼굴/입술에 짙은 체액이 튀는 페이셜 클라이맥스.
  - `154 / 155` (이라마치오 & 구강 사후여운 신설):
    - `154`: 목 불룩 딥스로트 이라마치오, 눈물과 쾌락의 구역질 (`deepthroat irrumatio, throat bulge, tears streaming, gagging pleasure`).
    - `155`: 입가에서 흘러넘쳐 가슴과 허벅지로 흐르는 체액 범벅, 멍하니 풀린 눈의 탈진 여운.
  - `156 / 157` (입위 & 샤워 씬): 벽 밀착 서서 애널, 투명 유리 샤워부스 벽 밀착 애널.
  - `158 / 159` (사후여운 다각화):
    - `158` (침대 사후여운): 침대에 누워 탈진, 시트에 고인 짙은 체액 웅덩이 연출 대폭 강화.
    - `159` (엎드린 사후여운 신설): 침대에 앞으로 엎어져 베개에 얼굴을 묻고 탈진, 허벅지 사이로 흘러내리는 풍성한 체액 연출 (`lying prone on bed, ass slightly raised, thick cum dripping between thighs`).
- **상호작용 보장 매커니즘**:
  - `runner.py`에서 `yaoi, 2boys` 자동 주입 및 네거티브에서 `2boys`, `yaoi`, `multiple characters`까지 완전 필터링 배제하여 2인 결합 보장.

### 3.7 챗봇 AI 이미지 선택 최적화를 위한 범용 절정 라벨 설계
- **배경 문제**:
  - 다양한 체위(선교, 후배위, 기승위, 역기승위, 대면좌위, 매팅프레스, 스푸닝, 입위벽밀착, 샤워 등 9종)에 비해 개별 절정 씬이 1:1로 존재하지 않아, 챗봇 AI(LLM)가 대면좌위/스푸닝/역기승위/벽밀착 등의 체위 진행 후 매칭되는 절정 이미지를 찾지 못하고 엉뚱한 기본 이미지를 호출하거나 체위 이미지를 반복하는 문제 발생.
- **해결 매커니즘 (구도 기반 범용 명칭 + 포괄 체위 키워드 병기)**:
  - **41 / 141 (`정면절정(선교/대면)`)**: 누운 자세 및 마주보는 모든 정면 체위(선교체위, 대면좌위 등)의 범용 클라이맥스로 100% 매칭.
  - **43 / 143 (`후방절정(후배위/스푸닝/벽)`)**: 엎드리거나 뒤에서 결합하는 모든 후방 체위(후배위, 스푸닝, 입위벽밀착, 샤워 등)의 범용 클라이맥스로 100% 매칭.
  - **45 / 145 (`상위절정(기승위/역기승)`)**: 위에서 주도하거나 뒤돌아 올라탄 모든 상위 체위(기승위, 역기승위 등)의 범용 클라이맥스로 100% 매칭.
  - **48 / 148 (`밀착절정(매팅프레스)`)**: 기존 단순 `매팅프레스` 라벨을 절정 명칭으로 전환하여, 다리를 접어올린 초밀착 압착 클라이맥스로 명확히 인지.
  - **51 / 151**: `가슴절정(파이즈리)` / `상호절정(핸드잡)`.
  - **53 / 153**: `구강절정(페이셜)`, **54 / 154**: `이라마치오(딥스로트)`.
  - **사후여운 분기**:
    - **58 / 158 (`침대사후여운(누움)`)**: 정면/상위/밀착 절정 후 침대 정자세 탈진 여운.
    - **59 / 159 (`엎드린사후여운(엎드림)`)**: 후방/애널/격렬한 결합 후 침대에 앞으로 엎어져 쉬는 탈진 여운.
- **효과**: AI가 텍스트 지문이나 대화의 체위 키워드(`대면`, `스푸닝`, `역기승`, `벽` 등) 및 구도 키워드(`정면`, `후방`, `상위`, `밀착`) 중 어떤 단어를 보더라도 즉시 가장 자연스러운 절정 이미지를 오차 없이 매칭함.

---

## 4. 신규 프로젝트 및 캐릭터 설정 규칙 (`projects/don/` 등)

### 4.1 캐릭터 JSON 작성 표준
```json
{
  "prefix": "char_id",
  "default_mode": "female",
  "positive": "masterpiece, best quality, newest, absurdres, aesthetic illustration, delicate anime coloring, soft shaded skin, finely detailed beautiful eyes BREAK 1girl, solo, [체형 및 나이], [고정 헤어스타일], [고정 눈동자 색], [기본 의상 묘사]",
  "negative": "worst quality, low quality, bad anatomy, bad hands, 1boy, male, masculine, photorealistic, 3d, [반대 속성 차단 태그]",
  "ref_weight": 0.65
}
```

### 4.2 필수 준수 사항
1. **캐릭터 기본 프롬프트에 고정 표정 및 홍조(`blush`, `smile` 등) 절대 금지**:
   - 캐릭터 기본 JSON에 표정이 들어가면 감정 씬(000~020)의 다양한 표정(화남, 경멸, 슬픔 등)이 씹히고 항상 동일한 얼굴로 고정됨.
2. **IP-Adapter 참조 가중치(`ref_weight`)**:
   - `0.6` ~ `0.7` 권장 (기본 `0.65`). `0.8` 이상 시 포즈 제어력이 급격히 떨어짐.
3. **네거티브 안전 규격**:
   - `sweat, perspiration, liquid, splatter` 등은 파이프라인 단위 검사에서 금지 태그로 지정되어 있으므로 네거티브에 임의 추가 금지.

---

## 5. Antigravity 에이전트 작업 및 커뮤니케이션 규칙

1. **응답 언어**: 모든 대화, 설명, 보고는 **항상 한국어**로 작성.
2. **콘솔 출력 및 문서 안전 정책 준수**:
   - 터미널 출력(stdout) 및 응답 텍스트에 노골적인 성인물 단어, 신체 부위, 성적 행동 묘사 출력 절대 금지.
   - 항상 기술적, 사무적 용어(구도, 페어링 태그, 파트너 객체, 네거티브 필터링, 프레이밍 등) 사용.
3. **Git 커밋 규격**:
   - `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:` 접두어 사용 및 민감 표현 배제한 사무적 서술 준수.
4. **작업 완료 전 검증**:
   - 코드나 DB를 수정했을 경우 반드시 `python sd_batch_generator.py --test` 실행하여 52개 테스트 전원 통과 확인.

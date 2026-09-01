# references/

IP-Adapter 참조 이미지를 두는 폴더입니다.

## 파일명 규칙

`--prefix` 값과 같은 이름으로 둡니다. 확장자는 아래 순서로 탐색합니다.

```
.png → .jpg → .jpeg → .webp
```

```
references/
├── mika.png      →  --prefix mika 로 실행하면 자동 발견
├── ryu.webp      →  --prefix ryu
└── sei.jpg       →  --prefix sei
```

`--ref_image` 로 경로를 직접 지정하면 이 자동 탐색을 건너뜁니다.

## 파일이 없으면

경고만 출력하고 텍스트 프롬프트만으로 정상 생성됩니다. 에러가 아닙니다.

```
[WARN] 참조 이미지 없음 (references/mika.*) - 텍스트 프롬프트만 사용
```

00번을 먼저 생성해서 참조로 쓰는 워크플로우에서는 참조 이미지가 아직
없는 상태가 정상입니다.

## 권장 워크플로우 (자체 부트스트랩)

외부에서 그림을 구하지 않고 자체 생성물로 일관성을 확보하는 방법입니다.

```powershell
# 1. 00번만 생성
python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes" --mode 0

# 2. 결과 확인. 마음에 안 들면 지우고 다시 생성
#    Remove-Item generated_assets\mika\mika_00.webp

# 3. 확정된 이미지를 참조로 복사
Copy-Item generated_assets\mika\mika_00.webp references\mika.webp

# 4. 나머지를 IP-Adapter 적용해 생성
python sd_batch_generator.py --prefix mika --char_prompt "silver hair, blue eyes" --mode 1-19
```

## 참조 이미지 조건

| 항목 | 권장 | 이유 |
|---|---|---|
| 인물 수 | 1명 단독 | 여러 명이면 특징이 섞인다 |
| 구도 | 정면 또는 3/4 각도 | 얼굴 특징 추출에 유리 |
| 표정·포즈 | 중립 | 강한 포즈는 결과물에 전이된다 |
| 배경 | 단순 | 배경도 함께 참조된다 |
| 해상도 | 512~1024 충분 | IP-Adapter 가 내부적으로 축소하므로 4K 는 낭비 |
| 화풍 | 사용할 모델과 유사 | 실사 참조 + 애니 모델은 충돌한다 |

## git 추적 대상입니다

`.gitignore` 에서 제외하지 않습니다. 참조 이미지는 재생성할 수 없는 원본
입력이고, 여러 PC 를 오갈 때 함께 동기화되어야 합니다.

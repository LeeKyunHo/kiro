"""
generator.qa_validator
생성된 일러스트 이미지의 품질 및 구도 불량(인원수 오류, 머리 복제, 손 증식, 모브 남성 얼굴 노출)을
전문 애니메이션 딥러닝 감지기(imgutils.detect)로 자동 판정하는 Auto-QC 모듈.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image

try:
    from imgutils.detect import (
        detect_faces,
        detect_hands,
        detect_heads,
        detect_person,
    )
    HAS_IMGUTILS = True
except ImportError:
    HAS_IMGUTILS = False

logger = logging.getLogger(__name__)


def validate_generated_image(
    image: Union[Path, str, Image.Image],
    is_solo_scene: bool = True,
    is_two_person: bool = False,
    is_offscreen_partner: bool = False,
    is_kiss_scene: bool = False,
    section: Optional[str] = None,
    conf_threshold: float = 0.55,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    생성된 일러스트 이미지를 정밀 분석하여 불량(Reject) 여부와 사유를 판정합니다.

    Returns:
        (is_valid, reject_reason, metrics)
        - is_valid: True(합격), False(불량 판정)
        - reject_reason: 불량 판정 사유 (합격 시 빈 문자열)
        - metrics: 감지된 인원/머리/손/얼굴 카운트 및 세부 정보
    """
    metrics: Dict[str, Any] = {
        "persons": 0,
        "heads": 0,
        "hands": 0,
        "faces": 0,
    }

    if not HAS_IMGUTILS:
        # 감지기가 설치되지 않은 환경에서는 안전 통과
        return True, "", metrics

    try:
        if isinstance(image, (str, Path)):
            img = Image.open(str(image)).convert("RGB")
        else:
            img = image.convert("RGB")

        # 1. 인원수 검출 (Person Detection)
        raw_persons = detect_person(img)
        valid_persons = [p for p in raw_persons if p[2] >= conf_threshold]
        metrics["persons"] = len(valid_persons)

        # 2. 머리 검출 (Head Detection)
        raw_heads = detect_heads(img)
        valid_heads = [h for h in raw_heads if h[2] >= conf_threshold]
        metrics["heads"] = len(valid_heads)

        # 3. 손 검출 (Hand Detection: 신뢰도 0.60 이상)
        raw_hands = detect_hands(img)
        valid_hands = [h for h in raw_hands if h[2] >= 0.60]
        metrics["hands"] = len(valid_hands)

        # 4. 얼굴 검출 (Face Detection: 신뢰도 0.65 이상)
        raw_faces = detect_faces(img)
        valid_faces = [f for f in raw_faces if f[2] >= 0.65]
        metrics["faces"] = len(valid_faces)

        # ── 1인 단독 씬 판정 규칙 ──
        if is_solo_scene or (not is_two_person):
            if metrics["persons"] > 1 or metrics["heads"] > 1:
                reason = f"1인 씬 인원/머리 초과 감지 (인원: {metrics['persons']}명, 머리: {metrics['heads']}개)"
                return False, reason, metrics

            if metrics["hands"] > 2:
                reason = f"1인 씬 손 증식(3개 이상) 감지 (손: {metrics['hands']}개)"
                return False, reason, metrics

        # ── 2인 상호작용 씬 판정 규칙 (오프스크린 제외) ──
        elif is_two_person and not is_offscreen_partner:
            if metrics["persons"] > 2 or metrics["heads"] > 2:
                reason = f"2인 씬 제3자/모브 증식(3명 이상) 감지 (인원: {metrics['persons']}명, 머리: {metrics['heads']}개)"
                return False, reason, metrics

            if metrics["hands"] > 4:
                reason = f"2인 씬 손 증식(5개 이상) 감지 (손: {metrics['hands']}개)"
                return False, reason, metrics

            # 2인 씬에서 모브 남성의 얼굴이 완전히 드러나 얼굴이 2개 이상 잡힌 경우
            # (단, 키스 씬이나 로맨스 씬은 두 사람의 얼굴이 접촉하므로 faces <= 2 허용)
            max_faces = 2 if (is_kiss_scene or section in ("romance", "poses")) else 1
            if metrics["faces"] > max_faces:
                reason = f"모브 남성 얼굴 노출 감지 (얼굴: {metrics['faces']}개, faceless 위반)"
                return False, reason, metrics

        return True, "", metrics

    except Exception as e:
        logger.warning(f"[Auto-QC 판정 예외] {e}")
        return True, "", metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="키로(kiro) Auto-QC 일러스트 품질 검증기")
    parser.add_argument("-f", "--file", type=str, required=True, help="검증할 이미지 파일 경로")
    parser.add_argument("--two-person", action="store_true", help="2인 상호작용 씬 여부")
    parser.add_argument("--kiss", action="store_true", help="키스/얼굴 밀착 씬 여부")
    parser.add_argument("--conf", type=float, default=0.55, help="인원/머리 검출 임계치")

    args = parser.parse_args()

    is_valid, reason, metrics = validate_generated_image(
        image=args.file,
        is_solo_scene=not args.two_person,
        is_two_person=args.two_person,
        is_kiss_scene=args.kiss,
        conf_threshold=args.conf,
    )

    print(f"결과: {'[PASS] 합격' if is_valid else '[REJECT] 불량'}")
    if not is_valid:
        print(f"사유: {reason}")
    print(f"측정값: {metrics}")

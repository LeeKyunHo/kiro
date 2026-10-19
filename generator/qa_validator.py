"""
plepa_engine.qa_validator
생성된 일러스트 이미지의 품질 및 구도 불량(인원수 오류, 머리 복제, 손 증식, 모브 얼굴 노출)을
전문 애니메이션 딥러닝 감지기(imgutils.detect)로 자동 판정하는 Auto-QC 모듈.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

# ──────────────────────────────────────────────────────────────────────────────
# 1. CPU 스레드 자동 제한 (전체 스레드 수 - 2) & 2. 프로세스 우선순위 하향 (Below Normal)
# 딥러닝 감지기 추론 중 CPU 100% 점유로 인한 윈도우 UI/마우스/브라우저 버벅임을 원천 방지합니다.
# ──────────────────────────────────────────────────────────────────────────────
usable_threads = max(2, (os.cpu_count() or 4) - 2)
os.environ["OMP_NUM_THREADS"] = str(usable_threads)
os.environ["MKL_NUM_THREADS"] = str(usable_threads)

try:
    import torch
    torch.set_num_threads(usable_threads)
except Exception:
    pass

try:
    import psutil
    p = psutil.Process(os.getpid())
    p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:
    pass

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
    prompt: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    생성된 이미지를 정밀 분석하여 불량(Reject) 여부와 사유를 판정합니다.

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
        # 감지기가 없으면 안전 통과
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

            # 1인칭 손 개입 씬(쓰다듬기 047, 덮쳐짐 040 등 유저 손 개입) 허용치 완화
            p_text = (prompt or "").lower()
            pov_hand_keywords = ("headpat", "patting head", "hand on head", "hands pressed on mattress", "pov")
            is_pov_hand_scene = any(kw in p_text for kw in pov_hand_keywords)
            max_hands = 4 if is_pov_hand_scene else 2

            if metrics["hands"] > max_hands:
                reason = f"1인 씬 손 증식({max_hands + 1}개 이상) 감지 (손: {metrics['hands']}개)"
                return False, reason, metrics

        # ── 2인 상호작용 씬 판정 규칙 (오프스크린 제외) ──
        elif is_two_person and not is_offscreen_partner:
            if metrics["persons"] > 2 or metrics["heads"] > 2:
                reason = f"2인 씬 제3자/남성 증식(3명 이상) 감지 (인원: {metrics['persons']}명, 머리: {metrics['heads']}개)"
                return False, reason, metrics

            if metrics["hands"] > 4:
                reason = f"2인 씬 손 증식(5개 이상) 감지 (손: {metrics['hands']}개)"
                return False, reason, metrics

            # 2인 씬에서 제3자의 얼굴까지 노출되는 것을 방지 (2명 씬이므로 faces <= 2 허용)
            max_faces = 2
            if metrics["faces"] > max_faces:
                reason = f"2인 씬 얼굴 과다 노출 감지 (얼굴: {metrics['faces']}개, 3인 이상 위반)"
                return False, reason, metrics

        return True, "", metrics

    except Exception as e:
        logger.warning(f"[Auto-QC 판정 예외] {e}")
        return True, "", metrics

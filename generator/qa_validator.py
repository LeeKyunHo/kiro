"""
generator.qa_validator
생성된 일러스트 이미지의 품질 및 구도 불량(Color Burn, 화각 드리프트, 피사체 증발,
인원수 오류, 머리 복제, 손 증식, 모브 얼굴 노출)을 Short-Circuit 방식으로 자동 판정하는 Auto-QC 모듈.
모듈 임포트 시 torch/imgutils 를 로드하지 않고 최초 실행 시 1회만 Lazy Load 한다.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from PIL import Image

logger = logging.getLogger(__name__)

_RUNTIME_INITIALIZED: bool = False
HAS_IMGUTILS: Optional[bool] = None
HAS_LAPLACIAN: Optional[bool] = None
HAS_AESTHETIC: Optional[bool] = None

detect_person: Any = None
detect_heads: Any = None
detect_hands: Any = None
detect_faces: Any = None
laplacian_score: Any = None
anime_dbaesthetic: Any = None

_HEADLESS_LOCAL_KEYWORDS: tuple[str, ...] = (
    "crotch focus",
    "ass focus",
    "foot focus",
    "feet focus",
    "pussy focus",
    "penis focus",
    "anal focus",
    "lower body",
    "pov crotch",
    "headless",
    "head out of frame",
    "cropped head",
    "eye focus",
    "mouth focus",
    "hand focus",
)

_CLOSEUP_ASPECT_KEYWORDS: tuple[str, ...] = (
    "face focus",
    "bust shot",
    "chest focus",
    "upper body",
    "tight bust",
    "intimate close-up",
    "portrait",
    "cowboy shot",
)

_POV_MULTI_HAND_PHRASES: tuple[str, ...] = (
    "pov",
    "headpat",
    "patting head",
    "patting her head",
    "stroking hair",
    "hand on head",
    "hands pressed on mattress",
    "pov hands",
    "paizuri",
    "breast grab",
    "grabbing",
    "hands cupping",
    "handjob",
    "caressing",
    "massage",
    "groping",
    "two hands",
)

_NIGHT_LIGHTING_KEYWORDS: tuple[str, ...] = (
    "night",
    "evening",
    "starry",
    "neon",
    "moonlight",
    "nighttime",
    "city lights",
    "darkness",
    "dimly lit",
)

_DARK_LIGHTING_RE = re.compile(
    r"\bdark\b(?!\s+(?:hair|purple|blue|red|green|brown|skin|eyes|dress|clothes|suit|coat|skirt|lingerie|bra|panties|stockings|thighhighs))",
    flags=re.IGNORECASE,
)


def _is_night_lighting_prompt(p_text: str) -> bool:
    """야간/어두운 조명 씬인지 판별한다(dark hair 등 외형 컬러 오매칭 배제)."""
    if any(kw in p_text for kw in _NIGHT_LIGHTING_KEYWORDS):
        return True
    return bool(_DARK_LIGHTING_RE.search(p_text))


def _is_headless_closeup_prompt(p_text: str) -> bool:
    """하반신/국소 클로즈업 등 머리가 프레임 밖에 있을 수 있는 프롬프트인지 판별한다."""
    if any(kw in p_text for kw in _HEADLESS_LOCAL_KEYWORDS):
        return True
    if "close-up" in p_text and not any(
        kw in p_text for kw in ("face focus", "upper body", "bust shot", "portrait", "cowboy shot")
    ):
        return True
    return False


def _compute_color_burn_ratio(img: Image.Image) -> float:
    """
    256x256 다운샘플링 기반으로 극단적 고채도 네온 과포화 및 RGB 채널 클리핑(Color Burn) 비율을 측정한다.
    순백 배경(S=0)과 어두운 밤 배경(low V / low delta)은 오탐에서 배제한다.
    """
    small = img.resize((256, 256), Image.Resampling.BILINEAR)
    try:
        import numpy as np

        arr = np.asarray(small, dtype=np.uint8)
        max_c = arr.max(axis=2)
        min_c = arr.min(axis=2)
        delta = max_c.astype(np.int16) - min_c.astype(np.int16)

        sat = np.zeros_like(max_c, dtype=np.float32)
        nz = max_c > 0
        sat[nz] = delta[nz].astype(np.float32) / max_c[nz].astype(np.float32)
        val = max_c.astype(np.float32) / 255.0

        neon_mask = (sat > 0.94) & (val >= 0.35)
        clip_mask = (max_c >= 254) & (min_c <= 1) & (delta >= 200)
        return float((neon_mask | clip_mask).mean())
    except ImportError:
        pixels = list(small.getdata())
        if not pixels:
            return 0.0
        burned = 0
        for r, g, b in pixels:
            mx = max(r, g, b)
            mn = min(r, g, b)
            d = mx - mn
            sat = (d / mx) if mx > 0 else 0.0
            val = mx / 255.0
            if (sat > 0.94 and val >= 0.35) or (mx >= 254 and mn <= 1 and d >= 200):
                burned += 1
        return burned / len(pixels)


def _check_closeup_aspect_drift(
    img_h: int,
    valid_faces: list[Any],
    p_text: str,
    metrics: Dict[str, Any],
    min_face_ratio: float = 0.08,
) -> Optional[str]:
    """상반신·포트레이트·클로즈업 씬에서 전신 롱샷으로 이탈하는 화각 드리프트를 검사한다."""
    if not valid_faces or not any(kw in p_text for kw in _CLOSEUP_ASPECT_KEYWORDS):
        return None
    max_face_h = max((f[0][3] - f[0][1]) for f in valid_faces)
    face_ratio = max_face_h / max(1, img_h)
    metrics["face_height_ratio"] = round(face_ratio, 3)
    if face_ratio < min_face_ratio:
        return (
            f"상반신/클로즈업 씬 화각 드리프트(전신 롱샷 이탈) 감지 "
            f"(얼굴 비율: {face_ratio * 100:.1f}% < {min_face_ratio * 100:.1f}%)"
        )
    return None


def _ensure_qa_runtime() -> bool:
    """
    Auto-QC 최초 실행 시 1회만 CPU 스레드 제한, 프로세스 우선순위 하향,
    imgutils 감지기 로드를 수행한다 (모듈 임포트 시 부수효과 원천 방지).
    """
    global _RUNTIME_INITIALIZED, HAS_IMGUTILS
    global detect_person, detect_heads, detect_hands, detect_faces

    # 단위 테스트 등에서 감지기가 이미 외부 주입된 경우 ML 런타임 로드 생략
    if (
        HAS_IMGUTILS is True
        and detect_person is not None
        and detect_heads is not None
        and detect_hands is not None
        and detect_faces is not None
    ):
        return True

    if _RUNTIME_INITIALIZED:
        return bool(HAS_IMGUTILS)

    usable_threads = max(2, (os.cpu_count() or 4) - 2)
    os.environ.setdefault("OMP_NUM_THREADS", str(usable_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(usable_threads))

    try:
        import torch
        torch.set_num_threads(usable_threads)
    except Exception as e:
        logger.debug(f"[Auto-QC] PyTorch 스레드 제한 생략: {e}")

    try:
        import psutil
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            p = psutil.Process(os.getpid())
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception as e:
        logger.debug(f"[Auto-QC] 프로세스 우선순위 조정 생략: {e}")

    try:
        from imgutils.detect import (
            detect_faces as _detect_faces,
            detect_hands as _detect_hands,
            detect_heads as _detect_heads,
            detect_person as _detect_person,
        )
        if detect_person is None:
            detect_person = _detect_person
        if detect_heads is None:
            detect_heads = _detect_heads
        if detect_hands is None:
            detect_hands = _detect_hands
        if detect_faces is None:
            detect_faces = _detect_faces
        HAS_IMGUTILS = True
    except ImportError:
        HAS_IMGUTILS = False

    _RUNTIME_INITIALIZED = True
    return bool(HAS_IMGUTILS)


def _ensure_blur_metric() -> bool:
    """opt-in check_blur 활성화 시에만 imgutils.metrics.laplacian_score 를 Lazy Import 한다."""
    global HAS_LAPLACIAN, laplacian_score
    if HAS_LAPLACIAN is True and laplacian_score is not None:
        return True
    if HAS_LAPLACIAN is False:
        return False
    try:
        from imgutils.metrics import laplacian_score as _laplacian_score
        if laplacian_score is None:
            laplacian_score = _laplacian_score
        HAS_LAPLACIAN = True
    except ImportError:
        HAS_LAPLACIAN = False
    return bool(HAS_LAPLACIAN)


def _ensure_aesthetic_metric() -> bool:
    """opt-in check_aesthetic 활성화 시에만 imgutils.metrics.anime_dbaesthetic 를 Lazy Import 한다."""
    global HAS_AESTHETIC, anime_dbaesthetic
    if HAS_AESTHETIC is True and anime_dbaesthetic is not None:
        return True
    if HAS_AESTHETIC is False:
        return False
    try:
        from imgutils.metrics import anime_dbaesthetic as _anime_dbaesthetic
        if anime_dbaesthetic is None:
            anime_dbaesthetic = _anime_dbaesthetic
        HAS_AESTHETIC = True
    except ImportError:
        HAS_AESTHETIC = False
    return bool(HAS_AESTHETIC)


def validate_generated_image(
    image: Union[Path, str, Image.Image],
    is_solo_scene: bool = True,
    is_two_person: bool = False,
    is_offscreen_partner: bool = False,
    is_kiss_scene: bool = False,
    section: Optional[str] = None,
    conf_threshold: float = 0.55,
    prompt: Optional[str] = None,
    check_blur: bool = False,
    min_blur_score: float = 60.0,
    check_aesthetic: bool = False,
    min_aesthetic_score: float = 4.0,
    check_color_burn: bool = True,
    max_color_burn_ratio: float = 0.22,
    require_subject: Optional[bool] = None,
    is_faceless_partner: Optional[bool] = None,
    **kwargs: Any,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    생성된 이미지를 Short-Circuit 방식으로 정밀 분석하여 불량(Reject) 여부와 사유를 판정한다.

    Args:
        image: 검사할 이미지(Path, 파일 경로 문자열 또는 PIL Image).
        is_solo_scene: 1인 단독 씬 여부.
        is_two_person: 2인 상호작용 씬 여부.
        is_offscreen_partner: 파트너가 화면 밖에 위치하는 POV 씬 여부.
        is_kiss_scene: 키스 씬 여부.
        section: 포즈 DB 섹션명 (호환성 유지용 메타데이터).
        conf_threshold: 사람/머리 감지기 신뢰도 임계치.
        prompt: 화각 드리프트·야간 예외·POV 손 개입·headless 판별에 사용하는 포즈 프롬프트.
        check_blur: opt-in 선명도(Laplacian) 검사 활성화 여부 (기본 False).
        min_blur_score: 선명도 하한 임계치.
        check_aesthetic: opt-in 미적 점수(anime_dbaesthetic) 검사 활성화 여부 (기본 False).
        min_aesthetic_score: 미적 점수 하한 임계치.
        check_color_burn: 색감 과포화/RGB 클리핑(Color Burn) 검사 여부 (기본 True).
        max_color_burn_ratio: 허용 최대 Color Burn 비율 (기본 0.22, 야간 씬 0.35 완화).
        require_subject: 피사체 증발 감지 활성화 여부 (None 시 prompt 존재 여부로 자동 결정).
        is_faceless_partner: 2인 씬 파트너 faceless 제약 여부 (None 시 prompt 내 faceless 키워드 자동 판별).

    Returns:
        (is_valid, reject_reason, metrics)
    """
    metrics: Dict[str, Any] = {
        "persons": 0,
        "heads": 0,
        "hands": 0,
        "faces": 0,
        "blur_score": 0.0,
        "color_burn_ratio": 0.0,
    }

    if not _ensure_qa_runtime():
        return True, "", metrics

    try:
        if isinstance(image, (str, Path)):
            with Image.open(str(image)) as raw_im:
                img = raw_im.convert("RGB")
        else:
            img = image.convert("RGB")

        p_text = (prompt or "").lower()
        eff_require_subject = bool(p_text.strip()) if require_subject is None else bool(require_subject)
        headless_allowed = _is_headless_closeup_prompt(p_text)

        # 0. 선명도(opt-in), 미적 점수(opt-in), 색감 과포화(Color Burn) 검사 (Short-Circuit)
        if check_blur and _ensure_blur_metric():
            try:
                b_score = float(laplacian_score(img))
                metrics["blur_score"] = round(b_score, 2)
                if b_score < min_blur_score:
                    reason = f"이미지 블러/초점 이탈 감지 (선명도: {b_score:.1f} < 임계치 {min_blur_score})"
                    return False, reason, metrics
            except Exception as e:
                logger.debug(f"[Auto-QC] 선명도 계산 예외: {e}")

        if check_aesthetic and _ensure_aesthetic_metric():
            try:
                a_score = float(anime_dbaesthetic(img))
                metrics["aesthetic_score"] = round(a_score, 2)
                if a_score < min_aesthetic_score:
                    reason = f"이미지 미적 완성도 미달 감지 (점수: {a_score:.2f} < 임계치 {min_aesthetic_score})"
                    return False, reason, metrics
            except Exception as e:
                logger.debug(f"[Auto-QC] 미적 점수 계산 예외: {e}")

        if check_color_burn:
            burn_ratio = _compute_color_burn_ratio(img)
            metrics["color_burn_ratio"] = round(burn_ratio, 4)
            is_night_scene = _is_night_lighting_prompt(p_text)
            eff_max_burn = (
                0.35 if (is_night_scene and max_color_burn_ratio <= 0.22) else max_color_burn_ratio
            )
            if burn_ratio >= eff_max_burn:
                reason = (
                    f"색감 과포화(Color Burn) 감지 "
                    f"(과포화/클리핑 비율: {burn_ratio * 100:.1f}% >= {eff_max_burn * 100:.1f}%)"
                )
                return False, reason, metrics

        is_solo = is_solo_scene or (not is_two_person)
        is_kiss = bool(is_kiss_scene) or any(kw in p_text for kw in ("kiss", "french kiss", "deep kiss"))
        is_nursing = any(
            kw in p_text for kw in ("nursing handjob", "breastfeeding", "sucking on nipple", "mouth on breast")
        )
        is_close_up_focus = any(
            kw in p_text
            for kw in (
                "face focus",
                "bust shot",
                "chest focus",
                "upper body close-up",
                "tight bust",
                "intimate close-up",
                "close-up upper body",
            )
        )

        # 1. 인원수 검출 (Person Detection -> 초과/증발 시 후속 ONNX 모델 Short-Circuit)
        raw_persons = detect_person(img)
        valid_persons = [p for p in raw_persons if p[2] >= conf_threshold]
        metrics["persons"] = len(valid_persons)

        if is_solo and metrics["persons"] > 1:
            return False, f"1인 씬 인원/머리 초과 감지 (인원: {metrics['persons']}명)", metrics
        if not is_solo and metrics["persons"] > 2:
            return False, f"2인 씬 제3자/남성 증식(3명 이상) 감지 (인원: {metrics['persons']}명)", metrics
        if eff_require_subject and not headless_allowed and metrics["persons"] == 0:
            return False, "피사체 증발/구도 붕괴 감지 (인원 미검출)", metrics

        # 2. 머리 검출 (Head Detection -> 초과 시 후속 ONNX 모델 Short-Circuit)
        raw_heads = detect_heads(img)
        valid_heads = [h for h in raw_heads if h[2] >= conf_threshold]
        metrics["heads"] = len(valid_heads)

        is_head_contact_allowed = is_kiss or is_nursing or any(
            kw in p_text
            for kw in (
                "back of male head", "back of dark-haired male head", "head between thighs",
                "burying face", "face buried", "head buried", "buried in her chest",
                "princess carry", "carried in dark-haired male", "carried in male partner",
                "back hug", "hugging partner", "hug from behind", "grabbing from behind",
                "dark-haired male mob",
            )
        )

        if is_solo:
            if metrics["heads"] > 1:
                return (
                    False,
                    f"1인 씬 인원/머리 초과 감지 (인원: {metrics['persons']}명, 머리: {metrics['heads']}개)",
                    metrics,
                )
        elif is_two_person and is_offscreen_partner:
            max_offscreen_heads = 2 if is_head_contact_allowed else 1
            if metrics["heads"] > max_offscreen_heads:
                return (
                    False,
                    f"오프스크린 파트너 씬 머리 초과 감지 (머리: {metrics['heads']}개, 최대 {max_offscreen_heads}개 허용)",
                    metrics,
                )
        else:
            if is_close_up_focus and not is_kiss and not is_nursing and metrics["heads"] > 1:
                return (
                    False,
                    f"상반신 밀착 씬 모브 머리 침범 감지 (머리: {metrics['heads']}개, 단독 1인 머리만 허용)",
                    metrics,
                )
            if metrics["heads"] > 2:
                return (
                    False,
                    f"2인 씬 제3자/남성 증식(3명 이상) 감지 (인원: {metrics['persons']}명, 머리: {metrics['heads']}개)",
                    metrics,
                )

        # 3. 얼굴 검출 (Face Detection -> 증발/모브 노출/화각 드리프트 시 detect_hands Short-Circuit)
        raw_faces = detect_faces(img)
        valid_faces = [f for f in raw_faces if f[2] >= 0.65]
        metrics["faces"] = len(valid_faces)

        if eff_require_subject and not headless_allowed and metrics["heads"] == 0 and metrics["faces"] == 0:
            return False, "피사체 증발/구도 붕괴 감지 (머리·얼굴 동시 미검출)", metrics

        if is_solo:
            if metrics["faces"] > 1:
                return False, f"1인 씬 얼굴 초과 감지 (얼굴: {metrics['faces']}개)", metrics
        elif is_two_person and is_offscreen_partner:
            max_offscreen_faces = 2 if (is_kiss or is_nursing) else 1
            if metrics["faces"] > max_offscreen_faces:
                return (
                    False,
                    f"오프스크린 파트너 씬 모브 얼굴 노출 감지 (얼굴: {metrics['faces']}개, 단독 {max_offscreen_faces}인 얼굴만 허용)",
                    metrics,
                )
        elif not is_solo:
            if is_close_up_focus and not is_kiss and not is_nursing and metrics["faces"] > 1:
                return (
                    False,
                    f"상반신 밀착 씬 모브 얼굴 침범 감지 (얼굴: {metrics['faces']}개, 단독 1인 얼굴만 허용)",
                    metrics,
                )

            eff_faceless = (
                bool(is_faceless_partner)
                if is_faceless_partner is not None
                else (("faceless male" in p_text) or ("faceless" in p_text))
            )
            max_faces = 1 if (eff_faceless and not is_kiss) else 2
            if metrics["faces"] > max_faces:
                if max_faces == 1:
                    return (
                        False,
                        f"2인 씬 모브/파트너 얼굴 노출(faceless 위반) 감지 (얼굴: {metrics['faces']}개)",
                        metrics,
                    )
                return (
                    False,
                    f"2인 씬 얼굴 과다 노출 감지 (얼굴: {metrics['faces']}개, 3인 이상 위반)",
                    metrics,
                )

        if not headless_allowed:
            aspect_err = _check_closeup_aspect_drift(img.size[1], valid_faces, p_text, metrics)
            if aspect_err:
                return False, aspect_err, metrics

        # 4. 손 검출 (Hand Detection: 신뢰도 0.60 이상)
        raw_hands = detect_hands(img)
        valid_hands = [h for h in raw_hands if h[2] >= 0.60]
        metrics["hands"] = len(valid_hands)

        if is_solo:
            is_pov_hand_scene = any(kw in p_text for kw in _POV_MULTI_HAND_PHRASES)
            max_hands = 4 if is_pov_hand_scene else 2
            if metrics["hands"] > max_hands:
                return False, f"1인 씬 손 증식({max_hands + 1}개 이상) 감지 (손: {metrics['hands']}개)", metrics
        else:
            if metrics["hands"] > 4:
                return False, f"2인 씬 손 증식(5개 이상) 감지 (손: {metrics['hands']}개)", metrics

        return True, "", metrics

    except Exception as e:
        logger.warning(f"[Auto-QC 판정 예외] {e}")
        metrics["error"] = str(e)
        metrics["qc_bypassed"] = True
        return True, "", metrics

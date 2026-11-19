"""
generator.censor
2D 애니메이션 일러스트 전용 자동 검열(Censorship) 단일 코어 모듈 (SSOT).
- 렌더링 스타일 함수들은 generator.censor_styles 로 분리하고 하위 호환을 위해 모두 재노출한다.
- 스레드 안전 MobileSAM 싱글톤(_SAM_MODEL_LOCK), ROI 패딩 크롭 추론, VRAM 해제(unload_censor_models),
  2차 사후 검증/치유 게이트(verify_and_heal_censor), Windows 파일락 안전 교체(_atomic_replace_with_retry)를 제공한다.
"""

from __future__ import annotations

import gc
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, List, Optional, Sequence, Tuple, Union
import uuid

import cv2
import numpy as np
from PIL import Image

from generator.censor_styles import (
    _BASE_RESOLUTION,
    _compute_scale_factor,
    _estimate_penis_axis_and_base,
    _odd_kernel,
    _render_censored_style as _dispatch_censor_style,
    _restore_alpha,
    _scale_int,
    _split_rgb_and_alpha,
    apply_debug_bbox,
    apply_manga_bar,
    apply_manga_glow,
    apply_mosaic,
    apply_shadow_silhouette,
    apply_slim_black_bar,
    apply_wide_solid_bar,
    get_skin_mask,
)

logger = logging.getLogger(__name__)

try:
    from imgutils.detect import detect_censors
    HAS_IMGUTILS = True
except ImportError:
    HAS_IMGUTILS = False

# MobileSAM 모델 싱글톤 (Lazy Load, Thread-Safe)
_SAM_MODEL = None
_SAM_MODEL_LOCK = threading.Lock()


def _extract_sam_mask_data(results: Any) -> Optional[np.ndarray]:
    """SAM 추론 결과에서 마스크 numpy 배열을 안전하게 추출한다 (빈 텐서·테스트 모의 객체 겸용)."""
    if not results or getattr(results[0], "masks", None) is None:
        return None
    raw_data = getattr(results[0].masks, "data", None)
    if raw_data is None:
        return None
    arr = raw_data.cpu().numpy() if hasattr(raw_data, "cpu") else np.asarray(raw_data)
    if arr is None or len(arr) == 0:
        return None
    return arr


def get_sam_model():
    """MobileSAM 세그멘테이션 모델을 스레드 안전하게 싱글톤 로드한다."""
    global _SAM_MODEL
    if _SAM_MODEL is not None:
        return _SAM_MODEL if _SAM_MODEL is not False else None

    with _SAM_MODEL_LOCK:
        if _SAM_MODEL is None:
            try:
                from ultralytics import SAM
                _SAM_MODEL = SAM("mobile_sam.pt")
                logger.info("[Censor] MobileSAM 모델 로드 완료 (mobile_sam.pt)")
            except Exception as e:
                logger.warning(f"[Censor] MobileSAM 로드 실패, OpenCV 폴백 모드로 동작합니다: {e}")
                _SAM_MODEL = False
    return _SAM_MODEL if _SAM_MODEL is not False else None


def unload_censor_models() -> None:
    """로드된 MobileSAM 싱글톤과 GPU VRAM 캐시를 안전하게 해제한다."""
    global _SAM_MODEL
    with _SAM_MODEL_LOCK:
        _SAM_MODEL = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _atomic_replace_with_retry(
    temp_dst: Path,
    target_dst: Path,
    retries: int = 5,
    delay: float = 0.08,
) -> None:
    """Windows 파일 락 일시 충돌 시 백오프 재시도로 원자적 os.replace 를 수행한다."""
    last_err: Optional[OSError] = None
    total_attempts = max(1, int(retries))
    for attempt in range(total_attempts):
        try:
            os.replace(temp_dst, target_dst)
            return
        except (PermissionError, OSError) as err:
            last_err = err
            if attempt + 1 < total_attempts:
                time.sleep(delay * (2 ** attempt))
    if last_err is not None:
        raise last_err


def _run_sam_on_roi(
    rgb_img: Image.Image,
    sam: Any,
    box: Tuple[int, int, int, int],
    scale: float,
) -> Optional[np.ndarray]:
    """전체 고해상도 이미지 대신 패딩된 ROI 크롭 영역만 MobileSAM 으로 추론해 원본 좌표 마스크를 반환한다."""
    w, h = rgb_img.size
    ex1, ey1, ex2, ey2 = [int(v) for v in box]
    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
    pad = max(_scale_int(32, scale, minimum=16), int(max(ebw, ebh) * 0.25))

    rx1 = max(0, ex1 - pad)
    ry1 = max(0, ey1 - pad)
    rx2 = min(w, ex2 + pad)
    ry2 = min(h, ey2 + pad)
    cw, ch = max(1, rx2 - rx1), max(1, ry2 - ry1)

    crop_pil = rgb_img.crop((rx1, ry1, rx2, ry2))
    local_bbox = [ex1 - rx1, ey1 - ry1, ex2 - rx1, ey2 - ry1]

    with _SAM_MODEL_LOCK:
        results = sam(crop_pil, bboxes=[local_bbox], verbose=False)

    m_data = _extract_sam_mask_data(results)
    if m_data is None:
        return None

    crop_m = (m_data[0].astype(np.float32) * 255.0).clip(0, 255).astype(np.uint8)
    if crop_m.shape != (ch, cw):
        crop_m = cv2.resize(crop_m, (cw, ch), interpolation=cv2.INTER_NEAREST)

    full_m = np.zeros((h, w), dtype=np.uint8)
    full_m[ry1:ry2, rx1:rx2] = crop_m
    return full_m


def apply_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    color: Union[str, Tuple[int, int, int]] = "black",
) -> Image.Image:
    """
    실제 선화 외곽선(Contour)을 따라 내부만 솔리드 컬러(#000000 또는 #FFFFFF)로 채우는 실루엣 검열을 렌더링한다.
    - MobileSAM 추론 시 ROI 패딩 크롭 영역만 추론하여 고해상도 연산량·VRAM 사용량을 절감.
    - 832px 기준 동적 해상도 스케일 커널 및 알파 채널 보존 적용.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    w, h = rgb_img.size
    scale = _compute_scale_factor(w, h)
    img_np = np.array(rgb_img)
    final_mask = np.zeros((h, w), dtype=np.uint8)

    sam = get_sam_model()

    # 0. 손(Hands), 얼굴(Faces), 머리/헤어(Heads) 보호 마스크 추출 (ROI 크롭 추론)
    protection_mask = np.zeros((h, w), dtype=np.uint8)
    try:
        from imgutils.detect import detect_faces, detect_hands, detect_heads
        detected_hands = detect_hands(rgb_img, conf_threshold=0.25)
        detected_faces = detect_faces(rgb_img, conf_threshold=0.25)
        detected_heads = detect_heads(rgb_img, conf_threshold=0.25)

        overlapping_protect_boxes: List[Tuple[int, int, int, int]] = []
        for item in boxes_with_labels:
            bx1, by1, bx2, by2 = item[0]
            for (hx1, hy1, hx2, hy2), _, _ in detected_hands:
                if max(bx1, hx1) < min(bx2, hx2) and max(by1, hy1) < min(by2, hy2):
                    overlapping_protect_boxes.append((int(hx1), int(hy1), int(hx2), int(hy2)))
            for (fx1, fy1, fx2, fy2), _, _ in detected_faces:
                if max(bx1, fx1) < min(bx2, fx2) and max(by1, fy1) < min(by2, fy2):
                    overlapping_protect_boxes.append((int(fx1), int(fy1), int(fx2), int(fy2)))
            for (head_x1, head_y1, head_x2, head_y2), _, _ in detected_heads:
                if max(bx1, head_x1) < min(bx2, head_x2) and max(by1, head_y1) < min(by2, head_y2):
                    overlapping_protect_boxes.append((int(head_x1), int(head_y1), int(head_x2), int(head_y2)))

        if sam is not None and overlapping_protect_boxes:
            for p_box in overlapping_protect_boxes:
                try:
                    pm = _run_sam_on_roi(rgb_img, sam, p_box, scale)
                    if pm is not None:
                        px1, py1, px2, py2 = p_box
                        crop_p = np.zeros_like(pm)
                        crop_p[max(0, py1):min(h, py2), max(0, px1):min(w, px2)] = 255
                        pm = cv2.bitwise_and(pm, crop_p)
                        protection_mask = np.maximum(protection_mask, pm)
                except Exception as p_err:
                    logger.debug(f"[Censor] 보호 영역 세그멘테이션 예외 (무시): {p_err}")
    except Exception as pe:
        logger.debug(f"[Censor] 보호 영역 검출 생략: {pe}")

    min_contour_area = max(120.0, 300.0 * (scale ** 2))

    for item in boxes_with_labels:
        box = item[0]
        label = str(item[1]) if len(item) >= 2 else "penis"
        x1, y1, x2, y2 = [int(v) for v in box]
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)

        if label == "penis":
            m_x = 0.04
            m_top = 0.02
            m_bot = 0.08 if bh > bw * 1.3 else 0.02
            ex1 = max(0, int(x1 - bw * m_x))
            ey1 = max(0, int(y1 - bh * m_top))
            ex2 = min(w, int(x2 + bw * m_x))
            ey2 = min(h, int(y2 + bh * m_bot))
        else:
            m_x = 0.02
            m_bot = 0.03
            ex1 = max(0, int(x1 - bw * m_x))
            ey1 = max(0, y1)
            ex2 = min(w, int(x2 + bw * m_x))
            ey2 = min(h, int(y2 + bh * m_bot))

        mask_obtained = False

        # 1. MobileSAM ROI 크롭 추론 시도
        if sam is not None:
            try:
                sub_m = _run_sam_on_roi(rgb_img, sam, (ex1, ey1, ex2, ey2), scale)
                if sub_m is not None:
                    crop_mask = np.zeros_like(sub_m)
                    crop_mask[ey1:ey2, ex1:ex2] = 255
                    sub_m = cv2.bitwise_and(sub_m, crop_mask)

                    contours, _ = cv2.findContours(sub_m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    if contours:
                        max_area = max(cv2.contourArea(c) for c in contours)
                        cleaned_sub_m = np.zeros_like(sub_m)
                        for c in contours:
                            c_area = cv2.contourArea(c)
                            if c_area >= min_contour_area and c_area >= max_area * 0.05:
                                cv2.drawContours(cleaned_sub_m, [c], -1, 255, -1)
                        sub_m = cleaned_sub_m

                    if np.count_nonzero(sub_m) > 0:
                        final_mask = np.maximum(final_mask, sub_m)
                        mask_obtained = True
            except Exception as e:
                logger.warning(f"[Censor] MobileSAM ROI 추론 실패, OpenCV 폴백: {e}")

        # 2. SAM 실패 시 OpenCV GrabCut 전경 마스크 폴백
        if not mask_obtained:
            try:
                roi = img_np[ey1:ey2, ex1:ex2]
                if roi.size > 0 and (ex2 - ex1) >= 6 and (ey2 - ey1) >= 6:
                    bgd_model = np.zeros((1, 65), np.float64)
                    fgd_model = np.zeros((1, 65), np.float64)
                    rect = (2, 2, max(1, ex2 - ex1 - 4), max(1, ey2 - ey1 - 4))
                    grab_mask = np.zeros(roi.shape[:2], np.uint8)
                    cv2.grabCut(roi, grab_mask, rect, bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_RECT)
                    fg_mask = np.where((grab_mask == 2) | (grab_mask == 0), 0, 255).astype(np.uint8)
                    if np.count_nonzero(fg_mask) > 0:
                        final_mask[ey1:ey2, ex1:ex2] = np.maximum(final_mask[ey1:ey2, ex1:ex2], fg_mask)
                        mask_obtained = True
            except Exception as grab_err:
                logger.warning(f"[Censor] GrabCut 전경 마스크 추출 실패, 사각형 폴백 적용: {grab_err}")

        # 3. 최후 폴백: 직사각형
        if not mask_obtained:
            cv2.rectangle(final_mask, (ex1, ey1), (ex2, ey2), 255, -1)

    # 손가락 및 얼굴 보호 레이어 차감
    if np.count_nonzero(protection_mask) > 0:
        kp = _odd_kernel(3, scale, minimum=3)
        kernel_protect = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kp, kp))
        dilated_protect = cv2.dilate(protection_mask, kernel_protect, iterations=1)
        final_mask = cv2.bitwise_and(final_mask, cv2.bitwise_not(dilated_protect))

    if np.count_nonzero(final_mask) == 0:
        return img

    kc = _odd_kernel(5, scale, minimum=3)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kc, kc))
    closed = cv2.morphologyEx(final_mask, cv2.MORPH_CLOSE, kernel_close)

    kb = _odd_kernel(3, scale, minimum=3)
    blurred = cv2.GaussianBlur(closed, (kb, kb), 1.0 * scale)

    if isinstance(color, str):
        color_rgb = (255, 255, 255) if color.lower() in ("white", "whiteout", "white_silhouette") else (0, 0, 0)
    else:
        color_rgb = color

    alpha = (blurred.astype(np.float32) / 255.0)[:, :, np.newaxis]
    target_layer = np.full_like(img_np, color_rgb, dtype=np.float32)
    blended = (1.0 - alpha) * img_np.astype(np.float32) + alpha * target_layer
    blended = np.clip(blended, 0, 255).astype(np.uint8)

    return _restore_alpha(Image.fromarray(blended), alpha_ch)


def apply_black_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
) -> Image.Image:
    """블랙 실루엣(#000000) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="black")


def apply_white_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
) -> Image.Image:
    """화이트 실루엣(#FFFFFF) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="white")


def _render_censored_style(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    style: str = "white_silhouette",
    *,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
) -> Image.Image:
    """검열 스타일명(`style`)에 대응하는 렌더러로 라우팅한다."""
    return _dispatch_censor_style(
        img,
        boxes_with_labels,
        style=style,
        silhouette_fn=apply_silhouette,
        skin_np=skin_np,
        pose_cat=pose_cat,
    )


def _normalize_censor_targets(targets: Sequence[str]) -> set[str]:
    """검열 타겟 라벨 별칭을 imgutils 표준 라벨 집합으로 정규화한다."""
    normalized: set[str] = set()
    for t in targets:
        t_clean = str(t).strip().lower()
        if t_clean in ("all", "both"):
            normalized.update(["penis", "pussy", "nipple_f"])
        elif t_clean in ("genital", "genitals", "sex"):
            normalized.update(["penis", "pussy"])
        elif t_clean in ("pussy", "vagina", "vulva"):
            normalized.add("pussy")
        elif t_clean in ("penis", "cock", "dick"):
            normalized.add("penis")
        elif t_clean in ("nipple", "nipples", "nipple_f"):
            normalized.add("nipple_f")
        else:
            normalized.add(t_clean)
    return normalized


# 체위별 안전 앵커 바운딩 박스 (정규화 좌표: [x0, y0, x1, y1])
POSE_SAFE_ANCHORS = {
    "missionary": [0.34, 0.58, 0.66, 0.84],
    "doggystyle": [0.35, 0.52, 0.65, 0.78],
    "cowgirl":    [0.34, 0.60, 0.66, 0.86],
    "fellatio":   [0.38, 0.45, 0.64, 0.70],
    "side":       [0.35, 0.55, 0.65, 0.82],
    "default_h":  [0.34, 0.56, 0.66, 0.84],
}

# 비노출 여운/포옹/키스 씬 목록 (성기 검열 대상 제외)
NON_EXPOSURE_H_POSES = frozenset({
    "101", "102", "104", "105", "171", "172", "174", "175"
})

_POSE_CODE_PATTERN = re.compile(r"_(\d{2,4})(?:_|\.)")

H_SCENE_SECTIONS: frozenset[str] = frozenset({
    "h_scenes",
    "special_h",
    "scenes_otokonoko",
    "special_otokonoko",
})


def extract_pose_code(filename: str) -> Optional[int]:
    """에셋 파일명(code/hybrid 모두 지원)에서 포즈 코드 정수를 추출한다."""
    match = _POSE_CODE_PATTERN.search(filename)
    return int(match.group(1)) if match else None


def is_censor_target(
    pose_code: Optional[Union[str, int]] = None,
    *,
    section: Optional[str] = None,
    is_nude_scene: bool = False,
) -> bool:
    """
    성인 씬 검열 대상 여부를 판정하는 단일 진실 공급원(SSOT).
    - NON_EXPOSURE_H_POSES(101, 102, 104, 105, 171, 172, 174, 175)는 무조건 제외.
    - V2 H-씬 대역: 80..135, 150..200.
    - runner.py 등에서는 section in H_SCENE_SECTIONS 또는 is_nude_scene=True 도 포함.
    """
    if pose_code is not None:
        try:
            code_int = int(str(pose_code).strip())
        except (TypeError, ValueError):
            code_int = None
        if code_int is not None:
            if str(code_int).zfill(3) in NON_EXPOSURE_H_POSES:
                return False
            if (80 <= code_int <= 135) or (150 <= code_int <= 200):
                return True
    if is_nude_scene:
        return True
    if section and section in H_SCENE_SECTIONS:
        return True
    return False


def get_origin_backup_path(file_path: Union[Path, str]) -> Path:
    """에셋 파일에 대응하는 표준 무검열 원본 백업 경로(assets/{char}_origin/{filename})를 반환한다."""
    src = Path(file_path)
    parent_dir = src.parent
    return parent_dir.parent / f"{parent_dir.name}_origin" / src.name


def get_pose_category(pose_code: Union[str, int]) -> Optional[str]:
    """
    V2 포즈 코드(080~135, 150~200)에 따른 체위 카테고리를 반환한다.
    비노출 씬이거나 일반 일상 포즈는 None을 반환한다.
    """
    code_str = str(pose_code).zfill(3)
    if code_str in NON_EXPOSURE_H_POSES:
        return None

    try:
        code_int = int(code_str)
    except ValueError:
        return None

    if code_str in ("086", "087", "126", "127", "132", "135", "156", "157", "196", "197"):
        return "fellatio"
    if code_str in ("088", "089", "090", "118", "121", "130", "133", "154", "158", "159", "160", "188", "191"):
        return "doggystyle"
    if code_str in ("091", "092", "093", "109", "114", "131", "134", "161", "162", "163", "179", "184"):
        return "cowgirl"
    if code_str in ("098", "119", "168", "189"):
        return "side"
    if code_str in ("080", "081", "082", "083", "084", "085", "094", "096", "097", "150", "151", "152", "153", "164", "166", "167", "168"):
        return "missionary"
    if (80 <= code_int <= 135) or (150 <= code_int <= 200):
        return "default_h"

    return None


def get_pose_anchor_box(
    pose_code: Union[str, int], img_width: int, img_height: int
) -> Optional[Tuple[int, int, int, int]]:
    """포즈 코드에 해당하는 안전 앵커 픽셀 바운딩 박스 (x1, y1, x2, y2)를 계산하여 반환한다."""
    cat = get_pose_category(pose_code)
    if not cat or cat not in POSE_SAFE_ANCHORS:
        return None
    x0_pct, y0_pct, x1_pct, y1_pct = POSE_SAFE_ANCHORS[cat]
    x1 = int(round(x0_pct * img_width))
    y1 = int(round(y0_pct * img_height))
    x2 = int(round(x1_pct * img_width))
    y2 = int(round(y1_pct * img_height))
    return (x1, y1, x2, y2)


def verify_and_heal_censor(
    censored_img: Image.Image,
    style: str = "white_silhouette",
    min_confidence: float = 0.32,
    targets: Sequence[str] = ("penis", "pussy"),
    pose_cat: Optional[str] = None,
) -> Tuple[Image.Image, bool]:
    """
    검열이 완료된 이미지를 대상으로 검출기를 다시 실행해 잔여 노출 여부를 2차 검증·치유한다.
    과도한 2중 팽창을 방지하기 위해 기본 신뢰도 0.32 이상의 명백한 잔여 노출에만 보정을 발동한다.
    반환값: (최종 안전 이미지, 추가 보정 발생 여부)
    """
    if not HAS_IMGUTILS:
        return censored_img, False

    try:
        rgb_img, alpha_ch = _split_rgb_and_alpha(censored_img)
        target_labels = _normalize_censor_targets(targets)
        eff_min_conf = max(0.05, float(min_confidence))
        try:
            results = detect_censors(rgb_img, conf_threshold=eff_min_conf)
        except TypeError:
            results = detect_censors(rgb_img)

        leaks = [
            ((int(b[0]), int(b[1]), int(b[2]), int(b[3])), str(label).strip().lower(), float(score))
            for b, label, score in results
            if str(label).strip().lower() in target_labels and float(score) >= eff_min_conf
        ]
        if not leaks:
            return censored_img, False

        logger.warning(
            f"[Censor Gate] 2차 사후 검증에서 잔여 노출 {len(leaks)}건 감지! 즉시 2차 안전 보정을 수행합니다."
        )
        healed_rgb = _render_censored_style(rgb_img, leaks, style=style, pose_cat=pose_cat)
        return _restore_alpha(healed_rgb, alpha_ch), True
    except Exception as e:
        logger.debug(f"[Censor Gate] 사후 검증 예외: {e}")
        return censored_img, False


def censor_image(
    image: Union[Image.Image, Path, str],
    style: str = "white_silhouette",
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.20,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
    *,
    verify_heal: bool = False,
    heal_min_confidence: float = 0.32,
) -> Tuple[Image.Image, int]:
    """
    이미지 내 검열 대상 부위를 자동으로 감지하여 지정된 스타일로 검열을 적용한다.
    - 기본 스타일: 'white_silhouette'
    - verify_heal=True 시 검열 후 잔여 노출 2차 재검증/치유 게이트(verify_and_heal_censor) 적용.
    - 반환값: (검열된 PIL 이미지, 감지된 부위 개수)
    """
    if isinstance(image, (str, Path)):
        with Image.open(str(image)) as opened:
            raw_img = opened.copy()
    else:
        raw_img = image

    rgb_img, alpha_ch = _split_rgb_and_alpha(raw_img)
    normalized_targets = _normalize_censor_targets(targets)
    detected_boxes_with_labels: List[Tuple[Tuple[int, int, int, int], str, float]] = []

    if HAS_IMGUTILS:
        try:
            results = detect_censors(rgb_img)
            img_w, img_h = rgb_img.size
            for box, label, score in results:
                label_low = str(label).strip().lower()
                if label_low in normalized_targets and float(score) >= min_confidence:
                    x1, y1, x2, y2 = [int(v) for v in box]
                    bw = max(1, x2 - x1)
                    bh = max(1, y2 - y1)

                    if label_low == "pussy":
                        if y2 < img_h * 0.48 and bh < img_h * 0.15:
                            logger.info(f"[Censor] 하복부 자궁문신 오인식 박스 제외: ({x1},{y1},{x2},{y2})")
                            continue
                        if bh > bw * 1.25 and bh > 60:
                            safe_cut = int(bh * 0.25)
                            y1 = min(y2 - 20, y1 + safe_cut)

                    detected_boxes_with_labels.append(((x1, y1, x2, y2), label_low, float(score)))
        except Exception as e:
            logger.warning(f"[Censor] 감지 모델 추론 예외: {e}")

    pose_cat = get_pose_category(pose_code) if pose_code is not None else None
    if not detected_boxes_with_labels and pose_code is not None and pose_cat:
        logger.warning(
            f"[WARN] [Censor] 포즈 {pose_code}: 미탐지! 안전 앵커 영역({pose_cat}) 폴백 점검 대상입니다."
        )
        if enable_anchor_fallback:
            w, h = rgb_img.size
            anchor_box = get_pose_anchor_box(pose_code, w, h)
            if anchor_box:
                detected_boxes_with_labels.append((anchor_box, "anchor", 1.0))
                logger.info(f"[Censor] 포즈 {pose_code}: 안전 앵커 폴백 박스 적용 {anchor_box}")

    if not detected_boxes_with_labels:
        return _restore_alpha(rgb_img, alpha_ch), 0

    censored_rgb = _render_censored_style(
        rgb_img,
        detected_boxes_with_labels,
        style=style,
        pose_cat=pose_cat,
    )
    if verify_heal and str(style).lower().strip() not in ("debug", "debug_bbox", "bbox", "bbox_debug"):
        censored_rgb, _ = verify_and_heal_censor(
            censored_rgb,
            style=style,
            min_confidence=heal_min_confidence,
            targets=targets,
            pose_cat=pose_cat,
        )

    return _restore_alpha(censored_rgb, alpha_ch), len(detected_boxes_with_labels)


def censor_file(
    src_path: Union[Path, str],
    dst_path: Optional[Union[Path, str]] = None,
    style: str = "white_silhouette",
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.20,
    quality: int = 95,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
    backup_dir: Optional[Union[Path, str]] = None,
    *,
    verify_heal: bool = False,
) -> Optional[Path]:
    """
    파일 경로를 입력받아 검열본을 생성하고 원자적으로 저장한다(_atomic_replace_with_retry).
    - dst_path 미지정 시 'stem_censored{원본확장자}'로 자동 저장.
    - backup_dir 지정 시 감지 성공 여부와 무관하게 원본 파일을 해당 폴더로 안전하게 선제 복사 백업.
    - 감지된 검열 대상이 없는 경우 검열본을 생성하지 않고 None 반환.
    """
    src = Path(src_path)
    if not src.exists():
        return None

    if backup_dir:
        b_dir = Path(backup_dir)
        b_dir.mkdir(parents=True, exist_ok=True)
        b_path = b_dir / src.name
        if not b_path.exists():
            shutil.copy2(src, b_path)

    with Image.open(src) as opened:
        orig_alpha = (
            opened.convert("RGBA").getchannel("A")
            if opened.mode in ("RGBA", "LA", "PA") or (opened.mode == "P" and "transparency" in opened.info)
            else None
        )
        img = opened.convert("RGB")

    censor_kwargs: dict[str, Any] = {
        "style": style,
        "targets": targets,
        "min_confidence": min_confidence,
        "pose_code": pose_code,
        "enable_anchor_fallback": enable_anchor_fallback,
    }
    if verify_heal:
        censor_kwargs["verify_heal"] = True
    censored_img, count = censor_image(img, **censor_kwargs)

    if count == 0:
        return None

    target_dst = src.with_name(f"{src.stem}_censored{src.suffix}") if dst_path is None else Path(dst_path)
    target_dst.parent.mkdir(parents=True, exist_ok=True)
    ext = target_dst.suffix.lower()
    temp_dst = target_dst.with_name(
        f".{target_dst.stem}_{uuid.uuid4().hex[:8]}.tmp{target_dst.suffix or '.webp'}"
    )
    try:
        if ext == ".png":
            save_img = censored_img.convert("RGBA") if orig_alpha is not None else censored_img
            if orig_alpha is not None:
                save_img.putalpha(orig_alpha)
            save_img.save(temp_dst, "PNG", optimize=True)
        elif ext in (".jpg", ".jpeg"):
            censored_img.convert("RGB").save(temp_dst, "JPEG", quality=quality, subsampling=0)
        else:
            censored_img.convert("RGB").save(temp_dst, "WEBP", quality=quality)
        _atomic_replace_with_retry(temp_dst, target_dst)
    finally:
        if temp_dst.exists():
            temp_dst.unlink(missing_ok=True)

    return target_dst


def detect_genitals(
    image: Union[Path, str, Image.Image],
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
) -> List[Tuple[Tuple[int, int, int, int], str, float]]:
    """이미지에서 검열 대상 부위를 감지하여 [(box, label, score), ...] 형태로 반환한다."""
    if not HAS_IMGUTILS:
        return []
    try:
        if isinstance(image, (str, Path)):
            with Image.open(str(image)) as opened:
                img = opened.convert("RGB")
        else:
            img = image.convert("RGB")
        results = detect_censors(img)
        filtered = []
        normalized = _normalize_censor_targets(targets)
        for box, label, score in results:
            if str(label).strip().lower() in normalized and float(score) >= min_confidence:
                filtered.append((box, str(label), float(score)))
        return filtered
    except Exception as e:
        logger.warning(f"[Censor] detect_genitals 감지 예외: {e}")
        return []


def _restore_from_origin_dirs(
    base_dir: Path,
    allowed_chars: Optional[set[str]] = None,
) -> int:
    """
    _origin 백업 폴더에서 원본 에셋을 작업 폴더로 안전하게 복원한다.
    (_origin 내부 파일은 읽기 전용으로만 참조하며 절대 수정하지 않는다.)
    """
    extensions = ("*.webp", "*.png", "*.jpg", "*.jpeg")
    origin_pairs: list[tuple[Path, Path]] = []

    if base_dir.name.endswith("_origin"):
        char_name = base_dir.name[: -len("_origin")]
        if not allowed_chars or char_name in allowed_chars:
            origin_pairs.append((base_dir, base_dir.parent / char_name))
    else:
        sibling_origin = base_dir.parent / f"{base_dir.name}_origin"
        if sibling_origin.is_dir():
            if not allowed_chars or base_dir.name in allowed_chars:
                origin_pairs.append((sibling_origin, base_dir))

        for child in sorted(base_dir.iterdir()):
            if child.is_dir() and child.name.endswith("_origin"):
                char_name = child.name[: -len("_origin")]
                if not char_name:
                    continue
                if allowed_chars and char_name not in allowed_chars:
                    continue
                origin_pairs.append((child, base_dir / char_name))

    restored = 0
    for origin_dir, asset_dir in origin_pairs:
        asset_dir.mkdir(parents=True, exist_ok=True)
        for ext in extensions:
            for orig_file in sorted(origin_dir.glob(ext)):
                if orig_file.name.startswith(".tmp_"):
                    continue
                shutil.copy2(orig_file, asset_dir / orig_file.name)
                restored += 1

    if restored:
        logger.info(f"[Censor] _origin 원본 {restored}장 복원 완료")
    return restored


def run_batch_censor(
    folder_path: Union[Path, str],
    style: str = "white_silhouette",
    overwrite: bool = True,
    recursive: bool = True,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    enable_anchor_fallback: bool = False,
    *,
    backup_origin: bool = True,
    restore_origin: bool = False,
    char_names: Optional[Sequence[str]] = None,
    verify_heal: bool = False,
) -> int:
    """
    폴더 내의 모든 일러스트 에셋을 스캔하여 일괄 검열을 수행한다 (단일 코어 SSOT).
    - backup_origin=True & overwrite=True 시 assets/{char}_origin/ 에 무검열 원본을 선제 보존.
    - restore_origin=True 시 검열 전 _origin 원본을 먼저 복원해 클린 상태에서 재검열 수행.
    - char_names 지정 시 해당 캐릭터 폴더/프리픽스만 필터링.
    - _atomic_replace_with_retry 로 Windows 파일락 충돌 및 중단 시 파일 손상 원천 방지.
    """
    base_dir = Path(folder_path).resolve()
    if not base_dir.exists():
        logger.error(f"[오류] 대상 폴더를 찾을 수 없습니다: {base_dir}")
        return 0

    allowed_chars: Optional[set[str]] = None
    if char_names:
        parsed = {
            token.strip()
            for raw in char_names
            for token in str(raw).split(",")
            if token.strip()
        }
        if parsed:
            allowed_chars = parsed

    if restore_origin:
        _restore_from_origin_dirs(base_dir, allowed_chars=allowed_chars)

    extensions = ["*.webp", "*.png", "*.jpg", "*.jpeg"]
    image_files: List[Path] = []
    for ext in extensions:
        if recursive:
            image_files.extend(base_dir.rglob(ext))
        else:
            image_files.extend(base_dir.glob(ext))

    target_files = [
        f
        for f in sorted(set(image_files))
        if not f.stem.endswith("_censored")
        and not any("_origin" in part for part in f.parts)
        and not any("backup" in part.lower() for part in f.parts)
        and not f.name.startswith(".tmp_")
        and (
            allowed_chars is None
            or f.parent.name in allowed_chars
            or any(f.name.startswith(f"{c}_") for c in allowed_chars)
        )
    ]
    total = len(target_files)
    processed_count = 0

    logger.info(
        f"[Censor] 스캔 시작: 총 {total}장 검사 (스타일={style}, 덮어쓰기={overwrite}, "
        f"원본백업={backup_origin and overwrite}, 원본복원={restore_origin})"
    )

    for idx, file_path in enumerate(target_files, 1):
        pose_code = extract_pose_code(file_path.name)

        if pose_code is not None and not is_censor_target(pose_code):
            continue

        tmp_censored = file_path.parent / f".tmp_{file_path.name}"
        try:
            if overwrite and backup_origin:
                origin_path = get_origin_backup_path(file_path)
                origin_path.parent.mkdir(parents=True, exist_ok=True)
                if not origin_path.exists():
                    shutil.copy2(file_path, origin_path)

            dst = (
                tmp_censored
                if overwrite
                else file_path.with_name(f"{file_path.stem}_censored{file_path.suffix}")
            )
            saved = censor_file(
                src_path=file_path,
                dst_path=dst,
                style=style,
                targets=targets,
                min_confidence=min_confidence,
                pose_code=pose_code,
                enable_anchor_fallback=enable_anchor_fallback,
                verify_heal=verify_heal,
            )
            if saved is not None:
                if overwrite and tmp_censored.exists():
                    _atomic_replace_with_retry(tmp_censored, file_path)
                processed_count += 1
                logger.info(f"[{idx}/{total}] 검열 완료: {file_path.name}")
            else:
                if tmp_censored.exists():
                    tmp_censored.unlink(missing_ok=True)
        except Exception as e:
            if tmp_censored.exists():
                tmp_censored.unlink(missing_ok=True)
            logger.warning(f"[Censor] 배치 검열 실패: {file_path.name} ({e})")

    logger.info(f"[Censor] 작업 완료: 총 {processed_count}장 정밀 검열 적용")
    return processed_count


__all__ = [
    "_BASE_RESOLUTION",
    "_SAM_MODEL_LOCK",
    "_atomic_replace_with_retry",
    "_compute_scale_factor",
    "_estimate_penis_axis_and_base",
    "_odd_kernel",
    "_render_censored_style",
    "_restore_from_origin_dirs",
    "_scale_int",
    "apply_black_silhouette",
    "apply_debug_bbox",
    "apply_manga_bar",
    "apply_manga_glow",
    "apply_mosaic",
    "apply_shadow_silhouette",
    "apply_silhouette",
    "apply_slim_black_bar",
    "apply_white_silhouette",
    "apply_wide_solid_bar",
    "censor_file",
    "censor_image",
    "detect_genitals",
    "extract_pose_code",
    "get_origin_backup_path",
    "get_pose_anchor_box",
    "get_pose_category",
    "get_sam_model",
    "get_skin_mask",
    "H_SCENE_SECTIONS",
    "HAS_IMGUTILS",
    "is_censor_target",
    "NON_EXPOSURE_H_POSES",
    "POSE_SAFE_ANCHORS",
    "run_batch_censor",
    "unload_censor_models",
    "verify_and_heal_censor",
]

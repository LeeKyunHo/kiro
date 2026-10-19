"""
plepa_engine.censor
2D 애니메이션 일러스트 전용 자동 성기 검열(Censorship) 모듈.
- 남성기+고환 외곽선 블랙 실루엣 (Black Silhouette via MobileSAM)
- 와이드 솔리드 캡슐 바 (Wide Solid Bar)
- 슬림 사선 블랙 바 (Thin Black Bar)
- 그림자 음영 박스 (Shadow Box)
- 격자 모자이크 (Pixelated Mosaic)
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
import shutil
from typing import Any, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

try:
    from imgutils.detect import detect_censors
    HAS_IMGUTILS = True
except ImportError:
    HAS_IMGUTILS = False

# MobileSAM 모델 싱글톤 (Lazy Load)
_SAM_MODEL = None


def get_sam_model():
    """MobileSAM 세그멘테이션 모델을 싱글톤으로 로드합니다."""
    global _SAM_MODEL
    if _SAM_MODEL is None:
        try:
            from ultralytics import SAM
            _SAM_MODEL = SAM("mobile_sam.pt")
            logger.info("[Censor] MobileSAM 모델 로드 완료 (mobile_sam.pt)")
        except Exception as e:
            logger.warning(f"[Censor] MobileSAM 로드 실패, OpenCV 폴백 모드로 동작합니다: {e}")
            _SAM_MODEL = False
    return _SAM_MODEL if _SAM_MODEL is not False else None


def apply_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Tuple[int, int, int, int], str]],
    color: Union[str, Tuple[int, int, int]] = "black",
) -> Image.Image:
    """
    네모난 박스가 아니라 남성기 전체와 고환의 실제 선화 외곽선(Contour)을 따라
    내부만 솔리드 컬러(블랙 #000000 또는 화이트 #FFFFFF)로 매끈하게 채우는 실루엣 검열을 렌더링합니다.
    - MobileSAM BBox Prompting으로 픽셀 단위 마스크 추출.
    - 펠라/오랄 구도에서 여주인공 팔/어깨 피부 침범을 원천 차단하는 적응형 마진 및 고립 노이즈 컨투어 정제.
    - 타원 커널 모폴로지 클로징 및 가우시안 안티앨리어싱으로 계단 현상 없는 유려한 곡선 완성.
    """
    w, h = img.size
    img_np = np.array(img.convert("RGB"))
    final_mask = np.zeros((h, w), dtype=np.uint8)

    sam = get_sam_model()

    # 0. 손(Hands), 얼굴(Faces), 머리/헤어(Heads) 보호 마스크 추출
    protection_mask = np.zeros((h, w), dtype=np.uint8)
    try:
        from imgutils.detect import detect_hands, detect_faces, detect_heads
        detected_hands = detect_hands(img, conf_threshold=0.25)
        detected_faces = detect_faces(img, conf_threshold=0.25)
        detected_heads = detect_heads(img, conf_threshold=0.25)

        overlapping_protect_boxes = []
        for box, _ in boxes_with_labels:
            bx1, by1, bx2, by2 = box
            for (hx1, hy1, hx2, hy2), _, _ in detected_hands:
                if max(bx1, hx1) < min(bx2, hx2) and max(by1, hy1) < min(by2, hy2):
                    overlapping_protect_boxes.append([hx1, hy1, hx2, hy2])
            for (fx1, fy1, fx2, fy2), _, _ in detected_faces:
                if max(bx1, fx1) < min(bx2, fx2) and max(by1, fy1) < min(by2, fy2):
                    overlapping_protect_boxes.append([fx1, fy1, fx2, fy2])
            for (head_x1, head_y1, head_x2, head_y2), _, _ in detected_heads:
                if max(bx1, head_x1) < min(bx2, head_x2) and max(by1, head_y1) < min(by2, head_y2):
                    overlapping_protect_boxes.append([head_x1, head_y1, head_x2, head_y2])

        if sam is not None and overlapping_protect_boxes:
            for p_box in overlapping_protect_boxes:
                try:
                    p_res = sam(img, bboxes=[p_box], verbose=False)
                    if p_res and p_res[0].masks is not None:
                        pm_data = p_res[0].masks.data.cpu().numpy()
                        pm = (pm_data[0] * 255).astype(np.uint8)
                        if pm.shape != (h, w):
                            pm = cv2.resize(pm, (w, h), interpolation=cv2.INTER_NEAREST)
                        crop_p = np.zeros_like(pm)
                        crop_p[p_box[1]:p_box[3], p_box[0]:p_box[2]] = 255
                        pm = cv2.bitwise_and(pm, crop_p)
                        protection_mask = np.maximum(protection_mask, pm)
                except Exception:
                    pass
    except Exception as pe:
        logger.debug(f"[Censor] 보호 영역 검출 생략: {pe}")

    for box, label in boxes_with_labels:
        x1, y1, x2, y2 = box
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)

        is_penis = label == "penis"
        if is_penis:
            # 적응형 마진: 가로형(bw >= bh)은 오랄/펠라 등 여주인공 신체 밀착 구도이므로 하단 확장 억제(0.04),
            # 세로형(bh > bw * 1.3)일 때만 고환 영역을 고려하여 하단 10% 확장
            m_x = 0.04
            m_top = 0.02
            m_bot = 0.08 if bh > bw * 1.3 else 0.02
            ex1 = max(0, int(x1 - bw * m_x))
            ey1 = max(0, int(y1 - bh * m_top))
            ex2 = min(w, int(x2 + bw * m_x))
            ey2 = min(h, int(y2 + bh * m_bot))
        else:
            # pussy 등 여성기: 상단(하복부/자궁문신)으로는 확장하지 않고 하단/측면만 미세 마진 적용
            m_x = 0.02
            m_top = 0.0
            m_bot = 0.03
            ex1 = max(0, int(x1 - bw * m_x))
            ey1 = max(0, y1)  # 상단 확장 원천 차단으로 자궁문신 완벽 보존
            ex2 = min(w, int(x2 + bw * m_x))
            ey2 = min(h, int(y2 + bh * m_bot))

        expanded_bbox = [ex1, ey1, ex2, ey2]
        mask_obtained = False

        # 1. MobileSAM 추론 시도
        if sam is not None:
            try:
                results = sam(img, bboxes=[expanded_bbox], verbose=False)
                if results and results[0].masks is not None:
                    m_data = results[0].masks.data.cpu().numpy()
                    sub_m = (m_data[0] * 255).astype(np.uint8)
                    if sub_m.shape != (h, w):
                        sub_m = cv2.resize(sub_m, (w, h), interpolation=cv2.INTER_NEAREST)

                    # BBox 범위 외곽 엄격 제한 (경계 밖 누수 차단)
                    crop_mask = np.zeros_like(sub_m)
                    crop_mask[ey1:ey2, ex1:ex2] = 255
                    sub_m = cv2.bitwise_and(sub_m, crop_mask)

                    # 노이즈 컨투어 정제: 면적 300px 미만 또는 주 컨투어 대비 5% 미만인 고립된 파편(팔/어깨 침범 원인) 완전 제거
                    contours, _ = cv2.findContours(sub_m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    if contours:
                        max_area = max(cv2.contourArea(c) for c in contours)
                        cleaned_sub_m = np.zeros_like(sub_m)
                        for c in contours:
                            c_area = cv2.contourArea(c)
                            if c_area >= 300 and c_area >= max_area * 0.05:
                                cv2.drawContours(cleaned_sub_m, [c], -1, 255, -1)
                        sub_m = cleaned_sub_m

                    if np.count_nonzero(sub_m) > 0:
                        final_mask = np.maximum(final_mask, sub_m)
                        mask_obtained = True
            except Exception as e:
                logger.warning(f"[Censor] MobileSAM 추론 실패, OpenCV 폴백: {e}")

        # 2. SAM 실패 시 OpenCV GrabCut / 엣지 기반 전경 마스크 폴백
        if not mask_obtained:
            try:
                roi = img_np[ey1:ey2, ex1:ex2]
                if roi.size > 0:
                    bgd_model = np.zeros((1, 65), np.float64)
                    fgd_model = np.zeros((1, 65), np.float64)
                    rect = (2, 2, max(1, ex2 - ex1 - 4), max(1, ey2 - ey1 - 4))
                    grab_mask = np.zeros(roi.shape[:2], np.uint8)
                    cv2.grabCut(roi, grab_mask, rect, bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_RECT)
                    fg_mask = np.where((grab_mask == 2) | (grab_mask == 0), 0, 255).astype(np.uint8)
                    final_mask[ey1:ey2, ex1:ex2] = np.maximum(final_mask[ey1:ey2, ex1:ex2], fg_mask)
                    mask_obtained = True
            except Exception:
                pass

        # 3. 최후 폴백: 둥근 직사각형
        if not mask_obtained:
            cv2.rectangle(final_mask, (ex1, ey1), (ex2, ey2), 255, -1)

    # 손가락 및 얼굴 보호 레이어 차감 (1~2px 팽창으로 손/얼굴 선화 경계선 깔끔 보존)
    if np.count_nonzero(protection_mask) > 0:
        kernel_protect = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        dilated_protect = cv2.dilate(protection_mask, kernel_protect, iterations=1)
        final_mask = cv2.bitwise_and(final_mask, cv2.bitwise_not(dilated_protect))

    if np.count_nonzero(final_mask) == 0:
        return img

    # 마스크 클로징 및 스무딩 (자연스러운 외곽선 곡선화)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.morphologyEx(final_mask, cv2.MORPH_CLOSE, kernel_close)

    # 테두리 가우시안 블러 (미세 안티앨리어싱)
    blurred = cv2.GaussianBlur(closed, (3, 3), 1.0)

    # 색상 레이어 합성
    if isinstance(color, str):
        color_rgb = (255, 255, 255) if color.lower() in ("white", "whiteout", "white_silhouette") else (0, 0, 0)
    else:
        color_rgb = color

    alpha = (blurred.astype(np.float32) / 255.0)[:, :, np.newaxis]
    target_layer = np.full_like(img_np, color_rgb, dtype=np.float32)
    blended = (1.0 - alpha) * img_np.astype(np.float32) + alpha * target_layer
    blended = np.clip(blended, 0, 255).astype(np.uint8)

    return Image.fromarray(blended)


def apply_black_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Tuple[int, int, int, int], str]],
) -> Image.Image:
    """블랙 실루엣(#000000) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="black")


def apply_white_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Tuple[int, int, int, int], str]],
) -> Image.Image:
    """화이트 실루엣(#FFFFFF) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="white")


def apply_wide_solid_bar(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    margin_ratio: float = 0.18,
    corner_radius_ratio: float = 0.25,
    min_margin: int = 6,
) -> Image.Image:
    """
    2D 서브컬처 상업 동인지/미연시(DLsite/FANZA) 표준 '와이드 솔리드 캡슐 바(Wide Solid Bar)'를 렌더링합니다.
    - 정밀 성기 BBox 영역을 상하좌우 18% 적응형 세이프 마진(Safe Margin)으로 팽창.
    - 모서리를 둥글게 깎는 캡슐형(Pill-shaped) 라운딩 적용.
    """
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    img_w, img_h = canvas.size

    for (x1, y1, x2, y2) in boxes:
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)

        pad_x = max(min_margin, int(w * margin_ratio))
        pad_y = max(min_margin, int(h * margin_ratio))

        bx1 = max(0, x1 - pad_x)
        by1 = max(0, y1 - pad_y)
        bx2 = min(img_w, x2 + pad_x)
        by2 = min(img_h, y2 + pad_y)

        box_w = bx2 - bx1
        box_h = by2 - by1

        radius = max(4, int(min(box_w, box_h) * corner_radius_ratio))
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=radius, fill=(0, 0, 0))

    return canvas


def apply_slim_black_bar(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    angle_deg: float = 25.0,
    thickness_ratio: float = 0.28,
) -> Image.Image:
    """
    2D 서브컬처 미니멀 슬림 블랙 바(Thin Black Bar)를 렌더링합니다.
    - 남성기 중심을 통과하는 사선(기본 25도) 얇은 검은색 띠를 얹습니다.
    """
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)

    for (x1, y1, x2, y2) in boxes:
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0

        bar_thickness = max(12, min(32, int(min(w, h) * thickness_ratio)))
        bar_length = max(w, h) * 0.95

        angle_rad = math.radians(angle_deg)
        dx = (bar_length / 2.0) * math.cos(angle_rad)
        dy = (bar_length / 2.0) * math.sin(angle_rad)

        p1 = (cx - dx, cy - dy)
        p2 = (cx + dx, cy + dy)

        draw.line([p1, p2], fill=(0, 0, 0), width=bar_thickness)
        r = bar_thickness / 2.0
        draw.ellipse([p1[0] - r, p1[1] - r, p1[0] + r, p1[1] + r], fill=(0, 0, 0))
        draw.ellipse([p2[0] - r, p2[1] - r, p2[0] + r, p2[1] + r], fill=(0, 0, 0))

    return canvas


def apply_shadow_silhouette(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    shadow_opacity: float = 0.85,
    blur_radius: int = 14,
) -> Image.Image:
    """
    형태 윤곽선과 굴곡(실루엣)은 은은하게 유지하되, 내부 선화 디테일만 짙은 그림자 톤으로 덮는
    고급 그림자 음영(Shadow Box) 검열을 적용합니다.
    """
    canvas = img.copy()

    for (x1, y1, x2, y2) in boxes:
        crop_box = (max(0, x1), max(0, y1), min(canvas.width, x2), min(canvas.height, y2))
        sub_img = canvas.crop(crop_box)

        blurred = sub_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        shadow = Image.new("RGB", blurred.size, color=(18, 18, 18))
        blended = Image.blend(blurred, shadow, alpha=shadow_opacity)

        canvas.paste(blended, crop_box)

    return canvas


def apply_mosaic(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    block_size: int = 14,
) -> Image.Image:
    """
    클래식 격자 모자이크(Pixelated Mosaic)를 적용합니다.
    """
    canvas = img.copy()

    for (x1, y1, x2, y2) in boxes:
        crop_box = (max(0, x1), max(0, y1), min(canvas.width, x2), min(canvas.height, y2))
        sub_img = canvas.crop(crop_box)
        w, h = sub_img.size
        small_w = max(1, w // block_size)
        small_h = max(1, h // block_size)

        pixelated = sub_img.resize((small_w, small_h), Image.Resampling.BILINEAR).resize(
            (w, h), Image.Resampling.NEAREST
        )
        canvas.paste(pixelated, crop_box)

    return canvas


# 체위별 안전 앵커 바운딩 박스 (정규화 좌표: [x0, y0, x1, y1])
POSE_SAFE_ANCHORS = {
    "missionary": [0.34, 0.58, 0.66, 0.84],   # 정상위/배위: 하단 중앙 고환/음순 영역
    "doggystyle": [0.35, 0.52, 0.65, 0.78],   # 후배위: 중앙 하단 결합부
    "cowgirl":    [0.34, 0.60, 0.66, 0.86],   # 기승위: 하단 기저부
    "fellatio":   [0.38, 0.45, 0.64, 0.70],   # 구강/오랄: 중상단 입/성기 접촉부
    "side":       [0.35, 0.55, 0.65, 0.82],   # 측와위: 측면 교차부
    "default_h":  [0.34, 0.56, 0.66, 0.84],   # 기본 성인 결합 씬
}

# 비노출 여운/포옹/키스 씬 목록 (성기 검열 대상 제외)
NON_EXPOSURE_H_POSES = frozenset({
    "101", "102", "104", "105", "171", "172", "174", "175"
})


def get_pose_category(pose_code: Union[str, int]) -> Optional[str]:
    """
    V2 포즈 코드(080~130, 150~200)에 따른 체위 카테고리를 반환합니다.
    비노출 씬이거나 일반 일상 포즈는 None을 반환합니다.
    """
    code_str = str(pose_code).zfill(3)
    if code_str in NON_EXPOSURE_H_POSES:
        return None

    try:
        code_int = int(code_str)
    except ValueError:
        return None

    # 구강/오랄 씬
    if code_str in ("086", "087", "126", "127", "132", "135", "156", "157", "196", "197"):
        return "fellatio"
    # 후배위/배면 씬
    if code_str in ("088", "089", "090", "118", "121", "130", "133", "154", "158", "159", "160", "188", "191"):
        return "doggystyle"
    # 기승위/승마 씬
    if code_str in ("091", "092", "093", "109", "114", "131", "134", "161", "162", "163", "179", "184"):
        return "cowgirl"
    # 측와위 씬
    if code_str in ("098", "119", "168", "189"):
        return "side"
    # 정상위/배위/누운 자세
    if code_str in ("080", "081", "082", "083", "084", "085", "094", "096", "097", "150", "151", "152", "153", "164", "166", "167", "168"):
        return "missionary"
    # 그 외 성인 H-씬 대역 (080~135, 150~200)
    if (80 <= code_int <= 135) or (150 <= code_int <= 200):
        return "default_h"

    return None


def get_pose_anchor_box(
    pose_code: Union[str, int], img_width: int, img_height: int
) -> Optional[Tuple[int, int, int, int]]:
    """포즈 코드에 해당하는 안전 앵커 픽셀 바운딩 박스 (x1, y1, x2, y2)를 계산하여 반환합니다."""
    cat = get_pose_category(pose_code)
    if not cat or cat not in POSE_SAFE_ANCHORS:
        return None
    x0_pct, y0_pct, x1_pct, y1_pct = POSE_SAFE_ANCHORS[cat]
    x1 = int(round(x0_pct * img_width))
    y1 = int(round(y0_pct * img_height))
    x2 = int(round(x1_pct * img_width))
    y2 = int(round(y1_pct * img_height))
    return (x1, y1, x2, y2)


def censor_image(
    image: Union[Image.Image, Path, str],
    style: str = "silhouette",
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.20,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
) -> Tuple[Image.Image, int]:
    """
    이미지 내 성기 부위를 자동으로 감지하여 검열을 적용합니다.
    - 기본 스타일: 'white_silhouette' (외곽선 화이트 실루엣)
    - 기본 대상: 남성기 및 여성기 ('penis', 'pussy')
    - 반환값: (검열된 PIL 이미지, 감지된 부위 개수)
    """
    if isinstance(image, (str, Path)):
        img = Image.open(str(image)).convert("RGB")
    else:
        img = image.convert("RGB")

    normalized_targets = set()
    for t in targets:
        t_clean = str(t).strip().lower()
        if t_clean in ("all", "both"):
            normalized_targets.update(["penis", "pussy", "nipple_f"])
        elif t_clean in ("genital", "genitals", "sex"):
            normalized_targets.update(["penis", "pussy"])
        elif t_clean in ("pussy", "vagina", "vulva"):
            normalized_targets.add("pussy")
        elif t_clean in ("penis", "cock", "dick"):
            normalized_targets.add("penis")
        elif t_clean in ("nipple", "nipples", "nipple_f"):
            normalized_targets.add("nipple_f")
        else:
            normalized_targets.add(t_clean)

    detected_boxes_with_labels: List[Tuple[Tuple[int, int, int, int], str]] = []

    if HAS_IMGUTILS:
        try:
            results = detect_censors(img)
            img_w, img_h = img.size
            for box, label, score in results:
                if label in normalized_targets and score >= min_confidence:
                    x1, y1, x2, y2 = [int(v) for v in box]
                    bw = max(1, x2 - x1)
                    bh = max(1, y2 - y1)

                    # ── 자궁문신(Womb Tattoo) 및 복부 오탐지 보호 필터 ──
                    if label == "pussy":
                        # 1) 만약 박스가 하복부(배꼽 부근)에만 고립되어 있고 치골/음부 기저부까지 내려가지 않는 경우,
                        #    자궁문신 문양 자체를 성기로 오인식한 것이므로 검열에서 완전 제외
                        if y2 < img_h * 0.48 and bh < img_h * 0.15:
                            logger.info(f"[Censor] 하복부 자궁문신 오인식 박스 제외: ({x1},{y1},{x2},{y2})")
                            continue

                        # 2) 만약 세로로 길게 잡혀 하복부와 음부가 한꺼번에 묶인 경우 (자궁문신 침범 방지),
                        #    문신이 위치하는 상단 25%를 보호 세이프존으로 남겨두고 y1을 하향 조정
                        if bh > bw * 1.25 and bh > 60:
                            safe_cut = int(bh * 0.25)
                            y1 = min(y2 - 20, y1 + safe_cut)

                    detected_boxes_with_labels.append(((x1, y1, x2, y2), label))
        except Exception as e:
            logger.warning(f"[Censor] 감지 모델 추론 예외: {e}")

    # 검열 미탐지 시 체위별 안전 앵커 폴백 점검
    if not detected_boxes_with_labels and pose_code is not None:
        cat = get_pose_category(pose_code)
        if cat:
            logger.warning(
                f"[WARN] [Censor] 포즈 {pose_code}: 성기 미탐지! 안전 앵커 영역({cat}) 폴백 점검 대상입니다."
            )
            if enable_anchor_fallback:
                w, h = img.size
                anchor_box = get_pose_anchor_box(pose_code, w, h)
                if anchor_box:
                    detected_boxes_with_labels.append((anchor_box, "safe_anchor"))
                    logger.info(f"[Censor] 포즈 {pose_code}: 안전 앵커 폴백 박스 적용 {anchor_box}")

    if not detected_boxes_with_labels:
        return img, 0

    detected_boxes = [box for box, _ in detected_boxes_with_labels]
    style_clean = style.lower().strip()

    if style_clean in ("white_silhouette", "white", "whiteout"):
        censored = apply_white_silhouette(img, detected_boxes_with_labels)
    elif style_clean in ("black_silhouette", "silhouette_black", "black"):
        censored = apply_black_silhouette(img, detected_boxes_with_labels)
    elif style_clean in ("silhouette", "contour"):
        # 기본 실루엣: 화이트 실루엣
        censored = apply_white_silhouette(img, detected_boxes_with_labels)
    elif style_clean in ("bar", "wide_bar", "solid_bar", "black_bar", "pill_bar"):
        censored = apply_wide_solid_bar(img, detected_boxes)
    elif style_clean in ("slim_bar", "thin_bar"):
        censored = apply_slim_black_bar(img, detected_boxes)
    elif style_clean in ("shadow", "shadow_box", "shadow_silhouette"):
        censored = apply_shadow_silhouette(img, detected_boxes)
    elif style_clean in ("mosaic", "pixel"):
        censored = apply_mosaic(img, detected_boxes)
    else:
        censored = apply_white_silhouette(img, detected_boxes_with_labels)

    return censored, len(detected_boxes_with_labels)


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
) -> Optional[Path]:
    """
    파일 경로를 입력받아 검열본을 생성하고 저장합니다.
    - dst_path 미지정 시 'stem_censored.webp'로 자동 저장.
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

    img = Image.open(src).convert("RGB")
    censored_img, count = censor_image(
        img,
        style=style,
        targets=targets,
        min_confidence=min_confidence,
        pose_code=pose_code,
        enable_anchor_fallback=enable_anchor_fallback,
    )

    if count == 0:
        return None

    if dst_path is None:
        target_dst = src.with_name(f"{src.stem}_censored{src.suffix}")
    else:
        target_dst = Path(dst_path)

    target_dst.parent.mkdir(parents=True, exist_ok=True)
    censored_img.save(target_dst, "WEBP", quality=quality)
    return target_dst


def detect_genitals(
    image: Union[Path, str, Image.Image],
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
) -> List[Tuple[Tuple[int, int, int, int], str, float]]:
    """이미지에서 검열 대상 부위(성기 등)를 감지하여 [(box, label, score), ...] 형태로 반환합니다."""
    if not HAS_IMGUTILS:
        return []
    try:
        if isinstance(image, (str, Path)):
            img = Image.open(str(image)).convert("RGB")
        else:
            img = image.convert("RGB")
        results = detect_censors(img)
        filtered = []
        normalized = set(t.lower() for t in targets)
        for box, label, score in results:
            if label.lower() in normalized and score >= min_confidence:
                filtered.append((box, label, score))
        return filtered
    except Exception as e:
        logger.warning(f"[Censor] detect_genitals 감지 예외: {e}")
        return []


def run_batch_censor(
    folder_path: Union[Path, str],
    style: str = "white_silhouette",
    overwrite: bool = True,
    recursive: bool = True,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    enable_anchor_fallback: bool = True,
) -> int:
    """폴더 내의 모든 일러스트 에셋을 스캔하여 일괄 검열을 수행합니다."""
    base_dir = Path(folder_path).resolve()
    if not base_dir.exists():
        logger.error(f"[오류] 대상 폴더를 찾을 수 없습니다: {base_dir}")
        return 0

    extensions = ["*.webp", "*.png", "*.jpg", "*.jpeg"]
    image_files: List[Path] = []
    for ext in extensions:
        if recursive:
            image_files.extend(base_dir.rglob(ext))
        else:
            image_files.extend(base_dir.glob(ext))

    target_files = [f for f in image_files if not f.stem.endswith("_censored")]
    total = len(target_files)
    processed_count = 0

    for idx, file_path in enumerate(target_files, 1):
        match = re.search(r"_(\d{3})\.", file_path.name)
        pose_code = int(match.group(1)) if match else None

        if pose_code is not None and get_pose_category(pose_code) is None:
            continue

        try:
            dst = file_path if overwrite else file_path.with_name(f"{file_path.stem}_censored{file_path.suffix}")
            saved = censor_file(
                src_path=file_path,
                dst_path=dst,
                style=style,
                targets=targets,
                min_confidence=min_confidence,
                pose_code=pose_code,
                enable_anchor_fallback=enable_anchor_fallback,
            )
            if saved is not None:
                processed_count += 1
        except Exception as e:
            logger.warning(f"[Censor] 배치 검열 실패: {file_path.name} ({e})")

    return processed_count



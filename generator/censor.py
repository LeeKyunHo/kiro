"""
generator.censor
2D 애니메이션 일러스트 전용 자동 성기 검열(Censorship) 엔진.
플에파(plepa) 프로덕션급 검열 시스템을 키로(kiro) 152종 포즈 규격으로 완벽 이식.

주요 스타일:
- 화이트 외곽선 실루엣 (White Silhouette, 기본 권장)
- 블랙 외곽선 실루엣 (Black Silhouette via MobileSAM)
- 와이드 솔리드 캡슐 바 (Wide Solid Bar, 모서리 라운딩)
- 슬림 사선 블랙 바 (Thin Black Bar, 25도 사선 미니멀)
- 고급 그림자 음영 박스 (Shadow Box, 선화 디테일 차단)
- 격자 모자이크 (Pixelated Mosaic, 클래식 픽셀레이션)

특징:
- MobileSAM / imgutils / NudeNet 다층 딥러닝 감지 엔진 지원
- 손(Hands) 및 얼굴(Faces) 보호 마스크 추출로 선화 파손 방지
- 키로 152종(048~099, 148~199) 체위별 안전 앵커(Safe Anchor) 폴백 탑재
"""

from __future__ import annotations

import argparse
import glob
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

# ── 윈도우 한글/특수문자 경로 안전 디코딩 ──
_original_imread = cv2.imread

def safe_imread(filename: str, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    try:
        data = np.fromfile(filename, dtype=np.uint8)
        img = cv2.imdecode(data, flags)
        if img is not None:
            return img
    except Exception:
        pass
    return _original_imread(filename, flags)

cv2.imread = safe_imread

# ── 외부 딥러닝 라이브러리 가용성 점검 ──
try:
    from imgutils.detect import detect_censors
    HAS_IMGUTILS = True
except ImportError:
    HAS_IMGUTILS = False

try:
    from nudenet import NudeDetector
    HAS_NUDENET = True
except ImportError:
    HAS_NUDENET = False

# MobileSAM 및 NudeNet 싱글톤 (Lazy Load)
_SAM_MODEL = None
_NUDE_DETECTOR = None


def get_sam_model():
    """MobileSAM 세그멘테이션 모델을 싱글톤으로 로드합니다."""
    global _SAM_MODEL
    if _SAM_MODEL is None:
        try:
            from ultralytics import SAM
            _SAM_MODEL = SAM("mobile_sam.pt")
            logger.info("[Censor] MobileSAM 모델 로드 완료 (mobile_sam.pt)")
        except Exception as e:
            logger.debug(f"[Censor] MobileSAM 로드 실패, OpenCV 폴백 모드로 동작합니다: {e}")
            _SAM_MODEL = False
    return _SAM_MODEL if _SAM_MODEL is not False else None


def get_nude_detector():
    """NudeNet 감지기를 싱글톤으로 로드합니다."""
    global _NUDE_DETECTOR
    if _NUDE_DETECTOR is None:
        if HAS_NUDENET:
            try:
                _NUDE_DETECTOR = NudeDetector()
                logger.info("[Censor] NudeNet 디텍터 로드 완료")
            except Exception as e:
                logger.debug(f"[Censor] NudeNet 초기화 실패: {e}")
                _NUDE_DETECTOR = False
        else:
            _NUDE_DETECTOR = False
    return _NUDE_DETECTOR if _NUDE_DETECTOR is not False else None


# ── 1. 실루엣 검열 렌더러 (Silhouette via MobileSAM / OpenCV GrabCut) ──

def apply_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Tuple[int, int, int, int], str]],
    color: Union[str, Tuple[int, int, int]] = "white",
) -> Image.Image:
    """
    네모난 박스가 아니라 남성기 전체와 고환의 실제 선화 외곽선(Contour)을 따라
    내부만 솔리드 컬러(화이트 #FFFFFF 또는 블랙 #000000)로 매끈하게 채우는 실루엣 검열을 렌더링합니다.
    - MobileSAM BBox Prompting으로 픽셀 단위 마스크 추출.
    - 펠라/오랄 구도에서 여주인공 신체 침범을 차단하는 적응형 마진 및 고립 노이즈 정제.
    - 타원 커널 모폴로지 클로징 및 가우시안 안티앨리어싱으로 계단 현상 없는 유려한 곡선 완성.
    """
    w, h = img.size
    img_np = np.array(img.convert("RGB"))
    final_mask = np.zeros((h, w), dtype=np.uint8)

    sam = get_sam_model()

    # 0. 손(Hands) 및 얼굴(Faces) 보호 마스크 추출
    protection_mask = np.zeros((h, w), dtype=np.uint8)
    try:
        from imgutils.detect import detect_faces, detect_hands
        detected_hands = detect_hands(img, conf_threshold=0.25)
        detected_faces = detect_faces(img, conf_threshold=0.30)

        overlapping_protect_boxes = []
        for box, _ in boxes_with_labels:
            bx1, by1, bx2, by2 = box
            for (hx1, hy1, hx2, hy2), _, _ in detected_hands:
                if max(bx1, hx1) < min(bx2, hx2) and max(by1, hy1) < min(by2, hy2):
                    overlapping_protect_boxes.append([hx1, hy1, hx2, hy2])
            for (fx1, fy1, fx2, fy2), _, _ in detected_faces:
                if max(bx1, fx1) < min(bx2, fx2) and max(by1, fy1) < min(by2, fy2):
                    overlapping_protect_boxes.append([fx1, fy1, fx2, fy2])

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

        is_penis = label in ("penis", "male_genitalia", "MALE_GENITALIA_EXPOSED")
        if is_penis:
            # 적응형 마진: 가로형(bw >= bh)은 오랄/펠라 등 신체 밀착 구도이므로 하단 확장 억제(0.04),
            # 세로형(bh > bw * 1.3)일 때만 고환 영역을 고려하여 하단 10% 확장
            m_x = 0.04
            m_top = 0.02
            m_bot = 0.08 if bh > bw * 1.3 else 0.02
            ex1 = max(0, int(x1 - bw * m_x))
            ey1 = max(0, int(y1 - bh * m_top))
            ex2 = min(w, int(x2 + bw * m_x))
            ey2 = min(h, int(y2 + bh * m_bot))
        else:
            ex1 = max(0, int(x1 - bw * 0.03))
            ey1 = max(0, int(y1 - bh * 0.03))
            ex2 = min(w, int(x2 + bw * 0.03))
            ey2 = min(h, int(y2 + bh * 0.03))

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

                    # 노이즈 컨투어 정제: 면적 300px 미만 또는 주 컨투어 대비 5% 미만 고립 파편 제거
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
                logger.debug(f"[Censor] MobileSAM 추론 실패, OpenCV 폴백: {e}")

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

    # 손가락 및 얼굴 보호 레이어 차감 (1~2px 팽창으로 경계선 보존)
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


# ── 2. 와이드 솔리드 바 렌더러 (Wide Solid Bar via Rounded Rectangle) ──

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


# ── 3. 슬림 사선 블랙 바 렌더러 (Slim Black Bar) ──

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


# ── 4. 그림자 음영 박스 렌더러 (Shadow Box) ──

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


# ── 5. 격자 모자이크 렌더러 (Mosaic) ──

def apply_mosaic(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    block_size: int = 14,
) -> Image.Image:
    """클래식 격자 모자이크(Pixelated Mosaic)를 적용합니다."""
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


# ── 6. 키로 152종 체위별 안전 앵커 (Safe Anchor Coordinates) ──

# 정규화 좌표: [x0, y0, x1, y1] (0.0 ~ 1.0)
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
    "032", "033", "034", "035", "037",
    "098", "099", "198", "199",
    "101", "102", "104", "105", "171", "172", "174", "175",  # 플에파 레거시 번호 호환
})


def get_pose_category(pose_code: Union[str, int]) -> Optional[str]:
    """
    키로 152종 정예화 체계(048~099, 148~199) 및 레거시 번호 대역에 따른 체위 카테고리를 반환합니다.
    오토코노코 씬(148~199)은 여성 씬(048~099)과 1:1 대칭 매핑됩니다.
    비노출 씬이거나 일반 일상 포즈는 None을 반환합니다.
    """
    code_str = str(pose_code).zfill(3)
    if code_str in NON_EXPOSURE_H_POSES:
        return None

    try:
        code_int = int(code_str)
    except ValueError:
        return None

    # 오토코노코 대칭 변환 (148~199 -> 048~099)
    base_code = code_int % 100 if (148 <= code_int <= 199 or 140 <= code_int <= 170) else code_int
    base_str = str(base_code).zfill(3)

    # 1. 152종 표준 체계 (048~099, 148~199 대칭 매핑)
    if 48 <= base_code <= 99:
        # 구강/오랄/펠라/핸드잡
        if base_str in ("068", "069", "070", "071", "086", "089", "096", "097"):
            return "fellatio"
        # 후배위/배면/애널/스마타
        if base_str in ("057", "058", "059", "090", "091", "093"):
            return "doggystyle"
        # 기승위/승마
        if base_str in ("060", "061", "062"):
            return "cowgirl"
        # 측와위/입위/스푸닝
        if base_str in ("051", "072", "073", "083", "088"):
            return "side"
        # 정상위/배위/교배프레스/좌위/사후여운 등
        if base_str in ("048", "049", "050", "054", "055", "056", "063", "064", "065", "066", "067", "081", "082", "084", "085", "087", "092", "094", "095"):
            return "missionary"
        return "default_h"

    # 2. 레거시 번호 대역 (40~75, 80~130)
    if base_code in (58, 59, 79, 80, 83, 84, 85, 126, 127):
        return "fellatio"
    if base_code in (43, 44, 62, 63, 64, 65, 66, 74, 88, 89, 90, 118, 121):
        return "doggystyle"
    if base_code in (45, 46, 47, 48, 68, 69, 70, 71, 72, 73, 91, 92, 93, 109, 114):
        return "cowgirl"
    if base_code in (51, 52, 53, 98, 119):
        return "side"
    if base_code in (40, 41, 49, 50, 54, 55, 56, 57, 60, 61, 80, 81, 82, 83, 84, 85, 94, 96, 97):
        return "missionary"
    if (40 <= code_int <= 75) or (80 <= code_int <= 130) or (140 <= code_int <= 170) or (150 <= code_int <= 200):
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


def merge_boxes(boxes: Sequence[Sequence[int]], pad: int = 20) -> List[List[int]]:
    """인접하거나 중첩되는 바운딩 박스들을 하나로 병합합니다."""
    if not boxes:
        return []

    sorted_boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    merged: List[List[int]] = []
    current = list(sorted_boxes[0])

    for nxt in sorted_boxes[1:]:
        if (nxt[1] <= current[3] + pad and 
            not (nxt[2] < current[0] - pad or nxt[0] > current[2] + pad)):
            current = [
                min(current[0], nxt[0]),
                min(current[1], nxt[1]),
                max(current[2], nxt[2]),
                max(current[3], nxt[3])
            ]
        else:
            merged.append(current)
            current = list(nxt)
    merged.append(current)
    return merged


# ── 7. 통합 이미지 검열 파이프라인 (Censor Image Pipeline) ──

def detect_genitals(
    image: Union[Image.Image, Path, str],
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
) -> List[Tuple[Tuple[int, int, int, int], str]]:
    """
    이미지 내 성기 부위를 감지하여 바운딩 박스와 라벨 목록을 반환합니다.
    - 플에파(plepa) strict_censor_qc 및 키로 Auto-QC 연동용
    - 반환 형식: [((x1, y1, x2, y2), label), ...]
    """
    if isinstance(image, (str, Path)):
        img = Image.open(str(image)).convert("RGB")
    else:
        img = image.convert("RGB")

    w, h = img.size

    # 정규화된 타깃 태그 세트
    normalized_targets = set()
    for t in targets:
        t_clean = str(t).strip().lower()
        if t_clean in ("all", "both"):
            normalized_targets.update(["penis", "pussy", "nipple_f", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"])
        elif t_clean in ("genital", "genitals", "sex"):
            normalized_targets.update(["penis", "pussy", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"])
        elif t_clean in ("pussy", "vagina", "vulva"):
            normalized_targets.update(["pussy", "FEMALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"])
        elif t_clean in ("penis", "cock", "dick"):
            normalized_targets.update(["penis", "MALE_GENITALIA_EXPOSED"])
        elif t_clean in ("nipple", "nipples", "nipple_f"):
            normalized_targets.update(["nipple_f", "FEMALE_BREAST_EXPOSED"])
        else:
            normalized_targets.add(t_clean)

    detected_boxes_with_labels: List[Tuple[Tuple[int, int, int, int], str]] = []

    # 1. imgutils.detect 기반 정밀 애니 감지
    if HAS_IMGUTILS:
        try:
            results = detect_censors(img)
            for box, label, score in results:
                if label in normalized_targets and score >= min_confidence:
                    x1, y1, x2, y2 = [int(v) for v in box]
                    detected_boxes_with_labels.append(((x1, y1, x2, y2), label))
        except Exception as e:
            logger.debug(f"[Censor] imgutils 추론 예외: {e}")

    # 2. imgutils 미탑재이거나 미탐지 시 NudeNet 폴백
    if not detected_boxes_with_labels:
        detector = get_nude_detector()
        if detector is not None:
            try:
                img_np = np.array(img)
                detections = detector.detect(img_np)
                for d in detections:
                    c_name = d["class"]
                    score = d["score"]
                    if score >= max(0.08, min_confidence * 0.5):
                        if c_name in normalized_targets or (c_name in ["FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"]):
                            x, y, bw, bh = d["box"]
                            if y < h * 0.30 and (y + bh) < h * 0.42:
                                continue
                            x0 = max(0, x - int(bw * 0.08))
                            y0 = max(0, y - int(bh * 0.05))
                            x1 = min(w, x + bw + int(bw * 0.08))
                            y1 = min(h, y + bh + int(bh * 0.30))
                            lbl = "penis" if "MALE" in c_name else "pussy"
                            detected_boxes_with_labels.append(((x0, y0, x1, y1), lbl))
            except Exception as e:
                logger.debug(f"[Censor] NudeNet 추론 예외: {e}")

    # 3. AI 미탐지 시 체위별 세이프 앵커 폴백
    if not detected_boxes_with_labels and pose_code is not None:
        cat = get_pose_category(pose_code)
        if cat:
            logger.info(f"[Censor] 포즈 {pose_code}: 성기 미탐지! 안전 앵커({cat}) 폴백 점검")
            if enable_anchor_fallback:
                anchor_box = get_pose_anchor_box(pose_code, w, h)
                if anchor_box:
                    detected_boxes_with_labels.append((anchor_box, "safe_anchor"))
                    logger.info(f"[Censor] 포즈 {pose_code}: 안전 앵커 폴백 박스 적용 {anchor_box}")

    return detected_boxes_with_labels


def censor_image(
    image: Union[Image.Image, Path, str],
    style: str = "white_silhouette",
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = True,
) -> Tuple[Image.Image, int]:
    """
    이미지 내 성기 부위를 자동으로 감지하여 지정된 스타일로 검열을 적용합니다.
    - 지원 스타일: 'white_silhouette', 'black_silhouette', 'wide_bar', 'slim_bar', 'shadow', 'mosaic'
    - 반환값: (검열된 PIL 이미지, 검열 적용 부위 수)
    """
    if isinstance(image, (str, Path)):
        img = Image.open(str(image)).convert("RGB")
    else:
        img = image.convert("RGB")

    detected_boxes_with_labels = detect_genitals(
        img,
        targets=targets,
        min_confidence=min_confidence,
        pose_code=pose_code,
        enable_anchor_fallback=enable_anchor_fallback,
    )

    if not detected_boxes_with_labels:
        return img, 0

    # 겹치는 박스 병합
    raw_boxes = [box for box, _ in detected_boxes_with_labels]
    merged_box_coords = merge_boxes(raw_boxes)
    # 레이블 매핑 유지
    final_boxes_with_labels: List[Tuple[Tuple[int, int, int, int], str]] = []
    for mb in merged_box_coords:
        mb_tuple = (mb[0], mb[1], mb[2], mb[3])
        # 근접한 원래 레이블 승계
        lbl = "penis"
        for orig_b, orig_lbl in detected_boxes_with_labels:
            if max(mb[0], orig_b[0]) < min(mb[2], orig_b[2]) and max(mb[1], orig_b[1]) < min(mb[3], orig_b[3]):
                lbl = orig_lbl
                break
        final_boxes_with_labels.append((mb_tuple, lbl))

    detected_boxes = [box for box, _ in final_boxes_with_labels]
    style_clean = style.lower().strip()

    if style_clean in ("white_silhouette", "white", "whiteout"):
        censored = apply_white_silhouette(img, final_boxes_with_labels)
    elif style_clean in ("black_silhouette", "silhouette_black", "black"):
        censored = apply_black_silhouette(img, final_boxes_with_labels)
    elif style_clean in ("silhouette", "contour"):
        censored = apply_white_silhouette(img, final_boxes_with_labels)
    elif style_clean in ("bar", "wide_bar", "solid_bar", "black_bar", "pill_bar"):
        censored = apply_wide_solid_bar(img, detected_boxes)
    elif style_clean in ("slim_bar", "thin_bar"):
        censored = apply_slim_black_bar(img, detected_boxes)
    elif style_clean in ("shadow", "shadow_box", "shadow_silhouette"):
        censored = apply_shadow_silhouette(img, detected_boxes)
    elif style_clean in ("mosaic", "pixel"):
        censored = apply_mosaic(img, detected_boxes)
    else:
        censored = apply_white_silhouette(img, final_boxes_with_labels)

    return censored, len(final_boxes_with_labels)


def censor_file(
    src_path: Union[Path, str],
    dst_path: Optional[Union[Path, str]] = None,
    style: str = "white_silhouette",
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.15,
    quality: int = 95,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = True,
) -> Optional[Path]:
    """
    단일 파일 경로를 입력받아 검열본을 생성하고 저장합니다.
    - dst_path 미지정 시 'stem_censored.webp'로 자동 저장.
    - 감지된 검열 대상이 없는 경우 검열본을 생성하지 않고 None 반환.
    """
    src = Path(src_path)
    if not src.exists():
        return None

    # 파일명에서 포즈 코드 자동 추출 시도 (예: aya_054.webp)
    if pose_code is None:
        match = re.search(r"_(\d{3})\.", src.name)
        if match:
            pose_code = int(match.group(1))

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
    fmt = "WEBP" if target_dst.suffix.lower() == ".webp" else ("PNG" if target_dst.suffix.lower() == ".png" else "JPEG")
    censored_img.save(target_dst, format=fmt, quality=quality)
    return target_dst


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
        print(f"[오류] 대상 폴더를 찾을 수 없습니다: {base_dir}")
        return 0

    extensions = ["*.webp", "*.png", "*.jpg", "*.jpeg"]
    image_files: List[Path] = []
    for ext in extensions:
        if recursive:
            image_files.extend(base_dir.rglob(ext))
        else:
            image_files.extend(base_dir.glob(ext))

    # 이미 검열된 파일(_censored) 제외
    target_files = [f for f in image_files if not f.stem.endswith("_censored")]
    total = len(target_files)

    print(f"\n[스캔 시작] 총 {total}장의 일러스트 에셋 검사를 시작합니다.")
    print(f"[설정] 스타일: {style} | 원본 덮어쓰기: {overwrite} | 안전 앵커: {enable_anchor_fallback}")

    processed_count = 0
    for idx, file_path in enumerate(target_files, 1):
        match = re.search(r"_(\d{3})\.", file_path.name)
        pose_code = int(match.group(1)) if match else None

        # 비노출 씬인지 사전 점검
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
                print(f"[{idx}/{total}] 검열 완료: {file_path.name}")
        except Exception as e:
            print(f"[{idx}/{total}] 처리 실패: {file_path.name} ({e})")

    print(f"\n[작업 완료] 총 {processed_count}장의 에셋에 정밀 검열을 적용했습니다.\n")
    return processed_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="키로(kiro) 2D 애니메이션 일러스트 고정밀 성기 검열 엔진")
    parser.add_argument("-d", "--dir", type=str, help="검열할 이미지 폴더 경로")
    parser.add_argument("-f", "--file", type=str, help="단일 이미지 파일 경로")
    parser.add_argument(
        "-m", "--style",
        type=str,
        default="white_silhouette",
        choices=["white_silhouette", "black_silhouette", "wide_bar", "slim_bar", "shadow", "mosaic"],
        help="검열 렌더링 스타일 (기본값: white_silhouette)"
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=["penis", "pussy"],
        help="검열 대상 라벨 (기본값: penis pussy)"
    )
    parser.add_argument("--conf", type=float, default=0.15, help="감지 신뢰도 임계값 (기본값: 0.15)")
    parser.add_argument("--no-overwrite", action="store_true", help="원본을 덮어쓰지 않고 _censored 파일로 별도 저장")
    parser.add_argument("--no-anchor", action="store_true", help="AI 미탐지 시 안전 앵커 폴백 비활성화")

    args = parser.parse_args()

    if args.file:
        dst = None if not args.no_overwrite else Path(args.file).with_name(f"{Path(args.file).stem}_censored{Path(args.file).suffix}")
        out = censor_file(
            src_path=args.file,
            dst_path=dst,
            style=args.style,
            targets=args.targets,
            min_confidence=args.conf,
            enable_anchor_fallback=not args.no_anchor,
        )
        if out:
            print(f"[완료] 검열본 저장: {out}")
        else:
            print("[통과] 검열 대상 부위가 감지되지 않았습니다.")
    elif args.dir:
        run_batch_censor(
            folder_path=args.dir,
            style=args.style,
            overwrite=not args.no_overwrite,
            targets=args.targets,
            min_confidence=args.conf,
            enable_anchor_fallback=not args.no_anchor,
        )
    else:
        parser.print_help()

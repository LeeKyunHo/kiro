"""
generator.censor_styles
2D 애니메이션 일러스트 검열 렌더링 스타일 모듈.
- 동적 해상도 스케일 커널 (832px 기준, 4K 업스케일 자동 비례)
- 와이드 솔리드 캡슐 바 (apply_wide_solid_bar)
- 슬림 사선 블랙 바 (apply_slim_black_bar)
- 소프트 타원 페더링 그림자 (apply_shadow_silhouette)
- ROI 국소 픽셀화 모자이크 (apply_mosaic)
- 주축·기저부 기하 기반 망가 바 / 망가 화이트 글로우 (apply_manga_bar, apply_manga_glow)
- BBox·신뢰도 진단 시각화 (apply_debug_bbox)
- 공용 스타일 라우터 (_render_censored_style)
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

_BASE_RESOLUTION: float = 832.0


def _compute_scale_factor(width: int, height: int) -> float:
    """기준 해상도(832px) 대비 현재 이미지 비율 스케일 팩터를 계산한다."""
    return max(0.5, float(min(max(1, width), max(1, height))) / _BASE_RESOLUTION)


def _scale_int(base: int, scale: float, minimum: int = 1) -> int:
    """스케일 팩터를 반영한 정수 픽셀 값을 반환한다."""
    return max(minimum, int(round(base * scale)))


def _odd_kernel(base: int, scale: float, minimum: int = 3) -> int:
    """OpenCV 커널/블러용 홀수 정수 크기를 반환한다."""
    val = max(minimum, int(round(base * scale)))
    return val if (val % 2 == 1) else (val + 1)


def _split_rgb_and_alpha(img: Image.Image) -> Tuple[Image.Image, Optional[Image.Image]]:
    """RGB 변환과 함께 알파 채널(RGBA/LA/투명 P)을 안전하게 분리 보존한다."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        return rgba.convert("RGB"), alpha
    if img.mode == "RGB":
        return img, None
    return img.convert("RGB"), None


def _restore_alpha(rgb_img: Image.Image, alpha: Optional[Image.Image]) -> Image.Image:
    """분리된 알파 채널이 있는 경우 RGBA 이미지로 복원한다."""
    if alpha is None:
        return rgb_img
    rgba = rgb_img.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba


def get_skin_mask(img_np: np.ndarray) -> np.ndarray:
    """
    2D 애니메이션 일러스트에서 밝은 피부 영역부터 태닝/음영 피부톤까지 HSV 필터로 추출한다.
    """
    hsv = cv2.cvtColor(img_np, cv2.COLOR_RGB2HSV)
    lower1 = np.array([0, 15, 55], dtype=np.uint8)
    upper1 = np.array([32, 190, 255], dtype=np.uint8)
    mask1 = cv2.inRange(hsv, lower1, upper1)

    lower2 = np.array([162, 15, 55], dtype=np.uint8)
    upper2 = np.array([180, 190, 255], dtype=np.uint8)
    mask2 = cv2.inRange(hsv, lower2, upper2)
    return cv2.bitwise_or(mask1, mask2)


def _normalize_censor_boxes(boxes: Sequence[Any]) -> List[Tuple[int, int, int, int]]:
    """단순 4-튜플 BBox 또는 `(bbox, label, ...)` 시퀀스를 `(x1, y1, x2, y2)` 리스트로 정규화한다."""
    normalized: List[Tuple[int, int, int, int]] = []
    for item in boxes:
        raw = item[0] if isinstance(item, (list, tuple)) and item and isinstance(item[0], (list, tuple)) else item
        normalized.append((int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3])))
    return normalized


def _to_rgb_ndarray(img_or_np: Union[Image.Image, np.ndarray]) -> np.ndarray:
    """PIL Image 또는 NumPy 배열을 RGB uint8 `(H, W, 3)` 배열로 변환한다."""
    if isinstance(img_or_np, np.ndarray):
        if img_or_np.ndim == 2:
            return cv2.cvtColor(img_or_np, cv2.COLOR_GRAY2RGB)
        if img_or_np.shape[2] == 4:
            return img_or_np[:, :, :3]
        return img_or_np
    return np.array(img_or_np.convert("RGB"), dtype=np.uint8)


def _extract_roi_foreground_mask(roi_rgb: np.ndarray, roi_skin: Optional[np.ndarray]) -> np.ndarray:
    """ROI 내부의 피부 또는 배경 대비 전경 픽셀 마스크(`bool`)를 추출한다."""
    rh, rw = roi_rgb.shape[:2]
    area = max(1, rh * rw)
    min_px = max(16, int(area * 0.03))

    skin_bool = (roi_skin > 0) if roi_skin is not None else (get_skin_mask(roi_rgb) > 0)
    skin_count = int(np.count_nonzero(skin_bool))

    if min_px <= skin_count <= int(area * 0.80):
        return skin_bool

    border_samples = np.concatenate(
        [roi_rgb[0, :], roi_rgb[-1, :], roi_rgb[:, 0], roi_rgb[:, -1]],
        axis=0,
    ).astype(np.float32)
    border_bg = np.median(border_samples, axis=0)
    dist = np.linalg.norm(roi_rgb.astype(np.float32) - border_bg, axis=2)
    max_dist = float(dist.max()) if dist.size else 0.0
    if max_dist > 16.0:
        contrast_bool = dist > max(18.0, max_dist * 0.35)
        contrast_count = int(np.count_nonzero(contrast_bool))
        if min_px <= contrast_count <= int(area * 0.82):
            return contrast_bool

    if skin_count >= min_px:
        return skin_bool
    return np.zeros((rh, rw), dtype=bool)


def _bbox_fallback_geometry(
    x1: int, y1: int, x2: int, y2: int, pose_cat: Optional[str] = None
) -> Dict[str, Any]:
    """픽셀 부족 또는 등방성 ROI에 대한 BBox 종횡비 기반 기하 폴백을 생성한다."""
    bw = max(1.0, float(x2 - x1))
    bh = max(1.0, float(y2 - y1))
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    if bw >= bh:
        ux, uy = 1.0, 0.0
        major_len = bw * 0.84
        minor_len = bh * 0.56
    else:
        ux, uy = 0.0, -1.0
        major_len = bh * 0.84
        minor_len = bw * 0.56

    if pose_cat and any(k in str(pose_cat).lower() for k in ("fellatio", "deepthroat", "oral", "blowjob", "irrumatio")):
        if abs(uy) < 0.25:
            ux, uy = 1.0, 0.0
        elif uy > 0:
            ux, uy = -ux, -uy

    half_len = major_len * 0.5
    base_t = -half_len * 0.65
    ry = max(4.0, minor_len * 0.425)
    rx = max(ry * 0.85, major_len * 0.20)
    return {
        "center": (cx, cy),
        "axis": (ux, uy),
        "angle": math.degrees(math.atan2(uy, ux)),
        "major_len": major_len,
        "minor_len": minor_len,
        "shaft_width": minor_len * 0.72,
        "shaft_start": (cx - ux * half_len, cy - uy * half_len),
        "shaft_end": (cx + ux * half_len, cy + uy * half_len),
        "base_center": (cx + ux * base_t, cy + uy * base_t),
        "base_radii": (rx, ry),
        "fallback": True,
    }


def _slice_cross_width(n_vals: np.ndarray, mask: np.ndarray, default_val: float) -> float:
    """주축 구간 슬라이스 내부의 수직 폭(`P98 - P2`)을 계산한다."""
    if int(np.count_nonzero(mask)) < 4:
        return default_val
    vals = n_vals[mask]
    return max(2.0, float(np.percentile(vals, 98.0) - np.percentile(vals, 2.0)))


def _estimate_penis_axis_and_base(
    img_np_or_rgb: Union[Image.Image, np.ndarray],
    bbox: Tuple[int, int, int, int],
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
) -> Dict[str, Any]:
    """
    BBox 내부 전경 마스크의 PCA 주축(기울기 각도)과 양 끝단 단면 폭을 비교해
    주축 방향과 기저부(Base) 위치·반경을 추정한다.
    """
    img_np = _to_rgb_ndarray(img_np_or_rgb)
    h_img, w_img = img_np.shape[:2]
    x1 = max(0, min(w_img - 1, int(bbox[0])))
    y1 = max(0, min(h_img - 1, int(bbox[1])))
    x2 = max(x1 + 1, min(w_img, int(bbox[2])))
    y2 = max(y1 + 1, min(h_img, int(bbox[3])))

    roi_rgb = img_np[y1:y2, x1:x2]
    roi_skin = skin_np[y1:y2, x1:x2] if skin_np is not None else None
    fg_mask = _extract_roi_foreground_mask(roi_rgb, roi_skin)
    ys, xs = np.nonzero(fg_mask)
    roi_area = max(1, (x2 - x1) * (y2 - y1))

    if len(xs) < 16 or len(xs) > int(roi_area * 0.88):
        return _bbox_fallback_geometry(x1, y1, x2, y2, pose_cat=pose_cat)

    pts = np.column_stack([xs.astype(np.float64) + x1, ys.astype(np.float64) + y1])
    mean = pts.mean(axis=0)
    centered = pts - mean
    cov = np.cov(centered, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(cov)
    lambda_minor = float(max(eigvals[0], 1e-6))
    lambda_major = float(max(eigvals[1], 1e-6))

    bw, bh = float(x2 - x1), float(y2 - y1)
    bbox_elongated = max(bw, bh) / max(1.0, min(bw, bh)) >= 1.35
    if (lambda_major / lambda_minor) < 1.25 and not bbox_elongated:
        return _bbox_fallback_geometry(x1, y1, x2, y2, pose_cat=pose_cat)

    u = eigvecs[:, 1].astype(np.float64)
    v = np.array([-u[1], u[0]], dtype=np.float64)
    t_vals = centered @ u
    n_vals = centered @ v

    t_min = float(np.percentile(t_vals, 1.0))
    t_max = float(np.percentile(t_vals, 99.0))
    span = max(6.0, t_max - t_min)

    neg_mask = t_vals <= (t_min + 0.30 * span)
    pos_mask = t_vals >= (t_max - 0.30 * span)
    mid_mask = (t_vals >= (t_min + 0.32 * span)) & (t_vals <= (t_max - 0.32 * span))

    fallback_w = max(4.0, float(np.percentile(n_vals, 95.0) - np.percentile(n_vals, 5.0)))
    width_neg = _slice_cross_width(n_vals, neg_mask, fallback_w)
    width_pos = _slice_cross_width(n_vals, pos_mask, fallback_w)
    shaft_width = _slice_cross_width(n_vals, mid_mask, min(width_neg, width_pos))

    is_oral = bool(
        pose_cat
        and any(k in str(pose_cat).lower() for k in ("fellatio", "deepthroat", "oral", "blowjob", "irrumatio"))
    )
    end_neg_pt = mean + u * (t_min + 0.16 * span)
    end_pos_pt = mean + u * (t_max - 0.16 * span)
    if is_oral and abs(float(end_neg_pt[1] - end_pos_pt[1])) > 0.15 * span:
        base_is_pos = float(end_pos_pt[1]) >= float(end_neg_pt[1])
    else:
        base_is_pos = width_pos > width_neg

    if base_is_pos:
        u = -u
        t_min, t_max = -t_max, -t_min
        base_width = width_pos
    else:
        base_width = width_neg

    ux, uy = float(u[0]), float(u[1])
    cx, cy = float(mean[0]), float(mean[1])
    overall_minor = min(max(shaft_width, base_width, fallback_w), max(bw, bh) * 0.95)

    base_t = t_min + 0.18 * span
    bx = cx + ux * base_t
    by = cy + uy * base_t

    ry = max(4.0, max(overall_minor * 0.85, base_width * 0.92) * 0.50)
    rx = max(ry * 0.82, span * 0.20)
    return {
        "center": (cx, cy),
        "axis": (ux, uy),
        "angle": math.degrees(math.atan2(uy, ux)),
        "major_len": span,
        "minor_len": overall_minor,
        "shaft_width": max(4.0, shaft_width),
        "shaft_start": (cx + ux * t_min, cy + uy * t_min),
        "shaft_end": (cx + ux * t_max, cy + uy * t_max),
        "base_center": (bx, by),
        "base_radii": (rx, ry),
        "fallback": False,
    }


def _rotated_rect_points(
    center: Tuple[float, float],
    axis: Tuple[float, float],
    length_along: float,
    thickness_perp: float,
) -> np.ndarray:
    """중심·방향벡터(`axis`)·길이·두께로부터 회전 직사각형 4꼭짓점(`int32`)을 계산한다."""
    cx, cy = center
    ux, uy = axis
    vx, vy = -uy, ux
    hl = max(1.0, length_along * 0.5)
    ht = max(1.0, thickness_perp * 0.5)
    return np.array(
        [
            [cx - ux * hl - vx * ht, cy - uy * hl - vy * ht],
            [cx + ux * hl - vx * ht, cy + uy * hl - vy * ht],
            [cx + ux * hl + vx * ht, cy + uy * hl + vy * ht],
            [cx - ux * hl + vx * ht, cy - uy * hl + vy * ht],
        ],
        dtype=np.int32,
    )


def apply_wide_solid_bar(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    margin_ratio: float = 0.18,
    corner_radius_ratio: float = 0.25,
    min_margin: int = 6,
) -> Image.Image:
    """
    2D 서브컬처 표준 '와이드 솔리드 캡슐 바(Wide Solid Bar)'를 동적 해상도 스케일로 렌더링한다.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    canvas = rgb_img.copy()
    draw = ImageDraw.Draw(canvas)
    img_w, img_h = canvas.size
    scale = _compute_scale_factor(img_w, img_h)
    scaled_min_margin = _scale_int(min_margin, scale, minimum=2)
    min_radius = _scale_int(4, scale, minimum=2)

    for (x1, y1, x2, y2) in boxes:
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)

        pad_x = max(scaled_min_margin, int(w * margin_ratio))
        pad_y = max(scaled_min_margin, int(h * margin_ratio))

        bx1 = max(0, x1 - pad_x)
        by1 = max(0, y1 - pad_y)
        bx2 = min(img_w, x2 + pad_x)
        by2 = min(img_h, y2 + pad_y)

        box_w = max(1, bx2 - bx1)
        box_h = max(1, by2 - by1)

        radius = max(min_radius, int(min(box_w, box_h) * corner_radius_ratio))
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=radius, fill=(0, 0, 0))

    return _restore_alpha(canvas, alpha_ch)


def apply_slim_black_bar(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    angle_deg: float = 25.0,
    thickness_ratio: float = 0.28,
) -> Image.Image:
    """
    2D 서브컬처 미니멀 슬림 블랙 바(Thin Black Bar)를 동적 해상도 스케일로 렌더링한다.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    canvas = rgb_img.copy()
    draw = ImageDraw.Draw(canvas)
    scale = _compute_scale_factor(*canvas.size)
    min_thickness = _scale_int(12, scale, minimum=4)
    max_thickness = _scale_int(32, scale, minimum=12)

    for (x1, y1, x2, y2) in boxes:
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0

        bar_thickness = max(min_thickness, min(max_thickness, int(min(w, h) * thickness_ratio)))
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

    return _restore_alpha(canvas, alpha_ch)


def apply_shadow_silhouette(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    shadow_opacity: float = 0.85,
    blur_radius: int = 14,
) -> Image.Image:
    """
    형태 윤곽선과 굴곡은 은은하게 유지하되 내부 선화 디테일만 짙은 그림자 톤으로 덮는
    소프트 타원 페더링 그림자 음영(Shadow Box) 검열을 적용한다.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    canvas = rgb_img.copy()
    scale = _compute_scale_factor(*canvas.size)
    eff_blur = max(2, int(round(blur_radius * scale)))
    fk = _odd_kernel(13, scale)

    for (x1, y1, x2, y2) in boxes:
        crop_box = (max(0, x1), max(0, y1), min(canvas.width, x2), min(canvas.height, y2))
        cw = crop_box[2] - crop_box[0]
        ch = crop_box[3] - crop_box[1]
        if cw <= 0 or ch <= 0:
            continue
        sub_img = canvas.crop(crop_box)

        blurred = sub_img.filter(ImageFilter.GaussianBlur(radius=eff_blur))
        shadow = Image.new("RGB", blurred.size, color=(18, 18, 18))
        blended = Image.blend(blurred, shadow, alpha=shadow_opacity)

        mask_np = np.zeros((ch, cw), dtype=np.uint8)
        rx = max(2, int(cw * 0.44))
        ry = max(2, int(ch * 0.44))
        cv2.ellipse(mask_np, (cw // 2, ch // 2), (rx, ry), 0, 0, 360, 255, -1)
        feathered_mask = cv2.GaussianBlur(mask_np, (fk, fk), 3.5 * scale)
        canvas.paste(blended, crop_box, Image.fromarray(feathered_mask, mode="L"))

    return _restore_alpha(canvas, alpha_ch)


def apply_mosaic(
    img: Image.Image,
    boxes: Sequence[Tuple[int, int, int, int]],
    block_size: int = 16,
) -> Image.Image:
    """ROI 국소 리사이즈 및 소프트 캡슐 페더링이 적용된 정밀 블러-모자이크를 렌더링한다."""
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    w_img, h_img = rgb_img.size
    scale = _compute_scale_factor(w_img, h_img)
    eff_block = _scale_int(block_size, scale, minimum=4)
    eff_pre_blur = max(1.0, 3.0 * scale)
    fk = _odd_kernel(9, scale)

    mask = np.zeros((h_img, w_img), dtype=np.uint8)
    ux1, uy1, ux2, uy2 = w_img, h_img, 0, 0
    for (x1, y1, x2, y2) in boxes:
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)
        ex1 = max(0, int(x1 - bw * 0.04))
        ey1 = max(0, int(y1 - bh * 0.02))
        ex2 = min(w_img, int(x2 + bw * 0.04))
        ey2 = min(h_img, int(y2 + bh * (0.16 if bh > bw * 0.8 else 0.10)))
        if ex2 <= ex1 or ey2 <= ey1:
            continue
        cx = int((ex1 + ex2) / 2)
        cy = int((ey1 + ey2) / 2)
        rx = max(_scale_int(6, scale, 4), int((ex2 - ex1) * 0.48))
        ry = max(_scale_int(6, scale, 4), int((ey2 - ey1) * 0.48))
        cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 255, -1)
        ux1, uy1 = min(ux1, ex1), min(uy1, ey1)
        ux2, uy2 = max(ux2, ex2), max(uy2, ey2)

    if np.count_nonzero(mask) == 0 or ux2 <= ux1 or uy2 <= uy1:
        return img.copy()

    ux1 = max(0, ux1 - fk)
    uy1 = max(0, uy1 - fk)
    ux2 = min(w_img, ux2 + fk)
    uy2 = min(h_img, uy2 + fk)

    orig_crop = rgb_img.crop((ux1, uy1, ux2, uy2))
    cw, ch = orig_crop.size
    blurred_crop = orig_crop.filter(ImageFilter.GaussianBlur(radius=eff_pre_blur))
    small_w = max(1, cw // eff_block)
    small_h = max(1, ch // eff_block)
    mosaic_crop = blurred_crop.resize((small_w, small_h), Image.Resampling.BILINEAR).resize(
        (cw, ch), Image.Resampling.NEAREST
    )

    feathered = cv2.GaussianBlur(mask[uy1:uy2, ux1:ux2], (fk, fk), 2.5 * scale)
    alpha = (feathered.astype(np.float32) / 255.0)[:, :, np.newaxis]
    blended_np = (1.0 - alpha) * np.array(orig_crop, dtype=np.float32) + alpha * np.array(mosaic_crop, dtype=np.float32)
    blended_crop = Image.fromarray(np.clip(blended_np, 0, 255).astype(np.uint8))

    out = rgb_img.copy()
    out.paste(blended_crop, (ux1, uy1))
    return _restore_alpha(out, alpha_ch)


def apply_manga_bar(
    img: Image.Image,
    boxes: Sequence[Any],
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
) -> Image.Image:
    """
    주축 회전 각도와 기저부 기하 구조를 따라 코어 바와 기저부 크로스 바를 배치하는
    정통 망가식 검은 바(`manga_bar`) 검열을 렌더링한다.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    norm_boxes = _normalize_censor_boxes(boxes)
    if not norm_boxes:
        return _restore_alpha(rgb_img.copy(), alpha_ch)

    img_np = np.array(rgb_img, dtype=np.uint8)
    h_img, w_img = img_np.shape[:2]
    skin_mask = skin_np if skin_np is not None else get_skin_mask(img_np)
    scale = _compute_scale_factor(w_img, h_img)
    min_bar_w = _scale_int(10, scale, minimum=5)

    mask = np.zeros((h_img, w_img), dtype=np.uint8)
    for (x1, y1, x2, y2) in norm_boxes:
        geom = _estimate_penis_axis_and_base(img_np, (x1, y1, x2, y2), skin_np=skin_mask, pose_cat=pose_cat)
        sx0, sy0 = geom["shaft_start"]
        sx1, sy1 = geom["shaft_end"]
        mid_shaft = ((sx0 + sx1) * 0.5, (sy0 + sy1) * 0.5)
        axis = geom["axis"]
        major_len = geom["major_len"]
        minor_len = geom["minor_len"]
        shaft_w = geom["shaft_width"]
        rx, ry = geom["base_radii"]

        box_mask = np.zeros((h_img, w_img), dtype=np.uint8)
        shaft_thick = max(float(min_bar_w), min(minor_len * 0.72, max(shaft_w * 0.85, minor_len * 0.42)))
        shaft_poly = _rotated_rect_points(mid_shaft, axis, major_len * 0.94, shaft_thick)
        cv2.fillConvexPoly(box_mask, shaft_poly, 255)

        base_len = max(float(min_bar_w) * 1.3, rx * 1.55)
        base_thick = max(float(min_bar_w) * 1.3, min(minor_len * 0.95, ry * 1.90))
        base_poly = _rotated_rect_points(geom["base_center"], axis, base_len, base_thick)
        cv2.fillConvexPoly(box_mask, base_poly, 255)

        pad = max(4, int(max(x2 - x1, y2 - y1) * 0.08))
        cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
        cx2, cy2 = min(w_img, x2 + pad), min(h_img, y2 + pad)
        mask[cy1:cy2, cx1:cx2] = np.maximum(mask[cy1:cy2, cx1:cx2], box_mask[cy1:cy2, cx1:cx2])

    out_np = img_np.copy()
    out_np[mask > 0] = (0, 0, 0)
    return _restore_alpha(Image.fromarray(out_np, mode="RGB"), alpha_ch)


def apply_manga_glow(
    img: Image.Image,
    boxes: Sequence[Any],
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
) -> Image.Image:
    """
    회전된 슬림 캡슐과 기저부 타원을 결합한 뒤, 코어는 불투명 순백색(255)으로 유지하고
    외곽만 부드럽게 발광·페더링하는 정통 망가식 화이트 글로우(`manga_glow`) 검열을 적용한다.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    norm_boxes = _normalize_censor_boxes(boxes)
    if not norm_boxes:
        return _restore_alpha(rgb_img.copy(), alpha_ch)

    img_np = np.array(rgb_img, dtype=np.uint8)
    h_img, w_img = img_np.shape[:2]
    skin_mask = skin_np if skin_np is not None else get_skin_mask(img_np)
    scale = _compute_scale_factor(w_img, h_img)
    min_cap_w = _scale_int(10, scale, minimum=5)
    fk = _odd_kernel(13, scale, minimum=5)

    alpha_total = np.zeros((h_img, w_img), dtype=np.float32)
    for (x1, y1, x2, y2) in norm_boxes:
        geom = _estimate_penis_axis_and_base(img_np, (x1, y1, x2, y2), skin_np=skin_mask, pose_cat=pose_cat)
        p0 = (int(round(geom["shaft_start"][0])), int(round(geom["shaft_start"][1])))
        p1 = (int(round(geom["shaft_end"][0])), int(round(geom["shaft_end"][1])))
        bc = (int(round(geom["base_center"][0])), int(round(geom["base_center"][1])))
        rx, ry = geom["base_radii"]
        minor_len = geom["minor_len"]
        shaft_w = geom["shaft_width"]
        angle = float(geom["angle"])

        outer_w = max(min_cap_w, int(round(min(minor_len * 0.72, max(shaft_w * 0.90, minor_len * 0.48)))))
        core_w = max(3, int(round(outer_w * 0.72)))

        outer_mask = np.zeros((h_img, w_img), dtype=np.uint8)
        cv2.line(outer_mask, p0, p1, 255, thickness=outer_w)
        cv2.ellipse(
            outer_mask,
            bc,
            (max(3, int(round(rx * 0.95))), max(3, int(round(ry * 0.95)))),
            angle,
            0,
            360,
            255,
            -1,
        )

        core_mask = np.zeros((h_img, w_img), dtype=np.uint8)
        cv2.line(core_mask, p0, p1, 255, thickness=core_w)
        cv2.ellipse(
            core_mask,
            bc,
            (max(2, int(round(rx * 0.74))), max(2, int(round(ry * 0.74)))),
            angle,
            0,
            360,
            255,
            -1,
        )

        sigma = max(1.8, min(float(x2 - x1), float(y2 - y1)) * 0.055)
        blurred = cv2.GaussianBlur(outer_mask, (fk, fk), sigma)
        box_alpha = np.maximum(blurred.astype(np.float32) / 255.0, (core_mask > 0).astype(np.float32))

        pad = max(6, int(max(x2 - x1, y2 - y1) * 0.14))
        cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
        cx2, cy2 = min(w_img, x2 + pad), min(h_img, y2 + pad)
        alpha_total[cy1:cy2, cx1:cx2] = np.maximum(alpha_total[cy1:cy2, cx1:cx2], box_alpha[cy1:cy2, cx1:cx2])

    alpha_3 = alpha_total[:, :, np.newaxis]
    blended = img_np.astype(np.float32) * (1.0 - alpha_3) + 255.0 * alpha_3
    out_np = np.clip(blended, 0, 255).astype(np.uint8)
    return _restore_alpha(Image.fromarray(out_np, mode="RGB"), alpha_ch)


def apply_debug_bbox(
    img: Image.Image,
    boxes_with_labels: Sequence[Any],
) -> Image.Image:
    """
    마스킹 대신 감지된 바운딩 박스(BBox), 라벨 및 신뢰도 점수를
    고대비 가시화 테두리와 뱃지 텍스트로 그려주는 진단(QA) 시각화 함수.
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)

    palette = {
        "penis": (0, 255, 64),
        "pussy": (0, 229, 255),
        "nipple_f": (255, 128, 192),
        "anchor": (255, 170, 0),
    }
    fallback_color = (255, 235, 59)

    for item in boxes_with_labels:
        if len(item) >= 3:
            box, label, score = item[0], str(item[1]), float(item[2])
            caption = f"{label} {score:.2f}"
        else:
            box, label = item[0], str(item[1])
            caption = label

        x1, y1, x2, y2 = [int(v) for v in box]
        color = palette.get(label.lower(), fallback_color)

        for off in range(3):
            draw.rectangle([x1 - off, y1 - off, x2 + off, y2 + off], outline=color)

        badge_h = 16
        text_w = max(45, len(caption) * 7 + 8)
        bx2 = min(out.width, x1 + text_w)
        by1 = max(0, y1 - badge_h)
        draw.rectangle([x1, by1, bx2, y1], fill=(20, 20, 20))
        draw.text((x1 + 4, by1 + 2), caption, fill=color)

    return out


def _render_censored_style(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    style: str = "white_silhouette",
    *,
    silhouette_fn: Optional[Callable[..., Image.Image]] = None,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    custom_seg_mask: Optional[np.ndarray] = None,
    fallback_boxes: Optional[Sequence[Tuple[Any, ...]]] = None,
    yolo_prior_masks: Optional[Sequence[Optional[np.ndarray]]] = None,
    context_boxes: Optional[Sequence[Optional[Tuple[int, int, int, int]]]] = None,
    extra_protection_mask: Optional[np.ndarray] = None,
) -> Image.Image:
    """검열 스타일명(`style`)에 대응하는 렌더러로 일관되게 라우팅한다."""
    detected_boxes = [item[0] for item in boxes_with_labels]
    style_clean = str(style or "white_silhouette").lower().strip()

    if style_clean in ("debug", "debug_bbox", "bbox", "bbox_debug"):
        return apply_debug_bbox(img, boxes_with_labels)
    if style_clean in ("manga_bar", "manga_black_bar", "haetae"):
        return apply_manga_bar(img, detected_boxes, skin_np=skin_np, pose_cat=pose_cat)
    if style_clean in ("manga_glow", "manga_white", "glow_bar"):
        return apply_manga_glow(img, detected_boxes, skin_np=skin_np, pose_cat=pose_cat)
    if style_clean in ("bar", "wide_bar", "solid_bar", "black_bar", "pill_bar"):
        return apply_wide_solid_bar(img, detected_boxes)
    if style_clean in ("slim_bar", "thin_bar"):
        return apply_slim_black_bar(img, detected_boxes)
    if style_clean in ("shadow", "shadow_box", "shadow_silhouette"):
        return apply_shadow_silhouette(img, detected_boxes)
    if style_clean in ("mosaic", "pixel"):
        return apply_mosaic(img, detected_boxes)

    if silhouette_fn is not None:
        color_arg = "black" if style_clean in ("black_silhouette", "silhouette_black", "black") else "white"
        extra_kwargs: Dict[str, Any] = {}
        if skin_np is not None:
            extra_kwargs["skin_np"] = skin_np
        if pose_cat is not None:
            extra_kwargs["pose_cat"] = pose_cat
        if custom_seg_mask is not None:
            extra_kwargs["custom_seg_mask"] = custom_seg_mask
        if fallback_boxes is not None:
            extra_kwargs["fallback_boxes"] = fallback_boxes
        if yolo_prior_masks is not None:
            extra_kwargs["yolo_prior_masks"] = yolo_prior_masks
        if context_boxes is not None:
            extra_kwargs["context_boxes"] = context_boxes
        if extra_protection_mask is not None:
            extra_kwargs["extra_protection_mask"] = extra_protection_mask
        if extra_kwargs:
            try:
                return silhouette_fn(img, boxes_with_labels, color_arg, **extra_kwargs)
            except TypeError:
                return silhouette_fn(img, boxes_with_labels, color_arg)
        return silhouette_fn(img, boxes_with_labels, color_arg)

    return apply_wide_solid_bar(img, detected_boxes)

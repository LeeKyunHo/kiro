"""
generator.censor_seg
Phase P2.5 하이브리드 세그멘테이션 엔진:
- SAM 2.1(sam2.1_s.pt) 우선 로드 및 MobileSAM(mobile_sam.pt) 자동 폴백 (Thread-Safe)
- 전용 YOLO11s-seg(censor_seg.pt) 싱글톤 로드 및 가중치 미보유 시 안전 폴백(None)
- Semantic-Guided SAM 투영(distanceTransform 코어 + 외곽 피부 Negative + Envelope 가드)
- Residual Span 중심축 회랑(_compute_prior_extension_corridor), 손 가로질림 Sub-BBox 분할 디코딩
- 2D 외곽 선화 스냅(_snap_to_outer_lineart) 및 얼굴/손/자궁문신 protection_mask 침범 0px 보장
"""

from __future__ import annotations

import gc
import logging
import math
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np
from PIL import Image

from generator.censor_styles import (
    _bbox_fallback_geometry,
    _compute_scale_factor,
    _estimate_penis_axis_and_base,
    _odd_kernel,
    _restore_alpha,
    _scale_int,
    _split_rgb_and_alpha,
    get_skin_mask,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Thread-Safe 모델 싱글톤 (Lazy Load)
_SAM_MODEL: Any = None
_SAM_MODEL_LOCK = threading.Lock()
_CENSOR_SEG_MODEL: Any = None
_CENSOR_SEG_LOCK = threading.Lock()


def _box_iou_seg(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(max(1, area_a + area_b - inter))


def _box_iomin_seg(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(min(area_a, area_b))


def _resolve_weight_candidates(
    filename: str,
    *,
    env_vars: Sequence[str] = (),
    base_dir: Optional[Path] = None,
) -> List[Path]:
    """
    가중치 탐색 순서를 반환한다:
    1) 환경변수(KIRO_* / PLEPA_*)
    2) kiro/models/{filename} 및 kiro/{filename}
    3) 형제 경로 ../plepa/models/{filename} 및 ../plepa/{filename} (존재 시 공유)
    """
    root = base_dir if base_dir is not None else _PROJECT_ROOT
    candidates: List[Path] = []
    for env_key in env_vars:
        val = os.environ.get(env_key, "").strip()
        if val:
            candidates.append(Path(val))

    candidates.extend([
        root / "models" / filename,
        root / filename,
        root.parent / "plepa" / "models" / filename,
        root.parent / "plepa" / filename,
    ])
    return candidates


def _find_existing_weight(
    filename: str,
    *,
    env_vars: Sequence[str] = (),
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """탐색 순서에 따라 실제 존재하는 첫 번째 가중치 파일 경로를 반환한다."""
    for cand in _resolve_weight_candidates(filename, env_vars=env_vars, base_dir=base_dir):
        if cand.is_file():
            return cand
    return None


def _extract_sam_mask_data(results: Any) -> Optional[np.ndarray]:
    """SAM/SAM2 추론 결과에서 마스크 numpy 배열을 안전하게 추출한다 (빈 텐서·테스트 모의 객체 겸용)."""
    if not results or getattr(results[0], "masks", None) is None:
        return None
    raw_data = getattr(results[0].masks, "data", None)
    if raw_data is None:
        return None
    arr = raw_data.cpu().numpy() if hasattr(raw_data, "cpu") else np.asarray(raw_data)
    if arr is None or len(arr) == 0:
        return None
    return arr


def get_sam_model(base_dir: Optional[Path] = None) -> Any:
    """SAM 2.1(sam2.1_s.pt) 우선 로드 및 MobileSAM(mobile_sam.pt) 자동 폴백 (Thread-Safe)."""
    global _SAM_MODEL
    if _SAM_MODEL is not None:
        return _SAM_MODEL if _SAM_MODEL is not False else None

    with _SAM_MODEL_LOCK:
        if _SAM_MODEL is None:
            env_sam = (
                os.environ.get("KIRO_SAM_MODEL", "").strip()
                or os.environ.get("PLEPA_SAM_MODEL", "").strip()
            )
            requested_names: List[str] = [env_sam] if env_sam else ["sam2.1_s.pt"]
            if "mobile_sam.pt" not in requested_names:
                requested_names.append("mobile_sam.pt")

            loaded = None
            for idx, req_name in enumerate(requested_names):
                try:
                    from ultralytics import SAM

                    existing = _find_existing_weight(req_name, base_dir=base_dir)
                    if existing is None and Path(req_name).is_file():
                        existing = Path(req_name)
                    sam_target = str(existing) if existing is not None else req_name
                    loaded = SAM(sam_target)
                    logger.info(f"[Censor] SAM 세그멘테이션 모델 로드 완료 ({sam_target})")
                    break
                except Exception as e:
                    next_hint = (
                        f" -> 폴백({requested_names[idx + 1]}) 시도"
                        if idx + 1 < len(requested_names)
                        else ""
                    )
                    logger.warning(f"[Censor] SAM({req_name}) 로드 실패: {e}{next_hint}")

            if loaded is not None:
                _SAM_MODEL = loaded
            else:
                logger.warning("[Censor] 사용 가능한 SAM 모델이 없어 OpenCV 폴백 모드로 동작합니다.")
                _SAM_MODEL = False
    return _SAM_MODEL if _SAM_MODEL is not False else None


def get_custom_censor_seg_model(base_dir: Optional[Path] = None) -> Any:
    """
    전용 YOLO11s-seg 가중치(censor_seg.pt)를 스레드 안전하게 싱글톤 로드한다.
    탐색 순서: KIRO_CENSOR_SEG_MODEL -> PLEPA_CENSOR_SEG_MODEL -> kiro/models/ -> ../plepa/models/
    가중치가 없거나 로드에 실패하면 크래시하지 않고 None 을 반환한다.
    """
    global _CENSOR_SEG_MODEL
    env_override = (
        os.environ.get("KIRO_CENSOR_SEG_MODEL", "").strip()
        or os.environ.get("PLEPA_CENSOR_SEG_MODEL", "").strip()
    )
    if _CENSOR_SEG_MODEL is not None and not env_override:
        return _CENSOR_SEG_MODEL if _CENSOR_SEG_MODEL is not False else None

    with _CENSOR_SEG_LOCK:
        model_path = _find_existing_weight(
            "censor_seg.pt",
            env_vars=("KIRO_CENSOR_SEG_MODEL", "PLEPA_CENSOR_SEG_MODEL"),
            base_dir=base_dir,
        )
        if model_path is None:
            return None

        if _CENSOR_SEG_MODEL is None or _CENSOR_SEG_MODEL is False or env_override:
            try:
                from ultralytics import YOLO

                _CENSOR_SEG_MODEL = YOLO(str(model_path))
                logger.info(f"[Censor] 전용 YOLO-seg 모델 로드 완료 ({model_path})")
            except Exception as e:
                logger.warning(f"[Censor] 전용 YOLO-seg 로드 실패, 기본 파이프라인으로 폴백합니다: {e}")
                _CENSOR_SEG_MODEL = False
    return _CENSOR_SEG_MODEL if _CENSOR_SEG_MODEL is not False else None


def unload_censor_models() -> None:
    """로드된 SAM 및 전용 YOLO-seg 싱글톤과 GPU VRAM 캐시를 안전하게 해제한다."""
    global _SAM_MODEL, _CENSOR_SEG_MODEL
    with _SAM_MODEL_LOCK:
        _SAM_MODEL = None
    with _CENSOR_SEG_LOCK:
        _CENSOR_SEG_MODEL = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _infer_custom_censor_seg_items(
    img_rgb: Image.Image,
    seg_model: Any,
    min_confidence: float = 0.16,
    skin_np: Optional[np.ndarray] = None,
) -> List[Tuple[Tuple[int, int, int, int], str, float, np.ndarray]]:
    """전용 YOLO11s-seg 모델로 (bbox, label, score, poly_pts) 항목 리스트를 추출한다 (ADR-99 병합 규칙)."""
    if seg_model is None:
        return []

    w, h = img_rgb.size
    items_out: List[Tuple[Tuple[int, int, int, int], str, float, np.ndarray]] = []

    try:
        infer_kwargs: Dict[str, Any] = {
            "conf": float(min_confidence),
            "verbose": False,
        }
        if hasattr(seg_model, "overrides"):
            infer_kwargs["imgsz"] = 1024
            infer_kwargs["retina_masks"] = True
        with _CENSOR_SEG_LOCK:
            results = seg_model(img_rgb, **infer_kwargs)
        if not results:
            return []

        res0 = results[0]
        res_masks = getattr(res0, "masks", None)
        res_boxes = getattr(res0, "boxes", None)
        names = getattr(res0, "names", None) or {}
        polys = getattr(res_masks, "xy", None) if res_masks is not None else None
        if polys is None or len(polys) == 0:
            return []

        xyxy_arr = None
        conf_arr = None
        cls_arr = None
        if res_boxes is not None:
            if getattr(res_boxes, "xyxy", None) is not None:
                xyxy_arr = (
                    res_boxes.xyxy.cpu().numpy()
                    if hasattr(res_boxes.xyxy, "cpu")
                    else np.asarray(res_boxes.xyxy)
                )
            if getattr(res_boxes, "conf", None) is not None:
                conf_arr = (
                    res_boxes.conf.cpu().numpy()
                    if hasattr(res_boxes.conf, "cpu")
                    else np.asarray(res_boxes.conf)
                )
            if getattr(res_boxes, "cls", None) is not None:
                cls_arr = (
                    res_boxes.cls.cpu().numpy()
                    if hasattr(res_boxes.cls, "cpu")
                    else np.asarray(res_boxes.cls)
                )

        skin_np_seg: Optional[np.ndarray] = skin_np
        min_box_px = max(8, int(round(min(w, h) * 0.01)))
        max_poly_area = float(w * h) * 0.25

        for idx, poly in enumerate(polys):
            poly_pts = np.round(np.asarray(poly, dtype=np.float32)).astype(np.int32)
            if poly_pts.ndim != 2 or len(poly_pts) < 3:
                continue

            if xyxy_arr is not None and idx < len(xyxy_arr):
                b = xyxy_arr[idx]
                box = (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
            else:
                xs, ys = poly_pts[:, 0], poly_pts[:, 1]
                box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

            bx1 = max(0, min(w - 1, box[0]))
            by1 = max(0, min(h - 1, box[1]))
            bx2 = max(bx1 + 1, min(w, box[2]))
            by2 = max(by1 + 1, min(h, box[3]))
            if (bx2 - bx1) < min_box_px or (by2 - by1) < min_box_px:
                continue
            if by2 < h * 0.10:
                continue

            score = float(conf_arr[idx]) if (conf_arr is not None and idx < len(conf_arr)) else 0.90
            yc = (by1 + by2) * 0.5
            if yc < h * 0.14 and score < 0.35:
                continue

            poly_area = abs(float(cv2.contourArea(poly_pts)))
            if poly_area < float(min_box_px * min_box_px) or poly_area > max_poly_area:
                continue

            if score < 0.40:
                if skin_np_seg is None:
                    skin_np_seg = get_skin_mask(np.array(img_rgb, dtype=np.uint8))
                roi_sk = skin_np_seg[by1:by2, bx1:bx2]
                if roi_sk.size == 0 or (np.count_nonzero(roi_sk) / float(roi_sk.size)) < 0.12:
                    continue

            cls_id = int(cls_arr[idx]) if (cls_arr is not None and idx < len(cls_arr)) else 0
            raw_name = names.get(cls_id, "penis") if isinstance(names, dict) else "penis"
            label = "penis" if str(raw_name).lower() in ("genital", "penis", "0", "") else str(raw_name).lower()
            items_out.append(((bx1, by1, bx2, by2), label, score, poly_pts))

        items_out.sort(key=lambda it: float(it[2]), reverse=True)
        merged_items: List[Tuple[Tuple[int, int, int, int], str, float, np.ndarray]] = []
        for cand in items_out:
            c_box, c_lbl, _, _ = cand
            if any(
                m_lbl == c_lbl and (_box_iou_seg(c_box, m_box) >= 0.35 or _box_iomin_seg(c_box, m_box) >= 0.55)
                for m_box, m_lbl, _, _ in merged_items
            ):
                continue
            merged_items.append(cand)

        return merged_items
    except Exception as e:
        logger.warning(f"[Censor] 전용 YOLO-seg 추론 예외, 기본 파이프라인으로 폴백합니다: {e}")
        return []


def _render_seg_items_mask(
    width: int,
    height: int,
    seg_items: Sequence[Tuple[Tuple[int, int, int, int], str, float, np.ndarray]],
) -> Optional[np.ndarray]:
    """선택된 YOLO-seg 항목들의 폴리곤을 단일 이진 마스크(`0/255`)로 래스터화한다."""
    if not seg_items:
        return None
    mask = np.zeros((height, width), dtype=np.uint8)
    for _, _, _, poly_pts in seg_items:
        cv2.fillPoly(mask, [poly_pts], 255)
    return mask if np.count_nonzero(mask) > 0 else None


def _infer_custom_censor_seg(
    img_rgb: Image.Image,
    seg_model: Any,
    min_confidence: float = 0.16,
) -> Tuple[List[Tuple[Tuple[int, int, int, int], str, float]], Optional[np.ndarray]]:
    """전용 YOLO-seg 모델로 BBox 목록과 이진 세그멘테이션 마스크를 추출한다."""
    items = _infer_custom_censor_seg_items(img_rgb, seg_model, min_confidence=min_confidence)
    if not items:
        return [], None
    w, h = img_rgb.size
    mask = _render_seg_items_mask(w, h, items)
    if mask is None:
        return [], None
    return [(box, label, score) for box, label, score, _ in items], mask


def _extract_hand_outer_skin_point(
    skin_mask_bool: np.ndarray,
    hbox: Tuple[int, int, int, int],
    genital_center_x: float,
) -> Optional[List[int]]:
    """손 BBox 내 중심축에서 먼 손등/손목 외측 절반 구간의 대표 피부 좌표를 추출한다."""
    img_h, img_w = skin_mask_bool.shape[:2]
    hx1, hy1 = max(0, int(hbox[0])), max(0, int(hbox[1]))
    hx2, hy2 = min(img_w, int(hbox[2])), min(img_h, int(hbox[3]))
    if hx2 <= hx1 or hy2 <= hy1:
        return None
    hx_mid, hy_mid = int((hx1 + hx2) / 2), int((hy1 + hy2) / 2)
    if hx_mid >= genital_center_x:
        ox1, ox2 = hx_mid, hx2
        fallback_x = int(hx1 + (hx2 - hx1) * 0.75)
    else:
        ox1, ox2 = hx1, hx_mid
        fallback_x = int(hx1 + (hx2 - hx1) * 0.25)

    if ox2 > ox1:
        ys, xs = np.where(skin_mask_bool[hy1:hy2, ox1:ox2])
        if len(xs) >= 12:
            return [ox1 + int(np.median(xs)), hy1 + int(np.median(ys))]
    return [min(img_w - 1, max(0, fallback_x)), min(img_h - 1, max(0, hy_mid))]


def _compute_mutually_exclusive_prompts(
    gbox: Tuple[int, int, int, int],
    hand_boxes: Sequence[Tuple[int, int, int, int]],
    skin_mask_bool: np.ndarray,
) -> Tuple[List[List[int]], List[List[int]]]:
    """타겟 BBox와 교차하는 손 BBox 목록 간의 상호 배타적 앵커 포인트를 계산한다."""
    img_h, img_w = skin_mask_bool.shape[:2]
    ex1, ey1 = max(0, int(gbox[0])), max(0, int(gbox[1]))
    ex2, ey2 = min(img_w, int(gbox[2])), min(img_h, int(gbox[3]))
    if ex2 <= ex1 or ey2 <= ey1:
        return [], []

    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
    gx_mid = (ex1 + ex2) / 2.0

    intersecting = [
        (max(0, int(hx1)), max(0, int(hy1)), min(img_w, int(hx2)), min(img_h, int(hy2)))
        for hx1, hy1, hx2, hy2 in hand_boxes
        if max(ex1, hx1) < min(ex2, hx2) and max(ey1, hy1) < min(ey2, hy2)
    ]
    if not intersecting:
        return [], []

    def _in_hands(px: int, py: int) -> bool:
        return any(hx1 <= px <= hx2 and hy1 <= py <= hy2 for hx1, hy1, hx2, hy2 in intersecting)

    safe_roi_skin = skin_mask_bool[ey1:ey2, ex1:ex2].copy()
    hand_anchor_pts: List[List[int]] = []
    for hx1, hy1, hx2, hy2 in intersecting:
        center_pt = [int((hx1 + hx2) / 2), int((hy1 + hy2) / 2)]
        hand_anchor_pts.append(center_pt)
        outer_pt = _extract_hand_outer_skin_point(skin_mask_bool, (hx1, hy1, hx2, hy2), gx_mid)
        if outer_pt is not None and outer_pt != center_pt:
            hand_anchor_pts.append(outer_pt)

        shx1, shy1 = max(0, hx1 - ex1), max(0, hy1 - ey1)
        shx2, shy2 = min(ebw, hx2 - ex1 + 1), min(ebh, hy2 - ey1 + 1)
        if shx2 > shx1 and shy2 > shy1:
            safe_roi_skin[shy1:shy2, shx1:shx2] = False

    min_hy1 = min(hb[1] for hb in intersecting)
    max_hy2 = max(hb[3] for hb in intersecting)
    unoccluded_pts: List[List[int]] = []

    top_cut = min(ebh, max(1, min_hy1 - ey1)) if min_hy1 > ey1 + 6 else max(1, int(ebh * 0.48))
    tys, txs = np.where(safe_roi_skin[:top_cut, :])
    if len(txs) >= 8:
        pt_top = [ex1 + int(np.median(txs)), ey1 + int(np.median(tys))]
        if not _in_hands(pt_top[0], pt_top[1]):
            unoccluded_pts.append(pt_top)
    elif min_hy1 > ey1 + 6:
        pt_top = [int(gx_mid), int((ey1 + min(ey2, min_hy1)) / 2)]
        if not _in_hands(pt_top[0], pt_top[1]):
            unoccluded_pts.append(pt_top)

    if ey2 > max_hy2 + 6:
        bot_start = min(ebh - 1, max(0, max_hy2 - ey1 + 1))
        bys, bxs = np.where(safe_roi_skin[bot_start:, :])
        if len(bxs) >= 8:
            pt_bot = [ex1 + int(np.median(bxs)), ey1 + bot_start + int(np.median(bys))]
            if not _in_hands(pt_bot[0], pt_bot[1]):
                unoccluded_pts.append(pt_bot)
        else:
            pt_bot = [int(gx_mid), int((max(ey1, max_hy2) + ey2) / 2)]
            if not _in_hands(pt_bot[0], pt_bot[1]):
                unoccluded_pts.append(pt_bot)

    if not unoccluded_pts:
        sys_all, sxs_all = np.where(safe_roi_skin)
        if len(sxs_all) >= 8:
            pt_side = [ex1 + int(np.median(sxs_all)), ey1 + int(np.median(sys_all))]
            if not _in_hands(pt_side[0], pt_side[1]):
                unoccluded_pts.append(pt_side)

    if not unoccluded_pts:
        cand_above = [int(gx_mid), max(0, min_hy1 - 4)]
        cand_below = [int(gx_mid), min(img_h - 1, max_hy2 + 4)]
        if min_hy1 > ey1 and not _in_hands(cand_above[0], cand_above[1]):
            unoccluded_pts.append(cand_above)
        elif ey2 > max_hy2 and not _in_hands(cand_below[0], cand_below[1]):
            unoccluded_pts.append(cand_below)

    return unoccluded_pts, hand_anchor_pts


def _extract_hand_occlusion_mask(
    rgb_img: Image.Image,
    sam: Any,
    hand_boxes: Sequence[Tuple[int, int, int, int]],
    genital_boxes: Sequence[Tuple[int, int, int, int]],
    scale: float,
    skin_np: Optional[np.ndarray] = None,
) -> np.ndarray:
    """양방향 앵커와 ROI 크롭 기반 SAM 추론으로 전경 손(Hand) 보호 실루엣을 분리한다."""
    w, h = rgb_img.size
    hand_mask = np.zeros((h, w), dtype=np.uint8)
    if not hand_boxes or not genital_boxes:
        return hand_mask

    skin_bool = skin_np > 0 if skin_np is not None else (get_skin_mask(np.array(rgb_img)) > 0)

    for raw_hx1, raw_hy1, raw_hx2, raw_hy2 in hand_boxes:
        hx1, hy1 = max(0, int(raw_hx1)), max(0, int(raw_hy1))
        hx2, hy2 = min(w, int(raw_hx2)), min(h, int(raw_hy2))
        if hx2 <= hx1 or hy2 <= hy1:
            continue

        intersecting = [
            (max(0, int(gx1)), max(0, int(gy1)), min(w, int(gx2)), min(h, int(gy2)))
            for (gx1, gy1, gx2, gy2) in genital_boxes
            if max(gx1, hx1) < min(gx2, hx2) and max(gy1, hy1) < min(gy2, hy2)
        ]
        if not intersecting:
            continue

        hbw, hbh = max(1, hx2 - hx1), max(1, hy2 - hy1)
        single_hand_m = np.zeros((h, w), dtype=np.uint8)
        sam_ok = False

        hand_pos_global: List[List[int]] = [[int((hx1 + hx2) / 2), int((hy1 + hy2) / 2)]]
        mean_gx = sum((gb[0] + gb[2]) / 2.0 for gb in intersecting) / float(len(intersecting))
        outer_pt = _extract_hand_outer_skin_point(skin_bool, (hx1, hy1, hx2, hy2), mean_gx)
        if outer_pt is not None and outer_pt != hand_pos_global[0]:
            hand_pos_global.append(outer_pt)

        genital_neg_global: List[List[int]] = []
        for gbox in intersecting:
            unocc_pts, _ = _compute_mutually_exclusive_prompts(gbox, [(hx1, hy1, hx2, hy2)], skin_bool)
            for pt in unocc_pts:
                if not (hx1 <= pt[0] <= hx2 and hy1 <= pt[1] <= hy2) and pt not in genital_neg_global:
                    genital_neg_global.append(pt)

        if sam is not None:
            try:
                pad = max(_scale_int(32, scale, minimum=16), int(max(hbw, hbh) * 0.25))
                rx1 = max(0, min([hx1] + [gb[0] for gb in intersecting]) - pad)
                ry1 = max(0, min([hy1] + [gb[1] for gb in intersecting]) - pad)
                rx2 = min(w, max([hx2] + [gb[2] for gb in intersecting]) + pad)
                ry2 = min(h, max([hy2] + [gb[3] for gb in intersecting]) + pad)
                cw, ch = max(1, rx2 - rx1), max(1, ry2 - ry1)
                crop_pil = rgb_img.crop((rx1, ry1, rx2, ry2))
                local_hbox = [hx1 - rx1, hy1 - ry1, hx2 - rx1, hy2 - ry1]

                local_pos = [[px - rx1, py - ry1] for px, py in hand_pos_global if rx1 <= px < rx2 and ry1 <= py < ry2]
                if not local_pos:
                    local_pos = [[int((hx1 + hx2) / 2) - rx1, int((hy1 + hy2) / 2) - ry1]]
                local_neg = [[px - rx1, py - ry1] for px, py in genital_neg_global if rx1 <= px < rx2 and ry1 <= py < ry2]

                all_pts = local_pos + local_neg
                all_lbls = [1] * len(local_pos) + [0] * len(local_neg)
                with _SAM_MODEL_LOCK:
                    try:
                        results = sam(crop_pil, bboxes=[local_hbox], points=[all_pts], labels=[all_lbls], verbose=False)
                    except Exception:
                        results = sam(crop_pil, bboxes=[local_hbox], verbose=False)

                m_data = _extract_sam_mask_data(results)
                if m_data is not None:
                    crop_m = (m_data[0].astype(np.float32) * 255.0).clip(0, 255).astype(np.uint8)
                    if crop_m.shape != (ch, cw):
                        crop_m = cv2.resize(crop_m, (cw, ch), interpolation=cv2.INTER_NEAREST)
                    raw_m = np.zeros((h, w), dtype=np.uint8)
                    raw_m[ry1:ry2, rx1:rx2] = crop_m
                    clipped = np.zeros((h, w), dtype=np.uint8)
                    clipped[hy1:hy2, hx1:hx2] = raw_m[hy1:hy2, hx1:hx2]
                    if np.count_nonzero(clipped) > 0:
                        single_hand_m = clipped
                        sam_ok = True
            except Exception as he:
                logger.debug(f"[Censor] 손 전경 마스크 추출 폴백: {he}")

        if not sam_ok:
            hcx, hcy = int((hx1 + hx2) / 2), int((hy1 + hy2) / 2)
            cv2.ellipse(single_hand_m, (hcx, hcy), (max(2, int(hbw * 0.38)), max(2, int(hbh * 0.38))), 0, 0, 360, 255, -1)

        kh = _odd_kernel(3, scale, minimum=3)
        smooth_hand = cv2.GaussianBlur(single_hand_m, (kh, kh), 0.8 * scale)
        _, smooth_hand = cv2.threshold(smooth_hand, 115, 255, cv2.THRESH_BINARY)
        bbox_bounded = np.zeros((h, w), dtype=np.uint8)
        bbox_bounded[hy1:hy2, hx1:hx2] = smooth_hand[hy1:hy2, hx1:hx2]
        hand_mask = np.maximum(hand_mask, bbox_bounded)

    return hand_mask


def _build_rounded_box_guard(
    w: int,
    h: int,
    ex1: int,
    ey1: int,
    ex2: int,
    ey2: int,
    scale: float,
    radius_ratio: float = 0.30,
) -> np.ndarray:
    """SAM 마스크 누수(Leakage) 시 허벅지/배경 코너 번짐을 막는 보수적 라운드 사각 가드를 생성한다."""
    guard = np.zeros((h, w), dtype=np.uint8)
    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
    rad = max(_scale_int(4, scale, 3), int(min(ebw, ebh) * radius_ratio))
    sub_c = np.zeros((ebh, ebw), dtype=np.uint8)
    if ebw > rad * 2 and ebh > rad * 2:
        cv2.rectangle(sub_c, (rad, 0), (ebw - rad, ebh), 255, -1)
        cv2.rectangle(sub_c, (0, rad), (ebw, ebh - rad), 255, -1)
        cv2.circle(sub_c, (rad, rad), rad, 255, -1)
        cv2.circle(sub_c, (ebw - rad, rad), rad, 255, -1)
        cv2.circle(sub_c, (rad, ebh - rad), rad, 255, -1)
        cv2.circle(sub_c, (ebw - rad, ebh - rad), rad, 255, -1)
    else:
        cv2.ellipse(sub_c, (ebw // 2, ebh // 2), (ebw // 2, ebh // 2), 0, 0, 360, 255, -1)
    guard[ey1:ey2, ex1:ex2] = sub_c
    return guard


def _compute_prior_extension_corridor(
    prior_bin: np.ndarray,
    target_box: Tuple[int, int, int, int],
) -> Optional[np.ndarray]:
    """
    YOLO prior 마스크보다 target_box가 끝단으로 크게 확장된 경우(Branch B Union Span),
    prior 주축(PCA 1주성분)을 따라 미커버 잔여 구간으로 이어지는 중심축 회랑(Corridor) 마스크를 생성한다.
    """
    h, w = prior_bin.shape[:2]
    ex1, ey1, ex2, ey2 = target_box
    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
    prior_in_box = prior_bin[ey1:ey2, ex1:ex2]
    pys_b, pxs_b = np.nonzero(prior_in_box)
    if len(pxs_b) < 16:
        return None

    px1, px2 = ex1 + int(pxs_b.min()), ex1 + int(pxs_b.max()) + 1
    py1, py2 = ey1 + int(pys_b.min()), ey1 + int(pys_b.max()) + 1
    gap_thresh = max(24, int(round(min(ebw, ebh) * 0.14)))
    gap_top, gap_bot = py1 - ey1, ey2 - py2
    gap_left, gap_right = px1 - ex1, ex2 - px2
    if max(gap_top, gap_bot, gap_left, gap_right) < gap_thresh:
        return None

    uncovered_rect = np.zeros((h, w), dtype=np.uint8)
    if gap_bot >= gap_thresh:
        uncovered_rect[py2:ey2, ex1:ex2] = 255
    if gap_top >= gap_thresh:
        uncovered_rect[ey1:py1, ex1:ex2] = 255
    if gap_left >= gap_thresh:
        uncovered_rect[ey1:ey2, ex1:px1] = 255
    if gap_right >= gap_thresh:
        uncovered_rect[ey1:ey2, px2:ex2] = 255

    gx = pxs_b.astype(np.float64) + float(ex1)
    gy = pys_b.astype(np.float64) + float(ey1)
    pcx, pcy = float(gx.mean()), float(gy.mean())
    pts_centered = np.column_stack((gx - pcx, gy - pcy))
    cov = np.cov(pts_centered, rowvar=False) if len(pts_centered) >= 4 else np.eye(2)
    eigvals, eigvecs = np.linalg.eigh(cov)
    major_idx = int(np.argmax(eigvals))
    minor_idx = 1 - major_idx
    eig_ratio = float(eigvals[major_idx]) / max(1e-6, float(eigvals[minor_idx]))
    if eig_ratio >= 1.25:
        vx, vy = float(eigvecs[0, major_idx]), float(eigvecs[1, major_idx])
    elif max(gap_top, gap_bot) >= max(gap_left, gap_right):
        vx, vy = 0.0, 1.0
    else:
        vx, vy = 1.0, 0.0
    norm_len = math.hypot(vx, vy) or 1.0
    vx, vy = vx / norm_len, vy / norm_len

    along_proj = pts_centered[:, 0] * vx + pts_centered[:, 1] * vy
    perp_proj = np.abs(pts_centered[:, 0] * (-vy) + pts_centered[:, 1] * vx)
    half_w = max(18, int(round(float(np.percentile(perp_proj, 95)) * 1.25)))

    corners = ((ex1, ey1), (ex2, ey1), (ex1, ey2), (ex2, ey2))
    box_projs = [(cx - pcx) * vx + (cy - pcy) * vy for cx, cy in corners]
    box_lo, box_hi = min(box_projs), max(box_projs)
    p_min, p_max = float(along_proj.min()), float(along_proj.max())
    p_lo = float(np.percentile(along_proj, 8))
    p_hi = float(np.percentile(along_proj, 92))

    corridor = np.zeros((h, w), dtype=np.uint8)
    drew = False
    if (box_hi - p_max) >= gap_thresh * 0.70:
        pt1 = (int(round(pcx + vx * p_hi)), int(round(pcy + vy * p_hi)))
        pt2 = (int(round(pcx + vx * box_hi)), int(round(pcy + vy * box_hi)))
        cv2.line(corridor, pt1, pt2, 255, thickness=max(16, half_w * 2))
        drew = True
    if (p_min - box_lo) >= gap_thresh * 0.70:
        pt1 = (int(round(pcx + vx * p_lo)), int(round(pcy + vy * p_lo)))
        pt2 = (int(round(pcx + vx * box_lo)), int(round(pcy + vy * box_lo)))
        cv2.line(corridor, pt1, pt2, 255, thickness=max(16, half_w * 2))
        drew = True

    if not drew:
        return None

    box_mask = np.zeros((h, w), dtype=np.uint8)
    box_mask[ey1:ey2, ex1:ex2] = 255
    corridor = cv2.bitwise_and(cv2.bitwise_and(corridor, box_mask), uncovered_rect)
    return corridor if np.count_nonzero(corridor) >= 16 else None


def _extract_semantic_sam_prompts(
    yolo_prior_mask: np.ndarray,
    target_box: Tuple[int, int, int, int],
    context_box: Optional[Tuple[int, int, int, int]],
    skin_bool: np.ndarray,
    hand_boxes: Sequence[Tuple[int, int, int, int]],
    scale: float,
) -> Tuple[List[List[int]], List[List[int]], np.ndarray]:
    """YOLO-seg prior 마스크에서 SAM 2.1용 distanceTransform 코어 Positive / 외곽 피부 Negative / Envelope 가드를 산출한다."""
    h, w = yolo_prior_mask.shape[:2]
    prior_bin = (yolo_prior_mask > 0).astype(np.uint8)
    ex1, ey1 = max(0, int(target_box[0])), max(0, int(target_box[1]))
    ex2, ey2 = min(w, int(target_box[2])), min(h, int(target_box[3]))
    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)

    rx = max(_scale_int(8, scale, minimum=6), int(round(ebw * 0.15)))
    ry = max(_scale_int(8, scale, minimum=6), int(round(ebh * 0.15)))
    env_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, rx * 2 + 1), max(3, ry * 2 + 1)))
    envelope_mask = (cv2.dilate(prior_bin * 255, env_kernel, iterations=1) > 0).astype(np.uint8) * 255

    ext_corridor = _compute_prior_extension_corridor(prior_bin, (ex1, ey1, ex2, ey2))
    if ext_corridor is not None:
        ext_pad = cv2.dilate(ext_corridor, env_kernel, iterations=1)
        box_clip = np.zeros((h, w), dtype=np.uint8)
        box_clip[max(0, ey1 - 6):min(h, ey2 + 6), max(0, ex1 - 6):min(w, ex2 + 6)] = 255
        envelope_mask = cv2.bitwise_or(envelope_mask, cv2.bitwise_and(ext_pad, box_clip))

    valid_fg = prior_bin.copy()
    for hx1, hy1, hx2, hy2 in hand_boxes:
        ix1, iy1, ix2, iy2 = max(0, int(hx1)), max(0, int(hy1)), min(w, int(hx2)), min(h, int(hy2))
        if ix2 > ix1 and iy2 > iy1:
            valid_fg[iy1:iy2, ix1:ix2] = 0
    if np.count_nonzero(valid_fg) < 16:
        valid_fg = prior_bin.copy()

    def _in_hands(px: int, py: int) -> bool:
        return any(hx1 <= px <= hx2 and hy1 <= py <= hy2 for hx1, hy1, hx2, hy2 in hand_boxes)

    pos_pts: List[List[int]] = []
    if np.count_nonzero(valid_fg) > 0:
        dist = cv2.distanceTransform(valid_fg, cv2.DIST_L2, 5)
        max_dist = float(dist.max())
        min_sep = max(12.0, max(ebw, ebh) * 0.22)

        def _try_add_ridge_pt(sub_dist: np.ndarray, x_off: int, y_off: int, min_val: float) -> None:
            if sub_dist.size == 0 or float(sub_dist.max()) < min_val:
                return
            ly, lx = np.unravel_index(int(np.argmax(sub_dist)), sub_dist.shape)
            cand = [int(lx + x_off), int(ly + y_off)]
            if _in_hands(cand[0], cand[1]):
                return
            if any(math.hypot(cand[0] - p[0], cand[1] - p[1]) < min_sep for p in pos_pts):
                return
            pos_pts.append(cand)

        _try_add_ridge_pt(dist[ey1:ey2, ex1:ex2], ex1, ey1, 0.0)
        ys_fg, xs_fg = np.nonzero(valid_fg[ey1:ey2, ex1:ex2])
        if len(xs_fg) >= 24 and max_dist > 0.0:
            ridge_thresh = max(1.5, max_dist * 0.38)
            x_span = int(xs_fg.max() - xs_fg.min() + 1)
            y_span = int(ys_fg.max() - ys_fg.min() + 1)
            if y_span >= x_span:
                y_a = ey1 + int(ys_fg.min() + y_span * 0.42)
                y_b = ey1 + int(ys_fg.min() + y_span * 0.58)
                _try_add_ridge_pt(dist[ey1:y_a, ex1:ex2], ex1, ey1, ridge_thresh)
                _try_add_ridge_pt(dist[y_b:ey2, ex1:ex2], ex1, y_b, ridge_thresh)
            else:
                x_a = ex1 + int(xs_fg.min() + x_span * 0.42)
                x_b = ex1 + int(xs_fg.min() + x_span * 0.58)
                _try_add_ridge_pt(dist[ey1:ey2, ex1:x_a], ex1, ey1, ridge_thresh)
                _try_add_ridge_pt(dist[ey1:ey2, x_b:ex2], x_b, ey1, ridge_thresh)

    neg_rx = max(_scale_int(6, scale, minimum=4), int(round(ebw * 0.08)))
    neg_ry = max(_scale_int(6, scale, minimum=4), int(round(ebh * 0.08)))
    neg_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (neg_rx * 2 + 1, neg_ry * 2 + 1))
    dilated_prior = cv2.dilate(prior_bin * 255, neg_kernel, iterations=1) > 0

    aux_pos_pts: List[List[int]] = []
    if ext_corridor is not None:
        ext_mask = (ext_corridor > 0) & (~dilated_prior)
        if np.count_nonzero(ext_mask) == 0:
            ext_mask = ext_corridor > 0
        for hx1, hy1, hx2, hy2 in hand_boxes:
            ext_mask[max(0, int(hy1)):min(h, int(hy2)), max(0, int(hx1)):min(w, int(hx2))] = False
        ext_skin = ext_mask & skin_bool
        eys, exs = np.where(ext_skin if np.count_nonzero(ext_skin) >= 12 else ext_mask)
        if len(exs) > 0:
            med_e = int(len(exs) // 2)
            cand_ext = [int(exs[med_e]), int(eys[med_e])]
            if not _in_hands(cand_ext[0], cand_ext[1]):
                aux_pos_pts.append(cand_ext)

    cx1, cy1, cx2, cy2 = [int(v) for v in (context_box if context_box is not None else target_box)]
    cx1, cy1 = max(0, min(ex1, cx1)), max(0, min(ey1, cy1))
    cx2, cy2 = min(w, max(ex2, cx2)), min(h, max(ey2, cy2))

    neg_pts: List[List[int]] = []
    if cx2 > cx1 + 4 and cy2 > cy1 + 4:
        protected_neg_zone = dilated_prior.copy()
        if ext_corridor is not None:
            protected_neg_zone |= (ext_corridor > 0)
        outer_skin = skin_bool[cy1:cy2, cx1:cx2] & (~protected_neg_zone[cy1:cy2, cx1:cx2])
        cw_ctx, ch_ctx = cx2 - cx1, cy2 - cy1
        mx, my = max(1, cw_ctx // 2), max(1, ch_ctx // 2)
        for qx1, qy1, qx2, qy2 in ((0, 0, mx, my), (mx, 0, cw_ctx, my), (0, my, mx, ch_ctx), (mx, my, cw_ctx, ch_ctx)):
            qys, qxs = np.where(outer_skin[qy1:qy2, qx1:qx2])
            if len(qxs) >= 12:
                med_idx = int(len(qxs) // 2)
                neg_pts.append([int(cx1 + qx1 + qxs[med_idx]), int(cy1 + qy1 + qys[med_idx])])

    return pos_pts[:3] + aux_pos_pts[:1], neg_pts[:4], envelope_mask


def _classify_sam_mask_leak(
    raw_m: np.ndarray,
    ex1: int,
    ey1: int,
    ex2: int,
    ey2: int,
    rx1: int,
    ry1: int,
    rx2: int,
    ry2: int,
    box_fill_thresh: float = 0.52,
) -> Tuple[bool, bool]:
    """SAM 마스크의 외부 경계 누수(global_leak) 및 BBox 내부 네모 과팽창(blocky_leak)을 판정한다."""
    in_box = raw_m[ey1:ey2, ex1:ex2] > 0
    in_count = int(np.count_nonzero(in_box))
    box_area = max(1, (ex2 - ex1) * (ey2 - ey1))
    box_fill = float(in_count) / float(box_area)
    total_fg = int(np.count_nonzero(raw_m))
    outside_ratio = float(max(0, total_fg - in_count)) / float(box_area)

    global_leak = False
    if box_fill >= 0.90 and outside_ratio >= 0.35:
        global_leak = True
    else:
        crop_slice = raw_m[ry1:ry2, rx1:rx2] > 0
        if crop_slice.size > 0 and outside_ratio >= 0.65:
            border_sides = sum(
                [
                    float(crop_slice[0, :].mean()) >= 0.35,
                    float(crop_slice[-1, :].mean()) >= 0.35,
                    float(crop_slice[:, 0].mean()) >= 0.35,
                    float(crop_slice[:, -1].mean()) >= 0.35,
                ]
            )
            if border_sides >= 2:
                global_leak = True

    blocky_leak = False
    if not global_leak and box_fill > float(box_fill_thresh) and in_count > 0:
        bw, bh = max(1, ex2 - ex1), max(1, ey2 - ey1)
        ys_in, xs_in = np.nonzero(in_box)
        mbw = int(xs_in.max() - xs_in.min() + 1)
        mbh = int(ys_in.max() - ys_in.min() + 1)
        rect_extent = float(in_count) / float(max(1, mbw * mbh))

        cw_c, ch_c = max(2, bw // 5), max(2, bh // 5)
        corner_fill = float(
            np.mean(
                [
                    float(in_box[:ch_c, :cw_c].mean()),
                    float(in_box[:ch_c, -cw_c:].mean()),
                    float(in_box[-ch_c:, :cw_c].mean()),
                    float(in_box[-ch_c:, -cw_c:].mean()),
                ]
            )
        )
        border_touches = sum(
            1
            for edge_mean in (
                float(in_box[0, :].mean()),
                float(in_box[-1, :].mean()),
                float(in_box[:, 0].mean()),
                float(in_box[:, -1].mean()),
            )
            if edge_mean >= 0.30
        )
        strict_single = float(box_fill_thresh) <= 0.45
        if (
            (rect_extent >= 0.86 and corner_fill >= 0.28)
            or (box_fill >= 0.62 and corner_fill >= 0.32)
            or (strict_single and border_touches >= 2 and corner_fill >= 0.20 and rect_extent >= 0.72)
        ):
            blocky_leak = True

    return global_leak, blocky_leak


def _is_sam_mask_leaky(
    raw_m: np.ndarray,
    ex1: int,
    ey1: int,
    ex2: int,
    ey2: int,
    rx1: int,
    ry1: int,
    rx2: int,
    ry2: int,
    box_fill_thresh: float = 0.52,
) -> bool:
    g_leak, b_leak = _classify_sam_mask_leak(raw_m, ex1, ey1, ex2, ey2, rx1, ry1, rx2, ry2, box_fill_thresh=box_fill_thresh)
    return g_leak or b_leak


def _build_oriented_core_guard(
    w: int,
    h: int,
    ex1: int,
    ey1: int,
    ex2: int,
    ey2: int,
    scale: float,
    img_np: Optional[np.ndarray] = None,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    minor_ratio_cap: float = 0.36,
) -> np.ndarray:
    """과팽창 감지 시 주축·기저부 추정 기반 타원형 코어 가드를 생성한다."""
    ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
    geom = (
        _estimate_penis_axis_and_base(img_np, (ex1, ey1, ex2, ey2), skin_np=skin_np, pose_cat=pose_cat)
        if img_np is not None
        else _bbox_fallback_geometry(ex1, ey1, ex2, ey2, pose_cat=pose_cat)
    )

    guard = np.zeros((h, w), dtype=np.uint8)
    cx, cy = int(round(geom["center"][0])), int(round(geom["center"][1]))
    angle = float(geom["angle"])
    major_r = max(4, int(round(min(max(ebw, ebh) * 0.52, max(float(geom["major_len"]) * 0.55, max(ebw, ebh) * 0.44)))))
    minor_r = max(
        4,
        int(round(min(min(ebw, ebh) * float(minor_ratio_cap), max(float(geom["minor_len"]) * 0.54, min(ebw, ebh) * min(0.28, float(minor_ratio_cap)))))),
    )
    cv2.ellipse(guard, (cx, cy), (major_r, minor_r), angle, 0, 360, 255, -1)

    bcx, bcy = int(round(geom["base_center"][0])), int(round(geom["base_center"][1]))
    brx = max(3, int(round(min(ebw * 0.38, float(geom["base_radii"][0])))))
    bry = max(3, int(round(min(ebh * 0.38, float(geom["base_radii"][1])))))
    cv2.ellipse(guard, (bcx, bcy), (brx, bry), angle, 0, 360, 255, -1)

    round_guard = _build_rounded_box_guard(w, h, ex1, ey1, ex2, ey2, scale, radius_ratio=0.30)
    clipped = cv2.bitwise_and(guard, round_guard)
    return clipped if np.count_nonzero(clipped) > 0 else round_guard


def _snap_to_outer_lineart(
    mask: np.ndarray,
    img_np: np.ndarray,
    scale: float,
    protection_mask: Optional[np.ndarray] = None,
    max_steps: Optional[int] = None,
) -> np.ndarray:
    """
    전경 마스크 바로 바깥쪽(1~3px) 어두운 외곽 선화 픽셀만 흡수해 밀착 스냅한다.
    protection_mask 내부로는 단 1px도 침범하지 않는다.
    """
    if mask.size == 0 or np.count_nonzero(mask) == 0 or img_np.size == 0:
        return mask

    steps = max(1, int(max_steps)) if max_steps is not None else min(3, max(1, _scale_int(2, scale, minimum=1)))
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    k_ctx = _odd_kernel(7, scale, minimum=5)
    k_bh = _odd_kernel(9, scale, minimum=5)
    local_bright = cv2.dilate(gray, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_ctx, k_ctx)))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_bh, k_bh)))

    contrast = local_bright.astype(np.int16) - gray.astype(np.int16)
    dark_lineart = (blackhat >= 14) | ((gray <= 135) & (contrast >= 20))

    allowed = (
        (protection_mask == 0)
        if (protection_mask is not None and protection_mask.shape == mask.shape)
        else np.ones_like(mask, dtype=bool)
    )
    cur = (mask > 0).astype(np.uint8) * 255
    k_step = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))

    for _ in range(steps):
        ring = (cv2.dilate(cur, k_step, iterations=1) > 0) & (cur == 0)
        absorb = ring & dark_lineart & allowed
        if not np.any(absorb):
            break
        cur[absorb] = 255

    k_smooth_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    cur = cv2.morphologyEx(cur, cv2.MORPH_CLOSE, k_smooth_close)
    if protection_mask is not None and protection_mask.shape == cur.shape:
        cur[protection_mask > 0] = 0
    return cur


def _finalize_and_blend_silhouette(
    img_np: np.ndarray,
    alpha_ch: Optional[Image.Image],
    mask: np.ndarray,
    color: Union[str, Tuple[int, int, int]],
    scale: float,
    protection_mask: Optional[np.ndarray] = None,
    apply_lineart_snap: bool = True,
) -> Optional[Image.Image]:
    """실루엣 마스크 마감(클로징 -> 외곽 선화 스냅 -> 보호 마스크 0px 차감 -> AA 블렌딩)을 수행한다."""
    if mask.size == 0 or np.count_nonzero(mask) == 0:
        return None

    kc = _odd_kernel(5, scale, minimum=3)
    closed = cv2.morphologyEx((mask > 0).astype(np.uint8) * 255, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kc, kc)))

    ks = _odd_kernel(5, scale, minimum=3)
    smooth_blur = cv2.GaussianBlur(closed, (ks, ks), 1.0 * scale)
    _, smoothed = cv2.threshold(smooth_blur, 115, 255, cv2.THRESH_BINARY)

    if protection_mask is not None and np.count_nonzero(protection_mask) > 0:
        smoothed = cv2.bitwise_and(smoothed, cv2.bitwise_not(protection_mask))

    if apply_lineart_snap and np.count_nonzero(smoothed) > 0:
        smoothed = _snap_to_outer_lineart(smoothed, img_np, scale, protection_mask=protection_mask)

    if protection_mask is not None and np.count_nonzero(protection_mask) > 0:
        smoothed = cv2.bitwise_and(smoothed, cv2.bitwise_not(protection_mask))

    if np.count_nonzero(smoothed) == 0:
        return None

    kf = _odd_kernel(3, scale, minimum=3)
    blurred = cv2.GaussianBlur(smoothed, (kf, kf), 0.8 * scale)
    if protection_mask is not None and np.count_nonzero(protection_mask) > 0:
        # AA 블러조차 protection_mask 내부로 단 1px도 번지지 않도록 0px 하드 클리핑
        blurred[protection_mask > 0] = 0

    if isinstance(color, str):
        color_rgb = (255, 255, 255) if color.lower() in ("white", "whiteout", "white_silhouette") else (12, 12, 16)
    else:
        color_rgb = color

    alpha = (blurred.astype(np.float32) / 255.0)[:, :, np.newaxis]
    target_layer = np.full_like(img_np, color_rgb, dtype=np.float32)
    blended = (1.0 - alpha) * img_np.astype(np.float32) + alpha * target_layer
    return _restore_alpha(Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8)), alpha_ch)


def apply_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    color: Union[str, Tuple[int, int, int]] = "black",
    *,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    custom_seg_mask: Optional[np.ndarray] = None,
    fallback_boxes: Optional[Sequence[Tuple[Any, ...]]] = None,
    yolo_prior_masks: Optional[Sequence[Optional[np.ndarray]]] = None,
    context_boxes: Optional[Sequence[Optional[Tuple[int, int, int, int]]]] = None,
    extra_protection_mask: Optional[np.ndarray] = None,
    sam_provider: Optional[Callable[[], Any]] = None,
    seg_model_provider: Optional[Callable[[], Any]] = None,
    has_imgutils: bool = True,
) -> Image.Image:
    """
    Phase P2.5 Semantic-Guided SAM 2.1 + 선화 스냅 + 손 분할 Sub-BBox + 보호 마스크 0px 보장을 적용한 실루엣 검열.
    """
    rgb_img, alpha_ch = _split_rgb_and_alpha(img)
    w, h = rgb_img.size
    scale = _compute_scale_factor(w, h)
    img_np = np.array(rgb_img)

    protection_mask = np.zeros((h, w), dtype=np.uint8)
    if extra_protection_mask is not None and extra_protection_mask.shape == (h, w):
        protection_mask = np.maximum(protection_mask, (extra_protection_mask > 0).astype(np.uint8) * 255)

    face_neg_pts: List[List[int]] = []
    detected_face_boxes: List[Tuple[int, int, int, int]] = []
    detected_hand_boxes: List[Tuple[int, int, int, int]] = []

    if has_imgutils:
        try:
            from imgutils.detect import detect_faces
            for (fx1, fy1, fx2, fy2), _, _ in detect_faces(rgb_img, conf_threshold=0.35):
                fx1_i, fy1_i, fx2_i, fy2_i = int(fx1), int(fy1), int(fx2), int(fy2)
                detected_face_boxes.append((fx1_i, fy1_i, fx2_i, fy2_i))
                fh = max(1, fy2_i - fy1_i)
                safe_fy2 = int(fy1_i + fh * 0.38)
                if safe_fy2 > fy1_i:
                    cv2.rectangle(protection_mask, (fx1_i, fy1_i), (fx2_i, safe_fy2), 255, -1)
                face_neg_pts.append([int((fx1_i + fx2_i) / 2), int(fy1_i + fh * 0.42)])
        except Exception as pe:
            logger.debug(f"[Censor] 얼굴 보호 영역 검출 생략: {pe}")

        try:
            from imgutils.detect import detect_hands
            for h_item in detect_hands(rgb_img, conf_threshold=0.45):
                hbox = h_item[0] if isinstance(h_item, (tuple, list)) and len(h_item) >= 3 else h_item
                if isinstance(hbox, (tuple, list)) and len(hbox) >= 4:
                    detected_hand_boxes.append((int(hbox[0]), int(hbox[1]), int(hbox[2]), int(hbox[3])))
        except Exception as he:
            logger.debug(f"[Censor] 손 영역 검출 생략: {he}")

    genital_boxes_raw: List[Tuple[int, int, int, int]] = [
        (int(it[0][0]), int(it[0][1]), int(it[0][2]), int(it[0][3]))
        for it in boxes_with_labels
        if it and isinstance(it[0], (tuple, list)) and len(it[0]) >= 4
    ]
    eff_skin_np = skin_np if skin_np is not None else get_skin_mask(img_np)

    active_seg_mask = custom_seg_mask
    if active_seg_mask is None and boxes_with_labels and fallback_boxes is None:
        seg_model = seg_model_provider() if seg_model_provider is not None else get_custom_censor_seg_model()
        if seg_model is not None:
            _, active_seg_mask = _infer_custom_censor_seg(rgb_img, seg_model)

    has_active_seg = active_seg_mask is not None and np.count_nonzero(active_seg_mask) > 0
    if fallback_boxes is not None:
        sam_target_boxes = list(fallback_boxes)
    elif has_active_seg:
        sam_target_boxes = []
    else:
        sam_target_boxes = list(boxes_with_labels)

    final_mask = np.zeros((h, w), dtype=np.uint8)
    if has_active_seg:
        seg_bin = (active_seg_mask > 0).astype(np.uint8) * 255
        if seg_bin.shape != (h, w):
            seg_bin = cv2.resize(seg_bin, (w, h), interpolation=cv2.INTER_NEAREST)
        final_mask = np.maximum(final_mask, seg_bin)

    has_intersecting_hand = any(
        max(gx1, hx1) < min(gx2, hx2) and max(gy1, hy1) < min(gy2, hy2)
        for (gx1, gy1, gx2, gy2) in genital_boxes_raw
        for (hx1, hy1, hx2, hy2) in detected_hand_boxes
    )
    sam_getter = sam_provider if sam_provider is not None else get_sam_model
    sam = sam_getter() if (len(sam_target_boxes) > 0 or has_intersecting_hand) else None

    if detected_hand_boxes and genital_boxes_raw:
        hand_occ_mask = _extract_hand_occlusion_mask(rgb_img, sam, detected_hand_boxes, genital_boxes_raw, scale, skin_np=eff_skin_np)
        if np.count_nonzero(hand_occ_mask) > 0:
            protection_mask = np.maximum(protection_mask, hand_occ_mask)
    else:
        hand_occ_mask = np.zeros((h, w), dtype=np.uint8)

    asym_thresh = 1.15 if pose_cat in ("fellatio", "paizuri") else 1.35

    for item_idx, item in enumerate(sam_target_boxes):
        x1, y1, x2, y2 = item[0]
        ex1, ey1 = max(0, int(x1)), max(0, int(y1))
        ex2, ey2 = min(w, int(x2)), min(h, int(y2))
        if ex2 <= ex1 or ey2 <= ey1:
            continue

        item_prior_raw = yolo_prior_masks[item_idx] if (yolo_prior_masks is not None and item_idx < len(yolo_prior_masks)) else None
        item_context_box = context_boxes[item_idx] if (context_boxes is not None and item_idx < len(context_boxes)) else None
        item_prior_mask: Optional[np.ndarray] = None
        if item_prior_raw is not None:
            prior_u8 = (np.asarray(item_prior_raw) > 0).astype(np.uint8) * 255
            if prior_u8.shape != (h, w):
                prior_u8 = cv2.resize(prior_u8, (w, h), interpolation=cv2.INTER_NEAREST)
            if np.count_nonzero(prior_u8) >= 16:
                item_prior_mask = prior_u8
        has_yolo_prior = item_prior_mask is not None
        prior_area = int(np.count_nonzero(item_prior_mask)) if has_yolo_prior else 0

        ebw, ebh = max(1, ex2 - ex1), max(1, ey2 - ey1)
        sub_m = np.zeros((h, w), dtype=np.uint8)
        sam_succeeded = False

        if sam is not None:
            try:
                roi_rgb = img_np[ey1:ey2, ex1:ex2]
                roi_hsv = cv2.cvtColor(roi_rgb, cv2.COLOR_RGB2HSV)
                roi_skin = eff_skin_np[ey1:ey2, ex1:ex2] > 0

                pos_pts: List[List[int]] = []
                local_neg_pts: List[List[int]] = list(face_neg_pts)
                box_hand_boxes = [
                    (hx1, hy1, hx2, hy2)
                    for hx1, hy1, hx2, hy2 in detected_hand_boxes
                    if max(ex1, hx1) < min(ex2, hx2) and max(ey1, hy1) < min(ey2, hy2)
                ]

                def _is_in_hand_boxes(px: int, py: int) -> bool:
                    return any(hx1 <= px <= hx2 and hy1 <= py <= hy2 for hx1, hy1, hx2, hy2 in box_hand_boxes)

                envelope_mask: Optional[np.ndarray] = None
                void_corner_mask = np.zeros((h, w), dtype=np.uint8)

                if has_yolo_prior and item_prior_mask is not None:
                    sem_pos, sem_neg, envelope_mask = _extract_semantic_sam_prompts(
                        item_prior_mask, (ex1, ey1, ex2, ey2), item_context_box, eff_skin_np > 0, box_hand_boxes, scale
                    )
                    pos_pts.extend(sem_pos)
                    for npt in sem_neg:
                        if npt not in local_neg_pts:
                            local_neg_pts.append(npt)
                    if box_hand_boxes:
                        _, hand_neg_pts = _compute_mutually_exclusive_prompts((ex1, ey1, ex2, ey2), box_hand_boxes, eff_skin_np > 0)
                        for hpt in hand_neg_pts:
                            if hpt not in local_neg_pts:
                                local_neg_pts.append(hpt)
                    if not pos_pts:
                        pys, pxs = np.nonzero(item_prior_mask[ey1:ey2, ex1:ex2])
                        if len(pxs) > 0:
                            pos_pts.append([ex1 + int(np.median(pxs)), ey1 + int(np.median(pys))])
                else:
                    mid_y_l, mid_x_l = int(ebh * 0.50), int(ebw * 0.50)
                    top_skin = roi_skin[int(ebh * 0.12):int(ebh * 0.48), :]
                    top_h_med = (
                        float(np.median(roi_hsv[int(ebh * 0.12):int(ebh * 0.48), :, 0][top_skin]))
                        if np.count_nonzero(top_skin) >= 20
                        else 11.0
                    )

                    def _side_target_score(sub_hsv: np.ndarray, sub_sk: np.ndarray) -> float:
                        if sub_hsv.size == 0:
                            return 0.0
                        h_d = np.minimum(
                            np.abs(sub_hsv[:, :, 0].astype(np.float32) - top_h_med),
                            180.0 - np.abs(sub_hsv[:, :, 0].astype(np.float32) - top_h_med),
                        )
                        return float(np.count_nonzero(sub_sk & (h_d <= 9.5))) / float(max(1, sub_sk.size))

                    l_score = _side_target_score(roi_hsv[mid_y_l:, :mid_x_l], roi_skin[mid_y_l:, :mid_x_l])
                    r_score = _side_target_score(roi_hsv[mid_y_l:, mid_x_l:], roi_skin[mid_y_l:, mid_x_l:])
                    if l_score > r_score * asym_thresh:
                        cand_bot = [int(ex1 + ebw * 0.24), int(ey1 + ebh * 0.72)]
                        local_neg_pts.append([int(ex1 + ebw * 0.76), int(ey1 + ebh * 0.78)])
                        if r_score < 0.22:
                            cv2.rectangle(void_corner_mask, (int(ex1 + ebw * 0.48), int(ey1 + ebh * 0.50)), (ex2, ey2), 255, -1)
                    elif r_score > l_score * asym_thresh:
                        cand_bot = [int(ex1 + ebw * 0.76), int(ey1 + ebh * 0.72)]
                        local_neg_pts.append([int(ex1 + ebw * 0.24), int(ey1 + ebh * 0.78)])
                        if l_score < 0.22:
                            cv2.rectangle(void_corner_mask, (ex1, int(ey1 + ebh * 0.50)), (int(ex1 + ebw * 0.52), ey2), 255, -1)
                    else:
                        cand_bot = [int((ex1 + ex2) / 2), int(ey1 + ebh * 0.68)]

                    if box_hand_boxes:
                        unocc_pts, hand_neg_pts = _compute_mutually_exclusive_prompts((ex1, ey1, ex2, ey2), box_hand_boxes, eff_skin_np > 0)
                        for hpt in hand_neg_pts:
                            if hpt not in local_neg_pts:
                                local_neg_pts.append(hpt)
                        for upt in unocc_pts:
                            if not _is_in_hand_boxes(upt[0], upt[1]) and upt not in pos_pts:
                                pos_pts.append(upt)
                        if not _is_in_hand_boxes(cand_bot[0], cand_bot[1]) and cand_bot not in pos_pts:
                            pos_pts.append(cand_bot)
                    else:
                        top_ys, top_xs = np.where(top_skin)
                        if len(top_xs) >= 10:
                            pos_pts.append([ex1 + int(np.median(top_xs)), ey1 + int(ebh * 0.12) + int(np.median(top_ys))])
                        else:
                            pos_pts.append([int((ex1 + ex2) / 2), int(ey1 + ebh * 0.30)])
                        pos_pts.append(cand_bot)

                if not pos_pts:
                    pos_pts.append([int((ex1 + ex2) / 2), int(ey1 + ebh * 0.22)])

                min_hy1 = min(hb[1] for hb in box_hand_boxes) if box_hand_boxes else ey1
                max_hy2 = max(hb[3] for hb in box_hand_boxes) if box_hand_boxes else ey2
                hand_span_x = max(min(ex2, hx2) - max(ex1, hx1) for hx1, _, hx2, _ in box_hand_boxes) if box_hand_boxes else 0
                hand_mid_y = int((min_hy1 + max_hy2) * 0.5)
                upper_pts = [pt for pt in pos_pts if pt[1] < min_hy1] if box_hand_boxes else []
                lower_pts = [pt for pt in pos_pts if pt[1] > max_hy2] if box_hand_boxes else []
                split_active = bool(box_hand_boxes and upper_pts and lower_pts and hand_span_x >= max(12, int(ebw * 0.35)))

                if split_active:
                    hand_band = max(0, max_hy2 - min_hy1)
                    band_pad = min(hand_band // 2, max(6, int(hand_band * 0.15)))
                    upper_sub_bbox = (ex1, ey1, ex2, min(ey2, max(ey1 + 16, min(max_hy2, hand_mid_y + band_pad))))
                    lower_sub_bbox = (ex1, max(ey1, min(ey2 - 16, max(min_hy1, hand_mid_y - band_pad))), ex2, ey2)
                else:
                    upper_sub_bbox = lower_sub_bbox = (ex1, ey1, ex2, ey2)

                sam_union = np.zeros((h, w), dtype=np.uint8)
                margin = _scale_int(4, scale, minimum=2)
                outer_round_guard: Optional[np.ndarray] = None
                zone_core_guards: Dict[Tuple[int, int, int, int], np.ndarray] = {}
                zone_rect_guards: Dict[Tuple[int, int, int, int], np.ndarray] = {}

                ctx_x1, ctx_y1, ctx_x2, ctx_y2 = [int(v) for v in item_context_box] if item_context_box is not None else [ex1, ey1, ex2, ey2]
                pad_x = max(_scale_int(64, scale, 32), int(ebw * 0.45))
                pad_y = max(_scale_int(64, scale, 32), int(ebh * 0.45))
                rx1, ry1 = max(0, min(ex1, ctx_x1) - pad_x), max(0, min(ey1, ctx_y1) - pad_y)
                rx2, ry2 = min(w, max(ex2, ctx_x2) + pad_x), min(h, max(ey2, ctx_y2) + pad_y)
                cw, ch = max(1, rx2 - rx1), max(1, ry2 - ry1)
                crop_pil = rgb_img.crop((rx1, ry1, rx2, ry2))
                neg_in_crop = [[px - rx1, py - ry1] for px, py in local_neg_pts if rx1 <= px < rx2 and ry1 <= py < ry2]
                min_raw_pixels = _scale_int(60, scale * scale, minimum=25)
                box_fill_thresh = 0.52 if has_yolo_prior else 0.42
                core_minor_cap = 0.36 if has_yolo_prior else 0.30

                for pt in pos_pts:
                    if split_active and pt[1] < min_hy1:
                        target_box = upper_sub_bbox
                    elif split_active and pt[1] > max_hy2:
                        target_box = lower_sub_bbox
                    else:
                        target_box = (ex1, ey1, ex2, ey2)

                    sx1, sy1, sx2, sy2 = target_box
                    local_bbox = [sx1 - rx1, sy1 - ry1, sx2 - rx1, sy2 - ry1]
                    local_pt = [pt[0] - rx1, pt[1] - ry1]
                    all_pts = [local_pt] + neg_in_crop
                    all_lbls = [1] + [0] * len(neg_in_crop)
                    with _SAM_MODEL_LOCK:
                        try:
                            results = sam(crop_pil, bboxes=[local_bbox], points=[all_pts], labels=[all_lbls], verbose=False)
                        except Exception:
                            results = sam(crop_pil, bboxes=[local_bbox], verbose=False)

                    m_data = _extract_sam_mask_data(results)
                    if m_data is not None:
                        crop_m = (m_data[0].astype(np.float32) * 255.0).clip(0, 255).astype(np.uint8)
                        if crop_m.shape != (ch, cw):
                            crop_m = cv2.resize(crop_m, (cw, ch), interpolation=cv2.INTER_NEAREST)
                        raw_m = np.zeros((h, w), dtype=np.uint8)
                        raw_m[ry1:ry2, rx1:rx2] = crop_m

                        if target_box not in zone_rect_guards:
                            zg = np.zeros((h, w), dtype=np.uint8)
                            zg[max(0, sy1 - margin):min(h, sy2 + margin), max(0, sx1 - margin):min(w, sx2 + margin)] = 255
                            zone_rect_guards[target_box] = zg
                        sub_rect_guard = zone_rect_guards[target_box]

                        global_leak, blocky_leak = _classify_sam_mask_leak(
                            raw_m, sx1, sy1, sx2, sy2, rx1, ry1, rx2, ry2, box_fill_thresh=box_fill_thresh
                        )
                        if has_yolo_prior and item_prior_mask is not None and envelope_mask is not None:
                            clamped_env = cv2.bitwise_and(raw_m, envelope_mask)
                            if global_leak or blocky_leak or np.count_nonzero(clamped_env) < max(min_raw_pixels, int(prior_area * 0.25)):
                                raw_m = cv2.bitwise_and(item_prior_mask, sub_rect_guard)
                            else:
                                raw_m = clamped_env
                        elif global_leak:
                            if outer_round_guard is None:
                                outer_round_guard = _build_rounded_box_guard(w, h, ex1, ey1, ex2, ey2, scale, radius_ratio=0.30)
                            guard_mask = cv2.bitwise_and(sub_rect_guard, outer_round_guard) if split_active else outer_round_guard
                            raw_m = cv2.bitwise_and(raw_m, guard_mask)
                        elif blocky_leak:
                            if target_box not in zone_core_guards:
                                zone_core_guards[target_box] = _build_oriented_core_guard(
                                    w, h, sx1, sy1, sx2, sy2, scale, img_np=img_np, skin_np=eff_skin_np, pose_cat=pose_cat, minor_ratio_cap=core_minor_cap
                                )
                            raw_m = cv2.bitwise_and(raw_m, zone_core_guards[target_box])
                        else:
                            raw_m = cv2.bitwise_and(raw_m, sub_rect_guard)

                        if np.count_nonzero(raw_m) >= min_raw_pixels:
                            sam_union = np.maximum(sam_union, raw_m)

                if np.count_nonzero(void_corner_mask) > 0:
                    vk = _odd_kernel(21, scale)
                    soft_void = cv2.GaussianBlur(void_corner_mask, (vk, vk), 6.0 * scale)
                    sam_union = np.clip(sam_union.astype(np.float32) * (1.0 - soft_void.astype(np.float32) / 255.0), 0, 255).astype(np.uint8)
                    _, sam_union = cv2.threshold(sam_union, 127, 255, cv2.THRESH_BINARY)

                if np.count_nonzero(hand_occ_mask) > 0:
                    sam_union = cv2.bitwise_and(sam_union, cv2.bitwise_not(hand_occ_mask))

                num_labels, labels_im, stats, centroids = cv2.connectedComponentsWithStats(sam_union, connectivity=8)
                if num_labels > 1:
                    max_area = float(stats[1:, cv2.CC_STAT_AREA].max())
                    min_noise_area = max(64, _scale_int(60, scale * scale, minimum=25)) if split_active else _scale_int(60, scale * scale, minimum=25)
                    min_cnt_area = max(min_noise_area, max_area * 0.15)
                    keep_labels = {i for i in range(1, num_labels) if stats[i, cv2.CC_STAT_AREA] >= min_cnt_area}

                    if split_active:
                        for zone_pts, zone_fn in ((upper_pts, lambda cy_val: cy_val < max_hy2), (lower_pts, lambda cy_val: cy_val >= min_hy1)):
                            best_lbl: Optional[int] = None
                            best_score = -1.0
                            for pt_z in zone_pts:
                                patch = labels_im[max(0, pt_z[1] - 4):min(h, pt_z[1] + 5), max(0, pt_z[0] - 4):min(w, pt_z[0] + 5)]
                                for lbl_hit in np.unique(patch):
                                    if lbl_hit > 0:
                                        area_val = float(stats[int(lbl_hit), cv2.CC_STAT_AREA])
                                        if area_val >= min_noise_area and area_val > best_score:
                                            best_score, best_lbl = area_val, int(lbl_hit)
                            if best_lbl is None:
                                best_dist = float("inf")
                                ref_pt = zone_pts[0]
                                for lbl_idx in range(1, num_labels):
                                    if float(stats[lbl_idx, cv2.CC_STAT_AREA]) >= min_noise_area and zone_fn(float(centroids[lbl_idx][1])):
                                        dist = math.hypot(float(centroids[lbl_idx][0]) - ref_pt[0], float(centroids[lbl_idx][1]) - ref_pt[1])
                                        if dist < best_dist:
                                            best_dist, best_lbl = dist, lbl_idx
                            if best_lbl is not None:
                                keep_labels.add(best_lbl)

                    for lbl_idx in sorted(keep_labels):
                        sub_m[labels_im == lbl_idx] = 255
                    min_accept_px = max(_scale_int(40, scale * scale, 16), int(prior_area * 0.25)) if has_yolo_prior else (ebw * ebh * 0.06)
                    if np.count_nonzero(sub_m) >= min_accept_px:
                        sam_succeeded = True
            except Exception as e:
                logger.warning(f"[Censor] SAM 세그멘테이션 폴백: {e}")

        if not sam_succeeded:
            if has_yolo_prior and item_prior_mask is not None:
                sub_m[ey1:ey2, ex1:ex2] = item_prior_mask[ey1:ey2, ex1:ex2]
                if np.count_nonzero(hand_occ_mask) > 0:
                    sub_m = cv2.bitwise_and(sub_m, cv2.bitwise_not(hand_occ_mask))
            else:
                bcx, bcy = int((ex1 + ex2) / 2), int((ey1 + ey2) / 2)
                erx = min(int(w * 0.12), max(_scale_int(4, scale, 3), int(ebw * 0.42)))
                ery = min(int(h * 0.16), max(_scale_int(4, scale, 3), int(ebh * 0.48)))
                cv2.ellipse(sub_m, (bcx, bcy), (erx, ery), 0, 0, 360, 255, -1)

        final_mask = np.maximum(final_mask, sub_m)

    blended_out = _finalize_and_blend_silhouette(
        img_np, alpha_ch, final_mask, color=color, scale=scale, protection_mask=protection_mask, apply_lineart_snap=True
    )
    return blended_out if blended_out is not None else img


def apply_black_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    **kwargs: Any,
) -> Image.Image:
    """블랙 실루엣(#000000) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="black", **kwargs)


def apply_white_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    **kwargs: Any,
) -> Image.Image:
    """화이트 실루엣(#FFFFFF) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="white", **kwargs)

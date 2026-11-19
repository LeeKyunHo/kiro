"""
generator.censor_detect
Phase P2.5 다중 스케일(1.6x Zoom-In ROI) 탐지, 오탐 필터, Kiro V2 포즈 DB 기반 체위 카테고리 추론,
ADR-103 오토코노코 컨텍스트 판별 및 DeepGHS + YOLO11s-seg Dual Consensus Gating + Residual Span 복원 엔진.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image

from generator.censor_seg import (
    _infer_custom_censor_seg_items,
    _render_seg_items_mask,
    get_custom_censor_seg_model,
)
from generator.censor_styles import (
    _compute_scale_factor,
    _scale_int,
    get_skin_mask,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 체위별 안전 앵커 바운딩 박스 (정규화 좌표: [x0, y0, x1, y1])
POSE_SAFE_ANCHORS: Dict[str, List[float]] = {
    "missionary":  [0.34, 0.58, 0.66, 0.84],
    "doggystyle":  [0.40, 0.60, 0.60, 0.74],
    "cowgirl":     [0.34, 0.60, 0.66, 0.86],
    "fellatio":    [0.36, 0.38, 0.64, 0.60],
    "paizuri":     [0.34, 0.40, 0.66, 0.68],
    "piledriver":  [0.36, 0.08, 0.64, 0.38],
    "side":        [0.35, 0.55, 0.65, 0.82],
    "standing":    [0.34, 0.44, 0.66, 0.72],
    "shower":      [0.34, 0.48, 0.66, 0.76],
    "handjob":     [0.34, 0.48, 0.66, 0.74],
    "default_h":   [0.34, 0.56, 0.66, 0.84],
}

# ADR-103: 여성 비노출 여운/포옹/키스 씬만 제외 (오토코노코 +70 대칭 150~200은 본인 노출 가능성을 위해 전수 스캔)
NON_EXPOSURE_H_POSES: frozenset[str] = frozenset({
    "101", "102", "104", "105",
})

H_SCENE_SECTIONS: frozenset[str] = frozenset({
    "h_scenes",
    "special_h",
    "scenes_otokonoko",
    "special_otokonoko",
})

_POSE_DB_CACHE: Dict[str, Any] = {}
_POSE_DB_CACHE_LOCK = threading.Lock()


def _get_cached_kiro_pose_db(base_dir: Optional[Path] = None) -> Any:
    """Kiro V2 pose_database.json 을 스레드 안전하게 지연 로드해 체위 카테고리 동적 추론에 활용한다."""
    root = (base_dir if base_dir is not None else _PROJECT_ROOT).resolve()
    key = str(root)
    with _POSE_DB_CACHE_LOCK:
        cached = _POSE_DB_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from generator.pose_db import load_pose_db
        loaded = load_pose_db(root)
    except Exception:
        loaded = None
    with _POSE_DB_CACHE_LOCK:
        _POSE_DB_CACHE.setdefault(key, loaded)
        return _POSE_DB_CACHE[key]


def _normalize_censor_targets(targets: Sequence[str]) -> set[str]:
    """검열 타겟 라벨 별칭을 표준 라벨 집합으로 정규화한다."""
    normalized: set[str] = set()
    for t in targets or ():
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
        elif t_clean:
            normalized.add(t_clean)
    return normalized or {"penis", "pussy"}


def _is_otokonoko_context(
    *,
    roster: Optional[str] = None,
    file_path: Union[str, Path, None] = None,
    pose_code: Optional[Union[str, int]] = None,
    section: Optional[str] = None,
    gender: Optional[str] = None,
) -> bool:
    """ADR-103: 로스터/경로/섹션/성별/포즈코드(133~135, 150~209)로부터 오토코노코 컨텍스트를 추론한다."""
    if gender and str(gender).strip().lower() in ("otokonoko", "femboy"):
        return True
    if section and str(section).strip().lower() in ("scenes_otokonoko", "special_otokonoko"):
        return True
    if pose_code is not None:
        try:
            c_int = int(str(pose_code).strip())
            if (133 <= c_int <= 135) or (150 <= c_int <= 209):
                return True
        except (TypeError, ValueError):
            pass
    r_low = (roster or "").strip().lower()
    if r_low == "oto" or "otokonoko" in r_low:
        return True
    if file_path is not None:
        p_norm = str(file_path).replace("\\", "/").lower()
        if "/oto/" in p_norm or "otokonoko" in p_norm:
            return True
    return False


def get_pose_category(
    pose_code: Union[str, int],
    *,
    base_dir: Optional[Path] = None,
    is_otokonoko: Optional[bool] = None,
) -> Optional[str]:
    """
    Kiro V2 pose_database.json 메타데이터(label + prompt)를 기반으로 체위 카테고리를 동적으로 판정한다.
    포즈 DB가 없는 환경에서는 안전 폴백으로 'default_h' 를 반환한다.
    """
    code_str = str(pose_code).strip().zfill(3)
    eff_otk = bool(is_otokonoko) if is_otokonoko is not None else _is_otokonoko_context(pose_code=pose_code)
    if (not eff_otk) and (code_str in NON_EXPOSURE_H_POSES):
        return None

    try:
        code_int = int(code_str)
    except ValueError:
        return None

    if not ((80 <= code_int <= 135) or (150 <= code_int <= 200)):
        return None

    db = _get_cached_kiro_pose_db(base_dir=base_dir)
    entry = db.entries.get(code_int) if (db is not None and hasattr(db, "entries")) else None
    if entry is not None:
        label = (entry.label or "").lower()
        prompt = (entry.prompt or "").lower()
        combined = f"{label} {prompt}"

        if any(kw in combined for kw in ("piledriver", "upside-down", "inverted position", "파일드라이버")):
            return "piledriver"
        if ("paizuri" in combined or "titfuck" in combined or "파이즈리" in label or "가슴사정" in label) and "fellatio" not in combined and "펠라" not in label:
            return "paizuri"
        if any(kw in combined for kw in ("fellatio", "blowjob", "deepthroat", "licking shaft", "licking penis", "펠라", "딥스로트", "남근핥기", "구강")):
            return "fellatio"
        if any(kw in combined for kw in ("doggystyle", "sex from behind", "anal doggystyle", "후배위", "교배프레스", "뒷스마타")):
            return "doggystyle"
        if any(kw in combined for kw in ("cowgirl", "straddling", "riding partner", "기승위")):
            return "cowgirl"
        if any(kw in combined for kw in ("side by side sex", "측위", "측와위")):
            return "side"
        if any(kw in combined for kw in ("standing sex", "입위", "들박")):
            return "standing"
        if any(kw in combined for kw in ("shower sex", "샤워", "욕실")):
            return "shower"
        if any(kw in combined for kw in ("handjob", "대딸", "핸드잡")):
            return "handjob"
        if any(kw in combined for kw in ("missionary", "정상위", "대면좌위", "m자개각")):
            return "missionary"

    return "default_h"


def get_pose_anchor_box(
    pose_code: Union[str, int],
    img_width: int,
    img_height: int,
    *,
    is_otokonoko: Optional[bool] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """포즈 코드에 해당하는 안전 앵커 픽셀 바운딩 박스 (x1, y1, x2, y2)를 반환한다."""
    cat = get_pose_category(pose_code, is_otokonoko=is_otokonoko)
    if not cat or cat not in POSE_SAFE_ANCHORS:
        return None
    x0_pct, y0_pct, x1_pct, y1_pct = POSE_SAFE_ANCHORS[cat]
    return (
        int(round(x0_pct * img_width)),
        int(round(y0_pct * img_height)),
        int(round(x1_pct * img_width)),
        int(round(y1_pct * img_height)),
    )


def _box_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(max(1, area_a + area_b - inter))


def _box_iomin(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """두 BBox 중 작은 박스 면적 대비 교집합 비율(IoMin)을 계산한다."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(min(area_a, area_b))


def _has_hand_between_boxes(
    box_a: Tuple[int, int, int, int],
    box_b: Tuple[int, int, int, int],
    hand_boxes: Sequence[Tuple[int, int, int, int]],
) -> bool:
    """두 타겟 후보 박스 사이의 수직 연결 구간을 손 BBox가 가로질러 양분하는지 판정한다."""
    if not hand_boxes:
        return False
    ya = (box_a[1] + box_a[3]) * 0.5
    yb = (box_b[1] + box_b[3]) * 0.5
    upper, lower = (box_a, box_b) if ya <= yb else (box_b, box_a)
    ux1, uy1, ux2, uy2 = [int(v) for v in upper]
    lx1, ly1, lx2, ly2 = [int(v) for v in lower]

    upper_cy, lower_cy = (uy1 + uy2) * 0.5, (ly1 + ly2) * 0.5
    corr_x1, corr_x2 = min(ux1, lx1) - 12, max(ux2, lx2) + 12
    gap_top, gap_bot = min(uy2, ly1) - 16, max(uy2, ly1) + 16

    for hx1, hy1, hx2, hy2 in hand_boxes:
        if min(corr_x2, hx2) <= max(corr_x1, hx1):
            continue
        hy_mid = (hy1 + hy2) * 0.5
        if upper_cy < hy_mid < lower_cy and min(gap_bot, hy2) >= max(gap_top, hy1):
            return True
    return False


def _get_roi_specs_for_pose(pose_cat: Optional[str]) -> List[Tuple[float, float, float, float]]:
    """체위 카테고리에 맞춰 1.6x Zoom-In ROI 스캔 영역을 최적화한다."""
    if pose_cat == "piledriver":
        return [(0.18, 0.00, 0.82, 0.46), (0.12, 0.12, 0.88, 0.70)]
    if pose_cat in ("fellatio", "paizuri"):
        return [(0.12, 0.12, 0.88, 0.70), (0.14, 0.38, 0.86, 0.98)]
    if pose_cat is not None:
        return [(0.14, 0.38, 0.86, 0.98), (0.20, 0.45, 0.80, 0.95)]
    return [
        (0.14, 0.38, 0.86, 0.98),
        (0.20, 0.45, 0.80, 0.95),
        (0.12, 0.12, 0.88, 0.70),
        (0.18, 0.00, 0.82, 0.46),
    ]


def _filter_bottom_edge_false_positives(
    candidates: Sequence[Tuple[Tuple[int, int, int, int], str, float]],
    img_w: int,
    img_h: int,
    img_rgb_np: Optional[np.ndarray] = None,
    skin_mask_np: Optional[np.ndarray] = None,
) -> List[Tuple[Tuple[int, int, int, int], str, float]]:
    """
    상단에 고신뢰 주 후보가 존재할 때, 최하단 경계 근처(y2 >= 0.88*H) 또는 수직 분리 영역의
    저신뢰(<0.35), 소형 평판 조각(Sliver), 고밝기 체액/저피부 오탐 박스를 제거한다.
    """
    if len(candidates) <= 1 or img_w <= 0 or img_h <= 0:
        return list(candidates)

    penis_indices = [i for i, item in enumerate(candidates) if str(item[1]).lower().strip() == "penis"]
    if len(penis_indices) <= 1:
        return list(candidates)

    filtered: List[Tuple[Tuple[int, int, int, int], str, float]] = []
    for idx, (box, lbl, score) in enumerate(candidates):
        if str(lbl).lower().strip() != "penis":
            filtered.append((box, lbl, score))
            continue

        x1, y1, x2, y2 = box
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        area = bw * bh
        cy = (y1 + y2) * 0.5
        aspect = float(bw) / float(bh)

        upper_anchors = [
            candidates[j]
            for j in penis_indices
            if j != idx and float(candidates[j][2]) >= 0.50 and ((candidates[j][0][1] + candidates[j][0][3]) * 0.5) < cy
        ]
        if not upper_anchors:
            filtered.append((box, lbl, score))
            continue

        top_box, _, _ = max(upper_anchors, key=lambda it: float(it[2]))
        top_h = max(1, top_box[3] - top_box[1])
        top_area = max(1, (top_box[2] - top_box[0]) * top_h)

        near_bottom = y2 >= int(img_h * 0.88)
        vert_isolated = y1 >= (top_box[3] - max(0, int(top_h * 0.08)))
        drop = False

        if near_bottom or vert_isolated:
            if float(score) < 0.35:
                drop = True
            elif y2 >= int(img_h * 0.94) and area < int(top_area * 0.14):
                if aspect >= 2.0 or bh < int(img_h * 0.06) or float(score) < 0.65:
                    drop = True
            elif near_bottom and img_rgb_np is not None and img_rgb_np.size > 0:
                roi_rgb = img_rgb_np[max(0, y1):min(img_h, y2), max(0, x1):min(img_w, x2)]
                if roi_rgb.size > 0:
                    bright_ratio = float((roi_rgb.min(axis=2) >= 222).mean())
                    skin_ratio = 1.0
                    if skin_mask_np is not None and skin_mask_np.size > 0:
                        roi_sk = skin_mask_np[max(0, y1):min(img_h, y2), max(0, x1):min(img_w, x2)]
                        skin_ratio = float(np.count_nonzero(roi_sk)) / float(max(1, roi_sk.size))
                    if bright_ratio >= 0.28 or (y2 >= int(img_h * 0.92) and skin_ratio < 0.25):
                        drop = True

        if not drop:
            filtered.append((box, lbl, score))

    return filtered if filtered else list(candidates)


def _call_detect_censors(detect_fn: Callable[..., Any], img: Image.Image, conf_threshold: float) -> Any:
    """실제 imgutils.detect.detect_censors 및 1인자 테스트 mock 모두 안전하게 호출한다."""
    try:
        return detect_fn(img, conf_threshold=conf_threshold)
    except TypeError:
        return detect_fn(img)


def _detect_target_candidates(
    img_rgb: Image.Image,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.16,
    pose_code: Optional[Union[str, int]] = None,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    *,
    detect_fn: Optional[Callable[..., Any]] = None,
    has_imgutils: bool = True,
    is_otokonoko: Optional[bool] = None,
) -> List[Tuple[Tuple[int, int, int, int], str, float]]:
    """
    전체 스캔 + 체위 맞춤형 1.6x Zoom-In ROI 정밀 재스캔으로 타겟 후보를 검출한다.
    ADR-99 동결 기준선:
    - 기본 min_confidence=0.16
    - 고신뢰(score >= 0.50) 역광/야경 피부율 게이트 면제
    - 중첩 병합: iou >= 0.35 or iomin >= 0.55, 라벨은 'penis' 우선
    """
    if not has_imgutils or detect_fn is None:
        return []

    eff_pose_cat = (
        pose_cat
        if pose_cat is not None
        else (get_pose_category(pose_code, is_otokonoko=is_otokonoko) if pose_code is not None else None)
    )
    normalized_targets = _normalize_censor_targets(targets)
    include_lower_junction = "pussy" in normalized_targets
    eff_conf = max(0.05, float(min_confidence))
    roi_conf_floor = min(eff_conf, 0.17)
    min_candidate_score = min(eff_conf, roi_conf_floor)

    img_w, img_h = img_rgb.size
    scale = _compute_scale_factor(img_w, img_h)
    min_box_px = _scale_int(12, scale, minimum=8)
    img_rgb_np = np.array(img_rgb)
    skin_check_np = skin_np if skin_np is not None else get_skin_mask(img_rgb_np)

    detected: List[Tuple[Tuple[int, int, int, int], str, float]] = []
    is_upper_close = eff_pose_cat in ("fellatio", "paizuri", "piledriver")
    min_y2_ratio = 0.04 if eff_pose_cat == "piledriver" else (0.10 if is_upper_close else 0.24)

    def _add_candidate(box_coords: Tuple[int, int, int, int], lbl: str, score: float = 1.0) -> None:
        lbl_low = str(lbl).lower().strip()
        x1, y1 = max(0, int(box_coords[0])), max(0, int(box_coords[1]))
        x2, y2 = min(img_w, int(box_coords[2])), min(img_h, int(box_coords[3]))
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        if bw < min_box_px or bh < min_box_px:
            return
        if y2 < img_h * min_y2_ratio:
            return
        if eff_pose_cat != "piledriver" and y1 < int(img_h * 0.07):
            y1 = max(y1, int(img_h * 0.07))
            if y2 <= y1:
                return
        # 고신뢰도(score >= 0.50) 박스는 역광·야경·체액 반사로 인한 저피부율 오기각 면제
        if score < 0.50:
            roi_skin = skin_check_np[y1:y2, x1:x2]
            if roi_skin.size == 0 or (np.count_nonzero(roi_skin) / float(roi_skin.size)) < 0.18:
                return
        if lbl_low not in normalized_targets and lbl_low != "anchor":
            return
        if lbl_low == "pussy":
            if y2 < img_h * 0.45:
                return
            if bh > bw * 1.25 and bh > 60:
                y1 = min(y2 - 20, y1 + int(bh * 0.25))
        if score < min_candidate_score:
            return

        cand = (x1, y1, x2, y2)
        for idx, (existing_box, existing_lbl, existing_score) in enumerate(detected):
            if _box_iou(cand, existing_box) >= 0.35 or _box_iomin(cand, existing_box) >= 0.55:
                merged = (
                    min(cand[0], existing_box[0]),
                    min(cand[1], existing_box[1]),
                    max(cand[2], existing_box[2]),
                    max(cand[3], existing_box[3]),
                )
                detected[idx] = (
                    merged,
                    "penis" if "penis" in (lbl_low, existing_lbl) else lbl_low,
                    max(score, float(existing_score)),
                )
                return
        detected.append((cand, lbl_low, score))

    try:
        results = _call_detect_censors(detect_fn, img_rgb, max(0.05, eff_conf * 0.85))
        for box, label, score in results:
            lbl_l = str(label).lower().strip()
            is_target = (lbl_l in normalized_targets and float(score) >= eff_conf) or (
                include_lower_junction and lbl_l == "pussy" and float(score) >= max(0.18, eff_conf)
            )
            if is_target:
                _add_candidate(tuple(int(v) for v in box), lbl_l, float(score))

        # 2단계: 체위 맞춤형 1.6x Zoom-In ROI 크롭 정밀 재스캔
        pussy_roi_thresh = min(max(0.18, eff_conf), 0.20)
        for rx0, ry0, rx1, ry1 in _get_roi_specs_for_pose(eff_pose_cat):
            cx1, cy1 = int(img_w * rx0), int(img_h * ry0)
            cx2, cy2 = int(img_w * rx1), int(img_h * ry1)
            if cx2 <= cx1 or cy2 <= cy1:
                continue
            crop_img = img_rgb.crop((cx1, cy1, cx2, cy2))
            up_w, up_h = max(1, int(crop_img.width * 1.6)), max(1, int(crop_img.height * 1.6))
            up_img = crop_img.resize((up_w, up_h), Image.Resampling.LANCZOS)
            crop_res = _call_detect_censors(detect_fn, up_img, min(0.13, roi_conf_floor))
            for box, label, score in crop_res:
                lbl_l = str(label).lower().strip()
                is_target = (lbl_l in normalized_targets and float(score) >= roi_conf_floor) or (
                    include_lower_junction and lbl_l == "pussy" and float(score) >= pussy_roi_thresh
                )
                if is_target:
                    bx1, by1, bx2, by2 = [int(v / 1.6) for v in box]
                    _add_candidate((bx1 + cx1, by1 + cy1, bx2 + cx1, by2 + cy1), lbl_l, float(score))
    except Exception as e:
        logger.warning(f"[Censor] 감지 모델 추론 예외: {e}")

    # 3단계: 수렴 병합 (ADR-99: iou >= 0.35 or iomin >= 0.55, penis 우선)
    changed = True
    while changed and len(detected) > 1:
        changed = False
        merged_list: List[Tuple[Tuple[int, int, int, int], str, float]] = []
        for cand_box, cand_lbl, cand_score in detected:
            merged_hit = False
            for idx, (ex_box, ex_lbl, ex_score) in enumerate(merged_list):
                if _box_iou(cand_box, ex_box) >= 0.35 or _box_iomin(cand_box, ex_box) >= 0.55:
                    merged_box = (
                        min(cand_box[0], ex_box[0]),
                        min(cand_box[1], ex_box[1]),
                        max(cand_box[2], ex_box[2]),
                        max(cand_box[3], ex_box[3]),
                    )
                    merged_list[idx] = (
                        merged_box,
                        "penis" if "penis" in (cand_lbl, ex_lbl) else cand_lbl,
                        max(float(cand_score), float(ex_score)),
                    )
                    merged_hit = True
                    changed = True
                    break
            if not merged_hit:
                merged_list.append((cand_box, cand_lbl, float(cand_score)))
        detected = merged_list

    return _filter_bottom_edge_false_positives(
        detected, img_w=img_w, img_h=img_h, img_rgb_np=img_rgb_np, skin_mask_np=skin_check_np
    )


def _resolve_hybrid_targets_with_priors(
    img_rgb: Image.Image,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.16,
    pose_code: Optional[Union[str, int]] = None,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    hand_boxes: Optional[Sequence[Tuple[int, int, int, int]]] = None,
    *,
    detect_fn: Optional[Callable[..., Any]] = None,
    has_imgutils: bool = True,
    seg_model_provider: Optional[Callable[[], Any]] = None,
    is_otokonoko: Optional[bool] = None,
) -> Tuple[
    List[Tuple[Tuple[int, int, int, int], str, float]],
    Optional[np.ndarray],
    Optional[List[Tuple[Tuple[int, int, int, int], str, float]]],
    Optional[List[Optional[np.ndarray]]],
    Optional[List[Optional[Tuple[int, int, int, int]]]],
]:
    """
    Phase P2.5 Dual Consensus Gating + Residual Span 2-Way 복원:
    - 커스텀 YOLO11s-seg 미존재 시 DeepGHS 다중 스케일 탐지 결과로 우아하게 폴백.
    - DeepGHS + YOLO-seg 합의 시 YOLO 타이트 prior 마스크와 context_box 를 SAM 2.1 로 투영.
    - YOLO 미커버 잔여 구간 발생 시 손 가로질림(분기 A: Sub-BBox 분할) vs 연속 기저부(분기 B: Union Span 확장)로 복원.
    """
    seg_getter = seg_model_provider if seg_model_provider is not None else get_custom_censor_seg_model
    custom_seg_model = seg_getter()
    if custom_seg_model is None:
        deep_cands = _detect_target_candidates(
            img_rgb,
            targets=targets,
            min_confidence=min_confidence,
            pose_code=pose_code,
            skin_np=skin_np,
            pose_cat=pose_cat,
            detect_fn=detect_fn,
            has_imgutils=has_imgutils,
            is_otokonoko=is_otokonoko,
        )
        return deep_cands, None, None, None, None

    w, h = img_rgb.size
    eff_min_conf = float(min_confidence)
    norm_targets = _normalize_censor_targets(targets)
    eff_skin = skin_np if skin_np is not None else get_skin_mask(np.array(img_rgb, dtype=np.uint8))

    seg_items_all = _infer_custom_censor_seg_items(img_rgb, custom_seg_model, min_confidence=eff_min_conf, skin_np=eff_skin)
    seg_items = [item for item in seg_items_all if item[1] in norm_targets and float(item[2]) >= eff_min_conf]

    fast_path_conf = float(
        os.environ.get("KIRO_YOLO_FASTPATH_CONF")
        or os.environ.get("PLEPA_YOLO_FASTPATH_CONF")
        or "0.92"
    )
    if any(float(item[2]) >= fast_path_conf for item in seg_items):
        selected_seg = [item for item in seg_items if float(item[2]) >= 0.25]
        seg_mask = _render_seg_items_mask(w, h, selected_seg)
        return [(item[0], item[1], float(item[2])) for item in selected_seg], seg_mask, [], None, None

    deep_cands = _detect_target_candidates(
        img_rgb,
        targets=targets,
        min_confidence=min_confidence,
        pose_code=pose_code,
        skin_np=eff_skin,
        pose_cat=pose_cat,
        detect_fn=detect_fn,
        has_imgutils=has_imgutils,
        is_otokonoko=is_otokonoko,
    )

    def _passes_case_a_rescue(item: Tuple[Tuple[int, int, int, int], str, float, np.ndarray], has_existing: bool) -> bool:
        s_box_c, _, s_score_c, _ = item
        min_r = 0.40 if has_existing else 0.19
        if float(s_score_c) < min_r:
            return False
        if not has_existing and 0.19 <= float(s_score_c) < 0.25:
            sx1, sy1, sx2, sy2 = [int(v) for v in s_box_c]
            if max(0, sx2 - sx1) * max(0, sy2 - sy1) < 900:
                return False
            roi_sk = eff_skin[max(0, sy1):min(h, sy2), max(0, sx1):min(w, sx2)]
            if float(np.count_nonzero(roi_sk)) / float(max(1, roi_sk.size)) < 0.12:
                return False
        return True

    if not deep_cands:
        rescue_items = [item for item in seg_items if _passes_case_a_rescue(item, has_existing=False)]
        if rescue_items:
            rescue_boxes = [(it[0], it[1], float(it[2])) for it in rescue_items]
            rescue_priors = [_render_seg_items_mask(w, h, [it]) for it in rescue_items]
            return rescue_boxes, None, list(rescue_boxes), rescue_priors, [None] * len(rescue_items)
        return [], None, None, None, None

    def _primary_rank(cand: Tuple[Tuple[int, int, int, int], str, float]) -> Tuple[float, int]:
        bx = cand[0]
        return (float(cand[2]), max(0, bx[2] - bx[0]) * max(0, bx[3] - bx[1]))

    primary_cand = max(deep_cands, key=_primary_rank)
    primary_box, primary_score = primary_cand[0], float(primary_cand[2])
    primary_yc = (primary_box[1] + primary_box[3]) * 0.5
    primary_h = max(1, primary_box[3] - primary_box[1])
    seg_confirmed_any = any(float(it[2]) >= 0.20 and ((it[0][1] + it[0][3]) * 0.5) >= (h * 0.14) for it in seg_items)

    cached_hands: Optional[List[Tuple[int, int, int, int]]] = list(hand_boxes) if hand_boxes is not None else None

    def _get_hands_lazy() -> List[Tuple[int, int, int, int]]:
        nonlocal cached_hands
        if cached_hands is not None:
            return cached_hands
        cached_hands = []
        if has_imgutils:
            try:
                from imgutils.detect import detect_hands
                for h_it in detect_hands(img_rgb, conf_threshold=0.45):
                    hb = h_it[0] if isinstance(h_it, (tuple, list)) and len(h_it) >= 3 else h_it
                    if isinstance(hb, (tuple, list)) and len(hb) >= 4:
                        cached_hands.append((int(hb[0]), int(hb[1]), int(hb[2]), int(hb[3])))
            except Exception:
                pass
        return cached_hands

    final_boxes: List[Tuple[Tuple[int, int, int, int], str, float]] = []
    final_priors: List[Optional[np.ndarray]] = []
    final_contexts: List[Optional[Tuple[int, int, int, int]]] = []
    consumed_seg_indices: set[int] = set()

    for d_box, d_lbl, d_score in deep_cands:
        matched_indexed = [
            (idx, it)
            for idx, it in enumerate(seg_items)
            if _box_iou(d_box, it[0]) >= 0.15 or _box_iomin(d_box, it[0]) >= 0.25
        ]
        max_matched_yolo = max((float(it[2]) for _, it in matched_indexed), default=0.0)
        dx1, dy1, dx2, dy2 = [int(v) for v in d_box]
        d_yc = (dy1 + dy2) * 0.5
        y2_ratio = float(dy2) / float(max(1, h))
        is_isolated = len(deep_cands) >= 2 and d_box != primary_box and _box_iou(d_box, primary_box) == 0.0

        if is_isolated and d_yc > (h * 0.68):
            roi_sk = eff_skin[max(0, dy1):min(h, dy2), max(0, dx1):min(w, dx2)]
            skin_ratio = float(np.count_nonzero(roi_sk)) / float(max(1, roi_sk.size))
            if (seg_confirmed_any and (skin_ratio >= 0.35 or float(d_score) < 0.28)) or (
                not seg_items and float(d_score) < 0.22 and primary_score >= float(d_score) + 0.06 and skin_ratio >= 0.35
            ):
                continue

        is_adjacent_lower = (
            len(deep_cands) >= 2
            and d_box != primary_box
            and _box_iou(d_box, primary_box) < 0.25
            and d_yc > primary_yc + max(20.0, primary_h * 0.25)
            and (y2_ratio > 0.82 or dy1 >= primary_box[3] - max(24, int(primary_h * 0.25)))
        )
        if is_adjacent_lower and not _has_hand_between_boxes(primary_box, d_box, _get_hands_lazy()) and max_matched_yolo < 0.58:
            continue

        d_area = max(1, (dx2 - dx1) * (dy2 - dy1))

        def _is_valid_prior_match(it: Tuple[Tuple[int, int, int, int], str, float, np.ndarray]) -> bool:
            s_bx, _, s_sc, _ = it
            sc = float(s_sc)
            if not (sc >= 0.32 or (sc >= 0.22 and _box_iomin(d_box, s_bx) >= 0.20)):
                return False
            sx1, sy1, sx2, sy2 = [int(v) for v in s_bx]
            s_area = max(1, (sx2 - sx1) * (sy2 - sy1))
            inter_area = max(0, min(dx2, sx2) - max(dx1, sx1)) * max(0, min(dy2, sy2) - max(dy1, sy1))
            if (float(inter_area) / float(d_area)) < 0.06 and sc < 0.55:
                return False
            s_cx, s_cy = (sx1 + sx2) * 0.5, (sy1 + sy2) * 0.5
            if not (dx1 <= s_cx <= dx2 and dy1 <= s_cy <= dy2) and (float(inter_area) / float(s_area)) < 0.55 and sc < 0.60:
                return False
            return True

        qualified_segs = [(idx, it) for idx, it in matched_indexed if _is_valid_prior_match(it)]
        if qualified_segs:
            cand_segs: List[Tuple[int, Any]] = []
            for idx, it in qualified_segs:
                if idx in consumed_seg_indices:
                    continue
                s_box = it[0]
                if any(_box_iou(s_box, fb[0]) >= 0.45 for fb in final_boxes):
                    continue
                if any(_box_iou(s_box, prev_it[0]) >= 0.45 for _, prev_it in cand_segs):
                    continue
                cand_segs.append((idx, it))

            if cand_segs:
                for idx, _ in cand_segs:
                    consumed_seg_indices.add(idx)

                dghs_w, dghs_h = max(1, dx2 - dx1), max(1, dy2 - dy1)

                def _merged_span(segs_rng: List[Tuple[int, int]]) -> int:
                    valid = sorted((a, b) for a, b in segs_rng if b > a)
                    if not valid:
                        return 0
                    total_len, (cur_a, cur_b) = 0, valid[0]
                    for na, nb in valid[1:]:
                        if na <= cur_b:
                            cur_b = max(cur_b, nb)
                        else:
                            total_len += cur_b - cur_a
                            cur_a, cur_b = na, nb
                    return total_len + (cur_b - cur_a)

                cov_y = _merged_span([(max(dy1, it[0][1]), min(dy2, it[0][3])) for _, it in cand_segs])
                min_sx1 = min(int(it[0][0]) for _, it in cand_segs)
                min_sy1 = min(int(it[0][1]) for _, it in cand_segs)
                max_sx2 = max(int(it[0][2]) for _, it in cand_segs)
                max_sy2 = max(int(it[0][3]) for _, it in cand_segs)
                top_res, bot_res = max(0, min_sy1 - dy1), max(0, dy2 - max_sy2)
                y_res_thresh = max(75, int(round(dghs_h * 0.35)))
                cov_x = _merged_span([(max(dx1, it[0][0]), min(dx2, it[0][2])) for _, it in cand_segs])
                x_res_thresh = max(75, int(round(dghs_w * 0.35)))
                lat_res = max(max(0, min_sx1 - dx1), max(0, dx2 - max_sx2))

                is_under_covered = (
                    (float(cov_y) / float(dghs_h)) < 0.62
                    or top_res >= y_res_thresh
                    or bot_res >= y_res_thresh
                    or (dghs_w >= dghs_h * 0.70 and ((float(cov_x) / float(dghs_w)) < 0.62 or lat_res >= x_res_thresh))
                )

                if not is_under_covered:
                    for _, it in cand_segs:
                        s_box, s_lbl, s_score, _ = it
                        final_boxes.append((s_box, str(s_lbl), max(float(s_score), float(d_score))))
                        final_priors.append(_render_seg_items_mask(w, h, [it]))
                        final_contexts.append(d_box)
                else:
                    hands_list = _get_hands_lazy()
                    yolo_union = (min_sx1, min_sy1, max_sx2, max_sy2)
                    bot_cand = (dx1, max_sy2, dx2, dy2) if bot_res >= max(50, int(round(dghs_h * 0.25))) else None
                    top_cand = (dx1, dy1, dx2, min_sy1) if top_res >= max(50, int(round(dghs_h * 0.25))) else None
                    cross_bot = next((hb for hb in hands_list if bot_cand and _has_hand_between_boxes(yolo_union, bot_cand, [hb])), None)
                    cross_top = next((hb for hb in hands_list if top_cand and _has_hand_between_boxes(yolo_union, top_cand, [hb])), None)

                    if cross_bot is not None or cross_top is not None:
                        left_uncov, right_uncov = max(0, min_sx1 - dx1), max(0, dx2 - max_sx2)
                        res_x1, res_x2 = dx1, dx2
                        if left_uncov >= max(45, right_uncov * 2):
                            res_x2 = min(dx2, min_sx1 + max(38, int(round(left_uncov * 0.33))))
                        elif right_uncov >= max(45, left_uncov * 2):
                            res_x1 = max(dx1, max_sx2 - max(38, int(round(right_uncov * 0.33))))
                        for _, it in cand_segs:
                            s_box, s_lbl, s_score, _ = it
                            final_boxes.append((s_box, str(s_lbl), max(float(s_score), float(d_score))))
                            final_priors.append(_render_seg_items_mask(w, h, [it]))
                            final_contexts.append(d_box)
                        if cross_bot is not None and bot_cand is not None:
                            res_y1 = max(max_sy2, int(cross_bot[1]))
                            if res_y1 >= dy2 - 24:
                                res_y1 = max_sy2
                            if dy2 - res_y1 >= 24 and res_x2 - res_x1 >= 24:
                                final_boxes.append(((res_x1, res_y1, res_x2, dy2), str(d_lbl), float(d_score)))
                                final_priors.append(None)
                                final_contexts.append(None)
                        if cross_top is not None and top_cand is not None:
                            res_y2 = min(min_sy1, int(cross_top[3]))
                            if res_y2 <= dy1 + 24:
                                res_y2 = min_sy1
                            if res_y2 - dy1 >= 24 and res_x2 - res_x1 >= 24:
                                final_boxes.append(((res_x1, dy1, res_x2, res_y2), str(d_lbl), float(d_score)))
                                final_priors.append(None)
                                final_contexts.append(None)
                    else:
                        union_box = (max(0, min(dx1, min_sx1)), max(0, min(dy1, min_sy1)), min(w, max(dx2, max_sx2)), min(h, max(dy2, max_sy2)))
                        merged_prior = _render_seg_items_mask(w, h, [it for _, it in cand_segs])
                        merged_score = max([float(d_score)] + [float(it[2]) for _, it in cand_segs])
                        final_boxes.append((union_box, str(d_lbl), merged_score))
                        final_priors.append(merged_prior)
                        final_contexts.append(d_box)
        else:
            final_boxes.append((d_box, str(d_lbl), float(d_score)))
            final_priors.append(None)
            final_contexts.append(None)

    # Case A: DeepGHS 미탐 독립 타겟을 YOLO-seg 합의로 구제
    for idx, it in enumerate(seg_items):
        if idx in consumed_seg_indices or not _passes_case_a_rescue(it, has_existing=bool(final_boxes)):
            continue
        s_box, s_lbl, s_score, _ = it
        if any(_box_iou(s_box, f_box) >= 0.12 or _box_iomin(s_box, f_box) >= 0.22 for f_box, _, _ in final_boxes):
            continue
        if final_boxes:
            top_primary = max(final_boxes, key=_primary_rank)[0]
            tp_yc = (top_primary[1] + top_primary[3]) * 0.5
            s_yc = (s_box[1] + s_box[3]) * 0.5
            if float(s_yc) < (float(h) * 0.14) and float(s_score) < 0.65:
                continue
            if (
                s_yc > tp_yc + 20.0
                and (float(s_box[3]) / float(max(1, h))) > 0.82
                and float(s_score) < 0.58
                and not _has_hand_between_boxes(top_primary, s_box, _get_hands_lazy())
            ):
                continue
        final_boxes.append((s_box, str(s_lbl), float(s_score)))
        final_priors.append(_render_seg_items_mask(w, h, [it]))
        final_contexts.append(None)

    return final_boxes, None, list(final_boxes), final_priors, final_contexts


def _resolve_hybrid_targets_and_mask(
    img_rgb: Image.Image,
    targets: Sequence[str] = ("penis", "pussy"),
    min_confidence: float = 0.16,
    pose_code: Optional[Union[str, int]] = None,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[
    List[Tuple[Tuple[int, int, int, int], str, float]],
    Optional[np.ndarray],
    Optional[List[Tuple[Tuple[int, int, int, int], str, float]]],
]:
    """기존 3-튜플 시그니처 호환용 래퍼."""
    final_boxes, seg_mask, fallback_boxes, _, _ = _resolve_hybrid_targets_with_priors(
        img_rgb, targets=targets, min_confidence=min_confidence, pose_code=pose_code, skin_np=skin_np, pose_cat=pose_cat, **kwargs
    )
    return final_boxes, seg_mask, fallback_boxes

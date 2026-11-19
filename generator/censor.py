"""
generator.censor
2D 애니메이션 일러스트 전용 자동 검열(Censorship) 단일 코어 오케스트레이터 (SSOT).
- 렌더링 스타일 함수: generator.censor_styles
- Phase P2.5 세그멘테이션(SAM 2.1 / MobileSAM / YOLO11s-seg / 선화 스냅 / 보호 마스크 0px): generator.censor_seg
- 다중 스케일 탐지(1.6x Zoom-In ROI), Dual Consensus + Residual Span, ADR-103 컨텍스트: generator.censor_detect
- Wave 3 / P2-A 공용 API(is_censor_target, extract_pose_code, get_origin_backup_path, run_batch_censor,
  _restore_from_origin_dirs, verify_and_heal_censor, _atomic_replace_with_retry) 100% 보존.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, List, Optional, Sequence, Tuple, Union
import uuid

import numpy as np
from PIL import Image

from generator import censor_seg as _censor_seg
from generator.censor_detect import (
    H_SCENE_SECTIONS,
    NON_EXPOSURE_H_POSES,
    POSE_SAFE_ANCHORS,
    _box_iomin,
    _box_iou,
    _detect_target_candidates,
    _filter_bottom_edge_false_positives,
    _get_roi_specs_for_pose,
    _has_hand_between_boxes,
    _is_otokonoko_context,
    _normalize_censor_targets,
    _resolve_hybrid_targets_and_mask,
    _resolve_hybrid_targets_with_priors,
    get_pose_anchor_box,
    get_pose_category,
)
from generator.censor_seg import (
    _CENSOR_SEG_LOCK,
    _SAM_MODEL_LOCK,
    _build_oriented_core_guard,
    _build_rounded_box_guard,
    _classify_sam_mask_leak,
    _compute_mutually_exclusive_prompts,
    _compute_prior_extension_corridor,
    _extract_hand_occlusion_mask,
    _extract_sam_mask_data,
    _extract_semantic_sam_prompts,
    _finalize_and_blend_silhouette,
    _find_existing_weight,
    _infer_custom_censor_seg,
    _infer_custom_censor_seg_items,
    _is_sam_mask_leaky,
    _render_seg_items_mask,
    _resolve_weight_candidates,
    _snap_to_outer_lineart,
)
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
    detect_censors = None  # type: ignore[assignment]
    HAS_IMGUTILS = False

# 테스트 monkeypatch 호환 미러 싱글톤 (실제 로드는 generator.censor_seg 수행)
_SAM_MODEL: Any = None
_CENSOR_SEG_MODEL: Any = None

_POSE_CODE_PATTERN = re.compile(r"_(\d{2,4})(?:_|\.)")


def get_sam_model() -> Any:
    """SAM 2.1(sam2.1_s.pt) 우선 로드 -> MobileSAM(mobile_sam.pt) 자동 폴백 (Thread-Safe)."""
    global _SAM_MODEL
    if _SAM_MODEL is not None:
        return _SAM_MODEL if _SAM_MODEL is not False else None
    loaded = _censor_seg.get_sam_model()
    _SAM_MODEL = loaded if loaded is not None else False
    return loaded


def get_custom_censor_seg_model() -> Any:
    """전용 YOLO11s-seg(censor_seg.pt) 싱글톤 로드 (미보유 시 None 안전 폴백)."""
    global _CENSOR_SEG_MODEL
    if _CENSOR_SEG_MODEL is not None:
        return _CENSOR_SEG_MODEL if _CENSOR_SEG_MODEL is not False else None
    loaded = _censor_seg.get_custom_censor_seg_model()
    _CENSOR_SEG_MODEL = loaded if loaded is not None else False
    return loaded


def unload_censor_models() -> None:
    """로드된 SAM 2.1 / MobileSAM / YOLO11s-seg 싱글톤과 GPU VRAM 캐시를 안전하게 해제한다."""
    global _SAM_MODEL, _CENSOR_SEG_MODEL
    _censor_seg.unload_censor_models()
    with _SAM_MODEL_LOCK:
        _SAM_MODEL = None
    with _CENSOR_SEG_LOCK:
        _CENSOR_SEG_MODEL = None


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
) -> Image.Image:
    """Phase P2.5 Semantic-Guided SAM 2.1 + 선화 스냅 + 보호 마스크 0px 보장을 적용한 실루엣 검열."""
    return _censor_seg.apply_silhouette(
        img,
        boxes_with_labels,
        color=color,
        skin_np=skin_np,
        pose_cat=pose_cat,
        custom_seg_mask=custom_seg_mask,
        fallback_boxes=fallback_boxes,
        yolo_prior_masks=yolo_prior_masks,
        context_boxes=context_boxes,
        extra_protection_mask=extra_protection_mask,
        sam_provider=get_sam_model,
        seg_model_provider=get_custom_censor_seg_model,
        has_imgutils=HAS_IMGUTILS,
    )


def apply_black_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    **kwargs: Any,
) -> Image.Image:
    """블랙 실루엣(#0C0C10) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="black", **kwargs)


def apply_white_silhouette(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    **kwargs: Any,
) -> Image.Image:
    """화이트 실루엣(#FFFFFF) 검열 래퍼 함수."""
    return apply_silhouette(img, boxes_with_labels, color="white", **kwargs)


def _render_censored_style(
    img: Image.Image,
    boxes_with_labels: Sequence[Tuple[Any, ...]],
    style: str = "white_silhouette",
    *,
    skin_np: Optional[np.ndarray] = None,
    pose_cat: Optional[str] = None,
    custom_seg_mask: Optional[np.ndarray] = None,
    fallback_boxes: Optional[Sequence[Tuple[Any, ...]]] = None,
    yolo_prior_masks: Optional[Sequence[Optional[np.ndarray]]] = None,
    context_boxes: Optional[Sequence[Optional[Tuple[int, int, int, int]]]] = None,
    extra_protection_mask: Optional[np.ndarray] = None,
) -> Image.Image:
    """검열 스타일명(`style`)에 대응하는 렌더러로 라우팅한다."""
    return _dispatch_censor_style(
        img,
        boxes_with_labels,
        style=style,
        silhouette_fn=apply_silhouette,
        skin_np=skin_np,
        pose_cat=pose_cat,
        custom_seg_mask=custom_seg_mask,
        fallback_boxes=fallback_boxes,
        yolo_prior_masks=yolo_prior_masks,
        context_boxes=context_boxes,
        extra_protection_mask=extra_protection_mask,
    )


def extract_pose_code(filename: str) -> Optional[int]:
    """에셋 파일명(code/hybrid 모두 지원)에서 포즈 코드 정수를 추출한다."""
    match = _POSE_CODE_PATTERN.search(filename)
    return int(match.group(1)) if match else None


def is_censor_target(
    pose_code: Optional[Union[str, int]] = None,
    *,
    section: Optional[str] = None,
    is_nude_scene: bool = False,
    is_otokonoko: Optional[bool] = None,
    roster: Optional[str] = None,
    file_path: Optional[Union[Path, str]] = None,
) -> bool:
    """
    성인 씬 검열 대상 여부를 판정하는 단일 진실 공급원(SSOT, ADR-103 반영).
    - 여성 비노출 여운/포옹/키스 씬(NON_EXPOSURE_H_POSES: 101, 102, 104, 105)만 제외.
    - 오토코노코 컨텍스트(is_otokonoko=True, roster='oto', 섹션 scenes_otokonoko/special_otokonoko,
      코드 150..200)는 171/172/174/175 포함 전수 검열 대상.
    - V2 H-씬 대역: 80..135, 150..200.
    - runner.py 등에서는 section in H_SCENE_SECTIONS 또는 is_nude_scene=True 도 포함.
    """
    eff_otk = (
        bool(is_otokonoko)
        if is_otokonoko is not None
        else _is_otokonoko_context(
            roster=roster,
            file_path=file_path,
            pose_code=pose_code,
            section=section,
        )
    )

    if pose_code is not None:
        try:
            code_int = int(str(pose_code).strip())
        except (TypeError, ValueError):
            code_int = None
        if code_int is not None:
            if (not eff_otk) and (str(code_int).zfill(3) in NON_EXPOSURE_H_POSES):
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
    if not HAS_IMGUTILS or detect_censors is None:
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
    min_confidence: float = 0.16,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
    *,
    is_nude: bool = False,
    is_otokonoko: Optional[bool] = None,
    roster: Optional[str] = None,
    verify_heal: bool = False,
    heal_min_confidence: float = 0.32,
) -> Tuple[Image.Image, int]:
    """
    Phase P2.5 하이브리드 검열(DeepGHS 다중 스케일 + YOLO11s-seg Dual Consensus + SAM 2.1/MobileSAM)을 적용한다.
    - ADR-99 동결 기준선: min_confidence=0.16, enable_anchor_fallback=False 기본값.
    - ADR-103: is_nude / is_otokonoko 컨텍스트를 탐지·체위 판별에 전파.
    - 반환값: (검열된 PIL 이미지, 감지된 부위 개수)
    """
    src_path_hint = image if isinstance(image, (str, Path)) else None
    if isinstance(image, (str, Path)):
        with Image.open(str(image)) as opened:
            raw_img = opened.copy()
    else:
        raw_img = image

    rgb_img, alpha_ch = _split_rgb_and_alpha(raw_img)
    eff_otk = (
        bool(is_otokonoko)
        if is_otokonoko is not None
        else _is_otokonoko_context(roster=roster, file_path=src_path_hint, pose_code=pose_code)
    )
    pose_cat = get_pose_category(pose_code, is_otokonoko=eff_otk) if pose_code is not None else None
    if pose_cat is None and is_nude:
        pose_cat = "default_h"

    skin_np = get_skin_mask(np.array(rgb_img, dtype=np.uint8))

    (
        detected_boxes_with_labels,
        custom_seg_mask,
        fallback_boxes,
        yolo_prior_masks,
        context_boxes,
    ) = _resolve_hybrid_targets_with_priors(
        rgb_img,
        targets=targets,
        min_confidence=min_confidence,
        pose_code=pose_code,
        skin_np=skin_np,
        pose_cat=pose_cat,
        detect_fn=detect_censors if HAS_IMGUTILS else None,
        has_imgutils=HAS_IMGUTILS,
        seg_model_provider=get_custom_censor_seg_model,
        is_otokonoko=eff_otk,
    )

    if not detected_boxes_with_labels and pose_code is not None and pose_cat:
        logger.warning(
            f"[WARN] [Censor] 포즈 {pose_code}: 미탐지! 안전 앵커 영역({pose_cat}) 폴백 점검 대상입니다."
        )
        if enable_anchor_fallback:
            w, h = rgb_img.size
            anchor_box = get_pose_anchor_box(pose_code, w, h, is_otokonoko=eff_otk)
            if anchor_box:
                detected_boxes_with_labels.append((anchor_box, "anchor", 1.0))
                fallback_boxes = list(detected_boxes_with_labels)
                yolo_prior_masks = [None]
                context_boxes = [None]
                logger.info(f"[Censor] 포즈 {pose_code}: 안전 앵커 폴백 박스 적용 {anchor_box}")

    if not detected_boxes_with_labels:
        return _restore_alpha(rgb_img, alpha_ch), 0

    censored_rgb = _render_censored_style(
        rgb_img,
        detected_boxes_with_labels,
        style=style,
        skin_np=skin_np,
        pose_cat=pose_cat,
        custom_seg_mask=custom_seg_mask,
        fallback_boxes=fallback_boxes,
        yolo_prior_masks=yolo_prior_masks,
        context_boxes=context_boxes,
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
    min_confidence: float = 0.16,
    quality: int = 95,
    pose_code: Optional[Union[str, int]] = None,
    enable_anchor_fallback: bool = False,
    backup_dir: Optional[Union[Path, str]] = None,
    *,
    is_nude: bool = False,
    is_otokonoko: Optional[bool] = None,
    roster: Optional[str] = None,
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

    base_kwargs: dict[str, Any] = {
        "style": style,
        "targets": targets,
        "min_confidence": min_confidence,
        "pose_code": pose_code,
        "enable_anchor_fallback": enable_anchor_fallback,
    }
    censor_kwargs: dict[str, Any] = dict(base_kwargs)
    if is_nude:
        censor_kwargs["is_nude"] = True
    if is_otokonoko is not None:
        censor_kwargs["is_otokonoko"] = bool(is_otokonoko)
    if roster:
        censor_kwargs["roster"] = roster
    if verify_heal:
        censor_kwargs["verify_heal"] = True
    try:
        censored_img, count = censor_image(img, **censor_kwargs)
    except TypeError:
        censored_img, count = censor_image(img, **base_kwargs)

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
    min_confidence: float = 0.16,
    *,
    pose_code: Optional[Union[str, int]] = None,
    is_otokonoko: Optional[bool] = None,
) -> List[Tuple[Tuple[int, int, int, int], str, float]]:
    """이미지에서 검열 대상 부위를 다중 스케일 탐지해 [(box, label, score), ...] 형태로 반환한다."""
    if not HAS_IMGUTILS or detect_censors is None:
        return []
    try:
        if isinstance(image, (str, Path)):
            with Image.open(str(image)) as opened:
                img = opened.convert("RGB")
        else:
            img = image.convert("RGB")
        return _detect_target_candidates(
            img,
            targets=targets,
            min_confidence=min_confidence,
            pose_code=pose_code,
            detect_fn=detect_censors,
            has_imgutils=HAS_IMGUTILS,
            is_otokonoko=is_otokonoko,
        )
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
    min_confidence: float = 0.16,
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
        is_otk = _is_otokonoko_context(file_path=file_path, pose_code=pose_code)

        if pose_code is not None and not is_censor_target(pose_code, is_otokonoko=is_otk, file_path=file_path):
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
            file_kwargs: dict[str, Any] = {
                "src_path": file_path,
                "dst_path": dst,
                "style": style,
                "targets": targets,
                "min_confidence": min_confidence,
                "pose_code": pose_code,
                "enable_anchor_fallback": enable_anchor_fallback,
                "verify_heal": verify_heal,
            }
            if is_otk:
                file_kwargs["is_otokonoko"] = True
            saved = censor_file(**file_kwargs)
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
    "_CENSOR_SEG_LOCK",
    "_SAM_MODEL_LOCK",
    "_atomic_replace_with_retry",
    "_box_iomin",
    "_box_iou",
    "_build_oriented_core_guard",
    "_build_rounded_box_guard",
    "_classify_sam_mask_leak",
    "_compute_mutually_exclusive_prompts",
    "_compute_prior_extension_corridor",
    "_compute_scale_factor",
    "_detect_target_candidates",
    "_estimate_penis_axis_and_base",
    "_extract_hand_occlusion_mask",
    "_extract_sam_mask_data",
    "_extract_semantic_sam_prompts",
    "_filter_bottom_edge_false_positives",
    "_finalize_and_blend_silhouette",
    "_find_existing_weight",
    "_get_roi_specs_for_pose",
    "_has_hand_between_boxes",
    "_infer_custom_censor_seg",
    "_infer_custom_censor_seg_items",
    "_is_otokonoko_context",
    "_is_sam_mask_leaky",
    "_odd_kernel",
    "_render_censored_style",
    "_render_seg_items_mask",
    "_resolve_hybrid_targets_and_mask",
    "_resolve_hybrid_targets_with_priors",
    "_resolve_weight_candidates",
    "_restore_from_origin_dirs",
    "_scale_int",
    "_snap_to_outer_lineart",
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
    "get_custom_censor_seg_model",
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

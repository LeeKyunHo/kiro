"""
gui/settings.py
Kiro Studio GUI 사용자 설정(gui_settings.json) 로드 및 저장 모듈.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger("generator.gui")

SETTINGS_FILENAME = "gui_settings.json"
DEFAULT_SETTINGS: dict[str, Any] = {
    "steps": 30,
    "sampler": "DPM++ 2M",
    "scheduler": "Karras",
    "cfg_scale": 6.0,
    "use_reference": True,
    "naming": "hybrid",
    "overwrite": True,
    "auto_qc": False,
    "max_retries": 3,
    "censor": False,
    "censor_style": "white_silhouette",
}


def load_gui_settings(base_dir: Path) -> dict[str, Any]:
    """gui_settings.json을 UTF-8(BOM 허용)로 로드하여 기본 설정과 병합해 반환한다."""
    path = base_dir / SETTINGS_FILENAME
    merged = dict(DEFAULT_SETTINGS)
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged.update(data)
        except Exception as e:
            logger.warning("[WARN] 설정 파일 로드 실패: %s", e)
    return merged


def save_gui_settings(base_dir: Path, data: dict[str, Any]) -> None:
    """현재 GUI 설정을 gui_settings.json에 저장한다."""
    path = base_dir / SETTINGS_FILENAME
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[ERROR] 설정 파일 저장 실패: %s", e)

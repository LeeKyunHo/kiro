"""
generator.prompt
프롬프트 조립, 태그 정규화/충돌 검출, 배경 프롬프트 해석 및 입력 검증.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from generator.config import (
    _BRACKET_TABLE,
    REF_WEIGHT_MAX,
    REF_WEIGHT_MIN,
    SAFE_PREFIX_PATTERN,
    WEIGHT_SUFFIX_PATTERN,
    ConfigError,
)


def normalize_tag(raw: str) -> str:
    """
    비교 가능한 형태로 태그를 정규화한다.
    괄호 강조와 가중치 표기를 제거하고 소문자·단일 공백으로 맞춘다.
    """
    tag = raw.strip().lower().translate(_BRACKET_TABLE)
    tag = WEIGHT_SUFFIX_PATTERN.sub("", tag)
    return " ".join(tag.split())


def split_tags(text: str) -> list[str]:
    """쉼표로 분리해 정규화한 태그 목록. 빈 토큰은 버린다."""
    return [tag for tag in map(normalize_tag, text.split(",")) if tag]


def find_tag_conflicts(positive: str, negative: str) -> list[str]:
    """포지티브와 네거티브에 동시에 존재하는 태그를 찾는다."""
    return sorted(set(split_tags(positive)) & set(split_tags(negative)))


def join_tags(*parts: str) -> str:
    """빈 조각을 건너뛰고 쉼표로 이어붙인다."""
    return ", ".join(part.strip() for part in parts if part and part.strip())


def assemble_prompt(
    base_positive: str,
    char_prompt: str,
    pose_prompt: str,
    trigger_tag: str = "",
) -> str:
    """
    포즈와 캐릭터 프롬프트를 최적의 CLIP 순서로 결합한다.
    base_positive 에 ' BREAK '가 포함되어 있으면:
      [품질 태그], [포즈 태그] BREAK [캐릭터 외형 태그], [트리거 태그]
    순으로 조립하여 포즈/구도가 긴 외형 묘사에 밀려 후순위 청크로 넘어가는 것을 방지한다.
    ' BREAK '가 없으면 기존 방식(base_positive, char_prompt, pose_prompt, trigger_tag)을 유지한다.
    """
    if " BREAK " in base_positive:
        quality_part, char_part = base_positive.split(" BREAK ", 1)
        first_chunk = join_tags(quality_part, pose_prompt)
        second_chunk = join_tags(char_part, char_prompt, trigger_tag)
        return f"{first_chunk} BREAK {second_chunk}"
    return join_tags(base_positive, char_prompt, pose_prompt, trigger_tag)


def resolve_background(
    cli_bg: str | None,
    bg_file: Path,
    preset: str = "default",
) -> str | None:
    """
    감정(emotions) 씬에 적용할 배경 프롬프트를 해석한다.

    1. CLI 또는 캐릭터 JSON에서 전달된 cli_bg 가 있으면 최우선 반환.
    2. bg_file (projects/{roster}/background.json) 이 존재하면:
       JSON 내에서 preset(기본 "default")에 해당하는 문자열 반환.
    3. 일치하는 프리셋이 없거나 파일이 없으면 None 반환 (기존 clean background 유지).
    """
    if cli_bg and cli_bg.strip():
        return cli_bg.strip()

    if bg_file.exists():
        try:
            with open(bg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                bg_val = data.get(preset)
                if isinstance(bg_val, str) and bg_val.strip():
                    return bg_val.strip()
                if preset != "default" and "default" in data:
                    print(
                        f"[WARN] 배경 프리셋 '{preset}' 이(가) 없어 'default' 프리셋으로 폴백합니다."
                    )
                    default_val = data.get("default")
                    if isinstance(default_val, str) and default_val.strip():
                        return default_val.strip()
        except Exception as e:
            print(f"[WARN] 배경 설정 파일 로드 실패 ({bg_file}): {e}", file=sys.stderr)

    return None


def validate_ref_weight(value: float) -> float:
    """IP-Adapter 적용 강도를 검증한다."""
    if not REF_WEIGHT_MIN <= value <= REF_WEIGHT_MAX:
        raise ConfigError(
            f"--ref_weight 는 {REF_WEIGHT_MIN}~{REF_WEIGHT_MAX} 범위여야 합니다: {value}",
            "0.5~0.8 이 실무 범위입니다. 1.0 이상은 참조 이미지의 포즈까지 전이됩니다.",
        )
    return value


def validate_prefix(prefix: str) -> str:
    """prefix 를 안전한 단일 경로 세그먼트로 제한한다."""
    candidate = prefix.strip()
    if not SAFE_PREFIX_PATTERN.match(candidate):
        raise ConfigError(
            f"prefix '{prefix}' 를 사용할 수 없습니다.",
            "영문·숫자·밑줄·하이픈 1~64자만 허용합니다. (예: mika, test_01)",
        )
    return candidate

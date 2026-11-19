"""
generator.option_specs
GenerationOptions ↔ CLI(sd_batch_generator.py) ↔ GUI(gui/settings.py · runner_bridge.py)
삼위일체(Trinity Sync) 옵션 메타데이터 단일 진실 공급원(SSOT) 레지스트리.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """단일 생성 옵션의 CLI·GUI·모델 메타데이터 명세."""

    field: str
    cli_flag: str
    gui_key: str | None
    label: str
    tooltip: str
    default: Any
    value_type: str  # "bool" | "int" | "float" | "str"
    gui_default: Any = None
    has_gui_default: bool = False
    gui_inverted: bool = False
    choices: tuple[str, ...] = ()

    @property
    def effective_gui_default(self) -> Any:
        """GUI 설정 파일(DEFAULT_SETTINGS)에 저장되는 기본값."""
        if self.has_gui_default:
            return self.gui_default
        if self.gui_inverted and isinstance(self.default, bool):
            return not self.default
        return self.default

    def to_dict(self) -> dict[str, Any]:
        """직렬화/스냅샷/UI 바인딩용 딕셔너리 표현."""
        return {
            "field": self.field,
            "cli_flag": self.cli_flag,
            "gui_key": self.gui_key,
            "label": self.label,
            "tooltip": self.tooltip,
            "default": self.default,
            "value_type": self.value_type,
            "gui_default": self.effective_gui_default if self.gui_key else None,
            "gui_inverted": self.gui_inverted,
            "choices": self.choices,
        }


# Kiro 핵심 생성 옵션 SSOT 레지스트리
OPTION_SPEC_OBJECTS: tuple[OptionSpec, ...] = (
    OptionSpec(
        field="steps",
        cli_flag="--steps",
        gui_key="steps",
        label="생성 스텝 (Steps)",
        tooltip="이미지 샘플링 스텝 수 (미지정 시 체크포인트 프로필/기본값 적용)",
        default=None,
        value_type="int",
        gui_default=30,
        has_gui_default=True,
    ),
    OptionSpec(
        field="sampler",
        cli_flag="--sampler",
        gui_key="sampler",
        label="샘플러 (Sampler)",
        tooltip="SD WebUI 샘플러 이름 (예: DPM++ 2M, Euler a)",
        default=None,
        value_type="str",
        gui_default="DPM++ 2M",
        has_gui_default=True,
    ),
    OptionSpec(
        field="scheduler",
        cli_flag="--scheduler",
        gui_key="scheduler",
        label="스케줄러 (Scheduler)",
        tooltip="노이즈 스케줄러 (예: Karras, Automatic, SGM Uniform)",
        default=None,
        value_type="str",
        gui_default="Karras",
        has_gui_default=True,
    ),
    OptionSpec(
        field="cfg_scale",
        cli_flag="--cfg-scale",
        gui_key="cfg_scale",
        label="CFG Scale",
        tooltip="프롬프트 충실도 가중치 (미지정 시 체크포인트 프로필/기본값 적용)",
        default=None,
        value_type="float",
        gui_default=6.0,
        has_gui_default=True,
    ),
    OptionSpec(
        field="no_ref",
        cli_flag="--no_ref",
        gui_key="use_reference",
        label="레퍼런스(IP-Adapter) 사용",
        tooltip="참조 이미지 기반 IP-Adapter 연동 여부 (GUI에서는 use_reference 반전 토글)",
        default=False,
        value_type="bool",
        gui_default=True,
        has_gui_default=True,
        gui_inverted=True,
    ),
    OptionSpec(
        field="naming",
        cli_flag="--naming",
        gui_key="naming",
        label="파일명 명명 규칙",
        tooltip="에셋 파일명 규칙 (code / hybrid / label)",
        default="code",
        value_type="str",
        gui_default="hybrid",
        has_gui_default=True,
        choices=("code", "hybrid", "label"),
    ),
    OptionSpec(
        field="overwrite",
        cli_flag="--overwrite",
        gui_key="overwrite",
        label="기존 파일 덮어쓰기",
        tooltip="이미 존재하는 에셋 파일을 건너뛰지 않고 강제로 재생성",
        default=False,
        value_type="bool",
        gui_default=True,
        has_gui_default=True,
    ),
    OptionSpec(
        field="auto_qc",
        cli_flag="--auto-qc",
        gui_key="auto_qc",
        label="Auto-QC 자동 검수/재시도",
        tooltip="인원 초과·머리 복제·손 증식·모브 얼굴 노출 감지 시 시드 변경 자동 재시도",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="strict_censor_qc",
        cli_flag="--strict-censor-qc",
        gui_key="strict_censor_qc",
        label="성인 씬 타겟 미탐지 리롤 (Strict QC)",
        tooltip="성인 노출 필수 씬에서 타겟 미탐지(0건) 시 구도 실패로 간주하고 자동 리롤",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="max_retries",
        cli_flag="--max-retries",
        gui_key="max_retries",
        label="QC 최대 재시도 횟수",
        tooltip="Auto-QC / Strict QC 불량 시 최대 시드 리롤 횟수",
        default=3,
        value_type="int",
    ),
    OptionSpec(
        field="censor",
        cli_flag="--censor",
        gui_key="censor",
        label="성인 씬 자동 검열",
        tooltip="성인/탈의 씬 생성 시 자동 검열 적용 및 assets/{char}_origin/ 원본 보존",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="censor_style",
        cli_flag="--censor-style",
        gui_key="censor_style",
        label="검열 렌더링 스타일",
        tooltip="자동 검열 시 적용할 마스킹 스타일",
        default="white_silhouette",
        value_type="str",
        choices=(
            "white_silhouette",
            "black_silhouette",
            "mosaic",
            "wide_bar",
            "slim_bar",
            "shadow",
            "manga_bar",
            "manga_glow",
            "debug_bbox",
        ),
    ),
    OptionSpec(
        field="clip_skip",
        cli_flag="--clip-skip",
        gui_key=None,
        label="CLIP Skip",
        tooltip="CLIP Skip 레이어 수 직접 지정 (예: 2)",
        default=None,
        value_type="int",
    ),
    OptionSpec(
        field="clamp_weights",
        cli_flag="--clamp-weights",
        gui_key=None,
        label="프롬프트 가중치 클램핑",
        tooltip="과도한 강조 가중치를 긍정 1.15 / 부정 1.25 이하로 제한 (선택 옵션, 기본 비활성)",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="seed",
        cli_flag="--seed",
        gui_key=None,
        label="고정 시드",
        tooltip="특정 난수 시드 고정 생성 (지정 시 결정론적 포즈 시드보다 우선)",
        default=None,
        value_type="int",
    ),
    OptionSpec(
        field="deterministic_pose_seed",
        cli_flag="--deterministic-pose-seed",
        gui_key=None,
        label="포즈 결정론적 시드",
        tooltip="포즈 코드·캐릭터 prefix 기반 CRC32 결정론적 시드 자동 적용",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="pose_set",
        cli_flag="--pose-set",
        gui_key=None,
        label="포즈 세트 프리셋",
        tooltip="pose_sets.json 프리셋 ID 지정 (예: sample10, daily_all, female_full)",
        default=None,
        value_type="str",
    ),
    OptionSpec(
        field="ckpt_profile",
        cli_flag="--ckpt-profile",
        gui_key=None,
        label="체크포인트 프로필",
        tooltip="체크포인트 수치 파라미터 프로필 적용 (unholy, wai, flatbread 등)",
        default=None,
        value_type="str",
    ),
    OptionSpec(
        field="enable_freeu",
        cli_flag="--enable-freeu",
        gui_key=None,
        label="FreeU 활성화",
        tooltip="Forge 내장 FreeU SDXL 디테일 강화 활성화",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="enable_adetailer",
        cli_flag="--enable-adetailer",
        gui_key=None,
        label="ADetailer 활성화",
        tooltip="ADetailer 얼굴/손 자동 보정 활성화",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="enable_lightning",
        cli_flag="--enable-lightning",
        gui_key=None,
        label="SDXL Lightning 가속",
        tooltip="SDXL Lightning LoRA 초고속 생성 모드 활성화",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="mock",
        cli_flag="--mock",
        gui_key=None,
        label="모의 생성 (Mock)",
        tooltip="WebUI 통신 없이 더미 이미지를 생성해 파이프라인 검증",
        default=False,
        value_type="bool",
    ),
    OptionSpec(
        field="dry_run",
        cli_flag="--dry-run",
        gui_key=None,
        label="드라이 런 (Dry-Run)",
        tooltip="파일 쓰기 없이 대상 코드·파일명·프롬프트 계획만 점검",
        default=False,
        value_type="bool",
    ),
)

# 딕셔너리 기반 SSOT 뷰 (외부 도구·직렬화·테스트 호환)
GENERATION_OPTION_SPECS: list[dict[str, Any]] = [spec.to_dict() for spec in OPTION_SPEC_OBJECTS]


def get_option_spec(field_name: str) -> OptionSpec | None:
    """필드명으로 OptionSpec 을 조회한다."""
    for spec in OPTION_SPEC_OBJECTS:
        if spec.field == field_name:
            return spec
    return None


def get_gui_option_specs() -> tuple[OptionSpec, ...]:
    """GUI 설정(gui/settings.py)과 바인딩되는 옵션 목록만 반환한다."""
    return tuple(spec for spec in OPTION_SPEC_OBJECTS if spec.gui_key is not None)


__all__ = [
    "GENERATION_OPTION_SPECS",
    "OPTION_SPEC_OBJECTS",
    "OptionSpec",
    "get_gui_option_specs",
    "get_option_spec",
]

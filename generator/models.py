"""
generator.models
핵심 데이터 모델 (Data Classes) 정의.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


@dataclass(frozen=True, slots=True)
class RosterPaths:
    """
    로스터별 경로 캡슐화 (SSOT - Single Source of Truth).
    
    신규 프로젝트가 추가되어도 이 클래스만 생성하면 전체 파이프라인이
    자동으로 해당 경로를 참조한다.
    """
    roster_name: str
    characters_dir: Path
    assets_dir: Path
    references_dir: Path  # 모든 로스터가 공유하는 참조 이미지 폴더
    
    @property
    def events_file(self) -> Path:
        """로스터 전용 이벤트 포즈 파일 (pose_overrides.json 또는 events.json)"""
        p_overrides = self.characters_dir.parent / "pose_overrides.json"
        if p_overrides.is_file():
            return p_overrides
        return self.characters_dir.parent / "events.json"

    @property
    def background_file(self) -> Path:
        """로스터 전용 배경 설정 파일 (projects/{roster}/background.json)"""
        return self.characters_dir.parent / "background.json"

    def validate(self) -> tuple[bool, str]:
        """경로 존재 여부 검증. 반환: (성공 여부, 에러 메시지)"""
        if not self.characters_dir.exists():
            return False, f"캐릭터 폴더가 존재하지 않습니다: {self.characters_dir}"
        if not self.characters_dir.is_dir():
            return False, f"캐릭터 경로가 디렉터리가 아닙니다: {self.characters_dir}"
        return True, ""


@dataclass(frozen=True, slots=True)
class PoseEntry:
    """포즈/표정 단일 항목."""

    code: int
    prompt: str
    section: str
    width: int | None = None   # 동적 해상도 오버라이드 (optional)
    height: int | None = None
    label_override: str | None = None  # pose_database.json 의 "label" 필드 (optional)
    description: str = ""              # pose_database.json 의 "description" 필드 (optional)
    required_outfit: str = ""          # 의상 슬롯 요구사항 (optional)
    prompt_female: str = ""            # 여성 전용 프롬프트 (optional)
    prompt_otokonoko: str = ""         # 오토코노코 전용 프롬프트 (optional)
    negative_add: str = ""             # 포즈별 추가 네거티브 (optional)

    @property
    def label(self) -> str:
        """표시용 라벨.
        pose_database.json 에 'label' 필드가 있으면 그것을 우선 사용하고,
        없으면 프롬프트의 첫 태그를 폴백으로 반환한다.
        """
        if self.label_override:
            return self.label_override
        return self.prompt.split(",")[0].strip()

    def get_prompt(self, gender: str = "female") -> str:
        """성별(female, otokonoko 등)에 맞춘 전용 프롬프트를 반환한다. 미지정 시 기본 prompt 반환."""
        g = (gender or "").lower().strip()
        if g == "otokonoko" and self.prompt_otokonoko:
            return self.prompt_otokonoko
        if (g in ("female", "girl", "woman") or not g) and self.prompt_female:
            return self.prompt_female
        return self.prompt


@dataclass(frozen=True, slots=True)
class Profile:
    """
    성별 등 캐릭터 축 프리셋.

    포즈·표정과 직교하는 축이므로 포즈 섹션에 섞지 않고 별도로 둔다.
    """

    name: str
    base_positive: str
    base_negative: str


@dataclass(slots=True)
class PoseDatabase:
    entries: dict[int, PoseEntry] = field(default_factory=dict)
    sections: dict[str, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_codes(self) -> list[int]:
        return sorted(self.entries)

    @property
    def section_names(self) -> list[str]:
        return list(self.sections)


@dataclass(frozen=True, slots=True)
class CharacterConfig:
    """
    characters/{name}.json 에서 로드한 캐릭터 프리셋.

    --char name 으로 지정하면 이 값들이 커맨드라인 기본값으로 쓰인다.
    커맨드라인에 같은 인자가 있으면 커맨드라인 쪽이 우선한다.
    """

    name: str
    char_prompt: str
    prefix: str
    profile: str | None = None
    custom_neg: str = ""
    ref_weight: float | None = None
    positive: str | None = None
    negative: str | None = None
    default_mode: str | None = None
    background: str | None = None  # 캐릭터별 고유 배경 (emotions 씬 치환용)
    nude_positive: str | None = None  # 탈의/H-씬 전용 프롬프트 (sdxl_nude_positive 호환)
    gender: str = "female"  # 성별 (female, otokonoko 등)
    outfits: dict[str, str] = field(default_factory=dict)  # 테마 의상 매핑 (default, swimsuit, maid 등)
    gen_overrides: dict[str, Any] = field(default_factory=dict)  # 캐릭터별 선택 생성 수치 오버라이드 (steps/cfg/sampler 등)


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """
    IP-Adapter 참조 이미지.
    b64 는 호출마다 재인코딩하지 않고 생성 시점에 한 번만 계산하여 보관한다.
    """

    path: Path
    b64: str
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.path.name} ({self.width}x{self.height})"


@dataclass(frozen=True, slots=True)
class ControlNetSpec:
    """ControlNet 전처리기와 모델 조합."""

    module: str
    model: str
    source: str = "auto"


@dataclass(frozen=True, slots=True)
class TimingStats:
    """생성 시간 집계."""

    count: int
    total: float
    average: float
    fastest: float
    slowest: float

    def format(self) -> str:
        return (
            f"{self.count}장 / 총 {self.total:.1f}초 / "
            f"장당 평균 {self.average:.1f}초 "
            f"(최속 {self.fastest:.1f} ~ 최저 {self.slowest:.1f})"
        )


def summarize_durations(seconds: Sequence[float]) -> TimingStats | None:
    """측정값을 집계한다 (순수 함수). 빈 입력은 None."""
    if not seconds:
        return None
    total = sum(seconds)
    return TimingStats(
        count=len(seconds),
        total=total,
        average=total / len(seconds),
        fastest=min(seconds),
        slowest=max(seconds),
    )


@dataclass(slots=True)
class BatchResult:
    success: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)
    durations: list[tuple[int, float]] = field(default_factory=list)
    qa_warnings: list[tuple[int, str, str]] = field(default_factory=list)  # (code, label, reason)
    aborted: bool = False
    dry_run: bool = False

    @property
    def existing(self) -> list[int]:
        """마크다운 대상 코드."""
        if self.dry_run:
            return sorted(self.planned)
        return sorted(self.success + self.skipped)

    @property
    def failed_codes(self) -> list[int]:
        return [code for code, _ in self.failed]

    @property
    def timing(self) -> TimingStats | None:
        """생성 시간 집계. 측정값이 없으면 None."""
        return summarize_durations([sec for _, sec in self.durations])


@dataclass(slots=True)
class TestReport:
    """--test 결과 수집기."""

    passed: int = 0
    failed: int = 0
    warned: int = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.passed += ok
        self.failed += not ok
        self._emit("[PASS]" if ok else "[FAIL]", name, detail)
        return ok

    def warn(self, name: str, detail: str = "") -> None:
        self.warned += 1
        self._emit("[WARN]", name, detail)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        self._emit("[PASS]", name, detail)

    @staticmethod
    def _emit(tag: str, name: str, detail: str) -> None:
        print(f"  {tag} {name}" + (f" - {detail}" if detail else ""))

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(slots=True)
class GenerationOptions:
    """단일 및 전체 캐릭터 배치 생성을 위한 타입 안전 실행 설정."""

    prefix: str = ""
    char_prompt: str = ""
    profile: str | None = None
    custom_neg: str = ""
    positive: str | None = None
    negative: str | None = None
    mode: str = "all"
    codes: str | None = None
    pose_set: str | None = None
    bg: str | None = None
    bg_preset: str = "default"
    ref_image: str | None = None
    ref_weight: float = 0.7
    no_ref: bool = False
    cn_module: str | None = None
    cn_model: str | None = None
    dry_run: bool = False
    mock: bool = False
    enable_freeu: bool = False
    freeu_b1: float = 1.1
    freeu_b2: float = 1.2
    freeu_s1: float = 0.9
    freeu_s2: float = 0.2
    enable_adetailer: bool = False
    enable_lightning: bool = False
    lightning_steps: int = 2
    lightning_cfg: float = 1.0
    lightning_lora_name: str = "sdxl_lightning_2step_lora"
    steps: int | None = None
    sampler: str | None = None
    scheduler: str | None = None
    cfg_scale: float | None = None
    width: int | None = None
    height: int | None = None
    clip_skip: int | None = None
    naming: str = "code"
    overwrite: bool = False
    nude_positive: str | None = None
    gender: str | None = None
    outfits: dict[str, str] | None = None
    auto_qc: bool = False
    max_retries: int = 3
    censor: bool = False
    censor_style: str = "white_silhouette"
    clamp_weights: bool = False
    seed: int | None = None
    deterministic_pose_seed: bool = False
    ckpt_profile: str | None = None
    active_ckpt: Any = None
    char_gen_overrides: dict[str, Any] | None = None

    @classmethod
    def from_namespace(cls, args: Any) -> GenerationOptions:
        """argparse.Namespace 또는 유사 객체를 GenerationOptions 로 변환한다."""
        if isinstance(args, cls):
            return args
        auto_qc_val = bool(
            getattr(args, "auto_qc", False) or getattr(args, "auto_retry_qa", False)
        )
        return cls(
            prefix=str(getattr(args, "prefix", "") or ""),
            char_prompt=str(getattr(args, "char_prompt", "") or ""),
            profile=getattr(args, "profile", None),
            custom_neg=str(getattr(args, "custom_neg", "") or ""),
            positive=getattr(args, "positive", None),
            negative=getattr(args, "negative", None),
            mode=str(getattr(args, "mode", "all") or "all"),
            codes=getattr(args, "codes", None),
            pose_set=getattr(args, "pose_set", None),
            bg=getattr(args, "bg", None),
            bg_preset=str(getattr(args, "bg_preset", "default") or "default"),
            ref_image=getattr(args, "ref_image", None),
            ref_weight=float(
                getattr(args, "ref_weight", 0.7)
                if getattr(args, "ref_weight", None) is not None
                else 0.7
            ),
            no_ref=bool(getattr(args, "no_ref", False)),
            cn_module=getattr(args, "cn_module", None),
            cn_model=getattr(args, "cn_model", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            mock=bool(getattr(args, "mock", False)),
            enable_freeu=bool(getattr(args, "enable_freeu", False)),
            freeu_b1=float(getattr(args, "freeu_b1", 1.1)),
            freeu_b2=float(getattr(args, "freeu_b2", 1.2)),
            freeu_s1=float(getattr(args, "freeu_s1", 0.9)),
            freeu_s2=float(getattr(args, "freeu_s2", 0.2)),
            enable_adetailer=bool(getattr(args, "enable_adetailer", False)),
            enable_lightning=bool(getattr(args, "enable_lightning", False)),
            lightning_steps=int(getattr(args, "lightning_steps", 2)),
            lightning_cfg=float(getattr(args, "lightning_cfg", 1.0)),
            lightning_lora_name=str(
                getattr(args, "lightning_lora_name", "sdxl_lightning_2step_lora")
                or "sdxl_lightning_2step_lora"
            ),
            steps=getattr(args, "steps", None),
            sampler=getattr(args, "sampler", None),
            scheduler=getattr(args, "scheduler", None),
            cfg_scale=getattr(args, "cfg_scale", None),
            width=getattr(args, "width", None),
            height=getattr(args, "height", None),
            clip_skip=getattr(args, "clip_skip", None),
            naming=str(getattr(args, "naming", "code") or "code"),
            overwrite=bool(getattr(args, "overwrite", False)),
            nude_positive=getattr(args, "nude_positive", None),
            gender=getattr(args, "gender", None),
            outfits=getattr(args, "outfits", None),
            auto_qc=auto_qc_val,
            max_retries=int(getattr(args, "max_retries", 3)),
            censor=bool(getattr(args, "censor", False)),
            censor_style=str(
                getattr(args, "censor_style", "white_silhouette") or "white_silhouette"
            ),
            clamp_weights=bool(getattr(args, "clamp_weights", False)),
            seed=getattr(args, "seed", None),
            deterministic_pose_seed=bool(getattr(args, "deterministic_pose_seed", False)),
            ckpt_profile=getattr(args, "ckpt_profile", None),
            active_ckpt=getattr(args, "active_ckpt", None),
            char_gen_overrides=getattr(args, "char_gen_overrides", None),
        )


@dataclass(slots=True)
class AllCharsResult:
    """run_all_chars 전체 캐릭터 실행 집계."""

    total: int = 0
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    qa_issues: list[tuple[str, int, str, str]] = field(default_factory=list)
    aborted: bool = False


@dataclass(slots=True)
class RunHooks:
    """실행 진행률 콜백, 취소 체크, 캐릭터별 완료 이벤트 및 결과 수집 훅."""

    progress_callback: Callable[[int, str, Path, int, int], None] | None = None
    is_cancelled: Callable[[], bool] | None = None
    on_char_start: Callable[[int, int, str, CharacterConfig], None] | None = None
    char_progress_callback: (
        Callable[[int, int, str, CharacterConfig, int, str, Path, int, int], None]
        | None
    ) = None
    on_char_complete: (
        Callable[[int, int, str, CharacterConfig, BatchResult | None, int], None]
        | None
    ) = None
    last_result: BatchResult | None = None
    last_all_result: AllCharsResult | None = None

    @classmethod
    def from_namespace(cls, args: Any) -> RunHooks:
        if isinstance(args, cls):
            return args
        return cls(
            progress_callback=getattr(args, "progress_callback", None),
            is_cancelled=getattr(args, "is_cancelled", None),
        )

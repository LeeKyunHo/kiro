"""
generator.models
핵심 데이터 모델 (Data Classes) 정의.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


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
        """로스터 전용 이벤트 포즈 파일 (projects/{roster}/events.json)"""
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
    width: int | None = None  # 동적 해상도 오버라이드 (optional)
    height: int | None = None

    @property
    def label(self) -> str:
        """프롬프트 첫 태그를 사람이 읽을 라벨로 사용."""
        return self.prompt.split(",")[0].strip()


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
    profiles: dict[str, Profile] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_codes(self) -> list[int]:
        return sorted(self.entries)

    @property
    def section_names(self) -> list[str]:
        return list(self.sections)

    @property
    def profile_names(self) -> list[str]:
        return list(self.profiles)


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
class InterrogateResult:
    """태그 역추출 결과."""

    raw: str
    tags: list[str]
    gender_tags: list[str]

    @property
    def filtered(self) -> str:
        """성별·인원 태그를 제거한 프롬프트 문자열."""
        excluded = set(self.gender_tags)
        return ", ".join(tag for tag in self.tags if tag not in excluded)


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

"""
불변 값 객체.

모든 dataclass 를 `frozen=True, slots=True` 로 선언한다.
frozen 은 실수로 상태를 바꾸는 것을 막고, slots 는 인스턴스 `__dict__` 를
제거해 메모리와 속성 접근 비용을 줄인다.

예외는 `BatchResult` 하나다. 배치 진행 중 누적되는 가변 집계이므로
frozen 을 걸 수 없다. 대신 컨테이너 필드를 `field(default_factory=...)` 로
두어 클래스 레벨 공유 버그를 막는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import config


# ─────────────────────────────────────────────
# 포즈 데이터
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PoseEntry:
    """포즈/표정 단일 항목."""

    code: int
    prompt: str
    section: str

    @property
    def label(self) -> str:
        """
        프롬프트 첫 태그를 사람이 읽을 라벨로 사용한다.

        콘솔 출력과 젠잇 상태 매핑 가이드에 표시되므로, JSON 작성 시
        항목을 구별하는 서술어를 맨 앞에 두어야 한다.
        """
        return self.prompt.split(",")[0].strip()


@dataclass(frozen=True, slots=True)
class Profile:
    """
    성별 등 캐릭터 축 프리셋.

    포즈·표정과 직교하는 축이므로 포즈 섹션에 섞지 않고 별도로 둔다.
    섹션에 성별을 넣으면 emotions_female / emotions_male 처럼 조합이
    곱셈으로 늘어나 같은 감정을 여러 곳에서 관리해야 한다.

    base_positive / base_negative 는 스크립트 기본값을 **완전히 대체**한다.
    따라서 품질 태그도 프로필 쪽에 포함되어야 한다.
    """

    name: str
    base_positive: str
    base_negative: str


@dataclass(frozen=True, slots=True)
class PoseDatabase:
    """
    pose_database.json 을 정규화한 결과.

    warnings 는 파싱 중 수집한 경고다. 즉시 출력하지 않고 모아두는 이유는
    생성 로그와 섞이지 않게 하기 위함이다.
    """

    entries: dict[int, PoseEntry] = field(default_factory=dict)
    sections: dict[str, tuple[int, ...]] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    exclusive_groups: tuple[frozenset[str], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def all_codes(self) -> tuple[int, ...]:
        """전체 코드를 숫자 크기순으로 정렬해 반환한다."""
        return tuple(sorted(self.entries))

    @property
    def section_names(self) -> tuple[str, ...]:
        return tuple(self.sections)

    @property
    def profile_names(self) -> tuple[str, ...]:
        return tuple(self.profiles)

    def entry(self, code: int) -> PoseEntry:
        return self.entries[code]


# ─────────────────────────────────────────────
# 참조 이미지 및 ControlNet
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """
    IP-Adapter 참조 이미지.

    b64 를 프로퍼티가 아닌 필드로 갖는다. 20~50장을 생성하는 배치에서
    프로퍼티로 두면 접근마다 재인코딩되므로 생성 시점에 한 번만 계산한다.

    "참조 없음" 은 이 클래스의 특수 인스턴스가 아니라 None 으로 표현한다.
    Optional 이 부재를 가장 정직하게 표현하고, 호출부에서 `if reference:`
    한 줄로 분기된다.
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
    """
    ControlNet 전처리기와 모델 조합.

    source 는 자동 탐지("auto")인지 사용자 지정("manual")인지 구분한다.
    GPU 환경에서 어느 경로가 동작했는지 로그로 판단하기 위한 것이다.
    """

    module: str
    model: str
    source: str = "auto"

    @property
    def label(self) -> str:
        return f"{self.module} / {self.model} ({self.source})"


@dataclass(frozen=True, slots=True)
class ReferenceContext:
    """
    참조 이미지 축의 해석 결과 묶음.

    image 와 spec 이 **둘 다** 있을 때만 ControlNet 을 주입한다.
    참조는 있는데 ControlNet 해석이 실패한 경우(미설치 등)에는 텍스트
    프롬프트만으로 생성해야 하므로, 두 값을 함께 들고 다닌다.
    """

    image: ReferenceImage | None = None
    spec: ControlNetSpec | None = None
    weight: float = config.REF_WEIGHT_DEFAULT

    @property
    def active(self) -> bool:
        """ControlNet 주입이 가능한 상태인지."""
        return self.image is not None and self.spec is not None


# ─────────────────────────────────────────────
# 태그 역추출
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class InterrogateResult:
    """태그 역추출 결과."""

    raw: str
    tags: tuple[str, ...]
    gender_tags: tuple[str, ...]

    @property
    def filtered(self) -> str:
        """성별·인원 태그를 제거한 프롬프트 문자열."""
        excluded = set(self.gender_tags)
        return ", ".join(tag for tag in self.tags if tag not in excluded)


# ─────────────────────────────────────────────
# 실행 결과
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TimingStats:
    """
    생성 시간 집계.

    VRAM 설정(--medvram-sdxl, xFormers 등)을 비교할 때 판단 근거가 된다.
    한 배치에 수십 장을 순차 생성하므로 장당 손실이 누적된다.
    """

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


@dataclass(frozen=True, slots=True)
class VramSnapshot:
    """VRAM 사용량. GiB 단위."""

    peak: float
    total: float

    @property
    def ratio(self) -> float:
        return (self.peak / self.total * 100.0) if self.total else 0.0

    def format(self) -> str:
        return f"피크 {self.peak:.2f} / {self.total:.2f} GiB ({self.ratio:.0f}%)"


@dataclass(frozen=True, slots=True)
class Failure:
    """개별 코드의 생성 실패."""

    code: int
    reason: str


@dataclass(slots=True)
class BatchResult:
    """
    배치 실행 집계.

    유일한 가변 dataclass 다. 진행 중 누적되므로 frozen 을 걸 수 없다.
    """

    succeeded: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    aborted: bool = False
    plan_only: bool = False

    @property
    def deliverable_codes(self) -> tuple[int, ...]:
        """
        젠잇 마크다운에 실을 코드.

        기본/mock: 디스크에 실제 파일이 있는 코드만
        plan-only(--dry-run): 시뮬레이션 대상 전체

        분기를 프로퍼티 내부에 두어 호출부가 실행 모드를 알 필요가 없게 한다.
        dry-run 은 API 를 호출하지 않으므로 succeeded 가 항상 비어 있고,
        "실존 파일만" 규칙을 그대로 적용하면 마크다운이 0줄이 되어
        조립 로직을 검증할 수 없다.
        """
        if self.plan_only:
            return tuple(sorted(self.planned))
        return tuple(sorted(self.succeeded + self.skipped))

    @property
    def failed_codes(self) -> tuple[int, ...]:
        return tuple(failure.code for failure in self.failures)

    @property
    def timing(self) -> TimingStats | None:
        return summarize_durations(self.durations)


def summarize_durations(seconds: Sequence[float]) -> TimingStats | None:
    """측정값을 집계한다 (순수 함수). 빈 입력은 None."""
    if not seconds:
        return None
    total = float(sum(seconds))
    return TimingStats(
        count=len(seconds),
        total=total,
        average=total / len(seconds),
        fastest=min(seconds),
        slowest=max(seconds),
    )


# ─────────────────────────────────────────────
# 진단
# ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """
    단일 검사 결과.

    status 는 "PASS" / "FAIL" / "WARN".
    WARN 은 데이터 품질 문제로, 런타임에서 경고 후 계속 진행하는 항목과
    등급을 일치시킨다. 50개 항목 중 오타 하나로 종료 코드가 1이 되면
    CI 게이트로 쓰기 불편하다.
    """

    check_id: str
    name: str
    status: str
    detail: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status == "FAIL"

    def format(self) -> str:
        head = f"  [{self.status}] {self.check_id} {self.name}"
        return f"{head} - {self.detail}" if self.detail else head

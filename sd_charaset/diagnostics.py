"""
자체 진단 (`--test`).

**설계 원칙: 실제 구현 함수를 직접 호출한다.**

검사 코드에 로직을 복제하면 구현이 바뀔 때 검사가 함께 틀어져 회귀를
놓친다. 여기서는 `codes.CodeFormatter`, `tags.normalize_tag`,
`payload.build_generation_payload` 등을 그대로 불러 쓴다.

외부 테스트 프레임워크를 도입하지 않는 이유는 의존성이 `requests`,
`Pillow` 로 한정되고 단독 CLI 실행이 가능해야 하기 때문이다.

**검증 범위의 한계**

이 진단은 페이로드 **구조**만 검사한다. "WebUI 가 이 페이로드를
수락하는가" 는 범위 밖이며 GPU 환경이 필요하다. 그래서 결과 메시지에
"동작 확인" 이라는 표현을 쓰지 않는다.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from PIL import Image

from . import config, payload
from .codes import (
    CodeFormatter,
    looks_like_code_expression,
    parse_code_expression,
    resolve_codes,
)
from .database import (
    iter_pose_sections,
    parse_pose_database,
    read_pose_json,
    resolve_profile,
)
from .errors import ConfigError, DatabaseError, ValidationError
from .logging_setup import emit
from .models import (
    CheckOutcome,
    ControlNetSpec,
    InterrogateResult,
    PoseDatabase,
    PoseEntry,
    Profile,
    ReferenceContext,
    summarize_durations,
)
from .output import build_genit_block
from .storage import (
    find_reference_candidates,
    load_reference,
    resolve_reference_image,
)
from .tags import (
    find_duplicate_tags,
    find_exclusive_conflicts,
    join_tags,
    normalize_tag,
    parse_exclusive_groups,
    partition_gender_tags,
)
from .validators import (
    audit_profile,
    validate_interrogator,
    validate_prefix,
    validate_ref_weight,
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


# ─────────────────────────────────────────────
# 결과 수집
# ─────────────────────────────────────────────
@dataclass(slots=True)
class Report:
    """검사 결과 누적기."""

    outcomes: list[CheckOutcome] = field(default_factory=list)

    def record(
        self, check_id: str, name: str, status: str, detail: str = ""
    ) -> bool:
        outcome = CheckOutcome(check_id, name, status, detail)
        self.outcomes.append(outcome)
        emit(outcome.format())
        return status != FAIL

    def check(self, check_id: str, name: str, ok: bool, detail: str = "") -> bool:
        return self.record(check_id, name, PASS if ok else FAIL, detail)

    def warn(self, check_id: str, name: str, detail: str = "") -> None:
        self.record(check_id, name, WARN, detail)

    def ok(self, check_id: str, name: str, detail: str = "") -> None:
        self.record(check_id, name, PASS, detail)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.is_failure)

    @property
    def warned(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == WARN)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


# ─────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────
SYNTHETIC_DB: dict[str, Any] = {
    "_comment": "self-test fixture",
    "alpha": {"5": "five, tag", "12": "twelve, tag", "03": "three, tag"},
    "beta": {"7": "seven, tag"},
}

PARSER_CASES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("20-29", tuple(range(20, 30))),
    ("0,3,7", (0, 3, 7)),
    ("0-5,10,20-22", (0, 1, 2, 3, 4, 5, 10, 20, 21, 22)),
    ("29-20", tuple(range(20, 30))),          # 역순 교정
    ("0-5,3", (0, 1, 2, 3, 4, 5)),            # 중복 정규화
    (" 1 , 2 ", (1, 2)),                      # 공백 허용
)

REJECT_EXPRESSIONS: tuple[str, ...] = (
    "abc",
    "1-",
    f"0-{config.CODE_MAX + 1}",
    "-5",
)

UNSAFE_PREFIXES: tuple[str, ...] = (
    "..",
    "../evil",
    "a/b",
    "a\\b",
    'a" & calc & "',
    "",
    "x" * 65,
)

WIDTH_CASES: tuple[tuple[tuple[int, ...], int], ...] = (
    ((0, 19), 2),
    ((0, 99), 2),
    ((0, 100), 3),
    ((7,), 2),
    ((), 2),
    ((0, 1234), 4),
)

NORMALIZE_CASES: tuple[tuple[str, str], ...] = (
    ("(huge:1.3)", "huge"),
    ("((tag))", "tag"),
    ("[soft]", "soft"),
    (" Bad   Hands ", "bad hands"),
    ("(masterpiece:1.2)", "masterpiece"),
    ("plain", "plain"),
    ("(weight:-0.5)", "weight"),
)

CONFLICT_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("1girl, solo, smile", "1boy, male", ()),
    ("1girl, (breasts:1.2)", "breasts, muscular", ("breasts",)),
    ("a, b, c", "C, B", ("b", "c")),
    ("", "anything", ()),
)

VRAM_CASES: tuple[tuple[str, dict[str, Any], bool], ...] = (
    (
        "최상위 reserved_peak",
        {"cuda": {"system": {"total": 8 * config.GIB}, "reserved_peak": 6 * config.GIB}},
        True,
    ),
    (
        "중첩 reserved.peak",
        {"cuda": {"system": {"total": 8 * config.GIB}, "reserved": {"peak": 5 * config.GIB}}},
        True,
    ),
    (
        "active_peak 폴백",
        {"cuda": {"system": {"total": 8 * config.GIB}, "active_peak": 4 * config.GIB}},
        True,
    ),
    ("cuda 없음", {"ram": {}}, False),
    ("total 없음", {"cuda": {"reserved_peak": 1}}, False),
    ("peak 키 전무", {"cuda": {"system": {"total": 8 * config.GIB}}}, False),
    ("total 0", {"cuda": {"system": {"total": 0}, "reserved_peak": 1}}, False),
)

CN_MODEL_FIXTURE: tuple[str, ...] = (
    "control_v11p_sd15_openpose [cab727d4]",
    "ip-adapter_xl [4209e9f7]",
    "t2iadapter_style_sd14v1 [202e85cc]",
)
CN_MODULE_FIXTURE: tuple[str, ...] = (
    "none",
    "canny",
    "openpose_full",
    "ip-adapter_clip_sdxl",
)

REF_WEIGHT_REJECT: tuple[float, ...] = (-0.1, 2.1, -1.0, 99.0)
REF_WEIGHT_ACCEPT: tuple[float, ...] = (0.0, 0.5, 0.7, 1.0, 2.0)


@contextmanager
def temp_workspace(
    extensions: Sequence[str] = (), prefix: str = "t"
) -> Iterator[Path]:
    """
    임시 작업 폴더를 만들고 정리한다.

    저장소에 테스트용 바이너리를 커밋하지 않기 위해 Pillow 로 즉석
    생성한다. contextmanager 를 쓰면 검사 실패로 예외가 나도 정리된다.
    """
    root = Path(tempfile.mkdtemp(prefix="charaset_test_"))
    try:
        if extensions:
            refs = root / config.REFERENCES_DIRNAME
            refs.mkdir(parents=True, exist_ok=True)
            for ext in extensions:
                Image.new("RGB", (64, 96), (128, 128, 200)).save(refs / f"{prefix}{ext}")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ─────────────────────────────────────────────
# 데이터 검사 (T1~T7)
# ─────────────────────────────────────────────
def _check_data(report: Report, base_dir: Path) -> PoseDatabase | None:
    emit("\n[데이터 검사]")
    db_path = base_dir / config.POSE_DB_FILENAME

    if not report.check("T1", "JSON 파일 존재", db_path.is_file(), str(db_path)):
        return None

    try:
        raw = read_pose_json(base_dir)
    except DatabaseError as exc:
        report.check("T2", "JSON 로드", False, f"{exc.message} {exc.hint}".strip())
        return None
    report.ok("T2", "JSON 문법")
    report.ok("T3", "최상위 섹션 딕셔너리")

    non_dict = [
        name for name, body in iter_pose_sections(raw) if not isinstance(body, dict)
    ]
    if non_dict:
        report.warn("T3b", "비-딕셔너리 섹션", str(non_dict))

    _audit_quality(report, raw)

    database = parse_pose_database(raw)
    if not report.check(
        "T7", "유효 엔트리 1개 이상", bool(database.entries), f"{len(database.entries)}개"
    ):
        return None
    return database


def _audit_quality(report: Report, raw: dict[str, Any]) -> None:
    """
    T4~T6: 데이터 품질.

    런타임과 동일하게 경고로만 처리한다. 50개 항목 중 오타 하나로 종료
    코드가 1이 되면 CI 게이트로 쓰기 불편하고, 런타임은 이미 경고 후
    계속 진행하도록 설계했다.
    """
    bad_keys: list[str] = []
    empty_values: list[str] = []
    duplicates: list[str] = []
    seen: dict[int, str] = {}

    for section, body in iter_pose_sections(raw):
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            try:
                code = int(key)
            except (TypeError, ValueError):
                bad_keys.append(f"{section}/{key}")
                continue
            if not isinstance(value, str) or not value.strip():
                empty_values.append(f"{section}/{key}")
                continue
            if code in seen:
                duplicates.append(f"{code}({seen[code]}->{section})")
            seen[code] = section

    for check_id, name, findings in (
        ("T4", "비정수 키", bad_keys),
        ("T5", "빈 프롬프트", empty_values),
        ("T6", "중복 코드", duplicates),
    ):
        if findings:
            report.warn(check_id, name, str(findings))
        else:
            report.ok(check_id, f"{name} 없음")


# ─────────────────────────────────────────────
# 로직 검사 (T8~T17)
# ─────────────────────────────────────────────
def _check_logic(report: Report, database: PoseDatabase) -> None:
    emit("\n[로직 검사]")
    synthetic = parse_pose_database(SYNTHETIC_DB)

    # T8 — 문자열 사전순으로 정렬하면 "10" < "2" 가 되어 순서가 깨진다.
    lexicographic = [int(key) for key in sorted(["5", "12", "03"])]
    report.check(
        "T8",
        "정수 정렬 (사전순 아님)",
        synthetic.all_codes == (3, 5, 7, 12) and lexicographic != [3, 5, 12],
        f"{list(synthetic.all_codes)}, 사전순={lexicographic}",
    )
    report.check(
        "T8b",
        "실제 DB 정렬",
        list(database.all_codes) == sorted(database.all_codes),
        str(list(database.all_codes)),
    )

    # T9
    width_ok = all(
        CodeFormatter.for_codes(codes).width == expected
        for codes, expected in WIDTH_CASES
    )
    report.check(
        "T9",
        "CodeFormatter 패딩 폭",
        width_ok,
        str([(list(c), CodeFormatter.for_codes(c).width) for c, _ in WIDTH_CASES]),
    )

    # T10
    parser_failures: list[str] = []
    for expression, expected in PARSER_CASES:
        try:
            actual = parse_code_expression(expression)
            if actual != expected:
                parser_failures.append(f"'{expression}'->{actual}!={expected}")
        except Exception as exc:  # noqa: BLE001
            parser_failures.append(f"'{expression}' raised {exc}")
    report.check(
        "T10",
        f"parse_code_expression {len(PARSER_CASES)}케이스",
        not parser_failures,
        str(parser_failures) if parser_failures else "",
    )

    report.check(
        "T10b",
        "코드 표현식 판별",
        all(map(looks_like_code_expression, ("0,5,12", "10-14", "3")))
        and not any(map(looks_like_code_expression, ("emotions", "all", ""))),
    )

    # T11
    formatter = CodeFormatter(2)
    wide = CodeFormatter(3)
    report.check(
        "T11",
        "파일명 조립",
        formatter.filename("x", 7) == "x_07.webp"
        and wide.filename("x", 7) == "x_007.webp"
        and wide.filename("x", 123) == "x_123.webp"
        and formatter.trigger("x", 7) == "x_07",
        formatter.filename("x", 7),
    )

    # T12 / T13
    codes = synthetic.all_codes
    synthetic_formatter = CodeFormatter.for_codes(codes)
    block = build_genit_block(
        prefix="t",
        codes=codes,
        database=synthetic,
        formatter=synthetic_formatter,
    )
    calls = block.count("![image](")
    report.check(
        "T12",
        "마크다운 라인 수 == 대상 수",
        calls == len(codes),
        f"{calls}/{len(codes)}",
    )
    report.check("T13", "{{url}} 리터럴 포함", config.URL_PLACEHOLDER in block)

    # T14 — prefix 화이트리스트
    wrongly_allowed = []
    for unsafe in UNSAFE_PREFIXES:
        try:
            validate_prefix(unsafe)
            wrongly_allowed.append(unsafe)
        except ValidationError:
            pass
    report.check(
        "T14",
        "위험 prefix 차단",
        not wrongly_allowed,
        f"통과됨: {wrongly_allowed}"
        if wrongly_allowed
        else f"{len(UNSAFE_PREFIXES)}종 차단",
    )
    report.check(
        "T14b",
        "정상 prefix 허용",
        validate_prefix(" mika ") == "mika"
        and validate_prefix("test_01") == "test_01"
        and validate_prefix("rin-a") == "rin-a",
    )

    # T15
    wrongly_accepted = []
    for expression in REJECT_EXPRESSIONS:
        try:
            parse_code_expression(expression)
            wrongly_accepted.append(expression)
        except ValueError:
            pass
    report.check(
        "T15",
        "잘못된 표현식 거부",
        not wrongly_accepted,
        f"통과됨: {wrongly_accepted}"
        if wrongly_accepted
        else f"{len(REJECT_EXPRESSIONS)}종 거부",
    )

    # T16
    bad_normalize = [
        f"'{src}'->'{normalize_tag(src)}'!='{expected}'"
        for src, expected in NORMALIZE_CASES
        if normalize_tag(src) != expected
    ]
    report.check(
        "T16",
        f"normalize_tag {len(NORMALIZE_CASES)}케이스",
        not bad_normalize,
        str(bad_normalize) if bad_normalize else "",
    )

    # T17
    bad_conflict = [
        f"({pos!r},{neg!r})->{find_duplicate_tags(pos, neg)}!={expected}"
        for pos, neg, expected in CONFLICT_CASES
        if find_duplicate_tags(pos, neg) != expected
    ]
    report.check(
        "T17",
        f"find_duplicate_tags {len(CONFLICT_CASES)}케이스",
        not bad_conflict,
        str(bad_conflict) if bad_conflict else "",
    )

    # T17b — 의미적 상충 (같은 문자열이 아니라 검출되지 않는 영역)
    exclusive = find_exclusive_conflicts("1girl, 1boy, smile")
    report.check(
        "T17b",
        "상호배타 태그 검출",
        exclusive == (("1boy", "1girl"),),
        str(exclusive),
    )
    report.check(
        "T17c",
        "상호배타 규칙 JSON 파싱",
        parse_exclusive_groups([["a", "b"], ["solo"], "bad", [1, 2]])
        == (frozenset({"a", "b"}),),
    )


# ─────────────────────────────────────────────
# 프로필 검사 (T18~T20)
# ─────────────────────────────────────────────
def _check_profiles(report: Report, database: PoseDatabase) -> None:
    emit("\n[프로필 검사]")

    if not database.profiles:
        report.warn(
            "T18",
            f"'{config.PROFILES_KEY}' 섹션 없음",
            "내장 기본값으로 폴백합니다. 성별 전환이 필요하면 추가하세요",
        )
        builtin = Profile(
            config.BUILTIN_PROFILE_NAME,
            config.FALLBACK_POSITIVE,
            config.FALLBACK_NEGATIVE,
        )
        issues = audit_profile(builtin, database.exclusive_groups)
        if issues:
            for message in issues:
                report.warn("T19", "내장 기본값", message)
        else:
            report.ok("T19", "내장 기본값 충돌 없음")
        return

    report.ok(
        "T18", f"프로필 {len(database.profiles)}종 로드", str(list(database.profile_names))
    )

    found_issue = False
    for name, profile in database.profiles.items():
        for message in audit_profile(profile, database.exclusive_groups):
            found_issue = True
            report.warn("T19", f"프로필 '{name}'", message)
    if not found_issue:
        report.ok(
            "T19", "프로필 충돌 없음", f"{len(database.profiles)}종 검사"
        )

    if config.DEFAULT_PROFILE_NAME in database.profiles:
        report.ok("T20", f"기본 프로필 '{config.DEFAULT_PROFILE_NAME}' 존재")
    else:
        fallback = next(iter(database.profiles))
        report.warn(
            "T20",
            f"기본 프로필 '{config.DEFAULT_PROFILE_NAME}' 없음",
            f"--profile 생략 시 '{fallback}' 이 쓰입니다",
        )

    # T20b — 프로필 해석 경로
    resolved = resolve_profile(database, None)
    report.check(
        "T20b", "프로필 해석 (생략 시)", resolved.name in database.profiles, resolved.name
    )
    try:
        resolve_profile(database, "__nonexistent__")
        report.check("T20c", "미등록 프로필 거부", False, "예외가 발생하지 않음")
    except ValidationError:
        report.ok("T20c", "미등록 프로필 거부")


# ─────────────────────────────────────────────
# 참조 이미지 및 페이로드 검사 (T21~T32)
# ─────────────────────────────────────────────
def _check_reference(report: Report) -> None:
    emit("\n[참조 이미지 검사]")

    # T21
    with temp_workspace(config.REFERENCE_EXTENSIONS) as root:
        found = find_reference_candidates(root, "t")
        report.check(
            "T21",
            "확장자 우선순위",
            len(found) == len(config.REFERENCE_EXTENSIONS)
            and found[0].suffix == ".png",
            str([path.suffix for path in found]),
        )
        picked = resolve_reference_image(root, "t")
        report.check(
            "T21b",
            ".png 채택",
            picked is not None and picked.path.suffix == ".png",
            picked.path.name if picked else "None",
        )

    # T22
    with temp_workspace() as root:
        try:
            missing = resolve_reference_image(root, "nosuch")
            report.check("T22", "참조 부재 시 None", missing is None, repr(missing))
        except Exception as exc:  # noqa: BLE001
            report.check("T22", "참조 부재 시 None", False, f"예외 발생: {exc}")
        report.check(
            "T22b",
            "references/ 폴더 자체 부재",
            resolve_reference_image(root, "t") is None,
        )
        # T22c — 명시 지정 실패는 자동 탐색과 다르게 예외여야 한다
        try:
            resolve_reference_image(root, "t", explicit_path="nosuch.png")
            report.check("T22c", "--ref_image 부재 시 예외", False, "예외 없음")
        except ConfigError:
            report.ok("T22c", "--ref_image 부재 시 예외")

    # T23 ~ T26
    with temp_workspace((".png",)) as root:
        reference = resolve_reference_image(root, "t")
        assert reference is not None

        decoded = base64.b64decode(reference.b64)
        with Image.open(io.BytesIO(decoded)) as image:
            size_ok = image.size == (reference.width, reference.height) == (64, 96)
        report.check(
            "T23",
            "base64 왕복",
            size_ok,
            f"{reference.width}x{reference.height}",
        )

        emit("\n[페이로드 조립 검사]")
        spec = ControlNetSpec("ip-adapter_clip_sdxl", "ip-adapter_xl [test]", "manual")
        base_payload = payload.build_txt2img_payload(
            positive="p", negative="n", sampler="s"
        )
        report.check(
            "T24pre",
            "txt2img 상수 일치",
            base_payload["width"] == config.IMAGE_WIDTH
            and base_payload["height"] == config.IMAGE_HEIGHT
            and base_payload["steps"] == config.STEPS
            and base_payload["cfg_scale"] == config.CFG_SCALE
            and base_payload["batch_size"] == 1
            and base_payload["n_iter"] == 1,
            f"{base_payload['width']}x{base_payload['height']} steps={base_payload['steps']}",
        )

        unit = payload.build_controlnet_unit(reference, spec, 0.7)
        missing_keys = payload.CONTROLNET_REQUIRED_KEYS - unit.keys()
        report.check(
            "T24",
            "ControlNet 유닛 필수 키",
            not missing_keys and unit["enabled"] is True and unit["weight"] == 0.7,
            f"누락: {sorted(missing_keys)}" if missing_keys else f"{len(unit)}개 키",
        )
        report.check(
            "T24b",
            "유닛에 base64 이미지 포함",
            unit["input_image"] == reference.b64 and bool(unit["input_image"]),
        )

        report.check(
            "T25",
            "참조 없을 때 alwayson_scripts 미주입",
            payload.ALWAYSON_KEY not in base_payload,
            f"키 {len(base_payload)}개",
        )
        inactive = payload.build_generation_payload(
            positive="p",
            negative="n",
            sampler="s",
            reference=ReferenceContext(image=reference, spec=None),
        )
        report.check(
            "T25b",
            "spec 없으면 미주입",
            payload.ALWAYSON_KEY not in inactive,
        )

        injected = payload.inject_controlnet(base_payload, unit)
        try:
            args_list = injected[payload.ALWAYSON_KEY][payload.CONTROLNET_KEY]["args"]
            placed = len(args_list) == 1 and args_list[0] is unit
        except (KeyError, TypeError):
            placed = False
        report.check("T26", "주입 위치", placed)
        report.check(
            "T26b",
            "원본 페이로드 불변",
            payload.ALWAYSON_KEY not in base_payload,
            "inject_controlnet 이 원본을 변경하지 않음",
        )
        report.check(
            "T26c",
            "기존 키 보존",
            all(injected[key] == value for key, value in base_payload.items()),
        )

        active = payload.build_generation_payload(
            positive="p",
            negative="n",
            sampler="s",
            reference=ReferenceContext(image=reference, spec=spec, weight=0.42),
        )
        report.check(
            "T26d",
            "활성 시 weight 전달",
            active[payload.ALWAYSON_KEY][payload.CONTROLNET_KEY]["args"][0]["weight"]
            == 0.42,
        )

    emit("\n[검증 로직 검사]")

    # T27
    wrongly_accepted = []
    for value in REF_WEIGHT_REJECT:
        try:
            validate_ref_weight(value)
            wrongly_accepted.append(value)
        except ValidationError:
            pass
    wrongly_rejected = []
    for value in REF_WEIGHT_ACCEPT:
        try:
            validate_ref_weight(value)
        except ValidationError:
            wrongly_rejected.append(value)
    report.check(
        "T27",
        "ref_weight 범위",
        not wrongly_accepted and not wrongly_rejected,
        f"오통과 {wrongly_accepted} / 오거부 {wrongly_rejected}"
        if (wrongly_accepted or wrongly_rejected)
        else f"거부 {len(REF_WEIGHT_REJECT)}종 / 허용 {len(REF_WEIGHT_ACCEPT)}종",
    )
    try:
        validate_interrogator("bogus")
        report.check("T27b", "interrogator 검증", False, "예외 없음")
    except ValidationError:
        report.check(
            "T27b",
            "interrogator 검증",
            validate_interrogator(config.INTERROGATOR_DEFAULT)
            == config.INTERROGATOR_DEFAULT,
        )

    # T28
    interrogate_payload = payload.build_interrogate_payload(
        "BASE64", config.INTERROGATOR_DEFAULT
    )
    report.check(
        "T28",
        "interrogate 페이로드",
        set(interrogate_payload) == {"image", "model"}
        and interrogate_payload["model"] == config.INTERROGATOR_DEFAULT
        and interrogate_payload["image"] == "BASE64",
        str(interrogate_payload | {"image": "..."}),
    )

    # T29
    sample = ("1girl", "solo", "silver hair", "blue eyes", "1boy", "MALE")
    kept, removed = partition_gender_tags(sample)
    report.check(
        "T29",
        "성별 태그 필터",
        kept == ("silver hair", "blue eyes") and len(removed) == 4,
        f"유지 {list(kept)} / 제거 {list(removed)}",
    )
    result = InterrogateResult(
        raw=", ".join(sample), tags=sample, gender_tags=removed
    )
    report.check(
        "T29b",
        "filtered 프로퍼티",
        result.filtered == "silver hair, blue eyes",
        result.filtered,
    )
    weighted_kept, weighted_removed = partition_gender_tags(
        ("(1girl:1.2)", "silver hair")
    )
    report.check(
        "T29c",
        "가중치 표기 성별 태그",
        weighted_removed == ("(1girl:1.2)",) and weighted_kept == ("silver hair",),
    )

    # T30
    matched_model = payload.match_pattern(
        CN_MODEL_FIXTURE, config.IP_ADAPTER_MODEL_PATTERNS
    )
    matched_module = payload.match_pattern(
        CN_MODULE_FIXTURE, config.IP_ADAPTER_MODULE_PATTERNS
    )
    report.check(
        "T30",
        "해시 포함 모델명 매칭",
        matched_model == "ip-adapter_xl [4209e9f7]"
        and matched_module == "ip-adapter_clip_sdxl",
        f"{matched_model} / {matched_module}",
    )
    report.check(
        "T30b",
        "매칭 실패 시 None",
        payload.match_pattern(("canny", "openpose"), config.IP_ADAPTER_MODEL_PATTERNS)
        is None,
    )
    report.check(
        "T30c",
        "대소문자 무시 매칭",
        payload.match_pattern(["IP-Adapter_XL [ABC]"], ("ip-adapter",))
        == "IP-Adapter_XL [ABC]",
    )

    # T31
    stats = summarize_durations([2.0, 4.0, 6.0])
    report.check(
        "T31",
        "시간 집계",
        stats is not None
        and stats.count == 3
        and stats.total == 12.0
        and stats.average == 4.0
        and stats.fastest == 2.0
        and stats.slowest == 6.0,
        stats.format() if stats else "None",
    )
    report.check("T31b", "빈 측정값 None", summarize_durations([]) is None)

    # T32
    vram_failures = [
        name
        for name, body, expected in VRAM_CASES
        if (payload.extract_vram_peak(body) is not None) != expected
    ]
    report.check(
        "T32",
        f"VRAM 파싱 {len(VRAM_CASES)}케이스",
        not vram_failures,
        str(vram_failures) if vram_failures else "",
    )
    parsed = payload.extract_vram_peak(
        {"cuda": {"system": {"total": 8 * config.GIB}, "reserved_peak": 6 * config.GIB}}
    )
    report.check(
        "T32b",
        "GiB 환산",
        parsed is not None
        and abs(parsed[0] - 6.0) < 0.01
        and abs(parsed[1] - 8.0) < 0.01,
        f"{parsed[0]:.2f} / {parsed[1]:.2f} GiB" if parsed else "None",
    )


# ─────────────────────────────────────────────
# 통합 경로 검사 (T33~T35)
# ─────────────────────────────────────────────
def _check_integration(report: Report, database: PoseDatabase) -> None:
    emit("\n[통합 경로 검사]")

    # T33 — 코드 선택
    warnings: list[str] = []
    selection = resolve_codes(
        available=dict(database.entries),
        section_map=dict(database.sections),
        mode="all",
        on_warning=warnings.append,
    )
    report.check(
        "T33",
        "mode=all 선택",
        len(selection) == len(database.entries),
        f"{len(selection)}개",
    )

    if database.section_names:
        first = database.section_names[0]
        section_selection = resolve_codes(
            available=dict(database.entries),
            section_map=dict(database.sections),
            mode=first,
        )
        report.check(
            "T33b",
            f"mode=섹션명({first})",
            tuple(section_selection.codes) == database.sections[first],
            f"{len(section_selection)}개",
        )

    expr_selection = resolve_codes(
        available=dict(database.entries),
        section_map=dict(database.sections),
        mode="0,1,999",
        on_warning=warnings.append,
    )
    report.check(
        "T33c",
        "mode=코드표현식 + 미존재 경고",
        len(expr_selection) <= 2 and any("999" in message for message in warnings),
        f"선택 {len(expr_selection)}개, 경고 {len(warnings)}건",
    )

    try:
        resolve_codes(
            available=dict(database.entries),
            section_map=dict(database.sections),
            mode="__nosuch__",
        )
        report.check("T33d", "미등록 모드 거부", False, "예외 없음")
    except ValidationError:
        report.ok("T33d", "미등록 모드 거부")

    # T34 — 프롬프트 조립 순서
    from .prompt import PromptComposer

    profile = resolve_profile(database, None)
    composer = PromptComposer(
        profile=profile, char_prompt="silver hair", custom_negative="glasses"
    )
    first_code = database.all_codes[0]
    formatter = CodeFormatter.for_codes(database.all_codes)
    pair = composer.compose(database.entry(first_code), "mika", formatter)
    trigger = formatter.trigger("mika", first_code)
    report.check(
        "T34",
        "프롬프트 조립 순서",
        pair.positive.startswith(profile.base_positive)
        and "silver hair" in pair.positive
        and pair.positive.endswith(trigger),
        f"...{pair.positive[-40:]}",
    )
    report.check(
        "T34b",
        "custom_neg 는 추가(대체 아님)",
        pair.negative.endswith("glasses")
        and profile.base_negative.split(",")[0].strip() in pair.negative,
    )

    # T35 — 파일명·경로 정책
    from .storage import AssetPaths

    with temp_workspace() as root:
        real_paths = AssetPaths(root, "mika", is_mock=False)
        mock_paths = AssetPaths(root, "mika", is_mock=True)
        report.check(
            "T35",
            "mock 출력 경로 격리",
            real_paths.output_dir != mock_paths.output_dir
            and config.MOCK_ASSETS_DIRNAME in str(mock_paths.output_dir)
            and config.ASSETS_DIRNAME in str(real_paths.output_dir),
            f"{real_paths.output_dir.parent.name} vs {mock_paths.output_dir.parent.name}",
        )

        # T35b — 실제 폴더에 mock 매니페스트가 있으면 중단해야 한다
        from .storage import guard_real_output

        real_paths.ensure()
        real_paths.manifest_path.write_text("{}", encoding="utf-8")
        try:
            guard_real_output(real_paths)
            report.check("T35b", "mock 오염 감지", False, "감지하지 못함")
        except ConfigError:
            report.ok("T35b", "mock 오염 감지")

    # T36 — 원자적 쓰기
    from .storage import AtomicImageWriter

    with temp_workspace() as root:
        target_dir = root / "out"
        target_dir.mkdir()
        destination = target_dir / "atomic_test.webp"

        buffer = io.BytesIO()
        Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(buffer, format="PNG")

        writer = AtomicImageWriter()
        writer.write(buffer.getvalue(), destination)

        header = destination.read_bytes()[:12]
        leftovers = list(target_dir.glob(f"*{config.PARTIAL_SUFFIX}"))
        report.check(
            "T36",
            "원자적 쓰기 + WebP 변환",
            destination.is_file()
            and header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
            and not leftovers,
            f"RIFF/WEBP 확인, .part 잔여 {len(leftovers)}건",
        )

        try:
            writer.write(b"not an image", target_dir / "bad.webp")
            report.check("T36b", "손상 입력 거부", False, "예외 없음")
        except Exception:  # noqa: BLE001 — StorageError 를 기대하지만 종류 무관
            report.check(
                "T36b",
                "손상 입력 거부",
                not (target_dir / "bad.webp").exists()
                and not list(target_dir.glob(f"*{config.PARTIAL_SUFFIX}")),
                "부분 파일 미잔존",
            )


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────
def run_diagnostics(base_dir: Path) -> int:
    """
    자체 진단을 실행한다.

    파일 쓰기는 임시 폴더에만 하고, 네트워크 요청은 전혀 하지 않는다.

    Returns:
        종료 코드. FAIL 이 하나라도 있으면 1.
    """
    report = Report()

    emit(f"\n{config.SEPARATOR}")
    emit("  자체 진단 (--test)")
    emit(config.SEPARATOR)

    database = _check_data(report, base_dir)
    if database is None:
        return _finish(report)

    _check_logic(report, database)
    _check_profiles(report, database)
    _check_reference(report)
    _check_integration(report, database)
    return _finish(report)


def _finish(report: Report) -> int:
    emit(f"\n{config.SEPARATOR}")
    emit(
        f"  결과: PASS {report.passed} / FAIL {report.failed} / WARN {report.warned}"
    )
    emit(f"  종료 코드: {report.exit_code}")
    emit("  (페이로드 구조 검사이며 WebUI 수락 여부는 GPU 환경에서 확인해야 합니다)")
    emit(f"{config.SEPARATOR}\n")
    return report.exit_code

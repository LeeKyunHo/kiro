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
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from . import config, exporter, payload
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
    CharacterPreset,
    CheckOutcome,
    ControlNetSpec,
    InterrogateResult,
    PoseDatabase,
    Profile,
    ReferenceContext,
    summarize_durations,
)
from .output import build_genit_block
from .roster import (
    audit_preset,
    load_roster,
    merge_character,
    parse_roster,
    resolve_preset,
)
from .storage import (
    AssetPaths,
    OutputKind,
    find_reference_candidates,
    resolve_reference_image,
)
from .tags import (
    find_duplicate_tags,
    find_exclusive_conflicts,
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

# characters.json 파싱 픽스처. 유효 항목 2개와 무효 항목 5개를 섞어
# "한 명의 오류가 나머지를 막지 않는다" 를 검증한다.
SYNTHETIC_ROSTER: dict[str, Any] = {
    "_schema": {"note": "메타 키는 캐릭터로 세지 않는다"},
    "good": {
        "char_prompt": "silver hair, blue eyes",
        "profile": "female",
        "custom_neg": "glasses",
        "ref_weight": 0.55,
        "mode": "emotions",
        "note": "정상 항목",
    },
    "minimal": {"char_prompt": "short black hair"},
    "미카": {"char_prompt": "한글 이름은 prefix 규격 위반"},
    "no_prompt": {"profile": "female"},
    "empty_prompt": {"char_prompt": "   "},
    "not_a_dict": "문자열은 캐릭터가 될 수 없다",
    "bad_weight": {"char_prompt": "tag", "ref_weight": True},
    "typo_field": {"char_prompt": "tag", "char_promt": "오타 필드"},
}

# 카드 파일명 역파싱 픽스처. (파일 stem, 기대 코드 또는 None)
CARD_SCAN_FIXTURE: tuple[tuple[str, int | None], ...] = (
    ("mika_00", 0),
    ("mika_07", 7),
    ("mika_123", 123),
    ("mika_007", 7),          # 패딩 폭이 늘어난 뒤 남은 과거 파일
    ("other_03", None),       # 다른 접두어
    ("mika", None),           # 코드 없음
    ("mika_ab", None),        # 숫자 아님
    ("mika_00_extra", None),  # 코드가 말미가 아님
)


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
    with temp_workspace() as root:
        real_paths = AssetPaths(root, "mika", kind=OutputKind.REAL)
        mock_paths = AssetPaths(root, "mika", kind=OutputKind.MOCK)
        bench_paths = AssetPaths(
            root, "mika", kind=OutputKind.BENCHMARK, variant="w0.70"
        )
        report.check(
            "T35",
            "출력 경로 3종 격리",
            len({
                real_paths.output_dir,
                mock_paths.output_dir,
                bench_paths.output_dir,
            }) == 3
            and config.MOCK_ASSETS_DIRNAME in str(mock_paths.output_dir)
            and config.ASSETS_DIRNAME in str(real_paths.output_dir)
            and config.BENCHMARK_ASSETS_DIRNAME in str(bench_paths.output_dir),
            f"real/{real_paths.output_dir.parent.name} "
            f"mock/{mock_paths.output_dir.parent.name} "
            f"bench/{bench_paths.output_dir.name}",
        )
        report.check(
            "T35c",
            "variant 가 접두어를 바꾸지 않음",
            bench_paths.prefix == real_paths.prefix == "mika"
            and bench_paths.output_dir.parent.name == "mika",
            "트리거 태그가 동일하게 유지됨",
        )
        report.check(
            "T35d",
            "with_variant 사본",
            bench_paths.with_variant("w0.30").output_dir.name == "w0.30"
            and bench_paths.output_dir.name == "w0.70",
            "원본 불변",
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
# 캐릭터 프리셋 검사 (T37~T39)
# ─────────────────────────────────────────────
def _check_roster(report: Report, base_dir: Path, database: PoseDatabase) -> None:
    emit("\n[캐릭터 프리셋 검사]")

    # T37 — 합성 픽스처 파싱. 실제 파일 상태와 무관하게 항상 검사된다.
    #
    # char_prompt 가 유효하면 항목은 채택된다. ref_weight 오류나 오타 필드는
    # 경고로 남기고 해당 축만 버린다. 캐릭터 하나를 통째로 못 쓰게 만드는
    # 것보다 나머지 축을 살리는 편이 낫다.
    synthetic = parse_roster(SYNTHETIC_ROSTER)
    report.check(
        "T37",
        "프리셋 파싱 (채택 4 / 배제 4)",
        set(synthetic.names) == {"good", "minimal", "bad_weight", "typo_field"},
        f"채택 {list(synthetic.names)} / 경고 {len(synthetic.warnings)}건",
    )
    report.check(
        "T37a",
        "char_prompt 결손 항목만 배제",
        all(
            name not in synthetic
            for name in ("no_prompt", "empty_prompt", "not_a_dict", "미카")
        ),
        "프롬프트 부재·빈 값·타입 불일치·이름 규격 위반",
    )
    report.check(
        "T37b",
        "메타 키·규격 위반 이름 배제",
        "_schema" not in synthetic
        and "미카" not in synthetic
        and any("미카" in message for message in synthetic.warnings),
        "'_' 접두 키는 건너뛰고 한글 이름은 경고 후 무시",
    )
    report.check(
        "T37c",
        "필드 값 정규화",
        synthetic.entries["good"].ref_weight == 0.55
        and synthetic.entries["good"].mode == "emotions"
        and synthetic.entries["minimal"].profile is None
        and synthetic.entries["minimal"].custom_neg is None,
        "미지정 축은 None 으로 남아 병합에서 기본값이 적용됨",
    )
    report.check(
        "T37d",
        "bool ref_weight 거부 · 오타 필드 경고",
        synthetic.entries["bad_weight"].ref_weight is None
        and any("bad_weight" in message for message in synthetic.warnings)
        and any("char_promt" in message for message in synthetic.warnings),
        "isinstance(True, int) 로 1.0 이 되는 것을 막고 오타를 알린다. "
        "항목 자체는 살리고 해당 축만 버린다",
    )
    report.check(
        "T37e",
        "파일 부재와 빈 로스터 구분",
        parse_roster(None).available is False
        and parse_roster({}).available is True,
        "선택 기능의 부재는 정상, 항목 0개는 편집 실수",
    )

    # T38 — 병합 우선순위
    preset = synthetic.entries["good"]
    plain = merge_character(preset)
    report.check(
        "T38",
        "프리셋 값 적용 (CLI 미지정)",
        plain.prefix == "good"
        and plain.mode == "emotions"
        and plain.ref_weight == 0.55
        and plain.custom_neg == "glasses"
        and plain.overridden == (),
        f"mode={plain.mode} weight={plain.ref_weight} source={plain.source}",
    )

    overridden = merge_character(
        preset, prefix="good_v2", mode="all", ref_weight=0.9
    )
    report.check(
        "T38b",
        "CLI 가 프리셋을 덮어씀",
        overridden.mode == "all"
        and overridden.ref_weight == 0.9
        and overridden.prefix == "good_v2"
        and set(overridden.overridden) == {"mode", "ref_weight"},
        f"덮인 축 {list(overridden.overridden)}",
    )
    report.check(
        "T38c",
        "프리셋 미정의 축 지정은 오버라이드 아님",
        merge_character(
            synthetic.entries["minimal"], mode="poses"
        ).overridden == (),
        "minimal 은 mode 를 정하지 않았으므로 지정일 뿐 덮어쓴 것이 아니다",
    )

    empty_neg = merge_character(preset, custom_neg="")
    report.check(
        "T38d",
        '--custom_neg "" 로 프리셋 비우기',
        empty_neg.custom_neg == ""
        and empty_neg.overridden == ("custom_neg",),
        "빈 문자열이 argparse 기본값이면 표현할 수 없는 의도",
    )

    bare = merge_character(None)
    report.check(
        "T38e",
        "프리셋 없을 때 기본값만 채움",
        bare.mode == config.MODE_DEFAULT
        and bare.ref_weight == config.REF_WEIGHT_DEFAULT
        and bare.custom_neg == ""
        and bare.prefix is None
        and bare.char_prompt is None
        and bare.source == "cli",
        "prefix/char_prompt 는 None 을 유지해 argparse 필수 검사에 맡긴다",
    )

    # T39 — 조회 실패 경로
    failures: list[str] = []
    for label, roster_case, name in (
        ("파일 부재", parse_roster(None), "any"),
        ("항목 0개", parse_roster({}), "any"),
        ("미등록 이름", synthetic, "__nosuch__"),
    ):
        try:
            resolve_preset(roster_case, name)
            failures.append(label)
        except ValidationError:
            pass
    report.check(
        "T39",
        "프리셋 조회 실패 3종 거부",
        not failures,
        f"통과됨: {failures}" if failures else "각각 다른 안내 메시지",
    )

    # T39b — 실제 characters.json
    try:
        actual = load_roster(base_dir)
    except DatabaseError as exc:
        report.check(
            "T39b",
            f"{config.CHARACTERS_FILENAME} 로드",
            False,
            f"{exc.message} {exc.hint}".strip(),
        )
        return

    if not actual.available:
        report.warn(
            "T39b",
            f"{config.CHARACTERS_FILENAME} 없음",
            "--char 를 쓰려면 만드세요. 없어도 기존 명령은 정상 동작합니다",
        )
        return

    report.ok(
        "T39b",
        f"프리셋 {len(actual)}종 로드",
        str(list(actual.names)),
    )
    for message in actual.warnings:
        report.warn("T39c", f"{config.CHARACTERS_FILENAME} 경고", message)

    found_issue = False
    for name, entry in actual.entries.items():
        for message in audit_preset(
            entry,
            profile_names=database.profile_names,
            section_names=database.section_names,
        ):
            found_issue = True
            report.warn("T39d", f"캐릭터 '{name}'", message)
    if not found_issue:
        report.ok(
            "T39d",
            "프리셋 정합성",
            f"{len(actual)}종 검사 (프로필 참조·성별 태그·mode 유효성)",
        )


# ─────────────────────────────────────────────
# 젠잇 카드 검사 (T40~T42)
# ─────────────────────────────────────────────
def _card_meta(prefix: str = "t") -> exporter.CardMeta:
    return exporter.CardMeta(
        prefix=prefix,
        profile_name="female",
        char_prompt="silver hair",
        negative="worst quality",
        command="charaset --char t",
        kind_label="자체 진단",
    )


def _check_card(report: Report, database: PoseDatabase) -> None:
    emit("\n[젠잇 카드 검사]")

    synthetic = parse_pose_database(SYNTHETIC_DB)
    codes = synthetic.all_codes
    formatter = CodeFormatter.for_codes(codes)

    # 메타를 한 번만 만들어 재사용한다. 매번 새로 만들면 created_at 이
    # 분 경계에서 달라져 T42 의 문자열 비교가 간헐적으로 실패한다.
    meta = _card_meta()
    card = exporter.build_card(
        meta=meta,
        codes=codes,
        database=synthetic,
        formatter=formatter,
    )

    # T40 — 조립 불변식
    calls = card.count("![image](")
    report.check(
        "T40",
        "카드 호출 라인 수 == 대상 수",
        calls == len(codes),
        f"{calls}/{len(codes)}",
    )
    report.check(
        "T40b",
        "세 블록 모두 포함",
        "이미지 호출 코드" in card
        and "상태 매핑 가이드" in card
        and "상태창 템플릿" in card
        and config.GENIT_STATUS_TEMPLATE.split("|")[0] in card,
        "호출 코드 / 매핑 가이드 / 상태창",
    )
    report.check(
        "T40c",
        "{{url}} 리터럴 보존",
        config.URL_PLACEHOLDER in card,
        "젠잇이 치환하는 자리표시자가 그대로 남아야 한다",
    )
    report.check(
        "T40d",
        "재생성 명령 조립",
        exporter.build_command_hint(
            merge_character(CharacterPreset("mika", "silver hair")), "charaset", "mika"
        )
        == "charaset --char mika"
        and "--char_prompt" in exporter.build_command_hint(None, "charaset", "x"),
        "프리셋 사용 시 짧은 명령을 제시한다",
    )

    # T41 — 디스크 스캔
    with temp_workspace() as root:
        folder = root / "assets"
        folder.mkdir()
        for stem, _expected in CARD_SCAN_FIXTURE:
            (folder / f"{stem}{config.ASSET_SUFFIX}").write_bytes(b"x")
        # 확장자가 다른 파일은 스캔 대상이 아니다.
        (folder / "mika_55.png").write_bytes(b"x")

        scanned = exporter.scan_asset_codes(folder, "mika")
        expected = tuple(
            sorted({code for _stem, code in CARD_SCAN_FIXTURE if code is not None})
        )
        report.check(
            "T41",
            f"파일명 역파싱 {len(CARD_SCAN_FIXTURE)}케이스",
            scanned == expected,
            f"{list(scanned)} (기대 {list(expected)})",
        )
        report.check(
            "T41b",
            "다른 접두어·확장자 제외",
            3 not in scanned and 55 not in scanned,
            "other_03.webp 와 mika_55.png 를 무시",
        )
        report.check(
            "T41c",
            "폴더 부재 시 빈 튜플",
            exporter.scan_asset_codes(root / "nosuch", "mika") == (),
        )

        known, unknown = exporter.select_card_codes(folder, "mika", synthetic)
        report.check(
            "T41d",
            "DB 에 없는 코드 분리",
            set(known) <= set(synthetic.entries)
            and 123 in unknown
            and 0 in unknown,
            f"채택 {list(known)} / 제외 {list(unknown)}",
        )

    # T42 — 쓰기 왕복 및 경로 격리
    with temp_workspace() as root:
        real_paths = AssetPaths(root, "t", kind=OutputKind.REAL)
        mock_paths = AssetPaths(root, "t", kind=OutputKind.MOCK)
        real_paths.ensure()

        written = exporter.write_card(
            real_paths,
            meta=meta,
            codes=codes,
            database=synthetic,
            formatter=formatter,
        )
        reloaded = written.read_text(encoding="utf-8")
        report.check(
            "T42",
            "카드 쓰기·재읽기 왕복",
            written.is_file()
            and reloaded == card
            and reloaded.count("![image](") == len(codes),
            f"{written.name} ({len(reloaded)}자, 조립 결과와 바이트 일치)",
        )
        report.check(
            "T42b",
            "카드 경로가 출력 폴더 안",
            written.parent == real_paths.output_dir
            and written.name.endswith(".md")
            and exporter.card_path(mock_paths) != written,
            "mock 카드는 mock_assets/ 로 격리된다",
        )

        # T42c — 좁은 범위 실행이 카드를 축소시키지 않는다.
        # 파일은 전부 있는 상태에서 일부 코드만 넘겨도, 카드는 디스크
        # 스캔 결과를 쓰므로 전체를 유지해야 한다.
        for code in codes:
            (real_paths.output_dir / formatter.filename("t", code)).write_bytes(b"x")
        narrow = exporter.export_card(
            real_paths,
            database=synthetic,
            formatter=formatter,
            resolved=None,
            profile_name="female",
            negative="n",
            program="charaset",
            kind_label="자체 진단",
        )
        content = narrow.read_text(encoding="utf-8") if narrow else ""
        report.check(
            "T42c",
            "좁은 범위 실행이 카드를 축소시키지 않음",
            narrow is not None and content.count("![image](") == len(codes),
            f"{content.count('![image](')}/{len(codes)}장 유지",
        )

    # T42d — 실제 DB 로도 조립이 성립하는지 (라벨·섹션 누락 검출)
    actual_formatter = CodeFormatter.for_codes(database.all_codes)
    actual_card = exporter.build_card(
        meta=_card_meta(prefix="real"),
        codes=database.all_codes,
        database=database,
        formatter=actual_formatter,
    )
    report.check(
        "T42d",
        "실제 DB 로 카드 조립",
        actual_card.count("![image](") == len(database.all_codes)
        and "(unknown)" not in actual_card,
        f"{len(database.all_codes)}장",
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
    _check_roster(report, base_dir, database)
    _check_reference(report)
    _check_integration(report, database)
    _check_card(report, database)
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

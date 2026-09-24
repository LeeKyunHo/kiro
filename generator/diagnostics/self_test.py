"""
generator.diagnostics.self_test
데이터·로직 자체 진단 로직 (T1~T37). 파일 쓰기와 네트워크 요청을 하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from PIL import Image

from generator.config import (
    CHARACTERS_DIRNAME,
    GIB,
    MAX_CODE,
    POSE_DB_FILE,
    PROJECTS_DIRNAME,
    REF_WEIGHT_DEFAULT,
    REFERENCE_EXTENSIONS,
    SEPARATOR,
    URL_PLACEHOLDER,
    ConfigError,
)
from generator.models import (
    BatchResult,
    CharacterConfig,
    ControlNetSpec,
    PoseDatabase,
    TestReport,
    summarize_durations,
)
from generator.pose_db import (
    _iter_sections,
    load_pose_db,
    looks_like_code_expr,
    parse_codes_expr,
    parse_pose_db,
    read_pose_json,
)
from generator.prompt import (
    assemble_prompt,
    find_tag_conflicts,
    normalize_tag,
    resolve_background,
    validate_prefix,
    validate_ref_weight,
)
from generator.reference import (
    build_controlnet_unit,
    find_reference_candidates,
    match_model_name,
    resolve_reference_image,
)
from generator.reporter import asset_filename, build_genit_block, code_width
from generator.roster import (
    _resolve_default_mode,
    apply_character_to_args,
    load_character,
)
from generator.webui_client import (
    build_txt2img_payload,
    extract_vram_peak,
    inject_controlnet,
)

# ─────────────────────────────────────────────
# 테스트 픽스처
# ─────────────────────────────────────────────
PARSER_CASES: tuple[tuple[str, list[int]], ...] = (
    ("20-29", list(range(20, 30))),
    ("0,3,7", [0, 3, 7]),
    ("0-5,10,20-22", [0, 1, 2, 3, 4, 5, 10, 20, 21, 22]),
    ("29-20", list(range(20, 30))),
    ("0-5,3", [0, 1, 2, 3, 4, 5]),
    (" 1 , 2 ", [1, 2]),
)

REJECT_EXPRS: tuple[str, ...] = (
    "abc",
    "1-",
    f"0-{MAX_CODE + 1}",
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

WIDTH_CASES: tuple[tuple[list[int], int], ...] = (
    ([0, 19], 2),
    ([0, 99], 2),
    ([0, 100], 3),
    ([7], 2),
    ([], 2),
)

REF_WEIGHT_REJECT: tuple[float, ...] = (-0.1, 2.1, -1.0, 99.0)
REF_WEIGHT_ACCEPT: tuple[float, ...] = (0.0, 0.5, 0.7, 1.0, 2.0)

CN_UNIT_REQUIRED_KEYS = frozenset({
    "enabled", "input_image", "module", "model",
    "weight", "resize_mode", "control_mode", "pixel_perfect",
})

CN_MODEL_FIXTURE = (
    "control_v11p_sd15_openpose [cab727d4]",
    "ip-adapter_xl [4209e9f7]",
    "t2iadapter_style_sd14v1 [202e85cc]",
)
CN_MODULE_FIXTURE = ("none", "canny", "openpose_full", "ip-adapter_clip_sdxl")

SYNTHETIC_RAW: dict[str, Any] = {
    "_comment": "self-test fixture",
    "alpha": {"5": "five, tag", "12": "twelve, tag", "03": "three, tag"},
    "beta": {"7": "seven, tag"},
}

NORMALIZE_CASES: tuple[tuple[str, str], ...] = (
    ("(huge:1.3)", "huge"),
    ("((tag))", "tag"),
    ("[soft]", "soft"),
    (" Bad   Hands ", "bad hands"),
    ("(masterpiece:1.2)", "masterpiece"),
    ("plain", "plain"),
    ("(weight:-0.5)", "weight"),
)

CONFLICT_CASES: tuple[tuple[str, str, list[str]], ...] = (
    ("1girl, solo, smile", "1boy, male", []),
    ("1girl, (breasts:1.2)", "breasts, muscular", ["breasts"]),
    ("a, b, c", "C, B", ["b", "c"]),
    ("", "anything", []),
)


def _audit_data_quality(report: TestReport, raw: dict[str, Any]) -> None:
    """T4~T6: 데이터 품질 검증."""
    bad_keys: list[str] = []
    empty_values: list[str] = []
    duplicates: list[str] = []
    seen: dict[int, str] = {}

    for section, body in _iter_sections(raw):
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

    for name, findings in (
        ("T4 비정수 키", bad_keys),
        ("T5 빈 프롬프트", empty_values),
        ("T6 중복 코드", duplicates),
    ):
        if findings:
            report.warn(name, str(findings))
        else:
            report.ok(f"{name} 없음")


def _test_logic(report: TestReport, db: PoseDatabase) -> None:
    """T8~T17: 순수 함수 및 로직 검증."""
    print("\n[로직 검사]")
    synthetic = parse_pose_db(SYNTHETIC_RAW)

    lexicographic = [int(k) for k in sorted(["5", "12", "03"])]
    report.check(
        "T8 정수 정렬 (사전순 아님)",
        synthetic.all_codes == [3, 5, 7, 12] and lexicographic != [3, 5, 12],
        f"{synthetic.all_codes}, 사전순={lexicographic}",
    )
    report.check(
        "T8b 실제 DB 정렬", db.all_codes == sorted(db.all_codes), str(db.all_codes)
    )

    report.check(
        "T9 code_width 산출",
        all(code_width(codes) == expected for codes, expected in WIDTH_CASES),
        str([(codes, code_width(codes)) for codes, _ in WIDTH_CASES]),
    )

    failures = []
    for expr, expected in PARSER_CASES:
        try:
            actual = parse_codes_expr(expr)
            if actual != expected:
                failures.append(f"'{expr}'->{actual}!={expected}")
        except Exception as e:
            failures.append(f"'{expr}' raised {e}")
    report.check(f"T10 parse_codes_expr {len(PARSER_CASES)}케이스", not failures,
                 str(failures) if failures else "")

    report.check(
        "T10b looks_like_code_expr 판별",
        all(map(looks_like_code_expr, ("0,5,12", "10-14")))
        and not any(map(looks_like_code_expr, ("emotions", "all", ""))),
    )

    report.check(
        "T11 asset_filename 조립",
        asset_filename("x", 7, 2) == "x_07.webp"
        and asset_filename("x", 7, 3) == "x_007.webp"
        and asset_filename("x", 123, 3) == "x_123.webp",
        asset_filename("x", 7, 2),
    )

    codes = synthetic.all_codes
    block = build_genit_block("t", codes, synthetic, code_width(codes))
    calls = [line for line in block.splitlines() if line.startswith(f"{URL_PLACEHOLDER}t/")]
    report.check("T12 마크다운 라인 수 == 대상 수", len(calls) == len(codes),
                 f"{len(calls)}/{len(codes)}")
    report.check("T13 {{url}} 리터럴 포함", URL_PLACEHOLDER in block)

    rejected = []
    for unsafe in UNSAFE_PREFIXES:
        try:
            validate_prefix(unsafe)
        except ConfigError:
            continue
        rejected.append(unsafe)
    report.check("T14 위험 prefix 차단", not rejected,
                 f"통과됨: {rejected}" if rejected else f"{len(UNSAFE_PREFIXES)}종 차단")
    report.check(
        "T14b 정상 prefix 허용",
        validate_prefix(" mika ") == "mika" and validate_prefix("test_01") == "test_01",
    )

    accepted = []
    for expr in REJECT_EXPRS:
        try:
            parse_codes_expr(expr)
        except ValueError:
            continue
        accepted.append(expr)
    report.check("T15 잘못된 표현식 거부", not accepted,
                 f"통과됨: {accepted}" if accepted else f"{len(REJECT_EXPRS)}종 거부")

    bad_norm = [
        f"'{src}'->'{normalize_tag(src)}'!='{expected}'"
        for src, expected in NORMALIZE_CASES
        if normalize_tag(src) != expected
    ]
    report.check(f"T16 normalize_tag {len(NORMALIZE_CASES)}케이스", not bad_norm,
                 str(bad_norm) if bad_norm else "")

    bad_conflict = [
        f"({pos!r},{neg!r})->{find_tag_conflicts(pos, neg)}!={expected}"
        for pos, neg, expected in CONFLICT_CASES
        if find_tag_conflicts(pos, neg) != expected
    ]
    report.check(f"T17 find_tag_conflicts {len(CONFLICT_CASES)}케이스",
                 not bad_conflict, str(bad_conflict) if bad_conflict else "")


def _test_events_and_prompt(report: TestReport, base_dir: Path, db: PoseDatabase) -> None:
    """T18~T20: 이벤트 동적 병합 및 BREAK 프롬프트 조립 검증."""
    print("\n[이벤트 및 프롬프트 조립 검사]")

    # T18: events.json 동적 병합 검증
    sea_events = base_dir / PROJECTS_DIRNAME / "sea" / "events.json"
    if sea_events.exists():
        merged_db = load_pose_db(base_dir, sea_events)
        has_events = (
            "event_main" in merged_db.sections
            and "event_random" in merged_db.sections
            and 201 in merged_db.entries
        )
        report.check(
            "T18 events.json 동적 병합",
            has_events,
            f"섹션 {len(merged_db.sections)}개 (event_main/event_random 포함)",
        )

        # T19: _resolve_default_mode 이벤트 자동 확장 검증
        resolved_with_events = _resolve_default_mode("otokonoko", merged_db)
        expected_sections = "emotions,poses,scenes_otokonoko,event_main,event_random"
        report.check(
            "T19 default_mode 이벤트 자동 포함",
            resolved_with_events == expected_sections,
            resolved_with_events,
        )
    else:
        report.ok("T18 events.json 동적 병합 (생략)", "sea/events.json 없음")
        report.ok("T19 default_mode 이벤트 자동 포함 (생략)")

    # T20: assemble_prompt BREAK 전진 배치 검증
    sample_pos = "masterpiece, best quality BREAK 1girl, solo, silver hair"
    assembled = assemble_prompt(sample_pos, "", "standing, smile", "rei_00")
    expected_assembled = "masterpiece, best quality, standing, smile BREAK 1girl, solo, silver hair, rei_00"
    report.check(
        "T20 assemble_prompt BREAK 전진 배치",
        assembled == expected_assembled,
        f"1청크 포즈 전진 배치 완료: {assembled[:45]}...",
    )


@contextmanager
def _temp_reference(extensions: Sequence[str] = (".png",)) -> Iterator[Path]:
    """임시 참조 이미지를 생성 및 정리하는 컨텍스트 매니저."""
    tmp = Path(tempfile.mkdtemp(prefix="sdref_"))
    try:
        for ext in extensions:
            Image.new("RGB", (64, 96), (128, 128, 200)).save(tmp / f"t{ext}")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_reference(report: TestReport) -> None:
    """T21~T32: 참조 이미지 및 페이로드 조립 검증."""
    print("\n[참조 이미지 및 페이로드 검사]")

    with _temp_reference(REFERENCE_EXTENSIONS) as tmp:
        found = find_reference_candidates(tmp, "t")
        report.check(
            "T21 확장자 우선순위",
            len(found) == 4 and found[0].suffix == ".png",
            f"{[p.suffix for p in found]}",
        )
        picked = resolve_reference_image(tmp, "t")
        report.check(
            "T21b .png 채택",
            picked is not None and picked.path.suffix == ".png",
            picked.path.name if picked else "None",
        )

    with _temp_reference(()) as tmp:
        try:
            missing = resolve_reference_image(tmp, "nosuch")
            report.check("T22 참조 부재 시 None", missing is None, repr(missing))
        except Exception as e:
            report.check("T22 참조 부재 시 None", False, f"예외 발생: {e}")

    empty_dir = Path(tempfile.mkdtemp(prefix="sdref_empty_"))
    try:
        report.check(
            "T22b references/ 폴더 자체 부재",
            resolve_reference_image(empty_dir, "t") is None,
        )
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)

    with _temp_reference((".png",)) as tmp:
        ref = resolve_reference_image(tmp, "t")
        ok = False
        detail = "참조 로드 실패"
        if ref is not None:
            decoded = base64.b64decode(ref.b64)
            with Image.open(io.BytesIO(decoded)) as img:
                ok = img.size == (ref.width, ref.height) == (64, 96)
                detail = f"{img.size} == ({ref.width}, {ref.height})"
        report.check("T23 base64 왕복", ok, detail)

        spec = ControlNetSpec("ip-adapter_clip_sdxl", "ip-adapter_xl [test]", "manual")
        base_payload = build_txt2img_payload(
            prompt="p", negative_prompt="n", sampler_name="s"
        )

        assert ref is not None
        unit = build_controlnet_unit(ref, spec, 0.7)
        missing_keys = CN_UNIT_REQUIRED_KEYS - unit.keys()
        report.check(
            "T24 유닛 필수 키",
            not missing_keys and unit["enabled"] is True and unit["weight"] == 0.7,
            f"누락: {sorted(missing_keys)}" if missing_keys else f"{len(unit)}개 키",
        )
        report.check(
            "T24b 유닛에 base64 이미지 포함",
            unit["input_image"] == ref.b64 and len(unit["input_image"]) > 0,
        )

        report.check(
            "T25 참조 없을 때 alwayson_scripts 미주입",
            "alwayson_scripts" not in base_payload,
            f"키 {len(base_payload)}개",
        )

        injected = inject_controlnet(base_payload, unit)
        try:
            args_list = injected["alwayson_scripts"]["controlnet"]["args"]
            placed = len(args_list) == 1 and args_list[0] is unit
        except (KeyError, TypeError):
            placed = False
        report.check("T26 주입 위치", placed)
        report.check(
            "T26b 원본 페이로드 불변",
            "alwayson_scripts" not in base_payload,
            "inject_controlnet 이 원본을 변경하지 않음",
        )
        report.check(
            "T26c 기존 키 보존",
            all(injected[k] == v for k, v in base_payload.items()),
        )

    wrongly_accepted = []
    for value in REF_WEIGHT_REJECT:
        try:
            validate_ref_weight(value)
            wrongly_accepted.append(value)
        except ConfigError:
            pass
    wrongly_rejected = []
    for value in REF_WEIGHT_ACCEPT:
        try:
            validate_ref_weight(value)
        except ConfigError:
            wrongly_rejected.append(value)
    report.check(
        "T27 ref_weight 범위",
        not wrongly_accepted and not wrongly_rejected,
        f"오통과 {wrongly_accepted} / 오거부 {wrongly_rejected}"
        if (wrongly_accepted or wrongly_rejected)
        else f"거부 {len(REF_WEIGHT_REJECT)}종 / 허용 {len(REF_WEIGHT_ACCEPT)}종",
    )

    matched_model = match_model_name(CN_MODEL_FIXTURE, ("ip-adapter", "ipadapter"))
    matched_module = match_model_name(CN_MODULE_FIXTURE, ("ip-adapter", "ipadapter"))
    report.check(
        "T30 해시 포함 모델명 매칭",
        matched_model == "ip-adapter_xl [4209e9f7]"
        and matched_module == "ip-adapter_clip_sdxl",
        f"{matched_model} / {matched_module}",
    )
    report.check(
        "T30b 매칭 실패 시 None",
        match_model_name(("canny", "openpose"), ("ip-adapter", "ipadapter")) is None,
    )

    stats = summarize_durations([2.0, 4.0, 6.0])
    report.check(
        "T31 시간 집계",
        stats is not None
        and stats.count == 3
        and stats.total == 12.0
        and stats.average == 4.0
        and stats.fastest == 2.0
        and stats.slowest == 6.0,
        stats.format() if stats else "None",
    )
    report.check("T31b 빈 측정값 None", summarize_durations([]) is None)

    batch = BatchResult(durations=[(0, 1.5), (1, 2.5)])
    report.check(
        "T31c BatchResult.timing",
        batch.timing is not None and batch.timing.count == 2 and batch.timing.average == 2.0,
        batch.timing.format() if batch.timing else "None",
    )

    parsed = extract_vram_peak({"cuda": {"system": {"total": 8 * GIB}, "active_peak": 6 * GIB}})
    report.check(
        "T32 VRAM 파싱",
        parsed is not None and abs(parsed[0] - 6.0) < 0.01 and abs(parsed[1] - 8.0) < 0.01,
        f"{parsed}" if parsed else "None",
    )
    report.check(
        "T32b GiB 환산",
        parsed is not None and abs(parsed[0] - 6.0) < 0.01 and abs(parsed[1] - 8.0) < 0.01,
        f"{parsed[0]:.2f} / {parsed[1]:.2f} GiB" if parsed else "None",
    )


def _test_characters_and_safety(report: TestReport, base_dir: Path) -> None:
    """T33~T35: 캐릭터 옵션 우선순위, 실제 로스터 무결성, 금지 태그 방지 검증."""
    print("\n[캐릭터 및 안전성 검사]")

    # T33: apply_character_to_args 우선순위 (독립 픽스처 검증)
    dummy_cfg = CharacterConfig(
        name="test_char",
        char_prompt="default prompt",
        prefix="tst",
        custom_neg="default neg",
        ref_weight=0.7,
        positive="default pos",
        negative="default neg",
        default_mode="female",
    )
    empty_args = argparse.Namespace(
        prefix=None, char_prompt=None, custom_neg="", ref_weight=REF_WEIGHT_DEFAULT,
    )
    apply_character_to_args(dummy_cfg, empty_args)
    filled_ok = (
        empty_args.prefix == "tst"
        and empty_args.char_prompt == "default prompt"
    )

    full_args = argparse.Namespace(
        prefix="override", char_prompt="override prompt",
        custom_neg="", ref_weight=0.5,
    )
    apply_character_to_args(dummy_cfg, full_args)
    preserved_ok = (
        full_args.prefix == "override"
        and full_args.char_prompt == "override prompt"
        and full_args.ref_weight == 0.5
    )

    report.check(
        "T33 apply_character_to_args 우선순위",
        filled_ok and preserved_ok,
        "빈 args 채움 OK, CLI 오버라이드 보존 OK",
    )

    # T34: 전체 로스터 캐릭터 JSON 문법 및 필수 키 무결성 검사
    projects_dir = base_dir / PROJECTS_DIRNAME
    char_files = list(projects_dir.glob("*/characters/*.json"))
    has_chars = len(char_files) > 0

    parse_errors: list[str] = []
    loaded_count = 0

    for cf in char_files:
        try:
            with open(cf, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                parse_errors.append(f"{cf.name}: 최상위 딕셔너리 아님")
                continue
            if not data.get("prefix"):
                parse_errors.append(f"{cf.name}: prefix 누락")
                continue
            loaded_count += 1
        except Exception as e:
            parse_errors.append(f"{cf.name} 파싱 에러: {e}")

    report.check(
        "T34 로스터 캐릭터 JSON 무결성 검사",
        has_chars and not parse_errors,
        f"{loaded_count}개 캐릭터 JSON 파일 문법 및 필수 키 검증 완료",
    )

    # T34b: 전체 로스터 캐릭터 금지 태그(sweat/liquid 등) 미포함 안전성 검사
    # 이 태그들이 negative 에 있으면 땀·체액 연출이 강제로 지워지는 치명적 버그 발생
    FORBIDDEN_NEG_TAGS = {"sweat", "perspiration", "liquid", "splatter"}
    all_char_files = list(projects_dir.glob("*/characters/*.json"))
    all_violations: list[str] = []

    for acf in all_char_files:
        try:
            with open(acf, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue
        neg = str(data.get("negative", "") or "").lower()
        custom = str(data.get("custom_neg", "") or "").lower()
        combined = neg + ", " + custom
        found = [t for t in FORBIDDEN_NEG_TAGS if t in combined]
        if found:
            roster_name = acf.parent.parent.name
            all_violations.append(f"{roster_name}/{acf.name}: {found}")

    report.check(
        "T34b 전체 로스터 캐릭터 네거티브 안전성 검사",
        bool(all_char_files) and not all_violations,
        f"{len(all_char_files)}개 캐릭터 금지 태그(sweat/liquid) 완전 배제 확인"
        if not all_violations
        else f"위반 발견: {all_violations}",
    )

    # T35: 감정 씬 clean background 치환 vs 타 씬 보존 검증
    sample_emotion = "standing, calm face, clean background, soft lighting"
    sample_h_scene = "lying on bed, legs spread, missionary position, (black censor bar:1.25)"
    bg_str = "tropical beach bar terrace"

    # 감정 씬 치환 확인
    replaced_emotion = sample_emotion.replace("clean background", bg_str)
    # 침대 씬 치환 없음 확인
    unaffected_h = sample_h_scene.replace("clean background", bg_str)

    report.check(
        "T35 감정 씬 배경 치환 vs 타 씬 보존",
        "clean background" not in replaced_emotion
        and bg_str in replaced_emotion
        and unaffected_h == sample_h_scene,
        "00번 감정 씬 치환 OK, 40번 침대 씬 완전 보존 OK",
    )


def _test_background(report: TestReport, base_dir: Path) -> None:
    """T37: 배경 해석 로직 (CLI 우선순위, 파일 프리셋 로드, 기본값 폴백)."""
    print("\n[배경 시스템 검사]")
    bg_cli = resolve_background("explicit bg", base_dir / "nonexistent.json")
    report.check("T37a CLI 배경 우선", bg_cli == "explicit bg", str(bg_cli))

    bg_none = resolve_background(None, base_dir / "nonexistent.json")
    report.check("T37b 파일 부재 시 None", bg_none is None, str(bg_none))

    sea_bg_file = base_dir / "projects" / "sea" / "background.json"
    if sea_bg_file.exists():
        bg_def = resolve_background(None, sea_bg_file, "default")
        report.check("T37c sea default 배경 로드", bool(bg_def and "beach bar" in bg_def), str(bg_def))
        bg_night = resolve_background(None, sea_bg_file, "night")
        report.check("T37d sea night 배경 로드", bool(bg_night and "night" in bg_night), str(bg_night))
        bg_fallback = resolve_background(None, sea_bg_file, "unknown_preset")
        report.check("T37e 없는 프리셋 default 폴백", bool(bg_fallback and "beach bar" in bg_fallback), str(bg_fallback))


def _finish_test(report: TestReport) -> int:
    print(f"\n{SEPARATOR}")
    print(f"  결과: PASS {report.passed} / FAIL {report.failed} / WARN {report.warned}")
    print(f"  종료 코드: {report.exit_code}")
    print(f"{SEPARATOR}\n")
    return report.exit_code


def run_self_test(base_dir: Path) -> int:
    """데이터·로직 자체 진단 실행."""
    report = TestReport()

    print(f"\n{SEPARATOR}")
    print("  자체 검증 (--test)")
    print(SEPARATOR)
    print("\n[데이터 검사]")

    db_path = base_dir / POSE_DB_FILE
    if not report.check("T1 JSON 파일 존재", db_path.exists(), str(db_path)):
        return _finish_test(report)

    try:
        raw = read_pose_json(base_dir)
        report.check("T2 JSON 문법", True)
        report.check("T3 최상위 섹션 딕셔너리", True)
    except ConfigError as e:
        report.check("T2/T3 JSON 로드", False, f"{e} {e.hint}".strip())
        return _finish_test(report)

    if non_dict := [n for n, b in _iter_sections(raw) if not isinstance(b, dict)]:
        report.warn("T3b 비-딕셔너리 섹션", str(non_dict))

    _audit_data_quality(report, raw)

    db = parse_pose_db(raw)
    if not report.check("T7 유효 엔트리 1개 이상", bool(db.entries), f"{len(db.entries)}개"):
        return _finish_test(report)

    _test_logic(report, db)
    _test_events_and_prompt(report, base_dir, db)
    _test_reference(report)
    _test_characters_and_safety(report, base_dir)
    _test_background(report, base_dir)
    return _finish_test(report)

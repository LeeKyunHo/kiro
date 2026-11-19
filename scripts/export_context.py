"""
scripts/export_context.py
외부 AI 코드 리뷰 및 세션 인계용 Kiro 단독 3-Tier 컨텍스트 스냅샷 생성기.
(plepa_engine 의존성 없이 Kiro 코드/JSON/git 상태에서 직접 추출)

출력 파일 (기본 context_snapshot/):
  00_README_FOR_AI.md       읽기 규칙, snapshot_id, 폐지 목록(Deprecation Ledger), 직전 대비 변경분
  10_TIER1_CONSTITUTION.md  V2 포즈 대역, GEMINI.md 규칙, AI_HANDOVER_GUIDE.md 요약, 아키텍처 불변식
  20_TIER2_CORE_LOGIC.md    데이터 모델, GENERATION_OPTION_SPECS, QA/검열 파라미터, 핵심 모듈 API, 포즈 DB 요약
  30_TIER3_RUNTIME.md       변경분, Git 상태, 로스터 현황, (선택) 테스트/사전점검 결과
  40_CORE_CODE.md           (--full-code) Tier 2 핵심 모듈 원문
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

KIRO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR_NAME = "context_snapshot"
HASH_STATE_FILE = ".hashes.json"

# Kiro 폐지/불변 목록 (외부 AI의 구 체계 혼동 방지)
DEPRECATION_LEDGER: list[tuple[str, str, str]] = [
    ("구 80종 포즈 체계 / genit_actions", "V2 대역 000~197 (159종, events 13종 유지, 오토코노코 +70)", "Wave 2 SSOT"),
    ("캐릭터 JSON의 long slender limbs", "balanced body proportions", "제작규칙 1.1"),
    ("과도한 강조 가중치 전역 강제 클램핑", "기본값 False (opt-in --clamp-weights), 의도된 1.35~1.55 보호", "Wave 0~2 확정"),
    ("단일 거대 censor.py / kiro_gui.py 모놀리스", "censor(코어)+censor_styles+censor_seg+censor_detect / gui/* 분리", "Wave 3 · P2-B"),
    ("checkpoint quality_positive 자동 병합", "수치 파라미터(steps/cfg/sampler/scheduler/size/clip_skip)만 레이어 적용", "Wave 0~1 확정"),
    ("오토코노코 +70 비노출 제외(171~175)", "여성 101/102/104/105만 제외, 오토코노코 150~200 전수 검열", "ADR-103"),
]

CORE_MODULES: list[str] = [
    "generator/config.py",
    "generator/models.py",
    "generator/option_specs.py",
    "generator/prompt.py",
    "generator/scene_composer.py",
    "generator/runner.py",
    "generator/qa_validator.py",
    "generator/censor.py",
    "generator/censor_seg.py",
    "generator/censor_detect.py",
    "generator/censor_styles.py",
    "generator/precheck.py",
    "generator/pose_db.py",
    "generator/checkpoint.py",
]

TRACKED_GLOBS: list[str] = [
    "generator/**/*.py",
    "gui/**/*.py",
    "sd_batch_generator.py",
    "pose_database.json",
    "pose_sets.json",
    "checkpoint_profiles.json",
    "projects/*/pose_overrides.json",
    "projects/*/background.json",
    "projects/*/pose_background_mapping.json",
    "projects/*/characters/*.json",
    "GEMINI.md",
    "docs/AI_HANDOVER_GUIDE.md",
]


def _sha(path: Path, n: int = 8) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:n]
    except OSError:
        return "missing"


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _git_info(root: Path) -> dict[str, Any]:
    def _run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            ).stdout.strip()
        except Exception:
            return ""

    if not (root / ".git").exists():
        return {"sha": "no-git", "branch": "-", "dirty": 0, "dirty_files": []}
    porcelain = [line for line in _run("status", "--porcelain").splitlines() if line.strip()]
    return {
        "sha": _run("rev-parse", "--short", "HEAD") or "unknown",
        "branch": _run("rev-parse", "--abbrev-ref", "HEAD") or "-",
        "dirty": len(porcelain),
        "dirty_files": porcelain[:30],
    }


def _header(title: str, snap: dict[str, Any]) -> str:
    return (
        f"# {title}\n\n"
        f"> snapshot_id: `{snap['snapshot_id']}`  \n"
        f"> generated_at: {snap['generated_at']}  \n"
        f"> 이 파일은 `scripts/export_context.py`가 Kiro 코드베이스에서 자동 추출했습니다.\n\n"
    )


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def _cell(v: Any) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(":---" for _ in headers) + " |",
    ]
    out.extend("| " + " | ".join(_cell(c) for c in r) + " |" for r in rows)
    return "\n".join(out) + "\n"


def _short(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _parse_module(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _extract_constants(tree: ast.Module) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for node in tree.body:
        targets: list[ast.Name] = []
        value = None
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        for t in targets:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t.id) or value is None:
                continue
            try:
                lit = ast.literal_eval(value)
                if isinstance(lit, (dict, list, tuple, set, frozenset)) and len(lit) > 8:
                    shown = f"{type(lit).__name__}[{len(lit)}] " + _short(lit, 100)
                else:
                    shown = _short(lit)
            except Exception:
                shown = "(expr) " + _short(ast.unparse(value), 100)
            result.append((t.id, shown))
    return result


def _extract_signatures(tree: ast.Module) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    def _first_doc(n: ast.AST) -> str:
        doc = ast.get_docstring(n) or ""
        return doc.strip().splitlines()[0] if doc.strip() else ""

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            rows.append((f"def {node.name}({ast.unparse(node.args)})", "", _first_doc(node)))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            rows.append((f"class {node.name}({bases})", "", _first_doc(node)))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not sub.name.startswith("_") or sub.name == "__call__"
                ):
                    rows.append((f"  .{sub.name}()", node.name, _first_doc(sub)))
    return rows


def _extract_dataclass_fields(tree: ast.Module, class_name: str) -> list[tuple[str, str, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: list[tuple[str, str, str]] = []
            for sub in node.body:
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    default = ast.unparse(sub.value) if sub.value is not None else "(required)"
                    fields.append((sub.target.id, ast.unparse(sub.annotation), _short(default, 60)))
            return fields
    return []


def build_tier1(root: Path, snap: dict[str, Any]) -> str:
    """TIER 1: 불변 원칙, V2 포즈 대역, GEMINI.md, 핸드오버 가이드 요약을 생성한다."""
    out = [_header("TIER 1 · Kiro 불변 원칙 및 규격 (Constitution)", snap)]

    pose_db = _load_json(root / "pose_database.json") or {}
    structure = pose_db.get("_structure") or pose_db.get("_meta") or {}
    out.append("## 1. V2 포즈 번호 대역 (`pose_database.json`)\n\n")
    if isinstance(structure, dict) and structure:
        out.append(_md_table(["키", "정의"], [[k, _short(v, 160)] for k, v in structure.items()]))
    out.append(
        "\n- Kiro V2 포즈 DB: 159종 (`events` 13종 유지, 오토코노코 성인 코드 = 여성 +70 대칭)\n"
        "- 플랫 로스터 구조: `projects/{roster}/characters/*.json`, `assets/{char}/`, `background.json`\n"
        "- 핵심 불변식: `run_batch()` 150줄 이하, `import generator` Lazy ML Load, 가중치 클램핑 기본값 `False`\n\n"
    )

    rules = root / "GEMINI.md"
    if rules.is_file():
        out.append("## 2. 에이전트 규칙 (`GEMINI.md`)\n\n")
        out.append(rules.read_text(encoding="utf-8-sig").strip() + "\n\n")

    handover = root / "docs" / "AI_HANDOVER_GUIDE.md"
    if handover.is_file():
        headings = re.findall(r"^#{1,3}\s+(.+)$", handover.read_text(encoding="utf-8-sig"), re.M)
        out.append(f"## 3. 핸드오버 가이드 목차 (`docs/AI_HANDOVER_GUIDE.md`, 섹션 {len(headings)}개)\n\n")
        out.append("\n".join(f"- {h}" for h in headings[:60]) + "\n\n")

    return "".join(out)


def _summarize_pose_db(root: Path, include_prompts: bool) -> str:
    out: list[str] = []
    db = _load_json(root / "pose_database.json") or {}
    rows: list[list[Any]] = []
    for section, entries in db.items():
        if section.startswith("_") or not isinstance(entries, dict):
            continue
        codes = sorted(entries.keys(), key=lambda c: int(c) if str(c).isdigit() else 9999)
        rng = f"{codes[0]}~{codes[-1]}" if codes else "-"
        outfits: dict[str, int] = {}
        for e in entries.values():
            if isinstance(e, dict):
                key = str(e.get("required_outfit") or "default")
                outfits[key] = outfits.get(key, 0) + 1
        rows.append([section, len(entries), rng, ", ".join(f"{k}:{v}" for k, v in outfits.items())])
    out.append("### 섹션 요약\n\n" + _md_table(["섹션", "포즈 수", "코드 범위", "required_outfit 분포"], rows))

    if include_prompts:
        out.append("\n### 포즈 프롬프트 상세\n\n")
        for section, entries in db.items():
            if section.startswith("_") or not isinstance(entries, dict):
                continue
            out.append(f"**{section}**\n\n")
            for code in sorted(entries.keys(), key=lambda c: int(c) if str(c).isdigit() else 9999):
                val = entries[code]
                if isinstance(val, dict):
                    out.append(f"- `{code}` {val.get('label', '?')}: `{val.get('prompt', '')}`\n")
                else:
                    out.append(f"- `{code}`: `{val}`\n")
            out.append("\n")

    return "".join(out)


def _extract_qc_parameters(root: Path) -> list[list[str]]:
    path = root / "generator" / "qa_validator.py"
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    rows: list[list[str]] = []
    for label, pattern in (
        ("person/head 기본 임계값", r"conf_threshold:\s*float\s*=\s*([\d.]+)"),
        ("hand 임계값", r"raw_hands.*?>=\s*([\d.]+)"),
        ("face 임계값", r"raw_faces.*?>=\s*([\d.]+)"),
        ("2인 씬 얼굴 상한식", r"max_faces\s*=\s*(.+)"),
    ):
        m = re.search(pattern, text, re.S if "raw_" in pattern else 0)
        if m:
            rows.append([label, m.group(1).strip()])
    return rows


def build_tier2(root: Path, snap: dict[str, Any], include_prompts: bool = False) -> str:
    """TIER 2: 데이터 모델, 옵션 SSOT, QA/검열 상수, 핵심 모듈 API 및 포즈 DB 요약을 생성한다."""
    out = [_header("TIER 2 · Kiro 코어 로직 (Core Logic)", snap)]

    models_path = root / "generator" / "models.py"
    mtree = _parse_module(models_path)
    if mtree:
        out.append(f"## 1. 핵심 데이터 모델 (`generator/models.py` · sha {_sha(models_path)})\n\n")
        for cls in ("GenerationOptions", "PoseEntry", "CharacterConfig", "BatchResult"):
            flds = _extract_dataclass_fields(mtree, cls)
            if flds:
                out.append(f"### `{cls}` ({len(flds)}개 필드)\n\n" + _md_table(["필드", "타입", "기본값"], [list(x) for x in flds]) + "\n")

    qc_rows = _extract_qc_parameters(root)
    if qc_rows:
        out.append("## 2. Auto-QC 파라미터 (`generator/qa_validator.py`)\n\n")
        out.append(_md_table(["항목", "값"], qc_rows) + "\n")

    out.append("## 3. 핵심 모듈 상수 및 공개 API\n\n")
    for rel in CORE_MODULES:
        p = root / rel
        tree = _parse_module(p)
        if not tree:
            continue
        out.append(f"### `{rel}` · sha {_sha(p)} · {_line_count(p)}줄\n\n")
        consts = _extract_constants(tree)
        if consts:
            out.append(_md_table(["상수", "값"], [list(c) for c in consts]) + "\n")
        sigs = _extract_signatures(tree)
        if sigs:
            out.append(_md_table(["시그니처", "소속", "설명"], [list(s) for s in sigs]) + "\n")

    cp = _load_json(root / "checkpoint_profiles.json")
    if cp is not None:
        out.append("## 4. 체크포인트 프로필 (`checkpoint_profiles.json`)\n\n```json\n")
        out.append(json.dumps(cp, ensure_ascii=False, indent=2)[:4000] + "\n```\n\n")

    out.append("## 5. 포즈 DB 현황 (`pose_database.json`)\n\n" + _summarize_pose_db(root, include_prompts))
    return "".join(out)


def _build_roster_inventory(root: Path) -> str:
    projects_dir = root / "projects"
    if not projects_dir.is_dir():
        return "(projects/ 폴더 없음)\n"
    rows: list[list[Any]] = []
    for rdir in sorted(projects_dir.iterdir()):
        chars_dir = rdir / "characters"
        if not rdir.is_dir() or not chars_dir.is_dir():
            continue
        char_cnt = len(list(chars_dir.glob("*.json")))
        has_bg = "O" if (rdir / "background.json").is_file() else "-"
        has_ov = "O" if ((rdir / "pose_overrides.json").is_file() or (rdir / "events.json").is_file()) else "-"
        has_map = "O" if (rdir / "pose_background_mapping.json").is_file() else "-"
        rows.append([rdir.name, char_cnt, has_bg, has_ov, has_map])
    return _md_table(["로스터", "캐릭터 수", "background.json", "overrides/events", "bg_mapping"], rows) if rows else "(유효 로스터 없음)\n"


def _run_checks(root: Path) -> str:
    out: list[str] = []
    py = sys.executable
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONIOENCODING="utf-8")
    kw = {"cwd": str(root), "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 300}
    try:
        r1 = subprocess.run([py, "sd_batch_generator.py", "--test"], env=env, **kw)
        verdict = next((l.strip() for l in r1.stdout.splitlines() if "결과:" in l), f"exit {r1.returncode}")
        out.append(f"- `--test`: `{verdict}` (exit {r1.returncode})\n")
    except Exception as e:
        out.append(f"- `--test` 실패: {e}\n")
    try:
        r2 = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], env=env, **kw)
        tail = [l for l in r2.stdout.splitlines() if re.search(r"passed|failed|error", l)]
        out.append(f"- `pytest`: `{tail[-1].strip('= ') if tail else 'no summary'}` (exit {r2.returncode})\n")
    except Exception as e:
        out.append(f"- `pytest` 실패: {e}\n")
    return "".join(out)


def build_tier3(root: Path, snap: dict[str, Any], delta: list[str], run_tests: bool = False) -> str:
    """TIER 3: 직전 대비 변경분, Git 상태, 로스터 인벤토리 및 테스트 결과를 생성한다."""
    out = [_header("TIER 3 · Kiro 런타임 및 로스터 지표 (Runtime)", snap)]

    out.append("## 1. 직전 스냅샷 대비 변경분\n\n")
    out.append("\n".join(delta) + "\n\n" if delta else "변경 없음 (또는 첫 스냅샷)\n\n")

    g = snap["kiro"]
    out.append("## 2. Git 상태\n\n")
    out.append(_md_table(["레포", "branch", "HEAD", "미커밋 파일 수"], [["kiro", g["branch"], g["sha"], g["dirty"]]]))
    if g["dirty_files"]:
        out.append("\n<details><summary>미커밋 파일</summary>\n\n```\n" + "\n".join(g["dirty_files"]) + "\n```\n</details>\n")

    out.append("\n## 3. 로스터 현황 (`projects/`)\n\n" + _build_roster_inventory(root))

    out.append("\n## 4. 테스트 / 자체 검증\n\n")
    out.append(_run_checks(root) if run_tests else "실행 안 함 (`--run-tests`로 포함)\n")
    return "".join(out)


def collect_hashes(root: Path) -> dict[str, str]:
    """추적 대상 파일의 SHA256 프리픽스 해시 맵을 수집한다."""
    hashes: dict[str, str] = {}
    for pattern in TRACKED_GLOBS:
        for p in root.glob(pattern):
            if p.is_file() and "__pycache__" not in p.parts and "archive" not in p.parts:
                hashes[p.relative_to(root).as_posix()] = _sha(p, 16)
    return hashes


def compute_delta(prev: dict[str, str], cur: dict[str, str]) -> list[str]:
    """이전 해시 맵 대비 추가(A)·삭제(D)·수정(M) 목록을 산출한다."""
    if not prev:
        return []
    lines: list[str] = []
    for k in sorted(set(prev) | set(cur)):
        if k not in prev:
            lines.append(f"- A `{k}`")
        elif k not in cur:
            lines.append(f"- D `{k}`")
        elif prev[k] != cur[k]:
            lines.append(f"- M `{k}`")
    return lines


def build_readme(snap: dict[str, Any], delta: list[str], prev_id: str | None, files: list[str]) -> str:
    """00_README_FOR_AI.md 문서를 조립한다."""
    out = [_header("KIRO CONTEXT SNAPSHOT · 읽기 규칙", snap)]
    out.append(
        "## 외부 AI 읽기 규칙\n\n"
        "1. 이 스냅샷의 `snapshot_id`와 다른 과거 기억/업로드는 모두 **폐기**하세요.\n"
        "2. 포즈 번호, 라벨, 상수, 옵션명은 반드시 이 스냅샷 파일에서 확인한 값만 사용하세요.\n"
        "3. 아래 폐지 목록(Deprecation Ledger)의 구 체계는 참조하지 마세요.\n"
        "4. `projects/*/assets/` 및 `docs/archive/`는 스캔 대상이 아닙니다.\n\n"
    )
    out.append("## 파일 구성\n\n" + "\n".join(f"- `{f}`" for f in files) + "\n\n")
    out.append(f"## 직전 스냅샷 대비 변경분\n\n직전 snapshot_id: `{prev_id or '없음'}`\n\n")
    out.append("\n".join(delta) + "\n\n" if delta else "변경 없음 (또는 첫 스냅샷)\n\n")
    out.append("## 폐지 목록 (Deprecation Ledger)\n\n")
    out.append(_md_table(["폐지 항목", "현재 기준", "근거"], [list(r) for r in DEPRECATION_LEDGER]))
    return "".join(out)


def build_code_bundle(root: Path, snap: dict[str, Any]) -> str:
    """--full-code 지정 시 Tier 2 핵심 모듈 원문을 40_CORE_CODE.md 로 묶는다."""
    out = [_header("CORE CODE · Kiro 핵심 모듈 원문", snap)]
    for rel in CORE_MODULES:
        p = root / rel
        if p.is_file():
            out.append(f"## `{rel}` · sha {_sha(p)}\n\n```python\n{p.read_text(encoding='utf-8-sig')}\n```\n\n")
    return "".join(out)


def export_context_snapshot(
    *,
    root: Path | None = None,
    out_dir: Path | None = None,
    run_tests: bool = False,
    full_code: bool = False,
    include_prompts: bool = False,
) -> dict[str, Any]:
    """Kiro 3-Tier 컨텍스트 스냅샷 파일 세트를 생성하고 메타데이터를 반환한다."""
    base_root = (root if root is not None else KIRO_ROOT).resolve()
    target_dir = (out_dir if out_dir is not None else (base_root / OUT_DIR_NAME)).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    kiro_git = _git_info(base_root)
    tag = kiro_git["sha"] + ("+dirty" if kiro_git["dirty"] else "")
    snap = {
        "snapshot_id": f"{time.strftime('%Y%m%d-%H%M')}-kiro@{tag}",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kiro": kiro_git,
    }

    state_path = target_dir / HASH_STATE_FILE
    prev_state = _load_json(state_path) or {}
    cur_hashes = collect_hashes(base_root)
    delta = compute_delta(prev_state.get("hashes", {}), cur_hashes)

    files: dict[str, str] = {
        "10_TIER1_CONSTITUTION.md": build_tier1(base_root, snap),
        "20_TIER2_CORE_LOGIC.md": build_tier2(base_root, snap, include_prompts=include_prompts),
        "30_TIER3_RUNTIME.md": build_tier3(base_root, snap, delta=delta, run_tests=run_tests),
    }
    if full_code:
        files["40_CORE_CODE.md"] = build_code_bundle(base_root, snap)
    else:
        stale = target_dir / "40_CORE_CODE.md"
        if stale.exists():
            stale.unlink()

    all_file_names = ["00_README_FOR_AI.md", *files.keys()]
    files = {
        "00_README_FOR_AI.md": build_readme(snap, delta, prev_state.get("snapshot_id"), all_file_names),
        **files,
    }

    for name, content in files.items():
        (target_dir / name).write_text(content, encoding="utf-8")
    state_path.write_text(
        json.dumps({"snapshot_id": snap["snapshot_id"], "hashes": cur_hashes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "snapshot_id": snap["snapshot_id"],
        "out_dir": str(target_dir),
        "files": list(files.keys()),
        "delta_count": len(delta),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kiro 단독 3-Tier 컨텍스트 스냅샷 생성기")
    parser.add_argument("--root", default=str(KIRO_ROOT), help="Kiro 레포 루트 경로")
    parser.add_argument("--out", default=str(KIRO_ROOT / OUT_DIR_NAME), help="스냅샷 출력 폴더")
    parser.add_argument("--run-tests", action="store_true", help="--test 및 pytest 실행 결과 포함")
    parser.add_argument("--full-code", action="store_true", help="40_CORE_CODE.md 원문 번들 포함")
    parser.add_argument("--include-prompts", action="store_true", help="포즈 프롬프트 원문 포함")
    args = parser.parse_args(argv)

    info = export_context_snapshot(
        root=Path(args.root),
        out_dir=Path(args.out),
        run_tests=args.run_tests,
        full_code=args.full_code,
        include_prompts=args.include_prompts,
    )
    print(f"[export_context] snapshot_id: {info['snapshot_id']}")
    for name in info["files"]:
        p = Path(info["out_dir"]) / name
        print(f"  - {name} ({p.stat().st_size // 1024} KB)")
    print(f"  변경분: {info['delta_count']}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())

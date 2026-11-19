"""
tests/test_wave3_gui_censor.py
Wave 3 리팩토링(gui 패키지 모듈화·하위 호환, 실행 가드, 로그 스트리밍 핸들러, 검열 배치·백업 단일화) 검증 테스트.
"""
from __future__ import annotations

import logging
from pathlib import Path
from PIL import Image
import pytest

import auto_censor
from generator import censor as censor_mod
import gui
from gui.runner_bridge import (
    build_char_options,
    expand_gui_target_mode,
    format_all_chars_qa_markdown,
    format_qa_warnings_markdown,
)
from gui.settings import DEFAULT_SETTINGS, SETTINGS_FILENAME, load_gui_settings, save_gui_settings
from gui.state import (
    GuiLogBufferHandler,
    GuiRuntimeState,
    attach_gui_log_handler,
    detach_gui_log_handler,
    resolve_gui_mode_and_codes,
)
import kiro_gui


def test_gui_settings_utf8_sig_load_and_save_roundtrip(tmp_path: Path) -> None:
    """gui/settings.py가 UTF-8 BOM을 포함한 설정 파일 로드 및 저장 라운드트립을 정상 수행해야 한다."""
    cfg_path = tmp_path / SETTINGS_FILENAME
    cfg_path.write_bytes(
        '\ufeff{"steps": 36, "cfg_scale": 6.5, "auto_qc": true, "censor": true}'.encode("utf-8")
    )

    loaded = load_gui_settings(tmp_path)
    assert loaded["steps"] == 36
    assert loaded["cfg_scale"] == 6.5
    assert loaded["auto_qc"] is True
    assert loaded["censor"] is True
    assert loaded["sampler"] == DEFAULT_SETTINGS["sampler"]

    loaded["steps"] = 28
    save_gui_settings(tmp_path, loaded)
    reloaded = load_gui_settings(tmp_path)
    assert reloaded["steps"] == 28


def test_gui_runtime_state_duplicate_run_guard_and_cancel_lifecycle() -> None:
    """GuiRuntimeState가 실행 중 중복 시작을 차단하고 취소/종료 시 안전하게 상태를 복원해야 한다."""
    base_dir = Path(__file__).resolve().parent.parent
    state = GuiRuntimeState(base_dir)

    assert state.is_running is False
    ok1, run_id_1, was_running_1 = state.try_begin_run()
    assert ok1 is True
    assert was_running_1 is False
    assert state.is_running is True
    assert state.is_run_cancelled(run_id_1) is False

    # 이미 실행 중일 때 중복 실행 요청 거부
    ok2, held_id, was_running_2 = state.try_begin_run()
    assert ok2 is False
    assert held_id == run_id_1
    assert was_running_2 is True

    # 취소 요청 시 이전 run_id 즉시 무효화 및 실행 플래그 해제
    cancelled_id = state.request_cancel()
    assert cancelled_id > run_id_1
    assert state.is_running is False
    assert state.is_run_cancelled(run_id_1) is True
    assert state.finish_run(run_id_1) is False

    # 취소 이후 새 실행 시작 및 정상 종료
    ok3, run_id_3, _ = state.try_begin_run()
    assert ok3 is True
    assert state.is_run_cancelled(run_id_3) is False
    assert state.finish_run(run_id_3) is True
    assert state.is_running is False


def test_gui_log_buffer_handler_deduplication_and_streaming() -> None:
    """attach_gui_log_handler가 동일 handler_id 재연결 시 핸들러를 중복 등록하지 않고 로그를 스트리밍해야 한다."""
    emitted: list[str] = []
    h1 = GuiLogBufferHandler(max_lines=10, handler_id="test_tab_1", on_emit=emitted.append)
    h2 = GuiLogBufferHandler(max_lines=10, handler_id="test_tab_1", on_emit=emitted.append)

    try:
        attach_gui_log_handler(h1)
        attach_gui_log_handler(h2)

        gen_logger = logging.getLogger("generator")
        matching = [
            h
            for h in gen_logger.handlers
            if getattr(h, "_kiro_gui_log_handler", False)
            and getattr(h, "handler_id", None) == "test_tab_1"
        ]
        assert len(matching) == 1
        assert matching[0] is h2

        child_logger = logging.getLogger("generator.runner")
        child_logger.info("[INFO] Wave 3 GUI 로그 스트리밍 검증 메시지")
        assert "Wave 3 GUI 로그 스트리밍 검증 메시지" in h2.get_text()
        assert emitted and "Wave 3 GUI 로그 스트리밍 검증 메시지" in emitted[-1]

        h2.clear()
        assert h2.get_text() == ""
    finally:
        detach_gui_log_handler(h1)
        detach_gui_log_handler(h2)


def test_gui_runner_bridge_and_kiro_gui_backward_compatibility() -> None:
    """kiro_gui 래퍼 하위 호환 심볼과 gui.runner_bridge V2 섹션/옵션 조립이 정상 동작해야 한다."""
    assert kiro_gui.KiroGuiState is GuiRuntimeState
    assert kiro_gui.build_gui is gui.build_gui
    assert kiro_gui.resolve_gui_mode_and_codes is resolve_gui_mode_and_codes

    base_dir = Path(__file__).resolve().parent.parent
    state = GuiRuntimeState(base_dir)

    female_mode = expand_gui_target_mode("all", None, "female", state.db)
    otk_mode = expand_gui_target_mode("all", None, "otokonoko", state.db)
    assert "h_scenes" in female_mode.split(",")
    assert "scenes_otokonoko" not in female_mode.split(",")
    assert "scenes_otokonoko" in otk_mode.split(",")
    assert "h_scenes" not in otk_mode.split(",")

    if state.char_cfg is not None:
        opts = build_char_options(
            state,
            state.char_cfg,
            "60-63",
            dict(DEFAULT_SETTINGS),
            ckpt_name=next(iter(state.ckpt_profiles.keys()), None),
            mock=True,
        )
        assert opts.codes == "60-63"
        assert opts.mock is True
        assert opts.prefix == state.char_cfg.prefix

    single_qa_md = format_qa_warnings_markdown([(80, "missionary", "손가락 개수 이상")])
    assert "Auto-QC 품질 경고 (1건)" in single_qa_md
    assert "[080 missionary] 손가락 개수 이상" in single_qa_md

    all_qa_md = format_all_chars_qa_markdown([("ignis", 81, "doggy", "배경 누락")])
    assert "Auto-QC 품질 경고 요약 (1건)" in all_qa_md
    assert "[ignis] 포즈 081 (doggy): 배경 누락" in all_qa_md


def test_unified_run_batch_censor_backup_restore_and_non_exposure_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단일화된 run_batch_censor가 hybrid/code 파일명 판정, 비노출 스킵, _origin 백업 및 --restore-origin 복원을 수행해야 한다."""
    char_dir = tmp_path / "ignis"
    char_dir.mkdir(parents=True)

    # 1) 검열 대상 H-씬 (hybrid: 080, code: 150) & 비노출/일상 스킵 대상 (101, 010)
    img_080 = char_dir / "ignis_080_missionary.webp"
    img_150 = char_dir / "ignis_150.webp"
    img_101_skip = char_dir / "ignis_101_afterglow.webp"
    img_010_skip = char_dir / "ignis_010_smile.webp"

    original_rgb = (20, 40, 60)
    censored_rgb = (250, 250, 250)

    for p in (img_080, img_150, img_101_skip, img_010_skip):
        Image.new("RGB", (32, 32), color=original_rgb).save(p, format="WEBP")

    def fake_censor_image(
        image: Image.Image | Path | str,
        style: str = "white_silhouette",
        targets: object = ("penis", "pussy"),
        min_confidence: float = 0.20,
        pose_code: object = None,
        enable_anchor_fallback: bool = False,
    ) -> tuple[Image.Image, int]:
        return Image.new("RGB", (32, 32), color=censored_rgb), 1

    monkeypatch.setattr(censor_mod, "censor_image", fake_censor_image)

    # 1차 배치 검열 실행 (_origin 백업 활성화)
    processed = censor_mod.run_batch_censor(
        folder_path=tmp_path,
        style="white_silhouette",
        backup_origin=True,
    )
    assert processed == 2

    origin_dir = tmp_path / "ignis_origin"
    assert (origin_dir / img_080.name).is_file()
    assert (origin_dir / img_150.name).is_file()
    assert not (origin_dir / img_101_skip.name).exists()
    assert not (origin_dir / img_010_skip.name).exists()

    # _origin 백업본은 원본 픽셀을 보존하고, 본 폴더 파일은 검열된 픽셀로 교체되었는지 확인
    with Image.open(origin_dir / img_080.name) as orig_im:
        r, g, b = orig_im.convert("RGB").getpixel((0, 0))
        assert abs(r - original_rgb[0]) <= 10 and abs(b - original_rgb[2]) <= 10
    with Image.open(img_080) as cen_im:
        r, g, b = cen_im.convert("RGB").getpixel((0, 0))
        assert r > 220 and g > 220 and b > 220

    # 2차 실행: 본 폴더 이미지를 임의 색(검정)으로 오염시킨 뒤 restore_origin=True로 재검열
    Image.new("RGB", (32, 32), color=(0, 0, 0)).save(img_080, format="WEBP")
    restored_sources: list[tuple[int, int, int]] = []

    def inspect_restored_then_censor(
        image: Image.Image | Path | str,
        **kwargs: object,
    ) -> tuple[Image.Image, int]:
        im = Image.open(image) if isinstance(image, (str, Path)) else image
        restored_sources.append(im.convert("RGB").getpixel((0, 0)))
        return Image.new("RGB", (32, 32), color=censored_rgb), 1

    monkeypatch.setattr(censor_mod, "censor_image", inspect_restored_then_censor)

    processed_2 = auto_censor.run_batch_censor_with_backup(
        folder_path=tmp_path,
        restore_origin=True,
        char_names=["ignis"],
    )
    assert processed_2 == 2
    # 재검열 시 오염된 검정(0,0,0)이 아니라 _origin에서 복원된 원본 픽셀이 입력으로 들어갔는지 검증
    assert any(abs(px[2] - original_rgb[2]) <= 10 for px in restored_sources)


def test_auto_censor_cli_parser_supports_roster_char_and_restore_origin() -> None:
    """auto_censor.build_parser()가 --roster, --char, --restore-origin 옵션을 지원해야 한다."""
    parser = auto_censor.build_parser()
    args = parser.parse_args(
        ["--roster", "dark_generals", "--char", "ignis", "--restore-origin", "--enable-anchor"]
    )
    assert args.roster == "dark_generals"
    assert args.char == ["ignis"]
    assert args.restore_origin is True
    assert args.enable_anchor is True
    assert not Path("tools/apply_precise_censor.py").exists()

"""
gui/job_manager.py
Kiro Studio 전역 비동기 FIFO 배치/리롤 작업 큐 (GlobalJobManager).

- 단일 워커 스레드 기반 FIFO 순차 직렬 실행으로 Forge GPU 경합 원천 차단.
- 작업 생명주기: queued -> running -> completed | failed | cancelled.
- 개별 대기 작업 취소, 실행 중 작업 취소(request_cancel), 실패 격리(후속 큐 정상 진행) 지원.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable

from generator.models import BatchResult, GenerationOptions, RunHooks
from generator.pose_db import load_pose_db
from generator.roster import RosterPathManager, apply_character_to_args, load_character
from generator.runner import execute, run_all_chars
from generator.webui_client import interrupt_forge

logger = logging.getLogger("generator.gui.jobs")


@dataclass
class JobExecutionContext:
    """실행 중인 단일 작업에 주입되는 취소 확인·진행률·QA 경고 보고 컨텍스트."""

    job_id: str
    _cancel_event: threading.Event
    _on_progress: Callable[[int, int, str], None]
    _on_qa_warnings: Callable[[list[dict[str, Any]]], None]

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def report_progress(self, current: int, total: int, message: str = "") -> None:
        self._on_progress(current, total, message)

    def record_qa_warnings(self, warnings: list[dict[str, Any]]) -> None:
        if warnings:
            self._on_qa_warnings(warnings)


@dataclass
class _QueueItem:
    job_id: str
    record: dict[str, Any]
    fn: Callable[[JobExecutionContext], Any]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    on_complete: Callable[[dict[str, Any]], None] | None = None


class GlobalJobManager:
    """단일 워커 기반 비동기 FIFO 배치/리롤 작업 큐."""

    def __init__(
        self,
        *,
        interrupt_fn: Callable[[], Any] | None = None,
        max_history: int = 100,
    ) -> None:
        self._lock = threading.Lock()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._interrupt_fn: Callable[[], Any] = interrupt_fn or interrupt_forge

        self._seq: int = 0
        self._queue: deque[_QueueItem] = deque()
        self._active_item: _QueueItem | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._records_by_id: dict[str, dict[str, Any]] = {}
        self._worker_thread: threading.Thread | None = None

    @staticmethod
    def _serialize(rec: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in rec.items() if k != "_started_ts"}
        out["qa_warnings"] = [dict(w) for w in rec.get("qa_warnings", [])]
        return out

    def _create_record(
        self,
        *,
        kind: str,
        label: str,
        roster: str = "",
        char: str = "",
        codes: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        self._seq += 1
        job_id = f"job-{self._seq}"
        rec: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "label": label or f"{kind}:{char or 'all'}",
            "roster": roster,
            "char": char,
            "codes": codes,
            "status": "queued",
            "current": 0,
            "total": 0,
            "progress": 0.0,
            "message": "대기열 등록됨",
            "qa_warnings": [],
            "error": None,
            "created_at": time.strftime("%H:%M:%S"),
            "started_at": None,
            "finished_at": None,
            "duration_sec": None,
            "_started_ts": None,
        }
        self._history.append(rec)
        self._records_by_id[job_id] = rec
        # 오래된 히스토리 제거에 맞춰 인덱스도 정리
        if len(self._records_by_id) > (self._history.maxlen or 100) + 20:
            live_ids = {r["job_id"] for r in self._history}
            for stale_id in list(self._records_by_id.keys()):
                if stale_id not in live_ids:
                    self._records_by_id.pop(stale_id, None)
        return job_id, rec

    def _ensure_worker_locked(self) -> None:
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._idle_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="KiroJobQueueWorker",
                daemon=True,
            )
            self._worker_thread.start()

    def enqueue_task(
        self,
        *,
        kind: str,
        label: str,
        fn: Callable[[JobExecutionContext], Any],
        roster: str = "",
        char: str = "",
        codes: str | None = None,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """임의 실행 함수를 FIFO 대기열에 등록하고 생성된 작업 레코드 스냅샷을 반환한다."""
        with self._lock:
            job_id, rec = self._create_record(
                kind=kind,
                label=label,
                roster=roster,
                char=char,
                codes=codes,
            )
            item = _QueueItem(
                job_id=job_id,
                record=rec,
                fn=fn,
                on_complete=on_complete,
            )
            self._queue.append(item)
            queue_pos = len(self._queue)
            rec["queue_position"] = queue_pos
            self._ensure_worker_locked()
            return self._serialize(rec)

    def enqueue_reroll(
        self,
        base_dir: Path,
        roster_name: str,
        char_name: str,
        code: str | int,
        *,
        options_overrides: dict[str, Any] | None = None,
        executor: Callable[[GenerationOptions, Any, RunHooks], int] | None = None,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """단일 캐릭터·단일 포즈 리롤(overwrite=True)을 FIFO 큐에 등록한다."""
        code_str = str(code).strip()
        overrides = dict(options_overrides or {})
        run_fn = executor or execute

        def _task(ctx: JobExecutionContext) -> int:
            rpm = RosterPathManager(base_dir)
            roster = rpm.resolve_roster(roster_name)
            db = load_pose_db(base_dir, roster.events_file)
            cfg = load_character(roster.characters_dir, char_name)

            opts = GenerationOptions(
                mode="all",
                codes=code_str,
                overwrite=True,
                naming=str(overrides.get("naming", "hybrid")),
                censor=bool(overrides.get("censor", False)),
                censor_style=str(overrides.get("censor_style", "white_silhouette")),
                auto_qc=bool(overrides.get("auto_qc", False)),
                strict_censor_qc=bool(overrides.get("strict_censor_qc", False)),
                max_retries=int(overrides.get("max_retries", 3)),
                seed=overrides.get("seed"),
                mock=bool(overrides.get("mock", False)),
                no_ref=bool(overrides.get("no_ref", False)),
                steps=overrides.get("steps"),
                cfg_scale=overrides.get("cfg_scale"),
                sampler=overrides.get("sampler"),
                scheduler=overrides.get("scheduler"),
                ckpt_profile=overrides.get("ckpt_profile"),
            )
            apply_character_to_args(cfg, opts, db)

            def _prog(p_code: int, p_label: str, _save_path: Path, cur: int, tot: int) -> None:
                ctx.report_progress(cur, tot, f"[{cfg.prefix}] #{p_code:03d} {p_label}")

            hooks = RunHooks(
                progress_callback=_prog,
                is_cancelled=ctx.is_cancelled,
            )
            rc = run_fn(opts, roster, hooks)
            if hooks.last_result and hooks.last_result.qa_warnings:
                ctx.record_qa_warnings([
                    {"char": cfg.prefix, "code": qc, "label": ql, "reason": qr}
                    for qc, ql, qr in hooks.last_result.qa_warnings
                ])
            if ctx.is_cancelled():
                return rc
            if rc != 0:
                raise RuntimeError(f"리롤 실행 실패 (exit_code={rc})")
            return rc

        return self.enqueue_task(
            kind="reroll",
            label=f"리롤 {char_name}:{code_str}",
            fn=_task,
            roster=roster_name,
            char=char_name,
            codes=code_str,
            on_complete=on_complete,
        )

    def enqueue_batch(
        self,
        base_dir: Path,
        roster_name: str,
        options: GenerationOptions,
        *,
        all_chars: bool = False,
        char_name: str = "",
        label: str = "",
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """단일 또는 전체 캐릭터 배치 생성을 FIFO 큐에 등록한다."""
        def _task(ctx: JobExecutionContext) -> int:
            rpm = RosterPathManager(base_dir)
            roster = rpm.resolve_roster(roster_name)
            hooks = RunHooks(
                progress_callback=lambda c, lbl, _p, cur, tot: ctx.report_progress(
                    cur, tot, f"[{c:03d}] {lbl}"
                ),
                is_cancelled=ctx.is_cancelled,
            )
            rc = (
                run_all_chars(roster, options=options, hooks=hooks)
                if all_chars
                else execute(options, roster, hooks=hooks)
            )
            if hooks.last_result and hooks.last_result.qa_warnings:
                ctx.record_qa_warnings([
                    {"char": options.prefix or char_name, "code": qc, "label": ql, "reason": qr}
                    for qc, ql, qr in hooks.last_result.qa_warnings
                ])
            elif hooks.last_all_result and hooks.last_all_result.qa_issues:
                ctx.record_qa_warnings([
                    {"char": qch, "code": qc, "label": ql, "reason": qr}
                    for qch, qc, ql, qr in hooks.last_all_result.qa_issues
                ])
            if ctx.is_cancelled():
                return rc
            if rc != 0:
                raise RuntimeError(f"배치 실행 실패 (exit_code={rc})")
            return rc

        return self.enqueue_task(
            kind="batch",
            label=label or (f"전체 배치 ({roster_name})" if all_chars else f"배치 {char_name or options.prefix}"),
            fn=_task,
            roster=roster_name,
            char=char_name or options.prefix or ("ALL" if all_chars else ""),
            codes=options.codes or options.mode,
            on_complete=on_complete,
        )

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """특정 job_id(대기 중 또는 실행 중)를 취소한다."""
        to_interrupt = False
        cancelled_item: _QueueItem | None = None

        with self._lock:
            # 1) 현재 실행 중 작업 취소
            if self._active_item is not None and self._active_item.job_id == job_id:
                self._active_item.cancel_event.set()
                self._active_item.record["message"] = "취소 요청됨..."
                to_interrupt = True
                snap = self._serialize(self._active_item.record)
            else:
                # 2) 대기열(queued) 내부 개별 작업 취소
                found_idx = -1
                for idx, item in enumerate(self._queue):
                    if item.job_id == job_id:
                        found_idx = idx
                        break
                if found_idx >= 0:
                    q_list = list(self._queue)
                    cancelled_item = q_list.pop(found_idx)
                    self._queue = deque(q_list)
                    cancelled_item.cancel_event.set()
                    cancelled_item.record["status"] = "cancelled"
                    cancelled_item.record["finished_at"] = time.strftime("%H:%M:%S")
                    cancelled_item.record["message"] = "대기 중 취소됨"
                    snap = self._serialize(cancelled_item.record)
                else:
                    existing = self._records_by_id.get(job_id)
                    if existing is None:
                        raise KeyError(f"작업을 찾을 수 없습니다: {job_id}")
                    return {"cancelled": False, "job": self._serialize(existing)}

        if to_interrupt:
            try:
                self._interrupt_fn()
            except Exception:
                pass
        if cancelled_item is not None and cancelled_item.on_complete is not None:
            try:
                cancelled_item.on_complete(snap)
            except Exception:
                pass
        return {"cancelled": True, "job": snap}

    def request_cancel(self, *, cancel_all_queued: bool = False) -> list[str]:
        """현재 실행 중인 작업(및 옵션 시 전체 대기 작업)에 취소 신호를 전송한다."""
        cancelled_ids: list[str] = []
        dropped_items: list[_QueueItem] = []
        to_interrupt = False

        with self._lock:
            if self._active_item is not None:
                self._active_item.cancel_event.set()
                self._active_item.record["message"] = "취소 요청됨..."
                cancelled_ids.append(self._active_item.job_id)
                to_interrupt = True

            if cancel_all_queued:
                while self._queue:
                    it = self._queue.popleft()
                    it.cancel_event.set()
                    it.record["status"] = "cancelled"
                    it.record["finished_at"] = time.strftime("%H:%M:%S")
                    it.record["message"] = "일괄 취소됨"
                    cancelled_ids.append(it.job_id)
                    dropped_items.append(it)

        if to_interrupt:
            try:
                self._interrupt_fn()
            except Exception:
                pass

        for it in dropped_items:
            if it.on_complete is not None:
                try:
                    it.on_complete(self._serialize(it.record))
                except Exception:
                    pass
        return cancelled_ids

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """특정 job_id 의 최신 상태를 반환한다."""
        with self._lock:
            rec = self._records_by_id.get(job_id)
            return self._serialize(rec) if rec is not None else None

    def get_status(self) -> dict[str, Any]:
        """현재 실행 작업, 대기열 목록, 최근 이력 및 Auto-QC 경고 요약을 반환한다."""
        with self._lock:
            active_snap = (
                self._serialize(self._active_item.record)
                if self._active_item is not None
                else None
            )
            queued_snaps = [self._serialize(item.record) for item in self._queue]
            history_snaps = [self._serialize(rec) for rec in reversed(self._history)]
            recent_qa: list[dict[str, Any]] = []
            for rec in reversed(self._history):
                for w in rec.get("qa_warnings", []):
                    recent_qa.append({"job_id": rec["job_id"], **w})
                    if len(recent_qa) >= 30:
                        break
                if len(recent_qa) >= 30:
                    break

            return {
                "is_running": self._active_item is not None,
                "queue_length": len(queued_snaps),
                "active_job": active_snap,
                "queued_jobs": queued_snaps,
                "history": history_snaps,
                "qa_warnings": recent_qa,
            }

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """대기열과 현재 실행 작업이 모두 완료될 때까지 대기한다 (테스트/종료 동기화용)."""
        return self._idle_event.wait(timeout=timeout)

    def _worker_loop(self) -> None:
        """단일 백그라운드 워커: FIFO 순서로 작업을 하나씩 직렬 소비한다."""
        while True:
            with self._lock:
                if not self._queue:
                    self._active_item = None
                    self._idle_event.set()
                    return
                item = self._queue.popleft()
                self._active_item = item
                item.record["status"] = "running"
                item.record["started_at"] = time.strftime("%H:%M:%S")
                item.record["_started_ts"] = time.perf_counter()
                item.record["message"] = f"실행 중: {item.record['label']}"

            def _on_progress(cur: int, tot: int, msg: str, _rec: dict[str, Any] = item.record) -> None:
                with self._lock:
                    _rec["current"] = int(cur)
                    _rec["total"] = int(tot)
                    _rec["progress"] = (cur / tot) if tot > 0 else 0.0
                    if msg:
                        _rec["message"] = msg

            def _on_qa(warns: list[dict[str, Any]], _rec: dict[str, Any] = item.record) -> None:
                with self._lock:
                    _rec["qa_warnings"].extend(warns)

            ctx = JobExecutionContext(
                job_id=item.job_id,
                _cancel_event=item.cancel_event,
                _on_progress=_on_progress,
                _on_qa_warnings=_on_qa,
            )

            err_obj: Exception | None = None
            try:
                item.fn(ctx)
            except Exception as exc:
                err_obj = exc
                logger.warning("[JobQueue] 작업 실패 (%s): %s", item.job_id, exc)

            with self._lock:
                started_ts = item.record.get("_started_ts")
                item.record["duration_sec"] = (
                    round(time.perf_counter() - started_ts, 2) if started_ts else 0.0
                )
                item.record["finished_at"] = time.strftime("%H:%M:%S")
                if item.cancel_event.is_set():
                    item.record["status"] = "cancelled"
                    item.record["message"] = "취소됨"
                elif err_obj is not None:
                    item.record["status"] = "failed"
                    item.record["error"] = str(err_obj)
                    item.record["message"] = f"실패: {err_obj}"
                else:
                    item.record["status"] = "completed"
                    item.record["progress"] = 1.0
                    item.record["message"] = "완료"
                final_snap = self._serialize(item.record)
                self._active_item = None

            if item.on_complete is not None:
                try:
                    item.on_complete(final_snap)
                except Exception:
                    pass


# 전역 FIFO 작업 큐 싱글톤
global_job_manager = GlobalJobManager()

__all__ = [
    "GlobalJobManager",
    "JobExecutionContext",
    "global_job_manager",
]

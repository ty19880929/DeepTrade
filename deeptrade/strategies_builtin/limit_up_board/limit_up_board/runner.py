"""Plugin-internal run lifecycle: drives the pipeline generator, persists
events to ``lub_events``, and writes the run record to ``lub_runs``.

Replaces the deleted framework-side ``core/strategy_runner.py``: each plugin
manages its own run history on Plan A's pure-isolation model.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, Any

from deeptrade.core.run_status import RunStatus
from deeptrade.core.tushare_client import TushareUnauthorizedError
from deeptrade.plugins_api.events import EventLevel, EventType, StrategyEvent

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.llm_client import LLMClient

from .calendar import TradeCalendar
from .data import Round1Bundle, collect_round1, resolve_trade_date
from .pipeline import (
    run_final_ranking,
    run_r1,
    run_r2,
    select_finalists,
)
from .render import export_llm_calls, render_terminal_summary, write_report
from .runtime import LubRuntime, build_tushare_client, pick_llm_provider
from .schemas import FinalRankingResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run params (replaces deleted StrategyParams)
# ---------------------------------------------------------------------------


@dataclass
class RunParams:
    trade_date: str | None = None
    allow_intraday: bool = False
    force_sync: bool = False
    daily_lookback: int = 10
    moneyflow_lookback: int = 5


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    error: str | None
    seen_events: list[StrategyEvent]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LubRunner:
    """Drives the pipeline generator and persists run / events."""

    def __init__(self, rt: LubRuntime) -> None:
        self._rt = rt
        # Buffer for events emitted by sub-systems (currently TushareClient)
        # and drained between yields in the pipeline.
        self._pending: list[StrategyEvent] = []
        # Selected LLM client for the current run. Bound at execute() entry
        # via rt.llms.get_client(provider_name, ...). Stays None for
        # execute_sync_only().
        self._llm: LLMClient | None = None

    # ----- public --------------------------------------------------------

    def execute(self, params: RunParams) -> RunOutcome:
        run_id = str(uuid.uuid4())
        self._rt.run_id = run_id
        self._rt.is_intraday = params.allow_intraday
        self._rt.tushare = build_tushare_client(
            self._rt, intraday=params.allow_intraday, event_cb=self._on_tushare_event
        )
        from deeptrade.core import paths

        provider_name = pick_llm_provider(self._rt)
        self._llm = self._rt.llms.get_client(
            provider_name,
            plugin_id=self._rt.plugin_id,
            run_id=run_id,
            reports_dir=paths.reports_dir() / run_id,
        )

        self._record_run_start(run_id, params)

        events: list[StrategyEvent] = []
        seen_validation_failed = False
        terminal_status = RunStatus.SUCCESS
        terminal_error: str | None = None

        try:
            seq = 0
            for ev in self._iter_pipeline(params):
                seq += 1
                self._persist_event(run_id, seq, ev)
                events.append(ev)
                self._render_event(ev)
                if ev.type == EventType.VALIDATION_FAILED:
                    seen_validation_failed = True
        except KeyboardInterrupt:
            terminal_status = RunStatus.CANCELLED
            terminal_error = "KeyboardInterrupt"
        except Exception as e:  # noqa: BLE001
            terminal_status = RunStatus.FAILED
            terminal_error = f"{type(e).__name__}: {e}"
            logger.exception("limit-up-board run %s raised", run_id)

        if terminal_status == RunStatus.SUCCESS and seen_validation_failed:
            terminal_status = RunStatus.PARTIAL_FAILED

        self._record_run_finish(run_id, terminal_status, terminal_error, events)
        return RunOutcome(
            run_id=run_id, status=terminal_status, error=terminal_error, seen_events=events
        )

    def execute_sync_only(self, params: RunParams) -> RunOutcome:
        """Data-only path: same lifecycle as execute() but yields via _iter_sync."""
        run_id = str(uuid.uuid4())
        self._rt.run_id = run_id
        self._rt.is_intraday = params.allow_intraday
        self._rt.tushare = build_tushare_client(
            self._rt, intraday=params.allow_intraday, event_cb=self._on_tushare_event
        )

        self._record_run_start(run_id, params)
        events: list[StrategyEvent] = []
        terminal_status = RunStatus.SUCCESS
        terminal_error: str | None = None

        try:
            seq = 0
            for ev in self._iter_sync(params):
                seq += 1
                self._persist_event(run_id, seq, ev)
                events.append(ev)
                self._render_event(ev)
        except KeyboardInterrupt:
            terminal_status = RunStatus.CANCELLED
            terminal_error = "KeyboardInterrupt"
        except Exception as e:  # noqa: BLE001
            terminal_status = RunStatus.FAILED
            terminal_error = f"{type(e).__name__}: {e}"
            logger.exception("limit-up-board sync %s raised", run_id)

        self._record_run_finish(run_id, terminal_status, terminal_error, events)
        return RunOutcome(
            run_id=run_id, status=terminal_status, error=terminal_error, seen_events=events
        )

    # ----- pipeline iteration -------------------------------------------

    def _iter_sync(self, params: RunParams) -> Iterable[StrategyEvent]:
        """Data-only iteration (no LLM stages)."""
        rt = self._rt
        cfg = rt.config.get_app_config()

        yield rt.emit(EventType.STEP_STARTED, "Step 0: resolve trade date")
        cal_df = rt.tushare.call("trade_cal")  # type: ignore[union-attr]
        cal = TradeCalendar(cal_df)
        T, T1 = resolve_trade_date(
            datetime.now(),
            cal,
            user_specified=params.trade_date,
            allow_intraday=params.allow_intraday,
            close_after=cfg.app_close_after if cfg is not None else time(18, 0),
        )
        yield rt.emit(
            EventType.STEP_FINISHED,
            f"Step 0: T={T} T+1={T1}",
            payload={"trade_date": T, "next_trade_date": T1},
        )

        yield rt.emit(EventType.DATA_SYNC_STARTED, "Step 1: data assembly")
        bundle = collect_round1(
            tushare=rt.tushare,  # type: ignore[arg-type]
            trade_date=T,
            next_trade_date=T1,
            daily_lookback=params.daily_lookback,
            moneyflow_lookback=params.moneyflow_lookback,
            force_sync=params.force_sync,
        )
        yield from self._drain_pending()
        yield rt.emit(
            EventType.DATA_SYNC_FINISHED,
            f"synced {len(bundle.candidates)} candidates",
            payload={"candidates": len(bundle.candidates), "data_unavailable": bundle.data_unavailable},
        )

    def _iter_pipeline(self, params: RunParams) -> Iterable[StrategyEvent]:
        """Full pipeline: Step 0..5."""
        rt = self._rt
        cfg = rt.config.get_app_config()

        # Step 0
        yield rt.emit(EventType.STEP_STARTED, "Step 0: resolve trade date")
        cal_df = rt.tushare.call("trade_cal")  # type: ignore[union-attr]
        cal = TradeCalendar(cal_df)
        now = datetime.now()
        T, T1 = resolve_trade_date(
            now,
            cal,
            user_specified=params.trade_date,
            allow_intraday=params.allow_intraday,
            close_after=cfg.app_close_after if cfg is not None else time(18, 0),
        )
        today_str = now.strftime("%Y%m%d")
        auto_resolved_to_today_after_close = (
            params.trade_date is None and not params.allow_intraday and T == today_str
        )
        yield rt.emit(
            EventType.STEP_FINISHED,
            f"Step 0: T={T} T+1={T1}",
            payload={"trade_date": T, "next_trade_date": T1},
        )

        # Step 1
        yield rt.emit(EventType.STEP_STARTED, "Step 1: data assembly")
        try:
            bundle = collect_round1(
                tushare=rt.tushare,  # type: ignore[arg-type]
                trade_date=T,
                next_trade_date=T1,
                daily_lookback=params.daily_lookback,
                moneyflow_lookback=params.moneyflow_lookback,
                force_sync=params.force_sync,
            )
        except TushareUnauthorizedError as e:
            yield rt.emit(
                EventType.LOG, f"required tushare api unauthorized: {e}", level=EventLevel.ERROR
            )
            raise
        yield from self._drain_pending()
        yield rt.emit(
            EventType.STEP_FINISHED,
            f"Step 1: {len(bundle.candidates)} candidates",
            payload={
                "candidates": len(bundle.candidates),
                "data_unavailable": bundle.data_unavailable,
                "sector_strength_source": bundle.sector_strength.source,
            },
        )

        if not bundle.candidates and auto_resolved_to_today_after_close:
            raise RuntimeError(
                f"limit_list_d({T}) returned 0 rows after close_after — tushare "
                "data may not be published yet. Try again later, or use "
                "`--trade-date <YYYYMMDD>` to specify a known historical day."
            )

        if not bundle.candidates:
            yield from self._emit_empty_report(bundle, params)
            return

        # Step 2 — R1
        preset = cfg.app_profile  # v0.7: per-stage tuning resolved by plugin
        r1_result = None
        for ev, res in run_r1(llm=self._llm, bundle=bundle, preset=preset):
            yield ev
            if res is not None:
                r1_result = res
        selected = r1_result.selected if r1_result else []
        if not selected:
            yield from self._emit_empty_report(bundle, params, reason="no R1 selected")
            return

        # Step 4 — R2
        r2_result = None
        for ev, res in run_r2(
            llm=self._llm, selected=selected, bundle=bundle, preset=preset
        ):
            yield ev
            if res is not None:
                r2_result = res
        predictions = r2_result.predictions if r2_result else []

        # Step 4.5 — final_ranking when R2 was multi-batch
        final_obj: FinalRankingResponse | None = None
        final_ranking_attempted = False
        if r2_result and r2_result.success_batches > 1 and predictions:
            final_ranking_attempted = True
            finalists = select_finalists(predictions, batch_size_hint=r2_result.batch_size or 20)
            for ev, fr_obj in run_final_ranking(
                llm=self._llm,
                bundle=bundle,
                finalists=finalists,
                preset=preset,
            ):
                yield ev
                if fr_obj is not None:
                    final_obj = fr_obj

        # Step 5 — finalize
        terminal_status = RunStatus.SUCCESS
        if r1_result and r1_result.failed_batches > 0:
            terminal_status = RunStatus.PARTIAL_FAILED
        if r2_result and r2_result.failed_batches > 0:
            terminal_status = RunStatus.PARTIAL_FAILED
        if final_ranking_attempted and final_obj is None:
            terminal_status = RunStatus.PARTIAL_FAILED

        _write_stage_results(rt, "r1", selected)
        _write_stage_results(rt, "r2", predictions)
        if final_obj is not None:
            _write_stage_results(rt, "final_ranking", final_obj.finalists)

        failed_batches: list[str] = []
        if r1_result and r1_result.failed_batch_ids:
            failed_batches.extend(f"R1#{b}" for b in r1_result.failed_batch_ids)
        if r2_result and r2_result.failed_batch_ids:
            failed_batches.extend(f"R2#{b}" for b in r2_result.failed_batch_ids)
        if final_ranking_attempted and final_obj is None:
            failed_batches.append("final_ranking")

        report_path = write_report(
            rt.run_id,
            status=terminal_status,
            is_intraday=params.allow_intraday,
            bundle=bundle,
            selected=selected,
            predictions=predictions,
            final_ranking=final_obj,
            failed_batch_ids=failed_batches or None,
        )
        export_llm_calls(rt.run_id, rt.db)
        yield rt.emit(
            EventType.RESULT_PERSISTED,
            f"Report written: {report_path}",
            payload={
                "report_dir": str(report_path),
                "selected": len(selected),
                "predictions": len(predictions),
                "final_ranking_used": final_obj is not None,
            },
        )

    # ----- helpers ------------------------------------------------------

    def _emit_empty_report(
        self, bundle: Round1Bundle, params: RunParams, *, reason: str = "zero candidates"
    ) -> Iterable[StrategyEvent]:
        rt = self._rt
        report_path = write_report(
            rt.run_id,
            status=RunStatus.SUCCESS,
            is_intraday=params.allow_intraday,
            bundle=bundle,
            selected=[],
            predictions=[],
            final_ranking=None,
        )
        export_llm_calls(rt.run_id, rt.db)
        yield rt.emit(
            EventType.RESULT_PERSISTED,
            f"empty report ({reason})",
            payload={"report_dir": str(report_path), "reason": reason},
        )

    def _on_tushare_event(self, event_type: str, message: str, payload: dict) -> None:
        try:
            etype = EventType(event_type)
        except ValueError:
            logger.warning("unknown tushare event type: %s", event_type)
            return
        self._pending.append(
            StrategyEvent(type=etype, level=EventLevel.WARN, message=message, payload=payload)
        )

    def _drain_pending(self) -> Iterable[StrategyEvent]:
        while self._pending:
            yield self._pending.pop(0)

    def _render_event(self, ev: StrategyEvent) -> None:
        """Print events to stdout as they happen (replacement for the deleted TUI)."""
        glyph = "✔" if ev.level == EventLevel.INFO else ("⚠" if ev.level == EventLevel.WARN else "✘")
        print(f"  {glyph} [{ev.type.value}] {ev.message}", flush=True)

    # ----- DB helpers ---------------------------------------------------

    def _record_run_start(self, run_id: str, params: RunParams) -> None:
        self._rt.db.execute(
            "INSERT INTO lub_runs(run_id, trade_date, status, is_intraday, started_at, "
            "params_json) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (
                run_id,
                params.trade_date or "",
                RunStatus.RUNNING.value,
                params.allow_intraday,
                json.dumps(params.__dict__, ensure_ascii=False),
            ),
        )

    def _record_run_finish(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None,
        events: list[StrategyEvent],
    ) -> None:
        summary = {
            "event_count": len(events),
            "validation_failed_count": sum(
                1 for e in events if e.type == EventType.VALIDATION_FAILED
            ),
        }
        self._rt.db.execute(
            "UPDATE lub_runs SET status=?, finished_at=CURRENT_TIMESTAMP, "
            "summary_json=?, error=? WHERE run_id=?",
            (status.value, json.dumps(summary, ensure_ascii=False), error, run_id),
        )

    def _persist_event(self, run_id: str, seq: int, ev: StrategyEvent) -> None:
        self._rt.db.execute(
            "INSERT INTO lub_events(run_id, seq, level, event_type, message, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                seq,
                ev.level.value,
                ev.type.value,
                ev.message,
                json.dumps(ev.payload, ensure_ascii=False, default=str),
            ),
        )


def _write_stage_results(rt: LubRuntime, stage: str, items: list[Any]) -> None:
    """Persist R1/R2/final_ranking outputs to lub_stage_results."""
    if not items:
        return
    for i, item in enumerate(items):
        d = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        rt.db.execute(
            "INSERT INTO lub_stage_results(run_id, stage, batch_no, trade_date, ts_code, "
            "name, score, rank, decision, rationale, evidence_json, risk_flags_json, "
            "raw_response_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rt.run_id,
                stage,
                d.get("batch_no", 0),
                d.get("trade_date", ""),
                d.get("ts_code", ""),
                d.get("name"),
                d.get("score") or d.get("continuation_score"),
                d.get("rank") or d.get("final_rank") or i + 1,
                d.get("decision") or d.get("prediction") or d.get("final_prediction"),
                d.get("rationale") or d.get("reason_vs_peers"),
                json.dumps(d.get("evidence") or d.get("key_evidence") or [], ensure_ascii=False),
                json.dumps(d.get("risk_flags") or [], ensure_ascii=False),
                json.dumps(d, ensure_ascii=False),
            ),
        )


def render_finished_run(run_id: str) -> None:
    """Re-render a finished run's terminal summary (used by `report` subcommand)."""
    render_terminal_summary(run_id)

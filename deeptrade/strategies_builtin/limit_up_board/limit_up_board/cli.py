"""Plugin-managed CLI for limit-up-board.

Subcommands:
    run     — full pipeline (Step 0..5)
    sync    — data-only path (no LLM)
    history — list recent runs
    report  — re-render a finished run's terminal summary

Invoked via the framework's pure pass-through dispatch:
    deeptrade limit-up-board <subcommand> [...]
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from deeptrade.core import paths
from deeptrade.core.config import ConfigService
from deeptrade.core.db import Database

from .runner import LubRunner, RunParams, render_finished_run
from .runtime import LubRuntime

app = typer.Typer(
    name="limit-up-board",
    help="打板策略 — A 股涨停板双轮 LLM 漏斗。",
    no_args_is_help=True,
    add_completion=False,
)


def _open_runtime() -> tuple[Database, LubRuntime]:
    db = Database(paths.db_path())
    rt = LubRuntime(db=db, config=ConfigService(db))
    return db, rt


@app.command("run")
def cmd_run(
    trade_date: Optional[str] = typer.Option(None, "--trade-date", help="YYYYMMDD"),
    allow_intraday: bool = typer.Option(False, "--allow-intraday"),
    force_sync: bool = typer.Option(False, "--force-sync"),
    daily_lookback: int = typer.Option(10, "--daily-lookback"),
    moneyflow_lookback: int = typer.Option(5, "--moneyflow-lookback"),
) -> None:
    """Run the full打板策略 pipeline."""
    db, rt = _open_runtime()
    try:
        params = RunParams(
            trade_date=trade_date,
            allow_intraday=allow_intraday,
            force_sync=force_sync,
            daily_lookback=daily_lookback,
            moneyflow_lookback=moneyflow_lookback,
        )
        runner = LubRunner(rt)
        outcome = runner.execute(params)
        typer.echo(f"\nstatus: {outcome.status.value}  run_id: {outcome.run_id}")
        if outcome.error:
            typer.echo(f"error: {outcome.error}")
        if outcome.status.value not in {"success", "partial_failed"}:
            raise typer.Exit(1)
        # Print the terminal summary right after a successful run.
        render_finished_run(outcome.run_id)
    finally:
        db.close()


@app.command("sync")
def cmd_sync(
    trade_date: Optional[str] = typer.Option(None, "--trade-date", help="YYYYMMDD"),
    allow_intraday: bool = typer.Option(False, "--allow-intraday"),
    force_sync: bool = typer.Option(False, "--force-sync"),
    daily_lookback: int = typer.Option(10, "--daily-lookback"),
    moneyflow_lookback: int = typer.Option(5, "--moneyflow-lookback"),
) -> None:
    """Fetch + persist data only (no LLM stages)."""
    db, rt = _open_runtime()
    try:
        params = RunParams(
            trade_date=trade_date,
            allow_intraday=allow_intraday,
            force_sync=force_sync,
            daily_lookback=daily_lookback,
            moneyflow_lookback=moneyflow_lookback,
        )
        runner = LubRunner(rt)
        outcome = runner.execute_sync_only(params)
        typer.echo(f"\nstatus: {outcome.status.value}  run_id: {outcome.run_id}")
        if outcome.error:
            typer.echo(f"error: {outcome.error}")
        if outcome.status.value != "success":
            raise typer.Exit(1)
    finally:
        db.close()


@app.command("history")
def cmd_history(limit: int = typer.Option(20, "--limit")) -> None:
    """List recent runs of this plugin."""
    db = Database(paths.db_path())
    try:
        rows = db.fetchall(
            "SELECT run_id, trade_date, status, started_at, finished_at FROM lub_runs "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
    finally:
        db.close()
    if not rows:
        typer.echo("(no runs)")
        return
    for r in rows:
        typer.echo(f"{r[0]}  {r[1]:<10}  {r[2]:<15}  {r[3]} → {r[4] or '-'}")


@app.command("report")
def cmd_report(
    run_id: str = typer.Argument(..., help="Run UUID to view"),
    full: bool = typer.Option(
        False, "--full", help="Print the full markdown summary instead of the concise view."
    ),
) -> None:
    """Re-display a finished run's report."""
    if full:
        from rich.console import Console
        from rich.markdown import Markdown

        from deeptrade.theme import EVA_THEME

        report_dir = paths.reports_dir() / run_id
        summary = report_dir / "summary.md"
        if not summary.is_file():
            typer.echo(f"✘ no report at {summary}")
            raise typer.Exit(2)
        Console(theme=EVA_THEME).print(Markdown(summary.read_text(encoding="utf-8")))
        typer.echo(f"\nReport directory: {summary.parent}")
        return
    render_finished_run(run_id)


def main(argv: list[str]) -> int:
    """Plugin's dispatch entry. Returns exit code."""
    try:
        app(argv, standalone_mode=False)
        return 0
    except typer.Exit as e:
        return int(e.exit_code or 0)
    except SystemExit as e:
        try:
            return int(e.code or 0)
        except (TypeError, ValueError):
            return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n✘ cancelled by user\n")
        return 130
    except Exception as e:  # noqa: BLE001 — reflect to framework as exit 1
        sys.stderr.write(f"✘ {type(e).__name__}: {e}\n")
        return 1

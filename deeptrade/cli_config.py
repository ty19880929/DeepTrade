"""`deeptrade config` subcommand group.

V0.2 wires show/set/test. V0.3/V0.4 will replace the inline `test` probes
with proper TushareClient / DeepSeekClient calls.
"""

from __future__ import annotations

import questionary
import typer
from rich.console import Console
from rich.table import Table

from deeptrade.core import paths
from deeptrade.core.config import (
    ConfigService,
    known_keys,
)
from deeptrade.core.db import Database

app = typer.Typer(help="View / edit configuration", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_service() -> tuple[Database, ConfigService]:
    """Open the local DuckDB + ConfigService. Caller must close db."""
    db = Database(paths.db_path())
    return db, ConfigService(db)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@app.command("show")
def cmd_show() -> None:
    """List all known config keys with values (secrets masked) and source."""
    db, svc = _open_service()
    try:
        console = Console()
        table = Table(title="DeepTrade Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value", overflow="fold")
        table.add_column("Source", style="yellow")
        for key, value, source in svc.list_all():
            display = "" if value is None else str(value)
            table.add_row(key, display, source)
        console.print(table)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


@app.command("set")
def cmd_set(
    key: str = typer.Argument(..., help="Dotted key (e.g. `deepseek.profile`)"),
    value: str = typer.Argument(..., help="Value (string-coerced)"),
) -> None:
    """Set a single config key to a value (scriptable form)."""
    db, svc = _open_service()
    try:
        if key not in known_keys():
            typer.echo(f"Unknown key: {key!r}; valid keys:\n  " + "\n  ".join(known_keys()))
            raise typer.Exit(2)
        try:
            svc.set(key, value)
        except ValueError as e:
            typer.echo(f"Invalid value for {key!r}: {e}")
            raise typer.Exit(2) from e
        typer.echo(f"✔ Saved {key}")
    finally:
        db.close()


@app.command("set-tushare")
def cmd_set_tushare() -> None:
    """Interactive Tushare configuration."""
    db, svc = _open_service()
    try:
        cur = svc.get_app_config()
        token = questionary.password("Tushare token:").ask()
        if token is None:
            raise typer.Exit(1)
        rps_input = questionary.text(
            f"Tushare RPS [{cur.tushare_rps}]:",
            default=str(cur.tushare_rps),
        ).ask()
        timeout_input = questionary.text(
            f"Tushare timeout (s) [{cur.tushare_timeout}]:",
            default=str(cur.tushare_timeout),
        ).ask()
        if token:
            svc.set("tushare.token", token)
        try:
            svc.set("tushare.rps", float(rps_input))
            svc.set("tushare.timeout", int(timeout_input))
        except (ValueError, TypeError) as e:
            typer.echo(f"Invalid number: {e}")
            raise typer.Exit(2) from e
        typer.echo("✔ Saved tushare config")
    finally:
        db.close()


@app.command("set-deepseek")
def cmd_set_deepseek() -> None:
    """Interactive DeepSeek configuration."""
    db, svc = _open_service()
    try:
        cur = svc.get_app_config()
        api_key = questionary.password("DeepSeek API key:").ask()
        if api_key is None:
            raise typer.Exit(1)
        base_url = questionary.text(
            f"Base URL [{cur.deepseek_base_url}]:",
            default=cur.deepseek_base_url,
        ).ask()
        model = questionary.text(
            f"Model [{cur.deepseek_model}]:",
            default=cur.deepseek_model,
        ).ask()
        profile = questionary.select(
            f"Profile [{cur.deepseek_profile}]:",
            choices=["fast", "balanced", "quality"],
            default=cur.deepseek_profile,
        ).ask()
        if profile is None:
            raise typer.Exit(1)
        if api_key:
            svc.set("deepseek.api_key", api_key)
        svc.set("deepseek.base_url", base_url or cur.deepseek_base_url)
        svc.set("deepseek.model", model or cur.deepseek_model)
        svc.set("deepseek.profile", profile)
        typer.echo("✔ Saved deepseek config")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def _test_tushare(svc: ConfigService, db) -> tuple[bool, str]:  # noqa: ANN001
    """B3.6 — go through the production TushareClient so the call benefits from
    rate limiting, error translation, audit logging."""
    token = svc.get("tushare.token")
    if not token:
        return False, "tushare.token not configured"
    try:
        import time as _time  # noqa: PLC0415

        from deeptrade.core.tushare_client import (  # noqa: PLC0415
            FRAMEWORK_PLUGIN_ID,
            TushareClient,
            TushareSDKTransport,
        )

        cfg = svc.get_app_config()
        transport = TushareSDKTransport(str(token))
        client = TushareClient(
            db, transport, plugin_id=FRAMEWORK_PLUGIN_ID, rps=cfg.tushare_rps
        )
        t0 = _time.time()
        df = client.call("stock_basic", fields="ts_code")
        latency_ms = int((_time.time() - t0) * 1000)
        rows = 0 if df is None else len(df)
        return True, f"stock_basic returned {rows} rows in {latency_ms}ms"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _test_deepseek(svc: ConfigService, db) -> tuple[bool, str]:  # noqa: ANN001
    """B3.6 — go through DeepSeekClient with a stage-aware 1-token echo so the
    production no-tools / JSON-mode constraints are exercised."""
    api_key = svc.get("deepseek.api_key")
    if not api_key:
        return False, "deepseek.api_key not configured"
    try:
        import time as _time  # noqa: PLC0415

        from pydantic import BaseModel, ConfigDict  # noqa: PLC0415

        from deeptrade.core.deepseek_client import (  # noqa: PLC0415
            DeepSeekClient,
            OpenAIClientTransport,
        )

        class _Echo(BaseModel):
            model_config = ConfigDict(extra="forbid")
            ok: bool

        cfg = svc.get_app_config()
        profiles = svc.get_profile()
        transport = OpenAIClientTransport(
            api_key=str(api_key),
            base_url=cfg.deepseek_base_url,
            timeout=cfg.deepseek_timeout,
        )
        client = DeepSeekClient(db, transport, model=cfg.deepseek_model, profiles=profiles)
        t0 = _time.time()
        client.complete_json(
            system='Reply ONLY with this JSON: {"ok": true}',
            user="ping",
            schema=_Echo,
            stage="final_ranking",  # cheapest stage profile
        )
        latency_ms = int((_time.time() - t0) * 1000)
        return True, f"echo ok ({latency_ms}ms)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


@app.command("test")
def cmd_test() -> None:
    """End-to-end connectivity check via the production clients (B3.6)."""
    db, svc = _open_service()
    try:
        ok_t, msg_t = _test_tushare(svc, db)
        marker_t = "✔" if ok_t else "✘"
        typer.echo(f"{marker_t} Tushare: {msg_t}")
        ok_d, msg_d = _test_deepseek(svc, db)
        marker_d = "✔" if ok_d else "✘"
        typer.echo(f"{marker_d} DeepSeek: {msg_d}")
        if not (ok_t and ok_d):
            raise typer.Exit(1)
    finally:
        db.close()


# Note: cmd_set passes raw strings; Pydantic field validators in AppConfig
# coerce strings → int / float / time as needed.

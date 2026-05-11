"""`deeptrade config` subcommand group.

v0.6 — multi-provider LLM (DESIGN §0.7 / §10):

    * ``set-deepseek`` removed; replaced by ``set-llm`` (interactive new /
      edit / delete) + ``list-llm``.
    * ``config test`` replaced by ``test-llm [name]`` (provider-targeted).
    * ``show`` expands ``llm.providers`` so each provider's api_key gets its
      own masked row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # pragma: no cover
    pass

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
    key: str = typer.Argument(..., help="Dotted key (e.g. `app.profile`)"),
    value: str = typer.Argument(..., help="Value (string-coerced)"),
) -> None:
    """Set a single config key to a value (scriptable form).

    For multi-provider LLM keys (``llm.<name>.*``), prefer
    ``deeptrade config set-llm`` — it walks you through the full provider
    record interactively.
    """
    db, svc = _open_service()
    try:
        from deeptrade.core.config import is_secret_key  # noqa: PLC0415

        if key not in known_keys() and not is_secret_key(key):
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


# ---------------------------------------------------------------------------
# LLM provider management (v0.6)
# ---------------------------------------------------------------------------


_DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
}


@app.command("set-llm")
def cmd_set_llm() -> None:
    """Interactive LLM provider management (new / edit / delete).

    Walks the user through the full provider record (name + api_key +
    base_url + model + timeout). Each provider stored in ``llm.providers``
    is independently usable; plugins pick by name via
    ``LLMManager.get_client(name=...)``.
    """
    db, svc = _open_service()
    try:
        cfg = svc.get_app_config()
        existing = sorted(cfg.llm_providers.keys())

        if existing:
            choices = (
                ["[+] Add new provider"]
                + [f"[~] {n}" for n in existing]
                + ["[x] Delete a provider"]
            )
            picked = questionary.select("Pick action:", choices=choices).ask()
            if picked is None:
                raise typer.Exit(1)
            if picked.startswith("[+]"):
                _set_llm_new(svc)
            elif picked.startswith("[x]"):
                _set_llm_delete(svc, existing)
            else:
                name = picked[4:]  # strip "[~] "
                _set_llm_edit(svc, name)
        else:
            typer.echo("No LLM providers configured yet — let's add the first one.")
            _set_llm_new(svc)
    finally:
        db.close()


def _set_llm_new(svc: ConfigService) -> None:
    name = questionary.text("Provider name (e.g. deepseek, qwen-plus, kimi):").ask()
    if not name:
        raise typer.Exit(1)
    name = name.strip()
    if "." in name:
        typer.echo(f"Invalid provider name: {name!r} (must not contain '.')")
        raise typer.Exit(2)
    cfg = svc.get_app_config()
    if name in cfg.llm_providers:
        typer.echo(f"Provider {name!r} already exists; pick edit instead.")
        raise typer.Exit(2)
    default_base = _DEFAULT_BASE_URLS.get(name.split("-")[0], "")
    # Adding into an empty dict auto-promotes to default; offer the choice
    # only when a default is already in place.
    promote_default: bool | None = None
    if cfg.llm_providers:
        promote_default = bool(
            questionary.confirm(
                f"Set {name!r} as the default LLM provider?",
                default=False,
            ).ask()
        )
    _prompt_and_save_provider(
        svc,
        name,
        defaults=None,
        default_base_url=default_base,
        is_default=promote_default,
    )


def _set_llm_edit(svc: ConfigService, name: str) -> None:
    cfg = svc.get_app_config()
    cur = cfg.llm_providers[name]
    _prompt_and_save_provider(
        svc,
        name,
        defaults=cur.model_dump(),
        default_base_url=cur.base_url,
        is_default=None,
    )


def _prompt_and_save_provider(
    svc: ConfigService,
    name: str,
    *,
    defaults: dict | None,
    default_base_url: str,
    is_default: bool | None = None,
) -> None:
    base_url_default = defaults.get("base_url", default_base_url) if defaults else default_base_url
    model_default = defaults.get("model", "") if defaults else ""
    timeout_default = defaults.get("timeout", 180) if defaults else 180

    base_url = questionary.text(
        f"Base URL [{base_url_default}]:",
        default=base_url_default,
    ).ask()
    if not base_url:
        raise typer.Exit(1)
    model = questionary.text(
        f"Model name [{model_default}]:",
        default=model_default,
    ).ask()
    if not model:
        raise typer.Exit(1)
    timeout_input = questionary.text(
        f"Timeout (s) [{timeout_default}]:",
        default=str(timeout_default),
    ).ask()
    if timeout_input is None:
        raise typer.Exit(1)
    try:
        timeout = int(timeout_input)
    except ValueError as e:
        typer.echo(f"Invalid timeout: {e}")
        raise typer.Exit(2) from e

    api_key_prompt = (
        "API key (leave empty to keep existing):" if defaults is not None else "API key:"
    )
    api_key = questionary.password(api_key_prompt).ask()
    if api_key is None:
        raise typer.Exit(1)

    try:
        svc.set_llm_provider(
            name,
            base_url=base_url,
            model=model,
            timeout=timeout,
            api_key=api_key if api_key else None,
            is_default=is_default,
        )
    except ValueError as e:
        typer.echo(f"Invalid provider: {e}")
        raise typer.Exit(2) from e

    if defaults is None and not api_key:
        typer.echo(
            f"⚠ Saved provider {name!r} but no api_key was set — it won't appear "
            "in `list-llm` until you run set-llm again to add the key."
        )
    else:
        typer.echo(f"✔ Saved LLM provider {name!r}")


def _set_llm_delete(svc: ConfigService, existing: list[str]) -> None:
    name = questionary.select("Pick provider to delete:", choices=existing).ask()
    if name is None:
        raise typer.Exit(1)
    confirm = questionary.confirm(
        f"Delete provider {name!r} (and its api_key)?", default=False
    ).ask()
    if not confirm:
        raise typer.Exit(1)
    prior_default = svc.get_default_llm_provider()
    svc.delete_llm_provider(name)
    typer.echo(f"✔ Deleted LLM provider {name!r}")
    new_default = svc.get_default_llm_provider()
    if prior_default == name and new_default is not None:
        typer.echo(f"✔ Default LLM provider auto-switched to {new_default!r}")


@app.command("set-default-llm")
def cmd_set_default_llm(
    name: str = typer.Argument(..., help="Provider name to mark as default."),
) -> None:
    """Mark ``name`` as the default LLM provider.

    The default is consumed by ``LLMManager.get_client()`` when callers
    don't name a provider (non-debate plugin path). Switching default
    clears the flag on every other provider so the
    "exactly-one-default" invariant holds.
    """
    db, svc = _open_service()
    try:
        cfg = svc.get_app_config()
        provider = cfg.llm_providers.get(name)
        if provider is None:
            typer.echo(
                f"Unknown LLM provider: {name!r}; configured providers: "
                + (", ".join(sorted(cfg.llm_providers.keys())) or "(none)")
            )
            raise typer.Exit(2)
        if provider.is_default:
            typer.echo(f"{name!r} is already the default LLM provider")
            return
        svc.set_llm_provider(
            name,
            base_url=provider.base_url,
            model=provider.model,
            timeout=provider.timeout,
            is_default=True,
        )
        typer.echo(f"✔ Default LLM provider set to {name!r}")
    finally:
        db.close()


@app.command("list-llm")
def cmd_list_llm() -> None:
    """List all configured LLM providers (those with ``api_key`` set).

    Mirrors ``LLMManager.list_providers()`` — what plugins will see.
    """
    db, _svc = _open_service()
    try:
        from deeptrade.core.config import ConfigService  # noqa: PLC0415
        from deeptrade.core.llm_manager import LLMManager  # noqa: PLC0415

        cfg = ConfigService(db)
        mgr = LLMManager(db, cfg)
        names = mgr.list_providers()
        if not names:
            typer.echo("(no LLM providers configured; run `deeptrade config set-llm`)")
            return

        console = Console()
        table = Table(title="LLM Providers")
        table.add_column("Name", style="cyan")
        table.add_column("Model", overflow="fold")
        table.add_column("Base URL", overflow="fold")
        for name in names:
            info = mgr.get_provider_info(name)
            table.add_row(info.name, info.model, info.base_url)
        console.print(table)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# test-llm
# ---------------------------------------------------------------------------


@app.command("test-llm")
def cmd_test_llm(
    name: str | None = typer.Argument(
        None,
        help="Provider name to test; omit to test every configured provider.",
    ),
) -> None:
    """Connectivity check via the production ``LLMClient`` for one or all providers.

    Each test sends a tiny JSON-mode echo through the cheapest stage profile
    (``final_ranking``) so the no-tools / JSON-mode constraints are exercised.
    """
    db, _svc = _open_service()
    try:
        from deeptrade.core.config import ConfigService  # noqa: PLC0415
        from deeptrade.core.llm_manager import LLMManager  # noqa: PLC0415

        cfg = ConfigService(db)
        mgr = LLMManager(db, cfg)

        if name is not None:
            targets = [name]
        else:
            targets = mgr.list_providers()
            if not targets:
                typer.echo("(no LLM providers configured; run `deeptrade config set-llm`)")
                raise typer.Exit(1)

        any_failed = False
        for target in targets:
            ok, msg = _test_one_llm(mgr, target)
            marker = "✔" if ok else "✘"
            typer.echo(f"{marker} LLM[{target}]: {msg}")
            if not ok:
                any_failed = True
        if any_failed:
            raise typer.Exit(1)
    finally:
        db.close()


def _test_one_llm(mgr, target: str) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Echo a 1-token JSON via a minimal StageProfile through the production client."""
    import time as _time  # noqa: PLC0415

    from pydantic import BaseModel, ConfigDict  # noqa: PLC0415

    from deeptrade.core.llm_manager import LLMNotConfiguredError  # noqa: PLC0415
    from deeptrade.plugins_api import StageProfile  # noqa: PLC0415

    class _Echo(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ok: bool

    try:
        client = mgr.get_client(target, plugin_id="__framework__", run_id=None)
    except LLMNotConfiguredError as e:
        return False, str(e)

    # v0.7: framework owns no profile presets; supply a minimal echo-friendly
    # profile directly. thinking off + tiny output cap keeps the test cheap.
    echo_profile = StageProfile(
        thinking=False, reasoning_effort="low", temperature=0.0, max_output_tokens=1024
    )
    try:
        t0 = _time.time()
        client.complete_json(
            system='Reply ONLY with this JSON: {"ok": true}',
            user="ping",
            schema=_Echo,
            profile=echo_profile,
        )
        latency_ms = int((_time.time() - t0) * 1000)
        return True, f"echo ok ({latency_ms}ms)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# Note: cmd_set passes raw strings; Pydantic field validators in AppConfig
# coerce strings → int / float / time as needed.

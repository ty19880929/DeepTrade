"""`deeptrade data` subcommand group.

Currently a placeholder: the previous implementation depended on framework
assets (StrategyContext / StrategyParams / StrategyRunner) that were removed
in the v0.5 framework reshape. The ``data sync`` capability will be restored
in the next iteration as part of the per-plugin data ownership model
(see docs/plugin_cli_dispatch_evaluation.md §6).
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Sync data for a plugin without running its main pipeline.",
    no_args_is_help=True,
)


@app.command("sync")
def cmd_sync() -> None:
    """Stub — temporarily disabled while the data layer is being refactored.

    Run the plugin's own sync command directly, e.g.
    ``deeptrade <plugin_id> sync ...``.
    """
    typer.echo(
        "✘ `deeptrade data sync` is temporarily disabled while the data layer "
        "is being refactored.\n"
        "  Use the plugin's own sync command instead, e.g. "
        "`deeptrade <plugin_id> sync ...`."
    )
    raise typer.Exit(2)

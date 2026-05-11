"""Framework-level smoke tests.

Covers the always-on slice: --version / --help / unknown-command routing /
data sync stub. Plugin-side dispatch is exercised by tests/cli/test_routing.py.
"""

from __future__ import annotations

from typer.testing import CliRunner

from deeptrade import __version__
from deeptrade.cli import app

runner = CliRunner()


def test_version_output() -> None:
    """`deeptrade --version` should print the package version and exit 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_only_framework_commands() -> None:
    """`deeptrade --help` must enumerate framework commands only — never plugin
    subcommands. Plugin commands surface via `deeptrade <plugin_id> --help`."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Framework commands appear
    for cmd in ("init", "config", "plugin", "data"):
        assert cmd in result.stdout
    # Strategy command group is gone (plugins surface via `deeptrade <plugin_id>`)
    assert "strategy" not in result.stdout.lower()

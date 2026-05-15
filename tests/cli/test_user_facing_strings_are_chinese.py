"""v0.5 T16 — user-facing CLI strings are Chinese.

CLAUDE.md locks the localization split: text the user sees on the terminal
is Chinese, while raised exceptions / logger messages / source comments
stay English so bug reports keep travelling well across language
communities.

These tests are intentionally narrow keyword assertions rather than
full-string matches: any future copy edit that keeps the Chinese intact
will still pass, while a regression that flips a key surface back to
English (Typer auto-generated ``Usage:`` headers aside) fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Same minimal isolation as the other CLI tests."""
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner.invoke(app, ["init", "--no-prompts"])
    return tmp_path


# ---------------------------------------------------------------------------
# Positive assertions: each command surfaces enough Chinese to be obviously
# localized. We accept either of two anchor words per command — picking a
# single word and breaking on copy edits is too brittle.
# ---------------------------------------------------------------------------


def test_root_help_is_localized(home: Path) -> None:
    """``deeptrade --help`` mentions 配置 / 插件 / 数据库 to identify itself."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "配置" in out or "插件" in out, out


def test_config_show_localized(home: Path) -> None:
    """``deeptrade config show`` table header is Chinese (配置 / 来源)."""
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    out = result.stdout
    assert "配置" in out or "来源" in out, out


def test_config_show_help_localized(home: Path) -> None:
    """``deeptrade config show --help`` docstring is Chinese."""
    result = runner.invoke(app, ["config", "show", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "显示" in out or "配置" in out, out


def test_plugin_list_localized(home: Path) -> None:
    """``deeptrade plugin list`` (empty DB) prints the localized empty
    notice; on a non-empty DB it would print the Chinese table header
    instead — either path satisfies the assertion."""
    result = runner.invoke(app, ["plugin", "list"])
    assert result.exit_code == 0
    out = result.stdout
    assert "已安装" in out or "插件" in out, out


def test_plugin_install_help_localized(home: Path) -> None:
    """``deeptrade plugin install --help`` docstring is Chinese."""
    result = runner.invoke(app, ["plugin", "install", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "安装" in out, out


# ---------------------------------------------------------------------------
# Negative assertions: lock out specific English phrases we deliberately
# replaced. If a future PR pastes the old English copy back in, these
# trip. Restricted to PR#5's literal pre-rewrite strings — we are NOT
# trying to police every English word (Typer's auto-rendered ``Options:``,
# ``Usage:`` and ``Arguments:`` headers stay English by design).
# ---------------------------------------------------------------------------


_FORBIDDEN_PHRASES_BY_COMMAND: dict[tuple[str, ...], tuple[str, ...]] = {
    ("config", "show"): ("DeepTrade Configuration",),
    ("config", "show", "--help"): ("List all known config keys",),
    ("plugin", "list"): ("Installed Plugins", "no plugins installed"),
    ("plugin", "install", "--help"): (
        "Install a plugin from the registry",
        "Skip the confirmation prompt",
    ),
}


@pytest.mark.parametrize(
    "argv,forbidden",
    [
        (argv, phrase)
        for argv, phrases in _FORBIDDEN_PHRASES_BY_COMMAND.items()
        for phrase in phrases
    ],
)
def test_pre_v05_english_phrases_are_gone(
    home: Path, argv: tuple[str, ...], forbidden: str
) -> None:
    """Each pre-v0.5 English phrase must NOT appear in the localized
    output. This is the regression lock that catches accidental reverts."""
    result = runner.invoke(app, list(argv))
    assert forbidden not in result.stdout, (
        f"reverted English phrase {forbidden!r} reappeared in `deeptrade "
        f"{' '.join(argv)}` output:\n{result.stdout}"
    )

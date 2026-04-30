"""V0.2 DoD — `deeptrade config show/set/test` end-to-end via CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-prompts"])  # skip post-init prompts
    return tmp_path


def test_config_show_lists_keys(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "deepseek.profile" in result.stdout
    assert "tushare.rps" in result.stdout


def test_config_show_masks_secret_value(home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["config", "set", "tushare.token", "abcdef1234567890"])
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "abcdef" not in result.stdout
    assert "********" in result.stdout
    assert "7890" in result.stdout  # last-4 visible


def test_config_set_persists_value(home: Path) -> None:
    runner = CliRunner()
    r1 = runner.invoke(app, ["config", "set", "deepseek.profile", "fast"])
    assert r1.exit_code == 0
    r2 = runner.invoke(app, ["config", "show"])
    assert "fast" in r2.stdout


def test_config_set_unknown_key_returns_2(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "set", "bogus.key", "x"])
    assert result.exit_code == 2
    assert "Unknown key" in result.stdout


def test_config_set_invalid_profile_returns_2(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "set", "deepseek.profile", "ultra"])
    assert result.exit_code == 2


def test_init_no_prompts_skips_questionary(home: Path) -> None:
    """`init --no-prompts` must succeed even with no stdin / no TTY."""
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompts"])
    assert result.exit_code == 0

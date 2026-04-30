"""V0.1 — paths module: env override + layout creation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deeptrade.core import paths


def test_home_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPTRADE_HOME", raising=False)
    assert paths.home_dir() == Path.home() / ".deeptrade"


def test_home_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    assert paths.home_dir() == tmp_path.resolve()


def test_db_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.duckdb"
    monkeypatch.setenv("DEEPTRADE_DB_PATH", str(target))
    assert paths.db_path() == target.resolve()


def test_ensure_layout_creates_subdirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    paths.ensure_layout()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "reports").is_dir()
    assert (tmp_path / "plugins" / "installed").is_dir()
    assert (tmp_path / "plugins" / "cache").is_dir()


def test_ensure_layout_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    paths.ensure_layout()
    paths.ensure_layout()  # should not raise
    assert os.path.isdir(tmp_path)

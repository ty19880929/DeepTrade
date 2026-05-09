"""Unit tests for deeptrade.core.plugin_source — SourceResolver."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deeptrade.core.plugin_source import (
    FrameworkVersionTooOldError,
    ResolvedSource,
    SourceResolveError,
    SourceResolver,
)
from deeptrade.core.registry import (
    RegistryEntry,
    RegistryNotFoundError,
)


def _entry(**overrides: Any) -> RegistryEntry:
    base = {
        "plugin_id": "limit-up-board",
        "name": "打板策略",
        "type": "strategy",
        "description": "...",
        "repo": "ty19880929/DeepTradePluginOfficial",
        "subdir": "limit_up_board",
        "tag_prefix": "limit-up-board/",
        "min_framework_version": "0.1.0",
    }
    base.update(overrides)
    return RegistryEntry(**base)


def _make_local_plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "my-plugin"
    plugin.mkdir()
    (plugin / "deeptrade_plugin.yaml").write_text("plugin_id: x\n", encoding="utf-8")
    return plugin


# ---------------------------------------------------------------------------
# Local path resolution
# ---------------------------------------------------------------------------


def test_resolve_local_path(tmp_path: Path) -> None:
    plugin = _make_local_plugin(tmp_path)
    resolver = SourceResolver(registry=MagicMock(), framework_version="0.1.0")
    resolved = resolver.resolve(str(plugin))
    assert resolved.origin == "local"
    assert resolved.path == plugin.resolve()
    assert resolved.cleanup is None


# ---------------------------------------------------------------------------
# Short-name resolution (registry-driven)
# ---------------------------------------------------------------------------


def test_resolve_short_name_happy_path(tmp_path: Path) -> None:
    registry = MagicMock()
    registry.resolve.return_value = _entry()

    # Stand up a fake extracted tarball: tmp/<top>/limit_up_board/deeptrade_plugin.yaml
    def fake_fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
        top = dest_dir / "ty19880929-DeepTradePluginOfficial-abc1234"
        plugin = top / "limit_up_board"
        plugin.mkdir(parents=True)
        (plugin / "deeptrade_plugin.yaml").write_text("plugin_id: x\n", encoding="utf-8")
        return top

    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    with (
        patch(
            "deeptrade.core.plugin_source.latest_release_tag",
            return_value="limit-up-board/v0.4.0",
        ),
        patch(
            "deeptrade.core.plugin_source.fetch_tarball",
            side_effect=fake_fetch_tarball,
        ),
    ):
        resolved = resolver.resolve("limit-up-board")

    assert resolved.origin == "github_registry"
    assert resolved.path.name == "limit_up_board"
    assert (resolved.path / "deeptrade_plugin.yaml").is_file()
    assert resolved.origin_detail["ref"] == "limit-up-board/v0.4.0"
    assert resolved.cleanup is not None
    resolved.cleanup()


def test_resolve_short_name_explicit_ref(tmp_path: Path) -> None:
    registry = MagicMock()
    registry.resolve.return_value = _entry()

    def fake_fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
        top = dest_dir / "top"
        plugin = top / "limit_up_board"
        plugin.mkdir(parents=True)
        (plugin / "deeptrade_plugin.yaml").touch()
        return top

    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    with (
        patch("deeptrade.core.plugin_source.latest_release_tag") as mock_latest,
        patch(
            "deeptrade.core.plugin_source.fetch_tarball",
            side_effect=fake_fetch_tarball,
        ),
    ):
        resolver.resolve("limit-up-board", ref="limit-up-board/v0.3.0")

    mock_latest.assert_not_called()


def test_resolve_short_name_unknown_raises() -> None:
    registry = MagicMock()
    registry.resolve.side_effect = RegistryNotFoundError("not found")
    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    with pytest.raises(RegistryNotFoundError):
        resolver.resolve("bogus-plugin")


def test_resolve_short_name_framework_too_old_raises() -> None:
    registry = MagicMock()
    registry.resolve.return_value = _entry(min_framework_version="0.5.0")
    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    with pytest.raises(FrameworkVersionTooOldError, match="0.5.0"):
        resolver.resolve("limit-up-board")


def test_resolve_short_name_subdir_missing_raises() -> None:
    registry = MagicMock()
    registry.resolve.return_value = _entry()

    def fake_fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
        top = dest_dir / "top"
        top.mkdir()
        # Note: subdir 'limit_up_board' NOT created
        return top

    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    with (
        patch(
            "deeptrade.core.plugin_source.latest_release_tag",
            return_value="limit-up-board/v0.4.0",
        ),
        patch(
            "deeptrade.core.plugin_source.fetch_tarball",
            side_effect=fake_fetch_tarball,
        ),
        pytest.raises(SourceResolveError, match="subdir"),
    ):
        resolver.resolve("limit-up-board")


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_repo",
    [
        ("https://github.com/foo/bar", "foo/bar"),
        ("https://github.com/foo/bar.git", "foo/bar"),
        ("https://github.com/foo/bar/", "foo/bar"),
        ("git@github.com:foo/bar.git", "foo/bar"),
    ],
)
def test_resolve_url_parses_owner_repo(url: str, expected_repo: str) -> None:
    def fake_fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
        top = dest_dir / "top"
        top.mkdir()
        (top / "deeptrade_plugin.yaml").touch()
        return top

    captured: dict[str, str] = {}

    def fake_latest(repo: str, prefix: str) -> str:
        captured["repo"] = repo
        captured["prefix"] = prefix
        return "v1.0.0"

    resolver = SourceResolver(registry=MagicMock(), framework_version="0.1.0")
    with (
        patch("deeptrade.core.plugin_source.latest_release_tag", side_effect=fake_latest),
        patch(
            "deeptrade.core.plugin_source.fetch_tarball",
            side_effect=fake_fetch_tarball,
        ),
    ):
        resolved = resolver.resolve(url)

    assert captured["repo"] == expected_repo
    assert captured["prefix"] == ""  # URL form uses empty prefix
    assert resolved.origin == "github_url"
    resolved.cleanup()


def test_resolve_url_rejects_non_github() -> None:
    resolver = SourceResolver(registry=MagicMock(), framework_version="0.1.0")
    with pytest.raises(SourceResolveError, match="github.com"):
        resolver.resolve("https://gitlab.com/foo/bar")


def test_resolve_url_no_yaml_at_root_raises() -> None:
    def fake_fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
        top = dest_dir / "top"
        top.mkdir()
        # NO deeptrade_plugin.yaml at root (e.g. monorepo)
        return top

    resolver = SourceResolver(registry=MagicMock(), framework_version="0.1.0")
    with (
        patch("deeptrade.core.plugin_source.latest_release_tag", return_value="v1.0.0"),
        patch(
            "deeptrade.core.plugin_source.fetch_tarball",
            side_effect=fake_fetch_tarball,
        ),
        pytest.raises(SourceResolveError, match="deeptrade_plugin.yaml"),
    ):
        resolver.resolve("https://github.com/foo/bar")


# ---------------------------------------------------------------------------
# Form detection ordering
# ---------------------------------------------------------------------------


def test_existing_directory_beats_short_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a directory exists matching the input string, treat as local even
    if it would otherwise look like a short name."""
    monkeypatch.chdir(tmp_path)
    plugin = tmp_path / "limit-up-board"
    plugin.mkdir()
    (plugin / "deeptrade_plugin.yaml").touch()

    registry = MagicMock()
    resolver = SourceResolver(registry=registry, framework_version="0.1.0")
    resolved = resolver.resolve("limit-up-board")
    assert resolved.origin == "local"
    registry.resolve.assert_not_called()


def test_resolved_source_dataclass_has_expected_fields() -> None:
    rs = ResolvedSource(path=Path("."), origin="local")
    assert rs.path == Path(".")
    assert rs.origin == "local"
    assert rs.origin_detail == {}
    assert rs.cleanup is None

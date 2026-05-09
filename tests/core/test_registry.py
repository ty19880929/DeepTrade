"""Unit tests for deeptrade.core.registry — RegistryClient.

All network calls are mocked via patching ``urlopen`` in the registry module.
No real HTTP is performed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from deeptrade.core.registry import (
    RegistryClient,
    RegistryFetchError,
    RegistryNotFoundError,
    RegistrySchemaError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plugins": {
            "limit-up-board": {
                "name": "打板策略",
                "type": "strategy",
                "description": "...",
                "repo": "ty19880929/DeepTradePluginOfficial",
                "subdir": "limit_up_board",
                "tag_prefix": "limit-up-board/",
                "min_framework_version": "0.1.0",
            }
        },
    }


class _FakeResponse:
    def __init__(self, body: bytes, etag: str | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = {"ETag": etag} if etag else {}

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self._buf.close()


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "registry-cache.json"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_returns_parsed_registry(cache_path: Path) -> None:
    payload = _valid_payload()
    body = json.dumps(payload).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)

    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body, etag='"abc"'),
    ):
        registry = client.fetch()

    assert registry.schema_version == 1
    assert "limit-up-board" in registry.plugins
    entry = registry.plugins["limit-up-board"]
    assert entry.plugin_id == "limit-up-board"
    assert entry.subdir == "limit_up_board"
    assert entry.min_framework_version == "0.1.0"


def test_fetch_writes_cache_with_etag(cache_path: Path) -> None:
    payload = _valid_payload()
    body = json.dumps(payload).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)

    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body, etag='"abc"'),
    ):
        client.fetch()

    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["etag"] == '"abc"'
    assert cached["body"]["schema_version"] == 1


def test_fetch_sends_if_none_match_when_cached(cache_path: Path) -> None:
    cache_path.write_text(
        json.dumps({"etag": '"abc"', "body": _valid_payload()}),
        encoding="utf-8",
    )
    client = RegistryClient(url="https://x", cache_path=cache_path)

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, **_: Any) -> _FakeResponse:
        captured["if_none_match"] = req.headers.get("If-none-match")
        return _FakeResponse(json.dumps(_valid_payload()).encode("utf-8"), etag='"def"')

    with patch("deeptrade.core.registry.urlopen", side_effect=fake_urlopen):
        client.fetch()

    assert captured["if_none_match"] == '"abc"'


def test_fetch_force_bypasses_cache_etag(cache_path: Path) -> None:
    cache_path.write_text(
        json.dumps({"etag": '"abc"', "body": _valid_payload()}),
        encoding="utf-8",
    )
    client = RegistryClient(url="https://x", cache_path=cache_path)

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, **_: Any) -> _FakeResponse:
        captured["if_none_match"] = req.headers.get("If-none-match")
        return _FakeResponse(json.dumps(_valid_payload()).encode("utf-8"))

    with patch("deeptrade.core.registry.urlopen", side_effect=fake_urlopen):
        client.fetch(force=True)

    assert captured["if_none_match"] is None


# ---------------------------------------------------------------------------
# Cache fallback
# ---------------------------------------------------------------------------


def test_304_uses_cached_body(cache_path: Path) -> None:
    cache_path.write_text(
        json.dumps({"etag": '"abc"', "body": _valid_payload()}),
        encoding="utf-8",
    )
    client = RegistryClient(url="https://x", cache_path=cache_path)
    err = HTTPError("https://x", 304, "Not Modified", {}, None)  # type: ignore[arg-type]

    with patch("deeptrade.core.registry.urlopen", side_effect=err):
        registry = client.fetch()

    assert "limit-up-board" in registry.plugins


def test_url_error_with_cache_uses_cache(cache_path: Path) -> None:
    cache_path.write_text(
        json.dumps({"etag": '"abc"', "body": _valid_payload()}),
        encoding="utf-8",
    )
    client = RegistryClient(url="https://x", cache_path=cache_path)

    with patch(
        "deeptrade.core.registry.urlopen",
        side_effect=URLError("DNS down"),
    ):
        registry = client.fetch()

    assert "limit-up-board" in registry.plugins


def test_url_error_without_cache_raises(cache_path: Path) -> None:
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        side_effect=URLError("DNS down"),
    ):
        with pytest.raises(RegistryFetchError, match="network error"):
            client.fetch()


def test_http_error_non_304_raises(cache_path: Path) -> None:
    client = RegistryClient(url="https://x", cache_path=cache_path)
    err = HTTPError("https://x", 500, "Server Error", {}, None)  # type: ignore[arg-type]
    with patch("deeptrade.core.registry.urlopen", side_effect=err):
        with pytest.raises(RegistryFetchError, match="HTTP 500"):
            client.fetch()


# ---------------------------------------------------------------------------
# Schema errors
# ---------------------------------------------------------------------------


def test_invalid_json_raises(cache_path: Path) -> None:
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(b"not json"),
    ):
        with pytest.raises(RegistrySchemaError):
            client.fetch()


def test_missing_schema_version_raises(cache_path: Path) -> None:
    body = json.dumps({"plugins": {}}).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body),
    ):
        with pytest.raises(RegistrySchemaError, match="schema_version"):
            client.fetch()


def test_missing_required_entry_field_raises(cache_path: Path) -> None:
    payload = _valid_payload()
    del payload["plugins"]["limit-up-board"]["repo"]
    body = json.dumps(payload).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body),
    ):
        with pytest.raises(RegistrySchemaError, match="repo"):
            client.fetch()


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_known_plugin(cache_path: Path) -> None:
    body = json.dumps(_valid_payload()).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body),
    ):
        entry = client.resolve("limit-up-board")
    assert entry.subdir == "limit_up_board"


def test_resolve_unknown_plugin_raises(cache_path: Path) -> None:
    body = json.dumps(_valid_payload()).encode("utf-8")
    client = RegistryClient(url="https://x", cache_path=cache_path)
    with patch(
        "deeptrade.core.registry.urlopen",
        return_value=_FakeResponse(body),
    ):
        with pytest.raises(RegistryNotFoundError, match="bogus"):
            client.resolve("bogus")

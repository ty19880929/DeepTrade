"""Plugin registry client.

Fetches the official plugin index from
``raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json``
with ETag-based caching to ``~/.deeptrade/plugins/registry-cache.json``.

See ``docs/distribution-and-plugin-install-design.md`` §5 + §6.1.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from deeptrade.core import paths

logger = logging.getLogger(__name__)

REGISTRY_URL = (
    "https://raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json"
)

_REQUIRED_FIELDS = frozenset(
    {"name", "type", "description", "repo", "subdir", "tag_prefix", "min_framework_version"}
)


class RegistryError(Exception):
    """Generic registry error."""


class RegistryFetchError(RegistryError):
    """Network or HTTP failure fetching the registry."""


class RegistryNotFoundError(RegistryError):
    """plugin_id not present in the registry."""


class RegistrySchemaError(RegistryError):
    """Registry JSON does not match the expected schema."""


@dataclass(frozen=True)
class RegistryEntry:
    plugin_id: str
    name: str
    type: str
    description: str
    repo: str
    subdir: str
    tag_prefix: str
    min_framework_version: str


@dataclass(frozen=True)
class Registry:
    schema_version: int
    plugins: dict[str, RegistryEntry]


def _default_cache_path() -> Path:
    return paths.home_dir() / "plugins" / "registry-cache.json"


def _user_agent() -> str:
    from deeptrade import __version__

    return f"deeptrade-cli/{__version__}"


def _parse_registry(data: Any) -> Registry:
    if not isinstance(data, dict):
        raise RegistrySchemaError("registry root must be a JSON object")

    schema_version = data.get("schema_version")
    if schema_version != 1:
        raise RegistrySchemaError(f"schema_version must be 1, got {schema_version!r}")

    plugins_raw = data.get("plugins")
    if not isinstance(plugins_raw, dict):
        raise RegistrySchemaError("plugins must be an object")

    entries: dict[str, RegistryEntry] = {}
    for plugin_id, raw in plugins_raw.items():
        if not isinstance(raw, dict):
            raise RegistrySchemaError(f"plugins.{plugin_id} must be an object")
        missing = _REQUIRED_FIELDS - set(raw)
        if missing:
            raise RegistrySchemaError(f"plugins.{plugin_id} missing fields: {sorted(missing)}")
        entries[plugin_id] = RegistryEntry(
            plugin_id=plugin_id,
            **{k: raw[k] for k in _REQUIRED_FIELDS},
        )
    return Registry(schema_version=schema_version, plugins=entries)


class RegistryClient:
    """Fetches and caches the plugin registry index.

    Cache file format::

        {"etag": "<etag>", "body": <registry json>}

    On a 304 Not Modified the cached body is reused.
    On any network failure with a usable cache, the cached body is used and
    a warning is logged.
    """

    def __init__(
        self,
        url: str = REGISTRY_URL,
        cache_path: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.url = url
        self.cache_path = cache_path if cache_path is not None else _default_cache_path()
        self.timeout = timeout

    def fetch(self, *, force: bool = False) -> Registry:
        cached = None if force else self._read_cache()
        etag = cached.get("etag") if cached else None

        headers: dict[str, str] = {"User-Agent": _user_agent()}
        if etag and not force:
            headers["If-None-Match"] = etag
        req = Request(self.url, headers=headers)

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                new_etag = resp.headers.get("ETag")
                raw_bytes = resp.read()
        except HTTPError as e:
            if e.code == 304 and cached is not None:
                logger.debug("registry: 304 Not Modified, using cache")
                return _parse_registry(cached["body"])
            raise RegistryFetchError(f"HTTP {e.code} fetching registry: {e}") from e
        except URLError as e:
            if cached is not None:
                logger.warning("registry: network error %s, falling back to cache", e)
                return _parse_registry(cached["body"])
            raise RegistryFetchError(f"network error fetching registry: {e}") from e

        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RegistrySchemaError(f"registry not valid JSON: {e}") from e

        registry = _parse_registry(body)
        if new_etag:
            self._write_cache({"etag": new_etag, "body": body})
        return registry

    def resolve(self, plugin_id: str) -> RegistryEntry:
        registry = self.fetch()
        if plugin_id not in registry.plugins:
            raise RegistryNotFoundError(
                f"plugin {plugin_id!r} not in registry. Available: {sorted(registry.plugins)}"
            )
        return registry.plugins[plugin_id]

    def _read_cache(self) -> dict | None:
        if not self.cache_path.is_file():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, payload: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

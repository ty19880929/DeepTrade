"""Resolve a user-supplied plugin source spec to a local directory.

Three forms (judged in this order):

1. **Local path** — exists and is a directory (e.g. ``./my-plugin``,
   ``/abs/path``, ``C:\\plugins\\foo``)
2. **Git URL** — starts with ``http(s)://`` or ``git@``; only ``github.com`` is
   supported. Plugin must live at the repo root (no monorepo-subdir support
   for URL installs; use local path for those).
3. **Short name** — anything else, looked up in the official registry.

The returned :class:`ResolvedSource` has ``path`` pointing to a local directory
containing ``deeptrade_plugin.yaml``. For GitHub sources, ``cleanup()`` removes
the temporary extraction directory.

See ``CHANGELOG.md`` v0.3 entries for the distribution / install design context.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from deeptrade.core.github_fetch import (
    GitHubFetchError,
    NoMatchingReleaseError,
    fetch_tarball,
    latest_release_tag,
)
from deeptrade.core.registry import (
    RegistryClient,
    RegistryEntry,
    RegistryError,
    RegistryNotFoundError,
)


class SourceResolveError(Exception):
    """Failed to resolve a plugin source spec to a local directory."""


class FrameworkVersionTooOldError(SourceResolveError):
    """The installed framework is older than the plugin's min_framework_version."""


@dataclass
class ResolvedSource:
    path: Path
    origin: str  # "local" | "github_registry" | "github_url"
    origin_detail: dict = field(default_factory=dict)
    cleanup: Callable[[], None] | None = None


_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def _is_git_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@"))


def _parse_github_url(url: str) -> tuple[str, str]:
    m = _GITHUB_URL_RE.match(url)
    if not m:
        raise SourceResolveError(f"unsupported URL form (only github.com is supported): {url!r}")
    return m.group(1), m.group(2)


class SourceResolver:
    def __init__(
        self,
        registry: RegistryClient | None = None,
        framework_version: str | None = None,
    ) -> None:
        self.registry = registry if registry is not None else RegistryClient()
        if framework_version is None:
            from deeptrade import __version__

            framework_version = __version__
        self.framework_version = framework_version

    def resolve(self, raw: str, ref: str | None = None) -> ResolvedSource:
        path = Path(raw)
        if path.is_dir():
            return ResolvedSource(
                path=path.resolve(),
                origin="local",
                origin_detail={"local_path": str(path.resolve())},
                cleanup=None,
            )
        if _is_git_url(raw):
            return self._resolve_url(raw, ref)
        return self._resolve_short_name(raw, ref)

    def _resolve_short_name(self, plugin_id: str, ref: str | None) -> ResolvedSource:
        try:
            entry = self.registry.resolve(plugin_id)
        except RegistryNotFoundError:
            raise
        except RegistryError as e:
            raise SourceResolveError(f"registry error: {e}") from e

        self._check_framework_version(entry)

        if ref is None:
            try:
                ref = latest_release_tag(entry.repo, entry.tag_prefix)
            except (NoMatchingReleaseError, GitHubFetchError) as e:
                raise SourceResolveError(str(e)) from e

        tmp = tempfile.TemporaryDirectory(prefix="deeptrade-plugin-")
        try:
            top = fetch_tarball(entry.repo, ref, Path(tmp.name))
            plugin_path = top / entry.subdir
            if not plugin_path.is_dir():
                raise SourceResolveError(f"subdir {entry.subdir!r} not found in {entry.repo}@{ref}")
            yaml_path = plugin_path / "deeptrade_plugin.yaml"
            if not yaml_path.is_file():
                raise SourceResolveError(
                    f"no deeptrade_plugin.yaml in {entry.repo}@{ref} under {entry.subdir!r}"
                )
        except Exception:
            tmp.cleanup()
            raise

        return ResolvedSource(
            path=plugin_path,
            origin="github_registry",
            origin_detail={
                "repo": entry.repo,
                "ref": ref,
                "subdir": entry.subdir,
                "plugin_id": plugin_id,
            },
            cleanup=tmp.cleanup,
        )

    def _resolve_url(self, url: str, ref: str | None) -> ResolvedSource:
        owner, repo_name = _parse_github_url(url)
        repo = f"{owner}/{repo_name}"

        if ref is None:
            try:
                ref = latest_release_tag(repo, "")
            except (NoMatchingReleaseError, GitHubFetchError) as e:
                raise SourceResolveError(str(e)) from e

        tmp = tempfile.TemporaryDirectory(prefix="deeptrade-plugin-")
        try:
            top = fetch_tarball(repo, ref, Path(tmp.name))
            yaml_path = top / "deeptrade_plugin.yaml"
            if not yaml_path.is_file():
                raise SourceResolveError(
                    f"no deeptrade_plugin.yaml at repo root of {repo}@{ref}; "
                    f"URL install requires the plugin at the repo root "
                    f"(use local path install for monorepo subdirs)"
                )
        except Exception:
            tmp.cleanup()
            raise

        return ResolvedSource(
            path=top,
            origin="github_url",
            origin_detail={"repo": repo, "ref": ref},
            cleanup=tmp.cleanup,
        )

    def _check_framework_version(self, entry: RegistryEntry) -> None:
        try:
            cur = Version(self.framework_version)
            req = Version(entry.min_framework_version)
        except InvalidVersion as e:
            raise SourceResolveError(
                f"invalid version: {e}; framework={self.framework_version!r}, "
                f"plugin requires={entry.min_framework_version!r}"
            ) from e
        if cur < req:
            raise FrameworkVersionTooOldError(
                f"plugin {entry.plugin_id!r} requires deeptrade >= "
                f"{entry.min_framework_version}, but you have {self.framework_version}; "
                f"please upgrade the framework first"
            )

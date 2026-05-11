"""GitHub release / tarball helpers.

Used by the plugin source resolver to (a) find the latest SemVer release tag
matching a prefix and (b) download + extract a repo tarball into a directory.

Implementation uses stdlib ``urllib.request`` + ``tarfile``; no third-party
HTTP dependency. ``GITHUB_TOKEN`` env var is honored for rate-limit relief.

See ``docs/distribution-and-plugin-install-design.md`` §6.2.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubFetchError(Exception):
    """Generic GitHub API / fetch error."""


class TarballFetchError(GitHubFetchError):
    """Tarball download or extraction failure."""


class NoMatchingReleaseError(GitHubFetchError):
    """No releases match the requested tag_prefix."""


def _user_agent() -> str:
    from deeptrade import __version__

    return f"deeptrade-cli/{__version__}"


def _build_request(url: str, *, accept: str = "*/*") -> Request:
    headers = {
        "Accept": accept,
        "User-Agent": _user_agent(),
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        m = _NEXT_LINK_RE.search(part)
        if m:
            return m.group(1)
    return None


def latest_release_tag(repo: str, tag_prefix: str = "", *, timeout: float = 15.0) -> str:
    """Return the SemVer-highest release tag for ``repo``.

    If ``tag_prefix`` is non-empty, only tags starting with it are considered;
    after stripping the prefix and an optional leading ``v``, the remainder is
    parsed as a SemVer version and the highest is returned.

    If ``tag_prefix`` is empty, all release tags are considered (used for the
    URL-direct install case).

    Drafts and prereleases are skipped. Tags that do not parse as SemVer are
    skipped silently.
    """
    url: str | None = f"{GITHUB_API_BASE}/repos/{repo}/releases?per_page=100"
    candidates: list[tuple[Version, str]] = []

    while url:
        req = _build_request(url, accept="application/vnd.github+json")
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                link_header = resp.headers.get("Link")
        except HTTPError as e:
            raise GitHubFetchError(f"HTTP {e.code} listing releases for {repo}: {e}") from e
        except URLError as e:
            raise GitHubFetchError(f"network error listing releases for {repo}: {e}") from e

        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise GitHubFetchError(f"invalid JSON in releases response for {repo}: {e}") from e

        if not isinstance(data, list):
            raise GitHubFetchError(
                f"expected JSON array of releases for {repo}, got {type(data).__name__}"
            )

        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("draft") or r.get("prerelease"):
                continue
            tag = r.get("tag_name")
            if not isinstance(tag, str):
                continue
            if tag_prefix and not tag.startswith(tag_prefix):
                continue
            ver_str = tag[len(tag_prefix) :] if tag_prefix else tag
            ver_str = ver_str.lstrip("v")
            try:
                candidates.append((Version(ver_str), tag))
            except InvalidVersion:
                logger.debug("skipping non-semver tag: %s", tag)

        url = _next_link(link_header)

    if not candidates:
        suffix = f" with tag_prefix {tag_prefix!r}" if tag_prefix else ""
        raise NoMatchingReleaseError(f"no releases found for {repo}{suffix}")

    candidates.sort(reverse=True)
    return candidates[0][1]


def fetch_tarball(repo: str, ref: str, dest_dir: Path, *, timeout: float = 60.0) -> Path:
    """Download ``repo`` at ``ref`` from the GitHub tarball API and extract.

    Returns the unique top-level directory created inside ``dest_dir``
    (GitHub tarballs are wrapped in ``<owner>-<repo>-<sha7>/``).

    Raises :class:`TarballFetchError` on network, HTTP, or extraction failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"{GITHUB_API_BASE}/repos/{repo}/tarball/{ref}"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        req = _build_request(url, accept="application/vnd.github+json")
        try:
            with urlopen(req, timeout=timeout) as resp, tmp_path.open("wb") as fout:
                shutil.copyfileobj(resp, fout)
        except HTTPError as e:
            raise TarballFetchError(f"HTTP {e.code} downloading tarball {repo}@{ref}: {e}") from e
        except URLError as e:
            raise TarballFetchError(f"network error downloading tarball {repo}@{ref}: {e}") from e

        try:
            with tarfile.open(tmp_path, mode="r:gz") as tf:
                _safe_extract(tf, dest_dir)
        except tarfile.TarError as e:
            raise TarballFetchError(f"failed to extract tarball: {e}") from e
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    entries = [p for p in dest_dir.iterdir() if p.is_dir()]
    if len(entries) != 1:
        raise TarballFetchError(
            f"expected one top-level dir in tarball, found {len(entries)}: "
            f"{[p.name for p in entries]}"
        )
    return entries[0]


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tf`` into ``dest`` with path-traversal protection.

    Uses tarfile's ``data`` filter on Python 3.12+, with an explicit
    relative-path check on every member as a belt-and-braces guard for 3.11.
    """
    dest_resolved = dest.resolve()
    for m in tf.getmembers():
        member_path = (dest / m.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as e:
            raise tarfile.TarError(f"unsafe path in tarball (would escape dest): {m.name!r}") from e

    try:
        tf.extractall(dest, filter="data")
    except TypeError:
        tf.extractall(dest)  # noqa: S202 — paths already validated above

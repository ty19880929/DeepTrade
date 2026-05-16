"""GitHub tarball download helper (CDN-only).

The plugin install / upgrade path downloads release tarballs from
``codeload.github.com`` instead of ``api.github.com``. codeload is a
CDN-backed static endpoint and does **not** count against the GitHub REST
API rate limit (60/h for anonymous IPs), so plugin install works for
every user out of the box — no ``GITHUB_TOKEN`` required.

"Latest version" used to be resolved via ``GET /repos/<repo>/releases``,
but that costs an API request per install. It now comes from the
``latest_version`` field in the registry index, which is served from
``raw.githubusercontent.com`` (also CDN, also un-metered). See
``deeptrade.core.plugin_source`` for the wiring.

See ``CHANGELOG.md`` for the distribution / install design context.
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CODELOAD_BASE = "https://codeload.github.com"


class GitHubFetchError(Exception):
    """Generic GitHub fetch error."""


class TarballFetchError(GitHubFetchError):
    """Tarball download or extraction failure."""


def _user_agent() -> str:
    from deeptrade import __version__

    return f"deeptrade-cli/{__version__}"


def _build_request(url: str) -> Request:
    return Request(url, headers={"User-Agent": _user_agent()})


def fetch_tarball(repo: str, ref: str, dest_dir: Path, *, timeout: float = 60.0) -> Path:
    """Download ``repo`` at ``ref`` from codeload.github.com and extract.

    ``ref`` may be a tag (``v1.0.0`` or ``limit-up-board/v0.4.0``), a branch
    name (``main``), a SHA, or a full git ref (``refs/tags/v1.0.0`` /
    ``refs/heads/main``). codeload resolves all of these transparently.

    Returns the unique top-level directory created inside ``dest_dir``
    (codeload tarballs are wrapped in ``<owner>-<repo>-<sha7>/``).

    Raises :class:`TarballFetchError` on network, HTTP, or extraction failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"{CODELOAD_BASE}/{repo}/tar.gz/{ref}"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        req = _build_request(url)
        try:
            with urlopen(req, timeout=timeout) as resp, tmp_path.open("wb") as fout:
                shutil.copyfileobj(resp, fout)
        except HTTPError as e:
            raise TarballFetchError(
                f"HTTP {e.code} downloading tarball {repo}@{ref} from codeload: {e}"
            ) from e
        except URLError as e:
            raise TarballFetchError(
                f"network error downloading tarball {repo}@{ref} from codeload: {e}"
            ) from e

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

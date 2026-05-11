"""Unit tests for deeptrade.core.github_fetch.

Network calls (``urlopen``) are patched. Tarball extraction is exercised on
real .tar.gz produced in-memory.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from deeptrade.core.github_fetch import (
    GitHubFetchError,
    NoMatchingReleaseError,
    TarballFetchError,
    fetch_tarball,
    latest_release_tag,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read() if n == -1 else self._buf.read(n)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self._buf.close()


def _release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, Any]:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


def _releases_response(*tags: str | dict[str, Any]) -> _FakeResponse:
    items = [t if isinstance(t, dict) else _release(t) for t in tags]
    return _FakeResponse(json.dumps(items).encode("utf-8"))


# ---------------------------------------------------------------------------
# latest_release_tag
# ---------------------------------------------------------------------------


def test_latest_release_tag_filters_by_prefix_and_picks_highest() -> None:
    response = _releases_response(
        "limit-up-board/v0.3.0",
        "limit-up-board/v0.4.0",
        "limit-up-board/v0.4.1",
        "volume-anomaly/v0.6.0",
    )
    with patch("deeptrade.core.github_fetch.urlopen", return_value=response):
        tag = latest_release_tag("foo/bar", "limit-up-board/")
    assert tag == "limit-up-board/v0.4.1"


def test_latest_release_tag_skips_drafts_and_prereleases() -> None:
    response = _releases_response(
        _release("limit-up-board/v1.0.0", prerelease=True),
        _release("limit-up-board/v0.9.0", draft=True),
        "limit-up-board/v0.4.0",
    )
    with patch("deeptrade.core.github_fetch.urlopen", return_value=response):
        tag = latest_release_tag("foo/bar", "limit-up-board/")
    assert tag == "limit-up-board/v0.4.0"


def test_latest_release_tag_skips_non_semver_tags() -> None:
    response = _releases_response(
        "limit-up-board/release-final",
        "limit-up-board/v0.4.0",
    )
    with patch("deeptrade.core.github_fetch.urlopen", return_value=response):
        tag = latest_release_tag("foo/bar", "limit-up-board/")
    assert tag == "limit-up-board/v0.4.0"


def test_latest_release_tag_no_match_raises() -> None:
    response = _releases_response("volume-anomaly/v0.6.0")
    with patch("deeptrade.core.github_fetch.urlopen", return_value=response):
        with pytest.raises(NoMatchingReleaseError):
            latest_release_tag("foo/bar", "limit-up-board/")


def test_latest_release_tag_empty_prefix_considers_all() -> None:
    response = _releases_response("v0.1.0", "v0.2.0")
    with patch("deeptrade.core.github_fetch.urlopen", return_value=response):
        tag = latest_release_tag("foo/bar", "")
    assert tag == "v0.2.0"


def test_latest_release_tag_pagination() -> None:
    page1 = _FakeResponse(
        json.dumps([_release("limit-up-board/v0.3.0"), _release("limit-up-board/v0.3.1")]).encode(
            "utf-8"
        ),
        headers={"Link": '<https://api.github.com/page2>; rel="next"'},
    )
    page2 = _releases_response("limit-up-board/v0.5.0")

    responses = iter([page1, page2])

    def fake_urlopen(*args: Any, **kwargs: Any) -> _FakeResponse:
        return next(responses)

    with patch("deeptrade.core.github_fetch.urlopen", side_effect=fake_urlopen):
        tag = latest_release_tag("foo/bar", "limit-up-board/")
    assert tag == "limit-up-board/v0.5.0"


def test_latest_release_tag_http_error_raises() -> None:
    err = HTTPError("https://x", 403, "rate limited", {}, None)  # type: ignore[arg-type]
    with patch("deeptrade.core.github_fetch.urlopen", side_effect=err):
        with pytest.raises(GitHubFetchError, match="HTTP 403"):
            latest_release_tag("foo/bar", "limit-up-board/")


def test_latest_release_tag_url_error_raises() -> None:
    with patch("deeptrade.core.github_fetch.urlopen", side_effect=URLError("dns")):
        with pytest.raises(GitHubFetchError, match="network error"):
            latest_release_tag("foo/bar", "limit-up-board/")


# ---------------------------------------------------------------------------
# fetch_tarball
# ---------------------------------------------------------------------------


def _build_tarball(top_dir: str, files: dict[str, bytes]) -> bytes:
    """Build an in-memory .tar.gz with a single top-level directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Add the top dir
        info = tarfile.TarInfo(name=top_dir)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tf.addfile(info)
        for path, content in files.items():
            info = tarfile.TarInfo(name=f"{top_dir}/{path}")
            info.size = len(content)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_fetch_tarball_extracts_and_returns_top_dir(tmp_path: Path) -> None:
    tarball = _build_tarball(
        "owner-repo-abc1234",
        {"deeptrade_plugin.yaml": b"plugin_id: x\n", "data.py": b"# code\n"},
    )
    with patch(
        "deeptrade.core.github_fetch.urlopen",
        return_value=_FakeResponse(tarball),
    ):
        top = fetch_tarball("owner/repo", "v1.0.0", tmp_path)

    assert top.is_dir()
    assert top.name == "owner-repo-abc1234"
    assert (top / "deeptrade_plugin.yaml").is_file()
    assert (top / "data.py").read_text() == "# code\n"


def test_fetch_tarball_http_error_raises(tmp_path: Path) -> None:
    err = HTTPError("https://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    with patch("deeptrade.core.github_fetch.urlopen", side_effect=err):
        with pytest.raises(TarballFetchError, match="HTTP 404"):
            fetch_tarball("owner/repo", "v1.0.0", tmp_path)


def test_fetch_tarball_blocks_path_traversal(tmp_path: Path) -> None:
    """A tarball whose members escape dest_dir must be rejected."""
    dest = tmp_path / "extract_here"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="owner-repo-abc/")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        # This member resolves to dest_dir's parent — outside the destination
        info = tarfile.TarInfo(name="../escaped.txt")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"evil"))

    with patch(
        "deeptrade.core.github_fetch.urlopen",
        return_value=_FakeResponse(buf.getvalue()),
    ):
        with pytest.raises(TarballFetchError, match="extract"):
            fetch_tarball("owner/repo", "v1.0.0", dest)
    # Sanity: the escaped file was NOT written outside dest
    assert not (tmp_path / "escaped.txt").exists()

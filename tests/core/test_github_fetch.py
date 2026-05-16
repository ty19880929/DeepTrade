"""Unit tests for deeptrade.core.github_fetch.

Network calls (``urlopen``) are patched. Tarball extraction is exercised on
real .tar.gz produced in-memory. Since the v0.8 CDN refactor, github_fetch
only exposes ``fetch_tarball`` (latest-tag resolution moved to the registry
``latest_version`` field).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from deeptrade.core.github_fetch import (
    CODELOAD_BASE,
    TarballFetchError,
    fetch_tarball,
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


def _build_tarball(top_dir: str, files: dict[str, bytes]) -> bytes:
    """Build an in-memory .tar.gz with a single top-level directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
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


# ---------------------------------------------------------------------------
# fetch_tarball
# ---------------------------------------------------------------------------


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


def test_fetch_tarball_uses_codeload_url(tmp_path: Path) -> None:
    """Sanity-check the new endpoint: no api.github.com, codeload only."""
    tarball = _build_tarball("owner-repo-abc1234", {"x": b""})
    captured: dict[str, str] = {}

    def fake_urlopen(req: Any, **_: Any) -> _FakeResponse:
        captured["url"] = req.full_url
        return _FakeResponse(tarball)

    with patch("deeptrade.core.github_fetch.urlopen", side_effect=fake_urlopen):
        fetch_tarball("owner/repo", "limit-up-board/v0.4.0", tmp_path)

    assert captured["url"].startswith(CODELOAD_BASE + "/")
    assert "api.github.com" not in captured["url"]
    assert "owner/repo/tar.gz/limit-up-board/v0.4.0" in captured["url"]


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
        info = tarfile.TarInfo(name="../escaped.txt")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"evil"))

    with patch(
        "deeptrade.core.github_fetch.urlopen",
        return_value=_FakeResponse(buf.getvalue()),
    ):
        with pytest.raises(TarballFetchError, match="extract"):
            fetch_tarball("owner/repo", "v1.0.0", dest)
    assert not (tmp_path / "escaped.txt").exists()

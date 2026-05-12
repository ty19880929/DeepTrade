"""Plugin dependency resolution & installation.

Called by :class:`PluginManager` during install / upgrade to satisfy a
plugin's declared third-party Python dependencies (PEP 508 specifiers
in ``deeptrade_plugin.yaml::dependencies``).

Design: ``docs/plugin_dependency_management_design.md``.

Key invariants:
  * Plugins share the framework's Python interpreter — deps must be
    importable from ``sys.executable``'s site-packages.
  * Installer preference: ``uv`` (with ``--python <sys.executable>``) →
    ``python -m pip``. Both missing → :class:`DepInstallError`.
  * Conflicts (installed version not satisfying spec) are detected at
    install time and raised, not silently overridden.
  * Failed installs leave any already-installed deps in the environment
    (do not unwind — common deps would be wrongly removed).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300


class DepInstallError(Exception):
    """Dependency resolution, conflict, or install-subprocess failure."""


@dataclass
class DepConflict:
    requirement: Requirement
    installed_version: str
    owner: str  # human-readable attribution string

    def __str__(self) -> str:
        return (
            f"{self.requirement} not satisfied by installed "
            f"{self.requirement.name}=={self.installed_version} (owner: {self.owner})"
        )


@dataclass
class DepPlan:
    to_install: list[Requirement] = field(default_factory=list)
    skipped: list[tuple[Requirement, str]] = field(default_factory=list)
    conflicts: list[DepConflict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_specs(specs: Iterable[str]) -> list[Requirement]:
    """Parse PEP 508 specifier strings into ``Requirement`` objects.

    Rejects VCS/URL forms and duplicate package names. Same validation
    as :meth:`PluginMetadata._dependencies_valid` — kept here so callers
    that pre-validated metadata can reuse the parsed objects, and so
    runtime code (not just Pydantic) reads idiomatically.
    """
    out: list[Requirement] = []
    seen: dict[str, str] = {}
    for raw in specs:
        try:
            req = Requirement(raw)
        except InvalidRequirement as e:
            raise DepInstallError(f"invalid dependency spec {raw!r}: {e}") from e
        if req.url:
            raise DepInstallError(
                f"dependency {raw!r}: VCS/URL forms not allowed; use PEP 508 specifiers"
            )
        canonical = canonicalize_name(req.name)
        if canonical in seen:
            raise DepInstallError(
                f"duplicate dependency {req.name!r} (also {seen[canonical]!r}); combine into one spec"
            )
        seen[canonical] = raw
        out.append(req)
    return out


# ---------------------------------------------------------------------------
# Planning (satisfied / conflict / to-install)
# ---------------------------------------------------------------------------


def plan_install(
    requirements: list[Requirement],
    *,
    attribute_conflict: Callable[[str], str | None] | None = None,
) -> DepPlan:
    """Sort each requirement into ``skipped`` / ``to_install`` / ``conflicts``.

    ``attribute_conflict(canonical_name)`` returns a human-readable owner
    (e.g. ``"framework core dependency"``, ``"plugin foo"``) for the package
    when a conflict is detected. Falls back to ``"external (already in
    environment)"`` if ``None`` or returns ``None``.

    Marker-gated requirements (``"x>=1; python_version < '3.10'"``) whose
    marker evaluates to False in the current environment are dropped.
    """
    plan = DepPlan()
    for req in requirements:
        if req.marker is not None and not req.marker.evaluate():
            logger.debug("Skipping %s — marker did not match current env", req)
            continue
        try:
            installed = importlib_metadata.version(req.name)
        except importlib_metadata.PackageNotFoundError:
            plan.to_install.append(req)
            continue
        if not req.specifier or req.specifier.contains(installed, prereleases=True):
            plan.skipped.append((req, installed))
            continue
        owner = (
            attribute_conflict(canonicalize_name(req.name)) if attribute_conflict else None
        ) or "external (already in environment)"
        plan.conflicts.append(DepConflict(req, installed, owner))
    return plan


# ---------------------------------------------------------------------------
# Installer detection + subprocess invocation
# ---------------------------------------------------------------------------


def detect_installer() -> tuple[str, list[str]]:
    """Return ``(label, argv_prefix)`` for the chosen installer.

    Prefers ``uv`` (with ``--python <sys.executable>`` so packages land in
    the framework's interpreter). Falls back to ``python -m pip``. Raises
    :class:`DepInstallError` if neither is available.
    """
    uv_path = shutil.which("uv")
    if uv_path:
        return ("uv", [uv_path, "pip", "install", "--python", sys.executable])
    try:
        pip_check = subprocess.run(  # noqa: S603 — args fully controlled
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pip_check = None
    if pip_check is not None and pip_check.returncode == 0:
        return ("pip", [sys.executable, "-m", "pip", "install"])
    raise DepInstallError(
        "no installer available: neither 'uv' (on PATH) nor 'pip' (python -m pip) usable"
    )


def run_install(
    requirements: list[Requirement],
    *,
    reinstall: bool = False,
    timeout_seconds: int | None = None,
) -> None:
    """Spawn the installer subprocess. Raises :class:`DepInstallError` on
    timeout, missing binary, or non-zero exit. stdout/stderr are inherited
    so users see pip/uv progress in real time."""
    if not requirements:
        return
    label, argv = detect_installer()
    if reinstall:
        argv.append("--upgrade")
    argv.extend(str(r) for r in requirements)
    timeout = timeout_seconds if timeout_seconds is not None else _resolved_timeout()
    logger.info(
        "Installing %d plugin dep(s) via %s: %s",
        len(requirements),
        label,
        [str(r) for r in requirements],
    )
    try:
        result = subprocess.run(  # noqa: S603 — args fully controlled
            argv,
            timeout=timeout,
            text=True,
        )
    except subprocess.TimeoutExpired as e:
        raise DepInstallError(
            f"dependency install timed out after {timeout}s ({label}): "
            f"{[str(r) for r in requirements]}"
        ) from e
    except FileNotFoundError as e:
        raise DepInstallError(f"installer disappeared mid-run: {e}") from e
    if result.returncode != 0:
        raise DepInstallError(
            f"{label} install failed (exit {result.returncode}) for: "
            f"{[str(r) for r in requirements]}"
        )


def _resolved_timeout() -> int:
    raw = os.environ.get("DEEPTRADE_DEP_INSTALL_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError("must be positive")
        return v
    except ValueError:
        logger.warning(
            "Invalid DEEPTRADE_DEP_INSTALL_TIMEOUT=%r; using default %ds",
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS

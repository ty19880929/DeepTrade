"""Plugin dependency resolution & installation.

Called by :class:`PluginManager` during install / upgrade to satisfy a
plugin's declared third-party Python dependencies (PEP 508 specifiers
in ``deeptrade_plugin.yaml::dependencies``).

Design context: ``CHANGELOG.md`` v0.4.0 (per-plugin Python dependency
management; framework-interpreter install model rather than per-plugin venv).

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
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300
# Plan §2.6: dry-run preflight gets its own short timeout so a stuck `uv`
# can't block the real install path.
DRY_RUN_TIMEOUT_SECONDS = 60


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


# ---------------------------------------------------------------------------
# v0.7 H6 — dry-run preflight + dep snapshot
# ---------------------------------------------------------------------------


def framework_core_canonicals() -> set[str]:
    """Return canonical names of every direct dependency declared by the
    installed ``deeptrade-quant`` distribution. Used by the dry-run
    preflight (H6-a) to flag plugin installs that would silently mutate
    framework deps.

    Returns an empty set when the framework isn't installed via a wheel
    (running from source without ``pip install -e``); the protection is
    best-effort and shouldn't block development setups."""
    try:
        dist = importlib_metadata.distribution("deeptrade-quant")
    except importlib_metadata.PackageNotFoundError:
        return set()
    out: set[str] = set()
    for raw in dist.requires or []:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        # Drop extras-only requirements (e.g. dev / plugin-runtime entries
        # whose marker is "extra == 'dev'"). They are not part of the
        # base install footprint we want to protect.
        if req.marker is not None:
            try:
                if not req.marker.evaluate({"extra": ""}):
                    continue
            except Exception:  # noqa: BLE001 — marker eval edge cases
                continue
        out.add(canonicalize_name(req.name))
    return out


def _parse_dry_run_changes(text: str, watched: set[str]) -> set[str]:
    """Pull canonical package names out of a uv dry-run output that appear
    with a change-indicating marker. Returns names that intersect
    ``watched``.

    uv's dry-run lines we treat as "would touch the existing install":

    * ``- pkg==ver``  — would remove (or downgrade away from current)
    * ``~ pkg ...``    — would update/downgrade in place

    ``+ pkg==ver`` lines are skipped because they represent a fresh
    install — by definition that package wasn't already in the env, so it
    cannot be a framework core dep currently in use."""
    if not watched:
        return set()
    affected: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        if not (stripped.startswith("- ") or stripped.startswith("~ ")):
            continue
        # Grab the first whitespace-separated token after the marker,
        # then trim any version suffix (``pkg==1.2.3`` → ``pkg``;
        # ``pkg`` alone is also valid for the ``~`` "from a to b" form).
        rest = stripped[2:].lstrip().split()[0].split("==", 1)[0]
        if not rest:
            continue
        try:
            canonical = canonicalize_name(rest)
        except Exception:  # noqa: BLE001 — defensive against odd uv output
            continue
        if canonical in watched:
            affected.add(canonical)
    return affected


def preflight_dry_run(
    requirements: list[Requirement],
    *,
    watched: set[str] | None = None,
    timeout_seconds: int = DRY_RUN_TIMEOUT_SECONDS,
) -> set[str]:
    """Best-effort preflight: run ``uv pip install --dry-run`` against
    ``requirements`` and return the subset of ``watched`` (canonical
    names) the install would upgrade / downgrade / remove.

    Returns an empty set silently when:

    * ``requirements`` is empty;
    * ``uv`` is not on PATH (``pip`` has no equivalent dry-run shape);
    * uv exits non-zero (let the real install path surface the real error);
    * uv times out — we don't want to block on a stuck resolver.

    The caller decides what to do with the affected names: in the v0.7
    H6 plan, ``PluginManager._handle_dependencies`` raises ``DepInstallError``
    when the set is non-empty and ``--allow-core-bump`` wasn't passed."""
    if not requirements:
        return set()
    if watched is None:
        watched = framework_core_canonicals()
    if not watched:
        return set()

    uv_path = shutil.which("uv")
    if not uv_path:
        # ``pip`` has no analogous structured dry-run output; fall back to
        # no preflight rather than block the install path.
        return set()

    argv = [uv_path, "pip", "install", "--dry-run", "--python", sys.executable]
    argv.extend(str(r) for r in requirements)
    try:
        result = subprocess.run(  # noqa: S603 — args fully controlled
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("uv dry-run preflight skipped: %s", e)
        return set()

    if result.returncode != 0:
        logger.info(
            "uv dry-run exited %d; skipping core-bump preflight (real install "
            "will surface the underlying resolver error)",
            result.returncode,
        )
        return set()

    # uv prints the change plan to stderr; stdout used as fallback for
    # uv versions that route it differently.
    output = result.stderr or result.stdout or ""
    return _parse_dry_run_changes(output, watched)


def _snapshot_argv() -> list[str] | None:
    """Pick the freeze-list command to use for snapshots.

    Mirrors :func:`detect_installer` priority: prefer ``uv pip list``
    (works on uv-managed venvs which often ship without pip) and fall
    back to ``python -m pip list``. Returns ``None`` when neither path
    is usable — snapshot becomes a no-op rather than blocking install."""
    uv_path = shutil.which("uv")
    if uv_path:
        return [uv_path, "pip", "list", "--python", sys.executable, "--format=freeze"]
    try:
        check = subprocess.run(  # noqa: S603 — args fully controlled
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if check.returncode == 0:
        return [sys.executable, "-m", "pip", "list", "--format=freeze"]
    return None


def write_dep_snapshot(plugin_id: str, dir_root: Path) -> Path | None:
    """Capture a ``pip list --format=freeze`` baseline to
    ``<dir_root>/<plugin_id>/pre-install-<UTC-ISO>.txt`` so users have a
    point-in-time record to diff against when an install goes sideways.

    The snapshotting subprocess prefers ``uv pip list`` (uv venvs often
    ship without pip) and falls back to ``python -m pip list``. Returns
    the file path on success; ``None`` if neither is available or the
    write failed (best-effort — never aborts install)."""
    argv = _snapshot_argv()
    if argv is None:
        logger.warning("dep snapshot skipped — neither uv nor pip is usable for `pip list`")
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — args fully controlled
            argv,
            capture_output=True,
            text=True,
            timeout=DRY_RUN_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("dep snapshot skipped — list subprocess failed: %s", e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "dep snapshot skipped — list exited %d:\n%s",
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return None

    target_dir = dir_root / plugin_id
    target_dir.mkdir(parents=True, exist_ok=True)
    # Use UTC for the filename so multi-host installs collated from one
    # NAS sort sensibly; safe-ish for filenames across OSes (no ``:``).
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = target_dir / f"pre-install-{ts}.txt"
    try:
        path.write_text(proc.stdout, encoding="utf-8")
    except OSError as e:
        logger.warning("dep snapshot write failed: %s", e)
        return None
    return path

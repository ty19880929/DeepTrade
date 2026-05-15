"""Plugin dependency management tests.

Covers (per ``docs/plugin_dependency_management_design.md`` §6):
  1. ``PluginMetadata.dependencies`` field validation
  2. ``dep_installer`` parsing / plan / installer detection
  3. ``PluginManager`` install + upgrade integration with deps
  4. CLI flags (``--no-deps`` / ``--reinstall-deps``) and summary line
"""

from __future__ import annotations

import hashlib
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pydantic import ValidationError
from typer.testing import CliRunner

from deeptrade.cli import app
from deeptrade.core import dep_installer
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.dep_installer import (
    DepInstallError,
    detect_installer,
    parse_specs,
    plan_install,
)
from deeptrade.core.plugin_manager import (
    PluginInstallError,
    PluginManager,
    _load_metadata_yaml,
    summarize_for_install,
)
from deeptrade.plugins_api.metadata import PluginMetadata

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _minimal_meta_dict() -> dict:
    return {
        "plugin_id": "minimal-x",
        "name": "X",
        "version": "0.1.0",
        "type": "strategy",
        "api_version": "1",
        "entrypoint": "minimal_x.plugin:X",
        "description": "x",
        "permissions": {"llm": False, "llm_tools": False},
        "tables": [{"name": "x_t", "description": "x", "purge_on_uninstall": True}],
        "migrations": [
            {
                "version": "20260501_001",
                "file": "migrations/20260501_001_init.sql",
                "checksum": "sha256:" + "0" * 64,
            }
        ],
    }


def _fake_version_factory(installed: dict[str, str]):
    """Build a stand-in for ``importlib_metadata.version`` driven by a map."""

    def fake(name: str) -> str:
        key = canonicalize_name(name)
        if key in installed:
            return installed[key]
        raise importlib_metadata.PackageNotFoundError(name)

    return fake


def _raise_pkg_not_found(_name: str = "x"):
    raise importlib_metadata.PackageNotFoundError(_name)


def _make_plugin_dir(
    base: Path,
    plugin_id: str,
    *,
    version: str = "0.1.0",
    dependencies: list[str] | None = None,
) -> Path:
    pkg = plugin_id.replace("-", "_")
    src = base / f"_src_{plugin_id}_{version.replace('.', '_')}"
    src.mkdir(exist_ok=True)
    pkg_dir = src / pkg
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    mig_dir = src / "migrations"
    mig_dir.mkdir(exist_ok=True)
    table_name = f"{pkg}_t"
    # Version-independent body so historical migrations keep a stable
    # checksum across plugin-version bumps (T09 refuses mutation).
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER);\n"
    (mig_dir / "20260501_001_init.sql").write_text(sql)
    checksum = "sha256:" + hashlib.sha256(sql.encode()).hexdigest()
    yaml_text = (
        f"plugin_id: {plugin_id}\n"
        f"name: Test {plugin_id}\n"
        f"version: {version}\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg}.plugin:TestPlugin\n"
        "description: tiny test plugin\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {table_name}\n"
        "    description: test\n"
        "    purge_on_uninstall: true\n"
    )
    if dependencies:
        yaml_text += "dependencies:\n"
        for d in dependencies:
            yaml_text += f'  - "{d}"\n'
    (src / "deeptrade_plugin.yaml").write_text(yaml_text)
    return src


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


@pytest.fixture
def captured_install(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture ``run_install`` invocations from PluginManager; never spawn pip/uv."""
    calls: list[list[str]] = []

    def fake_run_install(reqs, *, reinstall: bool = False, timeout_seconds=None):
        # Mirror real short-circuit on empty
        if not reqs:
            return
        calls.append([str(r) for r in reqs])

    monkeypatch.setattr("deeptrade.core.plugin_manager.run_install", fake_run_install)
    return calls


# ---------------------------------------------------------------------------
# 1. PluginMetadata.dependencies validation
# ---------------------------------------------------------------------------


def test_metadata_dependencies_default_empty():
    meta = PluginMetadata.model_validate(_minimal_meta_dict())
    assert meta.dependencies == []


def test_metadata_dependencies_legal_pep508():
    md = _minimal_meta_dict()
    md["dependencies"] = ["pandas>=2.2,<3", "ta-lib>=0.4"]
    meta = PluginMetadata.model_validate(md)
    assert meta.dependencies == ["pandas>=2.2,<3", "ta-lib>=0.4"]


def test_metadata_dependencies_rejects_invalid_spec():
    md = _minimal_meta_dict()
    md["dependencies"] = ["%%not a spec%%"]
    with pytest.raises(ValidationError, match="invalid dependency"):
        PluginMetadata.model_validate(md)


def test_metadata_dependencies_rejects_vcs_url():
    md = _minimal_meta_dict()
    md["dependencies"] = ["foo @ git+https://github.com/x/y"]
    with pytest.raises(ValidationError, match="VCS/URL"):
        PluginMetadata.model_validate(md)


def test_metadata_dependencies_rejects_duplicate_package_case_insensitive():
    md = _minimal_meta_dict()
    md["dependencies"] = ["pandas>=2", "Pandas<3"]
    with pytest.raises(ValidationError, match="duplicate dependency"):
        PluginMetadata.model_validate(md)


# ---------------------------------------------------------------------------
# 2. dep_installer: parse_specs / plan_install / detect_installer
# ---------------------------------------------------------------------------


def test_parse_specs_round_trip():
    parsed = parse_specs(["pandas>=2.2", "numpy<2"])
    assert [str(r) for r in parsed] == ["pandas>=2.2", "numpy<2"]


def test_parse_specs_rejects_vcs():
    with pytest.raises(DepInstallError, match="VCS/URL"):
        parse_specs(["foo @ git+https://x"])


def test_parse_specs_rejects_duplicates():
    with pytest.raises(DepInstallError, match="duplicate"):
        parse_specs(["pandas>=2", "PANDAS<3"])


def test_plan_skipped_when_already_satisfied(monkeypatch):
    monkeypatch.setattr(
        dep_installer.importlib_metadata, "version", _fake_version_factory({"pandas": "2.2.3"})
    )
    plan = plan_install([Requirement("pandas>=2.0")])
    assert len(plan.skipped) == 1
    assert plan.to_install == []
    assert plan.conflicts == []


def test_plan_to_install_when_missing(monkeypatch):
    monkeypatch.setattr(dep_installer.importlib_metadata, "version", _fake_version_factory({}))
    plan = plan_install([Requirement("definitely-not-installed-xyz>=1")])
    assert len(plan.to_install) == 1
    assert plan.skipped == []
    assert plan.conflicts == []


def test_plan_conflict_with_attribution(monkeypatch):
    monkeypatch.setattr(
        dep_installer.importlib_metadata, "version", _fake_version_factory({"pandas": "2.2.3"})
    )
    plan = plan_install(
        [Requirement("pandas<2.0")],
        attribute_conflict=lambda _name: "framework core dependency",
    )
    assert len(plan.conflicts) == 1
    c = plan.conflicts[0]
    assert c.installed_version == "2.2.3"
    assert c.owner == "framework core dependency"
    assert "pandas<2.0" in str(c)


def test_plan_marker_filters_out_non_matching():
    """A marker that evaluates False in current env removes the requirement."""
    plan = plan_install([Requirement('foo>=1; python_version < "2.0"')])
    assert plan.to_install == []
    assert plan.skipped == []
    assert plan.conflicts == []


def test_detect_installer_prefers_uv(monkeypatch):
    monkeypatch.setattr(
        dep_installer.shutil, "which", lambda name: "/fake/bin/uv" if name == "uv" else None
    )
    label, argv = detect_installer()
    assert label == "uv"
    assert argv[0] == "/fake/bin/uv"
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == sys.executable


def test_detect_installer_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(dep_installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(dep_installer.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
    label, argv = detect_installer()
    assert label == "pip"
    assert argv[:3] == [sys.executable, "-m", "pip"]
    assert "install" in argv


def test_detect_installer_raises_when_none_available(monkeypatch):
    monkeypatch.setattr(dep_installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(dep_installer.subprocess, "run", lambda *a, **kw: MagicMock(returncode=1))
    with pytest.raises(DepInstallError, match="no installer available"):
        detect_installer()


def test_run_install_short_circuits_on_empty(monkeypatch):
    """No installer invocation when there's nothing to install."""
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return MagicMock(returncode=0)

    monkeypatch.setattr(dep_installer.subprocess, "run", _spy)
    monkeypatch.setattr(dep_installer.shutil, "which", lambda _: "/fake/uv")
    dep_installer.run_install([])
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# 3. PluginManager integration
# ---------------------------------------------------------------------------


def test_install_with_unmet_dep_calls_runner(
    home: Path, captured_install: list[list[str]], monkeypatch: pytest.MonkeyPatch
):
    """install() routes unmet deps to the installer."""
    src = _make_plugin_dir(home, "depplug", dependencies=["definitely-not-installed-xyz>=1"])
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src)
    finally:
        db.close()
    assert captured_install == [["definitely-not-installed-xyz>=1"]]


def test_install_satisfied_dep_does_not_call_runner(home: Path, captured_install: list[list[str]]):
    """When framework already provides pandas>=2.2, no installer call."""
    src = _make_plugin_dir(home, "satplug", dependencies=["pandas>=2.0"])
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src)
    finally:
        db.close()
    assert captured_install == []


def test_install_no_deps_flag_skips_runner(home: Path, captured_install: list[list[str]]):
    src = _make_plugin_dir(home, "skipplug", dependencies=["definitely-not-installed-xyz>=1"])
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src, install_deps=False)
    finally:
        db.close()
    assert captured_install == []


def test_install_reinstall_deps_passes_all_specs(
    home: Path, captured_install: list[list[str]], monkeypatch: pytest.MonkeyPatch
):
    """--reinstall-deps re-runs installer on every declared spec even if satisfied."""
    captured: list[tuple[list[str], bool]] = []

    def fake_run_install(reqs, *, reinstall: bool = False, timeout_seconds=None):
        captured.append(([str(r) for r in reqs], reinstall))

    monkeypatch.setattr("deeptrade.core.plugin_manager.run_install", fake_run_install)

    src = _make_plugin_dir(home, "reinst", dependencies=["pandas>=2.0"])
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src, reinstall_deps=True)
    finally:
        db.close()
    assert captured == [(["pandas>=2.0"], True)]


def test_install_conflict_with_framework_dep_rejected(
    home: Path, captured_install: list[list[str]]
):
    """pandas<1.0 against framework's pandas>=2.2 → hard reject."""
    src = _make_plugin_dir(home, "conflplug", dependencies=["pandas<1.0"])
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="conflict"):
            PluginManager(db).install(src)
    finally:
        db.close()
    assert captured_install == []


def test_install_dep_failure_cleans_install_path(home: Path, monkeypatch: pytest.MonkeyPatch):
    """run_install raises → copied install_path removed; DB has no row."""

    def fail(_reqs, *, reinstall: bool = False, timeout_seconds=None):
        raise DepInstallError("simulated install failure")

    monkeypatch.setattr("deeptrade.core.plugin_manager.run_install", fail)
    src = _make_plugin_dir(home, "failplug", dependencies=["definitely-not-installed-xyz>=1"])
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="simulated install failure"):
            PluginManager(db).install(src)
        mgr = PluginManager(db)
        assert not (mgr._install_root / "failplug" / "0.1.0").exists()
        rows = db.fetchall("SELECT plugin_id FROM plugins WHERE plugin_id = ?", ("failplug",))
        assert rows == []
    finally:
        db.close()


def test_conflict_attribution_points_at_other_plugin(
    home: Path, captured_install: list[list[str]], monkeypatch: pytest.MonkeyPatch
):
    """When framework doesn't own the package but another plugin declared it,
    conflict attribution names that plugin."""
    # Pretend numpy is installed at 1.25 in env; framework owns nothing.
    monkeypatch.setattr(
        dep_installer.importlib_metadata,
        "version",
        _fake_version_factory({"numpy": "1.25.0"}),
    )
    monkeypatch.setattr(
        "deeptrade.core.plugin_manager.importlib_metadata.distribution",
        lambda _name: (_ for _ in ()).throw(importlib_metadata.PackageNotFoundError("x")),
    )

    src1 = _make_plugin_dir(home, "owner-plug", dependencies=["numpy>=1.20"])
    src2 = _make_plugin_dir(home, "second-plug", dependencies=["numpy<1.0"])
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src1)
        with pytest.raises(PluginInstallError, match="owner-plug"):
            PluginManager(db).install(src2)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. Upgrade behaviour
# ---------------------------------------------------------------------------


def test_upgrade_new_dep_installs(home: Path, captured_install: list[list[str]]):
    src1 = _make_plugin_dir(home, "upplug", version="0.1.0")
    src2 = _make_plugin_dir(
        home,
        "upplug",
        version="0.2.0",
        dependencies=["definitely-not-installed-xyz>=1"],
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src1)
        captured_install.clear()
        PluginManager(db).upgrade(src2)
    finally:
        db.close()
    assert captured_install == [["definitely-not-installed-xyz>=1"]]


def test_upgrade_no_deps_flag_skips(home: Path, captured_install: list[list[str]]):
    src1 = _make_plugin_dir(home, "upplug2", version="0.1.0")
    src2 = _make_plugin_dir(
        home,
        "upplug2",
        version="0.2.0",
        dependencies=["definitely-not-installed-xyz>=1"],
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src1)
        captured_install.clear()
        PluginManager(db).upgrade(src2, install_deps=False)
    finally:
        db.close()
    assert captured_install == []


# ---------------------------------------------------------------------------
# 5. CLI summary line + flag pass-through
# ---------------------------------------------------------------------------


def test_summarize_includes_dependencies_line(home: Path):
    src = _make_plugin_dir(home, "sumplug", dependencies=["pandas>=2.2", "ta-lib>=0.4"])
    meta = _load_metadata_yaml(src / "deeptrade_plugin.yaml")
    out = summarize_for_install(meta, src)
    # v0.5 — summary labels are Chinese; assert the label and each spec line.
    assert "依赖" in out
    assert "pandas>=2.2" in out
    assert "ta-lib>=0.4" in out


def test_summarize_dependencies_none_when_empty(home: Path):
    src = _make_plugin_dir(home, "noplug")
    meta = _load_metadata_yaml(src / "deeptrade_plugin.yaml")
    out = summarize_for_install(meta, src)
    assert "（无）" in out


def test_cli_install_no_deps_flag_passes_through(home: Path, monkeypatch: pytest.MonkeyPatch):
    """`plugin install --no-deps` makes PluginManager skip dep installation."""
    src = _make_plugin_dir(home, "cliplug", dependencies=["definitely-not-installed-xyz>=1"])
    fake_run = MagicMock()
    monkeypatch.setattr("deeptrade.core.plugin_manager.run_install", fake_run)
    runner = CliRunner()
    result = runner.invoke(app, ["plugin", "install", str(src), "-y", "--no-deps"])
    assert result.exit_code == 0, result.output
    fake_run.assert_not_called()


def test_cli_install_reinstall_deps_flag(home: Path, monkeypatch: pytest.MonkeyPatch):
    """`--reinstall-deps` reaches run_install with reinstall=True for all specs."""
    src = _make_plugin_dir(home, "reiplug", dependencies=["pandas>=2.0"])
    seen: list[tuple[list[str], bool]] = []

    def fake(reqs, *, reinstall: bool = False, timeout_seconds=None):
        seen.append(([str(r) for r in reqs], reinstall))

    monkeypatch.setattr("deeptrade.core.plugin_manager.run_install", fake)
    runner = CliRunner()
    result = runner.invoke(app, ["plugin", "install", str(src), "-y", "--reinstall-deps"])
    assert result.exit_code == 0, result.output
    assert seen == [(["pandas>=2.0"], True)]

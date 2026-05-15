"""Plugin trust-boundary tests (v0.5).

Cover the model-validator + install pipeline rejections introduced for the
review's H1 / H3 findings:

* T17 — plugin cannot declare a framework-reserved table name in metadata.
* T18 — table_prefix: v0.5 warns on prefix mismatch when prefix is omitted,
        hard-fails when an explicit prefix is declared and violated.
* T22 — entrypoint top-level package ``deeptrade`` is rejected (would shadow
        the framework on import).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.plugin_manager import PluginInstallError, PluginManager
from deeptrade.plugins_api.metadata import (
    RESERVED_TABLE_NAMES,
    RESERVED_TOP_PACKAGES,
    PluginMetadata,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


def _minimal_metadata_dict(
    *,
    plugin_id: str = "good-plug",
    entrypoint: str | None = None,
    tables: list[dict[str, object]] | None = None,
    table_prefix: str | None = None,
) -> dict[str, object]:
    """A spec dict guaranteed to satisfy every validator unless caller
    overrides. ``checksum`` is a placeholder sha256 (no file present);
    sufficient for parse-time tests but not for install."""
    pkg = plugin_id.replace("-", "_")
    spec: dict[str, object] = {
        "plugin_id": plugin_id,
        "name": f"Test {plugin_id}",
        "version": "0.1.0",
        "type": "strategy",
        "api_version": "1",
        "entrypoint": entrypoint or f"{pkg}.plugin:TestPlugin",
        "description": "minimal test plugin",
        "permissions": {"llm": False, "llm_tools": False},
        "tables": tables
        if tables is not None
        else [
            {
                "name": f"{pkg}_t",
                "description": "test",
                "purge_on_uninstall": True,
            }
        ],
        "migrations": [
            {
                "version": "20260501_001",
                "file": "migrations/20260501_001_init.sql",
                "checksum": "sha256:" + "0" * 64,
            }
        ],
    }
    if table_prefix is not None:
        spec["table_prefix"] = table_prefix
    return spec


def _make_plugin_dir_for_install(
    base: Path,
    *,
    plugin_id: str,
    entrypoint: str | None = None,
    tables: list[tuple[str, str]] | None = None,
    table_prefix: str | None = None,
) -> Path:
    """Synthesize a plugin source tree that's parse-valid AND installable.

    ``tables`` is a list of ``(table_name, ddl_line)`` tuples; defaults to a
    single table named ``<pkg>_t``.
    """
    pkg = plugin_id.replace("-", "_")
    src = base / f"_src_{plugin_id}"
    src.mkdir()
    (src / pkg).mkdir()
    (src / pkg / "__init__.py").write_text("")
    (src / pkg / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    if tables is None:
        tables = [(f"{pkg}_t", f"CREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER);")]
    ddl = "\n".join(d for _, d in tables) + "\n"
    mig_dir = src / "migrations"
    mig_dir.mkdir()
    (mig_dir / "20260501_001_init.sql").write_text(ddl)
    checksum = "sha256:" + hashlib.sha256(ddl.encode()).hexdigest()

    yaml_body = (
        f"plugin_id: {plugin_id}\n"
        f"name: Test {plugin_id}\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {entrypoint or f'{pkg}.plugin:TestPlugin'}\n"
        "description: minimal test plugin\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
    )
    for tname, _ in tables:
        yaml_body += f"  - name: {tname}\n    description: t\n    purge_on_uninstall: true\n"
    if table_prefix is not None:
        yaml_body += f'table_prefix: "{table_prefix}"\n'
    (src / "deeptrade_plugin.yaml").write_text(yaml_body)
    return src


# ---------------------------------------------------------------------------
# T17 — RESERVED_TABLE_NAMES guard
# ---------------------------------------------------------------------------


def test_reserved_table_names_match_framework_schema() -> None:
    """If a new framework table lands in ``20260509_001_init.sql`` etc., the
    constant must be updated alongside — fail loudly otherwise."""
    assert "app_config" in RESERVED_TABLE_NAMES
    assert "plugins" in RESERVED_TABLE_NAMES
    assert "plugin_tables" in RESERVED_TABLE_NAMES
    assert "plugin_schema_migrations" in RESERVED_TABLE_NAMES
    assert "schema_migrations" in RESERVED_TABLE_NAMES
    assert "secret_store" in RESERVED_TABLE_NAMES
    assert "llm_calls" in RESERVED_TABLE_NAMES
    assert "tushare_calls" in RESERVED_TABLE_NAMES
    assert "tushare_sync_state" in RESERVED_TABLE_NAMES
    assert "tushare_cache_blob" in RESERVED_TABLE_NAMES


@pytest.mark.parametrize("reserved", sorted(RESERVED_TABLE_NAMES))
def test_plugin_cannot_declare_core_table(reserved: str) -> None:
    """Parse-time refusal: metadata claiming a reserved name → ValidationError.

    Hits every entry in :data:`RESERVED_TABLE_NAMES` so a future framework
    table accidentally left out of the constant trips the test that adds it
    instead of being silently claimable by plugins."""
    spec = _minimal_metadata_dict(
        tables=[
            {"name": reserved, "description": "evil", "purge_on_uninstall": True},
        ],
    )
    with pytest.raises(ValidationError, match="framework-reserved table name"):
        PluginMetadata.model_validate(spec)


def test_install_rejects_core_table_via_install_pipeline(home: Path) -> None:
    """The same guard, surfaced through the install pipeline as
    ``PluginInstallError`` (``_load_metadata_yaml`` wraps the parse error)."""
    src = _make_plugin_dir_for_install(
        home,
        plugin_id="evil-plug",
        tables=[("app_config", "CREATE TABLE IF NOT EXISTS app_config (k TEXT);")],
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="framework-reserved"):
            PluginManager(db).install(src, install_deps=False)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T18 — table_prefix: warn (v0.5) on derive mismatch; hard-fail when declared
# ---------------------------------------------------------------------------


def test_plugin_table_prefix_warns_when_omitted_and_mismatched() -> None:
    """v0.5 behaviour: no explicit ``table_prefix`` + a table outside the
    plugin_id-derived namespace → ``DeprecationWarning`` (not hard fail)."""
    spec = _minimal_metadata_dict(
        plugin_id="prefix-plug",
        tables=[
            {"name": "wrong_name", "description": "t", "purge_on_uninstall": True},
        ],
    )
    with pytest.warns(DeprecationWarning, match="derived table prefix 'prefix_plug_'"):
        meta = PluginMetadata.model_validate(spec)
    # Parse still succeeds — the warning does not abort install in v0.5.
    assert meta.tables[0].name == "wrong_name"


def test_plugin_table_prefix_no_warning_when_tables_match_derived() -> None:
    """Tables aligned with the derived prefix must NOT trigger the warning,
    otherwise correctly-namespaced plugins would emit noise on every install."""
    import warnings

    spec = _minimal_metadata_dict(
        plugin_id="quiet-plug",
        tables=[
            {"name": "quiet_plug_t", "description": "t", "purge_on_uninstall": True},
        ],
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        PluginMetadata.model_validate(spec)  # would raise if warning fired


def test_plugin_table_prefix_explicit_hard_fails_on_mismatch() -> None:
    """When ``table_prefix`` is declared explicitly, mismatch is a hard error
    immediately — this is the v0.6 default path, exposed today for plugins
    that want to opt in."""
    spec = _minimal_metadata_dict(
        plugin_id="explicit-plug",
        table_prefix="explicit_plug_",
        tables=[
            {"name": "other_name", "description": "t", "purge_on_uninstall": True},
        ],
    )
    with pytest.raises(ValidationError, match="do not start with declared table_prefix"):
        PluginMetadata.model_validate(spec)


def test_plugin_table_prefix_explicit_accepts_matching() -> None:
    spec = _minimal_metadata_dict(
        plugin_id="explicit-plug",
        table_prefix="explicit_plug_",
        tables=[
            {"name": "explicit_plug_runs", "description": "t", "purge_on_uninstall": True},
        ],
    )
    meta = PluginMetadata.model_validate(spec)
    assert meta.table_prefix == "explicit_plug_"


# ---------------------------------------------------------------------------
# T22 — entrypoint top_pkg constraint
# ---------------------------------------------------------------------------


def test_install_rejects_entrypoint_top_package_deeptrade() -> None:
    """A plugin claiming ``deeptrade.*`` as its entrypoint top package would
    shadow the framework on import — must be rejected at parse time before
    any file is copied."""
    # plugin_id == "deeptrade" satisfies the equality check but is itself in
    # RESERVED_TOP_PACKAGES, so the reserved-set branch fires.
    spec = _minimal_metadata_dict(
        plugin_id="deeptrade",
        entrypoint="deeptrade.plugin:Evil",
        tables=[
            {"name": "deeptrade_t", "description": "t", "purge_on_uninstall": True},
        ],
    )
    with pytest.raises(ValidationError, match="reserved by the framework"):
        PluginMetadata.model_validate(spec)


def test_install_rejects_entrypoint_top_package_mismatched_to_plugin_id() -> None:
    """Even when the top package is not reserved, it must equal the
    plugin_id (with ``-`` → ``_``) — otherwise the sys.path eviction in
    ``_load_entrypoint`` keys off a name the framework has no record of."""
    spec = _minimal_metadata_dict(
        plugin_id="mismatch-plug",
        entrypoint="something_else.plugin:Cls",
    )
    with pytest.raises(ValidationError, match="does not match plugin_id-derived"):
        PluginMetadata.model_validate(spec)


# ---------------------------------------------------------------------------
# T19/T20 — migration schema-diff
# ---------------------------------------------------------------------------


def test_plugin_migration_cannot_create_undeclared_table(home: Path) -> None:
    """A migration that creates a table absent from ``metadata.tables`` must
    fail the install — the metadata is the framework's only authoritative
    record of what a plugin owns, and a hidden table cannot be purged on
    uninstall."""
    plugin_id = "stealth-plug"
    pkg = plugin_id.replace("-", "_")
    # DDL creates TWO tables but only ONE is declared.
    sneaky_ddl = (
        f"CREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER);\n"
        f"CREATE TABLE IF NOT EXISTS {pkg}_secret (id INTEGER);\n"
    )
    src = home / f"_src_{plugin_id}"
    src.mkdir()
    (src / pkg).mkdir()
    (src / pkg / "__init__.py").write_text("")
    (src / pkg / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    (src / "migrations").mkdir()
    (src / "migrations" / "20260501_001_init.sql").write_text(sneaky_ddl)
    checksum = "sha256:" + hashlib.sha256(sneaky_ddl.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: Stealth\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg}.plugin:TestPlugin\n"
        "description: stealth\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {pkg}_t\n    description: declared\n    purge_on_uninstall: true\n"
    )

    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="not declared in metadata.tables"):
            PluginManager(db).install(src, install_deps=False)
        # rollback must leave NO trace of either table
        leaked = {
            r[0]
            for r in db.fetchall(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name IN (?, ?)",
                (f"{pkg}_t", f"{pkg}_secret"),
            )
        }
        assert leaked == set(), f"rollback failed; leaked tables: {leaked}"
    finally:
        db.close()


def test_plugin_migration_cannot_drop_core_table(home: Path) -> None:
    """A migration that DROPs a framework-reserved table must be rejected at
    install. T01 prevents the table name from appearing in
    ``metadata.tables``; T05 catches DROPs even when the table is never
    declared."""
    plugin_id = "evil-plug"
    pkg = plugin_id.replace("-", "_")
    # Migration creates one legitimately-declared table AND drops a framework
    # one out from under the framework.
    ddl = f"DROP TABLE app_config;\nCREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER);\n"
    src = home / f"_src_{plugin_id}"
    src.mkdir()
    (src / pkg).mkdir()
    (src / pkg / "__init__.py").write_text("")
    (src / pkg / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    (src / "migrations").mkdir()
    (src / "migrations" / "20260501_001_init.sql").write_text(ddl)
    checksum = "sha256:" + hashlib.sha256(ddl.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: Evil\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg}.plugin:TestPlugin\n"
        "description: evil\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {pkg}_t\n    description: cover\n    purge_on_uninstall: true\n"
    )

    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="framework-reserved"):
            PluginManager(db).install(src, install_deps=False)
        # Transaction rollback must restore app_config
        assert db.fetchone(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='app_config'"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T23 — Plugin protocol conformance (isinstance check after load)
# ---------------------------------------------------------------------------


def test_install_requires_dispatch_method(home: Path) -> None:
    """A class with ``validate_static`` but no ``dispatch`` fails the
    runtime-checkable Plugin protocol — caught at install before the
    plugin's first invocation."""
    plugin_id = "no-dispatch-plug"
    pkg = plugin_id.replace("-", "_")
    src = home / f"_src_{plugin_id}"
    src.mkdir()
    (src / pkg).mkdir()
    (src / pkg / "__init__.py").write_text("")
    # Intentionally omit ``dispatch``
    (src / pkg / "plugin.py").write_text(
        "class TestPlugin:\n    metadata = None\n    def validate_static(self, ctx): pass\n"
    )
    (src / "migrations").mkdir()
    ddl = f"CREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER);\n"
    (src / "migrations" / "20260501_001_init.sql").write_text(ddl)
    checksum = "sha256:" + hashlib.sha256(ddl.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: NoDispatch\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg}.plugin:TestPlugin\n"
        "description: missing dispatch\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {pkg}_t\n    description: t\n    purge_on_uninstall: true\n"
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="Plugin protocol"):
            PluginManager(db).install(src, install_deps=False)
        # Rollback must wipe registry rows AND the plugin's table.
        assert not db.fetchone("SELECT 1 FROM plugins WHERE plugin_id = ?", (plugin_id,))
        assert not db.fetchone(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            (f"{pkg}_t",),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T25 — upgrade refuses to apply when a historical migration's checksum
# changed (the plugin author edited a migration file in place)
# ---------------------------------------------------------------------------


def test_upgrade_rejects_changed_applied_migration_checksum(home: Path) -> None:
    """If an upgrade source bumps the version BUT changes the body of an
    already-applied migration, the recorded DB state no longer matches the
    metadata's claim — reject loudly rather than silently diverging."""
    plugin_id = "mutate-plug"
    pkg = plugin_id.replace("-", "_")

    def _write_plugin(version: str, ddl: str) -> Path:
        src = home / f"_src_{plugin_id}_{version.replace('.', '_')}"
        src.mkdir(exist_ok=True)
        (src / pkg).mkdir(exist_ok=True)
        (src / pkg / "__init__.py").write_text("")
        (src / pkg / "plugin.py").write_text(
            "class TestPlugin:\n"
            "    metadata = None\n"
            "    def validate_static(self, ctx): pass\n"
            "    def dispatch(self, argv): return 0\n"
        )
        mig_dir = src / "migrations"
        mig_dir.mkdir(exist_ok=True)
        (mig_dir / "20260501_001_init.sql").write_text(ddl)
        checksum = "sha256:" + hashlib.sha256(ddl.encode()).hexdigest()
        (src / "deeptrade_plugin.yaml").write_text(
            f"plugin_id: {plugin_id}\n"
            f"name: Mutator\n"
            f"version: {version}\n"
            "type: strategy\n"
            'api_version: "1"\n'
            f"entrypoint: {pkg}.plugin:TestPlugin\n"
            "description: mutator\n"
            "permissions:\n  llm: false\n  llm_tools: false\n"
            "migrations:\n"
            '  - version: "20260501_001"\n'
            "    file: migrations/20260501_001_init.sql\n"
            f'    checksum: "{checksum}"\n'
            "tables:\n"
            f"  - name: {pkg}_t\n    description: t\n    purge_on_uninstall: true\n"
        )
        return src

    original_ddl = f"CREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER);\n"
    tampered_ddl = f"CREATE TABLE IF NOT EXISTS {pkg}_t (id INTEGER, extra TEXT);\n"

    db = Database(home / "deeptrade.duckdb")
    try:
        mgr = PluginManager(db)
        # 1) Clean install of v0.1.0 with the original migration body.
        mgr.install(_write_plugin("0.1.0", original_ddl), install_deps=False)
        original_checksum_row = db.fetchone(
            "SELECT checksum FROM plugin_schema_migrations WHERE plugin_id = ? AND version = ?",
            (plugin_id, "20260501_001"),
        )
        assert original_checksum_row is not None

        # 2) Author bumps to v0.2.0 but edits the SAME migration version's
        #    body — different bytes, different sha256, same version string.
        with pytest.raises(PluginInstallError, match="checksum changed"):
            mgr.upgrade(_write_plugin("0.2.0", tampered_ddl), install_deps=False)

        # 3) The DB record must be untouched — refusing to upgrade must
        #    leave both the version row AND the migration checksum at v0.1.0
        #    state, otherwise a later retry would treat the tamper as
        #    "applied".
        row = db.fetchone("SELECT version FROM plugins WHERE plugin_id = ?", (plugin_id,))
        assert row is not None
        assert row[0] == "0.1.0"
        unchanged = db.fetchone(
            "SELECT checksum FROM plugin_schema_migrations WHERE plugin_id = ? AND version = ?",
            (plugin_id, "20260501_001"),
        )
        assert unchanged == original_checksum_row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T21 — defense in depth: purge refuses reserved tables even with poisoned
# plugin_tables rows
# ---------------------------------------------------------------------------


def test_uninstall_purge_refuses_core_table_even_if_in_plugin_tables(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Simulate a poisoned DB where ``plugin_tables`` lists a framework
    table for this plugin (bypassing the metadata-time guard). ``uninstall
    --purge`` must skip the reserved name, drop the legitimate tables, and
    leave the framework table intact.

    Sets ``affected_tables`` to NULL so the legacy fallback path is the one
    on test — i.e., the path that previously trusted ``plugin_tables`` and
    happily dropped ``app_config``."""
    import logging

    src = _make_plugin_dir_for_install(home, plugin_id="legit-plug")
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src, install_deps=False)
        # Poison: pretend the plugin owns app_config too.
        db.execute(
            "INSERT INTO plugin_tables(plugin_id, table_name, description, "
            "purge_on_uninstall) VALUES (?, ?, ?, TRUE)",
            ("legit-plug", "app_config", "poisoned"),
        )
        # Force the legacy fallback: nuke affected_tables so we go through
        # the plugin_tables-trusted code path.
        db.execute(
            "UPDATE plugin_schema_migrations SET affected_tables = NULL WHERE plugin_id = ?",
            ("legit-plug",),
        )

        assert db.fetchone("SELECT 1 FROM information_schema.tables WHERE table_name='app_config'")

        with caplog.at_level(logging.ERROR, logger="deeptrade.core.plugin_manager"):
            result = PluginManager(db).uninstall("legit-plug", purge=True)

        # Reserved table preserved
        assert db.fetchone(
            "SELECT 1 FROM information_schema.tables WHERE table_name='app_config'"
        ), "purge dropped a framework table — defense in depth failed"
        # Legitimate table gone
        assert not db.fetchone(
            "SELECT 1 FROM information_schema.tables WHERE table_name='legit_plug_t'"
        )
        # The poisoned row is not reported in dropped_tables
        assert "app_config" not in result["purged_tables"]
        assert "legit_plug_t" in result["purged_tables"]
        # Error log is the user-visible breadcrumb that something tried this
        assert any(
            "framework-reserved table" in rec.message and "app_config" in rec.message
            for rec in caplog.records
        ), "expected a logger.error breadcrumb for the refused DROP"
    finally:
        db.close()


def test_uninstall_purge_with_affected_tables_respects_record(home: Path) -> None:
    """When ``affected_tables`` is recorded (normal v0.5 install), uninstall
    --purge drops exactly the recorded set. Even if a row is manually
    INSERTed into ``plugin_tables`` for a table the plugin never actually
    created, the dirty row is NOT acted on."""
    src = _make_plugin_dir_for_install(home, plugin_id="record-plug")
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src, install_deps=False)
        # Poison plugin_tables with a non-existent extra table.
        db.execute(
            "INSERT INTO plugin_tables(plugin_id, table_name, description, "
            "purge_on_uninstall) VALUES (?, ?, ?, TRUE)",
            ("record-plug", "ghost_table", "did not exist"),
        )
        result = PluginManager(db).uninstall("record-plug", purge=True)
        # The framework-recorded affected_tables wins; ghost not dropped/listed.
        assert result["purged_tables"] == ["record_plug_t"]
    finally:
        db.close()


@pytest.mark.parametrize(
    "pkg",
    sorted(RESERVED_TOP_PACKAGES - {"deeptrade"}),
)
def test_entrypoint_top_package_rejects_every_reserved_name(pkg: str) -> None:
    """Each entry in :data:`RESERVED_TOP_PACKAGES` (other than ``deeptrade``,
    covered separately) must be unusable as a plugin's top package even when
    plugin_id equals the package name — guards the constant against future
    edits that drop entries."""
    # plugin_id may not match every reserved name (e.g. plugin_id must be
    # >=3 chars; all entries here satisfy that). Pick a plugin_id that
    # produces the same top_pkg so we exercise the RESERVED branch only.
    spec = _minimal_metadata_dict(
        plugin_id=pkg,
        entrypoint=f"{pkg}.plugin:Cls",
        tables=[
            {"name": f"{pkg}_t", "description": "t", "purge_on_uninstall": True},
        ],
    )
    with pytest.raises(ValidationError, match="reserved by the framework"):
        PluginMetadata.model_validate(spec)

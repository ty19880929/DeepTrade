# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

`deeptrade-quant` (PyPI) → `deeptrade` (CLI command + Python package). The wheel ships **only the framework**: `init / config / plugin / data / db`. All strategies live in separate plugin packages installed at runtime from the official registry at `DeepTradePluginOfficial`. Do not add business strategies or tushare-derived business tables to this repo. IM/chat integration was previously exposed as a `channel` plugin type but was retired in v0.3.0; the planned replacement is a framework-level ChatGateway module (not yet implemented).

User-facing docs live in `README.md` (commands, plugin install flows) and `CHANGELOG.md` (full history of breaking changes). The previous `docs/` and `review/` trees were deleted in v0.2.0; do not recreate them unless the user asks.

## Develop / test / lint

```bash
uv sync --all-extras                # install + dev extras
uv run pre-commit install           # hook ruff + mypy on commit
uv run pytest                       # full suite
uv run pytest tests/core/test_db.py::test_name   # single test
uv run pytest -m "not manual"       # skip the manual marker
uv run ruff check . && uv run ruff format .
uv run mypy deeptrade               # CI runs mypy on `deeptrade/` only, not `tests/`
```

CI (`.github/workflows/ci.yml`) runs ruff → mypy → pytest → `python -m build` on Ubuntu/Python 3.11. A pre-existing decision in `mypy.ini` disables `no-untyped-def` for tests; do not enable it project-wide. Tagging `v*` triggers `release.yml` (PyPI Trusted Publisher via OIDC + GitHub Release).

## Releasing

The package version lives in **two** places and they MUST be bumped together:

- `pyproject.toml::project.version` — what hatchling stamps onto the wheel filename and PyPI metadata. **This is the source of truth for the build.**
- `deeptrade/__init__.py::__version__` — runtime introspection only.

Bumping only `__init__.py` will silently produce a wheel with the previous version in its filename and tank the release at PyPI upload (HTTP 400 "File already exists" — PyPI filenames are immutable, you cannot overwrite). Always grep both files before tagging:

```bash
grep -n version pyproject.toml deeptrade/__init__.py
```

Release sequence:
1. Bump both files + update `CHANGELOG.md` in the same commit (or two adjacent commits).
2. Push to main and verify CI green.
3. `git tag -a vX.Y.Z -m "..."` then `git push origin vX.Y.Z` — the tag push is what triggers `release.yml`.

If the upload fails after a tag push (version mismatch, accidental rebuild), recover by moving the tag rather than burning a version number:

```bash
# fix the underlying issue, commit it
git tag -d vX.Y.Z                       # delete local
git push origin :refs/tags/vX.Y.Z       # delete remote
git tag -a vX.Y.Z -m "..."              # re-create on the fix commit
git push origin main && git push origin vX.Y.Z
```

This is safe **only** while the failed release hasn't shipped anything to PyPI (i.e. upload was rejected, not partially succeeded). If any artifact made it through, skip to the next patch version instead.

## Architecture

### Top-level CLI is a custom click.Group, not a static command tree

`deeptrade/cli.py` defines `_DeepTradeGroup(TyperGroup)`. On `deeptrade <token> ...`:

1. If `<token>` is a registered framework command (`init / config / plugin / data / db`), Click dispatches normally.
2. Otherwise the group looks `<token>` up in the `plugins` table and synthesizes a `click.Command` whose context has `ignore_unknown_options=True, allow_extra_args=True, help_option_names=[]` — the **plugin owns its own `--help`**, the framework never parses plugin subcommands.
3. Unknown + not installed → `ctx.fail` with the framework command list.

Reserved plugin IDs (rejected at install, defined in `RESERVED_PLUGIN_IDS` in `deeptrade/core/plugin_manager.py`): `init / config / plugin / data`. Adding a new framework subcommand reduces the open plugin namespace — prefer extending via a plugin type instead (this is recorded user feedback).

### Plugin contract — `deeptrade/plugins_api/`

This is the **public** surface; the rest of `deeptrade.*` is internal. Stable as `api_version = "1"`:

- `Plugin` Protocol — `metadata: PluginMetadata`, `validate_static(ctx) -> None` (must NOT touch the network — gates install acceptance), `dispatch(argv) -> int`.
- `PluginContext` — minimal services bundle (`db`, `config`, `plugin_id`). Plugins build `TushareClient` / `LLMManager` themselves from these primitives.
- `PluginMetadata` — Pydantic model parsed from `deeptrade_plugin.yaml`. Hard constraints: `type` is `Literal["strategy"]` (field is intentionally kept narrow but present, for future plugin-type extension), `llm_tools` is `Literal[False]` (M3), `migrations` cannot be empty (S1: DDL only via migrations), every migration carries an `sha256:<hex>` checksum.
- `StageProfile` — single-LLM-call tuning. Stage→profile mapping lives in plugins, not the framework (v0.7).

### Plugin install pipeline (`PluginManager` in `deeptrade/core/plugin_manager.py`)

`install(source_path)` is the canonical sequence and the rollback semantics matter:

1. Parse `deeptrade_plugin.yaml`, reject reserved `plugin_id`, version mismatch, `llm_tools=true`.
2. Verify each migration's sha256 checksum **before** any copy or DB write.
3. `shutil.copytree` → `~/.deeptrade/plugins/installed/<plugin_id>/<version>/`.
4. Inside one DB transaction: apply migrations, INSERT into `plugins / plugin_tables / plugin_schema_migrations`, verify all declared tables exist post-migration. On exception → remove copied dir.
5. Outside the transaction: load entrypoint via `_load_entrypoint` (insert install path into `sys.path`, `importlib.import_module`, evict cached modules first to avoid stale imports across reinstalls), call `validate_static`. On failure → `_rollback_install` drops owned tables + deletes registry rows + removes the copy.

`upgrade()` enforces SemVer via `packaging.Version`: equal → `UpgradeNoop`, lower → `PluginInstallError` (downgrade forbidden, no migration rollback model), higher → applies only migrations not in `plugin_schema_migrations` for that `plugin_id`. Keep this invariant when modifying upgrade logic.

`SourceResolver` (`deeptrade/core/plugin_source.py`) resolves three source forms in this fixed order: local directory exists → GitHub URL (`http(s)://github.com/...` or `git@github.com:...`) → registry short name. The registry index is fetched from `raw.githubusercontent.com/.../DeepTradePluginOfficial/main/registry/index.json` with ETag caching to `~/.deeptrade/plugins/registry-cache.json`; cache is reused on network error.

**The install/upgrade path must not hit `api.github.com`.** The 60/h anonymous rate limit makes the framework unusable for any user who didn't set `GITHUB_TOKEN`. Both metadata and tarball fetches go through CDN endpoints (`raw.githubusercontent.com` for the registry index, `codeload.github.com` for tarballs), neither of which counts against that quota. To support this:

- The registry entry carries a `latest_version` field (optional in the schema, but populated for every published plugin) — short-name installs read this directly instead of calling `GET /repos/.../releases`. Plugin release CI is responsible for keeping `registry/index.json` in sync after each tag push.
- `fetch_tarball(repo, ref, ...)` in `deeptrade/core/github_fetch.py` posts only to `codeload.github.com/<repo>/tar.gz/<ref>`. Do not add a code path that calls `api.github.com` from this module — even for "just one feature" — without re-evaluating the rate-limit story.
- URL-form installs without `--ref` default to the `main` branch. Repos with a different default branch must be installed with an explicit `--ref`.

### DuckDB — single-process, single-writer

`deeptrade/core/db.py::Database` wraps one DuckDB connection guarded by an `RLock` (re-entrant because `transaction()` and the user code inside it both acquire it). **The lock must span both `execute` AND `fetch`** on the same statement — releasing between them on the same connection causes Windows native heap corruption (0xC0000374). `transaction()` is reentrant via depth counting; only the outermost block issues `BEGIN/COMMIT/ROLLBACK`. There is no thread-safety contract — if you ever introduce background workers, route writes through a queue on the main thread.

Framework-owned tables (created by `deeptrade/core/migrations/core/20260509_001_init.sql`): `app_config`, `secret_store`, `schema_migrations`, `plugins`, `plugin_tables`, `plugin_schema_migrations`, `llm_calls`, `tushare_calls`, `tushare_sync_state`. The audit tables are keyed by `plugin_id` (sentinel `__framework__` for framework-internal calls). All business tables are owned by plugins via their own `migrations/*.sql`.

`apply_core_migrations` runs SQL migrations *and* idempotent data migrations in `config_migrations.py` (legacy `deepseek.*` → `llm.providers`, `deepseek.profile` → `app.profile`, default-provider backfill, v0.3.0 purge of non-`strategy` plugin rows). Data migrations are idempotent by inspection — they don't write to `schema_migrations`. The v0.3.0 purge must run before any `PluginManager.list_all()` call because the `type` field on `PluginMetadata` is now `Literal["strategy"]` and would otherwise raise on legacy `channel` rows still in the DB.

### Configuration & secrets

`ConfigService` (`deeptrade/core/config.py`) layers env var → `secret_store` (for secrets) → `app_config` (for non-secrets) → Pydantic default. Secrets are routed by key prefix (`is_secret_key`) and never written to `app_config`; non-secrets never go into `secret_store`. `SecretStore` uses OS `keyring` when available, falls back to plaintext-in-DuckDB. **Tests must monkeypatch `keyring` to None** — see the autouse `_isolate_keyring` fixture in `tests/conftest.py`. Without it, real CLI test runs silently overwrite the developer's actual `tushare.token` in the OS credential store, because `DEEPTRADE_HOME` only redirects the DuckDB file.

LLM is multi-provider: `llm.providers` is a JSON dict in `app_config`, each `llm.<name>.api_key` lives in `secret_store`. `LLMManager.get_client(name=..., plugin_id=..., run_id=...)` is the only path plugins should use; omitted name resolves to the `is_default=True` provider. Hard rule: **never pass `tools` / function calls** to LLM transports — JSON mode + Pydantic only.

Path overrides for tests: `DEEPTRADE_HOME` (root) and `DEEPTRADE_DB_PATH` (DuckDB file).

### IM / notifications

The framework no longer ships a built-in notification path. The previous chain (`AsyncDispatchNotifier` → `MultiplexNotifier` → `type=channel` plugins, plus `deeptrade.notify` / `notification_session` / `NotificationPayload`) was removed in v0.3.0 because IM flows (login, polling, send) don't fit the one-shot plugin dispatch lifecycle. The replacement is a framework-level ChatGateway module — design TBD; do not reintroduce per-plugin push hooks while that design is open.

## Conventions worth preserving

- **Plugins, not flags, for new variants.** New strategy / new data source / new skin → new plugin (or new plugin type), zero framework change.
- Plugin entrypoint dotted form: `module.path:Class` (regex-validated in `PluginMetadata`). `_load_entrypoint` evicts cached top-package modules before re-import to handle reinstalls cleanly.
- `metadata.tables` declares table **names + purge policy only** — no inline DDL. All DDL flows through `migrations/*.sql` with sha256 checksums (S1).
- The framework holds **no business tables**. `tushare_*` and `llm_calls` are audit-only and `plugin_id`-keyed.
- Pydantic models use `ConfigDict(extra="forbid")` everywhere — keep this when adding fields, prefer `Optional[T] = None` over invented "conditional_required" semantics (recorded user feedback against new framework concepts when primitives suffice).
- Ruff config (`ruff.toml`) intentionally ignores `B008` because Typer relies on call-in-default for options — don't "fix" those.

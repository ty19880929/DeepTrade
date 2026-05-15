# Repository Guidelines

## Project Structure & Module Organization

`deeptrade/` contains the Python package and CLI framework. Core services live in `deeptrade/core/`, public plugin-facing contracts live in `deeptrade/plugins_api/`, and command modules are split across `cli.py`, `cli_config.py`, `cli_data.py`, and `cli_plugin.py`. Database migrations are stored under `deeptrade/core/migrations/core/`. Tests mirror the package layout under `tests/core/`, `tests/cli/`, and `tests/plugins_api/`. Build and tool configuration lives in `pyproject.toml`, `pytest.ini`, `ruff.toml`, `mypy.ini`, and `.pre-commit-config.yaml`.

## Build, Test, and Development Commands

- `uv sync --all-extras` installs runtime, dev, and plugin-runtime dependencies.
- `uv run pre-commit install` enables local hooks for Ruff, formatting, mypy, and basic file checks.
- `uv run pytest` runs the full test suite.
- `uv run pytest tests/core/test_db.py::test_name` runs one focused test.
- `uv run pytest -m "not manual"` skips tests marked `manual`.
- `uv run ruff check . && uv run ruff format .` lints and formats the repository.
- `uv run mypy deeptrade` type-checks the package; CI checks `deeptrade/`, not `tests/`.
- `uv run python -m build` builds distribution artifacts.

## Coding Style & Naming Conventions

Target Python 3.11. Use 4-space indentation, double quotes, and Ruff formatting with a 100-character line length. Ruff checks `E`, `F`, `I`, `UP`, `B`, and `W`; `B008` is intentionally ignored because Typer options use call expressions in defaults. Keep public plugin APIs in `deeptrade/plugins_api/` stable and minimal. Do not add business strategies or Tushare-derived business tables to this framework repository; strategies belong in separate plugins.

## Testing Guidelines

Use pytest. Name tests `test_*.py` and place them near the relevant area under `tests/`. Prefer isolated unit tests for core services and CLI routing. Tests that touch secrets must avoid the real OS keyring; follow the existing fixtures in `tests/conftest.py`. Mark manually verified cases with `@pytest.mark.manual` so they can be skipped with `-m "not manual"`.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style subjects, for example `feat(plugins): ...`, `docs(claude): ...`, and `release(v0.4.2): ...`. Keep commits scoped and imperative. Pull requests should describe behavior changes, list tests run, link related issues, and include screenshots or terminal output when CLI behavior changes.

## Security & Configuration Tips

Do not commit credentials, local DuckDB files, generated reports, or plugin installs. Use `DEEPTRADE_HOME` or `DEEPTRADE_DB_PATH` to isolate local test data. Secrets belong in the configured secret store, not in `app_config` or source files.

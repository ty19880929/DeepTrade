# DeepTrade

## Project Overview

DeepTrade (packaged as `deeptrade-quant`) is a lightweight, locally-run CLI framework for A-share (Shanghai/Shenzhen main board) stock screening. It acts as an orchestrator combining market data (via Tushare), OpenAI-compatible LLMs (DeepSeek, Qwen, Kimi, etc.), and a local single-file DuckDB database. 

A key architectural feature of DeepTrade is its **plugin system**. The framework itself does not contain any business or trading strategies. Instead, it provides core services (database migrations, configuration, LLM/Tushare client management, and notification routing) and uses a pure pass-through CLI design. Unknown CLI commands are routed directly to installed plugins, which manage their own arguments, subcommands, and specific data migrations.

### Key Technologies
* **Language:** Python 3.11+
* **Database:** DuckDB (single-file local warehouse)
* **CLI Framework:** Typer / Click (with custom pass-through routing)
* **Data Source:** Tushare
* **LLM Integration:** OpenAI-compatible client, leveraging Pydantic for strong JSON schema validation.
* **Package Management:** `uv` and `hatchling`

## Building and Running

The project relies on `uv` for dependency management and environment isolation.

**Development Setup:**
```bash
# Clone the repository and install all dependencies (including dev)
uv sync --all-extras

# Install pre-commit hooks
uv run pre-commit install
```

**Running the CLI:**
The main entry point for the CLI is `deeptrade`.
```bash
# Show framework commands
uv run deeptrade --help

# Initialize the database and configurations (DuckDB + core migrations)
uv run deeptrade init
```

**Testing:**
Tests are located in the `tests/` directory and are executed using `pytest`.
```bash
uv run pytest
```

## Development Conventions

DeepTrade adheres to strict static analysis and formatting rules to ensure code quality.

* **Code Formatting & Linting:** Handled by `ruff`. 
  * Line length is set to 100 characters.
  * Uses double quotes for strings.
  * Disables specific rules like `E501` (handled by formatter) and `B008` (required for Typer argument defaults).
* **Type Checking:** Strict type checking is enforced using `mypy` (configured in `mypy.ini`), especially within the `deeptrade/` directory.
* **Pre-commit Hooks:** Code must pass `pre-commit` checks before being committed. This includes `ruff` (linting and formatting), `mypy`, and standard hooks (trailing whitespaces, EOF fixers).
* **Testing Practices:** 
  * `pytest` is the standard testing framework.
  * Custom markers include `manual` (for tests requiring human verification) and `anyio` (for async tests).
  * Warnings like `DeprecationWarning` are ignored during test runs.

## Directory Structure
* `deeptrade/`: The core Python package containing the CLI definitions, core services (DB, Config, LLM, Tushare), and the plugin API definitions (`plugins_api/`).
* `tests/`: Comprehensive test suite for the CLI, core modules, and plugin API.
* `docs/`: Project documentation.
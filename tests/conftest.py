"""Shared pytest fixtures.

V0.0: minimal — later iterations will populate this with database, transport,
and recorded LLM fixtures (see PLAN.md §6).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block tests from writing to the developer's real OS keyring.

    `DEEPTRADE_HOME` only redirects the DuckDB file. `SecretStore.set` for a
    `keyring`-backed secret writes the value to the OS credential store
    (`keyring.set_password("deeptrade", ...)`), which is process-global and
    NOT affected by tmp_path / monkeypatch.setenv. Without this fixture, any
    test that exercises the real CLI through `CliRunner` (e.g.
    `tests/cli/test_config_cmd.py::test_config_show_masks_secret_value` calling
    `config set tushare.token abcdef1234567890`) silently overwrites the
    developer's actual `tushare.token` / `llm.<provider>.api_key` in Windows
    Credential Manager / macOS Keychain / Secret Service.

    Forcing `_try_load_keyring` to return None makes `SecretStore` fall back
    to plaintext-in-DuckDB, which IS isolated by `DEEPTRADE_HOME`/tmp_path.
    """
    monkeypatch.setattr("deeptrade.core.secrets._try_load_keyring", lambda: None)

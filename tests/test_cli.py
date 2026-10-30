from __future__ import annotations

import logging
import sys

import pytest

from interfaces.cli.main import _configure_logging, _load_settings, _option, _parse_args, main


@pytest.fixture(scope="session", autouse=True)
def _pg_container():
    yield None


@pytest.fixture(autouse=True)
def _truncate_tables():
    """CLI tests do not need the suite-wide PostgreSQL fixture."""
    yield


def test_cli_defaults_to_serve_for_legacy_flags() -> None:
    args = _parse_args(["--host", "0.0.0.0", "--port", "9000"])

    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_cli_omitted_server_flags_are_loaded_from_settings() -> None:
    args = _parse_args([])

    assert args.command == "serve"
    assert args.host is None
    assert args.port is None
    assert args.reload is None


def test_cli_supports_no_reload_override() -> None:
    args = _parse_args(["serve", "--no-reload"])

    assert args.reload is False


def test_cli_supports_api_only_mode() -> None:
    args = _parse_args(["serve", "--api-only"])

    assert args.api_only is True


def test_cli_supports_worker_subcommand() -> None:
    args = _parse_args(["worker", "--worker-id", "server-1"])

    assert args.command == "worker"
    assert args.worker_id == "server-1"


def test_cli_supports_check_config_subcommand() -> None:
    args = _parse_args(["check-config", "--config-dir", "/etc/agent-task-platform"])

    assert args.command == "check-config"
    assert args.config_dir == "/etc/agent-task-platform"


def test_cli_supports_init_and_migrate(monkeypatch, tmp_path) -> None:
    assert _parse_args(["init", "--config-dir", "/tmp/initial-config"]).command == "init"
    assert _parse_args(["migrate", "--config-dir", "/tmp/config"]).command == "migrate"
    called = []
    monkeypatch.setattr("interfaces.cli.main._init_config", lambda args: called.append(args.command))
    main(["init", "--config-dir", str(tmp_path)])
    assert called == ["init"]


def test_settings_loader_and_cli_override_precedence(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        'SERVER__HOST=0.0.0.0\nSERVER__PORT=50010\nWORKER__WORKER_ID=worker-from-env\nAUTH__API_KEYS=["dev-key"]\n'
    )

    settings = _load_settings(str(config_dir))

    assert settings["server"]["host"] == "0.0.0.0"
    assert settings["auth"]["api_keys"] == ["dev-key"]
    assert _option(None, settings["server"]["port"], 9000) == 50010
    assert _option("127.0.0.1", settings["server"]["host"], "localhost") == "127.0.0.1"


def test_configure_logging_writes_application_logs_to_stdout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_basic_config(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("logging.basicConfig", fake_basic_config)

    _configure_logging({"logging": {"level": "debug"}})

    handlers = captured["handlers"]
    assert isinstance(handlers, list)
    stdout_handler = next(handler for handler in handlers if isinstance(handler, logging.StreamHandler))
    assert stdout_handler.stream is sys.stdout
    formatter = stdout_handler.formatter
    assert formatter is not None and formatter._fmt is not None
    assert "req_id=%(req_id)s" in formatter._fmt

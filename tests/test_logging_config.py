from __future__ import annotations

import logging
from datetime import datetime

from interfaces.logging_config import DailySizeRotatingFileHandler, configure_logging, reconfigure_logging


def _handler(tmp_path, now, *, max_bytes: int = 100, retention_days: int = 100):
    handler = DailySizeRotatingFileHandler(
        tmp_path,
        "api",
        max_bytes=max_bytes,
        retention_days=retention_days,
        now=lambda: now[0],
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def test_file_handler_rotates_at_size_limit(tmp_path) -> None:
    now = [datetime(2026, 8, 25, 10, 0)]
    handler = _handler(tmp_path, now, max_bytes=10)

    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "123456", (), None))
    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "abcdef", (), None))
    handler.close()

    assert (tmp_path / "api.2026-08-25.log").read_text() == "123456\n"
    assert (tmp_path / "api.2026-08-25.1.log").read_text() == "abcdef\n"


def test_file_handler_rotates_when_date_changes(tmp_path) -> None:
    now = [datetime(2026, 8, 25, 23, 59)]
    handler = _handler(tmp_path, now)
    record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)

    handler.emit(record)
    now[0] = datetime(2026, 8, 26, 0, 1)
    handler.emit(record)
    handler.close()

    assert (tmp_path / "api.2026-08-25.log").read_text() == "message\n"
    assert (tmp_path / "api.2026-08-26.log").read_text() == "message\n"


def test_file_handler_keeps_only_configured_calendar_days(tmp_path) -> None:
    for name in (
        "api.2026-08-07.log",
        "api.2026-08-08.log",
        "api.2026-08-08.1.log",
        "worker.2026-08-07.log",
    ):
        (tmp_path / name).write_text("old\n")

    now = [datetime(2026, 8, 10, 10, 0)]
    handler = _handler(tmp_path, now, retention_days=3)
    handler.close()

    assert not (tmp_path / "api.2026-08-07.log").exists()
    assert (tmp_path / "api.2026-08-08.log").exists()
    assert (tmp_path / "api.2026-08-08.1.log").exists()
    assert (tmp_path / "worker.2026-08-07.log").exists()


def test_file_handler_continues_with_next_part_after_restart(tmp_path) -> None:
    (tmp_path / "api.2026-08-25.log").write_text("1234567890")
    now = [datetime(2026, 8, 25, 10, 0)]

    handler = _handler(tmp_path, now, max_bytes=10)
    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "new", (), None))
    handler.close()

    assert (tmp_path / "api.2026-08-25.1.log").read_text() == "new\n"


def test_configure_logging_resolves_relative_directory_next_to_config(tmp_path, monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

    configure_logging(
        {"logging": {"directory": "logs", "max_file_size_mb": 100, "retention_days": 100}},
        "worker",
        config_dir=tmp_path / "config",
    )

    handlers = captured["handlers"]
    assert any(isinstance(handler, logging.StreamHandler) for handler in handlers)
    file_handler = next(handler for handler in handlers if isinstance(handler, DailySizeRotatingFileHandler))
    try:
        assert file_handler.directory == tmp_path / "logs"
        assert file_handler.current_path.name.startswith("worker.")
    finally:
        file_handler.close()


def test_reconfigure_logging_updates_existing_handler_without_reopening_file(tmp_path, monkeypatch) -> None:
    now = [datetime(2026, 8, 25, 10, 0)]
    handler = _handler(tmp_path, now)
    root = logging.RootLogger(logging.INFO)
    root.addHandler(handler)
    path = handler.current_path
    stream = handler._stream
    monkeypatch.setattr("interfaces.logging_config.logging.getLogger", lambda: root)
    try:
        reconfigure_logging({"logging": {"level": "debug", "max_file_size_mb": 2, "retention_days": 3}})
        assert root.level == logging.DEBUG
        assert handler.max_bytes == 2 * 1024 * 1024
        assert handler.retention_days == 3
        assert handler.current_path == path
        assert handler._stream is stream
    finally:
        handler.close()

"""Application logging configuration and file rotation."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from framework.observability import install_request_trace_log_record_factory


class DailySizeRotatingFileHandler(logging.Handler):
    """Write one log series per day and start a new part at the size limit."""

    terminator = "\n"

    def __init__(
        self,
        directory: str | Path,
        service_name: str,
        *,
        max_bytes: int,
        retention_days: int,
        encoding: str = "utf-8",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        if retention_days <= 0:
            raise ValueError("retention_days must be greater than zero")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", service_name):
            raise ValueError("service_name may only contain letters, numbers, underscores, and hyphens")

        super().__init__()
        self.directory = Path(directory).expanduser()
        self.service_name = service_name
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.encoding = encoding
        self._now = now or datetime.now
        self._stream: TextIO | None = None
        self._current_date: date | None = None
        self._current_size = 0

        self.directory.mkdir(parents=True, exist_ok=True)
        today = self._now().date()
        self._prune_expired(today)
        self._open_for_date(today)

    @property
    def current_path(self) -> Path:
        if self._stream is None:
            raise RuntimeError("log handler is closed")
        return Path(self._stream.name)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) + self.terminator
            encoded_size = len(message.encode(self.encoding, errors="backslashreplace"))
            today = self._now().date()

            if today != self._current_date:
                self._close_stream()
                self._prune_expired(today)
                self._open_for_date(today)
            elif self._current_size and self._current_size + encoded_size > self.max_bytes:
                self._close_stream()
                self._open_for_date(today, next_part=True)

            if self._stream is None:
                raise RuntimeError("log stream is not open")
            self._stream.write(message)
            self._stream.flush()
            self._current_size += encoded_size
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
            super().close()

    def _open_for_date(self, log_date: date, *, next_part: bool = False) -> None:
        parts = self._parts_for_date(log_date)
        part = max(parts, default=0)
        path = self._path_for(log_date, part)

        if next_part or (path.exists() and path.stat().st_size >= self.max_bytes):
            part += 1
            path = self._path_for(log_date, part)

        self._stream = path.open("a", encoding=self.encoding, errors="backslashreplace")
        self._current_date = log_date
        self._current_size = path.stat().st_size

    def _parts_for_date(self, log_date: date) -> list[int]:
        prefix = f"{self.service_name}.{log_date.isoformat()}"
        pattern = re.compile(rf"^{re.escape(prefix)}(?:\.(\d+))?\.log$")
        parts: list[int] = []
        for path in self.directory.glob(f"{prefix}*.log"):
            match = pattern.fullmatch(path.name)
            if match:
                parts.append(int(match.group(1) or 0))
        return parts

    def _path_for(self, log_date: date, part: int) -> Path:
        suffix = "" if part == 0 else f".{part}"
        return self.directory / f"{self.service_name}.{log_date.isoformat()}{suffix}.log"

    def _prune_expired(self, today: date) -> None:
        oldest_retained_date = today - timedelta(days=self.retention_days - 1)
        pattern = re.compile(rf"^{re.escape(self.service_name)}\.(\d{{4}}-\d{{2}}-\d{{2}})(?:\.\d+)?\.log$")
        for path in self.directory.glob(f"{self.service_name}.*.log"):
            match = pattern.fullmatch(path.name)
            if not match:
                continue
            try:
                log_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if log_date < oldest_retained_date:
                path.unlink(missing_ok=True)

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None


def configure_logging(
    settings: dict[str, Any],
    service_name: str,
    *,
    config_dir: str | Path | None = None,
) -> None:
    """Configure stdout plus daily, size-limited file logging for one process."""

    install_request_trace_log_record_factory()
    logging_settings = settings.get("logging", {})
    level = getattr(logging, str(logging_settings.get("level", "info")).upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s req_id=%(req_id)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stdout_handler]

    if logging_settings.get("file_enabled", True):
        directory = Path(logging_settings.get("directory", "logs")).expanduser()
        if not directory.is_absolute() and config_dir is not None:
            directory = Path(config_dir).expanduser().resolve().parent / directory
        file_handler = DailySizeRotatingFileHandler(
            directory,
            service_name,
            max_bytes=int(logging_settings.get("max_file_size_mb", 100)) * 1024 * 1024,
            retention_days=int(logging_settings.get("retention_days", 100)),
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def reconfigure_logging(settings: dict[str, Any]) -> None:
    """Apply logging level and rotation controls without replacing handlers."""
    logging_settings = settings.get("logging", {})
    level = getattr(logging, str(logging_settings.get("level", "info")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if isinstance(handler, DailySizeRotatingFileHandler):
            handler.acquire()
            try:
                handler.max_bytes = int(logging_settings.get("max_file_size_mb", 100)) * 1024 * 1024
                handler.retention_days = int(logging_settings.get("retention_days", 100))
            finally:
                handler.release()

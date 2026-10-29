"""Bounded JSONL transport for one Python-owned Pi child per Run attempt."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from framework.runtime.errors import RunCancelledError

NODE_DIR = Path(__file__).resolve().parent / "node"
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_PENDING_REQUESTS = 128
WORKER_ERROR_CODES = frozenset(
    {
        "worker_error",
        "max_turns_exceeded",
        "max_tool_calls_exceeded",
        "output_truncated",
        "empty_output",
        "model_error",
        "aborted",
        "response_too_large",
        "invalid_tool_calls",
        "context_too_large",
    }
)
_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class PiProcessError(RuntimeError):
    """Pi is unavailable, exited, or violated the local protocol."""


def worker_environment() -> dict[str, str]:
    """Pass transport settings, not application credentials or Node preload hooks."""
    return {key: value for key, value in os.environ.items() if key in _ENV_KEYS}


def runner_command() -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise PiProcessError("Pi requires Node.js >=22.19 on the service PATH; see docs/pi-guide.md")
    if not (NODE_DIR / "node_modules/@earendil-works/pi-agent-core/package.json").is_file():
        raise PiProcessError(f"Pi dependencies are missing; run npm ci --ignore-scripts --prefix {NODE_DIR}")
    return [node, str(NODE_DIR / "runner.mjs")]


class PiProcess:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._handlers: set[asyncio.Task] = set()
        self._stderr_task: asyncio.Task | None = None
        self._used = False

    async def run(
        self,
        start: dict[str, Any],
        handle: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        cancellation_signal: Any = None,
    ) -> dict[str, Any]:
        if self._used:
            raise PiProcessError("PiProcess instances can only run once")
        self._used = True
        if cancellation_signal is not None and cancellation_signal.is_set():
            raise RunCancelledError("Pi Run was canceled before starting")
        command = runner_command()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_RECORD_BYTES,
                env=worker_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise PiProcessError("Unable to start Pi worker; check the Node installation") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        failure = asyncio.get_running_loop().create_future()
        cancel_task = asyncio.create_task(self._wait_cancel(cancellation_signal))
        read_task: asyncio.Task | None = None
        write_task: asyncio.Task | None = None
        sequence = 0
        try:
            write_task = asyncio.create_task(self._write({**start, "type": "start", "protocol": 1}))
            done, _ = await asyncio.wait({write_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                raise RunCancelledError("Pi Run was canceled")
            await write_task
            while True:
                read_task = asyncio.create_task(self.process.stdout.readline())
                done, _ = await asyncio.wait({read_task, cancel_task, failure}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done:
                    raise RunCancelledError("Pi Run was canceled")
                if failure in done:
                    raise failure.result()
                try:
                    line = read_task.result()
                except ValueError:
                    raise PiProcessError("Pi protocol record exceeds the 8 MiB limit") from None
                if not line:
                    raise PiProcessError("Pi worker exited before producing a result; check Node and npm installation")
                if len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n"):
                    raise PiProcessError("Pi worker returned an oversized or incomplete record")
                try:
                    record = json.loads(line, parse_constant=_invalid_number, parse_float=_finite_float)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise PiProcessError("Pi worker returned invalid JSON") from None
                if not isinstance(record, dict):
                    raise PiProcessError("Pi worker record must be an object")
                if record.get("type") == "result":
                    if any(not task.done() for task in self._handlers):
                        raise PiProcessError("Pi completed with pending capability requests")
                    if not isinstance(record.get("text"), str) or not record["text"].strip():
                        raise PiProcessError("Pi worker returned an invalid result")
                    if type(record.get("turns")) is not int or record["turns"] < 0:
                        raise PiProcessError("Pi worker returned an invalid turn count")
                    return record
                if record.get("type") == "error":
                    # Do not copy arbitrary provider bodies (possibly containing credentials) into Run errors.
                    code = record.get("code")
                    code = code if isinstance(code, str) and code in WORKER_ERROR_CODES else "worker_error"
                    raise PiProcessError(f"Pi worker failed ({code})")
                if record.get("type") != "request" or record.get("id") != str(sequence + 1):
                    raise PiProcessError("Unexpected Pi worker record")
                sequence += 1
                if len(self._handlers) >= MAX_PENDING_REQUESTS:
                    raise PiProcessError("Pi worker exceeded the pending capability request limit")
                task = asyncio.create_task(self._respond(record, handle, failure))
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
        finally:
            # A second cancellation (shutdown racing with a Run timeout) must not orphan Node.
            cleanup = asyncio.create_task(self._cleanup(read_task, write_task, cancel_task, failure))
            interrupted = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError

    async def _cleanup(self, read_task, write_task, cancel_task, failure) -> None:
        tasks = [task for task in (read_task, write_task, cancel_task, *self._handlers) if task is not None]
        failure.cancel()
        for task in tasks:
            task.cancel()
        try:
            if read_task is not None:
                # Release the sole stdout reader before cleanup starts discarding unread output.
                await asyncio.gather(read_task, return_exceptions=True)
            # Reap Node even when a Python tool takes time to acknowledge cancellation.
            await self._close()
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _respond(self, record: dict, handle: Callable, failure: asyncio.Future) -> None:
        try:
            result = await handle(record)
            await self._write({"type": "response", "id": record["id"], "data": result})
        except (Exception, asyncio.CancelledError) as exc:
            if not failure.done():
                failure.set_result(exc)

    async def _write(self, record: dict) -> None:
        async with self._write_lock:
            # Only one encoded record is resident while the pipe is backpressured.
            encoded = self._encode(record)
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()

    @staticmethod
    def _encode(record: dict) -> bytearray:
        encoded = bytearray()
        try:
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for chunk in encoder.iterencode(record):
                remaining = MAX_RECORD_BYTES - len(encoded) - 1
                if len(chunk) > remaining:
                    raise PiProcessError("Pi protocol record exceeds the 8 MiB limit")
                fragment = chunk.encode()
                if len(fragment) > remaining:
                    raise PiProcessError("Pi protocol record exceeds the 8 MiB limit")
                encoded.extend(fragment)
        except PiProcessError:
            raise
        except Exception:
            raise PiProcessError("Pi protocol record must contain finite JSON data") from None
        encoded.extend(b"\n")
        return encoded

    async def _drain_stderr(self) -> None:
        # Drain without accumulating or logging raw third-party diagnostics.
        while await self.process.stderr.read(8192):
            pass

    async def _drain_stdout(self) -> None:
        while await self.process.stdout.read(8192):
            pass

    async def _wait_cancel(self, cancellation_signal: Any) -> None:
        if cancellation_signal is None:
            await asyncio.Future()
            return
        # The platform exposes a synchronous cancel() and only promises an is_set() flag.
        # Awaiting an asyncio.Event directly installs a loop-bound waiter; set() from a
        # different thread then fails to wake an idle loop (or raises in asyncio debug mode).
        while not cancellation_signal.is_set():
            await asyncio.sleep(0.05)

    async def _close(self) -> None:
        process = self.process
        # Process.wait() can otherwise deadlock on a paused, full StreamReader even after kill().
        stdout_task = asyncio.create_task(self._drain_stdout())
        try:
            if process.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._write({"type": "abort"}), timeout=0.2)
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
        finally:
            try:
                # The leader may already have exited while descendants still own its pipes.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                await asyncio.wait({stdout_task, self._stderr_task}, timeout=1)
            finally:
                stdout_task.cancel()
                self._stderr_task.cancel()
                await asyncio.gather(stdout_task, self._stderr_task, return_exceptions=True)


def _invalid_number(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _invalid_number(value)
    return number

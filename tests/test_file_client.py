from __future__ import annotations

import pytest
from conftest import make_store as SqliteRunStore

from domain import RunFile
from framework.runtime.files import FileClient, FileFetchError


def test_file_client_lists_primary_and_logs_access(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    files = [
        RunFile(file_id="front", url="https://files.test/front.png", file_name="front.png", mime_type="image/png"),
        RunFile(file_id="back", url="https://files.test/back.png", file_name="back.png", mime_type="image/png"),
    ]
    client = FileClient(files=files, store=store, run_id="TASK-file", trace_id="TRACE-file")

    assert client.list_dicts()[0]["file_id"] == "front"
    assert client.primary().file_id == "front"
    assert client.get("back").url == "https://files.test/back.png"

    logs = store.logs.for_run("TASK-file")
    assert [event.event_type for event in logs] == [
        "file_references_listed",
        "file_reference_primary_accessed",
        "file_reference_accessed",
    ]


def test_file_client_require_raises_for_unknown_file_and_logs_miss(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    client = FileClient(files=[RunFile(file_id="front")], store=store, run_id="TASK-file", trace_id="TRACE-file")

    with pytest.raises(KeyError, match="missing"):
        client.require("missing")

    logs = store.logs.for_run("TASK-file")
    assert logs[-1].event_type == "file_reference_accessed"
    assert logs[-1].data == {"file_id": "missing", "found": False}


def test_file_client_fetch_rejects_oversize_content_length(tmp_path) -> None:
    """Content-Length exceeding max_file_size should be rejected before download."""
    store = SqliteRunStore(tmp_path / "runs.db")
    client = FileClient(
        files=[RunFile(file_id="big", url="https://93.184.216.34/big.bin")],
        store=store,
        run_id="TASK-file",
        trace_id="TRACE-file",
        max_file_size=100,
    )

    class MockResponse:
        status_code = 200
        headers = {"content-length": "999", "content-type": "application/octet-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, **kw):
            yield b"x" * 999

    class MockClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url):
            return MockResponse()

    import httpx

    original = httpx.AsyncClient
    httpx.AsyncClient = MockClient
    try:
        with pytest.raises(FileFetchError, match="exceeds max"):
            import asyncio

            asyncio.run(client.fetch(file_id="big"))
    finally:
        httpx.AsyncClient = original


def test_file_client_fetch_rejects_disallowed_mime_type(tmp_path) -> None:
    """Content-Type not in allowed set should be rejected."""
    store = SqliteRunStore(tmp_path / "runs.db")
    client = FileClient(
        files=[RunFile(file_id="evil", url="https://93.184.216.34/evil.exe")],
        store=store,
        run_id="TASK-file",
        trace_id="TRACE-file",
        allowed_mime_types=frozenset({"image/png"}),
    )

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/x-msdownload", "content-length": "10"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, **kw):
            yield b"x" * 10

    class MockClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url):
            return MockResponse()

    import httpx

    original = httpx.AsyncClient
    httpx.AsyncClient = MockClient
    try:
        with pytest.raises(FileFetchError, match="not allowed"):
            import asyncio

            asyncio.run(client.fetch(file_id="evil"))
    finally:
        httpx.AsyncClient = original


def test_file_client_fetch_aborts_midstream_on_oversize(tmp_path) -> None:
    """Streaming download should abort when accumulated bytes exceed limit."""
    store = SqliteRunStore(tmp_path / "runs.db")
    client = FileClient(
        files=[RunFile(file_id="stream", url="https://93.184.216.34/stream.bin")],
        store=store,
        run_id="TASK-file",
        trace_id="TRACE-file",
        max_file_size=10,
    )

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, **kw):
            for _ in range(5):
                yield b"x" * 8  # 5*8=40 bytes, exceeds 10

    class MockClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url):
            return MockResponse()

    import httpx

    original = httpx.AsyncClient
    httpx.AsyncClient = MockClient
    try:
        with pytest.raises(FileFetchError, match="exceeded max size"):
            import asyncio

            asyncio.run(client.fetch(file_id="stream"))
    finally:
        httpx.AsyncClient = original


def test_file_client_fetch_rejects_domain_resolving_to_private_ip(tmp_path, monkeypatch) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    client = FileClient(
        files=[RunFile(file_id="private", url="https://files.example.test/private.png")],
        store=store,
        run_id="TASK-file",
        trace_id="TRACE-file",
    )

    import asyncio
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
    )

    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(client.fetch(file_id="private"))

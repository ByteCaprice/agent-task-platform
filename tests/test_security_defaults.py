from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from domain import ToolSpec
from framework.registry import RegistryError, ToolRegistry
from framework.tool.tools import ToolGateway
from interfaces.http.middleware import install_request_logging_middleware


@pytest.fixture(scope="session", autouse=True)
def _pg_container():
    yield


@pytest.fixture(autouse=True)
def _truncate_tables():
    yield


def test_file_client_rejects_redirects(monkeypatch) -> None:
    from framework.runtime.files import FileClient, FileFetchError

    class RedirectResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1/private"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url):
            return RedirectResponse()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    fetcher = FileClient(
        files=[],
        store=cast(Any, object()),
        run_id="run",
        trace_id="trace",
    )
    with pytest.raises(FileFetchError, match="redirects are not allowed"):
        asyncio.run(fetcher.fetch(url="https://93.184.216.34/input"))


def test_http_tool_blocks_private_destination_unless_explicitly_trusted(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.8", 8080))],
    )
    endpoint = {"protocol": "http", "url": "http://internal.example:8080/api"}

    class Client:
        async def request(self, *args, **kwargs):
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "json": lambda self: {"ok": True},
                    "raise_for_status": lambda self: None,
                },
            )()

    blocked = ToolGateway(
        registry=ToolRegistry([ToolSpec(name="http-test", endpoint=endpoint)]),
        store=cast(Any, object()),
        http_client=Client(),
    )
    with pytest.raises(RegistryError, match="outbound policy"):
        asyncio.run(blocked._invoke(endpoint, {}))

    trusted = ToolGateway(
        registry=ToolRegistry([ToolSpec(name="http-test", endpoint=endpoint)]),
        store=cast(Any, object()),
        http_client=Client(),
        trusted_private_http_endpoints=["http://internal.example:8080/api"],
    )
    assert asyncio.run(trusted._invoke(endpoint, {})) == {"ok": True}

    untrusted_path = {**endpoint, "url": "http://internal.example:8080/other"}
    with pytest.raises(RegistryError, match="outbound policy"):
        asyncio.run(trusted._invoke(untrusted_path, {}))


def test_http_logging_defaults_to_metadata_and_preserves_streaming_response(caplog) -> None:
    app = FastAPI()
    install_request_logging_middleware(app)

    @app.post("/payload")
    async def payload():
        async def stream():
            yield b"stream-secret"

        return StreamingResponse(stream(), media_type="text/plain", headers={"x-stream-mode": "passthrough"})

    caplog.set_level(logging.INFO, logger="agent_task_platform.http")
    with TestClient(app) as client:
        response = client.post("/payload?token=query-secret", json={"token": "body-secret"})
    assert response.text == "stream-secret"
    assert response.headers["x-stream-mode"] == "passthrough"
    assert "body-secret" not in caplog.text
    assert "query-secret" not in caplog.text
    assert "stream-secret" not in caplog.text
    assert '"query_keys":["token"]' in caplog.text


def test_http_payload_logging_is_explicit_opt_in(caplog) -> None:
    app = FastAPI()
    install_request_logging_middleware(app, settings={"logging": {"log_payloads": True}})

    @app.post("/payload")
    async def payload():
        return {"ok": True}

    caplog.set_level(logging.INFO, logger="agent_task_platform.http")
    with TestClient(app) as client:
        client.post("/payload", json={"public": "visible"})
    assert "visible" in caplog.text

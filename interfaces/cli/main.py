"""CLI entrypoint: argument parsing and launchers for the `serve`, `worker`
and `check-config` commands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

import uvicorn

from framework.observability import install_request_trace_log_record_factory
from interfaces.http.server import create_app
from interfaces.settings import load_settings


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "worker":
        _run_worker(args)
        return
    if args.command == "check-config":
        _check_config(args)
        return
    _run_server(args)


def _run_server(args: argparse.Namespace) -> None:
    settings = _load_settings(args.config_dir)
    _configure_logging(settings)
    server_settings = settings.get("server", {})
    host = _option(args.host, server_settings.get("host"), "127.0.0.1")
    port = int(_option(args.port, server_settings.get("port"), 8765))
    reload = bool(_option(args.reload, server_settings.get("reload"), False))
    if reload:
        os.environ["AGENT_TASK_PLATFORM_CONFIG_DIR"] = args.config_dir
        uvicorn.run(
            "interfaces.cli.main:create_app_for_uvicorn",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level=_log_level(settings),
        )
        return
    app = create_app(args.config_dir)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=_log_level(settings),
    )


def create_app_for_uvicorn():
    return create_app(os.environ.get("AGENT_TASK_PLATFORM_CONFIG_DIR", "config"))


def worker_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]) or "agent-task-platform-worker")
    _add_worker_args(parser)
    args = parser.parse_args(argv)
    _run_worker(args)


def _run_worker(args: argparse.Namespace) -> None:
    settings = _load_settings(args.config_dir)
    _configure_logging(settings)
    worker_settings = settings.get("worker", {})
    app = create_app(args.config_dir, auto_start=False)
    worker = app.state.worker
    worker.poll_interval_seconds = float(
        _option(
            args.poll_interval_seconds,
            worker_settings.get("poll_interval_seconds"),
            worker.poll_interval_seconds,
        )
    )
    worker.batch_size = int(_option(args.batch_size, worker_settings.get("batch_size"), worker.batch_size))
    worker.lease_seconds = float(
        _option(args.lease_seconds, worker_settings.get("lease_seconds"), worker.lease_seconds)
    )
    worker.worker_id = str(_option(args.worker_id, worker_settings.get("worker_id"), worker.worker_id))
    asyncio.run(worker.run_forever())


def _check_config(args: argparse.Namespace) -> None:
    app = create_app(args.config_dir, auto_start=False)
    store = app.state.store
    agents = app.state.agent_registry.list()
    tools = app.state.tool_registry.list()
    print("config: ok")
    print(f"config_dir: {args.config_dir}")
    print(f"store: {type(store).__name__}")
    print(f"agents: {len(agents)}")
    for agent in agents:
        print(f"  - {agent.name}@{agent.version} tags={agent.route_tags} tools={agent.tools}")
    print(f"tools: {len(tools)}")
    for tool in tools:
        print(f"  - {tool.name} protocol={tool.endpoint.get('protocol', 'builtin')}")


def _load_settings(config_dir: str) -> dict[str, Any]:
    return load_settings(config_dir)


def _configure_logging(settings: dict[str, Any]) -> None:
    install_request_trace_log_record_factory()
    logging.basicConfig(
        level=getattr(logging, _log_level(settings).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s req_id=%(req_id)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _log_level(settings: dict[str, Any]) -> str:
    return str(settings.get("logging", {}).get("level", "info")).lower()


def _option(cli_value: Any, settings_value: Any, default: Any) -> Any:
    if cli_value is not None:
        return cli_value
    if settings_value is not None:
        return settings_value
    return default


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]) or "agent-task-platform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the HTTP API server")
    _add_server_args(serve)

    worker = subparsers.add_parser("worker", help="Start a standalone worker")
    _add_worker_args(worker)

    check = subparsers.add_parser("check-config", help="Validate config and initialize the configured store")
    check.add_argument("--config-dir", default="config")

    return parser.parse_args(argv)


def _add_server_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None, type=int)
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=None)


def _add_worker_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--poll-interval-seconds", type=float, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lease-seconds", type=float, default=None)
    parser.add_argument("--worker-id", default=None)


if __name__ == "__main__":
    main()

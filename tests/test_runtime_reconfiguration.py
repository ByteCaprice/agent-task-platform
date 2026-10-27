from __future__ import annotations

import asyncio
from types import SimpleNamespace

from framework.model_gateway import ModelGateway
from infra.rate_limiter import RateLimiter
from interfaces.http.auth import ApiKeyRegistry
from interfaces.settings import Settings
from orchestration.callback_service import CallbackService
from orchestration.config_watcher import ConfigWatcher
from orchestration.scheduler import RunScheduler, SchedulerLimits
from orchestration.worker import RunWorker


def test_model_gateway_reconfigure_replaces_defaults() -> None:
    gateway = ModelGateway(
        store=object(),
        defaults={"model": "old", "timeout_seconds": 10, "fallback_models": ["fallback-old"]},
    )

    gateway.reconfigure(
        {
            "model": "new",
            "timeout_seconds": 20,
            "max_retries": 4,
            "fallback_models": ["fallback-new"],
        }
    )

    assert gateway.defaults["model"] == "new"
    assert gateway.timeout_seconds == 20
    assert gateway.retry_policy.max_retries == 4
    assert gateway.fallback_models == ["fallback-new"]


def test_callback_service_reconfigure_rebuilds_outbound_policy() -> None:
    service = CallbackService(store=object(), http_client=object(), signing_secret="secret")

    service.reconfigure(
        {
            "timeout_seconds": 15,
            "max_attempts": 5,
            "backoff_seconds": 2,
            "backoff_type": "fixed",
            "url_allowlist": ["https://callbacks.example.com"],
        },
        signing_secret="secret",
    )

    assert service.timeout_seconds == 15
    assert service.max_attempts == 5
    assert service.backoff_seconds == 2
    assert service.backoff_type == "fixed"
    assert service.url_allowlist == ["https://callbacks.example.com"]
    assert service.signing_secret == "secret"


def test_worker_reconfigure_updates_next_poll_controls() -> None:
    worker = RunWorker(manager=object(), store=object())

    worker.reconfigure({"poll_interval_seconds": 0.5, "batch_size": 8, "lease_seconds": 90})

    assert worker.poll_interval_seconds == 0.5
    assert worker.batch_size == 8
    assert worker.lease_seconds == 90


def test_rate_limiter_reconfigure_discards_old_buckets() -> None:
    limiter = RateLimiter({"enabled": True, "default": {"capacity": 1, "fill_rate": 0}})
    assert limiter.is_allowed("caller") is True
    assert limiter.is_allowed("caller") is False

    limiter.reconfigure({"enabled": True, "default": {"capacity": 2, "fill_rate": 0}})

    assert limiter.is_allowed("caller") is True
    assert limiter.is_allowed("caller") is True


def test_api_key_registry_reconfigure_rotates_keys_atomically() -> None:
    registry = ApiKeyRegistry(["old-key"])

    registry.reconfigure([{"key": "new-key", "name": "rotated", "scopes": ["runs"]}])

    assert registry.authenticate("old-key") is None
    entry = registry.authorize("new-key", "runs")
    assert entry is not None
    assert entry.name == "rotated"


def test_config_watcher_redacts_invalid_credential_input(tmp_path, monkeypatch) -> None:
    events = []
    store = SimpleNamespace(logs=SimpleNamespace(add=events.append))
    watcher = ConfigWatcher(
        config_dir=tmp_path,
        agent_registry=object(),
        tool_registry=object(),
        store=store,
    )

    def invalid_reload():
        Settings.model_validate({"auth": {"api_keys": "private-inbound-credential"}})

    async def one_poll(_interval):
        watcher.stop()

    monkeypatch.setattr(watcher, "_reload_agents", invalid_reload)
    monkeypatch.setattr("orchestration.config_watcher.asyncio.sleep", one_poll)
    asyncio.run(watcher.run_forever())

    assert "auth.api_keys" in watcher.last_error
    assert "private-inbound-credential" not in watcher.last_error
    assert "private-inbound-credential" not in str(events[0].data)


def test_scheduler_reconfigure_replaces_idle_concurrency_controls() -> None:
    scheduler = RunScheduler(store=object(), limits=SchedulerLimits(global_max_concurrency=2))

    scheduler.reconfigure(
        {
            "global_max_concurrency": 7,
            "route_tags": {"route": 3},
            "callers": {"caller": 4},
            "max_nested_depth": 6,
            "max_active_children_per_root": 8,
        }
    )

    assert scheduler.global_semaphore._value == 7
    assert scheduler.limits.route_tag_max_concurrency == {"route": 3}
    assert scheduler.limits.caller_max_concurrency == {"caller": 4}
    assert scheduler.limits.max_nested_depth == 6
    assert scheduler.limits.max_active_children_per_root == 8


def test_scheduler_reconfigure_rejects_changes_while_runs_are_active() -> None:
    scheduler = RunScheduler(store=object())
    scheduler._active_run_holders["run"] = {}

    try:
        scheduler.reconfigure({"global_max_concurrency": 7})
        raise AssertionError("active scheduler reconfiguration must fail")
    except RuntimeError as exc:
        assert "runs are active" in str(exc)

    assert scheduler.limits.global_max_concurrency == 20

from __future__ import annotations

from pathlib import Path

from conftest import make_store as SqliteRunStore

from domain import AgentSpec, ToolSpec
from framework.registry import AgentRegistry, ToolRegistry
from orchestration.config_watcher import ConfigWatcher


def _watcher(store, agents=None, tools=None) -> ConfigWatcher:
    return ConfigWatcher(
        config_dir=Path("."),
        agent_registry=AgentRegistry(agents or []),
        tool_registry=ToolRegistry(tools or []),
        store=store,
    )


def test_reload_tools_registers_new_db_row(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    store.tools.save(ToolSpec(name="t1"))
    watcher = _watcher(store)

    assert watcher._reload_tools() == 1
    assert watcher.tool_registry.get_optional("t1") is not None
    # idempotent: unchanged DB → no churn
    assert watcher._reload_tools() == 0


def test_reload_tools_deregisters_row_removed_from_db(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    watcher = _watcher(store, tools=[ToolSpec(name="stale")])

    assert watcher.tool_registry.get_optional("stale") is not None
    assert watcher._reload_tools() == 1
    assert watcher.tool_registry.get_optional("stale") is None


def test_reload_tools_applies_db_update(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    store.tools.save(ToolSpec(name="t1", description="old"))
    watcher = _watcher(store)
    watcher._reload_tools()

    store.tools.save(ToolSpec(name="t1", description="new"))
    assert watcher._reload_tools() == 1
    assert watcher.tool_registry.get("t1").description == "new"


def test_reload_agents_registers_route_from_db(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    store.agents.save(AgentSpec(name="a1", route_tags=["r.one"]))
    watcher = _watcher(store)

    assert watcher._reload_agents() == 1
    assert watcher.agent_registry.routes()["r.one"].name == "a1"


def test_reload_agents_handles_rename_without_route_conflict(tmp_path: Path) -> None:
    # old agent holds the route; DB now has a different agent on the same route.
    store = SqliteRunStore(tmp_path / "runs.db")
    watcher = _watcher(store, agents=[AgentSpec(name="old", route_tags=["r.shared"])])
    store.agents.save(AgentSpec(name="new", route_tags=["r.shared"]))

    # removal (old absent from DB) is applied before the new upsert, freeing the route
    assert watcher._reload_agents() == 2
    assert watcher.agent_registry.routes()["r.shared"].name == "new"
    assert watcher.agent_registry.get_optional("old", "1.0.0") is None


def test_env_settings_reload_is_logged_as_cold_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("WORKER__POLL_INTERVAL_SECONDS=0.1\n", encoding="utf-8")
    store = SqliteRunStore(tmp_path / "runs.db")
    watcher = ConfigWatcher(
        config_dir=config_dir,
        agent_registry=AgentRegistry([]),
        tool_registry=ToolRegistry([]),
        store=store,
    )

    counts = watcher.reload_all()

    assert counts["settings"] == 0
    logs = store.logs._list_logs()
    assert [event.event_type for event in logs] == ["settings_reload_skipped"]
    assert logs[0].level == "WARNING"
    assert "restart the server" in logs[0].message
    assert logs[0].data["file"].endswith(".env")

"""DB-as-source-of-truth startup loader tests.

YAML is now only an optional seed template.  Production agent/tool
registration is expected to live in DB rows loaded from SQL seed/admin APIs.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_store as SqliteRunStore

from domain import ToolSpec
from interfaces.http.server import _load_tool_registry


def _tools_yaml(tmp_path: Path, *, url: str = "http://seed-template/tool") -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(
        f"""
tools:
  - name: example-http-tool
    version: 1.0.0
    endpoint:
      protocol: http
      url: "{url}"
      method: POST
    input_schema:
      type: object
    output_schema:
      type: object
""",
        encoding="utf-8",
    )
    return path


def test_db_rows_are_loaded_without_yaml(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    store.tools.save(
        ToolSpec(
            name="example-http-tool",
            endpoint={"protocol": "http", "url": "https://service.example.test/x", "method": "POST"},
            managed_by="db",
        )
    )

    registry = _load_tool_registry(tmp_path / "missing-tools.yaml", store)

    tool = registry.get("example-http-tool")
    assert tool.endpoint["url"] == "https://service.example.test/x"
    assert tool.managed_by == "db"


def test_existing_db_row_is_not_overwritten_by_yaml(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    # The database is authoritative over optional seed configuration.
    store.tools.save(
        ToolSpec(
            name="example-http-tool",
            endpoint={"protocol": "http", "url": "https://service.example.test/x", "method": "POST"},
            managed_by="db",
        )
    )

    registry = _load_tool_registry(_tools_yaml(tmp_path), store)

    tool = registry.get("example-http-tool")
    assert tool.endpoint["url"] == "https://service.example.test/x"
    assert tool.managed_by == "db"


def test_optional_yaml_seeds_db_when_row_is_missing(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")

    registry = _load_tool_registry(_tools_yaml(tmp_path, url="http://template/tool"), store)

    tool = registry.get("example-http-tool")
    assert tool.endpoint["url"] == "http://template/tool"
    assert tool.managed_by == "yaml"
    assert store.tools.list()[0].name == "example-http-tool"

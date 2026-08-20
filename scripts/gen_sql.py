#!/usr/bin/env python3
"""Regenerate sql/ddl.sql (and sql/seed.dml.sql) from the SQLAlchemy metadata
and the YAML config seeds.  Run after any schema or seed change:

    .venv/bin/python scripts/gen_sql.py

- ddl.sql : PostgreSQL CREATE TABLE / CREATE INDEX for every table in
  store.tables.metadata, in ``CREATE ... IF NOT EXISTS`` form, alphabetical.
- seed.dml.sql : registry seed — agents.yaml -> ai_agent_config,
  tools.yaml -> ai_tool_config — as idempotent upserts on (name, version).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

ROOT = Path(__file__).resolve().parent.parent
PG = postgresql.dialect()


def gen_ddl() -> str:
    from infra.store.tables import metadata

    lines = [
        "-- agent-task-platform schema (PostgreSQL)",
        "-- AUTO-GENERATED from the ORM entities via scripts/gen_sql.py. Do not hand-edit;",
        "-- regenerate after schema changes. Source of truth: store/model/ + Alembic.",
        "",
    ]
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        lines.append(f"-- ===== {table.name} =====")
        ddl = str(CreateTable(table).compile(dialect=PG)).strip()
        ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        ddl = "\n".join(line.rstrip() for line in ddl.splitlines())
        lines.append(ddl.rstrip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            idx = str(CreateIndex(index).compile(dialect=PG)).strip()
            idx = idx.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            lines.append(idx.rstrip() + ";")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _load_yaml_list(path: Path, key: str) -> list[dict]:
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return doc.get(key) or []


def _sql_literal(value: object) -> str:
    """Render a Python value as a PostgreSQL SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dict, list)):
        blob = json.dumps(value, ensure_ascii=False)
        return f"$json${blob}$json$::jsonb"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _upsert(entity: object) -> str:
    """Render an idempotent per-column upsert for a config ORM entity.

    Every mapped column is written to its own column (JSONB columns inlined as
    ``::jsonb`` literals); the autoincrement ``id`` PK is omitted so the
    sequence assigns it, and ``last_time`` is set to ``now()``.  Conflict key
    is ``(name, version)``.
    """
    model_cls = type(entity)
    table = model_cls.__table__.name
    mapper = sa_inspect(model_cls)
    pk_keys = {col.key for col in mapper.primary_key}

    columns: list[str] = []
    literals: list[str] = []
    for attr in mapper.column_attrs:
        col_name = attr.expression.name
        value = getattr(entity, attr.key)
        if attr.key in pk_keys and value is None:
            continue  # autoincrement surrogate id
        columns.append(col_name)
        # ``last_time`` is stamped by the DB at load time (the ORM leaves it
        # None until the repository sets it), so emit now() instead of NULL.
        literals.append("now()" if col_name == "last_time" else _sql_literal(value))

    updates = ",\n  ".join(f"{col} = EXCLUDED.{col}" for col in columns if col not in {"name", "version"})
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n"
        f"  ({', '.join(literals)})\n"
        f"ON CONFLICT (name, version) DO UPDATE SET\n"
        f"  {updates};"
    )


def _subst(value: Any, env: dict[str, str]) -> Any:
    """Recursively replace ``${KEY}`` placeholders in every string with env[KEY].

    Used to bind deployment-specific endpoint values into an optional seed.
    Unknown placeholders are left untouched.
    """
    if not env:
        return value
    if isinstance(value, str):
        for key, replacement in env.items():
            value = value.replace("${" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {k: _subst(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, env) for v in value]
    return value


def gen_seed(env: dict[str, str] | None = None, managed_by: str | None = None) -> str:
    # Normalize through the Pydantic models so the seed matches exactly what
    # the registry loader/ConfigWatcher writes at runtime (defaults filled in),
    # then map to ORM columns via the entity's ``from_domain``.
    from domain import AgentSpec, ToolSpec
    from framework.skill.loader import SkillLoader
    from infra.store.model import AiAgentConfig, AiSkillConfig, AiToolConfig

    env = env or {}
    lines = [
        "-- agent-task-platform registry seed (PostgreSQL)",
        "-- AUTO-GENERATED from config/agents.yaml + config/tools.yaml + config/skills.yaml via scripts/gen_sql.py.",
        "-- Idempotent: re-running upserts by (name, version).",
    ]
    if env:
        lines.append(f"-- env substitutions: {', '.join(sorted(env))}")
    if managed_by:
        lines.append(f"-- managed_by = {managed_by}")
    lines.append("")
    for agent in _load_yaml_list(ROOT / "config" / "agents.yaml", "agents"):
        spec = AgentSpec.model_validate(_subst(agent, env))
        if managed_by:
            spec.managed_by = managed_by
        lines.append(_upsert(AiAgentConfig.from_domain(spec)))
        lines.append("")
    for tool in _load_yaml_list(ROOT / "config" / "tools.yaml", "tools"):
        spec = ToolSpec.model_validate(_subst(tool, env))
        if managed_by:
            spec.managed_by = managed_by
        lines.append(_upsert(AiToolConfig.from_domain(spec)))
        lines.append("")
    skill_loader = SkillLoader(ROOT / "plugins" / "skills")
    for skill in _load_yaml_list(ROOT / "config" / "skills.yaml", "skills"):
        source_path = skill.get("source_path")
        if not isinstance(source_path, str):
            raise ValueError("Skill seed requires a string source_path")
        spec = skill_loader.inspect(source_path)
        skill_loader.verify(spec)
        spec = spec.model_copy(
            update={
                key: _subst(skill[key], env) for key in ("enabled", "owner", "managed_by", "updated_by") if key in skill
            }
        )
        if managed_by:
            spec.managed_by = managed_by
        lines.append(_upsert(AiSkillConfig.from_domain(spec)))
        lines.append("")
    return "\n".join(lines) + "\n"


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--env expects KEY=VALUE, got: {pair!r}")
        key, _, val = pair.partition("=")
        env[key.strip()] = val
    return env


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Bind a ${KEY} placeholder in seed values. Repeatable.",
    )
    ap.add_argument(
        "--managed-by",
        default=None,
        help="Override managed_by on every seeded row (e.g. 'db' for per-env DBA loads).",
    )
    ap.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write the seed SQL here instead of sql/seed.dml.sql (per-env load_<env>.sql).",
    )
    ap.add_argument("--no-ddl", action="store_true", help="Skip regenerating sql/ddl.sql.")
    args = ap.parse_args()
    env = _parse_env(args.env)

    sql_dir = ROOT / "sql"
    sql_dir.mkdir(exist_ok=True)
    written = []
    if not args.no_ddl:
        (sql_dir / "ddl.sql").write_text(gen_ddl(), encoding="utf-8")
        written.append("sql/ddl.sql")
    out_path = Path(args.out) if args.out else sql_dir / "seed.dml.sql"
    out_path.write_text(gen_seed(env, args.managed_by), encoding="utf-8")
    written.append(str(out_path))
    print("regenerated " + ", ".join(written))


if __name__ == "__main__":
    main()

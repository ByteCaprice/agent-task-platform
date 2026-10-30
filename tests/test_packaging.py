from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.cli.main import _init_config, _migrate, _parse_args


@pytest.fixture(scope="session", autouse=True)
def _pg_container():
    yield None


@pytest.fixture(autouse=True)
def _truncate_tables():
    """Packaging tests do not need the suite-wide PostgreSQL fixture."""
    yield


def test_init_config_copies_packaged_templates_to_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "config"
    _init_config(_parse_args(["init-config", "--config-dir", str(target)]))

    assert {path.name for path in target.iterdir()} == {
        "agents.yaml",
        "skills.yaml",
        "tools.yaml",
        ".env.dev.example",
        ".env.profile.example",
    }
    assert "example-compliance" in (target / "skills.yaml").read_text()


def test_packaged_templates_and_migration_history_match_source_assets() -> None:
    resource_root = Path(__file__).resolve().parents[1] / "interfaces" / "resources"
    project_root = Path(__file__).resolve().parents[1]
    for name in ("agents.yaml", "skills.yaml", "tools.yaml", ".env.dev.example", ".env.profile.example"):
        assert (resource_root / "config" / name).read_bytes() == (project_root / "config" / name).read_bytes()
    for name in ("env.py", "README.md", "script.py.mako"):
        assert (resource_root / "migrations" / name).read_bytes() == (project_root / "migrations" / name).read_bytes()
    for name in ("0001_platform_baseline.py", "0002_normalized_platform_config.py"):
        assert (resource_root / "migrations" / "versions" / name).read_bytes() == (
            project_root / "migrations" / "versions" / name
        ).read_bytes()
    skill = "example-compliance"
    source = project_root / "plugins" / "skills" / skill
    for path in source.rglob("*"):
        if path.is_file():
            relative = path.relative_to(source)
            assert (resource_root / "skills" / skill / relative).read_bytes() == path.read_bytes()


def test_init_config_refuses_overwrite_unless_forced(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    existing = target / "agents.yaml"
    existing.write_text("local config")

    try:
        _init_config(_parse_args(["init-config", "--config-dir", str(target)]))
    except FileExistsError:
        pass
    else:
        raise AssertionError("init-config must protect existing files")

    assert existing.read_text() == "local config"


def test_migrate_uses_packaged_migration_history(monkeypatch, tmp_path: Path) -> None:
    from alembic import command

    captured: dict[str, object] = {}

    def upgrade(config, revision: str) -> None:
        from alembic.script import ScriptDirectory

        captured["script_location"] = config.get_main_option("script_location")
        captured["revision"] = revision
        captured["head"] = ScriptDirectory.from_config(config).get_current_head()

    monkeypatch.setattr(command, "upgrade", upgrade)
    _migrate(_parse_args(["migrate", "--config-dir", str(tmp_path)]))

    migration_dir = Path(str(captured["script_location"]))
    assert migration_dir.is_dir()
    assert (migration_dir / "versions" / "0001_platform_baseline.py").is_file()
    assert (migration_dir / "versions" / "0002_normalized_platform_config.py").is_file()
    assert captured["revision"] == "head"
    assert captured["head"] == "0002_normalized_platform_config"

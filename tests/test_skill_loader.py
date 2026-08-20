from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.registry import RegistryError, SkillRegistry
from framework.skill import SkillLoader, SkillResourceError, SkillValidationError
from interfaces.http.routes.admin import SkillPublishRequest, publish_skill


def _write_skill(
    root: Path, *, name: str = "example-skill", description: str = "Use this Skill for example tasks."
) -> Path:
    skill = root / name
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        '  version: "2.0.0"\n'
        "allowed-tools: example-tool other-tool\n"
        "---\n"
        "\n"
        "# Example\n"
        "\n"
        "Follow the example workflow.\n",
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("Example reference\n", encoding="utf-8")
    return skill


def test_loader_inspects_skill_and_loads_instruction_and_reference(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)

    spec = loader.inspect("example-skill")

    assert spec.name == "example-skill"
    assert spec.version == "2.0.0"
    assert spec.allowed_tools == ["example-tool", "other-tool"]
    assert loader.load_instructions(spec) == "# Example\n\nFollow the example workflow."
    assert loader.read_text_resource(spec, "references/guide.md") == "Example reference\n"
    loader.verify(spec)


def test_packaged_example_skill_is_valid() -> None:
    root = Path(__file__).resolve().parents[1] / "plugins" / "skills"

    spec = SkillLoader(root).inspect("example-compliance")

    assert spec.name == "example-compliance"
    assert spec.version == "1.0.0"


def test_loader_hash_changes_when_artifact_resource_changes(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root)
    loader = SkillLoader(root)

    first = loader.inspect("example-skill")
    (skill / "references" / "guide.md").write_text("Updated reference\n", encoding="utf-8")
    second = loader.inspect("example-skill")

    assert first.content_hash != second.content_hash
    with pytest.raises(SkillValidationError, match="hash does not match"):
        loader.verify(first.model_copy(update={"artifact": []}))


def test_loader_reads_db_artifact_without_a_local_skill_directory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    local = loader.inspect("example-skill")
    db_managed = local.model_copy(update={"source_path": "db://example-skill@2.0.0"})

    assert db_managed.artifact
    assert loader.load_instructions(db_managed) == "# Example\n\nFollow the example workflow."
    assert loader.read_text_resource(db_managed, "references/guide.md") == "Example reference\n"
    loader.verify(db_managed)


def test_admin_publish_stores_a_versioned_db_artifact(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    artifact = loader.inspect("example-skill").artifact
    saved = []
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                skill_loader=SkillLoader(tmp_path / "no-local-skills"),
                skill_registry=SkillRegistry([]),
                store=SimpleNamespace(skills=SimpleNamespace(save=saved.append)),
            )
        )
    )

    published = asyncio.run(publish_skill(request, SkillPublishRequest(artifact=artifact), x_actor="tester"))

    assert published["source_path"] == "db://example-skill@2.0.0"
    assert saved[0].managed_by == "admin"
    (root / "example-skill" / "SKILL.md").write_text(
        (root / "example-skill" / "SKILL.md").read_text(encoding="utf-8") + "Changed.\n",
        encoding="utf-8",
    )
    changed = loader.inspect("example-skill")

    with pytest.raises(RegistryError, match="publish a new version"):
        asyncio.run(publish_skill(request, SkillPublishRequest(artifact=changed.artifact)))


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../example-skill", "must not contain"),
        ("/tmp/example-skill", "must be relative"),
    ],
)
def test_loader_rejects_skill_paths_outside_trusted_root(tmp_path: Path, path: str, message: str) -> None:
    loader = SkillLoader(tmp_path / "skills")

    with pytest.raises(SkillValidationError, match=message):
        loader.inspect(path)


def test_loader_rejects_invalid_frontmatter_and_resource_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root)
    (skill / "SKILL.md").write_text("# No frontmatter\n", encoding="utf-8")
    loader = SkillLoader(root)

    with pytest.raises(SkillValidationError, match="frontmatter"):
        loader.inspect("example-skill")

    _write_skill(root)
    spec = loader.inspect("example-skill")
    with pytest.raises(SkillResourceError, match="safe relative"):
        loader.read_text_resource(spec, "../secret.txt")
    with pytest.raises(SkillResourceError, match="references/ or assets"):
        loader.read_text_resource(spec, "SKILL.md")


def test_loader_rejects_manifest_script_outside_scripts_directory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root)
    (skill / "agents").mkdir()
    (skill / "agents" / "platform.yaml").write_text(
        "schema_version: 1\nscripts:\n  - name: unsafe\n    path: outside.py\n    interpreter: python\n",
        encoding="utf-8",
    )
    loader = SkillLoader(root)

    with pytest.raises(SkillValidationError, match="under scripts"):
        loader.inspect("example-skill")

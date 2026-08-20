"""Validate Skills loaded from immutable DB artifacts or local development files."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from domain import SkillArtifactFile, SkillScriptSpec, SkillSpec
from framework.skill.errors import SkillResourceError, SkillValidationError


@dataclass(frozen=True, slots=True)
class ResourceHandle:
    """Bounded binary resource returned without exposing an artifact path."""

    name: str
    path: str
    mime_type: str
    content: bytes


class SkillLoader:
    """Parse and validate Skill packages beneath one trusted artifact root."""

    def __init__(self, trusted_root: str | Path, *, max_resource_bytes: int = 1024 * 1024) -> None:
        self.trusted_root = Path(trusted_root).resolve()
        self.max_resource_bytes = max_resource_bytes

    def inspect(self, source_path: str | Path) -> SkillSpec:
        root, relative_source = self._resolve_skill_root(source_path)
        spec = self.inspect_artifact(self._artifact_from_root(root), source_path=relative_source)
        if root.name != spec.name:
            raise SkillValidationError(f"Skill directory {root.name!r} does not match name {spec.name!r}")
        return spec

    def inspect_artifact(self, artifact: list[SkillArtifactFile], *, source_path: str) -> SkillSpec:
        """Build a verified spec from files stored in the DB as one artifact."""
        files = self._artifact_mapping(artifact)
        skill_md = files.get("SKILL.md")
        if skill_md is None:
            raise SkillValidationError("Skill artifact is missing SKILL.md")
        frontmatter, _ = self._parse_skill_markdown_text(skill_md, source="SKILL.md")
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not isinstance(name, str) or not name:
            raise SkillValidationError("SKILL.md frontmatter requires a non-empty name")
        if not isinstance(description, str) or not description:
            raise SkillValidationError("SKILL.md frontmatter requires a non-empty description")
        metadata = frontmatter.get("metadata") or {}
        if not isinstance(metadata, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
        ):
            raise SkillValidationError("SKILL.md metadata must be a string-to-string mapping")
        compatibility = frontmatter.get("compatibility")
        if compatibility is not None and not isinstance(compatibility, str):
            raise SkillValidationError("SKILL.md compatibility must be a string")
        allowed_tools = self._parse_allowed_tools(frontmatter.get("allowed-tools"))
        scripts = self._load_manifest_scripts_from_files(files)
        try:
            return SkillSpec(
                name=name,
                version=metadata.get("version", "1.0.0"),
                description=description,
                source_path=source_path,
                content_hash=self._artifact_content_hash(files),
                compatibility=compatibility,
                allowed_tools=allowed_tools,
                scripts=scripts,
                artifact=artifact,
                metadata=metadata,
            )
        except ValueError as exc:
            raise SkillValidationError(str(exc)) from exc

    def load_instructions(self, spec: SkillSpec) -> str:
        if spec.artifact:
            skill_md = self._artifact_mapping(spec.artifact).get("SKILL.md")
            if skill_md is None:
                raise SkillValidationError("Skill artifact is missing SKILL.md")
            _, body = self._parse_skill_markdown_text(skill_md, source="SKILL.md")
            return body
        root, _ = self._resolve_skill_root(spec.source_path)
        self._verify_name(root, spec)
        _, body = self._parse_skill_markdown(root / "SKILL.md")
        return body

    def read_text_resource(self, spec: SkillSpec, relative_path: str) -> str:
        if spec.artifact:
            try:
                return self._artifact_resource(spec, relative_path).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillResourceError(f"Skill resource {relative_path!r} is not UTF-8 text") from exc
        resource = self._resolve_resource(spec, relative_path)
        try:
            return resource.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillResourceError(f"Skill resource {relative_path!r} is not UTF-8 text") from exc

    def open_binary_resource(self, spec: SkillSpec, relative_path: str) -> ResourceHandle:
        if spec.artifact:
            content = self._artifact_resource(spec, relative_path)
            artifact_file = next(file for file in spec.artifact if file.path == relative_path)
            return ResourceHandle(
                name=spec.name,
                path=relative_path,
                mime_type=artifact_file.mime_type
                or mimetypes.guess_type(relative_path)[0]
                or "application/octet-stream",
                content=content,
            )
        resource = self._resolve_resource(spec, relative_path)
        return ResourceHandle(
            name=spec.name,
            path=relative_path,
            mime_type=mimetypes.guess_type(resource.name)[0] or "application/octet-stream",
            content=resource.read_bytes(),
        )

    def materialize_to_directory(self, spec: SkillSpec, target_dir: str | Path) -> Path:
        """Extract a verified Skill's files into a target directory for script execution."""
        dest_dir = Path(target_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        if spec.artifact:
            mapping = self._artifact_mapping(spec.artifact)
            for rel_path, content in mapping.items():
                dest = (dest_dir / rel_path).resolve()
                if not str(dest).startswith(str(dest_dir)):
                    raise SkillResourceError(f"File path {rel_path!r} escapes destination")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            return dest_dir

        root, _ = self._resolve_skill_root(spec.source_path)
        for item in root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                rel = item.relative_to(root)
                dest = (dest_dir / rel).resolve()
                if not str(dest).startswith(str(dest_dir)):
                    raise SkillResourceError(f"File path {rel.as_posix()!r} escapes destination")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(item.read_bytes())
        return dest_dir

    def verify(self, spec: SkillSpec) -> None:
        inspected = (
            self.inspect_artifact(spec.artifact, source_path=spec.source_path)
            if spec.artifact
            else self.inspect(spec.source_path)
        )
        if inspected.name != spec.name or inspected.version != spec.version:
            raise SkillValidationError(f"Skill artifact identity does not match {spec.name}@{spec.version}")
        if inspected.content_hash != spec.content_hash:
            raise SkillValidationError(f"Skill artifact hash does not match {spec.name}@{spec.version}")

    def _resolve_skill_root(self, source_path: str | Path) -> tuple[Path, str]:
        raw_path = Path(source_path)
        if raw_path.is_absolute():
            raise SkillValidationError("Skill source_path must be relative to the trusted root")
        if any(part == ".." for part in raw_path.parts):
            raise SkillValidationError("Skill source_path must not contain '..'")
        root = (self.trusted_root / raw_path).resolve()
        try:
            relative = root.relative_to(self.trusted_root)
        except ValueError as exc:
            raise SkillValidationError("Skill source_path escapes the trusted root") from exc
        if not root.is_dir():
            raise SkillValidationError(f"Skill artifact {relative.as_posix()!r} does not exist")
        self._reject_symlinks(root)
        return root, relative.as_posix()

    def _resolve_resource(self, spec: SkillSpec, relative_path: str) -> Path:
        normalized = self._normalize_resource_path(relative_path)
        self._validate_resource_path(normalized)
        root, _ = self._resolve_skill_root(spec.source_path)
        candidate = root / normalized
        if not candidate.is_file() or candidate.is_symlink():
            raise SkillResourceError(f"Skill resource {relative_path!r} does not exist")
        resource = candidate.resolve()
        try:
            resource.relative_to(root)
        except ValueError as exc:
            raise SkillResourceError("Skill resource path escapes the Skill root") from exc
        if resource.stat().st_size > self.max_resource_bytes:
            raise SkillResourceError(f"Skill resource {relative_path!r} exceeds the configured size limit")
        return resource

    def _artifact_resource(self, spec: SkillSpec, relative_path: str) -> bytes:
        files = self._artifact_mapping(spec.artifact)
        normalized = self._normalize_resource_path(relative_path, set(files))
        self._validate_resource_path(normalized)
        content = files.get(normalized)
        if content is None:
            raise SkillResourceError(f"Skill resource {relative_path!r} does not exist")
        if len(content) > self.max_resource_bytes:
            raise SkillResourceError(f"Skill resource {relative_path!r} exceeds the configured size limit")
        return content

    @classmethod
    def _normalize_resource_path(cls, relative_path: str, available_paths: set[str] | None = None) -> str:
        raw_path = Path(relative_path)
        if raw_path.is_absolute() or any(part == ".." for part in raw_path.parts):
            raise SkillResourceError("Skill resource path must be a safe relative path")
        posix = raw_path.as_posix()
        if posix.startswith("references/") or posix.startswith("assets/"):
            return posix
        if available_paths is not None:
            if f"references/{posix}" in available_paths:
                return f"references/{posix}"
            if f"assets/{posix}" in available_paths:
                return f"assets/{posix}"
        return posix

    @staticmethod
    def _validate_resource_path(relative_path: str) -> None:
        raw_path = Path(relative_path)
        if raw_path.is_absolute() or any(part == ".." for part in raw_path.parts):
            raise SkillResourceError("Skill resource path must be a safe relative path")
        if not raw_path.parts or raw_path.parts[0] not in {"references", "assets"}:
            raise SkillResourceError("Skill resources must be under references/ or assets/")

    def _load_manifest_scripts_from_files(self, files: dict[str, bytes]) -> list[SkillScriptSpec]:
        manifest_content = files.get("agents/platform.yaml")
        if manifest_content is None:
            return []
        try:
            manifest = yaml.safe_load(manifest_content.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SkillValidationError("agents/platform.yaml is not valid YAML") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise SkillValidationError("agents/platform.yaml requires schema_version: 1")
        raw_scripts = manifest.get("scripts", [])
        if not isinstance(raw_scripts, list):
            raise SkillValidationError("agents/platform.yaml scripts must be a list")
        try:
            scripts = [SkillScriptSpec.model_validate(item) for item in raw_scripts]
        except ValueError as exc:
            raise SkillValidationError(f"Invalid Skill script declaration: {exc}") from exc
        seen_names: set[str] = set()
        for script in scripts:
            if script.name in seen_names:
                raise SkillValidationError(f"Skill script {script.name!r} is declared more than once")
            seen_names.add(script.name)
            path = Path(script.path)
            if (
                path.is_absolute()
                or any(part == ".." for part in path.parts)
                or not path.parts
                or path.parts[0] != "scripts"
            ):
                raise SkillValidationError(f"Skill script {script.name!r} must be under scripts/")
            if path.as_posix() not in files:
                raise SkillValidationError(f"Skill script {script.name!r} does not exist")
        return scripts

    @staticmethod
    def _parse_allowed_tools(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, str):
            raise SkillValidationError("SKILL.md allowed-tools must be a space-separated string")
        return value.split()

    @staticmethod
    def _parse_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError("SKILL.md must be UTF-8 text") from exc
        return SkillLoader._parse_skill_markdown_text(text.encode(), source="SKILL.md")

    @staticmethod
    def _parse_skill_markdown_text(content: bytes, *, source: str) -> tuple[dict[str, Any], str]:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError(f"{source} must be UTF-8 text") from exc
        text = text.replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise SkillValidationError("SKILL.md must start with YAML frontmatter")
        closing = text.find("\n---\n", 4)
        if closing < 0:
            raise SkillValidationError("SKILL.md frontmatter is not closed")
        try:
            frontmatter = yaml.safe_load(text[4:closing]) or {}
        except yaml.YAMLError as exc:
            raise SkillValidationError("SKILL.md frontmatter is not valid YAML") from exc
        if not isinstance(frontmatter, dict):
            raise SkillValidationError("SKILL.md frontmatter must be a mapping")
        return frontmatter, text[closing + 5 :].strip()

    @staticmethod
    def _verify_name(root: Path, spec: SkillSpec) -> None:
        if root.name != spec.name:
            raise SkillValidationError(f"Skill artifact does not match {spec.name!r}")

    @staticmethod
    def _reject_symlinks(root: Path) -> None:
        for parent, directories, filenames in os.walk(root, followlinks=False):
            for entry in [*directories, *filenames]:
                path = Path(parent) / entry
                if path.is_symlink():
                    raise SkillValidationError(f"Skill artifact contains symlink {path.relative_to(root).as_posix()!r}")

    def _artifact_from_root(self, root: Path) -> list[SkillArtifactFile]:
        files: list[SkillArtifactFile] = []
        for parent, _, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                path = Path(parent) / name
                if path.is_symlink() or not path.is_file():
                    raise SkillValidationError("Skill artifact contains an unsupported file")
                relative = path.relative_to(root).as_posix()
                files.append(
                    SkillArtifactFile(
                        path=relative,
                        content_base64=base64.b64encode(path.read_bytes()).decode(),
                        mime_type=mimetypes.guess_type(relative)[0],
                    )
                )
        return files

    @staticmethod
    def _artifact_mapping(artifact: list[SkillArtifactFile]) -> dict[str, bytes]:
        return {file.path: file.content() for file in artifact}

    @staticmethod
    def _artifact_content_hash(files: dict[str, bytes]) -> str:
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            relative = name.encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

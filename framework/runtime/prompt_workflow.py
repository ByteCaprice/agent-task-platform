"""Reusable helpers for prompt-only business workflow agents."""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.runtime.files import DEFAULT_ALLOWED_MIME_TYPES, DEFAULT_MAX_FILE_SIZE, FileFetchError
from framework.skill.catalog import compose_instructions
from framework.skill.session import EmptySkillSession

if TYPE_CHECKING:
    from domain import RunFile
    from framework.runtime.context import AgentContext


class PromptWorkflowAgent:
    def __init__(
        self,
        *,
        anchor_file: str,
        prompt_file: str = "prompt.md",
        requires_files: bool = False,
        run_label: str = "prompt workflow",
    ) -> None:
        self.package_dir = Path(anchor_file).resolve().parent
        self.prompt_file = prompt_file
        self.prompt = (self.package_dir / prompt_file).read_text()
        self.prompt_name = f"{self.package_dir.name}/{prompt_file}"
        self.requires_files = requires_files
        self.run_label = run_label

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        files = await resolve_input_files(context, input_data)
        if self.requires_files and not files:
            raise ValueError(f"{self.run_label} requires at least one input file")
        message = self._build_user_message(files, input_data)
        composed = compose_instructions(
            base_instructions="",
            session=getattr(context, "skills", EmptySkillSession()),
            catalog_max_chars=int((context.agent.runtime or {}).get("skill_catalog_max_chars", 8_000)),
        )
        runtime: dict[str, Any] = {
            "prompt_name": self.prompt_name,
            "prompt_version": context.agent.version,
            "prompt_fingerprint_content": self.prompt,
        }
        if composed.provenance:
            runtime["instructions"] = composed.instructions
            runtime["prompt_fingerprint_content"] = self.prompt + "\n" + composed.fingerprint_content
            runtime["skill_provenance"] = composed.provenance
        model_output = await context.model_client.complete(
            run_id=context.run_id,
            trace_id=context.trace_id,
            agent_name=context.agent.name,
            agent_version=context.agent.version,
            input_data={"messages": [message]},
            metadata=context.metadata,
            runtime=runtime,
        )
        return {
            "data": extract_model_data(model_output),
            "model_call_id": model_output.get("model_call_id"),
            "agent": {"name": context.agent.name, "version": context.agent.version},
        }

    def _build_user_message(self, files: list[dict[str, Any]], input_data: dict[str, Any]) -> dict[str, Any]:
        if files:
            content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (
                        f"{self.prompt}\n\n"
                        f"Input files are attached below in order. totalFiles must be {len(files)}. "
                        "Use the exact fileName values and zero-based fileIndex values listed below."
                    ),
                }
            ]
            business_input = _business_input(input_data)
            if business_input:
                content.append({"type": "text", "text": f"Business input: {business_input}"})
            for index, file in enumerate(files):
                content.append(
                    {"type": "text", "text": f"Attached input fileIndex={index}, fileName={file['file_name']}"}
                )
                content.append(content_item(file))
            return {"role": "user", "content": content}

        return {
            "role": "user",
            "content": (f"{self.prompt}\n\nInput:\n{_text_input(input_data)}"),
        }


async def resolve_input_files(context: AgentContext, input_data: dict[str, Any]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for path_value in input_file_paths(input_data):
        path = Path(path_value).expanduser().resolve()
        resolved.append(local_file_payload(path, context=context))

    _validate_run_file_references(context.files)
    for run_file in context.files:
        payload = await run_file_payload(context, run_file)
        if payload:
            resolved.append(payload)
    return deduplicate_file_payloads(resolved)


def input_file_paths(input_data: dict[str, Any]) -> list[str]:
    raw = (
        input_data.get("file_paths")
        or input_data.get("filePaths")
        or input_data.get("file_path")
        or input_data.get("filePath")
    )
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


async def run_file_payload(context: AgentContext, run_file: RunFile) -> dict[str, Any] | None:
    local_path = run_file.metadata.get("local_path") or run_file.metadata.get("path")
    if isinstance(local_path, str) and local_path:
        path = Path(local_path).expanduser().resolve()
        return local_file_payload(
            path,
            file_name=run_file.file_name,
            mime_type=run_file.mime_type,
            context=context,
            source_keys=_run_file_source_keys(run_file, local_path=path),
        )
    source_type = run_file.metadata.get("sourceType")
    if source_type == "BASE64":
        content_b64 = run_file.metadata.get("content_base64")
        if not content_b64:
            return None
        mime_type = run_file.mime_type or guess_mime(run_file.file_name)
        _validate_mime(context, mime_type)
        compact = "".join(str(content_b64).split())
        padding = len(compact) - len(compact.rstrip("="))
        estimated_size = max(0, len(compact) * 3 // 4 - padding)
        _validate_size(context, estimated_size, source=run_file.file_name or "BASE64 file")
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Invalid BASE64 content for {run_file.file_name or 'input-file'}") from exc
        _validate_size(context, len(data), source=run_file.file_name or "BASE64 file")
        return {
            "file_name": run_file.file_name or "input-file",
            "mime_type": mime_type,
            "data": data,
            "_source_keys": _run_file_source_keys(run_file),
        }
    if source_type == "OSS":
        raise ValueError("OSS file source is not supported yet; use URL (e.g. an OSS presigned URL) or BASE64")
    if run_file.url:
        data = await context.file_client.fetch(file_id=run_file.file_id, url=None if run_file.file_id else run_file.url)
        mime_type = run_file.mime_type or guess_mime(run_file.file_name or run_file.url)
        file_name = run_file.file_name or run_file.file_id or Path(run_file.url).name or "input-file"
        return {
            "file_name": file_name,
            "mime_type": mime_type,
            "data": data,
            "_source_keys": _run_file_source_keys(run_file),
        }
    return None


def local_file_payload(
    path: Path,
    *,
    file_name: str | None = None,
    mime_type: str | None = None,
    context: AgentContext | None = None,
    source_keys: list[str] | None = None,
) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Input file path is not a regular file: {path}")
    size = path.stat().st_size
    _validate_size(context, size, source=str(path))
    resolved_mime_type = mime_type or guess_mime(file_name or path.name)
    _validate_mime(context, resolved_mime_type)
    return {
        "file_name": file_name or path.name,
        "mime_type": resolved_mime_type,
        "data": path.read_bytes(),
        "_source_keys": source_keys or [f"path:{path}"],
    }


def deduplicate_file_payloads(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    by_source: dict[str, tuple[str, str, str]] = {}
    content_digests: set[str] = set()
    for file in files:
        digest = hashlib.sha256(file["data"]).hexdigest()
        metadata = (digest, file["file_name"], file["mime_type"])
        duplicate_source = False
        for source_key in file.get("_source_keys", []):
            previous = by_source.get(source_key)
            if previous is not None:
                if previous != metadata:
                    raise ValueError(f"Conflicting metadata or content for file identity {source_key!r}")
                duplicate_source = True
            else:
                by_source[source_key] = metadata
        if duplicate_source or digest in content_digests:
            continue
        content_digests.add(digest)
        resolved.append(
            {
                "file_name": file["file_name"],
                "mime_type": file["mime_type"],
                "data": file["data"],
            }
        )
    return resolved


def content_item(file: dict[str, Any]) -> dict[str, Any]:
    data_url = data_url_for(file["data"], file["mime_type"])
    if file["mime_type"] == "application/pdf":
        return {"type": "file", "file": {"filename": file["file_name"], "file_data": data_url}}
    return {"type": "image_url", "image_url": {"url": data_url}}


def data_url_for(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"


def guess_mime(name: str | None) -> str:
    mime_type, _ = mimetypes.guess_type(name or "")
    return mime_type or "application/octet-stream"


def _run_file_source_keys(run_file: RunFile, *, local_path: Path | None = None) -> list[str]:
    keys: list[str] = []
    if run_file.file_id:
        keys.append(f"file_id:{run_file.file_id}")
    if local_path is not None:
        keys.append(f"path:{local_path}")
    if run_file.url:
        keys.append(f"url:{run_file.url}")
    return keys


def _validate_run_file_references(run_files: list[RunFile]) -> None:
    by_file_id: dict[str, tuple[Any, ...]] = {}
    for run_file in run_files:
        if not run_file.file_id:
            continue
        local_path = run_file.metadata.get("local_path") or run_file.metadata.get("path")
        content_b64 = run_file.metadata.get("content_base64")
        signature = (
            str(Path(local_path).expanduser().resolve()) if isinstance(local_path, str) else None,
            run_file.url,
            run_file.file_name,
            run_file.mime_type,
            run_file.metadata.get("sourceType"),
            hashlib.sha256(str(content_b64).encode()).hexdigest() if content_b64 else None,
        )
        previous = by_file_id.get(run_file.file_id)
        if previous is not None and previous != signature:
            raise ValueError(f"Conflicting references for file identity 'file_id:{run_file.file_id}'")
        by_file_id[run_file.file_id] = signature


def _validate_size(context: AgentContext | None, size: int, *, source: str) -> None:
    file_client = getattr(context, "file_client", None) if context is not None else None
    validator = getattr(file_client, "validate_size", None)
    if validator is not None:
        validator(size, source=source)
    elif size > DEFAULT_MAX_FILE_SIZE:
        raise FileFetchError(f"{source} size {size} bytes exceeds max {DEFAULT_MAX_FILE_SIZE} bytes")


def _validate_mime(context: AgentContext | None, mime_type: str) -> None:
    file_client = getattr(context, "file_client", None) if context is not None else None
    validator = getattr(file_client, "validate_mime_type", None)
    if validator is not None:
        validator(mime_type)
    elif mime_type.split(";", 1)[0].strip().lower() not in DEFAULT_ALLOWED_MIME_TYPES:
        raise FileFetchError(f"Content-Type {mime_type!r} is not allowed")


def extract_model_data(model_output: dict[str, Any]) -> Any:
    if set(model_output.keys()) <= {"data", "agent", "model_call_id"}:
        return model_output.get("data")
    return {key: value for key, value in model_output.items() if key not in {"agent", "model_call_id"}}


def _text_input(input_data: dict[str, Any]) -> Any:
    for key in ("note", "transactionNote", "transaction_note", "text", "input"):
        if key in input_data:
            return input_data[key]
    return input_data


def _business_input(input_data: dict[str, Any]) -> dict[str, Any]:
    ignored = {"file_paths", "filePaths", "file_path", "filePath"}
    return {key: value for key, value in input_data.items() if key not in ignored}

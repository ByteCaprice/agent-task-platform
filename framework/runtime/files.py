"""``FileClient``: fetches a run's input files (by file id or URL) with SSRF
protection and resource limits (max size, timeout, MIME allowlist), streaming
downloads so oversized files are aborted mid-transfer.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import httpx

from domain import LogEvent, RunFile
from infra.outbound_policy import OutboundPolicy
from infra.store import RunStore

# Default limits (can be overridden per FileClient instance)
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
DEFAULT_FETCH_TIMEOUT = 30.0
DEFAULT_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "application/pdf",
        "application/json",
        "text/plain",
        "text/csv",
        "application/octet-stream",
    }
)


class FileFetchError(Exception):
    """Raised when a file fetch violates resource limits."""


class FileClient:
    """Client for fetching run files with SSRF protection and resource limits.

    Parameters
    ----------
    files:
        List of :class:`RunFile` references available to the run.
    store:
        Task store for logging file access events.
    run_id, trace_id:
        Identifiers for log correlation.
    url_allowlist:
        Optional list of allowed URL prefixes (scheme://host/path).
    max_file_size:
        Maximum download size in bytes.  Downloads exceeding this are
        aborted mid-stream.  Default: 50 MB.
    fetch_timeout:
        Timeout for HTTP downloads in seconds.
    allowed_mime_types:
        Set of allowed MIME types.  ``application/octet-stream`` is always
        allowed for compatibility.  Set to ``None`` to skip MIME checks.
    """

    def __init__(
        self,
        *,
        files: list[RunFile],
        store: RunStore,
        run_id: str,
        trace_id: str,
        url_allowlist: list[str] | None = None,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        allowed_mime_types: frozenset[str] | None = DEFAULT_ALLOWED_MIME_TYPES,
    ) -> None:
        self._files = files
        self._store = store
        self._run_id = run_id
        self._trace_id = trace_id
        self._url_allowlist = url_allowlist or []
        self._outbound_policy = OutboundPolicy(
            allowlist=self._url_allowlist,
            block_private_networks=True,
        )
        self._max_file_size = max_file_size
        self._fetch_timeout = fetch_timeout
        self._allowed_mime_types = allowed_mime_types

    def list(self) -> list[RunFile]:
        self._log("file_references_listed", {"count": len(self._files)})
        return list(self._files)

    @property
    def max_file_size(self) -> int:
        return self._max_file_size

    def validate_size(self, size: int, *, source: str = "file") -> None:
        if size > self._max_file_size:
            raise FileFetchError(f"{source} size {size} bytes exceeds max {self._max_file_size} bytes")

    def validate_mime_type(self, mime_type: str) -> None:
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if self._allowed_mime_types and normalized not in self._allowed_mime_types:
            raise FileFetchError(f"Content-Type {normalized!r} is not allowed")

    def list_dicts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.list()]

    def get(self, file_id: str) -> RunFile | None:
        for file in self._files:
            if file.file_id == file_id:
                self._log("file_reference_accessed", {"file_id": file_id, "found": True})
                return file
        self._log("file_reference_accessed", {"file_id": file_id, "found": False})
        return None

    def require(self, file_id: str) -> RunFile:
        file = self.get(file_id)
        if not file:
            raise KeyError(f"Unknown run file_id {file_id!r}")
        return file

    def primary(self) -> RunFile | None:
        file = self._files[0] if self._files else None
        self._log(
            "file_reference_primary_accessed", {"file_id": file.file_id if file else None, "found": file is not None}
        )
        return file

    async def fetch(
        self,
        *,
        file_id: str | None = None,
        url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> bytes:
        """Fetch file content as bytes with resource-limit enforcement.

        Raises :class:`FileFetchError` if the download exceeds
        ``max_file_size`` or the content-type is not allowed.
        """
        target_url = url
        file_mime: str | None = None
        if file_id:
            file = self.require(file_id)
            target_url = file.url
            file_mime = file.mime_type
        if not target_url:
            raise ValueError("File fetch requires a URL or a run file with url")

        self._outbound_policy.validate(target_url)
        timeout = timeout_seconds or self._fetch_timeout

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=5) as client:
            async with client.stream("GET", target_url) as response:
                response.raise_for_status()

                # Check Content-Length early if available
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        cl = int(content_length)
                    except ValueError:
                        cl = None
                    if cl is not None:
                        self.validate_size(cl)

                # Check content-type
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if file_mime and content_type and content_type != "application/octet-stream":
                    # If run file declares a mime_type, verify server sends the same
                    if content_type != file_mime.lower():
                        raise FileFetchError(f"Content-Type mismatch: expected {file_mime}, got {content_type}")
                if content_type:
                    self.validate_mime_type(content_type)

                # Stream-download with size enforcement
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    total += len(chunk)
                    if total > self._max_file_size:
                        raise FileFetchError(
                            f"Download exceeded max size {self._max_file_size} bytes (received {total} bytes)"
                        )
                    chunks.append(chunk)

                content = b"".join(chunks)

        self._log(
            "file_reference_fetched",
            {
                "file_id": file_id,
                "url": target_url,
                "bytes": len(content),
                "content_type": content_type or None,
            },
        )
        return content

    async def fetch_to_temp(
        self,
        *,
        file_id: str | None = None,
        url: str | None = None,
        timeout_seconds: float | None = None,
        suffix: str = "",
        prefix: str = "ace_file_",
    ) -> tuple[bytes, str]:
        """Fetch file and also write to a named temporary file.

        Returns ``(content_bytes, temp_file_path)``.  The caller is
        responsible for deleting the temp file when done.
        """
        content = await self.fetch(file_id=file_id, url=url, timeout_seconds=timeout_seconds)
        fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        except Exception:
            os.unlink(path)
            raise
        return content, path

    def _log(self, event_type: str, data: dict[str, Any]) -> None:
        self._store.logs.add(
            LogEvent(
                run_id=self._run_id,
                trace_id=self._trace_id,
                component="file_client",
                event_type=event_type,
                message="Task file reference accessed",
                data=data,
            )
        )

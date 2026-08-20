"""Runtime file enums — how files are described to the model at run time.

Source type (URL / BASE64 / OSS), media type (IMAGE / PDF) and image detail
hint used by runtime adapters.
"""

from __future__ import annotations

from enum import StrEnum


class RuntimeFileSourceType(StrEnum):
    """File source accepted by runtime adapters.

    - ``URL``    — ``content`` is an HTTP(S) URL (incl. OSS presigned URL / "ossUrl").
    - ``BASE64`` — ``content`` is a base64-encoded byte stream ("文件流").
    - ``OSS``    — ``content`` is a bare OSS object key. Not supported yet
       (direct remote object-store references are intentionally unsupported).
    """

    OSS = "OSS"
    URL = "URL"
    BASE64 = "BASE64"


class RuntimeFileMediaType(StrEnum):
    """File media type."""

    IMAGE = "IMAGE"
    PDF = "PDF"


class RuntimeImageDetail(StrEnum):
    """Image detail hint."""

    AUTO = "AUTO"
    HIGH = "HIGH"
    LOW = "LOW"

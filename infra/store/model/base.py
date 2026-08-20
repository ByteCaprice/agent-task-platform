"""Declarative base and shared column types for Agent Task Platform.

Design conventions
---------------------------------------------------
* Entity == table.  One Pydantic field maps to exactly one column.
* Scalar fields → typed columns; ``dict`` / ``list`` / nested-model fields →
  their own purpose-named JSON column (JSONB on PostgreSQL).  There is **no**
  catch-all ``payload_json`` blob.
* Primary key column is always named ``id``.  Runtime records use a string
  (UUID) id; config tables use a BIGINT autoincrement id.
* Audit timestamps are stored in DB columns ``create_time`` / ``last_time``
  while the ORM attribute names mirror the domain objects
  (``create_time`` / ``update_time``).
* ``status`` is VARCHAR; booleans for config use varchar
  ``'1'`` / ``'0'`` convention where applicable, otherwise native Boolean.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Declarative base for all ORM entities."""


class UTCDateTime(TypeDecorator):
    """Timezone-aware ``DateTime`` that defensively normalizes to UTC.

    PostgreSQL ``timestamptz`` already preserves the offset; this guards
    against any naive datetime slipping in by stamping UTC on bind/result.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


# Dynamic interface content (dict / list / nested model) → JSONB.
JSON_VARIANT = JSONB

# 64-bit autoincrement PK for config tables (BIGSERIAL on PostgreSQL).
AUTO_PK = BigInteger

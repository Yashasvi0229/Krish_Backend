"""
Base SQLAlchemy model.

All ORM models inherit from `Base` and typically also mix in `TimestampMixin`
and/or `SoftDeleteMixin` to get audit columns for free.

We set a deterministic naming convention on Base.metadata so Alembic
autogenerate produces stable constraint names — this makes migration diffs
predictable and prevents "constraint renamed" churn between generations.
See https://alembic.sqlalchemy.org/en/latest/naming.html
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable, predictable names for indexes, unique constraints, check constraints,
# foreign keys, and primary keys. Applied to every table via Base.metadata below.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPKMixin:
    """UUID primary key generated on the database side via pgcrypto's gen_random_uuid()."""

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """`created_at` + `updated_at` (both timezone-aware). Server-side defaults so
    manual INSERTs from psql also get them."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """`deleted_at` — set on soft-delete, NULL for live rows.
    Queries must filter `deleted_at IS NULL` explicitly (no global filter magic)."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

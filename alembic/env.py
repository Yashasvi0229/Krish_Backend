"""
Alembic migration environment.

This file is executed by `alembic upgrade` / `alembic revision` etc.
It reads the sync DB URL from our app settings and picks up all ORM
models registered in `app.models` so autogenerate works.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---- Load our app config + models ----
from app.config import settings
from app.models import Base  # noqa: F401 — populates Base.metadata

# Alembic Config object (from alembic.ini)
config = context.config

# Inject the sync DB URL at runtime (kept out of the .ini file to avoid secrets).
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# Set up loggers per alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---- Metadata for autogenerate ----
# IMPORTANT: as we add models in `app/models/`, import them in
# `app/models/__init__.py` so their tables end up on Base.metadata.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB (the normal `alembic upgrade head`)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

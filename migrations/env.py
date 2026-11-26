"""Alembic environment — reads DATABASE_URL from the app's own Settings
(never duplicated in alembic.ini), and runs every migration with the SAME
statement_timeout/connect_args the app itself uses, so a migration can never
silently hang longer than the app would tolerate.

This repo's tables are created at boot by `Database.ensure_tables()`
(app/db/database.py) with `IF NOT EXISTS` DDL — that stays in place; Alembic
is additive, for deliberate, reviewed schema changes going forward (starting
with the index migration in versions/). See
docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 2 for why the two coexist for
now rather than one replacing the other outright.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    # Local import: avoids alembic needing the full app import graph just to
    # read config, and keeps this file import-safe before `app` is on the path.
    from app.core.config import get_settings

    return get_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

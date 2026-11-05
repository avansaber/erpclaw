"""Alembic environment: core's revisions and each loaded plugin's branch.
Every table on the metadata is created by exactly
one revision of core or of its plugin."""
from __future__ import annotations

from alembic import context

from erpclaw_core import models  # noqa: F401  (registers the core tables)
from erpclaw_core.db import metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in metadata.tables
    return True


def run() -> None:
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, target_metadata=metadata, include_object=include_object,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run()

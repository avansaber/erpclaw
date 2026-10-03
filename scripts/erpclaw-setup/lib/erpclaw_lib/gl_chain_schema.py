"""GL chain-head schema (m332a).

Declares the per-company checksum-chain head ``gl_chain_head`` as SQLAlchemy
metadata and provisions it through ``erpclaw_lib.seam``. The head hands out
the ``gl_entry.sequence`` values ``insert_gl_entries`` stamps on each leg, so
both the chain build and the integrity walk order by an explicit sequence
rather than by write-time ties.

``company`` is declared through ``seam.reference_table`` for key resolution
only and is never created here. The ``gl_entry(sequence)`` index travels as a
portable string executed beside provisioning, not as metadata, because the
``gl_entry`` table belongs to another DDL block.
"""
import importlib.util
import os
import sys

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(os.path.expanduser(
        os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib.seam import (  # noqa: E402
    Column, ForeignKey, Integer, MetaData, Table, Text,
    reference_table,
)

METADATA = MetaData()

reference_table("company", METADATA)

GL_CHAIN_HEAD = Table(
    "gl_chain_head", METADATA,
    Column("company_id", Text, ForeignKey("company.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("last_sequence", Integer, nullable=False),
    Column("last_checksum", Text, nullable=False),
    Column("updated_at", Text),
)

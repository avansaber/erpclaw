"""the core tables create_all still made, by migration; item rebuilt

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
Data class: schema

Eight core tables were still made by `metadata.create_all` rather than by a
revision: plugin_install, approval, operation, number_series, audit_log,
outbox, item and erpclaw_books. This
revision creates each one that is absent, as defined here. Where older books
already have one (made by create_all), its columns (names, types, lengths,
nullability) and constraints (primary key, foreign keys, unique keys, and
each named check with its definition) must equal the ones create_all made,
or the upgrade refuses, naming the table and what differs. `item` is rebuilt to the core books rules:
a company key for composite references, its name unique per company, and a
rate that is canonical, positive and has at most four decimal places. No
downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

from erpclaw_core.db import rate_check

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def _item_constraints() -> list:
    return [sa.UniqueConstraint('company_id', 'id', name='uq_item_company_id'),
            sa.UniqueConstraint('company_id', 'name', name='uq_item_name'),
            sa.CheckConstraint(rate_check('standard_rate'), name='ck_item_rate')]


def _tables() -> dict[str, tuple]:
    """Each table's columns and constraints, frozen at this revision."""
    return {
        'erpclaw_books': (sa.Column('mark', sa.String(length=64), nullable=False),
                          sa.PrimaryKeyConstraint('mark')),
        'plugin_install': (
            sa.Column('name', sa.String(length=60), nullable=False),
            sa.Column('version', sa.String(length=20), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('installed_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('manifest_digest', sa.String(length=64), nullable=False),
            sa.Column('artifact_digest', sa.String(length=64), nullable=False),
            sa.Column('after_core_revision', sa.String(length=40), nullable=False),
            sa.Column('status', sa.String(length=12), nullable=False),
            sa.CheckConstraint("status in ('installed','disabled')", name='ck_plugin_install_status'),
            sa.PrimaryKeyConstraint('name')),
        'number_series': (
            sa.Column('company_id', sa.String(length=36), nullable=False),
            sa.Column('series', sa.String(length=20), nullable=False),
            sa.Column('fiscal_year', sa.Integer(), nullable=False),
            sa.Column('next_value', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['company.id']),
            sa.PrimaryKeyConstraint('company_id', 'series', 'fiscal_year')),
        'audit_log': (
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('company_id', sa.String(length=36), nullable=False),
            sa.Column('at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('actor', sa.String(length=100), nullable=False),
            sa.Column('action', sa.String(length=100), nullable=False),
            sa.Column('document_type', sa.String(length=40), nullable=True),
            sa.Column('document_id', sa.String(length=36), nullable=True),
            sa.Column('approval_id', sa.String(length=36), nullable=True),
            sa.Column('detail', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['company_id'], ['company.id']),
            sa.PrimaryKeyConstraint('id')),
        'approval': (
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('company_id', sa.String(length=36), nullable=False),
            sa.Column('conversation_id', sa.String(length=36), nullable=True),
            sa.Column('contract', sa.String(length=100), nullable=False),
            sa.Column('args_json', sa.Text(), nullable=False),
            sa.Column('digest', sa.String(length=64), nullable=False),
            sa.Column('card_json', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=12), nullable=False),
            sa.Column('operation_key', sa.String(length=36), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("status in ('awaiting','approved','rejected','expired','superseded','committed','failed')",
                               name='ck_approval_status'),
            sa.ForeignKeyConstraint(['company_id'], ['company.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('operation_key')),
        'operation': (
            sa.Column('key', sa.String(length=36), nullable=False),
            sa.Column('approval_id', sa.String(length=36), nullable=False),
            sa.Column('contract', sa.String(length=100), nullable=False),
            sa.Column('status', sa.String(length=12), nullable=False),
            sa.Column('result_json', sa.Text(), nullable=True),
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("status in ('started','committed','failed')", name='ck_operation_status'),
            sa.ForeignKeyConstraint(['approval_id'], ['approval.id']),
            sa.PrimaryKeyConstraint('key')),
        'outbox': (
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('company_id', sa.String(length=36), nullable=False),
            sa.Column('operation_key', sa.String(length=36), nullable=True),
            sa.Column('slot', sa.String(length=40), nullable=False),
            sa.Column('recipient', sa.String(length=200), nullable=False),
            sa.Column('subject', sa.String(length=300), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=12), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('detail', sa.Text(), nullable=True),
            sa.CheckConstraint("status in ('queued','delivered','failed')", name='ck_outbox_status'),
            sa.ForeignKeyConstraint(['company_id'], ['company.id']),
            sa.PrimaryKeyConstraint('id')),
        'item': (
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('company_id', sa.String(length=36), nullable=False),
            sa.Column('name', sa.String(length=200), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False),
            sa.Column('standard_rate', sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['company.id']),
            sa.PrimaryKeyConstraint('id'),
            *_item_constraints()),
    }


def _model_shape(name: str, parts: tuple) -> dict:
    if name == "item":
        parts = tuple(p for p in parts if not isinstance(p, (sa.UniqueConstraint, sa.CheckConstraint))) + (
            sa.UniqueConstraint('company_id', 'name'),)
    t = sa.Table(name, sa.MetaData(), *[p.copy() if isinstance(p, sa.Column) else p for p in parts])
    return {
        "columns": {c.name: (type(c.type.as_generic()).__name__, getattr(c.type, "length", None), c.nullable)
                    for c in t.columns},
        "primary key": sorted(c.name for c in t.primary_key.columns),
        "foreign keys": sorted((tuple(fk.parent.name for fk in k.elements), k.elements[0].target_fullname.split(".")[0],
                                tuple(fk.target_fullname.split(".")[1] for fk in k.elements))
                               for k in t.foreign_key_constraints),
        "unique keys": sorted(tuple(sorted(c.name for c in u.columns)) for u in t.constraints
                              if isinstance(u, sa.UniqueConstraint)),
        "checks": _rendered_checks(name, t),
    }


def _rendered_checks(name: str, t: sa.Table) -> list:
    """Each check's name and definition as this database renders it, from a scratch table built to the
    frozen definition and dropped again, so both sides are spelled the same way (names alone
    accepted a different predicate under the same name)."""
    bind = op.get_bind()
    scratch = sa.Table(f"erpclaw_shape_{name}", sa.MetaData(), *[c.copy() for c in t.columns],
                       *[sa.CheckConstraint(c.sqltext, name=c.name) for c in t.constraints
                         if isinstance(c, sa.CheckConstraint)])
    scratch.create(bind)
    try:
        return _checks_of(bind, scratch.name)
    finally:
        scratch.drop(bind)


def _checks_of(bind, name: str) -> list:
    return sorted((c["name"], " ".join(str(c["sqltext"]).split()))
                  for c in sa.inspect(bind).get_check_constraints(name) if c["name"])


def _books_shape(bind, name: str) -> dict:
    i = sa.inspect(bind)
    return {
        "columns": {c["name"]: (type(c["type"].as_generic()).__name__, getattr(c["type"], "length", None),
                                c["nullable"]) for c in i.get_columns(name)},
        "primary key": sorted(i.get_pk_constraint(name)["constrained_columns"]),
        "foreign keys": sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                               for f in i.get_foreign_keys(name)),
        "unique keys": sorted(tuple(sorted(u["column_names"])) for u in i.get_unique_constraints(name)),
        "checks": _checks_of(bind, name),
    }


def upgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name, parts in _tables().items():  # in foreign-key order
        if name not in present:
            op.create_table(name, *parts)
            continue
        want, have = _model_shape(name, parts), _books_shape(bind, name)
        differs = [k for k in want if want[k] != have[k]]
        if differs:
            raise RuntimeError(f"these books' {name} table was made by an earlier build and differs from the engine's "
                               f"in its {', '.join(differs)}; recreate the books")
        if name == "item":  # made by create_all: rebuilt with the company key, the named unique and the rate rule
            with op.batch_alter_table("item", recreate="always") as batch:
                batch.create_unique_constraint("uq_item_company_id", ["company_id", "id"])
                batch.create_check_constraint("ck_item_rate", rate_check("standard_rate"))


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

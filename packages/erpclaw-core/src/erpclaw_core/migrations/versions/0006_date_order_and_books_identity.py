"""the company date order and the books' identity

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29
Data class: schema and backfill

- `company.date_order`: how the owner writes figure dates, `MDY` (month
  first) or `DMY` (day first), or null when unset (a CHECK allows nothing
  else). Existing companies stay unset: the column is added with no value
  written into their rows.
- `books_identity`: one row holding the books' identity, a UUID written once.
  A trigger refuses every UPDATE, DELETE and TRUNCATE on it, so it never
  changes. Older books (an ownership mark or a company already in them) get
  theirs here, generated once; new books get theirs when they are
  initialised, and restored books get the one their backup carries.
No downgrade: it holds books.
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

UUID_SHAPE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column('company', sa.Column('date_order', sa.String(length=3), nullable=True))
    op.create_check_constraint('ck_company_date_order', 'company', "date_order in ('MDY','DMY')")

    op.create_table('books_identity',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('books_id', sa.String(length=36), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_books_identity_one_row'),
        sa.CheckConstraint(f"books_id ~ '{UUID_SHAPE}'", name='ck_books_identity_uuid'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute("CREATE FUNCTION erpclaw_refuse_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$ "
               "BEGIN RAISE EXCEPTION 'the books'' identity is written once: % refused', TG_OP; END $$")
    op.execute("CREATE TRIGGER books_identity_written_once BEFORE UPDATE OR DELETE ON books_identity "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_identity_change()")
    # TRUNCATE fires no row triggers, so it needs its own statement trigger.
    op.execute("CREATE TRIGGER books_identity_no_truncate BEFORE TRUNCATE ON books_identity "
               "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_refuse_identity_change()")

    # Older books (an ownership mark or a company already in them) get their identity here. A database being
    # initialised, or a restore's staging database, holds neither yet: it gets its identity from its
    # initialisation, or from its backup.
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM erpclaw_books) OR EXISTS (SELECT 1 FROM company)")).scalar():
        bind.execute(sa.text("INSERT INTO books_identity (id, books_id) VALUES (1, :b)"), {"b": str(uuid.uuid4())})


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

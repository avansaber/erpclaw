"""the file store: one row per stored file of a document; an outbox attachment names a stored file

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
Data class: schema

What it changes:
- `file_object`: a stored file's company, sha256, size, media type, the document it belongs to, when it was
  stored, its retention date (NULL: kept as long as the books) and its render inputs (canonical JSON text). The same
  bytes for the same document are one row.
- The database refuses every UPDATE and TRUNCATE of `file_object`, and a DELETE unless the row's retention date has
  passed (a UTC date); the same trigger writes the delete's audit row.
- `outbox_attachment.file_id` becomes NOT NULL, with a foreign key to `file_object` that refuses the file's removal.
  Attachments were refused until now, so the table is empty; the revision refuses if any row has no file.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

MAX_SIZE = 10 * 1024 * 1024  # written out, so a later change to the limit never changes this revision


def upgrade() -> None:
    op.create_table(
        'file_object',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('company_id', sa.String(length=36), sa.ForeignKey('company.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('media_type', sa.String(length=100), nullable=False),
        sa.Column('voucher_type', sa.String(length=40), nullable=False),
        sa.Column('voucher_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('retention_until', sa.Date(), nullable=True),
        sa.Column('render_inputs', sa.Text(), nullable=True),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_file_object_sha256'),
        sa.CheckConstraint(f"size > 0 AND size <= {MAX_SIZE}", name='ck_file_object_size'),
        sa.CheckConstraint("media_type = 'application/pdf'", name='ck_file_object_media_type'),
        # a cast of text that is not JSON raises, so the row is refused
        sa.CheckConstraint("render_inputs IS NULL OR render_inputs::json IS NOT NULL",
                           name='ck_file_object_render_inputs'),
        sa.UniqueConstraint('company_id', 'voucher_type', 'voucher_id', 'sha256', name='uq_file_object_document'),
    )
    op.create_index('ix_file_object_sha256', 'file_object', ['sha256', 'company_id'])
    op.execute("CREATE TRIGGER file_object_no_update BEFORE UPDATE ON file_object "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_change()")
    op.execute("CREATE TRIGGER file_object_no_truncate BEFORE TRUNCATE ON file_object "
               "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_refuse_change()")
    # A row kept as long as the books (NULL) or not yet past its date is never deleted, by any role; a delete that
    # passes writes its audit row in the same transaction. The date is compared in UTC, whatever the session's zone.
    op.execute(
        "CREATE OR REPLACE FUNCTION erpclaw_file_retire() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF OLD.retention_until IS NULL OR OLD.retention_until >= (now() AT TIME ZONE 'UTC')::date THEN "
        "RAISE EXCEPTION 'file_object %: a file is removed only after its retention date', OLD.id; END IF; "
        "INSERT INTO audit_log (id, company_id, at, actor, action, document_type, document_id, detail) VALUES "
        "(gen_random_uuid()::text, OLD.company_id, now(), current_user, 'file_retired', OLD.voucher_type, "
        "OLD.voucher_id, json_build_object('file_id', OLD.id, 'sha256', OLD.sha256, 'size', OLD.size)::text); "
        "RETURN OLD; END $$")
    op.execute("CREATE TRIGGER file_object_retire BEFORE DELETE ON file_object "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_file_retire()")
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM outbox_attachment WHERE file_id IS NULL) THEN "
               "RAISE EXCEPTION 'outbox_attachment holds a row with no file; it cannot be upgraded'; END IF; END $$")
    op.alter_column('outbox_attachment', 'file_id', existing_type=sa.String(length=36), nullable=False)
    op.create_foreign_key('fk_outbox_attachment_file', 'outbox_attachment', 'file_object', ['file_id'], ['id'],
                          ondelete='RESTRICT')


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

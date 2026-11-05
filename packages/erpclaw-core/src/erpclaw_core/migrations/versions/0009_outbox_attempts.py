"""the outbox sends at most once: statuses, the approved message, its attempts

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30
Data class: schema and backfill

What it changes:
- `ck_outbox_status`: queued, sending, accepted, unknown, failed. "delivered" is
  no longer written: an existing delivered row becomes accepted (delivery has
  been off since the planned switch, so real books hold none).
- `outbox` gains the approved message (message_id, from_address, from_name,
  reply_to) and its sending state (current_attempt_id, next_try_at, tries,
  first_failed_at, accepted_at).
- `outbox_attempt`: one row per try, with its lease, its data mark and its
  result.
- `outbox_attachment`: the attachments of a message, append-only.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

# The statuses and results as this revision makes them, written out so a later change to the models never changes it.
STATUSES = ("queued", "sending", "accepted", "unknown", "failed")
RESULTS = ("accepted", "temporary", "permanent", "no_answer", "lost_ownership", "abandoned")


def _quoted(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint('ck_outbox_status', 'outbox', type_='check')
    op.execute("UPDATE outbox SET status = 'accepted' WHERE status = 'delivered'")
    for name, type_ in (('message_id', sa.String(length=300)), ('from_address', sa.String(length=200)),
                        ('from_name', sa.String(length=200)), ('reply_to', sa.String(length=200)),
                        ('current_attempt_id', sa.String(length=36)), ('next_try_at', sa.DateTime(timezone=True)),
                        ('first_failed_at', sa.DateTime(timezone=True)), ('accepted_at', sa.DateTime(timezone=True))):
        op.add_column('outbox', sa.Column(name, type_, nullable=True))
    op.add_column('outbox', sa.Column('tries', sa.Integer(), nullable=False, server_default='0'))
    op.execute("UPDATE outbox SET accepted_at = delivered_at WHERE status = 'accepted'")
    op.create_check_constraint('ck_outbox_status', 'outbox', f"status in ({_quoted(STATUSES)})")
    op.create_table(
        'outbox_attempt',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('outbox_id', sa.String(length=36), sa.ForeignKey('outbox.id'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('code', sa.Integer(), nullable=True),
        sa.Column('result', sa.String(length=16), nullable=True),
        sa.Column('text', sa.String(length=200), nullable=True),
        sa.CheckConstraint(f"result is null or result in ({_quoted(RESULTS)})", name='ck_outbox_attempt_result'),
    )
    op.create_table(
        'outbox_attachment',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('outbox_id', sa.String(length=36), sa.ForeignKey('outbox.id'), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('media_type', sa.String(length=100), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('file_id', sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

"""the approval row: the conversation fence, one pending card per conversation, the stored decision

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29
Data class: schema and backfill

What it adds:
- `conversation_fence`: one row per conversation, its generation and live
  token, deciding which turn may make or approve a card.
- `approval.decision_json`: what ended a card without a commit, so a
  repeated approval returns it; `approval.request_card_token`: the turn's
  request that made the card.
- `uq_approval_one_pending` becomes one pending card per conversation,
  whatever its operation. On older books, a conversation holding more than
  one pending card keeps its newest; the others are superseded by this
  revision, each recorded in the audit log, so the index can be made.
  Older cards have no token and no decision, and behave as before.
No downgrade: it holds books.
"""
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

SET_ASIDE = "That card was replaced by a newer one; nothing was saved from it."
AUDIT_NS = uuid.UUID("6f1d3c2e-0007-4a5b-9c3d-000000000007")  # fixed, so the backfill is reproducible


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table('conversation_fence',
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('live_token', sa.String(length=64), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('generation >= 0', name='ck_conversation_fence_generation'),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.PrimaryKeyConstraint('conversation_id'),
    )
    op.add_column('approval', sa.Column('decision_json', sa.Text(), nullable=True))
    op.add_column('approval', sa.Column('request_card_token', sa.String(length=64), nullable=True))

    # Older books: all but each conversation's newest pending card are superseded, and audited.
    older = bind.execute(sa.text(
        "SELECT id, company_id, conversation_id, contract FROM ("
        " SELECT id, company_id, conversation_id, contract, row_number() OVER ("
        "  PARTITION BY conversation_id ORDER BY created_at DESC, id DESC) AS n"
        " FROM approval WHERE status = 'pending' AND conversation_id IS NOT NULL) ranked "
        "WHERE n > 1 ORDER BY id")).mappings().all()
    decision = json.dumps({"status": "superseded", "text": SET_ASIDE}, sort_keys=True, separators=(",", ":"))
    for a in older:
        bind.execute(sa.text("UPDATE approval SET status = 'superseded', decided_at = CURRENT_TIMESTAMP, "
                             "decision_json = :d WHERE id = :id AND status = 'pending'"), {"d": decision, "id": a["id"]})
        bind.execute(sa.text(
            "INSERT INTO audit_log (id, company_id, at, actor, action, document_type, document_id, approval_id, detail) "
            "VALUES (:id, :c, CURRENT_TIMESTAMP, 'revision 0007', 'card_set_aside', NULL, NULL, :a, :detail)"),
            {"id": str(uuid.uuid5(AUDIT_NS, a["id"])), "c": a["company_id"], "a": a["id"],
             "detail": json.dumps({"conversation_id": a["conversation_id"], "contract": a["contract"],
                                   "reason": "one pending card per conversation"}, sort_keys=True)})

    op.drop_index('uq_approval_one_pending', table_name='approval')
    op.create_index('uq_approval_one_pending', 'approval', ['conversation_id'], unique=True,
                    postgresql_where=sa.text("status = 'pending' AND conversation_id IS NOT NULL"))


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

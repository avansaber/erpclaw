"""principals and grants, the rebuilt approval card, operation outcomes

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
Data class: schema and backfill

Principals, approval cards and durable operation outcomes:
- `principal` and `principal_grant`: who may view, draft, approve and post,
  per company. Only a SHA-256 of a principal's API token is stored. Older
  books get the owner principal, with every permission on every company.
- `approval`, rebuilt: the card names its drafter, the drafter's grants
  version, the engine and plugin versions, the read set, a random nonce and
  a digest over all of them; statuses pending, approved, consumed, declined,
  expired, superseded, invalidated; at most one pending card per
  conversation and operation (a partial unique index). Older cards keep
  their history: awaiting becomes expired (it was made without a nonce or a
  read set, so it can never be approved now), committed and failed become
  consumed, rejected becomes declined.
- `operation_outcome` replaces `operation`: written in the same transaction
  as the effect, committed or refused. An older committed operation becomes
  a committed outcome, a failed one a refused outcome; a started one with
  no result had no effect, so it has no outcome.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

PERMISSIONS = ('view', 'draft', 'approve', 'post', 'reverse', 'close_period', 'export', 'administer_users',
               'install_plugins', 'manage_secrets')
CARD_STATUS = ('pending', 'approved', 'consumed', 'declined', 'expired', 'superseded', 'invalidated')
OLD_STATUS = {'awaiting': 'expired', 'approved': 'expired', 'committed': 'consumed', 'failed': 'consumed',
              'rejected': 'declined', 'expired': 'expired', 'superseded': 'superseded'}


def _in(column: str, values: tuple) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table('principal',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('disabled', sa.Boolean(), nullable=False),
        sa.Column('grants_version', sa.Integer(), nullable=False),
        sa.Column('token_sha256', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind in ('person')", name='ck_principal_kind'),
        sa.CheckConstraint('grants_version >= 1', name='ck_principal_grants_version'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_principal_name'),
        sa.UniqueConstraint('token_sha256', name='uq_principal_token'),
    )
    op.create_table('principal_grant',
        sa.Column('principal_id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('permission', sa.String(length=20), nullable=False),
        sa.CheckConstraint(_in('permission', PERMISSIONS), name='ck_principal_grant_permission'),
        sa.ForeignKeyConstraint(['principal_id'], ['principal.id']),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.PrimaryKeyConstraint('principal_id', 'company_id', 'permission'),
    )
    op.create_table('approval_r5',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('principal_id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('contract', sa.String(length=100), nullable=False),
        sa.Column('contract_version', sa.String(length=20), nullable=False),
        sa.Column('policy_version', sa.Integer(), nullable=False),
        sa.Column('impl_version', sa.String(length=120), nullable=False),
        sa.Column('args_json', sa.Text(), nullable=False),
        sa.Column('read_set_json', sa.Text(), nullable=False),
        sa.Column('card_json', sa.Text(), nullable=False),
        sa.Column('nonce', sa.String(length=32), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('operation_key', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', sa.String(length=36), nullable=True),
        sa.Column('approver_grants_version', sa.Integer(), nullable=True),
        sa.CheckConstraint(_in('status', CARD_STATUS), name='ck_approval_status'),
        sa.ForeignKeyConstraint(['principal_id'], ['principal.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['principal.id']),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('operation_key', name='uq_approval_operation_key'),
    )
    op.create_table('operation_outcome',
        sa.Column('key', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('approval_id', sa.String(length=36), nullable=False),
        sa.Column('card_digest', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('committed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('committed','refused')", name='ck_operation_outcome_status'),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.ForeignKeyConstraint(['approval_id'], ['approval_r5.id']),
        sa.PrimaryKeyConstraint('key'),
    )

    # Older books: the owner principal with every permission on every company, then their cards and outcomes.
    companies = list(bind.execute(sa.text("SELECT id FROM company")).scalars())
    if companies or bind.execute(sa.text("SELECT count(*) FROM approval")).scalar_one():
        owner = "00000000-0000-7000-8000-000000000001"  # fixed, so the backfill is reproducible
        bind.execute(sa.text("INSERT INTO principal (id, name, kind, disabled, grants_version, token_sha256, created_at) "
                             "VALUES (:id, 'owner', 'person', :f, 1, NULL, CURRENT_TIMESTAMP)"), {"id": owner, "f": False})
        for cid in companies:
            for perm in PERMISSIONS:
                bind.execute(sa.text("INSERT INTO principal_grant VALUES (:p, :c, :perm)"),
                             {"p": owner, "c": cid, "perm": perm})
        cases = " ".join(f"WHEN '{a}' THEN '{b}'" for a, b in OLD_STATUS.items())
        bind.execute(sa.text(
            "INSERT INTO approval_r5 (id, principal_id, company_id, conversation_id, contract, contract_version, "
            "policy_version, impl_version, args_json, read_set_json, card_json, nonce, digest, status, operation_key, "
            "created_at, expires_at, decided_at) "
            f"SELECT id, :owner, company_id, conversation_id, contract, 'pre-0005', 1, 'pre-0005', args_json, '[]', "
            f"card_json, '', digest, CASE status {cases} ELSE 'expired' END, operation_key, created_at, expires_at, "
            "decided_at FROM approval"), {"owner": owner})
        bind.execute(sa.text(
            "INSERT INTO operation_outcome (key, company_id, approval_id, card_digest, status, result_json, committed_at) "
            "SELECT o.key, a.company_id, o.approval_id, a.digest, "
            "CASE o.status WHEN 'committed' THEN 'committed' ELSE 'refused' END, COALESCE(o.result_json, '{}'), "
            "COALESCE(o.finished_at, o.started_at) FROM operation o JOIN approval a ON a.id = o.approval_id "
            "WHERE o.status IN ('committed', 'failed')"))
    op.drop_table('operation')
    op.drop_table('approval')
    op.execute("ALTER TABLE approval_r5 RENAME TO approval")
    for old, new in (("approval_r5_pkey", "approval_pkey"),):
        op.execute(f"ALTER TABLE approval RENAME CONSTRAINT {old} TO {new}")
    # one pending card per conversation and operation: an edit supersedes the card it replaces
    op.create_index('uq_approval_one_pending', 'approval', ['conversation_id', 'contract'], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

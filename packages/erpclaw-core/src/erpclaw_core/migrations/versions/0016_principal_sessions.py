"""Hash-only sign-in codes and principal sessions, retained as books history.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-07
Data class: schema

What it changes:
- Creates principal_sign_in and principal_session, their principal foreign keys,
  unique keys, expiry and session-end CHECKs.
- Only used_at changes on a code; only last_seen_at, idle_expires_at, ended_at
  and ended_why change on a session. DELETE and TRUNCATE are refused for both.
- Primary and unique keys are ordinary, non-deferrable constraints.
  PostgreSQL enforces all constraints directly; its errors may include row values.
- Consumption and ending go forward only: once set, used_at, ended_at and ended_why cannot be cleared or changed
  by any role (two plain trigger functions with fixed column names).
  No existing table or trigger function changes.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'principal_sign_in',
        sa.Column('code_sha256', sa.String(length=64), nullable=False),
        sa.Column('principal_id', sa.String(length=36), sa.ForeignKey('principal.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=100), nullable=False),
        sa.Column('run_id', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('code_sha256', name='pk_principal_sign_in'),
        sa.CheckConstraint('expires_at > created_at', name='ck_principal_sign_in_expiry'),
    )
    op.create_table(
        'principal_session',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('secret_sha256', sa.String(length=64), nullable=False),
        sa.Column('principal_id', sa.String(length=36), sa.ForeignKey('principal.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('idle_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_why', sa.String(length=20), nullable=True),
        sa.Column('user_agent', sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_principal_session'),
        sa.UniqueConstraint('secret_sha256', name='uq_principal_session_secret'),
        sa.CheckConstraint("ended_why is null or ended_why in "
                           "('sign_out','expired','disabled','restored','engine_start','sign_out_all')",
                           name='ck_principal_session_end_reason'),
        sa.CheckConstraint('(ended_at is null) = (ended_why is null)', name='ck_principal_session_end_pair'),
        sa.CheckConstraint('idle_expires_at <= expires_at', name='ck_principal_session_idle'),
    )

    # Consumption and ending go forward only: once set, used_at, ended_at and ended_why can never be cleared or
    # rewritten, by any role. Plain functions with fixed column names; writing the same value again is allowed.
    op.execute("""
        CREATE FUNCTION erpclaw_sign_in_stays_used() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
                RAISE EXCEPTION 'principal_sign_in: a used code stays used';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION erpclaw_session_stays_ended() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF (OLD.ended_at IS NOT NULL OR OLD.ended_why IS NOT NULL)
               AND (NEW.ended_at IS DISTINCT FROM OLD.ended_at OR NEW.ended_why IS DISTINCT FROM OLD.ended_why) THEN
                RAISE EXCEPTION 'principal_session: an ended session stays ended';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER principal_sign_in_stays_used BEFORE UPDATE ON principal_sign_in "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_sign_in_stays_used()")
    op.execute("CREATE TRIGGER principal_session_stays_ended BEFORE UPDATE ON principal_session "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_session_stays_ended()")
    for table, columns in (
        ('principal_sign_in', "'used_at'"),
        ('principal_session', "'last_seen_at', 'idle_expires_at', 'ended_at', 'ended_why'"),
    ):
        op.execute(f"CREATE TRIGGER {table}_columns BEFORE UPDATE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION erpclaw_only_columns({columns})")
        op.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_change()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_refuse_change()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

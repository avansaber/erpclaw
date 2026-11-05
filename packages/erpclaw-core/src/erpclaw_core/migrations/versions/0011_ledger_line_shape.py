"""Ledger line shape and reserved dimension set.

Revision ID: 0011
Revises: 0010
Data class: schema; old books are refused, never rewritten.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _scale(column):
    # Frozen from the currency table at this revision, independent of future model changes.
    pos = f"strpos({column}, '.')"
    shape = f"{column} ~ '^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$' AND {column} !~ '^-0(\\.0+)?$'"
    cases = " ".join(f"WHEN '{cur}' THEN {pos} = 0" if exp == 0 else
                     f"WHEN '{cur}' THEN {pos} > 0 AND {pos} = length({column}) - {exp}"
                     for cur, exp in (("EUR", 2), ("GBP", 2), ("INR", 2), ("JPY", 0), ("KWD", 3), ("USD", 2)))
    return f"({shape}) AND (CASE functional_currency {cases} ELSE false END)"


def upgrade():
    # A snapshot established before the lock at REPEATABLE READ or SERIALIZABLE
    # can miss a writer that commits while ACCESS EXCLUSIVE is being acquired.
    # Fail closed before any migration changes; READ COMMITTED takes a fresh
    # snapshot for the emptiness check after the lock has been acquired.
    op.execute("DO $$ BEGIN IF current_setting('transaction_isolation') <> 'read committed' "
               "THEN RAISE EXCEPTION 'ledger format 2 migration requires READ COMMITTED isolation'; "
               "END IF; END $$")
    # Serialize the empty-books check against writers; failure is transactional.
    op.execute("LOCK TABLE company, posting, ledger_entry, erpclaw_books, books_identity IN ACCESS EXCLUSIVE MODE")
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM company) OR EXISTS (SELECT 1 FROM posting) "
               "OR EXISTS (SELECT 1 FROM erpclaw_books) OR EXISTS (SELECT 1 FROM books_identity) "
               "THEN RAISE EXCEPTION 'older books are disposable and cannot be upgraded to ledger format 2'; "
               "END IF; END $$")
    op.create_unique_constraint("uq_company_functional_currency", "company", ["id", "functional_currency"])
    op.create_table("dimension_set",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("company.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "id", name="uq_dimension_set_company_id"),
        sa.UniqueConstraint("company_id", "digest", name="uq_dimension_set_digest"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_dimension_set_digest"),
        sa.CheckConstraint("length(content) > 1", name="ck_dimension_set_content"))
    op.execute("CREATE TRIGGER dimension_set_append_only BEFORE UPDATE OR DELETE ON dimension_set "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_change()")
    op.execute("CREATE TRIGGER dimension_set_no_truncate BEFORE TRUNCATE ON dimension_set "
               "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_refuse_change()")
    op.add_column("posting", sa.Column("hash_format", sa.Integer(), nullable=False))
    op.create_check_constraint("ck_posting_hash_format", "posting", "hash_format = 2")
    for name in ("rate", "functional_debit", "functional_credit"):
        op.add_column("ledger_entry", sa.Column(name, sa.String(40), nullable=(name == "rate")))
    op.add_column("ledger_entry", sa.Column("functional_currency", sa.String(3), nullable=False))
    op.add_column("ledger_entry", sa.Column("kind", sa.String(10), nullable=False))
    op.add_column("ledger_entry", sa.Column("dimension_set_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_entry_functional_currency", "ledger_entry", "company",
                          ["company_id", "functional_currency"], ["id", "functional_currency"])
    op.create_foreign_key("fk_entry_dimension_set", "ledger_entry", "dimension_set",
                          ["company_id", "dimension_set_id"], ["company_id", "id"])
    checks = {
        "ck_entry_no_dimension_v30": "dimension_set_id IS NULL",
        "ck_entry_kind": "kind IN ('measured','clearing','adjustment')",
        "ck_entry_rate": "(kind = 'adjustment' AND rate IS NULL) OR (kind <> 'adjustment' AND rate IS NOT NULL AND "
                         "rate ~ '^(0|[1-9][0-9]*)(\\.[0-9]{0,8}[1-9])?$' AND rate ~ '[1-9]')",
        "ck_entry_functional_v30": "currency = functional_currency AND rate IS NOT NULL AND rate ~ '^1(\\.0+)?$' AND "
                                   "kind = 'measured' AND functional_debit = debit AND functional_credit = credit",
        "ck_entry_functional_debit_scale": _scale("functional_debit"),
        "ck_entry_functional_credit_scale": _scale("functional_credit"),
    }
    for name, expression in checks.items():
        op.create_check_constraint(name, "ledger_entry", expression)


def downgrade():
    raise NotImplementedError("revisions that hold books do not downgrade")

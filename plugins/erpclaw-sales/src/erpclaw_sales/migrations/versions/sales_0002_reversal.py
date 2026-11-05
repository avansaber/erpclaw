"""reversal: an invoice may owe money again only when its rows say so, and a cancellation is a reversal

Revision ID: sales_0002
Revises: sales_0001
Create Date: 2026-10-28
Data class: schema

PostgreSQL only (the books run on PostgreSQL). An invoice's status follows its
outstanding amount, as state checks that hold for restored rows too. Status may
move back among submitted, partly paid and paid when a payment is reversed, but
never back to a draft and never out of cancelled. Outstanding must equal the
total less the allocations of payments still standing, so no writer moves it
without the rows that justify it. A submitted invoice is cancelled, and a
payment is marked reversed, only once a posting reversing its own posting
exists. A payment gains a cancel key, which changes only with that move.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = 'sales_0002'
down_revision = 'sales_0001'
branch_labels = None
depends_on = None
creates = ()  # columns, checks and trigger rules only

INVOICE_FIXED = ("number", "customer_id", "posting_date", "due_date", "currency", "total", "operation_key",
                 "submit_operation_key", "created_at")
PAYMENT_FIXED = ("number", "customer_id", "posting_date", "currency", "amount", "unallocated", "method",
                 "reference", "operation_key", "created_at")
OPEN = "('submitted', 'partly_paid', 'paid')"
# the allocations of payments still standing, for one invoice
LIVE = ("COALESCE((SELECT sum(a.amount::numeric) FROM sales_payment_allocation a JOIN sales_payment p "
        "ON p.company_id = a.company_id AND p.id = a.payment_id WHERE a.company_id = NEW.company_id "
        "AND a.invoice_id = NEW.id AND p.status = 'submitted'), 0)")


def _reversed(voucher_type: str) -> str:
    return ("EXISTS (SELECT 1 FROM posting o JOIN posting r ON r.company_id = o.company_id "
            "AND r.reverses_posting_id = o.id WHERE o.company_id = NEW.company_id "
            f"AND o.voucher_type = '{voucher_type}' AND o.voucher_id = NEW.id)")


def _changed(columns) -> str:
    return " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in columns)


STATE_CHECKS = {
    "ck_sales_invoice_paid_owes_nothing": "status <> 'paid' OR outstanding::numeric = 0",
    "ck_sales_invoice_partly_paid_between": ("status <> 'partly_paid' OR (outstanding::numeric > 0 "
                                             "AND outstanding::numeric < total::numeric)"),
    "ck_sales_invoice_submitted_owes_total": "status <> 'submitted' OR outstanding::numeric = total::numeric",
    "ck_sales_invoice_cancelled_owes_nothing": "status <> 'cancelled' OR outstanding::numeric = 0",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    # SECURITY DEFINER: the rules read allocations, payments and postings, which the runtime role may not read in
    # full; the functions run as the owner with the books' schema pinned, so no session can stand in a table.
    path = f"SET search_path = {schema}, pg_temp"
    for name, check in STATE_CHECKS.items():
        op.create_check_constraint(name, "sales_invoice", check)
    op.add_column("sales_payment", sa.Column("cancel_operation_key", sa.String(length=36), nullable=True))
    op.create_unique_constraint("uq_sales_payment_cancel_key", "sales_payment", ["cancel_operation_key"])
    transitions = ("NEW.status = OLD.status OR (OLD.status = 'draft' AND NEW.status IN ('submitted', 'cancelled')) "
                   f"OR (OLD.status IN {OPEN} AND NEW.status IN {OPEN}) "
                   "OR (OLD.status = 'submitted' AND NEW.status = 'cancelled')")
    op.execute(f"""CREATE OR REPLACE FUNCTION erpclaw_sales_invoice_update() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER {path}
AS $$ BEGIN
  IF NOT ({transitions}) OR {_changed(('id', 'company_id'))}
     OR (OLD.status <> 'draft' AND ({_changed(INVOICE_FIXED)}))
     OR (OLD.cancel_operation_key IS NOT NULL AND NEW.cancel_operation_key IS DISTINCT FROM OLD.cancel_operation_key)
  THEN RAISE EXCEPTION 'sales_invoice: that change is refused once it is submitted; status only moves forward, except back among submitted, partly paid and paid when a payment is reversed, and never to a draft';
  END IF;
  IF NEW.status IN {OPEN} AND NEW.outstanding::numeric <> NEW.total::numeric - {LIVE} THEN
    RAISE EXCEPTION 'sales_invoice: outstanding must be the total less the allocations of payments still standing';
  END IF;
  IF NEW.status = 'cancelled' AND OLD.status = 'submitted' AND ({LIVE} <> 0 OR NOT {_reversed('sales_invoice')}) THEN
    RAISE EXCEPTION 'sales_invoice: a submitted invoice is cancelled only with no payment standing and its posting reversed';
  END IF;
  RETURN NEW;
END $$""")
    op.execute(f"""CREATE OR REPLACE FUNCTION erpclaw_sales_payment_update() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER {path}
AS $$ BEGIN
  IF NOT (NEW.status = OLD.status OR (OLD.status = 'submitted' AND NEW.status = 'cancelled'))
     OR {_changed(('id', 'company_id') + PAYMENT_FIXED)}
     OR (NEW.cancel_operation_key IS DISTINCT FROM OLD.cancel_operation_key AND NEW.status = OLD.status)
  THEN RAISE EXCEPTION 'sales_payment: that change is refused, and status only moves forward';
  END IF;
  IF NEW.status = 'cancelled' AND OLD.status = 'submitted' AND NOT {_reversed('sales_payment')} THEN
    RAISE EXCEPTION 'sales_payment: a payment is marked reversed only once its posting is reversed';
  END IF;
  RETURN NEW;
END $$""")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

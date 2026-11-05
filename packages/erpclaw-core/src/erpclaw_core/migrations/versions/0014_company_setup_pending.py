"""a company pending its setup, and its setup fixed once it is ready

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02
Data class: schema and backfill

What it changes:
- `company.status`: `pending` (made by `init --own-books`, its business choices not made yet) or `ready` (a CHECK
  allows nothing else). Existing companies become `ready`: the column is added with that server default.
- `company.name`, `company.functional_currency` and `company.fiscal_year_start_month` become nullable, and a CHECK
  allows null in them only while the company is pending.
- A trigger refuses a change of the name, currency, fiscal year start, date order or status of a ready company by any
  role without the owner's authority (one that is not a member of the role owning `company`): the runtime role,
  which may update those columns of a pending company through the setup card. The owner's own updates (the audited
  `company set --date-order`, the demo's init) go through as before.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

# Written out, so a later change to the models or the wording never changes this revision.
READY_CHECK = ("status = 'pending' or (name is not null and functional_currency is not null "
               "and fiscal_year_start_month is not null)")
FIXED = "The company''s name, currency, fiscal year start and date order are fixed once the books are ready."


def upgrade() -> None:
    op.add_column('company', sa.Column('status', sa.String(length=10), nullable=False, server_default='ready'))
    op.create_check_constraint('ck_company_status', 'company', "status in ('pending','ready')")
    op.alter_column('company', 'name', existing_type=sa.String(length=200), nullable=True)
    op.alter_column('company', 'functional_currency', existing_type=sa.String(length=3), nullable=True)
    op.alter_column('company', 'fiscal_year_start_month', existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint('ck_company_ready', 'company', READY_CHECK)
    # The owner's authority is membership in the role that owns the table (a superuser counts); the runtime role
    # belongs to no role (grants.verify_grants), so its change of a ready company is refused.
    op.execute(
        "CREATE FUNCTION erpclaw_company_fixed_once_ready() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF OLD.status = 'ready' "
        "AND (NEW.name, NEW.functional_currency, NEW.fiscal_year_start_month, NEW.date_order, NEW.status) "
        "IS DISTINCT FROM (OLD.name, OLD.functional_currency, OLD.fiscal_year_start_month, OLD.date_order, "
        "OLD.status) "
        "AND NOT pg_has_role(current_user, (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID), 'MEMBER') "
        f"THEN RAISE EXCEPTION '{FIXED}'; END IF; RETURN NEW; END $$")
    op.execute("CREATE TRIGGER company_fixed_once_ready BEFORE UPDATE ON company "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_company_fixed_once_ready()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

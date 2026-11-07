"""Stock-owned goods-received header.

Revision ID: stock_0003
Revises: stock_0002
Data class: schema
Frozen literal DDL; the ordered receipt lines remain stock ledger entries.
"""
from alembic import op
import sqlalchemy as sa

revision = "stock_0003"
down_revision = "stock_0002"
branch_labels = None
depends_on = None
creates = ("stock_goods_received",)

DDL = """CREATE TABLE stock_goods_received (
    id VARCHAR(36) NOT NULL,
    company_id VARCHAR(36) NOT NULL,
    voucher_id VARCHAR(36) NOT NULL,
    supplier_id VARCHAR(36) NOT NULL,
    settlement VARCHAR(12) NOT NULL,
    account_id VARCHAR(36) NOT NULL,
    number VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_stock_goods_received_voucher FOREIGN KEY(company_id, voucher_id) REFERENCES stock_voucher (company_id, id),
    CONSTRAINT fk_stock_goods_received_supplier FOREIGN KEY(company_id, supplier_id) REFERENCES party (company_id, id),
    CONSTRAINT fk_stock_goods_received_account FOREIGN KEY(company_id, account_id) REFERENCES account (company_id, id),
    CONSTRAINT uq_stock_goods_received_company_id UNIQUE (company_id, id),
    CONSTRAINT uq_stock_goods_received_voucher UNIQUE (company_id, voucher_id),
    CONSTRAINT uq_stock_goods_received_number UNIQUE (company_id, number),
    CONSTRAINT ck_stock_goods_received_settlement CHECK (settlement in ('paid_now','still_owed'))
)"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    op.execute(DDL)
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    op.execute(f"CREATE TRIGGER stock_goods_received_no_change BEFORE UPDATE OR DELETE "
               f"ON {schema}.stock_goods_received FOR EACH ROW EXECUTE FUNCTION {schema}.erpclaw_stock_refuse()")
    op.execute(f"CREATE TRIGGER stock_goods_received_no_truncate BEFORE TRUNCATE "
               f"ON {schema}.stock_goods_received FOR EACH STATEMENT EXECUTE FUNCTION {schema}.erpclaw_stock_refuse()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

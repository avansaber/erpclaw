"""Stock-owned voucher storage; opening producer first.

Revision ID: stock_0002
Revises: stock_0001
Data class: schema

Frozen table definitions: no current-model imports. Core0015 is required by the
manifest, not an Alembic dependency. Both tables are company-keyed and append-only.
Count-only nullable columns reserve storage, not count behavior.
"""
from alembic import op
import sqlalchemy as sa

revision = "stock_0002"
down_revision = "stock_0001"
branch_labels = None
depends_on = None
creates = ("stock_voucher", "stock_voucher_line")

DDL = (
    r"""CREATE TABLE stock_voucher (
	id VARCHAR(36) NOT NULL,
	company_id VARCHAR(36) NOT NULL,
	kind VARCHAR(20) NOT NULL,
	number VARCHAR(40) NOT NULL,
	posting_date DATE NOT NULL,
	reason VARCHAR(140),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_stock_voucher_company_id UNIQUE (company_id, id),
	CONSTRAINT uq_stock_voucher_number UNIQUE (company_id, number),
	CONSTRAINT ck_stock_voucher_kind CHECK (kind in ('opening','count','write_off','goods_received')),
	FOREIGN KEY(company_id) REFERENCES company (id)
)""",
    r"""CREATE TABLE stock_voucher_line (
	id VARCHAR(36) NOT NULL,
	company_id VARCHAR(36) NOT NULL,
	voucher_id VARCHAR(36) NOT NULL,
	line_no INTEGER NOT NULL,
	item_id VARCHAR(36) NOT NULL,
	warehouse_id VARCHAR(36) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	qty_moved VARCHAR(40) NOT NULL,
	counted_qty VARCHAR(40),
	books_qty VARCHAR(40),
	rate VARCHAR(40),
	direction VARCHAR(3) NOT NULL,
	value VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_stock_voucher_line_voucher FOREIGN KEY(company_id, voucher_id) REFERENCES stock_voucher (company_id, id),
	CONSTRAINT fk_stock_voucher_line_item FOREIGN KEY(company_id, item_id) REFERENCES item (company_id, id),
	CONSTRAINT fk_stock_voucher_line_warehouse FOREIGN KEY(company_id, warehouse_id) REFERENCES stock_warehouse (company_id, id),
	CONSTRAINT uq_stock_voucher_line_company_id UNIQUE (company_id, id),
	CONSTRAINT uq_stock_voucher_line_number UNIQUE (voucher_id, line_no),
	CONSTRAINT uq_stock_voucher_line_pair UNIQUE (voucher_id, item_id, warehouse_id),
	CONSTRAINT ck_stock_voucher_line_number CHECK (line_no > 0),
	CONSTRAINT ck_stock_voucher_line_direction CHECK (direction in ('in','out')),
	CONSTRAINT ck_stock_voucher_line_qty CHECK (qty_moved ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'),
	CONSTRAINT ck_stock_voucher_line_counted_qty CHECK (counted_qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'),
	CONSTRAINT ck_stock_voucher_line_books_qty CHECK (books_qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'),
	CONSTRAINT ck_stock_voucher_line_rate_positive CHECK (rate ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,4})?$' AND rate ~ '[1-9]'),
	CONSTRAINT ck_stock_voucher_line_rate_canonical CHECK (rate ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'),
	CONSTRAINT ck_stock_voucher_line_value_scale CHECK ((value ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'GBP' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'INR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'JPY' THEN strpos(value, '.') = 0 WHEN 'KWD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 3 WHEN 'USD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 ELSE false END)),
	CONSTRAINT ck_stock_voucher_line_value_sign CHECK (NOT (value ~ '^-')),
	FOREIGN KEY(currency) REFERENCES currency (code)
)""",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    for statement in DDL:
        op.execute(statement)
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    # Reuse stock_0001's pinned refusal function, explicitly in the books schema.
    for table in creates:
        op.execute(f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {schema}.{table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {schema}.erpclaw_stock_refuse()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {schema}.{table} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.erpclaw_stock_refuse()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

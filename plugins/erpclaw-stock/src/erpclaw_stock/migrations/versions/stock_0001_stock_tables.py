"""the stock tables, to the core books rules, and the triggers that keep the stock ledger append only

Revision ID: stock_0001
Revises: (base of the stock branch)
Create Date: 2026-10-30
Data class: schema

The base of the stock plugin's branch: its own base, no depends_on; the engine checks that the books' core revision
is at or after the manifest's (0008, which gives the chart its stock roles). Composite company keys on every
reference, values at the company currency's scale, quantities canonical with at most four places. The statements are
the tables as this revision made them, frozen here, so a later change to `tables.py` is a new revision. Triggers, the
second layer behind the runtime role's privileges (the books run on PostgreSQL only):
- the stock ledger, its FIFO layers and their takes are never updated or deleted (a reversal writes mirror rows);
- warehouses and stock items are never updated or deleted (insert only);
- a head is updated, never deleted;
- TRUNCATE is refused, and the trigger function resolves tables in the books' schema only.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = 'stock_0001'
down_revision = None
branch_labels = ('stock',)
depends_on = None
creates = ('stock_warehouse', 'stock_item', 'stock_head', 'stock_ledger_entry', 'stock_layer', 'stock_layer_take')

TABLES = creates
NO_CHANGE = ('stock_warehouse', 'stock_item', 'stock_ledger_entry', 'stock_layer', 'stock_layer_take')

DDL = (
    r"""CREATE TABLE stock_warehouse (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    name VARCHAR(140) NOT NULL, 
    account_id VARCHAR(36) NOT NULL, 
    is_default BOOLEAN NOT NULL, 
    position INTEGER NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_warehouse_account FOREIGN KEY(company_id, account_id) REFERENCES account (company_id, id), 
    CONSTRAINT uq_stock_warehouse_company_id UNIQUE (company_id, id), 
    CONSTRAINT uq_stock_warehouse_name UNIQUE (company_id, name), 
    CONSTRAINT uq_stock_warehouse_position UNIQUE (company_id, position), 
    CONSTRAINT ck_stock_warehouse_position CHECK (position > 0), 
    FOREIGN KEY(company_id) REFERENCES company (id)
)""",
    r"""CREATE UNIQUE INDEX uq_stock_warehouse_default ON stock_warehouse (company_id) WHERE is_default""",
    r"""CREATE TABLE stock_item (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    item_id VARCHAR(36) NOT NULL, 
    method VARCHAR(14) NOT NULL, 
    default_warehouse_id VARCHAR(36), 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_item_item FOREIGN KEY(company_id, item_id) REFERENCES item (company_id, id), 
    CONSTRAINT fk_stock_item_default_warehouse FOREIGN KEY(company_id, default_warehouse_id) REFERENCES stock_warehouse (company_id, id), 
    CONSTRAINT uq_stock_item_company_id UNIQUE (company_id, id), 
    CONSTRAINT uq_stock_item_item UNIQUE (company_id, item_id), 
    CONSTRAINT ck_stock_item_method CHECK (method in ('fifo', 'moving_average')), 
    FOREIGN KEY(company_id) REFERENCES company (id)
)""",
    r"""CREATE TABLE stock_head (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    item_id VARCHAR(36) NOT NULL, 
    warehouse_id VARCHAR(36) NOT NULL, 
    currency VARCHAR(3) NOT NULL, 
    last_seq INTEGER NOT NULL, 
    qty VARCHAR(40) NOT NULL, 
    value VARCHAR(40) NOT NULL, 
    last_date DATE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_head_item FOREIGN KEY(company_id, item_id) REFERENCES item (company_id, id), 
    CONSTRAINT fk_stock_head_warehouse FOREIGN KEY(company_id, warehouse_id) REFERENCES stock_warehouse (company_id, id), 
    CONSTRAINT uq_stock_head_company_id UNIQUE (company_id, id), 
    CONSTRAINT uq_stock_head_pair UNIQUE (company_id, item_id, warehouse_id), 
    CONSTRAINT ck_stock_head_seq CHECK (last_seq > 0), 
    CONSTRAINT ck_stock_head_qty CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'), 
    CONSTRAINT ck_stock_head_value_scale CHECK ((value ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'GBP' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'INR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'JPY' THEN strpos(value, '.') = 0 WHEN 'KWD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 3 WHEN 'USD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 ELSE false END)), 
    CONSTRAINT ck_stock_head_value_sign CHECK (NOT (value ~ '^-')), 
    CONSTRAINT ck_stock_head_nothing_left_is_worth_nothing CHECK (qty::numeric > 0 OR value::numeric = 0), 
    FOREIGN KEY(currency) REFERENCES currency (code)
)""",
    r"""CREATE TABLE stock_ledger_entry (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    item_id VARCHAR(36) NOT NULL, 
    warehouse_id VARCHAR(36) NOT NULL, 
    currency VARCHAR(3) NOT NULL, 
    posting_date DATE NOT NULL, 
    seq INTEGER NOT NULL, 
    direction VARCHAR(3) NOT NULL, 
    qty VARCHAR(40) NOT NULL, 
    value VARCHAR(40) NOT NULL, 
    qty_after VARCHAR(40) NOT NULL, 
    value_after VARCHAR(40) NOT NULL, 
    voucher_type VARCHAR(40) NOT NULL, 
    voucher_id VARCHAR(36) NOT NULL, 
    line_no INTEGER NOT NULL, 
    against_entry_id VARCHAR(36), 
    reverses_entry_id VARCHAR(36), 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_entry_item FOREIGN KEY(company_id, item_id) REFERENCES item (company_id, id), 
    CONSTRAINT fk_stock_entry_warehouse FOREIGN KEY(company_id, warehouse_id) REFERENCES stock_warehouse (company_id, id), 
    CONSTRAINT fk_stock_entry_against FOREIGN KEY(company_id, against_entry_id) REFERENCES stock_ledger_entry (company_id, id), 
    CONSTRAINT fk_stock_entry_reverses FOREIGN KEY(company_id, reverses_entry_id) REFERENCES stock_ledger_entry (company_id, id), 
    CONSTRAINT uq_stock_entry_company_id UNIQUE (company_id, id), 
    CONSTRAINT uq_stock_entry_seq UNIQUE (company_id, item_id, warehouse_id, seq), 
    CONSTRAINT uq_stock_entry_reversed_once UNIQUE (reverses_entry_id), 
    CONSTRAINT ck_stock_entry_seq CHECK (seq > 0), 
    CONSTRAINT ck_stock_entry_line CHECK (line_no > 0), 
    CONSTRAINT ck_stock_entry_direction CHECK (direction in ('in','out')), 
    CONSTRAINT ck_stock_ledger_entry_qty_positive CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,4})?$' AND qty ~ '[1-9]'), 
    CONSTRAINT ck_stock_ledger_entry_qty_canonical CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'), 
    CONSTRAINT ck_stock_ledger_entry_qty_after CHECK (qty_after ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'), 
    CONSTRAINT ck_stock_ledger_entry_value_scale CHECK ((value ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'GBP' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'INR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'JPY' THEN strpos(value, '.') = 0 WHEN 'KWD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 3 WHEN 'USD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 ELSE false END)), 
    CONSTRAINT ck_stock_ledger_entry_value_sign CHECK (NOT (value ~ '^-')), 
    CONSTRAINT ck_stock_ledger_entry_value_after_scale CHECK ((value_after ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value_after !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value_after, '.') > 0 AND strpos(value_after, '.') = length(value_after) - 2 WHEN 'GBP' THEN strpos(value_after, '.') > 0 AND strpos(value_after, '.') = length(value_after) - 2 WHEN 'INR' THEN strpos(value_after, '.') > 0 AND strpos(value_after, '.') = length(value_after) - 2 WHEN 'JPY' THEN strpos(value_after, '.') = 0 WHEN 'KWD' THEN strpos(value_after, '.') > 0 AND strpos(value_after, '.') = length(value_after) - 3 WHEN 'USD' THEN strpos(value_after, '.') > 0 AND strpos(value_after, '.') = length(value_after) - 2 ELSE false END)), 
    CONSTRAINT ck_stock_ledger_entry_value_after_sign CHECK (NOT (value_after ~ '^-')), 
    FOREIGN KEY(currency) REFERENCES currency (code)
)""",
    r"""CREATE TABLE stock_layer (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    entry_id VARCHAR(36) NOT NULL, 
    item_id VARCHAR(36) NOT NULL, 
    warehouse_id VARCHAR(36) NOT NULL, 
    currency VARCHAR(3) NOT NULL, 
    qty VARCHAR(40) NOT NULL, 
    value VARCHAR(40) NOT NULL, 
    group_id VARCHAR(36) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_layer_entry FOREIGN KEY(company_id, entry_id) REFERENCES stock_ledger_entry (company_id, id), 
    CONSTRAINT fk_stock_layer_group FOREIGN KEY(company_id, group_id) REFERENCES stock_layer (company_id, id), 
    CONSTRAINT uq_stock_layer_company_id UNIQUE (company_id, id), 
    CONSTRAINT uq_stock_layer_entry UNIQUE (entry_id), 
    CONSTRAINT ck_stock_layer_qty_positive CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,4})?$' AND qty ~ '[1-9]'), 
    CONSTRAINT ck_stock_layer_qty_canonical CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'), 
    CONSTRAINT ck_stock_layer_value_scale CHECK ((value ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'GBP' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'INR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'JPY' THEN strpos(value, '.') = 0 WHEN 'KWD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 3 WHEN 'USD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 ELSE false END)), 
    CONSTRAINT ck_stock_layer_value_sign CHECK (NOT (value ~ '^-')), 
    FOREIGN KEY(currency) REFERENCES currency (code)
)""",
    r"""CREATE TABLE stock_layer_take (
    id VARCHAR(36) NOT NULL, 
    company_id VARCHAR(36) NOT NULL, 
    entry_id VARCHAR(36) NOT NULL, 
    group_id VARCHAR(36) NOT NULL, 
    layer_id VARCHAR(36), 
    currency VARCHAR(3) NOT NULL, 
    qty VARCHAR(40) NOT NULL, 
    value VARCHAR(40) NOT NULL, 
    kind VARCHAR(9) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT fk_stock_take_entry FOREIGN KEY(company_id, entry_id) REFERENCES stock_ledger_entry (company_id, id), 
    CONSTRAINT fk_stock_take_group FOREIGN KEY(company_id, group_id) REFERENCES stock_layer (company_id, id), 
    CONSTRAINT fk_stock_take_layer FOREIGN KEY(company_id, layer_id) REFERENCES stock_layer (company_id, id), 
    CONSTRAINT uq_stock_take_company_id UNIQUE (company_id, id), 
    CONSTRAINT ck_stock_take_kind CHECK (kind in ('take','give_back')), 
    CONSTRAINT ck_stock_layer_take_qty_positive CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,4})?$' AND qty ~ '[1-9]'), 
    CONSTRAINT ck_stock_layer_take_qty_canonical CHECK (qty ~ '^(0|[1-9][0-9]*)(\.[0-9]{0,3}[1-9])?$'), 
    CONSTRAINT ck_stock_layer_take_value_scale CHECK ((value ~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$' AND value !~ '^-0(\.0+)?$') AND (CASE currency WHEN 'EUR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'GBP' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'INR' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 WHEN 'JPY' THEN strpos(value, '.') = 0 WHEN 'KWD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 3 WHEN 'USD' THEN strpos(value, '.') > 0 AND strpos(value, '.') = length(value) - 2 ELSE false END)), 
    CONSTRAINT ck_stock_layer_take_value_sign CHECK (NOT (value ~ '^-')), 
    FOREIGN KEY(currency) REFERENCES currency (code)
)""",
)


def _postgres_triggers(schema: str) -> None:
    # Pinned search_path: the books' schema, then pg_temp last, so a session's temporary table can never stand in
    # for a stock table (PostgreSQL searches pg_temp first unless it is named).
    path = f"SET search_path = {schema}, pg_temp"
    op.execute(f"CREATE FUNCTION erpclaw_stock_refuse() RETURNS trigger LANGUAGE plpgsql {path} AS $$ "
               "BEGIN RAISE EXCEPTION '%: % refused', TG_TABLE_NAME, TG_OP; END $$")

    def trigger(name, timing, table, level="ROW"):
        op.execute(f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH {level} EXECUTE FUNCTION erpclaw_stock_refuse()")

    for table in NO_CHANGE:
        trigger(f"{table}_no_change", "BEFORE UPDATE OR DELETE", table)
    trigger("stock_head_no_delete", "BEFORE DELETE", "stock_head")
    for table in TABLES:  # TRUNCATE fires no row triggers
        trigger(f"{table}_no_truncate", "BEFORE TRUNCATE", table, level="STATEMENT")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    for statement in DDL:
        op.execute(statement)
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    _postgres_triggers(schema)


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

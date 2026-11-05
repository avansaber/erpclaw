"""the item's kind: a service, or kept in stock

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31
Data class: schema

What it changes:
- `item.kind`: service or stock, service by default. Every existing item becomes
  a service; a core revision never reads a plugin's table, so an item the stock
  plugin already keeps stays a service until it is marked again (the stock
  plugin's verification reports such an item).
- A trigger keeps the kind one way: service may become stock, stock never
  becomes service.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('item', sa.Column('kind', sa.String(length=8), nullable=False, server_default='service'))
    op.create_check_constraint('ck_item_kind', 'item', "kind in ('service','stock')")
    op.execute("CREATE FUNCTION erpclaw_item_kind_one_way() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
               "IF OLD.kind = 'stock' AND NEW.kind <> 'stock' THEN "
               "RAISE EXCEPTION 'item: a stock item never becomes a service again'; END IF; "
               "RETURN NEW; END $$")
    op.execute("CREATE TRIGGER item_kind_one_way BEFORE UPDATE OF kind ON item "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_item_kind_one_way()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

"""a party's city key: the city as a case-insensitive match compares it

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-02
Data class: schema and backfill

What it changes:
- `party.city_key`: the city's comparison key (Unicode NFKC, outer whitespace dropped, then case folded), computed
  in Python and written beside the city, so a case-insensitive city match compares keys by equality and never uses
  the database's case rules. Each existing party with a city takes its city's key; one without a city has none.
- `ck_party_city_key`: a party has a city key exactly when it has a city.
No downgrade: it holds books.
"""
import unicodedata

from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

party = sa.table('party', sa.column('id', sa.String), sa.column('city', sa.String), sa.column('city_key', sa.Text))


def _key(city: str) -> str:
    """The key as this revision writes it, written out so a later change to the engine never changes this revision."""
    return unicodedata.normalize("NFKC", city).strip().casefold()


def upgrade() -> None:
    op.add_column('party', sa.Column('city_key', sa.Text(), nullable=True))
    conn = op.get_bind()
    for pid, city in conn.execute(sa.select(party.c.id, party.c.city).where(party.c.city.is_not(None))).all():
        conn.execute(party.update().where(party.c.id == pid).values(city_key=_key(city)))
    op.create_check_constraint('ck_party_city_key', 'party', '(city is null) = (city_key is null)')


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

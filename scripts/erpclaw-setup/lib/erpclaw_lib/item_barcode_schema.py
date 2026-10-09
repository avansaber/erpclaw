"""Inventory-owned, company-scoped barcode mappings for stock drafts."""
from erpclaw_lib.seam import (
    Column, ForeignKey, MetaData, Table, Text, UniqueConstraint,
    reference_table,
)

METADATA = MetaData()
reference_table("company", METADATA)
reference_table("item", METADATA)

ITEM_BARCODE = Table(
    "item_barcode", METADATA,
    Column("id", Text, primary_key=True),
    Column("company_id", Text, ForeignKey("company.id", ondelete="RESTRICT"),
           nullable=False),
    Column("item_id", Text, ForeignKey("item.id", ondelete="RESTRICT"),
           nullable=False),
    Column("barcode", Text, nullable=False),
    UniqueConstraint("company_id", "barcode"),
)

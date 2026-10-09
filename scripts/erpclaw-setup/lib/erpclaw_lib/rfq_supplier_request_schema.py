"""Buying-owned storage for company-scoped, unsent supplier preparations."""
from erpclaw_lib.seam import (
    Column, ForeignKey, Index, MetaData, Table, Text, reference_table,
)

METADATA = MetaData()
reference_table("company", METADATA)
reference_table("request_for_quotation", METADATA)

RFQ_SUPPLIER_REQUEST = Table(
    "rfq_supplier_request", METADATA,
    Column("id", Text, primary_key=True),
    Column("company_id", Text, ForeignKey("company.id", ondelete="RESTRICT"),
           nullable=False),
    Column("rfq_id", Text, ForeignKey("request_for_quotation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("prepared_at", Text, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Index("idx_rfq_supplier_request_scope", "company_id", "rfq_id"),
)

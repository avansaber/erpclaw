"""Company sender settings; credentials belong only in the environment/keychain."""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, String, Table, UniqueConstraint

from erpclaw_core.db import Runtime, metadata

FIELDS = ("from_address", "display_name", "smtp_host", "port", "security", "username", "reply_to")
email_sender = Table(
    "email_sender", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("from_address", String(200), nullable=False),
    Column("display_name", String(200), nullable=False),
    Column("smtp_host", String(253), nullable=False),
    Column("port", Integer, nullable=False),
    Column("security", String(8), nullable=False),
    Column("username", String(200), nullable=False),
    Column("reply_to", String(200)),
    UniqueConstraint("company_id", name="uq_email_sender_company"),
    CheckConstraint("id = company_id", name="ck_email_sender_company_key"),
    CheckConstraint("port BETWEEN 1 AND 65535", name="ck_email_sender_port"),
    CheckConstraint("security IN ('starttls', 'tls')", name="ck_email_sender_security"),
    info={"owner": "email", "runtime": Runtime(insert=True, update=FIELDS)},
)

"""Company sender settings, without credentials.

Revision ID: email_0001
Revises: (base of the email branch)
Data class: schema

Frozen definitions, independent of current plugin models. The company key also
identifies the settings row, including its absence in an approval read set.
"""
from alembic import op

revision = "email_0001"
down_revision = None
branch_labels = ("email",)
depends_on = None
creates = ("email_sender",)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    op.execute("""CREATE TABLE email_sender (
        id VARCHAR(36) PRIMARY KEY,
        company_id VARCHAR(36) NOT NULL REFERENCES company(id),
        from_address VARCHAR(200) NOT NULL,
        display_name VARCHAR(200) NOT NULL,
        smtp_host VARCHAR(253) NOT NULL,
        port INTEGER NOT NULL,
        security VARCHAR(8) NOT NULL,
        username VARCHAR(200) NOT NULL,
        reply_to VARCHAR(200),
        CONSTRAINT uq_email_sender_company UNIQUE (company_id),
        CONSTRAINT ck_email_sender_company_key CHECK (id = company_id),
        CONSTRAINT ck_email_sender_port CHECK (port BETWEEN 1 AND 65535),
        CONSTRAINT ck_email_sender_security CHECK (security IN ('starttls', 'tls'))
    )""")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")

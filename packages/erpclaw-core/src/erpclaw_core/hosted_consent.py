"""Read the company's live hosted model consent from its PostgreSQL books."""
from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, RowMapping

from .models import hosted_books_consent


def live_consent(conn: Connection, company_id: str) -> RowMapping | None:
    """Return live consent, or None when the table or the company's live row is absent."""
    if not inspect(conn).has_table(hosted_books_consent.name):
        return None
    return conn.execute(select(hosted_books_consent).where(
        hosted_books_consent.c.company_id == company_id,
        hosted_books_consent.c.status == "live",
    )).mappings().one_or_none()

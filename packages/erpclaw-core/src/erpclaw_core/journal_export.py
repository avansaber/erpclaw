"""Read-only journal CSV, published privately without replacing an existing file."""
from __future__ import annotations

import csv
import io
import os
from datetime import date
from pathlib import Path

from sqlalchemy import Text, cast, select
from sqlalchemy.engine import Connection

from . import models as m, principals
from .common import Refusal, company_ctx, new_id


HEADER = (
    "posting_date", "seq", "voucher_type", "voucher_id", "memo", "line_no",
    "account_number", "account_name", "party", "debit", "credit", "currency", "rate",
    "functional_currency", "functional_debit", "functional_credit", "kind",
)
NUMERIC_COLUMNS = frozenset({"seq", "line_no", "debit", "credit", "rate",
                             "functional_debit", "functional_credit"})
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _period(start: str, end: str) -> tuple[date, date]:
    try:
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        if lo.isoformat() != start or hi.isoformat() != end:
            raise ValueError
    except (TypeError, ValueError):
        raise Refusal("Give --from and --to as YYYY-MM-DD dates.", "bad_period") from None
    if hi < lo:
        raise Refusal("The end date is before the start date.", "bad_period")
    return lo, hi


def _fresh_principal(conn: Connection, principal: str | dict | None) -> dict:
    if isinstance(principal, dict):
        # An embedded caller may carry a row captured before disablement.
        return principals.by_id(conn, principal["id"])
    if principal is None or principal == principals.OWNER:
        return principals.owner(conn)
    row = conn.execute(select(m.principal).where(m.principal.c.name == principal)).mappings().first()
    if row is None:
        raise Refusal(f"There is no principal called {principal}.", "no_principal")
    return dict(row)


def _journal_query(company_id: str, lo: date, hi: date):
    p, e, a, party = m.posting, m.ledger_entry, m.account, m.party
    # MoneyText's result processor returns Decimal. SQL casts bypass it so the
    # CSV preserves the database's exact spelling, including every trailing zero.
    return select(
        p.c.posting_date, p.c.seq, p.c.voucher_type, p.c.voucher_id, p.c.memo, e.c.line_no,
        a.c.number.label("account_number"), a.c.name.label("account_name"), party.c.name.label("party"),
        cast(e.c.debit, Text).label("debit"), cast(e.c.credit, Text).label("credit"), e.c.currency,
        cast(e.c.rate, Text).label("rate"), e.c.functional_currency,
        cast(e.c.functional_debit, Text).label("functional_debit"),
        cast(e.c.functional_credit, Text).label("functional_credit"), e.c.kind,
    ).select_from(
        p.join(e, e.c.posting_id == p.c.id).join(a, a.c.id == e.c.account_id)
        .outerjoin(party, party.c.id == e.c.party_id)
    ).where(
        p.c.company_id == company_id, p.c.posting_date >= lo, p.c.posting_date <= hi,
    ).order_by(p.c.seq, e.c.line_no)


def _cell(column: str, value):
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if column not in NUMERIC_COLUMNS and isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _already_exists() -> Refusal:
    return Refusal("The output already exists; a journal export never overwrites.", "output_exists")


def _publish(path: Path, rows) -> None:
    if os.path.lexists(path):
        raise _already_exists()
    temp = path.with_name(f".{path.name}.{new_id()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            # Including CR in the writer's terminator makes QUOTE_MINIMAL quote
            # both CR and LF in fields. Replace only each record's final CRLF.
            record = io.StringIO(newline="")
            writer = csv.writer(record, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)

            def write_row(values):
                record.seek(0)
                record.truncate()
                writer.writerow(values)
                out.write(record.getvalue()[:-2] + "\n")

            write_row(HEADER)
            for row in rows:
                write_row([_cell(column, row[column]) for column in HEADER])
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            raise _already_exists() from None
    finally:
        temp.unlink()


def export_journal(conn: Connection, path: Path, *, from_date: str, to_date: str,
                   company_name: str | None = None, principal: str | dict | None = None) -> None:
    """Export one company's inclusive period, requiring current view AND export.

    `principal` is a name, an embedded caller's row (re-read by id), or the
    default owner. Only SELECTs run; the caller owns the connection/transaction.
    Grant changes committed after the fresh check are outside this read's scope.
    """
    lo, hi = _period(from_date, to_date)
    ctx = company_ctx(conn, company_name=company_name)
    fresh = _fresh_principal(conn, principal)
    try:
        principals.require(conn, fresh, ctx.company_id, "view", "export")
    except Refusal as exc:
        if exc.code != "forbidden":
            raise
        raise Refusal("You may not export this company's journal.", "forbidden") from None
    rows = conn.execute(_journal_query(ctx.company_id, lo, hi)).mappings()
    _publish(Path(path), rows)

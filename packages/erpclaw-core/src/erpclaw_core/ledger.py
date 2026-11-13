"""The posting service: the only writer of `posting` and `ledger_entry`.

`post` checks, in order, stopping at the first failure (nothing is written):
  1. the company exists (its chain head is locked first)
  2. the posting date falls in an open fiscal period of the company (locked next);
     only the year-end close posts into a locked period, through a private path
  3. at least two lines
  4. every account is this company's, not a group, not frozen; a receivable
     line names a customer and a payable line a supplier of this company;
     other lines name no party
  5. every amount parses at the functional currency's scale, is not negative,
     and exactly one side of each line is positive
  6. debits equal credits exactly (a rounding residue is a line of its own,
     added by `balance_with_rounding`, never absorbed here)
  7. the voucher is new for the company
  8. opening entries: balance sheet accounts only, in the first fiscal year, and
     never dated after an ordinary posting; an ordinary posting is never dated
     before an opening entry (both checked under the chain-head lock)

Locks, always in this order, held to commit: the company's chain head, then
the period. They are row locks (SELECT ... FOR UPDATE).

Rows are never updated or deleted (database triggers refuse it): a reversal is
a mirror posting linked to its original. Each posting extends the company's
hash chain; `verify_chain` recomputes it.

A ledger line carries its transaction amount (debit, credit, currency), the
rate to the functional currency, the functional debit and credit, its kind
(measured, clearing or adjustment) and a reference to a dimension set. In this
release every line is measured in the functional currency at rate 1 with no
dimension set (database checks hold it so), so the posting service takes no
rate, currency, kind or dimension. The line hash is versioned (`HASH_FORMAT`):
a later field appends to the field order under a new format, and books from
before format 2 are not carried forward.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select, update

from . import models as m
from .common import Ctx, Refusal, new_id, now
from .money import MINOR_UNITS, MoneyError, to_amount

GENESIS = "0" * 64
ZERO = Decimal("0")
ONE = Decimal("1")
MEASURED = "measured"
OPENING = "opening"
CLOSING = "year_end_close"

# The hashed form, format 2: the header's fields, then each line's, in these orders. A new field is appended to the
# end of its order under a new format; `dimset` (the line's dimension set digest, empty when it has none) stays after
# the rate and the functional amounts.
HASH_FORMAT = 2
HEADER_FIELDS = ("company_id", "seq", "posting_date", "voucher_type", "voucher_id", "memo", "reverses_posting_id",
                 "operation_key", "created_by", "ledger")
LINE_FIELDS = ("line_no", "account_id", "party_id", "debit", "credit", "currency", "rate", "functional_currency",
               "functional_debit", "functional_credit", "kind", "dimset")


@dataclass
class Line:
    account_id: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    party_id: str | None = None


@dataclass(frozen=True)
class PostingResult:
    posting_id: str
    seq: int
    hash: str


def account_by_number(ctx: Ctx, number: str) -> dict:
    row = ctx.conn.execute(
        select(m.account).where(m.account.c.company_id == ctx.company_id, m.account.c.number == number)
    ).mappings().first()
    if row is None:
        raise Refusal(f"Account {number} does not exist in {ctx.company_name}.", "no_account")
    return dict(row)


# A role in the owner's words, so a refusal never shows a column value; a role not named here is its own name.
ROLE_WORDS = {
    "payable": "accounts payable",
    "opening_balance": "opening balance equity",
    "stock_adjustment": "stock adjustment",
    "stock_write_off": "inventory write-off",
    "stock": "inventory",
    "cost_of_goods_sold": "cost of goods sold",
    "stock_received_not_billed": "stock received not billed",
    "sales_returns": "sales returns",
    "retained_earnings": "retained earnings",
    "exchange_gain_loss_realised": "realized exchange gain or loss",
    "exchange_gain_loss_unrealised": "unrealized exchange gain or loss",
}


def account_by_type(ctx: Ctx, account_type: str) -> dict:
    """The company's one account with this role that is not a group and not frozen. It never chooses: none, only
    frozen ones, or more than one is a refusal. A caller that holds several by design names its account itself."""
    rows = ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == account_type,
        m.account.c.is_group.is_(False)).order_by(m.account.c.number)).mappings().all()
    words = ROLE_WORDS.get(account_type, account_type)
    live = [r for r in rows if not r["frozen"]]
    if len(live) == 1:
        return dict(live[0])
    if live:
        raise Refusal(f"{ctx.company_name} has more than one {words} account; say which one.",
                      f"several_{account_type}_accounts")
    if rows:
        raise Refusal(f"{rows[0]['number']} {rows[0]['name']} is frozen.", "frozen_account")
    raise Refusal(f"{ctx.company_name} has no {words} account.", f"no_{account_type}_account")


def cash_account(ctx: Ctx) -> dict:
    """Cash is identified by its normalised name, never by the bank role or account number."""
    rows = ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.is_group.is_(False))
        .order_by(m.account.c.number)).mappings().all()
    rows = [r for r in rows if " ".join(r["name"].split()).lower() == "cash on hand"]
    live = [r for r in rows if not r["frozen"]]
    if len(live) == 1:
        return dict(live[0])
    if live:
        names = ", ".join(f"{r['number']} {r['name']}" for r in live)
        raise Refusal(f"{ctx.company_name} has more than one Cash on Hand account: {names}; say which one.",
                      "several_cash_accounts")
    if rows:
        raise Refusal(f"{rows[0]['number']} {rows[0]['name']} is frozen.", "frozen_account")
    raise Refusal(f"{ctx.company_name} has no Cash on Hand account.", "no_cash_account")


def _for_update(ctx: Ctx, query):
    return query.with_for_update()


for_update = _for_update  # a plugin locks its own rows the same way (FOR UPDATE)


def _for_share(conn, query):
    """FOR SHARE (a principal's row at a card commit)."""
    return query.with_for_update(read=True)


def _lock_head(ctx: Ctx) -> dict:
    head = m.posting_chain_head
    row = ctx.conn.execute(_for_update(ctx, select(head).where(head.c.company_id == ctx.company_id))).mappings().first()
    if row is None:
        raise Refusal("That company does not exist.", "no_company")
    return dict(row)


def lock_chain(ctx: Ctx) -> None:
    """Take the company's chain-head lock first, for a caller that locks its own rows before posting
    (the ledger's lock order: the chain head, then the period; `post` takes it again harmlessly)."""
    _lock_head(ctx)


def find_period(ctx: Ctx, posting_date: date, *, lock: bool = False) -> dict:
    fp = m.fiscal_period
    q = select(fp).where(fp.c.company_id == ctx.company_id, fp.c.start_date <= posting_date,
                         fp.c.end_date >= posting_date)
    row = ctx.conn.execute(_for_update(ctx, q) if lock else q).mappings().first()
    if row is None:
        raise Refusal(f"There is no accounting period for {posting_date.isoformat()}.", "no_period")
    return dict(row)


def open_period(ctx: Ctx, posting_date: date, *, lock: bool = False) -> dict:
    row = find_period(ctx, posting_date, lock=lock)
    if row["status"] != "open":
        raise Refusal(f"The period containing {posting_date.isoformat()} is {row['status']}.", "period_closed")
    return row


def _writer_lock_periods(ctx: Ctx) -> None:
    """Status changes take the company's chain head first, like a posting and a
    close, so every path locks in one order (head, year, periods) and a
    transaction that unlocks a period and then posts cannot deadlock against a
    close."""
    _lock_head(ctx)


def set_period_status(ctx: Ctx, period_id: str, status: str) -> None:
    """Open or lock a period. Takes the chain head, then the period row, the
    order every writer uses. The status is reread under the lock: `closed` is
    terminal, and only `close_year` closes."""
    if status not in ("open", "locked"):
        raise Refusal("A period is closed only by closing its fiscal year.", "close_by_year")
    fp = m.fiscal_period
    _writer_lock_periods(ctx)
    row = ctx.conn.execute(_for_update(ctx, select(fp).where(
        fp.c.id == period_id, fp.c.company_id == ctx.company_id))).mappings().first()
    if row is None:
        raise Refusal("That period does not exist.", "no_period")
    if row["status"] == "closed":
        raise Refusal("That period is closed with its fiscal year, and stays closed.", "period_closed")
    ctx.conn.execute(update(fp).where(fp.c.id == period_id).values(status=status))


_LOCKEDNESS = {"open": 0, "locked": 1, "closed": 2}


def lock_through(ctx: Ctx, through: date) -> int:
    """Freeze the books through `through`, which must be a period's last day:
    every open period ending on or before it is locked. Locks run forward
    without gaps (closed, then locked, then open, in date order), so a lock
    that would leave an open period before a locked one is refused. Returns
    how many periods it locked."""
    fp = m.fiscal_period
    _writer_lock_periods(ctx)
    rows = [dict(r) for r in ctx.conn.execute(_for_update(ctx, select(fp).where(
        fp.c.company_id == ctx.company_id).order_by(fp.c.start_date))).mappings()]
    if not any(r["end_date"] == through for r in rows):
        holding = next((r for r in rows if r["start_date"] <= through <= r["end_date"]), None)
        if holding is None:
            raise Refusal(f"There is no accounting period for {through.isoformat()}.", "no_period")
        before = [r for r in rows if r["end_date"] < holding["start_date"]]
        options = f"through {holding['end_date']:%d %B %Y}, the end of that period"
        if before:
            options += f", or through {before[-1]['end_date']:%d %B %Y}"
        raise Refusal(f"The books freeze through the end of a period: {options}.", "freeze_not_period_end")
    after = [_LOCKEDNESS["locked"] if r["end_date"] <= through and r["status"] == "open" else _LOCKEDNESS[r["status"]]
             for r in rows]
    if any(a < b for a, b in zip(after, after[1:])):
        raise Refusal("That freeze would leave an open period before a locked one; locks run forward in order.",
                      "freeze_gap")
    targets = [r["id"] for r in rows if r["end_date"] <= through and r["status"] == "open"]
    if targets:
        ctx.conn.execute(update(fp).where(fp.c.id.in_(targets)).values(status="locked"))
    return len(targets)


def _parse(ctx: Ctx, value) -> Decimal:
    try:
        return to_amount(value, ctx.currency)
    except MoneyError as exc:
        raise Refusal(f"An amount is not valid: {exc}.", "bad_amount") from None


def _check_lines(ctx: Ctx, lines: list[Line]) -> list[Line]:
    if len(lines) < 2:
        raise Refusal("A posting needs at least two lines.", "too_few_lines")
    clean: list[Line] = []
    for ln in lines:
        acc = ctx.conn.execute(select(m.account).where(
            m.account.c.id == ln.account_id, m.account.c.company_id == ctx.company_id)).mappings().first()
        if acc is None:
            raise Refusal("A line uses an account that is not in this company.", "foreign_account")
        if acc["is_group"]:
            raise Refusal(f"{acc['number']} {acc['name']} is a group account; post to one of its accounts.",
                          "group_account")
        if acc["frozen"]:
            raise Refusal(f"{acc['number']} {acc['name']} is frozen.", "frozen_account")
        need = {"receivable": "customer", "payable": "supplier"}.get(acc["account_type"])
        if need:
            if ln.party_id is None:
                raise Refusal(f"A {acc['account_type']} line must name a {need}.", f"{acc['account_type']}_needs_party")
            p = ctx.conn.execute(select(m.party).where(
                m.party.c.id == ln.party_id, m.party.c.company_id == ctx.company_id)).mappings().first()
            if p is None or p["kind"] != need:
                raise Refusal(f"A {acc['account_type']} line must name a {need} of this company.", "bad_party")
        elif ln.party_id is not None:
            raise Refusal(f"{acc['number']} {acc['name']} takes no party.", "party_not_allowed")
        clean.append(Line(ln.account_id, _parse(ctx, ln.debit), _parse(ctx, ln.credit), ln.party_id))
    for ln in clean:
        if (ln.debit > 0) == (ln.credit > 0):
            raise Refusal("Each ledger line is a debit or a credit: one side positive, the other zero.",
                          "one_side_per_line")
    total_d = sum((ln.debit for ln in clean), ZERO)
    total_c = sum((ln.credit for ln in clean), ZERO)
    if total_d != total_c:
        raise Refusal(f"Debits ({total_d}) and credits ({total_c}) do not match.", "unbalanced")
    return clean


def balance_with_rounding(ctx: Ctx, lines: list[Line]) -> list[Line]:
    """A document's rounded lines may leave a residue of at most half a unit
    per line; it becomes an explicit line on the rounding account. A larger
    residue is an error in the document, so it is refused."""
    total = sum((_parse(ctx, ln.debit) - _parse(ctx, ln.credit) for ln in lines), ZERO)
    if total == 0:
        return list(lines)
    unit = Decimal(1).scaleb(-MINOR_UNITS[ctx.currency])
    if abs(total) > unit / 2 * len(lines):
        raise Refusal(f"The lines are out of balance by {abs(total)}, more than rounding can explain.", "unbalanced")
    rounding = account_by_type(ctx, "rounding")
    extra = Line(rounding["id"], credit=total) if total > 0 else Line(rounding["id"], debit=-total)
    return [*lines, extra]


def _canonical(header: dict, lines: list[dict]) -> bytes:
    """Format 2: the format, the header's fields and the lines, each in its fixed order (never sorted by name)."""
    body = {"format": HASH_FORMAT, **{f: header[f] for f in HEADER_FIELDS},
            "lines": [{f: ln[f] for f in LINE_FIELDS} for ln in lines]}
    return json.dumps(body, separators=(",", ":")).encode()


def _hash(prev_hash: str, header: dict, lines: list[dict]) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(header, lines)).hexdigest()


def _header(company_id, seq, posting_date, voucher_type, voucher_id, memo, reverses, operation_key, actor,
            ledger: str = "general") -> dict:
    return {"company_id": company_id, "seq": seq, "posting_date": posting_date.isoformat(),
            "voucher_type": voucher_type, "voucher_id": voucher_id, "memo": memo, "reverses_posting_id": reverses,
            "operation_key": operation_key, "created_by": actor, "ledger": ledger}


def _amount_text(value) -> str | None:
    return None if value is None else format(value, "f")


def _entry_dict(row, dimset: str) -> dict:
    """A stored (or about to be stored) ledger line in its hashed form; `dimset` is its set's digest, or ''."""
    return {"line_no": row["line_no"], "account_id": row["account_id"], "party_id": row["party_id"],
            "debit": _amount_text(row["debit"]), "credit": _amount_text(row["credit"]), "currency": row["currency"],
            "rate": _amount_text(row["rate"]), "functional_currency": row["functional_currency"],
            "functional_debit": _amount_text(row["functional_debit"]),
            "functional_credit": _amount_text(row["functional_credit"]), "kind": row["kind"], "dimset": dimset}


def _entries(lines: list[Line], currency: str) -> list[dict]:
    """The rows `post` writes: each line measured in the functional currency at rate 1, with no dimension set."""
    return [{"line_no": i, "account_id": ln.account_id, "party_id": ln.party_id, "debit": ln.debit,
             "credit": ln.credit, "currency": currency, "rate": ONE, "functional_currency": currency,
             "functional_debit": ln.debit, "functional_credit": ln.credit, "kind": MEASURED,
             "dimension_set_id": None} for i, ln in enumerate(lines, 1)]


# --- dimension sets: insert-only, content-addressed; no line names one in this release ---------------------------

def dimension_content(values: dict[str, str]) -> str:
    """A set's canonical content: the {dimension type: value id} map, sorted, in compact JSON."""
    return json.dumps(dict(sorted(values.items())), separators=(",", ":"))


def dimension_digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def dimension_set_holds(content: str, digest: str) -> bool:
    """Whether a stored set's digest is the SHA-256 of its content, and its content the canonical form of a map of
    names to ids: the line hash covers the digest, so the content is checked against it, never trusted."""
    try:
        values = json.loads(content)
    except ValueError:
        return False
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        return False
    return content == dimension_content(values) and digest == dimension_digest(content)


@dataclass(frozen=True)
class _Closing:
    """The year-end close's own posting: the one exception to the open-period check."""
    fiscal_year_id: str


def _check_closing_target(ctx: Ctx, closing: _Closing, posting_date: date, voucher_type: str,
                          voucher_id: str) -> None:
    fy = ctx.conn.execute(select(m.fiscal_year).where(
        m.fiscal_year.c.id == closing.fiscal_year_id, m.fiscal_year.c.company_id == ctx.company_id)).mappings().first()
    if fy is None or fy["status"] != "open":
        raise Refusal("The closing entry needs this company's open fiscal year.", "closing_target")
    if voucher_type != CLOSING or voucher_id != fy["id"] or posting_date != fy["end_date"]:
        raise Refusal("The closing entry is dated the year's last day and names that year.", "closing_target")
    period = find_period(ctx, posting_date, lock=True)
    last = ctx.conn.execute(select(m.fiscal_period).where(m.fiscal_period.c.fiscal_year_id == fy["id"])
                            .order_by(m.fiscal_period.c.start_date.desc()).limit(1)).mappings().first()
    if period["id"] != last["id"] or period["status"] != "locked":
        raise Refusal("The closing entry goes into the year's last period, locked.", "closing_target")


def _check_opening_order(ctx: Ctx, posting_date: date, voucher_type: str, lines: list[Line]) -> None:
    """Check opening balances and posting date order under the chain-head lock."""
    p = m.posting
    if voucher_type == OPENING:
        roots = ctx.conn.execute(select(m.account.c.root_type).where(
            m.account.c.id.in_([ln.account_id for ln in lines]))).scalars().all()
        if any(r in ("income", "expense") for r in roots):
            raise Refusal("An opening entry carries balance sheet balances only, never income or expense.",
                          "opening_pl")
        first = ctx.conn.execute(select(m.fiscal_year).where(m.fiscal_year.c.company_id == ctx.company_id)
                                 .order_by(m.fiscal_year.c.start_date).limit(1)).mappings().first()
        if first is None or not first["start_date"] <= posting_date <= first["end_date"]:
            raise Refusal("An opening entry is dated in the company's first fiscal year.", "opening_year")
        if ctx.conn.execute(select(p.c.id).where(p.c.company_id == ctx.company_id, p.c.voucher_type != OPENING,
                                                 p.c.posting_date < posting_date).limit(1)).first():
            raise Refusal("An opening entry cannot come after entries dated before it.", "opening_after_entries")
    elif ctx.conn.execute(select(p.c.id).where(p.c.company_id == ctx.company_id, p.c.voucher_type == OPENING,
                                               p.c.posting_date > posting_date).limit(1)).first():
        raise Refusal("That date is before the opening balances; entries start on or after them.",
                      "before_opening")


def check_post(ctx: Ctx, posting_date: date, voucher_type: str, voucher_id: str | None,
               lines: list[Line], *, _opening_preview: bool = False) -> list[Line]:
    """Every refusal `post` can raise before its first write, without writing and without a lock, so a preview can
    refuse a card for a posting the books would refuse. `voucher_id` None is a
    document not yet created (an invoice created and submitted in one card): it cannot already be in the books, so
    the duplicate check is skipped here and run by `post` with the real id. Returns the cleaned lines."""
    if voucher_type == CLOSING:
        raise Refusal("A year-end closing entry is made only by closing the year.", "closing_voucher")
    open_period(ctx, posting_date)
    clean = _check_lines(ctx, lines)
    from .payables import check_payable_source

    if _opening_preview and voucher_type == OPENING:
        check_payable_source(ctx, voucher_type, clean, preview=True)
    else:
        check_payable_source(ctx, voucher_type, clean)
    _check_opening_order(ctx, posting_date, voucher_type, clean)
    from .integration import account_codes, system_of_record

    mode, external = system_of_record(ctx, "accounting")
    if mode == "external":
        account_codes(ctx, external, [ln.account_id for ln in clean])
    if voucher_id is not None and ctx.conn.execute(select(m.posting.c.id).where(
            m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == voucher_type,
            m.posting.c.voucher_id == voucher_id)).first():
        raise Refusal("That document is already in the books.", "duplicate_voucher")
    return clean


def post(ctx: Ctx, posting_date: date, voucher_type: str, voucher_id: str, lines: list[Line], *,
         memo: str | None = None, operation_key: str | None = None,
         _reverses: str | None = None) -> PostingResult:
    check_post(ctx, posting_date, voucher_type, voucher_id, lines)  # the preview's checks, the same code
    return _post(ctx, posting_date, voucher_type, voucher_id, lines, memo=memo, operation_key=operation_key,
                 reverses=_reverses, closing=None)


def _post(ctx: Ctx, posting_date: date, voucher_type: str, voucher_id: str, lines: list[Line], *,
          memo: str | None, operation_key: str | None, reverses: str | None,
          closing: _Closing | None) -> PostingResult:
    head = _lock_head(ctx)
    if closing is None:
        open_period(ctx, posting_date, lock=True)
    else:
        _check_closing_target(ctx, closing, posting_date, voucher_type, voucher_id)
    clean = _check_lines(ctx, lines)
    from .payables import check_payable_source

    check_payable_source(ctx, voucher_type, clean)
    _check_opening_order(ctx, posting_date, voucher_type, clean)
    # The ledger kind comes only from the system of record, read under the head lock.
    from .integration import account_codes, stage_outbound_posting, system_of_record

    mode, external = system_of_record(ctx, "accounting")
    kind = "sub" if mode == "external" else "general"
    codes = account_codes(ctx, external, [ln.account_id for ln in clean]) if kind == "sub" else {}
    p = m.posting
    if ctx.conn.execute(select(p.c.id).where(p.c.company_id == ctx.company_id, p.c.voucher_type == voucher_type,
                                             p.c.voucher_id == voucher_id)).first():
        raise Refusal("That document is already in the books.", "duplicate_voucher")
    seq = head["last_seq"] + 1
    header = _header(ctx.company_id, seq, posting_date, voucher_type, voucher_id, memo, reverses,
                     operation_key, ctx.actor, kind)
    entries = _entries(clean, ctx.currency)
    digest = _hash(head["last_hash"], header, [_entry_dict(e, "") for e in entries])
    pid = new_id()
    ctx.conn.execute(p.insert().values(
        id=pid, company_id=ctx.company_id, seq=seq, posting_date=posting_date, voucher_type=voucher_type,
        voucher_id=voucher_id, memo=memo, reverses_posting_id=reverses, operation_key=operation_key,
        created_at=now(), created_by=ctx.actor, prev_hash=head["last_hash"], hash=digest, ledger=kind,
        hash_format=HASH_FORMAT))
    for e in entries:
        ctx.conn.execute(m.ledger_entry.insert().values(id=new_id(), company_id=ctx.company_id, posting_id=pid, **e))
    ctx.conn.execute(update(m.posting_chain_head).where(m.posting_chain_head.c.company_id == ctx.company_id)
                     .values(last_seq=seq, last_hash=digest))
    if kind == "sub":  # the outbound journal, in the posting's own transaction
        body = {"date": posting_date.isoformat(), "memo": memo, "voucher_type": voucher_type,
                "voucher_id": voucher_id,
                "lines": [{"account": codes[ln.account_id], "debit": format(ln.debit, "f"),
                           "credit": format(ln.credit, "f"), "currency": ctx.currency} for ln in clean]}
        stage_outbound_posting(ctx, external, pid, operation_key or f"{voucher_type}:{voucher_id}", body)
    return PostingResult(pid, seq, digest)


def posting_for_voucher(ctx: Ctx, voucher_type: str, voucher_id: str) -> dict:
    row = ctx.conn.execute(select(m.posting).where(
        m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == voucher_type,
        m.posting.c.voucher_id == voucher_id)).mappings().first()
    if row is None:
        raise Refusal("There is nothing in the books to reverse for that document.", "nothing_to_reverse")
    return dict(row)


def check_reverse(ctx: Ctx, posting_id: str, reversal_date: date) -> list[Line]:
    """Every refusal `reverse` can raise before its first write, without writing and without a lock:
    its own checks, then `check_post` on the mirrored lines. Returns the mirrored lines."""
    orig, mirror = _reversal_of(ctx, posting_id, reversal_date)
    return check_post(ctx, reversal_date, orig["voucher_type"] + ":reversal", orig["voucher_id"], mirror)


def _reversal_of(ctx: Ctx, posting_id: str, reversal_date: date) -> tuple[dict, list[Line]]:
    orig = ctx.conn.execute(select(m.posting).where(
        m.posting.c.id == posting_id, m.posting.c.company_id == ctx.company_id)).mappings().first()
    if orig is None:
        raise Refusal("There is nothing in the books to reverse for that document.", "nothing_to_reverse")
    if orig["reverses_posting_id"] is not None:
        raise Refusal("That entry is itself a reversal.", "reversal_of_reversal")
    if orig["voucher_type"] == CLOSING:
        raise Refusal("A year-end closing entry is not reversed; reopening a closed year is not available.",
                      "closing_not_reversed")
    if reversal_date < orig["posting_date"]:
        raise Refusal("A reversal cannot be dated before the entry it reverses.", "reversal_before_original")
    if ctx.conn.execute(select(m.posting.c.id).where(m.posting.c.reverses_posting_id == posting_id)).first():
        raise Refusal("That document has already been reversed.", "already_reversed")
    le = m.ledger_entry
    rows = ctx.conn.execute(select(le).where(le.c.posting_id == posting_id).order_by(le.c.line_no)).mappings().all()
    return orig, [Line(r["account_id"], debit=r["credit"], credit=r["debit"], party_id=r["party_id"]) for r in rows]


def reverse(ctx: Ctx, posting_id: str, reversal_date: date) -> PostingResult:
    """A mirror posting (debits and credits swapped), dated `reversal_date`,
    linked to the original. The original is never touched; a posting is
    reversed at most once (a unique index), and a reversal is never reversed."""
    orig, mirror = _reversal_of(ctx, posting_id, reversal_date)  # the same checks as check_reverse
    return post(ctx, reversal_date, orig["voucher_type"] + ":reversal", orig["voucher_id"], mirror,
                memo=f"reverses posting {orig['seq']}", _reverses=posting_id)


def _posting_hash(conn, row, prev: str) -> str | None:
    """The hash a stored posting's rows give after `prev`, or None when it has none: a format this engine does not
    know, or a line naming a dimension set that is not in the posting's company or whose digest does not hold for
    its content (the set's digest is recomputed, never taken as stored)."""
    if row["hash_format"] != HASH_FORMAT:
        return None
    le, ds = m.ledger_entry, m.dimension_set
    lines = conn.execute(select(le).where(le.c.posting_id == row["id"]).order_by(le.c.line_no)).mappings().all()
    named = {r["dimension_set_id"] for r in lines if r["dimension_set_id"] is not None}
    digests: dict[str, str] = {}
    if named:
        for s in conn.execute(select(ds).where(ds.c.company_id == row["company_id"], ds.c.id.in_(named))).mappings():
            if not dimension_set_holds(s["content"], s["digest"]):
                return None
            digests[s["id"]] = s["digest"]
        if set(digests) != named:
            return None
    header = _header(row["company_id"], row["seq"], row["posting_date"], row["voucher_type"], row["voucher_id"],
                     row["memo"], row["reverses_posting_id"], row["operation_key"], row["created_by"], row["ledger"])
    return _hash(prev, header, [_entry_dict(r, digests.get(r["dimension_set_id"], "")) for r in lines])


def verify_chain(conn, company_id: str) -> int | None:
    """The seq of the first posting whose link or hash does not verify (or the
    head's seq when the head does not match the chain); None when all verify.
    Every dimension set a line names is checked against its digest on the way."""
    p = m.posting
    prev, last_seq = GENESIS, 0
    for row in conn.execute(select(p).where(p.c.company_id == company_id).order_by(p.c.seq)).mappings():
        if row["seq"] != last_seq + 1 or row["prev_hash"] != prev or _posting_hash(conn, row, prev) != row["hash"]:
            return row["seq"]
        prev, last_seq = row["hash"], row["seq"]
    head = conn.execute(select(m.posting_chain_head).where(
        m.posting_chain_head.c.company_id == company_id)).mappings().first()
    if head is None or head["last_seq"] != last_seq or head["last_hash"] != prev:
        return (head or {}).get("last_seq", last_seq)
    return None


def trial_balance_difference(ctx: Ctx) -> Decimal:
    rows = ctx.conn.execute(select(m.ledger_entry.c.debit, m.ledger_entry.c.credit).where(
        m.ledger_entry.c.company_id == ctx.company_id)).all()
    return sum((r.debit - r.credit for r in rows), ZERO)


def balance(ctx: Ctx, account_id: str, as_of: date) -> Decimal:
    """Debit minus credit on an account, from postings dated on or before
    `as_of` (a later reversal does not change an earlier as-of balance)."""
    le, p = m.ledger_entry, m.posting
    rows = ctx.conn.execute(select(le.c.debit, le.c.credit).join(p, and_(p.c.id == le.c.posting_id)).where(
        le.c.company_id == ctx.company_id, le.c.account_id == account_id, p.c.posting_date <= as_of)).all()
    return sum((r.debit - r.credit for r in rows), ZERO)


def close_year(ctx: Ctx, fiscal_year_id: str) -> PostingResult | None:
    """Close a fiscal year. Locks, in this
    fixed order: the chain head, the year, its periods by date. Writes the
    closing posting (none when every income and expense balance is zero),
    closes the periods and the year, and records the close; or refuses and
    writes nothing."""
    fy_t, fp = m.fiscal_year, m.fiscal_period
    _lock_head(ctx)
    fy = ctx.conn.execute(_for_update(ctx, select(fy_t).where(
        fy_t.c.id == fiscal_year_id, fy_t.c.company_id == ctx.company_id))).mappings().first()
    if fy is None:
        raise Refusal("That fiscal year is not in this company's books.", "no_year")
    if fy["status"] != "open":
        raise Refusal(f"Fiscal year {fy['label']} is already closed.", "year_closed")
    if ctx.conn.execute(select(fy_t.c.id).where(fy_t.c.company_id == ctx.company_id, fy_t.c.status != "closed",
                                                fy_t.c.start_date < fy["start_date"]).limit(1)).first():
        raise Refusal("An earlier fiscal year is still open; years close in order.", "earlier_year_open")
    periods = [dict(r) for r in ctx.conn.execute(_for_update(ctx, select(fp).where(
        fp.c.fiscal_year_id == fiscal_year_id).order_by(fp.c.start_date))).mappings()]
    not_locked = [r for r in periods if r["status"] != "locked"]
    if not_locked:
        r = not_locked[0]
        raise Refusal(f"Lock every period of the year first: the period starting {r['start_date']:%d %b %Y} "
                      f"is {r['status']}.", "periods_not_locked")
    if not ctx.conn.execute(select(fy_t.c.id).where(fy_t.c.company_id == ctx.company_id,
                                                    fy_t.c.start_date > fy["end_date"]).limit(1)).first():
        raise Refusal("Create the next fiscal year first; it is where the year carries forward.", "no_next_year")
    re_rows = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
                                                       m.account.c.account_type == "retained_earnings")).mappings().all()
    if len(re_rows) != 1:
        raise Refusal("The company needs exactly one retained earnings account to close a year.",
                      "retained_earnings")
    re_acc = re_rows[0]
    if re_acc["root_type"] != "equity" or re_acc["is_group"] or re_acc["frozen"]:
        raise Refusal("The retained earnings account must be an equity account, not a group and not frozen.",
                      "retained_earnings")
    if verify_chain(ctx.conn, ctx.company_id) is not None:
        raise Refusal("The books' chain does not verify; a year is not closed on books that fail it.", "chain_broken")
    if trial_balance_difference(ctx) != 0:
        raise Refusal("The trial balance does not balance; a year is not closed on it.", "trial_balance")
    le, p, acc = m.ledger_entry, m.posting, m.account
    rows = ctx.conn.execute(select(acc.c.id, acc.c.number, acc.c.name, acc.c.frozen, le.c.debit, le.c.credit)
                            .join(le, le.c.account_id == acc.c.id).join(p, p.c.id == le.c.posting_id)
                            .where(acc.c.company_id == ctx.company_id, acc.c.root_type.in_(("income", "expense")),
                                   p.c.posting_date <= fy["end_date"])).all()
    balances: dict[str, Decimal] = {}
    names: dict[str, tuple] = {}
    for r in rows:
        balances[r.id] = balances.get(r.id, ZERO) + r.debit - r.credit
        names[r.id] = (r.number, r.name, r.frozen)
    lines: list[Line] = []
    for aid in sorted(balances, key=lambda a: names[a][0]):
        bal = balances[aid]
        if bal == 0:
            continue
        number, name, frozen = names[aid]
        if frozen:
            raise Refusal(f"{number} {name} is frozen and has a balance; the year cannot close past it.",
                          "frozen_account")
        lines.append(Line(aid, credit=bal) if bal > 0 else Line(aid, debit=-bal))
    net = sum(balances.values(), ZERO)  # debit minus credit: negative is a profit
    if net != 0:
        lines.append(Line(re_acc["id"], credit=-net) if net < 0 else Line(re_acc["id"], debit=net))
    result = None
    if lines:
        result = _post(ctx, fy["end_date"], CLOSING, fy["id"], lines, memo=f"closes fiscal year {fy['label']}",
                       operation_key=None, reverses=None, closing=_Closing(fy["id"]))
    ctx.conn.execute(update(fp).where(fp.c.fiscal_year_id == fiscal_year_id).values(status="closed"))
    ctx.conn.execute(update(fy_t).where(fy_t.c.id == fiscal_year_id).values(status="closed"))
    ctx.conn.execute(m.fiscal_year_close.insert().values(
        company_id=ctx.company_id, fiscal_year_id=fiscal_year_id,
        closing_posting_id=result.posting_id if result else None, closed_by=ctx.actor, closed_at=now()))
    return result

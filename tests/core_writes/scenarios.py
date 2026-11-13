"""Core writes approved over real books with an existing ledger."""
from sqlalchemy import select

from erpclaw_core import models as m
from erpclaw_core.administration import ensure_owner
from erpclaw_core.common import new_id, now
from erpclaw_core.setup import _chart


def add_customer(request, engine):
    return ({"name": "Juniper Instruments", "email": "orders@juniper.example", "city": "Madison"},
            "Add Juniper Instruments, orders@juniper.example, in Madison as a customer.")


def add_supplier(request, engine):
    return ({"name": "Juniper Supplies", "email": "orders@juniper.example", "city": "Madison"},
            "Add Juniper Supplies, orders@juniper.example, in Madison as a supplier.")


def add_cash_account(request, engine):
    return ({}, "Add a cash account.")


def set_up_company(request, engine):
    """A second company in the bootstrap's pending state; the demo company retains its real postings."""
    with engine.begin() as conn:
        assert conn.execute(select(m.posting.c.id)).first()
        assert conn.execute(select(m.ledger_entry.c.id)).first()
        company = new_id()
        conn.execute(m.company.insert().values(id=company, name=None, functional_currency=None,
            fiscal_year_start_month=None, date_order=None, status="pending", created_at=now()))
        _chart(conn, company)
        ensure_owner(conn, company)
    return ({"name": "Juniper Trading", "currency": "US dollars", "fiscal_year_start_month": 1, "date_order": "MDY"},
            "Set up Juniper Trading in US dollars, with a January financial year and month-first dates.")


def resend_message(request, engine):
    from test_resend_message import seed_message

    oid = seed_message(engine)
    return {"message": oid}, f"Send message {oid} again"


NO_MONEY_SCENARIOS = {"add_customer": add_customer, "add_supplier": add_supplier, "add_cash_account": add_cash_account,
                      "set_up_company": set_up_company, "resend_message": resend_message}


def record_opening_position(request, engine):
    """Bank and cash figures; the caller proposes and approves the opening through the write path."""
    from erpclaw_core import approvals
    from erpclaw_core.plugins import REGISTRY

    card = approvals.propose(engine, REGISTRY.contracts["add_cash_account"], {})
    done = approvals.approve(engine, card.approval_id, card.digest)
    assert done.status == "committed", done
    return ({"bank_lines": [{"account": "1100", "amount": "1234.56"},
                            {"account": "Cash on Hand", "amount": "78.91"}]},
            "Record an opening position with 1234.56 in Bank and 78.91 in Cash on Hand.")


MONEY_SCENARIOS = {"record_opening_position": record_opening_position}


def stored_opening_position(engine, out, before):
    """Read the committed posting and its ordered ledger lines independently of the card."""
    from bookdiff import changes, snapshot

    with engine.connect() as conn:
        posting = dict(conn.execute(select(m.posting).where(
            m.posting.c.operation_key == out.operation_key)).mappings().one())
        lines = [dict(row) for row in conn.execute(select(m.ledger_entry).where(
            m.ledger_entry.c.posting_id == posting["id"]).order_by(m.ledger_entry.c.line_no)).mappings()]
        accounts = {row["id"]: dict(row) for row in conn.execute(select(m.account).where(
            m.account.c.company_id == posting["company_id"])).mappings()}
    return {"posting": posting, "lines": lines, "accounts": accounts,
            "ledger": changes(before, snapshot(engine)).get("ledger_entry", {}).get("added", [])}


def opening_position_value_problems(card, texts, stored):
    """The opening card's figures and result, compared with the persisted bank and equity entries."""
    from collections import Counter
    from decimal import Decimal

    equity = "Opening balance (owner's equity)"
    posting, lines, accounts = stored["posting"], stored["lines"], stored["accounts"]
    when = posting["posting_date"]
    dated = f"{when.day} {when:%b %Y}"
    currency = lines[0]["currency"]
    amount = lambda value: f"{Decimal(str(value)):,.2f}"
    debits = [line for line in lines if Decimal(str(line["debit"]))]
    total = sum((Decimal(str(line["debit"])) for line in debits), Decimal(0))
    expected = [("Date", f"{dated} (when these books start)")]
    expected += [(accounts[line["account_id"]]["name"], f"{amount(line['debit'])} {currency}") for line in debits]
    expected += [("Total starting value", f"{amount(total)} {currency}"),
                 ("Total owed to suppliers", f"{amount(0)} {currency}"),
                 (equity, f"what the business owns minus what it owes, at the start: {amount(total)} {currency}")]
    books = []
    for line in lines:
        account = accounts[line["account_id"]]
        credit = Decimal(str(line["credit"]))
        name = equity if credit else account["name"]
        side, value = ("credit", credit) if credit else ("debit", line["debit"])
        books.append(f"{account['number']} {name}: {side} {amount(value)} {currency}")
    problems = []
    shown = [(k, v) for k, v in card["lines"] if k != "Company"]
    if shown != expected:
        problems.append(f"card lines: shown {shown}, stored {expected}")
    if Counter(card["books"]) != Counter(books):
        problems.append(f"books: shown {card['books']}, stored {books}")
    result = (f"Recorded the opening position dated {dated}: total starting value {amount(total)} {currency}, "
              f"total owed to suppliers {amount(0)} {currency}, {equity.lower()} {amount(total)} {currency}.")
    for text in texts:
        if text != result:
            problems.append(f"text: shown {text}, stored {result}")
    return problems

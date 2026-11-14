"""Public write contract tests for reversing a received delivery.

Covers registration as a stock write, owner-word delivery-number input,
propose by digest, commit under an engine-minted key, replay, every preview
refusal, exact mirror lines for both settlements, chat reachability, catalogue
routing with the receipt noun phrases, the condition (1) negatives, and the
shared regressions.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from bookdiff import snapshot
from test_supplier_payment_reversal_contract import assert_changes, assert_reversal_diff
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from erpclaw_core import (
    administration as admin,
    approvals,
    catalogue as cat,
    ledger,
    masters,
    models as m,
    payables,
)
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core.payable_tables import supplier_payable
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_core.setup import create_company
from erpclaw_stock import PLUGIN, reverse_goods_received as backend
from erpclaw_stock import tables as stock_tables, service as stock

CONTRACT = "reverse_goods_received"
NUMBER = "GR-2026-00001"
TOTAL = Decimal("1450.00")
QTY = Decimal("200")
ITEM_DAY = date(2026, 10, 10)
ORIGINAL_DAY = date(2026, 10, 20)
VOUCHER = "stock_goods_received"


@pytest.fixture(autouse=True)
def reversal_activation(monkeypatch, every_feature):
    for name in ("reverse_supplier_payment", "reverse_goods_received"):
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", None)


def contract():
    """The registered stock reversal contract under test."""
    return REGISTRY.contracts[CONTRACT]


def propose(engine, args, **kwargs):
    return approvals.propose(engine, contract(), args, **kwargs)


def tool(args):
    return ToolCall("call_reverse_goods_received", CONTRACT, args, json.dumps(args))


def chat_client(engine, args, prefix=()):
    contract()
    model = ScriptedModel([*prefix, *[Reply("", [tool(args)]) for _ in range(4)]])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    return client, cid


def message(client, cid, text="", choice=None):
    body = {"text": text} if choice is None else {"text": choice["label"], "choice": choice["code"]}
    response = client.post(f"/v1/conversations/{cid}/messages", json=body)
    assert response.status_code == 200, response.text
    return response.json()["events"]


def approve_http(client, card):
    aid = card["approval_id"] if isinstance(card, dict) else card.approval_id
    digest = card["digest"] if isinstance(card, dict) else card.digest
    response = client.post(f"/v1/cards/{aid}/approve", json={"digest": digest})
    assert response.status_code == 200, response.text
    return response.json()["events"]


def live_company(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        return {"id": ctx.company_id, "currency": ctx.currency, "today": ctx.today}


def ensure_account(engine, number, name, account_type):
    """Use the chart seeded by company setup; these books need no extra accounts."""
    with engine.connect() as conn:
        return ledger.account_by_number(company_ctx(conn), number)


def bank_account(engine):
    return ensure_account(engine, "1100", "Bank", "bank")


def period_on(engine, day):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        row = conn.execute(
            select(m.fiscal_period).where(
                m.fiscal_period.c.company_id == ctx.company_id,
                m.fiscal_period.c.start_date <= day,
                m.fiscal_period.c.end_date >= day,
            )
        ).mappings().first()
        assert row is not None, f"no fiscal period contains {day}"
        return dict(row)


def max_posting_seq(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        rows = conn.execute(
            select(m.posting.c.seq).where(m.posting.c.company_id == ctx.company_id)
        ).scalars().all()
        return max(rows) if rows else 0


def seed_delivery(
    engine,
    *,
    tag="",
    number=NUMBER,
    total=TOTAL,
    qty=QTY,
    settlement="still_owed",
    posting_date=ORIGINAL_DAY,
    owed_item=True,
):
    """Insert a fully consistent received delivery, mirroring the unit-test rows.

    Voucher plus header, supplier, stock item in its warehouse with head, entry
    the delivery posting with ledger lines, and the untouched payable
    for the still-owed settlement. Returns the ids and figures tests assert on.
    """
    prefix = f"{tag}-" if tag else ""
    ids = {
        "voucher": prefix + "gr", "header": prefix + "header", "item": prefix + "item",
        "stock_item": prefix + "stock-item", "warehouse": prefix + "wh",
        "head": prefix + "head", "entry": prefix + "entry",
        "posting": prefix + "posting", "payable": prefix + "payable",
    }
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        supplier_id = conn.execute(select(m.party.c.id).where(
            m.party.c.company_id == ctx.company_id, m.party.c.kind == "supplier",
            m.party.c.name == "Copperline Metals")).scalar_one_or_none()
        if supplier_id is None:
            supplier_id = masters.create_party(ctx, "supplier", "Copperline Metals", city="Leeds")
        bank = ledger.account_by_number(ctx, "1100")
        ap = ledger.account_by_number(ctx, "2100")
        inventory = ledger.account_by_number(ctx, "1300")
        counter = bank if settlement == "paid_now" else ap
        # No public forward card exists yet. These document headers mirror
        # test_reverse_goods_received_unit.Recording; movements use stock's writer.
        conn.execute(stock_tables.voucher.insert().values(
            id=ids["voucher"], company_id=ctx.company_id, kind="goods_received",
            number=number, posting_date=posting_date, reason=None, created_at=now()))
        conn.execute(stock_tables.goods_received.insert().values(
            id=ids["header"], company_id=ctx.company_id, voucher_id=ids["voucher"],
            supplier_id=supplier_id, settlement=settlement, account_id=counter["id"], number=number))
        ids["item"] = masters.create_item(ctx, "Copper Wire " + prefix if prefix else "Reversal Copper Wire",
                                          Decimal("7.25"), unit="m")
        ids["stock_item"] = stock.make_stock_item(ctx, ids["item"], "moving_average")
        ids["warehouse"] = stock.create_warehouse(ctx, "Main " + prefix if prefix else "Main")
        move = stock._receipt_move(ctx, ids["item"], ids["warehouse"], qty, total / qty, posting_date, lock=True)
        ids["entry"] = stock._entry_id(ctx, move, posting_date, VOUCHER, ids["voucher"])
        ids["head"] = stock.head(ctx, ids["item"], ids["warehouse"])["id"]
        ids["posting"] = ledger.post(ctx, posting_date, VOUCHER, ids["voucher"], [
            ledger.Line(inventory["id"], debit=total),
            ledger.Line(counter["id"], credit=total,
                        party_id=supplier_id if settlement == "still_owed" else None)]).posting_id
        if settlement == "still_owed" and owed_item:
            conn.execute(supplier_payable.insert().values(
                id=ids["payable"], company_id=ctx.company_id, supplier_id=supplier_id,
                source_type=VOUCHER, source_id=ids["voucher"], source_number=number,
                posting_date=posting_date, currency=ctx.currency, amount=total,
                outstanding=total, status="open", created_at=now()))

    return {
        "supplier_id": supplier_id, "bank": bank, "ap": ap, "inventory": inventory,
        "counter": counter, "number": number, "total": total, "settlement": settlement,
        **ids,
    }


def close_period(engine, day):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(
            m.fiscal_period.update().where(
                m.fiscal_period.c.company_id == ctx.company_id,
                m.fiscal_period.c.start_date <= day,
                m.fiscal_period.c.end_date >= day,
            ).values(status="closed")
        )


def principal(engine, company_id, name, permissions):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, permissions, [company_id])
        return pr.authenticate(conn, token), token


def test_contract_registered_as_stock_write_with_money_and_tool_schema(engine):
    """Pins the public registration: stock write, money-moving, owner-word schema.

    Fails if the contract is unregistered, owned elsewhere, not a write, not
    money-moving, planned off, or its tool schema takes ids or a reason.
    """
    assert contract().name == backend.NAME == CONTRACT
    assert contract().plugin == "stock" and REGISTRY.contract_plugin[CONTRACT] == "stock"
    assert contract().effect == "write" and contract().moves_money is True
    assert contract().planned is None
    assert PLUGIN.manifest.contracts[CONTRACT] == "write"
    assert contract().read_set is not None and contract().find_by_operation_key is not None
    props = contract().tool_schema()["function"]["parameters"]
    assert set(props["properties"]) == {"delivery", "date"}
    assert props["required"] == ["delivery"]
    assert "voucher_id" not in json.dumps(props)


def test_owner_names_delivery_by_number_never_id(engine):
    """Pins that the input takes the stored GR NUMBER, never an engine id.

    Fails if the input accepts voucher_id, stores an id in the card args, or
    shows an id instead of the number on the card.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER})
    assert card.card["title"] == "Reverse goods received"
    assert ["Delivery", f"{NUMBER} from Copperline Metals"] in card.card["lines"]
    assert NUMBER in json.dumps(card.card)
    with engine.connect() as conn:
        stored = conn.execute(
            select(m.approval).where(m.approval.c.id == card.approval_id)
        ).mappings().one()
        args = json.loads(stored["args_json"])
    assert set(args) == {"delivery", "date"}
    assert args["delivery"] == NUMBER
    assert "voucher_id" not in set(args)
    with pytest.raises(ValidationError):
        propose(engine, {"delivery": NUMBER, "voucher_id": "model-picked-id"})
    with pytest.raises(ValidationError):
        propose(engine, {"delivery": NUMBER, "reason": "Recorded twice"})


def test_trailing_digits_resolve_when_unique(engine):
    """Pins trailing-digit resolution for a number ending in exactly one delivery.

    Fails if the resolver demands the exact full number.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": "00001"})
    assert ["Delivery", f"{NUMBER} from Copperline Metals"] in card.card["lines"]


def test_ambiguous_tail_asks_with_options(engine):
    """Pins that a tail ending in several deliveries asks instead of guessing.

    Fails if the resolver silently binds the first match and makes a card.
    """
    seed_delivery(engine)
    seed_delivery(engine, tag="b", number="GR-2025-00001")
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": "00001"})
    assert err.value.code == "delivery_ambiguous"
    assert {NUMBER, "GR-2025-00001"} <= set(err.value.options)
    assert_changes(before, snapshot(engine), {})


def test_unknown_number_refused_plain_sentence(engine):
    """Pins the unknown-number refusal with no card and no writes.

    Fails if an unknown number previews a card or writes.
    """
    seed_delivery(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": "GR-2026-00999"})
    assert err.value.code == "delivery_unknown"
    assert err.value.message == "There is no such delivery in these books."
    assert_changes(before, snapshot(engine), {})


def test_foreign_company_number_refused_plain_sentence(engine):
    """Pins that a number from another company is not found in these books.

    Fails if company scoping leaks the other company's delivery.
    """
    seed_delivery(engine)
    create_company(engine, "Other Co")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Co").company_id
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER}, company_id=other)
    assert err.value.code == "delivery_unknown"
    assert err.value.message == "There is no such delivery in these books."
    assert_changes(before, snapshot(engine), {})


def test_propose_approve_commit_still_owed_with_minted_key(engine):
    """Pins the full still-owed move: card, digest approval, payable closed.

    Fails if any write is dropped: the stock stays, the mirror posting is
    missing, the payable stays open, or the outcome is not stored under the
    engine-minted key.
    """
    seed = seed_delivery(engine)
    co = live_company(engine)
    before_card = snapshot(engine)
    card = propose(engine, {"delivery": NUMBER})
    assert card.operation_key and card.operation_key != card.approval_id
    assert len(card.digest) == 64
    assert card.card["title"] == "Reverse goods received"
    assert ["Total reversed", f"1,450.00 {co['currency']}"] in card.card["lines"]
    assert ["Settlement", "Still owed to Copperline Metals; this delivery's untouched payable will be closed."] in card.card["lines"]
    assert ["Delivery payable", f"1,450.00 {co['currency']} outstanding becomes 0.00 {co['currency']}; status reversed."] in card.card["lines"]
    assert "Copper Wire" in json.dumps(card.card) and "Main" in json.dumps(card.card)
    assert ["Original date", f"{ORIGINAL_DAY.day} {ORIGINAL_DAY:%b %Y}"] in card.card["lines"]
    assert ["Reversal date", f"{co['today'].day} {co['today']:%b %Y}"] in card.card["lines"]
    assert card.card["footer"].endswith("Valid 10 minutes. Rechecked before saving.")
    before = snapshot(engine)
    stored = before["approval"][(card.approval_id,)]
    assert_changes(before_card, before, {"approval": {"added": [stored]}})
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    assert result.text == (
        f"Reversed goods received {NUMBER}, dated {co['today'].day} {co['today']:%b %Y}: "
        f"1,450.00 {co['currency']}."
    )
    assert result.figures == ["1,450.00"]
    assert result.data["number"] == NUMBER and result.data["voucher_id"] == "gr"
    assert result.data["voucher_type"] == VOUCHER + ":reversal"
    after = snapshot(engine)
    reversal_posting = [
        r for r, row in after["posting"].items() if row["reverses_posting_id"] == seed["posting"]
    ]
    assert len(reversal_posting) == 1
    reversal_id = reversal_posting[0][0]
    assert_reversal_diff(before, after, card, result, seed, delivery=True)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        head = conn.execute(
            select(stock_tables.head).where(
                stock_tables.head.c.company_id == ctx.company_id,
                stock_tables.head.c.id == seed["head"],
            )
        ).mappings().one()
        assert head["qty"] == 0 and head["value"] == 0 and head["last_seq"] == 2
        entries = conn.execute(
            select(stock_tables.entry).where(
                stock_tables.entry.c.company_id == ctx.company_id,
                stock_tables.entry.c.reverses_entry_id == seed["entry"],
            )
        ).mappings().all()
        assert len(entries) == 1 and entries[0]["voucher_id"] == "gr"
    assert approvals.read_back(engine, card.operation_key).data == result.data


def test_paid_now_restores_bank_and_touches_no_payable(engine):
    """Pins the paid-now move: bank restored, no payable row read or written.

    Fails if a payable is opened, read, or changed for a paid-now delivery.
    """
    seed = seed_delivery(engine, settlement="paid_now")
    card = propose(engine, {"delivery": NUMBER})
    assert "Delivery payable" not in dict(card.card["lines"])
    assert f"Paid from {seed['bank']['name']} ({seed['bank']['number']})" in json.dumps(card.card)
    before = snapshot(engine)

    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    after = snapshot(engine)
    assert_reversal_diff(before, after, card, result, seed, delivery=True)
    assert before["supplier_payable"] == after["supplier_payable"]
    reversal_posting = [
        r for r, row in after["posting"].items() if row["reverses_posting_id"] == seed["posting"]
    ]
    assert len(reversal_posting) == 1
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        lines = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == reversal_posting[0][0],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
    assert [(r["account_id"], r["debit"], r["credit"], r["party_id"]) for r in lines] == [
        (seed["inventory"]["id"], Decimal("0"), TOTAL, None),
        (seed["bank"]["id"], TOTAL, Decimal("0"), None),
    ]


def test_default_date_is_today(engine):
    """Pins that an omitted date reverses today.

    Fails if the default is not today.
    """
    seed_delivery(engine)
    co = live_company(engine)
    card = propose(engine, {"delivery": NUMBER})
    assert ["Reversal date", f"{co['today'].day} {co['today']:%b %Y}"] in card.card["lines"]
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    assert result.data["posting_date"] == co["today"].isoformat()


def test_explicit_date_with_date_source_posts_it(engine):
    """Pins that an owner-given date is posted and quoted as its source.

    Fails if the input date is ignored or the date-source line is missing.
    """
    seed_delivery(engine)
    day = date(2026, 10, 22)
    card = propose(
        engine, {"delivery": NUMBER, "date": day},
        date_source={"phrase": "22 Oct 2026", "date": "2026-10-22"},
        owner_words="Reverse GR-2026-00001 on 22 Oct 2026",
    )
    assert ["Reversal date", "22 Oct 2026"] in card.card["lines"]
    assert ["Date source", "From your words: '22 Oct 2026' (22 Oct 2026)"] in card.card["lines"]
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"


def test_mirror_lines_equal_original_exactly(engine):
    """Pins that the reversal posting mirrors the original line for line.

    Fails if an account, amount, side or party differs, or an original line moves.
    """
    seed = seed_delivery(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        before_lines = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == seed["posting"],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
        before_values = [
            (r["account_id"], r["debit"], r["credit"], r["party_id"], r["currency"]) for r in before_lines
        ]
    card = propose(engine, {"delivery": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        reversal = conn.execute(
            select(m.posting).where(
                m.posting.c.company_id == ctx.company_id,
                m.posting.c.reverses_posting_id == seed["posting"],
            )
        ).mappings().one()
        after_lines = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == reversal["id"],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
        same_lines = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == seed["posting"],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
    assert {(r["account_id"], r["credit"], r["debit"], r["party_id"], r["currency"]) for r in after_lines} == set(before_values)
    assert [(r["account_id"], r["debit"], r["credit"], r["party_id"]) for r in same_lines] == [
        (r["account_id"], r["debit"], r["credit"], r["party_id"]) for r in before_lines
    ]


def test_replay_returns_stored_result_writes_nothing(engine, monkeypatch):
    """Pins that the same key and digest replay the stored outcome.

    Fails if a replay re-runs commit instead of returning the stored result.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER})
    first = approvals.approve(engine, card.approval_id, card.digest)
    assert first.status == "committed"
    before = snapshot(engine)

    def cannot_commit(*args):
        pytest.fail("A consumed card must return its stored outcome without re-running commit")

    monkeypatch.setattr(contract(), "commit", cannot_commit)
    assert approvals.approve(engine, card.approval_id, card.digest).text == first.text
    assert approvals.read_back(engine, card.operation_key).data == first.data
    assert_changes(before, snapshot(engine), {})


def test_consumed_card_rejects_another_digest(engine):
    """Pins digest binding on a consumed card.

    Fails if a different digest is accepted after commit.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.approve(engine, card.approval_id, "0" * 64)
    assert err.value.code == "digest"
    assert_changes(before, snapshot(engine), {})


def test_already_reversed_refused(engine):
    """Pins the already-reversed refusal with its exact sentence.

    Fails if a second reversal of the same delivery previews a card.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER})
    assert err.value.code == "already_reversed"
    assert err.value.message == f"{NUMBER} is already reversed."
    assert_changes(before, snapshot(engine), {})


def test_reversal_before_original_refused(engine):
    """Pins the backdated refusal with its exact sentence.

    Fails if a reversal dated before its delivery previews a card.
    """
    seed_delivery(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER, "date": date(2026, 10, 19)})
    assert err.value.code == "reversal_before_original"
    assert err.value.message == "A reversal cannot be dated before its delivery."
    assert_changes(before, snapshot(engine), {})


def test_future_reversal_refused(engine):
    """Pins the future-date refusal with its exact sentence.

    Fails if a reversal dated after today previews a card.
    """
    seed_delivery(engine)
    co = live_company(engine)
    before = snapshot(engine)
    future = date.fromordinal(co["today"].toordinal() + 1)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER, "date": future})
    assert err.value.code == "future_date"
    assert err.value.message == "A reversal can't be dated in the future."
    assert_changes(before, snapshot(engine), {})


def test_closed_reversal_period_refused(engine):
    """Pins the closed-period refusal in the posting service's words.

    Fails if a reversal in a closed period previews a card.
    """
    seed_delivery(engine)
    co = live_company(engine)
    close_period(engine, co["today"])
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER})
    assert err.value.code == "period_closed"
    assert err.value.message == f"The period containing {co['today'].isoformat()} is closed."
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("change,code", [
    ({"outstanding": Decimal("1150.00")}, "payable_touched"),
    ({"status": "paid", "outstanding": Decimal("0.00")}, "payable_touched"),
    ({"status": "reversed", "outstanding": Decimal("0.00")}, "payable_reversed"),
])
def test_touched_or_ended_payable_refuses(engine, change, code):
    """Pins that a touched or ended payable blocks the delivery reversal.

    Fails if a payable that a payment has reached still previews a card.
    """
    seed_delivery(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(
            supplier_payable.update().where(
                supplier_payable.c.company_id == ctx.company_id,
                supplier_payable.c.id == "payable",
            ).values(**change)
        )
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER})
    assert err.value.code == code
    if code == "payable_touched":
        assert err.value.message == f"{NUMBER} has a payment against it; reverse the payment first."
    assert_changes(before, snapshot(engine), {})


def test_missing_owed_item_refuses(engine):
    """Pins that a still-owed delivery without its payable cannot reverse.

    Fails if a missing payable silently defaults to a paid-now reversal.
    """
    seed_delivery(engine, owed_item=False)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"delivery": NUMBER})
    assert err.value.code == "no_payable"
    assert_changes(before, snapshot(engine), {})


def test_commit_without_approval_refused(engine):
    """Pins the commit gate: no approved card, no reversal.

    Fails if the backend commits outside its registered card's commit.
    """
    seed_delivery(engine)
    before = snapshot(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        inp = contract().input_model(delivery=NUMBER)
        with pytest.raises(Refusal) as err:
            contract().commit(ctx, inp, "unapproved-key")
    assert err.value.code == "not_approved"
    assert_changes(before, snapshot(engine), {})


def test_chat_routes_reversal_words_to_card_and_commits(engine):
    """Pins chat reachability: reversal words propose the card, approval commits.

    Fails if the words still give a planned reply instead of the reversal card.
    """
    seed = seed_delivery(engine)
    client, cid = chat_client(
        engine, {"delivery": NUMBER},
        prefix=[Reply("", [call("find_capability", request="reverse goods received")]),
                Reply("", [call("choose_capability", pick="op.reverse_goods_received")])],
    )
    before = snapshot(engine)
    [card] = message(client, cid, f"Reverse goods received {NUMBER}")
    assert card["type"] == "card" and card["card"]["title"] == "Reverse goods received", card
    assert ["Delivery", f"{NUMBER} from Copperline Metals"] in card["card"]["lines"]
    from types import SimpleNamespace

    before = snapshot(engine)
    events = approve_http(client, card)
    assert events[-1]["status"] == "committed", events
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        status = conn.execute(
            select(supplier_payable.c.status).where(
                supplier_payable.c.company_id == ctx.company_id,
                supplier_payable.c.id == "payable",
            )
        ).scalar_one()
    assert status == "reversed"
    with engine.connect() as conn:
        stored = conn.execute(select(m.approval).where(m.approval.c.id == card["approval_id"])).mappings().one()
    bound = SimpleNamespace(approval_id=card["approval_id"], digest=card["digest"], operation_key=stored["operation_key"])
    result = approvals.read_back(engine, stored["operation_key"], ownership_check=lambda conversation: conversation == cid)
    assert_reversal_diff(before, snapshot(engine), bound, result, seed, delivery=True)


@pytest.mark.xfail(strict=True, reason="awaiting the reversal routing: delivery reference selects the goods reversal")
def test_reversal_phrase_routes_only_to_goods_reversal(engine):
    """Pins reversal-only routing for the documented delivery phrase.

    Fails if the phrase keeps a planned reply or names a forward override.
    """
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
        target = entries["op.reverse_goods_received"]
        assert target.state == "installed"
        assert target.plugin == "stock" and target.target == CONTRACT
        said = "reverse goods received GR-2026-00003"
        named = cat.named_by_synonym(conn, said)
        assert "op.reverse_goods_received" in {e.id for e, _ in named}
        found = cat.find_capability(conn, said)
        expected = "a"
        decision = cat.decide(found, "planned.stock", named=named)
        assert decision.entry.id == "op.reverse_goods_received"
        assert decision.outcome == expected
        direct = cat.decide(found, "op.reverse_goods_received", named=named)
        assert direct.entry.id == "op.reverse_goods_received"
        assert direct.outcome == expected


@pytest.mark.parametrize("said", [
    "reverse the goods receipt",
    "reverse the stock receipt",
    "undo the delivery",
])
@pytest.mark.xfail(strict=True, reason="awaiting the reversal routing: receipt words select the goods reversal")
def test_receipt_noun_phrases_route_only_to_goods_reversal(engine, said):
    """Pins noun-phrase routing without a document reference, reversals only.

    Fails if a noun phrase keeps a planned reply or reaches a forward override.
    """
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
        target = entries["op.reverse_goods_received"]
        named = cat.named_by_synonym(conn, said)
        assert "op.reverse_goods_received" in {e.id for e, _ in named}
        expected = "a"
        decision = cat.decide(cat.find_capability(conn, said), "planned.stock", named=named)
        assert decision.entry.id == "op.reverse_goods_received"
        assert decision.outcome == expected
        assert not any(e.id == "op.record_goods_received" for e, _ in named)


def test_delivery_phrase_without_reversal_word_never_names_reversal(engine):
    """Pins that delivery words without a reversal word never reach the reversal.

    Fails if the reversal predicate fires without a reversal word.
    """
    with engine.connect() as conn:
        said = "receive the delivery into stock"
        named = cat.named_by_synonym(conn, said)
        assert "op.reverse_goods_received" not in {e.id for e, _ in named}
        decision = cat.decide(cat.find_capability(conn, said), "planned.stock", named=named)
        assert decision.outcome == "c" and decision.capability == "planned.stock"


def test_planned_stock_synonym_inventory(engine):
    """Pins the generated receipt vocabulary on the planned stock area.

    Fails if generation drops the receipt phrases selected by the negative
    cases, adds their bare forms, or loses the long-standing stock synonyms.
    """
    with engine.connect() as conn:
        areas = {a["id"]: a for a in cat._planned()}
        synonyms = areas["planned.stock"]["synonyms"]
    for word in ("goods receipt", "stock receipt", "delivery of goods", "goods delivered", "undo the delivery"):
        assert word in synonyms, word
    assert not {"receipt", "delivery", "delivered"} & set(synonyms)
    for word in ("stock", "inventory", "warehouse", "stock level"):
        assert word in synonyms, word


@pytest.mark.skip(reason="record_goods_received is not registered on this base; enable when it registers")
def test_full_matrix_noun_phrases_with_forwards_enabled(engine):
    """Pins noun-phrase precedence with forwards and reversals enabled together.

    Fails if a noun phrase reaches a forward override or the planned reply once
    both families are installed.
    """
    with engine.connect() as conn:
        assert "record_goods_received" in REGISTRY.contracts
        for said in ("reverse the goods receipt", "reverse the stock receipt", "undo the delivery"):
            named = cat.named_by_synonym(conn, said)
            decision = cat.decide(cat.find_capability(conn, said), "planned.stock", named=named)
            assert decision.outcome == "a"
            assert decision.entry.id == "op.reverse_goods_received"


@pytest.mark.skip(reason="record_goods_received is not registered on this base; enable when it registers")
def test_forwards_only_partial_activation_keeps_reversal_planned(engine):
    """Pins that noun phrases keep the planned reply with only forwards on.

    Fails if a reversal noun phrase routes to a forward when reversals are off.
    """
    with engine.connect() as conn:
        assert "record_goods_received" in REGISTRY.contracts
        for said in ("reverse the goods receipt", "reverse the stock receipt", "undo the delivery"):
            named = cat.named_by_synonym(conn, said)
            assert "op.record_goods_received" not in {e.id for e, _ in named}
            decision = cat.decide(cat.find_capability(conn, said), "planned.stock", named=named)
            assert decision.outcome == "c" and decision.capability == "planned.stock"


def test_registration_gate_planned_when_off(engine, monkeypatch):
    """Pins the catalogue gate: with the contract off, the words stay planned.

    Fails if the reversal routes while its contract is not live.
    """
    monkeypatch.setattr(contract(), "planned", "later")
    with engine.connect() as conn:
        decision = cat.decide(
            cat.find_capability(conn, "reverse goods received GR-2026-00003"), "planned.stock",
            named=cat.named_by_synonym(conn, "reverse goods received GR-2026-00003"))
        assert decision.outcome == "c" and decision.capability == "planned.stock"


@pytest.mark.parametrize("pick", ["planned.messaging", "planned.documents"])
def test_bare_receipt_keeps_present_planned_reply(engine, pick, as_released):
    """Pins condition (1): a bare receipt never reaches a reversal or forward.

    Fails if the bare word steals the request from its present planned reply.
    """
    said = "send a receipt to Pied Piper" if "messaging" in pick else "email the payment receipt"
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        assert not any(e.id in {"op.reverse_supplier_payment", "op.reverse_goods_received"}
                       for e, _ in named)
        assert not any(e.overrides for e, _ in named)
        assert "planned.stock" not in {e.id for e, _ in named}
        decision = cat.decide(cat.find_capability(conn, said), pick, named=named)
        assert decision.outcome == "c" and decision.capability == pick
        assert decision.text == (
            "Plain-text email is not in ERPClaw yet."
            if pick == "planned.messaging"
            else "Documents and attachments is not in ERPClaw yet."
        )
        assert cat.planned_instead(conn, "create_invoice", said) is None
    before = snapshot(engine)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("said", ["when is the delivery", "delivery note"])
def test_bare_delivery_keeps_present_reply(engine, said, as_released):
    """Pins condition (1): delivery words retain their existing catalogue matches.

    Fails if the stock vocabulary steals the present reply and id.
    """
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        assert {e.id for e, _ in named} == ({"op.deliver_order"} if said == "delivery note" else set())
        found = cat.find_capability(conn, said)
        decision = cat.decide(found, "none", named=named)
        if said == "delivery note":
            assert decision.outcome == "c" and decision.capability == "planned.op.deliver_order"
            assert decision.text == "Delivering an order is not in ERPClaw yet."
        else:
            assert decision.outcome == "d" and decision.capability == cat.NO_MATCH
            assert decision.text.startswith("ERPClaw does not do that yet.")

    before = snapshot(engine)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.xfail(strict=True, reason="awaiting the reversal routing: disabled stock maps to the available reversal")
def test_disabled_stock_refused_at_mapping(engine):
    """Pins the mapping gate: with stock disabled, the words stay available.

    Fails if the reversal still maps while its plugin is disabled.
    """
    with engine.begin() as conn:
        disable_plugin(conn, "stock")
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
        assert entries["op.reverse_goods_received"].state == "available"
        said = "reverse goods received GR-2026-00003"
        decision = cat.decide(cat.find_capability(conn, said), "planned.stock",
                              named=cat.named_by_synonym(conn, said))
        assert decision.outcome == "c"
        assert decision.capability == "available.op.reverse_goods_received"


def test_disabled_stock_refused_at_propose(engine):
    """Pins the propose gate: with stock disabled, no card is made.

    Fails if a reversal card is proposed while its plugin is disabled.
    """
    seed_delivery(engine)
    with engine.begin() as conn:
        disable_plugin(conn, "stock")
    before = snapshot(engine)
    with pytest.raises(Refusal):
        propose(engine, {"delivery": NUMBER})
    assert_changes(before, snapshot(engine), {})


def test_disabled_stock_refused_at_commit(engine):
    """Pins the commit gate: disabling before approval ends the card unsaved.

    Fails if the approval still commits while its plugin is disabled.
    """
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER})
    with engine.begin() as conn:
        disable_plugin(conn, "stock")
    before = snapshot(engine)
    outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert outcome.status == "superseded"
    after = snapshot(engine)
    assert_changes(before, after, {"approval": {"changed": [({"id": card.approval_id}, {
        "status": "superseded", "decided_at": after["approval"][(card.approval_id,)]["decided_at"],
        "decision_json": json.dumps({"status": "superseded", "text": outcome.text,
            "code": "plugin_not_enabled", "options": [], "data": {},
            "capability": "available.op.reverse_goods_received"})})]}})


def test_draft_only_cannot_approve_own_card(engine):
    """Pins that a draft-only principal cannot approve its own card.

    Fails if the approve grant check is removed.
    """
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    drafter, token = principal(engine, cid, "drafter", ["view", "draft"])
    seed_delivery(engine)
    card = propose(engine, {"delivery": NUMBER}, principal=drafter)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403 and "approve" in response.text, response.text
    assert_changes(before, snapshot(engine), {})


def test_cross_company_approval_refused(engine):
    """Pins that an approval from another company is refused.

    Fails if the company grant scope check is removed.
    """
    seed_delivery(engine)
    with engine.connect() as conn:
        ours = company_ctx(conn).company_id
    create_company(engine, "Other Co")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Co").company_id
    _, token = principal(engine, other, "other approver", ["view", "draft", "approve", "post"])
    card = propose(engine, {"delivery": NUMBER}, company_id=ours)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403, response.text
    assert_changes(before, snapshot(engine), {})


def test_period_warning_when_reversal_in_later_period(engine):
    """The card names both period beginnings when the reversal falls in a later period."""
    seed_delivery(engine, posting_date=date(2026, 9, 15))
    original = period_on(engine, date(2026, 9, 15))
    reversal = period_on(engine, date(2026, 10, 26))
    card = propose(engine, {"delivery": NUMBER})
    assert (f"{NUMBER} stays in the period beginning {original['start_date']}; "
            f"this reversal is in the period beginning {reversal['start_date']}.") in card.card["warnings"]
    assert ["Original entries", "The original entries stay; reversing entries are added on 2026-10-26."] in card.card["lines"]
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"

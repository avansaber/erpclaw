"""Public write contract tests for reversing a posted supplier payment.

Covers registration as a core write, owner-word number input, propose by digest,
commit under an engine-minted key, replay, every preview refusal, exact mirror
lines, chat reachability, catalogue routing, and the shared regressions.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from bookdiff import changes, snapshot
from erpclaw_core.db import metadata
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
from erpclaw_core import supplier_payment_reversals as backend
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core import core_contracts
from erpclaw_core.payable_tables import (
    supplier_payment,
    supplier_payable,
    supplier_payment_allocation,
)
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_core.setup import create_company

CONTRACT = "reverse_supplier_payment"
NUMBER = "SPAY-2026-00001"
SECOND_NUMBER = "SPAY-2026-00002"
GR_ONE = "GR-2026-00001"
GR_TWO = "GR-2026-00002"
AMOUNT = Decimal("1600.00")
FIRST_ALLOCATION = Decimal("1450.00")
SECOND_ALLOCATION = Decimal("150.00")
ITEM_DAY = date(2026, 10, 10)
ORIGINAL_DAY = date(2026, 10, 20)


def assert_changes(before, after, expected):
    """Compare every full row and column, normalizing values and list order only."""
    def value(v):
        if isinstance(v, Decimal):
            return str(v.normalize())
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if isinstance(v, date):
            return v.isoformat()
        return v

    def row(table, source):
        assert set(source) == set(metadata.tables[table].c.keys()), (table, source)
        out = {}
        for k, v in source.items():
            if v is not None and k in {"amount", "outstanding", "debit", "credit", "functional_debit",
                                      "functional_credit", "rate", "qty", "value", "qty_after", "value_after"}:
                v = Decimal(v)
            if isinstance(v, str) and (k.endswith("_json") or k == "detail"):
                v = json.loads(v)
            out[k] = value(v)
        return out

    want = {}
    for table, kinds in expected.items():
        full = {k: [row(table, r) for r in kinds.get(k, [])] for k in ("added", "removed")}
        full["changed"] = []
        for key, updates in kinds.get("changed", []):
            pk = [c.name for c in metadata.tables[table].primary_key]
            assert set(key) == set(pk)
            old = before[table][tuple(key[c] for c in pk)]
            full["changed"].append([row(table, old), row(table, {**old, **updates})])
        want[table] = full
    got = changes(before, after)
    for table, kinds in got.items():
        for kind in ("added", "removed"):
            kinds[kind] = [row(table, r) for r in kinds[kind]]
        kinds["changed"] = [[row(table, old), row(table, new)] for old, new in kinds["changed"]]
    for diff in (got, want):
        for kinds in diff.values():
            for rows in kinds.values():
                rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
    assert got == want


def assert_reversal_diff(before, after, card, result, seed, *, delivery=False, reason=""):
    """Expected full reversal effects; only minted identities, times and hashes come from the result."""
    aid, key = card.approval_id, card.operation_key
    stored = before["approval"][(aid,)]
    cid = stored["company_id"]
    original_id = seed["posting"] if delivery else seed["posting_id"]
    original = before["posting"][(original_id,)]
    [posted] = [r for pk, r in after["posting"].items() if pk not in before["posting"]]
    pid = posted["id"]
    day = json.loads(stored["args_json"])["date"] or "2026-10-26"
    old_head = before["posting_chain_head"][(cid,)]
    post = dict(id=pid, company_id=cid, seq=old_head["last_seq"] + 1, posting_date=day,
                voucher_type=original["voucher_type"] + ":reversal", voucher_id=original["voucher_id"],
                memo=f"reverses posting {original['seq']}", reverses_posting_id=original_id,
                operation_key=None, created_at=posted["created_at"], created_by="owner",
                prev_hash=old_head["last_hash"], hash=posted["hash"], hash_format=2, ledger="general")
    new_lines = {r["line_no"]: r for pk, r in after["ledger_entry"].items() if pk not in before["ledger_entry"]}
    lines = []
    for old in before["ledger_entry"].values():
        if old["posting_id"] == original_id:
            lines.append({**old, "id": new_lines[old["line_no"]]["id"], "posting_id": pid,
                          "debit": old["credit"], "credit": old["debit"],
                          "functional_debit": old["functional_credit"], "functional_credit": old["functional_debit"]})
    outcome = after["operation_outcome"][(key,)]
    result_json = json.dumps({"text": result.text, "data": result.data, "figures": result.figures,
                              "amounts": result.amounts})
    want = {
        "approval": {"changed": [({"id": aid}, {"status": "consumed",
            "decided_at": after["approval"][(aid,)]["decided_at"], "approved_by": stored["principal_id"],
            "approver_grants_version": stored["policy_version"]})]},
        "operation_outcome": {"added": [dict(key=key, company_id=cid, approval_id=aid, card_digest=card.digest,
            status="committed", result_json=result_json, committed_at=outcome["committed_at"])]},
        "posting": {"added": [post]}, "ledger_entry": {"added": lines},
        "posting_chain_head": {"changed": [({"company_id": cid}, {"last_seq": post["seq"], "last_hash": post["hash"]})]},
    }
    audit_specs = [(f"commit:{stored['contract']}", None, None, aid, {"operation_key": key, **result.data})]
    if delivery:
        if seed["settlement"] == "still_owed":
            want["supplier_payable"] = {"changed": [({"id": seed["payable"]}, {"status": "reversed", "outstanding": Decimal(0)})]}
            audit_specs.append(("close_payable", "supplier_payable", seed["payable"], None, {"number": seed["number"]}))
        old_entry = before["stock_ledger_entry"][(seed["entry"],)]
        [entry] = [r for pk, r in after["stock_ledger_entry"].items() if pk not in before["stock_ledger_entry"]]
        want["stock_ledger_entry"] = {"added": [{**old_entry, "id": entry["id"], "created_at": entry["created_at"],
            "posting_date": day, "seq": 2, "direction": "out", "qty_after": Decimal(0), "value_after": Decimal(0),
            "voucher_type": "stock_goods_received:reversal", "reverses_entry_id": seed["entry"]}]}
        want["stock_head"] = {"changed": [({"id": seed["head"]}, {"qty": Decimal(0), "value": Decimal(0), "last_seq": 2, "last_date": day})]}
    else:
        want["supplier_payment"] = {"changed": [({"id": seed["payment_id"]}, {"status": "reversed", "reverse_operation_key": key})]}
        want["supplier_payable"] = {"changed": [({"id": i}, {"outstanding": amount, "status": "open"})
                                               for i, amount in (("one", FIRST_ALLOCATION), ("two", SECOND_ALLOCATION))]}
        audit_specs.append((CONTRACT, "supplier_payment", seed["payment_id"], None,
                            {"number": seed["number"], "reason": reason, "posting_id": pid, "operation_key": key}))
    audits = [r for pk, r in after["audit_log"].items() if pk not in before["audit_log"]]
    expected_audits = []
    for action, dtype, did, approval_id, detail in audit_specs:
        [actual] = [r for r in audits if r["action"] == action]
        expected_audits.append(dict(id=actual["id"], at=actual["at"], company_id=cid, actor="owner", action=action,
                                   document_type=dtype, document_id=did, approval_id=approval_id,
                                   detail=json.dumps(detail, sort_keys=True)))
    want["audit_log"] = {"added": expected_audits}
    assert_changes(before, after, want)


@pytest.fixture(autouse=True)
def reversal_activation(monkeypatch, every_feature):
    for name in ("reverse_supplier_payment", "reverse_goods_received"):
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", None)


def contract():
    """The registered core reversal contract under test."""
    return REGISTRY.contracts[CONTRACT]


def propose(engine, args, **kwargs):
    return approvals.propose(engine, contract(), args, **kwargs)


def tool(args):
    return ToolCall("call_reverse_supplier_payment", CONTRACT, args, json.dumps(args))


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


def seed_supplier_payment(
    engine,
    *,
    tag="",
    number=NUMBER,
    amount=AMOUNT,
    posting_date=ORIGINAL_DAY,
    item_day=ITEM_DAY,
    operation_key="forward-key",
    payment_id="payment",
    specs=(("one", "receipt-one", GR_ONE, FIRST_ALLOCATION),
           ("two", "receipt-two", GR_TWO, SECOND_ALLOCATION)),
):
    """Insert a fully consistent posted payment world, mirroring the unit-test rows.

    One posted payment, fully allocated across its delivery payables, with the
    delivery source postings and the payment's own posting plus ledger lines.
    Returns the ids, numbers and amounts the tests assert on.
    """
    prefix = f"{tag}-" if tag else ""
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
        # The guarded forward card is not public yet. Header/allocation rows mirror
        # test_supplier_payment_reversal_unit.world; ledger rows use its own writer.
        conn.execute(supplier_payment.insert().values(
            id=prefix + payment_id, company_id=ctx.company_id, number=number,
            supplier_id=supplier_id, posting_date=posting_date, currency=ctx.currency,
            amount=amount, unallocated=Decimal("0.00"), account_id=bank["id"], status="posted",
            operation_key=operation_key, reverse_operation_key=None, created_at=now()))
        for payable_id, source_id, source_number, total in specs:
            conn.execute(supplier_payable.insert().values(
                id=prefix + payable_id, company_id=ctx.company_id, supplier_id=supplier_id,
                currency=ctx.currency, source_type=payables.DELIVERY, source_id=prefix + source_id,
                source_number=source_number, posting_date=item_day, amount=total,
                outstanding=Decimal("0.00"), status="paid", created_at=now()))
            conn.execute(supplier_payment_allocation.insert().values(
                id=prefix + "allocation-" + payable_id, company_id=ctx.company_id,
                payment_id=prefix + payment_id, payable_id=prefix + payable_id,
                currency=ctx.currency, amount=total))
            ledger.post(ctx, item_day, payables.DELIVERY, prefix + source_id, [
                ledger.Line(inventory["id"], debit=total),
                ledger.Line(ap["id"], credit=total, party_id=supplier_id)])
        payment_posting = ledger.post(ctx, posting_date, "supplier_payment", prefix + payment_id, [
            ledger.Line(ap["id"], debit=amount, party_id=supplier_id),
            ledger.Line(bank["id"], credit=amount)]).posting_id

    return {
        "supplier_id": supplier_id, "bank": bank, "ap": ap, "inventory": inventory,
        "payment_id": prefix + payment_id, "number": number, "amount": amount,
        "posting_id": payment_posting, "operation_key": operation_key,
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


def original_lines(engine, payment_id):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        posting = ledger.posting_for_voucher(ctx, "supplier_payment", payment_id)
        rows = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == posting["id"],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
        return posting, [dict(r) for r in rows]


def reversal_lines(engine, original_posting_id):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        posting = conn.execute(
            select(m.posting).where(
                m.posting.c.company_id == ctx.company_id,
                m.posting.c.reverses_posting_id == original_posting_id,
            )
        ).mappings().one()
        rows = conn.execute(
            select(m.ledger_entry).where(
                m.ledger_entry.c.company_id == ctx.company_id,
                m.ledger_entry.c.posting_id == posting["id"],
            ).order_by(m.ledger_entry.c.line_no)
        ).mappings().all()
        return dict(posting), [dict(r) for r in rows]


def principal(engine, company_id, name, permissions):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, permissions, [company_id])
        return pr.authenticate(conn, token), token


def test_contract_registered_as_core_write_with_money_and_tool_schema(engine):
    """Pins the public registration: core write, money-moving, owner-word schema.

    Fails if the contract is unregistered, owned elsewhere, not a write, not
    money-moving, planned off, or its tool schema takes ids or requires a date.
    """
    assert contract().name == backend.NAME == CONTRACT
    assert contract().plugin == "core" and REGISTRY.contract_plugin[CONTRACT] == "core"
    assert contract().effect == "write" and contract().moves_money is True
    assert contract().planned is None
    assert core_contracts.MANIFEST.contracts[CONTRACT] == "write"
    assert "supplier_payment" in core_contracts.MANIFEST.voucher_types
    assert contract().read_set is not None and contract().find_by_operation_key is not None
    props = contract().tool_schema()["function"]["parameters"]
    assert set(props["properties"]) == {"payment", "date", "reason"}
    assert props["required"] == ["payment"]
    assert "payment_id" not in json.dumps(props)


def test_owner_names_document_by_number_never_id(engine):
    """Pins that the input takes the stored payment NUMBER, never an engine id.

    Fails if the input accepts payment_id/voucher_id, stores an id in the card
    args, or shows an id instead of the number on the card.
    """
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": NUMBER})
    assert card.card["title"] == "Reverse a supplier payment"
    assert ["Payment", NUMBER] in card.card["lines"]
    assert NUMBER in json.dumps(card.card)
    with engine.connect() as conn:
        stored = conn.execute(
            select(m.approval).where(m.approval.c.id == card.approval_id)
        ).mappings().one()
        args = json.loads(stored["args_json"])
    assert set(args) == {"payment", "date", "reason"}
    assert args["payment"] == NUMBER
    assert not {"payment_id", "voucher_id"} & set(args)
    with pytest.raises(ValidationError):
        propose(engine, {"payment": NUMBER, "payment_id": "model-picked-id"})


def test_trailing_digits_resolve_when_unique(engine):
    """Pins trailing-digit resolution for a number ending in exactly one document.

    Fails if the resolver demands the exact full number.
    """
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": "00001"})
    assert ["Payment", NUMBER] in card.card["lines"]
    assert card.card["title"] == "Reverse a supplier payment"


def test_ambiguous_tail_asks_with_options(engine):
    """Pins that a tail ending in several documents asks instead of guessing.

    Fails if the resolver silently binds the first match and makes a card.
    """
    seed_supplier_payment(engine)
    seed_supplier_payment(
        engine, tag="b", number="SPAY-2025-00001", operation_key="forward-key-b",
        payment_id="payment-b",
        specs=(("b-one", "b-receipt-one", "GR-2025-00001", FIRST_ALLOCATION),
               ("b-two", "b-receipt-two", "GR-2025-00002", SECOND_ALLOCATION)),
    )
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": "00001"})
    assert err.value.code == "payment_ambiguous"
    assert {NUMBER, "SPAY-2025-00001"} <= set(err.value.options)
    assert_changes(before, snapshot(engine), {})


def test_unknown_number_refused_plain_sentence(engine):
    """Pins the unknown-number refusal with no card and no writes.

    Fails if an unknown number previews a card or writes.
    """
    seed_supplier_payment(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": "SPAY-2026-00999"})
    assert err.value.code == "payment_unknown"
    assert err.value.message == "There is no such payment in these books."
    assert_changes(before, snapshot(engine), {})


def test_foreign_company_number_refused_plain_sentence(engine):
    """Pins that a number from another company is not found in these books.

    Fails if company scoping leaks the other company's document.
    """
    seed_supplier_payment(engine)
    create_company(engine, "Other Co")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Co").company_id
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER}, company_id=other)
    assert err.value.code == "payment_unknown"
    assert err.value.message == "There is no such payment in these books."
    assert_changes(before, snapshot(engine), {})


def test_reason_bound_and_stored(engine):
    """Pins the 140-character reason bound, its card line and its audit row.

    Fails if a longer reason previews, or a valid reason is dropped from the
    card or the audit detail.
    """
    seed_supplier_payment(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER, "reason": "x" * 141})
    assert err.value.code == "payment_reversal_reason"
    assert_changes(before, snapshot(engine), {})
    card = propose(engine, {"payment": NUMBER, "reason": "Recorded twice"})
    assert ["Reason", "Recorded twice"] in card.card["lines"]
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        rows = conn.execute(
            select(m.audit_log).where(
                m.audit_log.c.company_id == ctx.company_id,
                m.audit_log.c.action == CONTRACT,
            )
        ).mappings().all()
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"])["reason"] == "Recorded twice"


def test_propose_approve_commit_once_with_minted_key_and_stored_outcome(engine):
    """Pins the full money move: card, digest approval, one commit, stored outcome.

    Fails if any write is dropped: the payment stays posted, a payable stays
    shut, the mirror posting is missing, the originals change, or the outcome
    is not stored under the engine-minted key.
    """
    seed = seed_supplier_payment(engine)
    co = live_company(engine)
    before_card = snapshot(engine)
    card = propose(engine, {"payment": NUMBER, "reason": "Recorded twice"})
    assert card.operation_key and card.operation_key != card.approval_id
    assert len(card.digest) == 64
    assert card.card["title"] == "Reverse a supplier payment"
    assert ["Payment", NUMBER] in card.card["lines"]
    assert ["Supplier", "Copperline Metals"] in card.card["lines"]
    assert ["Amount paid", "1,600.00"] in card.card["lines"]
    assert ["Original date", ORIGINAL_DAY.isoformat()] in card.card["lines"]
    assert ["Reversal date", co["today"].isoformat()] in card.card["lines"]
    assert [GR_ONE, "1,450.00 owed again: outstanding 0.00 to 1,450.00; paid to open."] in card.card["lines"]
    assert [GR_TWO, "150.00 owed again: outstanding 0.00 to 150.00; paid to open."] in card.card["lines"]
    assert ["Original entries", f"The original entries stay; reversing entries are added on {co['today'].isoformat()}."] in card.card["lines"]
    assert card.card["footer"].endswith("Valid 10 minutes. Rechecked before saving.")
    assert any("Use this when the payment was not made" in w for w in card.card["warnings"])
    assert len(card.card["amounts"]) == 9
    before = snapshot(engine)
    stored = before["approval"][(card.approval_id,)]
    assert_changes(before_card, before, {"approval": {"added": [stored]}})
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    assert result.text == (
        f"Reversed {NUMBER} (1,600.00 {co['currency']}) on {co['today'].isoformat()}: "
        f"{GR_ONE} is owed 1,450.00 again; {GR_TWO} is owed 150.00 again. "
        "The original entries are kept."
    )
    assert result.figures == ["1,600.00", "1,450.00", "150.00"]
    assert result.data["number"] == NUMBER and result.data["id"] == "payment"
    assert result.data["operation_key"] == card.operation_key
    after = snapshot(engine)
    reversal_posting = [
        r for r, row in after["posting"].items()
        if row["reverses_posting_id"] == seed["posting_id"]
    ]
    assert len(reversal_posting) == 1
    reversal_id = reversal_posting[0][0]
    assert result.data["posting_id"] == reversal_id
    bank_line = f"{seed['bank']['number']} {seed['bank']['name']}: debit 1,600.00 {co['currency']}"
    ap_line = f"2100 Accounts Payable (Copperline Metals): credit 1,600.00 {co['currency']}"
    assert_reversal_diff(before, after, card, result, seed, reason="Recorded twice")
    assert bank_line in card.card["books"]
    assert ap_line in card.card["books"]
    assert approvals.read_back(engine, card.operation_key).data == result.data


def test_default_date_is_today_without_period_warning(engine):
    """Pins that an omitted date reverses today with no same-period warning.

    Fails if the default is not today or a same-period warning appears.
    """
    seed_supplier_payment(engine)
    co = live_company(engine)
    card = propose(engine, {"payment": NUMBER})
    assert ["Reversal date", co["today"].isoformat()] in card.card["lines"]
    assert not any("stays in the period" in w for w in card.card["warnings"])
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"



def test_explicit_date_with_date_source_posts_it(engine):
    """Pins that an owner-given date is posted and quoted as its source.

    Fails if the input date is ignored or the date-source line is missing.
    """
    seed_supplier_payment(engine)
    day = date(2026, 10, 22)
    card = propose(
        engine, {"payment": NUMBER, "date": day},
        date_source={"phrase": "22 Oct 2026", "date": "2026-10-22"},
        owner_words="Reverse SPAY-2026-00001 on 22 Oct 2026",
    )
    assert ["Reversal date", "2026-10-22"] in card.card["lines"]
    assert ["Date source", "From your words: '22 Oct 2026' (22 Oct 2026)"] in card.card["lines"]
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed"
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        posting = conn.execute(
            select(m.posting).where(
                m.posting.c.company_id == ctx.company_id,
                m.posting.c.reverses_posting_id == seed_supplier_payment_posting(engine),
            )
        ).mappings().one()
        assert posting["posting_date"] == day


def seed_supplier_payment_posting(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        return conn.execute(
            select(m.posting.c.id).where(
                m.posting.c.company_id == ctx.company_id,
                m.posting.c.voucher_type == "supplier_payment",
                m.posting.c.voucher_id == "payment",
            )
        ).scalar_one()


def test_period_warning_when_reversal_in_later_period(engine):
    """Pins the cross-period warning naming both period beginnings.

    Fails if the warning is dropped when the reversal period differs.
    """
    seed_supplier_payment(engine, posting_date=date(2026, 9, 15), item_day=date(2026, 9, 5))
    original = period_on(engine, date(2026, 9, 15))
    reversal = period_on(engine, date(2026, 10, 26))
    assert original["id"] != reversal["id"]
    card = propose(engine, {"payment": NUMBER})
    assert (
        f"{NUMBER} stays in the period beginning {original['start_date']}; "
        f"this reversal is in the period beginning {reversal['start_date']}."
    ) in card.card["warnings"]
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"


def test_mirror_lines_equal_original_exactly(engine):
    """Pins that the reversal posting mirrors the original line for line.

    Fails if an account, amount, side or party differs, or an original line moves.
    """
    seed = seed_supplier_payment(engine)
    _, before_lines = original_lines(engine, "payment")
    before_values = [
        (r["account_id"], r["debit"], r["credit"], r["party_id"], r["currency"]) for r in before_lines
    ]
    card = propose(engine, {"payment": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    _, after_lines = reversal_lines(engine, seed["posting_id"])
    assert {(r["account_id"], r["credit"], r["debit"], r["party_id"], r["currency"]) for r in after_lines} == set(before_values)
    _, same_lines = original_lines(engine, "payment")
    assert [(r["account_id"], r["debit"], r["credit"], r["party_id"]) for r in same_lines] == [
        (r["account_id"], r["debit"], r["credit"], r["party_id"]) for r in before_lines
    ]
    with engine.connect() as conn:
        allocations = conn.execute(select(supplier_payment_allocation)).mappings().all()
    assert len(allocations) == 2


def test_replay_returns_stored_result_writes_nothing(engine, monkeypatch):
    """Pins that the same key and digest replay the stored outcome.

    Fails if a replay re-runs commit instead of returning the stored result.
    """
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": NUMBER})
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
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.approve(engine, card.approval_id, "0" * 64)
    assert err.value.code == "digest"
    assert_changes(before, snapshot(engine), {})


def test_already_reversed_refused(engine):
    """Pins the already-reversed refusal with its exact sentence.

    Fails if a second reversal of the same payment previews a card.
    """
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": NUMBER})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER})
    assert err.value.code == "already_reversed"
    assert err.value.message == f"{NUMBER} is already reversed."
    assert_changes(before, snapshot(engine), {})


def test_reversal_before_original_refused(engine):
    """Pins the backdated refusal with its exact sentence.

    Fails if a reversal dated before its payment previews a card.
    """
    seed_supplier_payment(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER, "date": date(2026, 10, 19)})
    assert err.value.code == "reversal_before_original"
    assert err.value.message == "A reversal cannot be dated before its payment."
    assert_changes(before, snapshot(engine), {})


def test_future_reversal_refused(engine):
    """Pins the future-date refusal with its exact sentence.

    Fails if a reversal dated after today previews a card.
    """
    seed_supplier_payment(engine)
    co = live_company(engine)
    before = snapshot(engine)
    future = date.fromordinal(co["today"].toordinal() + 1)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER, "date": future})
    assert err.value.code == "future_date"
    assert err.value.message == "A reversal can't be dated in the future."
    assert_changes(before, snapshot(engine), {})


def test_closed_reversal_period_refused(engine):
    """Pins the closed-period refusal in the posting service's words.

    Fails if a reversal in a closed period previews a card.
    """
    seed_supplier_payment(engine)
    co = live_company(engine)
    close_period(engine, co["today"])
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER})
    assert err.value.code == "period_closed"
    assert err.value.message == f"The period containing {co['today'].isoformat()} is closed."
    assert_changes(before, snapshot(engine), {})


def test_reversed_delivery_behind_payment_refused(engine):
    """Pins the reversed-delivery guard with its exact sentence.

    Fails if a payment whose settled delivery was reversed still previews.
    """
    seed = seed_supplier_payment(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        source_id = conn.execute(
            select(m.posting.c.id).where(
                m.posting.c.company_id == ctx.company_id,
                m.posting.c.voucher_type == payables.DELIVERY,
                m.posting.c.voucher_id == "receipt-one",
            )
        ).scalar_one()
        ledger.reverse(ctx, source_id, ITEM_DAY)

    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"payment": NUMBER})
    assert err.value.code == "payment_reversal_source"
    assert err.value.message == "A delivery settled by this payment has been reversed."
    assert_changes(before, snapshot(engine), {})


def test_commit_without_approval_refused(engine):
    """Pins the commit gate: no approved card, no reversal.

    Fails if the backend commits outside its registered card's commit.
    """
    seed_supplier_payment(engine)
    before = snapshot(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        inp = contract().input_model(payment=NUMBER)
        with pytest.raises(Refusal) as err:
            contract().commit(ctx, inp, "unapproved-key")
    assert err.value.code == "not_approved"
    assert_changes(before, snapshot(engine), {})


def test_chat_routes_reversal_words_to_card_and_commits(engine):
    """Pins chat reachability: reversal words propose the card, approval commits.

    Fails if the words still give a planned reply instead of the reversal card.
    """
    seed = seed_supplier_payment(engine)
    client, cid = chat_client(
        engine, {"payment": NUMBER},
        prefix=[Reply("", [call("find_capability", request="reverse supplier payment")]),
                Reply("", [call("choose_capability", pick="op.reverse_supplier_payment")])],
    )
    before = snapshot(engine)
    [card] = message(client, cid, "Reverse supplier payment SPAY-2026-00001")
    assert card["type"] == "card" and card["card"]["title"] == "Reverse a supplier payment", card
    assert ["Payment", NUMBER] in card["card"]["lines"]
    from types import SimpleNamespace

    before = snapshot(engine)
    events = approve_http(client, card)
    assert events[-1]["status"] == "committed", events
    with engine.connect() as conn:
        status = conn.execute(
            select(supplier_payment.c.status).where(supplier_payment.c.id == "payment")
        ).scalar_one()
    assert status == "reversed"
    with engine.connect() as conn:
        stored = conn.execute(select(m.approval).where(m.approval.c.id == card["approval_id"])).mappings().one()
    bound = SimpleNamespace(approval_id=card["approval_id"], digest=card["digest"], operation_key=stored["operation_key"])
    result = approvals.read_back(engine, stored["operation_key"], ownership_check=lambda conversation: conversation == cid)
    assert_reversal_diff(before, snapshot(engine), bound, result, seed)


@pytest.mark.xfail(strict=True, reason="awaiting the reversal routing: supplier payment reference selects its reversal")
def test_reversal_phrase_routes_only_to_supplier_reversal(engine):
    """Pins reversal-only routing for the documented supplier phrase.

    Fails if the phrase keeps a planned reply or names a forward override.
    """
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
        target = entries["op.reverse_supplier_payment"]
        assert target.state == "installed"
        assert target.plugin == "core" and target.target == CONTRACT
        said = "reverse supplier payment SPAY-2026-00012"
        named = cat.named_by_synonym(conn, said)
        assert "op.reverse_supplier_payment" in {e.id for e, _ in named}
        found = cat.find_capability(conn, said)
        expected = "a"
        decision = cat.decide(found, "planned.purchasing", named=named)
        assert decision.entry.id == "op.reverse_supplier_payment"
        assert decision.outcome == expected
        direct = cat.decide(found, "op.reverse_supplier_payment", named=named)
        assert direct.entry.id == "op.reverse_supplier_payment"
        assert direct.outcome == expected


def test_payment_phrases_never_name_reversal(engine):
    """Pins that payment words without a reversal word never reach the reversal.

    Fails if the reversal predicate fires without a reversal word.
    """
    with engine.connect() as conn:
        for said in ("pay the supplier bill", "pay the supplier invoice INV-7",
                     "settle the supplier bill"):
            named = cat.named_by_synonym(conn, said)
            assert "op.reverse_supplier_payment" not in {e.id for e, _ in named}
            decision = cat.decide(cat.find_capability(conn, said), "planned.purchasing", named=named)
            assert decision.outcome == "c" and decision.capability == "planned.purchasing"


def test_customer_payment_reference_never_routes_to_supplier_reversal(engine):
    """Pins that a customer payment reference is not a supplier reversal.

    Fails if a bare payment reference without overlap words binds the reversal.
    """
    with engine.connect() as conn:
        said = "reverse PAY-2026-00012"
        named = cat.named_by_synonym(conn, said)
        assert "op.reverse_supplier_payment" not in {e.id for e, _ in named}
        decision = cat.decide(cat.find_capability(conn, said), "none", named=named)
        assert decision.outcome != "a"


@pytest.mark.skip(reason="pay_supplier is not registered on this base; enable when it registers")
def test_full_precedence_forwards_and_reversals_together(engine):
    """Pins forward/reversal precedence with both families enabled.

    Fails if a forward phrase reaches the reversal, a reversal phrase reaches a
    forward, or either keeps a planned reply once both are installed.
    """
    with engine.connect() as conn:
        assert "pay_supplier" in REGISTRY.contracts
        for said, pick, entry in (
            ("pay the supplier bill", "planned.purchasing", "op.pay_supplier"),
            ("reverse supplier payment SPAY-2026-00012", "planned.purchasing",
             "op.reverse_supplier_payment"),
        ):
            named = cat.named_by_synonym(conn, said)
            decision = cat.decide(cat.find_capability(conn, said), pick, named=named)
            assert decision.outcome == "a" and decision.entry.id == entry


@pytest.mark.skip(reason="pay_supplier is not registered on this base; enable when it registers")
def test_forwards_only_partial_activation_keeps_reversal_planned(engine):
    """Pins that reversal phrases keep the planned reply with only forwards on.

    Fails if a reversal phrase routes to a forward when reversals are off.
    """
    with engine.connect() as conn:
        assert "pay_supplier" in REGISTRY.contracts
        named = cat.named_by_synonym(conn, "reverse supplier payment SPAY-2026-00012")
        assert "op.pay_supplier" not in {e.id for e, _ in named}
        decision = cat.decide(cat.find_capability(conn, "reverse supplier payment"), "planned.purchasing", named=named)
        assert decision.outcome == "c" and decision.capability == "planned.purchasing"


def test_registration_gate_planned_when_off(engine, monkeypatch):
    """Pins the catalogue gate: with the contract off, the words stay planned.

    Fails if the reversal routes while its contract is not live.
    """
    monkeypatch.setattr(contract(), "planned", "later")
    with engine.connect() as conn:
        said = "reverse supplier payment SPAY-2026-00012"
        decision = cat.decide(cat.find_capability(conn, said), "planned.purchasing",
                              named=cat.named_by_synonym(conn, said))
        assert decision.outcome == "c" and decision.capability == "planned.purchasing"


def test_draft_only_cannot_approve_own_card(engine):
    """Pins that a draft-only principal cannot approve its own card.

    Fails if the approve grant check is removed.
    """
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    drafter, token = principal(engine, cid, "drafter", ["view", "draft"])
    seed_supplier_payment(engine)
    card = propose(engine, {"payment": NUMBER}, principal=drafter)
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
    seed_supplier_payment(engine)
    with engine.connect() as conn:
        ours = company_ctx(conn).company_id
    create_company(engine, "Other Co")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Co").company_id
    _, token = principal(engine, other, "other approver", ["view", "draft", "approve", "post"])
    card = propose(engine, {"payment": NUMBER}, company_id=ours)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403, response.text
    assert_changes(before, snapshot(engine), {})


def test_core_cannot_be_disabled(engine):
    """Core is always enabled and has no disableable installation row."""
    before = snapshot(engine)
    with engine.begin() as conn:
        with pytest.raises(Refusal) as err:
            disable_plugin(conn, "core")
        assert err.value.code == "plugin_not_enabled"
        assert err.value.message == "The core plugin is not enabled in these books."
        from erpclaw_core.plugins import enabled_plugins
        assert "core" in enabled_plugins(conn)
    assert_changes(before, snapshot(engine), {})


def supplier_reversal_scenario(engine):
    seed_supplier_payment(engine, posting_date=date(2026, 10, 10), item_day=date(2026, 10, 5))
    return {"payment": NUMBER}


def delivery_reversal_scenario(engine):
    from stock.test_goods_received_reversal_contract import seed_delivery

    seed_delivery(engine, posting_date=date(2026, 10, 10))
    return {"delivery": "GR-2026-00001"}


def stored_reversal(engine, args, outcome, before):
    after = snapshot(engine)
    return {"before": before, "after": after, "args": args, "outcome": outcome,
            "reversed": after["supplier_payment"] if "payment" in args else after["stock_goods_received"]}


def reversal_value_problems(contract, card, texts, stored):
    """Independent card-value expectations from persisted original and mirror rows."""
    from erpclaw_core.money import fmt, fmt_qty

    before, after, result = stored["before"], stored["after"], stored["outcome"]
    delivery = contract == "reverse_goods_received"
    document = before["stock_voucher"][("gr",)] if delivery else before["supplier_payment"][("payment",)]
    original_type = "stock_goods_received" if delivery else "supplier_payment"
    [original] = [p for p in before["posting"].values() if p["voucher_type"] == original_type
                  and p["voucher_id"] == document["id"]]
    [posting] = [p for p in after["posting"].values() if p["reverses_posting_id"] == original["id"]]
    currency = card["currency"]
    amount = lambda v: fmt(Decimal(v), currency)
    short_date = lambda day: f"{date.fromisoformat(day).day} {date.fromisoformat(day):%b %Y}"
    day = posting["posting_date"]
    lines, books = [], []
    mirror = sorted([r for r in after["ledger_entry"].values() if r["posting_id"] == posting["id"]], key=lambda r: r["line_no"])
    total = sum((Decimal(r["debit"]) for r in mirror), Decimal(0))
    for entry in mirror:
        account = after["account"][(entry["account_id"],)]
        party = f" ({after['party'][(entry['party_id'],)]['name']})" if entry["party_id"] else ""
        side, value = ("debit", entry["debit"]) if Decimal(entry["debit"]) else ("credit", entry["credit"])
        books.append(f"{account['number']} {account['name']}{party}: {side} {amount(value)} {currency}")
    if delivery:
        [header] = [r for r in before["stock_goods_received"].values() if r["voucher_id"] == document["id"]]
        supplier = after["party"][(header["supplier_id"],)]["name"]
        lines = [("Delivery", f"{document['number']} from {supplier}"), ("Original date", short_date(document["posting_date"])),
                 ("Reversal date", short_date(day)), ("Total reversed", f"{amount(total)} {currency}"),
                 ("Settlement", f"Still owed to {supplier}; this delivery's untouched payable will be closed.")]
        for entry in after["stock_ledger_entry"].values():
            if entry["voucher_type"] != "stock_goods_received:reversal" or entry["voucher_id"] != document["id"]:
                continue
            item = after["item"][(entry["item_id"],)]
            wh = after["stock_warehouse"][(entry["warehouse_id"],)]["name"]
            lines.append((item["name"], f"Remove {fmt_qty(Decimal(entry['qty']))} {item['unit']} from {wh}: "
                f"{amount(entry['value'])} {currency}; {fmt_qty(Decimal(entry['qty_after']))} {item['unit']} remain."))
        lines.extend([("Delivery payable", f"{amount(total)} {currency} outstanding becomes 0.00 {currency}; status reversed.")])
        expected_text = f"Reversed goods received {document['number']}, dated {short_date(day)}: {amount(total)} {currency}."
    else:
        supplier = after["party"][(document["supplier_id"],)]["name"]
        lines = [("Payment", document["number"]), ("Supplier", supplier), ("Original date", document["posting_date"]),
                 ("Reversal date", day), ("Amount paid", amount(total))]
        reopened = []
        allocations = sorted([r for r in before["supplier_payment_allocation"].values() if r["payment_id"] == document["id"]],
                             key=lambda r: r["payable_id"])
        for allocation in allocations:
            key = (allocation["payable_id"],)
            old, new = before["supplier_payable"][key], after["supplier_payable"][key]
            lines.append((old["source_number"], f"{amount(allocation['amount'])} owed again: outstanding "
                f"{amount(old['outstanding'])} to {amount(new['outstanding'])}; {old['status']} to open."))
            reopened.append(f"{old['source_number']} is owed {amount(allocation['amount'])} again")
        expected_text = f"Reversed {document['number']} ({amount(total)} {currency}) on {day}: " + "; ".join(reopened) + ". The original entries are kept."
    lines.extend([("Accounting destination", "ERPClaw general ledger"),
                  ("Original entries", f"The original entries stay; reversing entries are added on {day}.")])
    errors = []
    if [list(r) for r in lines] != [r for r in card["lines"] if r[0] != "Company"]:
        errors.append(("lines", lines, card["lines"]))
    if books != card["books"]:
        errors.append(("books", books, card["books"]))
    if texts != [expected_text, expected_text]:
        errors.append(("result", expected_text, texts))
    return errors

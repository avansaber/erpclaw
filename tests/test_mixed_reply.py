"""Supplier payments combined with customer billing answer without choosing a write."""
from __future__ import annotations

import json
from itertools import product

import pytest

from erpclaw_assistant import resolve
from erpclaw_core import catalogue as cat
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY

ONE_CHANGE = "One change at a time, please: send each change as its own message."


def decision(engine, said, pick):
    with engine.connect() as conn:
        return cat.decide(cat.find_capability(conn, said), pick,
                          named=cat.named_by_synonym(conn, said), mixed=cat.is_mixed(said))


def planned_text(engine):
    with engine.connect() as conn:
        area = next(e for e in cat.catalogue(conn) if e.id == "planned.purchasing")
        d = cat.decide([area], area.id)
    assert d.outcome == "c" and d.capability == "planned.purchasing"
    return d.text


def assert_combined(engine, d):
    assert d is not None
    assert d.outcome == "c" and d.capability == "planned.purchasing"
    assert d.entry.id == "planned.purchasing" and d.choices == ()
    assert d.text == planned_text(engine) + " " + ONE_CHANGE


def orders(customer):
    return ["pay the supplier bill and " + customer, customer + " and pay the supplier bill"]


def test_one_change_sentence_matches_language_table():
    assert resolve._say("one_change") == ONE_CHANGE


@pytest.mark.parametrize("modifier", ["a new", "another", "a customer", "an"])
def test_mixed_modifier_combined_reply(engine, modifier):
    for verb, recipient in product(["create", "make", "send", "raise", "draft"],
                                   ["for Acme", "to Acme"]):
        for said in orders(f"{verb} {modifier} invoice {recipient}"):
            assert_combined(engine, decision(engine, said, "op.create_invoice"))


@pytest.mark.parametrize("said", orders("create a new invoice for Acme"))
@pytest.mark.parametrize("pick", ["op.create_invoice", "planned.purchasing", "none", "unsure", "op.invented"])
def test_mixed_explicit_none_unsure_unknown_combined(engine, said, pick):
    assert_combined(engine, decision(engine, said, pick))


@pytest.mark.parametrize("said", orders("invoice the customer") + orders("bill Acme"))
@pytest.mark.parametrize("pick", ["op.create_invoice", "planned.purchasing"])
def test_mixed_ownership_exclusions_do_not_exclude_detection(engine, said, pick):
    record = next(r for r in json.loads(cat.OVERRIDES.read_text()) if r["area"] == "planned.purchasing")
    assert "invoice the customer" in record["exclude"]
    assert_combined(engine, decision(engine, said, pick))


def test_mixed_message_not_entries_decides(engine):
    mixed, single = "pay the supplier bill and bill Acme", "bill Acme for the supplier delivery"
    with engine.connect() as conn:
        inventories = [cat.named_by_synonym(conn, text) for text in (mixed, single)]
    assert {"planned.purchasing", "op.create_invoice"} <= set.intersection(
        *({e.id for e, _ in inventory} for inventory in inventories))
    assert_combined(engine, decision(engine, mixed, "op.create_invoice"))
    d = decision(engine, single, "op.create_invoice")
    assert d.outcome == "a" and d.entry.id == "op.create_invoice"


@pytest.mark.parametrize("said", ["bill Acme for the supplier delivery",
                                  "bill the customer for this supplier delivery",
                                  "bill Acme Corporation for 5 copper wire", "pay the invoice for Acme"])
def test_customer_only_still_runs(engine, said):
    d = decision(engine, said, "op.create_invoice")
    assert d.outcome == "a" and d.entry.id == "op.create_invoice"
    if said == "pay the invoice for Acme":
        with engine.connect() as conn:
            assert "planned.purchasing" not in {e.id for e, _ in cat.named_by_synonym(conn, said)}


@pytest.mark.parametrize("said", ["pay the supplier bill", "pay the vendor's bill",
                                  "pay the supplier invoice INV-7", "pay the supplier's invoice INV-7",
                                  "send the supplier an invoice and pay the supplier bill"])
@pytest.mark.parametrize("pick", ["planned.purchasing", "op.create_invoice", "none", "unsure"])
def test_pure_supplier_stays_planned_only(engine, said, pick):
    d = decision(engine, said, pick)
    assert d.outcome == "c" and d.capability == "planned.purchasing"
    assert d.text == planned_text(engine) and ONE_CHANGE not in d.text


@pytest.mark.parametrize("said", ["pay the supplier bill and create a purchase order",
                                  "pay the supplier bill and create an invoice for Acme and a purchase order"])
def test_purchase_order_excluded_from_mixed_detection(engine, said):
    assert not cat.is_mixed(said)
    d = decision(engine, said, "planned.purchasing")
    assert d.outcome == "c" and d.text == planned_text(engine)
    if "invoice" not in said:
        with engine.connect() as conn:
            d = cat.planned_instead(conn, "create_invoice", said)
        assert d is None  # purchase-order ownership exclusion preserves the direct guard's existing behavior


@pytest.mark.parametrize("state", ["absent", "installed", "available"])
@pytest.mark.parametrize("said", orders("create a new invoice for Acme"))
@pytest.mark.parametrize("pick", ["op.create_invoice", "planned.purchasing"])
def test_mixed_activation_states_combined(engine, monkeypatch, state, said, pick):
    with engine.connect() as conn:
        entries = cat.catalogue(conn)
    if state != "absent":
        entries.append(cat.Entry("op.pay_supplier", "operation", "pay supplier", "", (), state,
                                 plugin="core", target="pay_supplier"))
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    assert_combined(engine, decision(engine, said, pick))
    if state == "installed":
        pure = decision(engine, "pay the supplier bill", pick)
        assert pure.outcome == "a" and pure.entry.id == "op.pay_supplier"


@pytest.mark.parametrize("said", orders("create a new invoice for Acme"))
def test_mixed_direct_guard_combined(engine, said):
    with engine.connect() as conn:
        d = cat.planned_instead(conn, "create_invoice", said)
    assert_combined(engine, d)


@pytest.mark.parametrize("said", orders("create a new invoice for Acme"))
def test_mixed_direct_caller_forwards_combined(engine, said):
    with engine.connect() as conn, pytest.raises(resolve.Planned) as caught:
        resolve.resolve_args(company_ctx(conn), REGISTRY.contracts["create_invoice"], {}, said, resolve.Session())
    assert caught.value.message == planned_text(engine) + " " + ONE_CHANGE
    assert caught.value.route == "planned.purchasing"


def test_esign_keeps_own_reply(engine):
    d = decision(engine, "e-sign the invoice", "planned.esign")
    assert d.outcome == "c" and d.capability == "planned.esign"
    assert ONE_CHANGE not in d.text


def test_known_customer_invoice_to_supplier_limit():
    record = next(r for r in json.loads(cat.OVERRIDES.read_text()) if r["area"] == "planned.purchasing")
    assert cat._customer_billing(cat._tokens("pay a customer invoice INV-7 to the supplier"), set(record["intent"]))

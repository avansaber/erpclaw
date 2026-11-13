"""Accepted plan checks against engine-owned data; no planning-repository export is available.

The frozen inventories below describe the pre-change engine data. The release
matrix is independent of the live file, so restoring an overclaim fails locally.
This cannot validate notes-side task ownership or reproduce its exporter.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import NoInspectionAvailable

from erpclaw_assistant import resolve, store
from erpclaw_assistant.model import Reply, call
from erpclaw_core import catalogue as cat, models as m
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY, disable_plugin
from test_catalogue import books, chat, find_then


WAVES = {
    "Core": "alerts invoice_fields users",
    "v3.0": "connector.csv messaging sales stock stock_item",
    "v3.1": "advanced_accounting connector.csv_journals connector.csv_import connector.quickbooks "
            "connector.shopify connector.sso connector.xero documents esign lending multi_currency "
            "period_close schedules",
    "later": "assets budgets connector.bank_statements connector.netsuite connector.stripe country_packs crm "
             "discounts hr ledger_dimensions maintenance manufacturing payment_terms payments_banking payroll "
             "projects purchasing quality several_changes subscriptions support tax_engine",
}

# Frozen copies of the three historical synonym inventories, kept in this
# allowed test file rather than introducing a separate fixture file.
FROZEN = {
    "planned.invoice_fields": ("rate per item", "item rate", "invoice date", "memo", "note on the invoice"),
    "planned.country_packs": ("country", "localisation", "localization", "vat", "gst", "compliance",
                              "canada", "uk", "eu", "india", "us"),
    "planned.multi_currency": ("foreign currency", "another currency", "in euros", "in pounds", "exchange rate"),
}


def test_engine_plan_release_matrix_and_live_scope():
    rows = cat._planned()
    expected = {f"planned.{name}": wave for wave, names in WAVES.items() for name in names.split()}
    assert {r["id"]: r["wave"] for r in rows} == expected
    assert len(rows) == len(expected)
    areas = {r["id"]: r for r in rows}
    assert areas["planned.payments_banking"]["name"] == "bank accounts, reconciliation and payment runs"
    assert areas["planned.messaging"]["name"] == "plain-text email"
    reply = areas["planned.users"]["reply"]
    assert reply == ("These books have a named owner and message authors. "
                     "Second-person screens and roles are not in ERPClaw yet.")
    assert "NetSuite" not in areas["planned.connector.quickbooks"]["name"]
    assert "bank" not in areas["planned.connector.xero"]["name"].lower()
    split_synonyms = {
        "connector.csv": ["csv", "spreadsheet", "export", "csv export", "export csv"],
        "connector.csv_import": ["import", "upload", "csv import", "import csv"],
        "connector.csv_journals": ["csv journal", "csv journals", "import journals"],
        "connector.bank_statements": ["bank statement", "bank feed", "import statement"],
        "connector.quickbooks": ["quickbooks", "qbo", "sync"],
        "connector.netsuite": ["netsuite"],
        "connector.xero": ["xero"],
    }
    for name, synonyms in split_synonyms.items():
        assert areas[f"planned.{name}"]["synonyms"] == synonyms


def test_frozen_and_live_synonym_inventories():
    assert "memo" in FROZEN["planned.invoice_fields"]
    assert {"vat", "gst"} <= set(FROZEN["planned.country_packs"])
    assert "tax" not in FROZEN["planned.country_packs"]
    assert FROZEN["planned.multi_currency"][-1:] == ("exchange rate",)
    assert not {"exchange rates", "fx rates"} & set(FROZEN["planned.multi_currency"])
    areas = {a["id"]: a for a in cat._planned()}
    assert areas["planned.invoice_fields"]["synonyms"] == list(FROZEN["planned.invoice_fields"])
    assert areas["planned.country_packs"]["synonyms"] == [
        s for s in FROZEN["planned.country_packs"] if s not in ("vat", "gst")]
    assert areas["planned.multi_currency"]["synonyms"] == [*FROZEN["planned.multi_currency"], "exchange rates", "fx rates"]
    for token in ("vat", "gst", "exchange rate", "exchange rates", "fx rates", "bill", "import", "bank statement"):
        holders = {a["id"] for a in areas.values() if token in a["synonyms"]}
        expected = ("tax_engine" if token in ("vat", "gst") else "multi_currency" if "rate" in token
                    else "purchasing" if token == "bill" else "connector.csv_import" if token == "import"
                    else "connector.bank_statements")
        assert holders == {f"planned.{expected}"}
    assert "sign" not in areas["planned.esign"]["synonyms"]
    assert {"reconcile", "reconciliation"} <= set(areas["planned.payments_banking"]["synonyms"])
    assert {"email", "message", "send", "mail"} <= set(areas["planned.messaging"]["synonyms"])


@pytest.mark.skip(reason="the invoice-fields assistant work has not landed; enable when it retires planned.invoice_fields.")
def test_invoice_fields_is_absent_once_the_invoice_fields_work_lands():
    assert "planned.invoice_fields" not in {a["id"] for a in cat._planned()}


def test_override_data_is_bound_and_requires_payment_intent():
    records = json.loads(cat.OVERRIDES.read_text())
    supplier = next(r for r in records if r["area"] == "planned.purchasing")
    assert supplier["target"] == "op.pay_supplier"
    assert set(supplier["states"]) == {"installed", "available"}
    assert set(supplier["intent"]) == {"pay", "paid", "payment", "settle", "owe"}
    assert set(supplier["triggers"]) == {"bill", "bills", "supplier", "vendor", "supplier invoice"}
    assert set(supplier["exclude"]) >= {"purchase order", "order", "quote", "request",
                                         "bill the customer", "invoice the customer"}
    assert not any(r["target"] == "op.create_invoice" for r in records)
    assert {r["area"] for r in records} == {"planned.purchasing", "planned.stock"}
    assert len(records) == 3
    stock = next(r for r in records if r["area"] == "planned.stock")
    assert stock["target"] == "op.record_goods_received"
    assert set(stock["states"]) == {"installed", "available"}
    areas = {a["id"]: a for a in cat._planned()}
    for record in records:
        if record["target"] == "op.reverse_supplier_payment":
            continue  # operation-specific triggers need not be area synonyms
        assert set(record["triggers"]) <= set(areas[record["area"]]["synonyms"])


SAID = "pay the supplier bill for Copperline"
SUPPLIER_PAYMENTS = [SAID, "pay the supplier's invoice INV-7", "pay the vendor's bill"]


def test_planned_instead_reads_books_only_when_the_words_can_matter():
    class NoBooks:
        def __getattribute__(self, name):
            raise AssertionError("The connection must not be used")

    conn = NoBooks()
    assert cat.planned_instead(
        conn, "create_invoice", "invoice Acme Corporation for 5 Copper Wire from Main Store") is None
    # A supplier-payment synonym still reaches the real catalogue's books check.
    with pytest.raises(NoInspectionAvailable):
        cat.planned_instead(conn, "create_invoice", "pay the supplier bill")


@pytest.mark.parametrize("said", SUPPLIER_PAYMENTS)
@pytest.mark.parametrize("pick", ["planned.purchasing", "op.create_invoice", "none", "unsure"])
def test_real_registry_supplier_bill_stays_planned_before_activation(engine, as_released, pick, said):
    assert "pay_supplier" not in REGISTRY.contracts  # flip this expectation when its contract lands
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        assert {"planned.purchasing", "op.create_invoice"} <= {e.id for e, _ in named}
        d = cat.decide(cat.find_capability(conn, said), pick, named=named)
        assert d.outcome == "c" and d.capability == "planned.purchasing"
    before = books(engine)
    ev, _ = chat(engine, find_then(pick, said), said)
    assert ev[-1]["outcome"] == "c" and ev[-1]["capability"] == "planned.purchasing"
    assert ev[-1]["text"] == "Purchasing: purchase orders and supplier invoices is not in ERPClaw yet."
    assert books(engine) == before
    assert len(store.demand()) == 1 and store.demand()[0][4] == said


@pytest.mark.parametrize("said", SUPPLIER_PAYMENTS)
def test_a_direct_customer_invoice_call_cannot_handle_a_supplier_bill(engine, as_released, said):
    with engine.connect() as conn:
        d = cat.planned_instead(conn, "create_invoice", said)
    assert d is not None and d.outcome == "c" and d.capability == "planned.purchasing"
    before = books(engine)
    ev, _ = chat(engine, [Reply("", [call("create_invoice", customer="Acme Corporation",
                                       items=[{"item": "copper wire", "quantity": 1}])])], said)
    # This head's change-scope resolver can ask before resolve_args invokes the
    # guard. Either path must prevent a customer invoice card.
    assert all(e["type"] != "card" for e in ev)
    assert ev[-1]["type"] in ("ask", "answer", "refused")
    assert books(engine) == before


def bound_entries(engine):
    with engine.connect() as conn:
        return cat.catalogue(conn)


@pytest.mark.parametrize("customer", [
    "create an invoice for Acme",
    "make an invoice for Acme",
    "send Acme an invoice",
    "raise an invoice to Acme",
    "invoice Acme",
    "bill Acme",
    "create this invoice for Acme",
    "make the invoice for Acme",
    "raise that invoice to Acme",
])
@pytest.mark.parametrize("supplier_first", [True, False], ids=["supplier-first", "customer-first"])
@pytest.mark.parametrize("supplier_state", ["absent", "installed", "available"])
def test_customer_invoice_intent_keeps_its_decision(engine, monkeypatch, customer, supplier_first, supplier_state):
    entries = bound_entries(engine)
    if supplier_state != "absent":
        entries.append(cat.Entry("op.pay_supplier", "operation", "pay supplier", "", (), supplier_state,
                                 plugin="core", target="pay_supplier"))
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    clauses = ["pay the supplier bill", customer]
    said = " and ".join(clauses if supplier_first else reversed(clauses))
    named = cat.named_by_synonym(None, said)
    assert not any(e.overrides for e, _ in named)
    d = cat.decide(cat.find_capability(None, said), "op.create_invoice", named=named, mixed=cat.is_mixed(said))
    assert d.outcome == "c" and d.entry.id == "planned.purchasing"
    area = next(e for e in entries if e.id == "planned.purchasing")
    expected = cat.decide([area], area.id).text + " " + resolve._say("one_change")
    assert d.text == expected
    direct = cat.planned_instead(None, "create_invoice", said)
    assert direct is not None and direct.outcome == "c" and direct.text == expected


@pytest.mark.parametrize("document", ["bill", "invoice"])
def test_plain_supplier_payment_still_overrides_customer_invoice(engine, document):
    said = f"pay the supplier {document}"
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        assert any("op.create_invoice" in e.overrides for e, _ in named)
        d = cat.decide(cat.find_capability(conn, said), "op.create_invoice", named=named)
        direct = cat.planned_instead(conn, "create_invoice", said)
    assert d.outcome == "c" and d.capability == "planned.purchasing"
    assert direct.outcome == "c" and direct.capability == "planned.purchasing"


@pytest.mark.parametrize("noun", [
    "supplier", "vendor", "suppliers", "vendors",
    "supplier's", "vendor's", "supplier’s", "vendor’s",
    "suppliers'", "vendors'", "suppliers’", "vendors’",
    "supplier/vendor", "supplier-vendor",
])
def test_supplier_noun_variants_keep_payment_routes(engine, noun):
    with engine.connect() as conn:
        for form in (noun, noun.upper(), f"{noun},", f"{noun.upper()}!"):
            for phrase in (
                f"pay the {form}",  # the noun alone must activate the overlap trigger
                f"pay the {form} invoice INV-7",
                f"pay the {form} bill",
                f"pay the {form} bill INV-7",
                f"send an invoice to the {form} and pay the supplier bill",
                f"send the {form} an invoice and pay the supplier bill",
            ):
                named = cat.named_by_synonym(conn, phrase)
                candidates = cat.find_capability(conn, phrase)
                for pick in ("planned.purchasing", "op.create_invoice"):
                    d = cat.decide(candidates, pick, named=named)
                    assert d.outcome == "c" and d.capability == "planned.purchasing", (phrase, pick)
                    assert d.text == "Purchasing: purchase orders and supplier invoices is not in ERPClaw yet.", phrase
                direct = cat.planned_instead(conn, "create_invoice", phrase)
                assert direct is not None and direct.capability == "planned.purchasing", phrase
                assert direct.outcome == "c" and direct.text == (
                    "Purchasing: purchase orders and supplier invoices is not in ERPClaw yet."), phrase


@pytest.mark.parametrize("state", ["installed", "available", "planned", "absent"])
@pytest.mark.parametrize("pick", ["planned.purchasing", "op.create_invoice"])
def test_constructed_supplier_binding_preserves_activation_and_state(engine, monkeypatch, state, pick):
    entries = bound_entries(engine)
    if state != "absent":
        entries.append(cat.Entry("op.pay_supplier", "operation", "pay supplier", "Supplier payment", (), state,
                                 plugin="supplier_plugin", target="pay_supplier", wave="Core"))
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    found = cat.find_capability(None, SAID)
    named = cat.named_by_synonym(None, SAID)
    d = cat.decide(found, pick, named=named)
    if state in ("installed", "available"):
        assert d.entry.id == "op.pay_supplier"
        assert d.outcome == ("a" if state == "installed" else "c")
        if state == "available":
            assert d.capability == "available.op.pay_supplier"
            assert d.text == "That needs the supplier_plugin plugin, which is available but not installed in these books."
    else:
        assert d.outcome == "c" and d.capability == "planned.purchasing"
    if state == "installed":
        with pytest.raises(Refusal, match="Please choose pay supplier"):
            cat.planned_instead(None, "create_invoice", SAID)
    else:
        direct = cat.planned_instead(None, "create_invoice", SAID)
        assert direct.outcome == "c"
        assert direct.capability == ("available.op.pay_supplier" if state == "available" else "planned.purchasing")


@pytest.mark.parametrize("said, pick", [
    ("create a purchase order for this supplier", "planned.purchasing"),
    ("bill the customer for this supplier delivery", "op.create_invoice"),
    ("bill Acme for the supplier delivery", "op.create_invoice"),
    ("pay the supplier bill and create a purchase order", "planned.purchasing"),
    ("pay the supplier bill and invoice the customer", "op.create_invoice"),
    ("pay the supplier bill and bill Acme", "op.create_invoice"),
    ("Pay the supplier bill and invoice Acme", "op.create_invoice"),
    ("invoice Acme and pay the supplier bill", "op.create_invoice"),
    ("supplier bill", "planned.purchasing"),
    ("pay Copperline", "none"),
], ids=["purchase-order", "customer-billing", "bill-customer-name", "both-purchase-intents",
        "both-customer-intents", "both-named-customer-intents", "payment-then-invoice-customer",
        "invoice-customer-then-payment", "overlap-only", "payment-only"])
def test_supplier_override_does_not_steal_other_intents(engine, monkeypatch, said, pick):
    entries = bound_entries(engine) + [cat.Entry("op.pay_supplier", "operation", "pay supplier", "", (), "installed",
                                                plugin="core", target="pay_supplier")]
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    named = cat.named_by_synonym(None, said)
    assert not any(e.overrides for e, _ in named)
    mixed = cat.is_mixed(said)
    d = cat.decide(cat.find_capability(None, said), pick, named=named, mixed=mixed)
    assert d.entry is None or d.entry.id != "op.pay_supplier"
    if mixed:
        area = next(e for e in entries if e.id == "planned.purchasing")
        assert d.outcome == "c" and d.entry.id == "planned.purchasing"
        assert d.text == cat.decide([area], area.id).text + " " + resolve._say("one_change")
    elif pick != "none":
        assert d.entry.id == pick


@pytest.mark.parametrize("intent", ["pay", "paid", "payment", "settle", "owe"])
@pytest.mark.parametrize("document", ["supplier bill", "supplier bill INV-7", "supplier invoice", "supplier invoice INV-7"])
def test_each_supplier_payment_intent_activates_with_overlap(engine, monkeypatch, intent, document):
    entries = bound_entries(engine) + [cat.Entry("op.pay_supplier", "operation", "supplier payment", "", (),
                                                "installed", plugin="core", target="pay_supplier")]
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    said = f"{intent} {document}"
    d = cat.decide(cat.find_capability(None, said), "planned.purchasing", named=cat.named_by_synonym(None, said))
    assert d.outcome == "a" and d.entry.id == "op.pay_supplier"


def test_bound_target_must_be_a_candidate_and_a_model_pick_must_be_valid(engine, monkeypatch):
    entries = bound_entries(engine) + [cat.Entry("op.pay_supplier", "operation", "supplier payment", "", (),
                                                "installed", plugin="core", target="pay_supplier")]
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    found, named = cat.find_capability(None, SAID), cat.named_by_synonym(None, SAID)
    assert cat.decide(found, "op.invented", named=named).outcome != "a"
    found = [e for e in found if e.id != "op.pay_supplier"]
    assert cat.decide(found, "planned.purchasing", named=named).outcome != "a"
    assert cat.decide(cat.find_capability(None, SAID), "none", named=named).outcome != "a"


@pytest.mark.parametrize("state", ["installed", "available", "absent"])
def test_goods_receipt_binding_uses_only_a_real_active_target(engine, monkeypatch, state):
    entries = bound_entries(engine)
    if state != "absent":
        entries += [cat.Entry("op.record_goods_received", "operation", "goods receipt", "", (), state,
                              plugin="stock", target="record_goods_received")]
    monkeypatch.setattr(cat, "catalogue", lambda conn: entries)
    said = "receive the delivery into stock"
    d = cat.decide(cat.find_capability(None, said), "planned.stock", named=cat.named_by_synonym(None, said))
    assert d.outcome == ("a" if state == "installed" else "c")
    assert d.entry.id == ("planned.stock" if state == "absent" else "op.record_goods_received")
    if state == "available":
        assert d.capability == "available.op.record_goods_received" and "stock plugin" in d.text


def test_search_keeps_synonym_matches_and_prioritizes_installed_over_planned(monkeypatch):
    installed = cat.Entry("op.example", "operation", "example", "", ("special phrase",), "installed")
    planned = cat.Entry("planned.example", "area", "example", "", ("special phrase",), "planned", wave="later")
    descriptions = [cat.Entry(f"op.other_{i}", "operation", "special phrase", "special phrase", (), "installed")
                    for i in range(7)]
    monkeypatch.setattr(cat, "catalogue", lambda conn: [planned, *descriptions, installed])
    found = cat.find_capability(None, "special phrase")
    assert [e.id for e in found[:2]] == [installed.id, planned.id]
    assert len(found) == cat.MAX_CANDIDATES


def test_disabled_sales_keeps_availability_and_reenabled_sales_hides_its_area(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    before = books(engine)
    ev, _ = chat(engine, find_then("op.create_invoice", "invoice Acme"), "invoice Acme")
    assert ev[-1]["outcome"] == "c" and ev[-1]["capability"] == "available.op.create_invoice"
    assert "sales plugin, which is available but not installed" in ev[-1]["text"]
    assert books(engine) == before
    with engine.connect() as conn:
        assert "planned.sales" in {e.id for e in cat.catalogue(conn)}
    with engine.begin() as conn:
        # This head has no public re-enable helper; restore the install status.
        conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "sales").values(status="installed"))
    with engine.connect() as conn:
        assert "planned.sales" not in {e.id for e in cat.catalogue(conn)}


@pytest.mark.parametrize("said, pick, capability, wave", [
    ("reconcile the bank statement", "planned.payments_banking", "planned.payments_banking", "later"),
    ("set up a project with timesheets", "planned.projects", "planned.projects", "later"),
    ("take card payments online", "planned.connector.stripe", "planned.connector.stripe", "later"),
    ("close the year by chat", "planned.period_close", "planned.period_close", "v3.1"),
    ("e-sign the invoice", "planned.esign", "planned.esign", "v3.1"),
    ("sign with an electronic signature", "planned.esign", "planned.esign", "v3.1"),
    ("email INV-7 to Acme", "planned.messaging", "planned.messaging", "v3.0"),
    ("receive the delivery into stock", "planned.stock", "planned.stock", "v3.0"),
    ("sign me in", "none", cat.NO_MATCH, None),
    ("set the opening bank balance to 5000", "none", cat.NO_MATCH, None),
])
def test_honest_replies_write_no_books_or_outbox(engine, as_released, said, pick, capability, wave):
    assert "record_goods_received" not in REGISTRY.contracts
    assert [(c.name, c.planned, c.plugin) for c in REGISTRY.plugins["email"].contracts] == [("update_email_sender", None, "email")]
    opening_contracts = [c for n, c in REGISTRY.contracts.items() if "opening" in n and "stock" not in n]
    assert len(opening_contracts) == 1
    opening = opening_contracts[0]
    with engine.connect() as conn:
        entries = [e for e in cat.catalogue(conn) if e.target == opening.name]
    assert len(entries) == 1
    assert (entries[0].state, entries[0].wave, entries[0].plugin) == (
        "planned", opening.planned, REGISTRY.contract_plugin[opening.name])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(opening, "planned", None)
        with engine.connect() as conn:
            found = cat.find_capability(conn, "set the opening bank balance to 5000")
        targets = [e for e in found if e.state == "installed" and e.target and "opening" in e.target and "stock" not in e.target]
        assert len(targets) == 1
        assert targets[0].target == opening.name
        assert cat.decide(found, targets[0].id).outcome == "a"
    before = books(engine)
    with engine.connect() as conn:
        outbox = conn.execute(select(func.count()).select_from(m.outbox)).scalar_one()
        named = cat.named_by_synonym(conn, said)
    if capability == cat.NO_MATCH:
        assert named == []
    ev, _ = chat(engine, find_then(pick, said), said)
    assert ev[-1]["outcome"] in ("c", "d", "e")
    if capability:
        assert ev[-1]["capability"] == capability
    if wave:
        area = next(entry for entry, _ in named if entry.id == capability)
        assert ev[-1]["text"] == cat.planned_text(area.name, wave)
        assert "release" not in ev[-1]["text"].lower()
    if capability == cat.NO_MATCH:
        assert ev[-1]["outcome"] == "d"
    assert books(engine) == before
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.outbox)).scalar_one() == outbox


def test_collision_inventory_uses_only_synonyms(engine):
    with engine.connect() as conn:
        assert "planned.purchasing" in {e.id for e, _ in cat.named_by_synonym(conn, "bill")}
        # The area is present, but bare "invoice" is not one of its synonyms.
        assert "planned.invoice_fields" not in {e.id for e, _ in cat.named_by_synonym(conn, "invoice")}
    entry = cat.Entry("planned.example", "area", "invoice", "invoice narration", ("whole phrase only",),
                      "planned", wave="later")
    # A description match must never become an owner-named match.
    from unittest.mock import patch
    with patch.object(cat, "catalogue", return_value=[entry]):
        assert cat.named_by_synonym(None, "invoice narration") == []


@pytest.mark.skip(reason="Supplier-payment chat contract is unregistered; its delivery must enable this assertion.")
def test_registered_supplier_payment_installed_outcome(engine):
    with engine.connect() as conn:
        d = cat.decide(cat.find_capability(conn, SAID), "planned.purchasing", named=cat.named_by_synonym(conn, SAID))
    assert d.outcome == "a" and d.entry.target == "pay_supplier"


@pytest.mark.skip(reason="Goods-receipt chat contract is unregistered; its delivery must enable this assertion.")
def test_registered_goods_receipt_installed_outcome(engine):
    said = "receive the delivery into stock"
    with engine.connect() as conn:
        d = cat.decide(cat.find_capability(conn, said), "planned.stock", named=cat.named_by_synonym(conn, said))
    assert d.outcome == "a" and d.entry.target == "record_goods_received"


@pytest.mark.skip(reason="Outbound email has slots only; chat send registration and its binding must land first.")
def test_registered_outbound_email_installed_outcome(engine):
    said = "email INV-7 to Acme"
    with engine.connect() as conn:
        d = cat.decide(cat.find_capability(conn, said), "planned.messaging", named=cat.named_by_synonym(conn, said))
    assert d.outcome == "a" and d.entry.kind == "operation" and d.entry.target in REGISTRY.contracts


@pytest.mark.skip(reason="Opening-bank-position chat contract and its name are still unspecified and unregistered.")
def test_registered_opening_position_installed_outcome(engine):
    with engine.connect() as conn:
        found = cat.find_capability(conn, "set the opening bank balance to 5000")
    targets = [e for e in found if e.state == "installed" and e.target and "opening" in e.target and "stock" not in e.target]
    assert len(targets) == 1
    assert cat.decide(found, targets[0].id).outcome == "a"

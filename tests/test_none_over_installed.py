"""A declined catalogue choice must not deny an installed capability."""
from __future__ import annotations

import pytest

from bookdiff import snapshot
from erpclaw_assistant import store
from erpclaw_core import catalogue as cat
from test_catalogue import chat, find_then


SAID = "charge Acme Corporation for 5 copper wire"
HONEST = "I didn't do anything with that. If you want to draft invoices, say so with the details."
NO_MATCH_TEXT = ("ERPClaw does not do that yet. If you need it, just say so: requests like this "
                 "are recorded, so it is clear what to build next.")
PAYROLL_TEXT = "Payroll is not in ERPClaw yet."
MULTIPLE_INSTALLED_TEXT = "I didn't do anything with that. If you want to do one of those, say so with the details."


def decision(engine, said):
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        result = cat.decide(cat.find_capability(conn, said), "none", cat.switched_on(conn), named)
    return named, result


def test_none_over_installed_decision(engine, as_released):
    named, result = decision(engine, SAID)
    assert [(e.id, e.state) for e, _ in named] == [("op.create_invoice", "installed")]
    assert (result.outcome, result.entry, result.capability) == ("d", None, cat.NO_MATCH)
    assert result.text == HONEST
    assert "does not do that yet" not in result.text


def test_none_over_installed_chat_records_unchanged_demand_and_writes_nothing(engine, as_released):
    before = snapshot(engine)
    events, model = chat(engine, find_then("none", SAID), SAID)
    assert len(events) == 1
    assert (events[0]["outcome"], events[0]["capability"], events[0]["text"]) == ("d", cat.NO_MATCH, HONEST)
    assert "does not do that yet" not in events[0]["text"]
    assert len(model.tools_seen) == 2
    assert snapshot(engine) == before
    rows = store.demand()
    # The existing demand record stores no entry for NO_MATCH; the event carries its capability.
    assert len(rows) == 1 and rows[0][2:] == ("d", None, SAID)


def test_none_over_installed_metric_names_the_figure(engine):
    with engine.connect() as conn:
        metric = next(e for e in cat.catalogue(conn) if e.id == "metric.sales")
    assert metric.state == "installed"
    result = cat.decide([metric], "none", named=[(metric, 2)])
    assert (result.outcome, result.entry, result.capability, result.text) == (
        "d", None, cat.NO_MATCH,
        "I didn't do anything with that. If you want to see Sales, say so with the details.")


def test_none_with_multiple_names_and_ties_keeps_existing_results(engine, as_released):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    invoice = entries["op.create_invoice"]
    supplier = entries["op.add_supplier"]
    named = [(invoice, 3), (supplier, 2)]
    result = cat.decide([invoice, supplier], "none", named=named)
    assert (result.outcome, result.capability, result.text) == ("d", cat.NO_MATCH, MULTIPLE_INSTALLED_TEXT)
    tied = [(invoice, 3), (supplier, 3)]
    assert cat.decide([invoice, supplier], "none", named=tied) == cat.decide(
        [invoice, supplier], "unsure", named=tied)


def test_none_over_planned_keeps_its_reply(engine, as_released):
    named, result = decision(engine, "run payroll")
    assert [(e.id, e.state) for e, _ in named] == [("planned.payroll", "planned")]
    assert (result.outcome, result.capability, result.text) == ("c", "planned.payroll", PAYROLL_TEXT)
    events, _ = chat(engine, find_then("none", "run payroll"), "run payroll")
    assert (events[0]["outcome"], events[0]["capability"], events[0]["text"]) == (
        "c", "planned.payroll", PAYROLL_TEXT)


def test_none_with_nothing_named_keeps_its_reply(engine, as_released):
    said = "fly me to the moon"
    named, result = decision(engine, said)
    assert named == []
    with engine.connect() as conn:
        expected = NO_MATCH_TEXT + " " + cat.switched_on(conn)
    assert (result.outcome, result.capability, result.text) == ("d", cat.NO_MATCH, expected)
    events, _ = chat(engine, find_then("none", said), said)
    assert (events[0]["outcome"], events[0]["capability"], events[0]["text"]) == ("d", cat.NO_MATCH, expected)


@pytest.mark.parametrize("score", [6, 5])
def test_none_margin_rejects_near_ties(score):
    installed = cat.Entry("op.example", "operation", "example", "example", (), "installed")
    refused = cat.Entry("unsupported.edit_posted", "area", "editing", "editing", (), "unsupported", reply="refused")
    result = cat.decide([installed, refused], "none", named=[(installed, score), (refused, 8)])
    assert result.capability == cat.NO_MATCH, f"8 vs {score}: {result.capability}"


@pytest.mark.parametrize("score,expected", [(2, "unsupported.edit_posted"), (4, "unsupported.edit_posted"),
                                           (8, cat.NO_MATCH), (0, "unsupported.edit_posted")])
def test_none_margin_boundaries(score, expected):
    installed = cat.Entry("op.example", "operation", "example", "example", (), "installed")
    refused = cat.Entry("unsupported.edit_posted", "area", "editing", "editing", (), "unsupported", reply="refused")
    result = cat.decide([installed, refused], "none", named=[(installed, score), (refused, 8)])
    assert result.capability == expected


def test_none_margin_considers_second_refusal():
    installed = cat.Entry("op.example", "operation", "example", "example", (), "installed")
    refused = cat.Entry("unsupported.edit_posted", "area", "editing", "editing", (), "unsupported", reply="refused")
    other = cat.Entry("planned.payroll", "area", "payroll", "payroll", (), "planned", wave="later")
    result = cat.decide([installed, refused, other], "none", named=[(installed, 2), (refused, 8), (other, 6)])
    assert result.capability == cat.NO_MATCH


def test_none_applies_shadowing_before_first_named_state():
    installed = cat.Entry("op.example", "operation", "example", "example", (), "installed")
    refused = cat.Entry("unsupported.edit_posted", "area", "editing", "editing", (), "unsupported",
                        reply="refused", overrides=(installed.id,))
    result = cat.decide([installed, refused], "none", named=[(installed, 8), (refused, 2)])
    assert (result.capability, result.text) == ("unsupported.edit_posted", "refused")


def test_none_margin_does_not_depend_on_supplier_binding(tmp_path, monkeypatch):
    import json
    records = json.loads(cat.OVERRIDES.read_text())
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps([r for r in records if r["target"] != "op.pay_supplier"]))
    monkeypatch.setattr(cat, "OVERRIDES", path)
    test_none_margin_boundaries(2, "unsupported.edit_posted")


def test_none_without_names_matches_catalogue_reply_byte_for_byte(engine, as_released):
    said = "fly me to the moon"
    named, result = decision(engine, said)
    assert named == []
    events, _ = chat(engine, find_then("none", said), said)
    assert events[0]["capability"] == cat.NO_MATCH
    assert events[0]["text"].encode("utf-8") == result.text.encode("utf-8")


def test_none_with_two_installed_metrics_names_both(engine, monkeypatch):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    sales, receivables = entries["metric.sales"], entries["metric.receivables"]
    assert sales.state == receivables.state == "installed"
    monkeypatch.setattr(cat, "named_by_synonym", lambda conn, text: [(sales, 3), (receivables, 2)])
    said = "show sales and receivables"
    events, _ = chat(engine, find_then("none", said), said)
    assert events[0]["capability"] == cat.NO_MATCH
    assert events[0]["text"] == "I didn't do anything with that. I can see Sales or see Owed to us."
    assert "one of those" not in events[0]["text"]


def test_none_with_installed_operation_and_metric_names_both(engine, monkeypatch):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    invoice, sales = entries["op.create_invoice"], entries["metric.sales"]
    assert invoice.state == sales.state == "installed"
    monkeypatch.setattr(cat, "named_by_synonym", lambda conn, text: [(invoice, 3), (sales, 2)])
    said = "draft an invoice and show sales"
    events, _ = chat(engine, find_then("none", said), said)
    assert events[0]["capability"] == cat.NO_MATCH
    assert events[0]["text"] == "I didn't do anything with that. I can draft invoices or see Sales."

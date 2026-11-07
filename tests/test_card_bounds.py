"""Card bounds apply to the complete stored preview without truncation."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.contracts import Preview, Shown
from erpclaw_core.masters import create_item, create_party, find_customer, find_item
from erpclaw_core.plugins import REGISTRY

ARGS = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]}
SHOWN_FOOTER = len(approvals.FOOTER) + 1  # the engine's own footer, after the contract's and a space


def _cards(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.approval)).scalar_one()


def _with_preview(monkeypatch, build) -> None:
    """The invoice's preview replaced by one whose card `build` writes into a fresh recorder; the read set, the input
    and every check after the preview stay the engine's own."""
    def preview(ctx, inp):
        sh = Shown(ctx.currency)
        sh.title("Create and submit a sales invoice")
        build(sh)
        return Preview(sh.card(), "token")
    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "preview", preview)


def _lines(n):  # n lines as shown: the company line is the card's first
    return lambda sh: [sh.line("Item", "x") for _ in range(n - 1)]


# (bound, the card at it, the card one over it, what to measure on the stored card)
BOUNDS = [
    ("title", lambda sh: sh.title("😀" * 200), lambda sh: sh.title("😀" * 201), lambda c: len(c["title"])),
    ("lines", _lines(100), _lines(101), lambda c: len(c["lines"])),
    ("label", lambda sh: sh.line("界" * 120, "x"), lambda sh: sh.line("界" * 121, "x"),
     lambda c: max(len(k) for k, _ in c["lines"])),
    ("value", lambda sh: sh.line("Item", "é" * 500), lambda sh: sh.line("Item", "é" * 501),
     lambda c: max(len(v) for _, v in c["lines"])),
    ("books", lambda sh: [sh.book("b") for _ in range(50)], lambda sh: [sh.book("b") for _ in range(51)],
     lambda c: len(c["books"])),
    ("book", lambda sh: sh.book("b" * 300), lambda sh: sh.book("b" * 301), lambda c: max(map(len, c["books"]))),
    ("warnings", lambda sh: [sh.warning("w") for _ in range(50)], lambda sh: [sh.warning("w") for _ in range(51)],
     lambda c: len(c["warnings"])),
    ("warning", lambda sh: sh.warning("w" * 300), lambda sh: sh.warning("w" * 301),
     lambda c: max(map(len, c["warnings"]))),
    ("footer", lambda sh: sh.footer("f" * (500 - SHOWN_FOOTER)), lambda sh: sh.footer("f" * (501 - SHOWN_FOOTER)),
     lambda c: len(c["footer"])),
]
LIMIT = {"title": 200, "lines": 100, "label": 120, "value": 500, "books": 50, "book": 300, "warnings": 50,
         "warning": 300, "footer": 500}


@pytest.mark.parametrize("bound, at, over, measure", BOUNDS, ids=[b[0] for b in BOUNDS])
def test_a_card_at_each_bound_is_made_whole(engine, monkeypatch, bound, at, over, measure):
    _with_preview(monkeypatch, at)
    out = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    assert out.approval_id and out.card is not None
    assert measure(out.card) == LIMIT[bound]  # exactly at the bound, as stored and shown: nothing cut


@pytest.mark.parametrize("bound, at, over, measure", BOUNDS, ids=[b[0] for b in BOUNDS])
def test_a_card_one_over_any_bound_is_refused_as_too_large_and_nothing_is_stored(engine, monkeypatch, bound, at,
                                                                                 over, measure):
    _with_preview(monkeypatch, over)
    before = snapshot(engine)
    with pytest.raises(Refusal) as e:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    assert e.value.code == "card_too_large", (e.value.code, e.value.message)
    said = e.value.message
    assert said and said[0].isupper() and said.endswith(".") and "\n" not in said  # a plain sentence
    assert_changes(before, snapshot(engine), {})  # no card, truncated or not


def test_a_too_large_card_in_a_turn_is_a_refusal_with_its_code_and_no_card(engine, monkeypatch):
    _with_preview(monkeypatch, BOUNDS[6][2])  # 51 warnings
    c = TestClient(create_app(engine, ScriptedModel([Reply("", [call("create_invoice", **ARGS)])]),
                              embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    n = _cards(engine)
    r = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 2 copper wire"})
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert [e["type"] for e in events if e["type"] == "card"] == [], events
    assert any(e["type"] == "refused" and e["code"] == "card_too_large" for e in events), events
    assert _cards(engine) == n



def _size(card):
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _with_sized_preview(monkeypatch, size):
    def preview(ctx, inp):
        sh = Shown(ctx.currency)
        sh.title('Invoice résumé 😀 "quoted"\n')
        for _ in range(99):
            sh.line("l" * 120, "")
        for _ in range(50):
            sh.book("")
        card = sh.card()
        complete = card.as_dict()
        complete["lines"].insert(0, ["Company", ctx.company_name])
        complete["footer"] = approvals.FOOTER
        remaining = size - _size(complete)
        assert remaining > 0
        for i in range(len(card.lines)):
            take = min(remaining, 500)
            card.lines[i] = (card.lines[i][0], "x" * take)
            remaining -= take
        for i in range(len(card.books)):
            take = min(remaining, 300)
            card.books[i] = "x" * take
            remaining -= take
        assert remaining == 0
        return Preview(card, "token")

    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "preview", preview)


def test_a_card_of_exactly_65536_compact_utf8_bytes_is_stored_whole(engine, monkeypatch):
    _with_sized_preview(monkeypatch, 65536)
    out = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    assert _size(out.card) == 65536
    with engine.connect() as conn:
        stored = conn.execute(select(m.approval.c.card_json).where(m.approval.c.id == out.approval_id)).scalar_one()
    assert json.loads(stored) == out.card


def test_a_card_of_65537_compact_utf8_bytes_is_refused_without_storing_anything(engine, monkeypatch):
    _with_sized_preview(monkeypatch, 65537)
    before = snapshot(engine)
    with pytest.raises(Refusal) as e:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    assert e.value.code == "card_too_large"
    assert snapshot(engine) == before


def test_a_card_with_every_field_at_its_maximum_exceeds_the_whole_card_cap():
    from erpclaw_core.contracts import Card, check_card_amounts

    card = Card("t" * 200, [("l" * 120, "v" * 500)] * 100, ["b" * 300] * 50,
                ["w" * 300] * 50, "f" * 500, currency="USD").as_dict()
    assert _size(card) == 93887
    with pytest.raises(Refusal) as e:
        check_card_amounts(card)
    assert e.value.code == "card_too_large"


def test_a_fifty_line_invoice_sample_with_120_character_values_passes(engine, monkeypatch):
    def build(sh):
        sh.title("Create and submit a sales invoice")
        for i in range(49):  # the company is the first of the fifty displayed lines
            text = f"1 x Copper wire spool {i + 1:02d} for the workshop @ 25.00 = 25.00"
            sh.line("Item", text.ljust(120))
        sh.book("Accounts Receivable +1,225.00")
        sh.book("Sales +1,225.00")

    _with_preview(monkeypatch, build)
    out = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    assert len(out.card["lines"]) == 50
    assert all(len(v) == 120 for _, v in out.card["lines"][1:])
    assert 6500 < _size(out.card) < 7000


def test_a_real_fifty_item_invoice_with_120_character_item_values_passes(engine):
    from erpclaw_sales import service as s

    # Submitting an existing draft does not have the new-invoice input's twenty-item limit.
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "Sample Customer", "ap@sample.example", "Pune")
        customer = find_customer(ctx, "Sample Customer")
        # The invoice's real formatter adds twenty characters to each item name.
        names = [f"Part {i:02d} " + "z" * 92 for i in range(50)]
        for name in names:
            create_item(ctx, name, Decimal("25.00"))
        items = [find_item(ctx, name) for name in names]
        invoice = s.create_invoice(ctx, customer, [(item, Decimal(1)) for item in items], ctx.today,
                                   submit=False, email=False)
    out = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": invoice["number"]})
    lines = [v for k, v in out.card["lines"] if k == "Item"]
    assert len(lines) == 50 and {len(v) for v in lines} == {120}
    assert all(name in value for name, value in zip(names, lines))
    assert _size(out.card) < 65536

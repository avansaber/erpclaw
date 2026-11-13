"""Master lists through chat and the read contracts."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from bookdiff import snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_assistant.resolve_text import table
from erpclaw_core import catalogue, models
from erpclaw_core.common import Ctx, company_ctx, new_id
from erpclaw_core.core_contracts import MASTER_PAGE_SIZE, TEXT
from erpclaw_core.db import read_only
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY


def chat(engine, said, contract_name, **args):
    with engine.connect() as conn:
        choices = catalogue.find_capability(conn, said)
    assert any(e.target == contract_name and e.state == "installed" for e in choices)
    model = ScriptedModel([Reply("", [call("find_capability", request=said)]),
                           Reply("", [call("choose_capability", pick=f"op.{contract_name}")]),
                           Reply("", [call(contract_name, **args)])])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    conversation = client.post("/v1/conversations").json()["id"]
    events = client.post(f"/v1/conversations/{conversation}/messages", json={"text": said}).json()["events"]
    assert model.tools_seen[-1] == [contract_name]
    return events


@pytest.mark.parametrize("what,name,said", [
    ("customer", "list_customers", "who are my customers?"),
    ("supplier", "list_suppliers", "list my suppliers"),
    ("item", "list_items", "list my items"),
])
def test_lists_in_chat_do_not_change_books(engine, as_released, what, name, said):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if what == "item":
            create_item(ctx, "Birchwood Widget", Decimal("7.1234"), "each")
        else:
            create_party(ctx, what, "Birchwood Workshop")
    before = snapshot(engine)
    events = chat(engine, said, name)
    assert snapshot(engine) == before
    assert [e["type"] for e in events] == ["answer"]
    assert "Birchwood" in events[0]["text"]
    assert "Total" in events[0]["text"]
    assert all("approval_id" not in e for e in events)
    if what == "item":
        assert "7.1234 USD per each" in events[0]["text"]
        assert any(a["value"] == "7.1234" and a["currency"] == "USD" for a in events[0]["amounts"])


def test_pages_have_stable_raw_name_order_and_total(engine, as_released):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for n in range(100):
            create_party(ctx, "customer", f"Harborview {n:03}")
        create_party(ctx, "customer", "harborview Studio")
        create_party(ctx, "customer", "Hárborview Studio")
    with engine.connect() as conn:
        rows = conn.execute(select(models.party.c.name, models.party.c.id).where(
            models.party.c.company_id == company_ctx(conn).company_id, models.party.c.kind == "customer")).all()
    ordered = sorted(rows, key=lambda r: (r.name, r.id))
    before = snapshot(engine)
    first = chat(engine, "who are my customers?", "list_customers")[0]["text"]
    second = chat(engine, "show customers page 2", "list_customers", page=2)[0]["text"]
    assert snapshot(engine) == before
    assert f"Total customers: {len(ordered)}." in first and f"Total customers: {len(ordered)}." in second
    for page, text in ((ordered[:MASTER_PAGE_SIZE], first), (ordered[MASTER_PAGE_SIZE:2 * MASTER_PAGE_SIZE], second)):
        positions = [text.index(row.name) for row in page]
        assert positions == sorted(positions)
    assert ordered[MASTER_PAGE_SIZE].name not in first
    assert "page 2" in first and "by name" in first
    assert chat(engine, "who are my customers?", "list_customers")[0]["text"] == first


def test_item_prices_keep_the_stored_spelling(engine, as_released):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_item(ctx, "Rate One", Decimal("19.90"))
        create_item(ctx, "Rate Two", Decimal("1000.1200"))
        create_item(ctx, "Rate Three", Decimal("7.1234"))
    before = snapshot(engine)
    event = chat(engine, "list my items", "list_items")[0]
    assert snapshot(engine) == before
    for amount in ("19.90", "1000.1200", "7.1234"):
        assert f"{amount} USD" in event["text"]
        assert any(a["value"] == amount and a["kind"] == "unit_price" for a in event["amounts"])
    assert "1027.14" not in event["text"]


@pytest.mark.parametrize("what,contract", [("customer", "list_customers"), ("supplier", "list_suppliers"),
                                          ("item", "list_items")])
def test_empty_and_isolation_in_company_context(engine, as_released, what, contract):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        other = dict(conn.execute(select(models.company).where(models.company.c.id == ctx.company_id)).mappings().one())
        other.update(id=new_id(), name="Other Books")
        conn.execute(models.company.insert().values(**other))
        other_ctx = Ctx(conn, other["id"], other["name"], ctx.currency, ctx.today)
        if what == "item":
            create_item(ctx, "Visible Master", Decimal("1.0000"))
        else:
            create_party(ctx, what, "Visible Master")
    before = snapshot(engine)
    with read_only(engine) as conn:
        ctx = Ctx(conn, other["id"], other["name"], "USD", ctx.today)
        result = REGISTRY.contracts[contract].run(ctx, REGISTRY.contracts[contract].input_model())
    assert snapshot(engine) == before
    assert table()["text"]["no_records"].format(what=what) in result.text
    assert "Total" in result.text and "0" in result.text and "add" in result.text
    assert "Visible Master" not in result.text
    assert TEXT["en"]["no_records"] == table()["text"]["no_records"]


@pytest.mark.parametrize("what,contract", [("customer", "list_customers"), ("supplier", "list_suppliers"),
                                          ("item", "list_items")])
def test_name_filter_uses_existing_resolution(engine, as_released, what, contract):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if what == "item":
            ids = [create_item(ctx, name, Decimal("1.0000")) for name in
                   ("Fernway Workshop", "Fernway Studio", "Other Place")]
        else:
            ids = [create_party(ctx, what, name) for name in
                   ("Fernway Workshop", "Fernway Studio", "Other Place")]
    before = snapshot(engine)
    event = chat(engine, f"show me {what} Fernway Workshop", contract, name_filter="Fernway Workshop")[0]
    assert event["type"] == "answer" and "Fernway Workshop" in event["text"] and "Fernway Studio" not in event["text"]
    assert f"Total {what}s: 1." in event["text"] and "Other Place" not in event["text"]
    partial_events = chat(engine, f"show me {what} Fernway", contract, name_filter="Fernway")
    assert [e["type"] for e in partial_events] == ["answer"]
    partial = partial_events[0]
    assert "Fernway Workshop" in partial["text"] and "Fernway Studio" in partial["text"]
    assert "Other Place" not in partial["text"] and f"Total {what}s: 2." in partial["text"]
    assert not any(e.get("code", "").endswith("_ambiguous") for e in partial_events)
    unknown = chat(engine, f"show me {what} Missing Place", contract, name_filter="Missing Place")[0]
    assert unknown["type"] == "answer"
    assert f'No {what}s match "Missing Place".' in unknown["text"]
    total = {"customer": 8, "supplier": 3, "item": 6}[what]
    assert f"There are {total} {what}s in all." in unknown["text"]
    assert f"Total {what}s: 0." in unknown["text"]
    assert all(record_id not in reply["text"] for record_id in ids for reply in (event, partial, unknown))
    assert snapshot(engine) == before

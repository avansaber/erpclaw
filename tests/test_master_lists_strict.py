"""Registry and result boundaries for master reads."""
from decimal import Decimal
from uuid import uuid4

import pytest

from bookdiff import snapshot
from erpclaw_core import catalogue
from erpclaw_core.common import company_ctx
from erpclaw_core.contracts import Amount, Result, check_answer
from erpclaw_core.db import read_only
from erpclaw_core.masters import create_party
from erpclaw_core.plugins import REGISTRY, enabled_plugins


NAMES = ("list_customers", "list_suppliers", "list_items")


def test_offered_set_and_switched_on_sentence(engine, as_released):
    with engine.connect() as conn:
        enabled = enabled_plugins(conn)
        offered = {c.name for c in REGISTRY.contracts.values()
                   if REGISTRY.contract_plugin[c.name] in enabled and not c.planned}
        text = catalogue.switched_on(conn)
    assert offered == {"set_up_company", "add_customer", "add_supplier", "add_cash_account", "add_item",
                       *NAMES, "create_invoice", "submit_invoice",
                       "record_payment", "cancel_invoice", "reverse_payment"}
    assert text == ("Right now I can add customers, add suppliers, add cash accounts, add items, list customers, "
                    "list suppliers, list items, draft invoices, submit a draft invoice, record payments, "
                    "cancel an invoice, and reverse a payment.")


@pytest.mark.parametrize("name", NAMES)
def test_read_contract_shape_and_no_changes(engine, as_released, name):
    contract = REGISTRY.contracts[name]
    assert contract.effect == "read" and contract.run is not None
    assert contract.preview is None and contract.commit is None and not contract.planned
    assert not contract.input_model.model_json_schema().get("required")
    before = snapshot(engine)
    with read_only(engine) as conn:
        result = contract.run(company_ctx(conn), contract.input_model())
        check_answer(contract, result)
    assert snapshot(engine) == before


def test_exact_page_wording_comes_from_table(engine, as_released):
    from erpclaw_core.core_contracts import MASTER_PAGE_SIZE, say
    contract = REGISTRY.contracts["list_customers"]
    with read_only(engine) as conn:
        existing_count = contract.run(company_ctx(conn), contract.input_model()).data["count"]
    added_count = max(1, MASTER_PAGE_SIZE + 1 - existing_count)
    name_prefix = uuid4().hex
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for n in range(added_count):
            create_party(ctx, "customer", f"Aardvark Workshop {name_prefix} {n}")
    with read_only(engine) as conn:
        result = contract.run(company_ctx(conn), contract.input_model())
    expected_count = existing_count + added_count
    assert result.data["count"] == expected_count
    assert result.text.startswith(say("master_page", plural_capitalised="Customers", page=1))
    assert say("master_total", plural="customers", count=expected_count) in result.text
    assert say("master_more", next_page=2, what="customer") in result.text


def test_single_added_customer_on_one_page_has_no_next_page(engine, as_released):
    from erpclaw_core.core_contracts import MASTER_PAGE_SIZE, say
    contract = REGISTRY.contracts["list_customers"]
    name = f"Single Customer {uuid4().hex}"
    inp = contract.input_model()
    with read_only(engine) as conn:
        ctx = company_ctx(conn)
        existing_count = contract.run(ctx, inp).data["count"]
        if existing_count >= MASTER_PAGE_SIZE:
            inp = contract.input_model(name_filter=name)
            existing_count = contract.run(ctx, inp).data["count"]
            while existing_count:
                name = f"Single Customer {uuid4().hex}"
                inp = contract.input_model(name_filter=name)
                existing_count = contract.run(ctx, inp).data["count"]
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", name)
    with read_only(engine) as conn:
        result = contract.run(company_ctx(conn), inp)
    expected_count = existing_count + 1
    assert expected_count <= MASTER_PAGE_SIZE
    assert result.data["count"] == expected_count
    assert result.text.startswith(say("master_page", plural_capitalised="Customers", page=1))
    assert say("master_total", plural="customers", count=expected_count) in result.text
    assert say("master_more", next_page=2, what="customer") not in result.text


def test_exact_stored_unit_price_is_checked(engine):
    contract = REGISTRY.contracts["list_items"]
    amount = Amount("rate", "Example", "1000.1200", "USD", kind="unit_price")
    assert check_answer(contract, Result("Example: 1000.1200 USD", amounts=[amount]))[0]["value"] == "1000.1200"


def test_case_and_unicode_names_remain_distinct_and_ordered(engine, as_released):
    names = ["Aardvark Workshop", "aardvark workshop", "Áardvark Workshop"]
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name in names:
            create_party(ctx, "customer", name)
    contract = REGISTRY.contracts["list_customers"]
    before = snapshot(engine)
    with read_only(engine) as conn:
        first = contract.run(company_ctx(conn), contract.input_model()).text
        second = contract.run(company_ctx(conn), contract.input_model()).text
    assert snapshot(engine) == before
    assert all(name in first for name in names)
    assert [first.index(name) for name in names] == sorted(first.index(name) for name in names)
    assert first == second


def test_reads_keep_each_companys_masters_separate(engine, as_released):
    from sqlalchemy import select
    from erpclaw_core import models
    from erpclaw_core.common import Ctx, new_id
    from erpclaw_core.masters import create_item

    with engine.begin() as conn:
        own = company_ctx(conn)
        other = dict(conn.execute(select(models.company).where(models.company.c.id == own.company_id)).mappings().one())
        other.update(id=new_id(), name="Other Books")
        conn.execute(models.company.insert().values(**other))
        other_ctx = Ctx(conn, other["id"], other["name"], own.currency, own.today)
        for what in ("customer", "supplier"):
            create_party(own, what, "Own Master")
            create_party(other_ctx, what, "Other Master")
        create_item(own, "Own Master", Decimal("12.30"))
        create_item(other_ctx, "Other Master", Decimal("99.00"))
    before = snapshot(engine)
    with read_only(engine) as conn:
        contexts = [Ctx(conn, own.company_id, own.company_name, own.currency, own.today),
                    Ctx(conn, other["id"], other["name"], own.currency, own.today)]
        for name in NAMES:
            contract = REGISTRY.contracts[name]
            first, second = [contract.run(ctx, contract.input_model()).text for ctx in contexts]
            assert "Own Master" in first and "Other Master" not in first
            assert "Other Master" in second and "Own Master" not in second
    assert snapshot(engine) == before


def test_supplier_name_lookup_stays_available_in_chat(engine, as_released):
    from test_master_lists_chat import chat
    with engine.begin() as conn:
        create_party(company_ctx(conn), "supplier", "Fernway Workshop")
    before = snapshot(engine)
    event = chat(engine, "show me supplier Fernway Workshop", "list_suppliers", name_filter="Fernway Workshop")[0]
    assert event["type"] == "answer" and "Fernway Workshop" in event["text"]
    assert snapshot(engine) == before

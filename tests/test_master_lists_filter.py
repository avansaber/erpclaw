"""Name filters return all matching masters, with Unicode-aware paging."""
from decimal import Decimal

import pytest

from erpclaw_core.common import company_ctx
from erpclaw_core.core_contracts import MASTER_PAGE_SIZE
from erpclaw_core.db import read_only
from erpclaw_core.masters import create_item, create_party, find_customer, find_item, find_supplier
from erpclaw_core.plugins import REGISTRY


@pytest.mark.parametrize("what", ["customer", "supplier", "item"])
def test_filter_returns_all_matches_and_empty_list(engine, as_released, what):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name in ("Fernway Workshop", "Fernway Studio", "Other Place"):
            if what == "item":
                create_item(ctx, name, Decimal("2.00"))
            else:
                create_party(ctx, what, name)
    contract = REGISTRY.contracts[f"list_{what}s"]
    with read_only(engine) as conn:
        ctx = company_ctx(conn)
        found = contract.run(ctx, contract.input_model(name_filter="Fernway"))
        empty = contract.run(ctx, contract.input_model(name_filter="Missing"))
    assert found.data["count"] == 2
    assert "Fernway Workshop" in found.text and "Fernway Studio" in found.text
    assert "Other Place" not in found.text
    assert empty.data["count"] == 0
    assert f'No {what}s match "Missing".' in empty.text
    total = {"customer": 8, "supplier": 3, "item": 6}[what]
    assert f"There are {total} {what}s in all." in empty.text


@pytest.mark.parametrize("stored,query", [("東京商店", "東京商店"), ("Straße", "STRASSE")])
@pytest.mark.parametrize("what", ["customer", "supplier", "item"])
def test_filter_matches_unicode_names(engine, as_released, stored, query, what):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if what == "item":
            create_item(ctx, stored, Decimal("2.00"))
        else:
            create_party(ctx, what, stored)
    contract = REGISTRY.contracts[f"list_{what}s"]
    with read_only(engine) as conn:
        found = contract.run(company_ctx(conn), contract.input_model(name_filter=query))
    assert found.data["count"] == 1 and stored in found.text


@pytest.mark.parametrize("stored,query", [("東京商店", "東京商店"), ("Straße", "STRASSE")])
@pytest.mark.parametrize("what", ["customer", "supplier", "item"])
def test_resolver_matches_unicode_names(engine, as_released, stored, query, what):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if what == "item":
            create_item(ctx, stored, Decimal("2.00"))
        else:
            create_party(ctx, what, stored)
    resolver = {"customer": find_customer, "supplier": find_supplier, "item": find_item}[what]
    with read_only(engine) as conn:
        assert resolver(company_ctx(conn), query)["name"] == stored


@pytest.mark.parametrize("what", ["customer", "supplier", "item"])
def test_filter_pages_only_matches_and_stops_at_last(engine, as_released, what):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for n in range(MASTER_PAGE_SIZE + 1):
            name = f"Fernway {n:03}"
            if what == "item":
                create_item(ctx, name, Decimal("2.00"))
            else:
                create_party(ctx, what, name)
        if what == "item":
            create_item(ctx, "Other Place", Decimal("2.00"))
        else:
            create_party(ctx, what, "Other Place")
    contract = REGISTRY.contracts[f"list_{what}s"]
    with read_only(engine) as conn:
        ctx = company_ctx(conn)
        pages = [contract.run(ctx, contract.input_model(name_filter="Fernway", page=page))
                 for page in (1, 2, 3)]
    assert all(page.data["count"] == MASTER_PAGE_SIZE + 1 for page in pages)
    assert "Fernway 000" in pages[0].text and "Fernway 025" not in pages[0].text
    assert "Fernway 025" in pages[1].text and "Fernway 000" not in pages[1].text
    assert "Other Place" not in pages[0].text and "Other Place" not in pages[1].text
    assert "page 2" in pages[0].text
    assert "page 3" not in pages[1].text
    assert "There are no" in pages[2].text and "page 4" not in pages[2].text


def test_unfiltered_last_page_has_no_next_page(engine, as_released):
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "Only Customer")
    contract = REGISTRY.contracts["list_customers"]
    with read_only(engine) as conn:
        result = contract.run(company_ctx(conn), contract.input_model())
    assert 1 <= result.data["count"] <= MASTER_PAGE_SIZE
    assert "page 2" not in result.text


def test_single_filtered_match_is_not_repeated_on_later_page(engine, as_released):
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "Sole Juniper")
    contract = REGISTRY.contracts["list_customers"]
    with read_only(engine) as conn:
        result = contract.run(company_ctx(conn), contract.input_model(name_filter="Sole Juniper", page=2))
    assert result.data["count"] == 1
    assert "Sole Juniper" not in result.text
    assert "There are no customers on this page." in result.text
    assert "page 3" not in result.text

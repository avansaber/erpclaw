"""Pure resolved-ID payment planning; recording selects, no SQL/network.

Does not qualify database transactions, privileges, live approval or activation.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erpclaw_core import approvals, supplier_payments as p
from erpclaw_core.common import Refusal

D = Decimal
APR5, APR8, APR10 = date(2026, 4, 5), date(2026, 4, 8), date(2026, 4, 10)


class Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def scalars(self): return iter(self.rows)
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)


class Recording:
    def __init__(self):
        self.supplier = dict(id="supplier", company_id="co", kind="supplier", name="Copperline Metals")
        self.bank = dict(id="bank", company_id="co", account_type="bank", root_type="asset", is_group=False,
                         frozen=False, number="1100", name="Bank")
        self.ap = dict(id="ap", company_id="co", account_type="payable", root_type="liability", is_group=False,
                       frozen=False, number="2100", name="Accounts Payable")
        self.items, self.settled_items, self.queries, self.mappings = [], [], [], ["bank-map", "ap-map"]
        self.postings, self.lines, self.histories, self.payments = {}, {}, {}, {}
        self.reversed = set()
    def execute(self, statement):
        assert statement.is_select, "A read-only planner attempted a write"
        self.queries.append(statement)
        params = statement.compile().params
        table = next(iter(statement.selected_columns)).table.name
        if table == "party": rows = [] if self.supplier is None else [self.supplier]
        elif table == "account": rows = [] if self.bank is None else [self.bank]
        elif table == "supplier_payable":
            rows = ([row for row in self.items+self.settled_items if row["id"] == params["id_1"]]
                    if "id_1" in params else self.items)
        elif table == "posting": rows = [{"id": "reversal"}] if params["reverses_posting_id_1"] in self.reversed else []
        elif table == "ledger_entry": rows = self.lines[params["posting_id_1"]]
        elif table == "supplier_payment_allocation":
            rows = (self.histories.get(params["payable_id_1"], []) if "payable_id_1" in params else
                    [row for group in self.histories.values() for row in group
                     if row["payment_id"] == params["payment_id_1"]])
        elif table == "supplier_payment": rows = [self.payments[params["id_1"]]] if params["id_1"] in self.payments else []
        elif table == "account_map": rows = self.mappings
        else: raise AssertionError(f"Unexpected read: {table}")
        if statement._limit_clause is not None: rows = rows[:statement._limit_clause.value]
        return Rows(rows)
    def item(self, key, amount="1450.00", day=APR5, number=None):
        amount = D(amount)
        row = dict(id=key, company_id="co", supplier_id="supplier", currency="USD", status="open",
                   source_type="stock_goods_received", source_id="receipt-"+key,
                   source_number=number or "GR-"+key, posting_date=day, amount=amount, outstanding=amount)
        self.items.append(row)
        posting = dict(id="post-"+key, company_id="co", voucher_type="stock_goods_received",
                       voucher_id=row["source_id"], posting_date=day, reverses_posting_id=None)
        self.postings[row["source_id"]] = posting
        self.lines[posting["id"]] = [
            dict(id="inventory-"+key, company_id="co", posting_id=posting["id"], line_no=1,
                 account_id="inventory", party_id=None, currency="USD", debit=amount, credit=D(0)),
            dict(id="payable-"+key, company_id="co", posting_id=posting["id"], line_no=2,
                 account_id="ap", party_id="supplier", currency="USD", debit=D(0), credit=amount)]
        return row
    def history(self, item, amount="100.00", status="posted", key="payment"):
        allocation = dict(id="allocation-"+key, company_id="co", payable_id=item["id"], payment_id=key,
                          currency="USD", amount=D(amount))
        self.histories.setdefault(item["id"], []).append(allocation)
        self.payments[key] = dict(id=key, company_id="co", supplier_id="supplier", currency="USD", status=status,
                                 posting_date=APR8, amount=D(amount), unallocated=D(0))
        if status == "posted": item["outstanding"] -= D(amount)
        return allocation


@pytest.fixture
def world(monkeypatch):
    conn = Recording()
    ctx = SimpleNamespace(conn=conn, company_id="co", company_name="Synthetic books", currency="USD",
                          today=APR10, extra={})
    checks = []
    def posting(ctx, kind, key):
        assert kind == "stock_goods_received"
        if key not in conn.postings: raise Refusal("Missing original posting", "nothing_to_reverse")
        return conn.postings[key]
    monkeypatch.setattr(p.ledger, "posting_for_voucher", posting)
    monkeypatch.setattr(p.payables, "payable_account", lambda ctx: conn.ap)
    monkeypatch.setattr(p.ledger, "open_period", lambda ctx, day: {"id": "period", "fiscal_year_id": "year"})
    monkeypatch.setattr(p.ledger, "check_post", lambda ctx, day, kind, key, lines: checks.append((day, kind, key, lines)))
    monkeypatch.setattr(p.integration, "system_of_record", lambda ctx, what: ("native", None))
    monkeypatch.setattr(p.integration, "account_codes", lambda ctx, external, ids: {"bank": "EXT-BANK", "ap": "EXT-AP"})
    return SimpleNamespace(conn=conn, ctx=ctx, checks=checks)


def request(amount="600.00", day=APR8, **changes):
    values = dict(supplier_id="supplier", bank_account_id="bank", amount=amount, posting_date=day)
    values.update(changes)
    return p.PaySupplier(**values)


def plan(w, amount="600.00", day=APR8, **changes):
    return p.plan_payment(w.ctx, request(amount, day, **changes))


def refuses(code, action):
    with pytest.raises(Refusal) as found: action()
    assert found.value.code == code
    return found.value


def test_si5_partial_payment_uses_eligible_debt_and_retains_future_owed(world):
    world.conn.item("first")
    world.conn.item("later", "300.00", APR10)
    result = plan(world)
    assert (result.amount, result.total_owed, result.eligible_owed, result.owed_after) == tuple(map(D, ["600", "1750", "1450", "1150"]))
    assert [(a.item.id, a.amount, a.outstanding_after) for a in result.allocations] == [("first", D(600), D(850))]
    assert len(result.items) == 2
    assert [(l.account_id, l.debit, l.credit, l.party_id) for l in result.posting_lines()] == [
        ("ap", D(600), D(0), "supplier"), ("bank", D(0), D(600), None)]
    assert world.checks[0][:3] == (APR8, "supplier_payment", None)
    assert all(q.is_select for q in world.conn.queries)


def test_oldest_first_is_date_then_number_then_identity(world):
    world.conn.item("z", "100", APR5, "GR-2")
    world.conn.item("b", "100", APR5, "GR-1")
    world.conn.item("a", "100", APR5, "GR-1")
    result = plan(world, "250")
    assert [(a.item.id, a.amount, a.outstanding_after) for a in result.allocations] == [
        ("a", D(100), D(0)), ("b", D(100), D(0)), ("z", D(50), D(50))]


@pytest.mark.parametrize("amount,code", [("2000", "payment_above_owed"), ("1600", "payment_above_eligible")])
def test_overpayment_refusal_order_uses_all_debt_before_eligible_debt(world, amount, code):
    world.conn.item("first");world.conn.item("later", "300", APR10)
    refuses(code, lambda: plan(world, amount))
    assert not world.checks


def test_all_future_debt_precedes_above_total_refusal(world):
    world.conn.item("later", "300", APR10)
    refuses("owed_after_payment", lambda: plan(world, "2000"))


def test_no_open_items_is_nothing_owed(world):
    refuses("nothing_owed", lambda: plan(world))


@pytest.mark.parametrize("amount", [0, -1, True, 1.2, "1e2", "NaN", "Infinity", "0.001", "1000000000000000"])
def test_exact_positive_payment_boundary(world, amount):
    refuses("bad_amount", lambda: plan(world, amount))
    assert not world.conn.queries


@pytest.mark.parametrize("day,code", [(date(2026,4,11), "future_date"), (datetime(2026,4,8), "payment_date"), ("2026-04-08", "payment_date")])
def test_payment_date_is_exact_and_not_future(world, day, code):
    refuses(code, lambda: plan(world, day=day))


@pytest.mark.parametrize("field,value", [("supplier_id", " supplier"), ("bank_account_id", ""), ("supplier_id", 8)])
def test_resolved_id_boundary(world, field, value):
    refuses("payment_input", lambda: plan(world, **{field:value}))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"kind":"customer"},{"id":"other"}])
def test_supplier_must_be_current_company_supplier(world, mutation):
    world.conn.supplier.update(mutation)
    refuses("bad_party", lambda: plan(world))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"account_type":"income"},{"is_group":True},{"id":"other"}])
def test_resolved_bank_identity_is_not_name_or_cross_company_fallback(world, mutation):
    world.conn.item("one");world.conn.bank.update(mutation)
    refuses("payment_account", lambda: plan(world))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"supplier_id":"other"},{"currency":"JPY"},
    {"source_type":"opening"},{"source_type":"purchase_bill"},{"status":"paid"},{"outstanding":D("1451")},
    {"outstanding":D(0)},{"amount":D(0)}])
def test_unsupported_or_inconsistent_stored_items_refuse(world, mutation):
    world.conn.item("one").update(mutation)
    expected = "bad_amount" if mutation in [{"outstanding":D(0)},{"amount":D(0)}] else "payment_source"
    refuses(expected, lambda: plan(world))


def test_repeated_stored_item_cannot_allocate_twice(world):
    item = world.conn.item("one");world.conn.items.append(item)
    refuses("payment_source", lambda: plan(world))


def test_original_account_change_is_not_silently_remapped(world):
    world.conn.item("one")
    world.conn.lines["post-one"][1]["account_id"] = "old-payable"
    refuses("payment_source_account", lambda: plan(world))


def test_reversed_source_is_never_payable(world):
    world.conn.item("one");world.conn.reversed.add("post-one")
    refuses("payable_reversed", lambda: plan(world))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"voucher_type":"opening"},{"voucher_id":"other"},
    {"posting_date":APR10},{"reverses_posting_id":"something"}])
def test_original_posting_identity_matches_stored_source(world, mutation):
    world.conn.item("one");world.conn.postings["receipt-one"].update(mutation)
    refuses("payment_source", lambda: plan(world))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"currency":"JPY"},{"posting_id":"other"},
    {"party_id":"other"},{"credit":D("1449")},{"debit":D(1)}])
def test_original_supplier_line_is_exact(world, mutation):
    world.conn.item("one");world.conn.lines["post-one"][1].update(mutation)
    refuses("payment_source", lambda: plan(world))


def test_missing_posting_line_does_not_certify_prefix(world):
    world.conn.item("one");world.conn.lines["post-one"].pop(0)
    refuses("payment_source", lambda: plan(world))


def test_live_and_reversed_prior_allocations_preserve_exact_membership(world):
    item = world.conn.item("one", "2000")
    world.conn.history(item, "550", "posted", "live")
    world.conn.history(item, "100", "reversed", "undone")
    result = plan(world)
    assert result.total_owed == D(1450)
    assert result.allocations[0].outstanding_after == D(850)
    assert [(a.allocation_id, a.payment_id, a.amount, a.payment_status) for a in result.items[0].prior_allocations] == [
        ("allocation-live", "live", D(550), "posted"), ("allocation-undone", "undone", D(100), "reversed")]
    assert {("supplier_payment_allocation","allocation-live"),("supplier_payment","live"),
            ("supplier_payment_allocation","allocation-undone"),("supplier_payment","undone")} <= set(result.read_set)


def test_partial_outstanding_without_stored_allocation_refuses(world):
    world.conn.item("one")["outstanding"] = D(1400)
    refuses("payment_source", lambda: plan(world))


@pytest.mark.parametrize("mutation", [{"company_id":"other"},{"supplier_id":"other"},{"currency":"JPY"},
    {"status":"pending"},{"posting_date":date(2026,4,4)},{"amount":D(50)},{"unallocated":D(1)}])
def test_prior_payment_must_match_actual_stored_allocation(world, mutation):
    item=world.conn.item("one");world.conn.history(item)
    world.conn.payments["payment"].update(mutation)
    refuses("payment_source", lambda: plan(world))


def test_missing_prior_payment_is_not_zero_or_ignored(world):
    item=world.conn.item("one");world.conn.history(item);world.conn.payments.clear()
    refuses("payment_source", lambda: plan(world))


def test_read_set_covers_future_items_original_postings_lines_accounts_and_period(world):
    world.conn.item("one");world.conn.item("later", "300", APR10)
    rows=set(p.payment_read_set(world.ctx,request()))
    assert {("company","co"),("party","supplier"),("account","ap"),("account","bank"),
            ("fiscal_period","period"),("fiscal_year","year"),("supplier_payable","later"),
            ("posting","post-later"),("ledger_entry","inventory-one"),("ledger_entry","payable-one")} <= rows
    for q in world.conn.queries:
        params=q.compile().params
        assert any(k.startswith("company_id") and v=="co" for k,v in params.items())


def test_external_destination_and_codes_are_in_actual_core_card(world, monkeypatch):
    world.conn.item("one")
    monkeypatch.setattr(p.integration,"system_of_record",lambda ctx,what:("external","external-books"))
    card=approvals._preview(world.ctx,SimpleNamespace(preview=p.preview_payment),request())
    text=str(card)
    assert "external-books" in text and "EXT-AP" in text and "EXT-BANK" in text
    assert {("account_map","ap-map"),("account_map","bank-map")} <= set(plan(world).read_set)
    assert "A payment carries no tax of its own." in text


def test_native_preview_lists_exact_applied_and_remaining_money(world):
    world.conn.item("one")
    card=approvals._preview(world.ctx,SimpleNamespace(preview=p.preview_payment),request())
    assert "GR-one" in str(card) and "850.00" in str(card) and "600.00" in str(card)
    assert "ERPClaw general ledger" in str(card)


def test_existing_posting_refusal_propagates_before_any_effect(world,monkeypatch):
    world.conn.item("one")
    def reject(*args):raise Refusal("Account frozen","account_frozen")
    monkeypatch.setattr(p.ledger,"check_post",reject)
    refuses("account_frozen",lambda:plan(world))


def test_item_collection_uses_limit_plus_one_and_never_allocates_prefix(world,monkeypatch):
    monkeypatch.setattr(p,"MAX_ITEMS",2)
    for key in ["a","b","c"]:world.conn.item(key)
    refuses("payment_items_limit",lambda:plan(world))
    q=next(q for q in world.conn.queries if next(iter(q.selected_columns)).table.name=="supplier_payable")
    assert q._limit_clause.value==3
    assert not world.checks


def test_allocation_history_overflow_refuses_before_live_filter(world,monkeypatch):
    monkeypatch.setattr(p,"MAX_HISTORY_PER_ITEM",1)
    item=world.conn.item("one")
    world.conn.history(item,"10","reversed","a");world.conn.history(item,"10","reversed","b")
    refuses("payment_items_limit",lambda:plan(world))


def test_total_history_budget_is_shared_between_items(world,monkeypatch):
    monkeypatch.setattr(p,"MAX_HISTORY",1)
    for key in ["a","b"]:
        item=world.conn.item(key);world.conn.history(item,"10","reversed",key)
    refuses("payment_items_limit",lambda:plan(world))


def test_source_lines_cannot_be_silently_truncated(world,monkeypatch):
    monkeypatch.setattr(p,"MAX_SOURCE_LINES",1);world.conn.item("one")
    refuses("payment_items_limit",lambda:plan(world))


@pytest.mark.parametrize("currency,amount",[("JPY","600"),("KWD","600.001")])
def test_currency_minor_units_preserved(world,currency,amount):
    world.ctx.currency=currency;world.conn.item("one","1450")
    world.conn.items[0]["currency"]=currency
    for line in world.conn.lines["post-one"]:line["currency"]=currency
    result=plan(world,amount)
    assert result.amount==D(amount) and result.owed_after==D(1450)-D(amount)


def test_aggregate_amount_overflow_is_typed(world):
    world.conn.item("a","999999999999999");world.conn.item("b","999999999999999")
    refuses("bad_amount",lambda:plan(world,"1"))


def test_shared_payment_cannot_hide_excess_total_allocations(world):
    for key in ["a", "b"]:
        item=world.conn.item(key,"1000")
        world.conn.history(item,"250","posted","shared")["id"]="allocation-"+key
    world.conn.payments["shared"]["amount"]=D(400)
    refuses("payment_source",lambda:plan(world))


def test_complete_shared_payment_includes_paid_target_outside_open_collection(world):
    first=world.conn.item("open","1000")
    other=world.conn.item("paid","250")
    world.conn.history(first,"250","posted","shared")["id"]="allocation-open"
    world.conn.history(other,"250","posted","shared")["id"]="allocation-paid"
    world.conn.payments["shared"]["amount"]=D(500)
    other["status"]="paid"
    world.conn.items.remove(other);world.conn.settled_items.append(other)
    result=plan(world)
    assert result.total_owed == D(750) and result.owed_after == D(150)
    assert {("supplier_payable","paid"),("supplier_payment_allocation","allocation-paid")} <= set(result.read_set)
    assert len(result.items)==1


@pytest.mark.parametrize("mutation",[{"supplier_id":"other"},{"company_id":"other"},{"currency":"JPY"},
    {"posting_date":APR10},{"source_type":"opening"},{"status":"reversed"}])
def test_paid_allocation_target_still_requires_source_scope(world,mutation):
    first=world.conn.item("open","1000");other=world.conn.item("paid","250")
    world.conn.history(first,"250","posted","shared")["id"]="allocation-open"
    world.conn.history(other,"250","posted","shared")["id"]="allocation-paid"
    world.conn.payments["shared"]["amount"]=D(500)
    other.update(status="paid",**mutation) if "status" not in mutation else other.update(mutation)
    world.conn.items.remove(other);world.conn.settled_items.append(other)
    refuses("payment_source",lambda:plan(world))

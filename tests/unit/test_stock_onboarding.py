"""Stock setup consumer units. Fake rows/service boundary only; no database or graph."""
import copy
import datetime as dt
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from erpclaw_core import core_contracts as cc
from erpclaw_core.common import Ctx, Refusal
from erpclaw_assistant import resolve
from erpclaw_assistant import resolve_args

FIXTURES = Path(__file__).resolve().parents[1] / "contracts/stock/v2"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


class Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def __iter__(self): return iter(self.rows)
    def first(self): return next(iter(self.rows), None)


class CoreRows:
    def __init__(self):
        self.items = []
        self.accounts = [{"id": "@acct:inv", "number": "1300", "name": "Inventory"}]
        self.reads = []

    def execute(self, statement):
        assert statement.is_select, "A preview must never write"
        table = statement.get_final_froms()[0].name
        assert table in {"item", "account"}, "Core must not read stock-owned tables"
        self.reads.append((table, statement.compile().params))
        return Rows(self.accounts if table == "account" else self.items)


@pytest.fixture
def ctx():
    return Ctx(CoreRows(), "company-A", "Owner Books", "USD", dt.date(2026, 11, 4),
               extra={"commit": "approved-card"})


@pytest.fixture
def boundary(monkeypatch):
    state = {"warehouses": [], "calls": [], "refusals": {},
             "warehouse_preview": fixture("preview_create_warehouse_one_account")["output"]}

    def call(ctx, caller, name, args):
        assert caller == "core"
        name = name.removeprefix("stock.")
        state["calls"].append((name, copy.deepcopy(args)))
        if name in state["refusals"]:
            raise state["refusals"][name]
        if name == "list_warehouses": return {"warehouses": copy.deepcopy(state["warehouses"])}
        if name == "preview_create_warehouse": return copy.deepcopy(state["warehouse_preview"])
        if name == "preview_make_stock_item":
            return {"method": args["method"], "read_set": []}
        if name == "create_warehouse": return {"warehouse": "@wh:new", **args}
        if name == "make_stock_item": return {**args, "kind": "stock"}
        raise AssertionError("Unexpected service: " + name)

    monkeypatch.setattr(cc, "call_service", call)
    monkeypatch.setattr(cc, "enabled_plugins", lambda conn: {"core", "stock"})  # these books have the stock plugin on
    return state


def item(**overrides):
    return cc.AddItem(**({"name": "Copper Coil", "unit": "meter", "price": "12.00",
                         "kind": "stock", "valuation_method": "fifo"} | overrides))


def warehouse(name="Main Warehouse", id="@wh:main"):
    return {"warehouse": id, "name": name, "account": "@acct:inv", "company_default": True}


def caught(fn):
    with pytest.raises(Refusal) as got: fn()
    return got.value


def owner_choice(rf, session, scope, position):
    public = resolve.with_codes(rf, session, scope)
    code = public.data["choices"][position]["code"]
    assert all("ref" not in c for c in public.data["choices"])
    assert session.answer(code)
    assert not session.answer(code)  # one-use choice
    return public


def resolved(monkeypatch, ctx, session, scope="add_item#0:", **values):
    # Isolate this consumer from the deliberately retained catalogue guard.
    # The actual installed stock route is still unavailable; no graph is run.
    _replacement = (lambda *_: None)
    for _module in (resolve, resolve_args):
        monkeypatch.setattr(_module, 'planned_instead', _replacement)
    contract = next(c for c in cc.PLUGIN.contracts if c.name == "add_item")
    return resolve.resolve_args(ctx, contract,
        {"name": "Copper Coil", "unit": "meter", "price": "12.00", **values},
        "Add an item called Copper Coil, sold by meter at 12.00", session, scope=scope).args


def test_choice_fields_are_hidden_and_have_no_default_choice():
    props = cc.AddItem.model_json_schema()["properties"]
    for name in ("kind", "valuation_method", "warehouse", "inventory_account"):
        assert name not in props
        assert getattr(cc.AddItem(name="New"), name) is None
    assert set(cc.MANIFEST.uses) == {"stock." + name + "?" for name in
        ("list_warehouses", "preview_create_warehouse", "create_warehouse",
         "preview_make_stock_item", "make_stock_item", "plan_position_stock", "write_position_stock")}
    from erpclaw_stock import OpeningInput, OpeningWriteInput
    for model in (OpeningInput, OpeningWriteInput):
        for name, field in model.model_fields.items():
            assert name not in props
            assert field.is_required()


def test_missing_kind_is_asked_before_any_stock_service(ctx, boundary):
    rf = caught(lambda: cc.preview_add_item(ctx, item(kind=None)))
    assert rf.code == "item_kind_missing" and rf.data["field"] == "kind"
    assert [c["ref"] for c in rf.data["choices"]] == ["service", "stock"]
    assert boundary["calls"] == []


def test_model_invented_choices_are_discarded_and_actual_owner_choices_bind_only_their_write(
        monkeypatch, ctx, boundary):
    session = resolve.Session()
    invented = dict(kind="stock", valuation_method="fifo", warehouse="Main Warehouse", inventory_account="1300")
    args = resolved(monkeypatch, ctx, session, **invented)
    assert not set(invented) & args.keys()
    rf = caught(lambda: cc.preview_add_item(ctx, cc.AddItem(**args)))
    owner_choice(rf, session, "add_item#0:", 1)
    args = resolved(monkeypatch, ctx, session, **invented)
    assert args["kind"] == "stock" and "valuation_method" not in args
    rf = caught(lambda: cc.preview_add_item(ctx, cc.AddItem(**args)))
    assert rf.code == "valuation_method_missing"
    owner_choice(rf, session, "add_item#0:", 1)
    args = resolved(monkeypatch, ctx, session, **invented)
    assert args["valuation_method"] == "moving_average"
    assert "warehouse" not in args and "inventory_account" not in args
    other = resolved(monkeypatch, ctx, session, scope="add_item#1:", **invented)
    assert not set(invented) & other.keys()


def test_price_stays_grounded_in_owner_digits_with_new_stock_choices(monkeypatch, ctx):
    session = resolve.Session(bindings={"add_item#0:kind": "stock", "add_item#0:valuation_method": "fifo"})
    rf = caught(lambda: resolved(monkeypatch, ctx, session, price="99"))
    assert rf.code == "amount_missing"
    assert rf.data["field"] == "add_item#0:price"


@pytest.mark.parametrize("method", ["fifo", "moving_average"])
def test_no_warehouse_previews_main_default_and_exact_read_set_without_writes(ctx, boundary, method):
    inp = item(valuation_method=method)
    preview = cc.preview_add_item(ctx, inp)
    assert boundary["calls"] == [
        ("list_warehouses", {}),
        ("preview_create_warehouse", {"name": "Main Warehouse", "company_default": True}),
        ("preview_make_stock_item", {"method": method})]
    text = json.dumps(asdict(preview.card))
    for value in ("Copper Coil", "meter", "12.00", "USD", "1300 Inventory", "Create Main Warehouse",
                  "company's default", "Stock: none yet. Receive or count stock to add some.", "Nothing is posted."):
        assert value in text
    assert cc.read_set_add_item(ctx, inp) == [("company", "company-A"), ("account", "@acct:inv")]


def test_only_warehouse_is_selected_without_creating_another(ctx, boundary):
    boundary["warehouses"] = [warehouse()]
    preview = cc.preview_add_item(ctx, item())
    assert boundary["calls"] == [("list_warehouses", {}),
        ("preview_make_stock_item", {"method": "fifo", "default_warehouse": "@wh:main"})]
    assert "Main Warehouse" in json.dumps(asdict(preview.card))
    assert "Create Main Warehouse" not in json.dumps(asdict(preview.card))


def test_multiple_warehouses_ask_names_and_preserve_selected_choice(monkeypatch, ctx, boundary):
    boundary["warehouses"] = [warehouse(), warehouse("West Store", "@wh:west")]
    rf = caught(lambda: cc.preview_add_item(ctx, item()))
    assert rf.code == "warehouse_ambiguous"
    assert [c["ref"] for c in rf.data["choices"]] == ["Main Warehouse", "West Store"]
    session = resolve.Session(bindings={"add_item#0:kind": "stock", "add_item#0:valuation_method": "fifo"})
    owner_choice(rf, session, "add_item#0:", 1)
    inp = cc.AddItem(**resolved(monkeypatch, ctx, session, warehouse="invented"))
    assert inp.warehouse == "West Store"
    cc.preview_add_item(ctx, inp)
    assert boundary["calls"][-1] == ("preview_make_stock_item", {"method": "fifo", "default_warehouse": "@wh:west"})


@pytest.mark.parametrize("existing", [False, True])
def test_unknown_requested_warehouse_is_not_silently_replaced(ctx, boundary, existing):
    if existing: boundary["warehouses"] = [warehouse()]
    rf = caught(lambda: cc.preview_add_item(ctx, item(warehouse="Different Warehouse")))
    assert rf.code == "warehouse_unknown" and rf.data["field"] == "warehouse"
    assert boundary["calls"] == [("list_warehouses", {})]


def test_original_account_choice_fixture_binds_number_and_passes_company_scoped_id(monkeypatch, ctx, boundary):
    data = fixture("preview_create_warehouse_asks_account")["refusal"]
    boundary["refusals"]["preview_create_warehouse"] = Refusal("Which inventory account?", **data)
    rf = caught(lambda: cc.preview_add_item(ctx, item()))
    assert rf.code == "account_ambiguous" and rf.data["field"] == "inventory_account"
    assert rf.data["choices"] == data["choices"]
    session = resolve.Session(bindings={"add_item#0:kind": "stock", "add_item#0:valuation_method": "fifo"})
    owner_choice(rf, session, "add_item#0:", 1)
    inp = cc.AddItem(**resolved(monkeypatch, ctx, session, inventory_account="model-id"))
    assert inp.inventory_account == "1300"
    del boundary["refusals"]["preview_create_warehouse"]
    cc.preview_add_item(ctx, inp)
    assert boundary["calls"][-2] == ("preview_create_warehouse", {
        "name": "Main Warehouse", "company_default": True, "account": "@acct:inv"})
    table, params = next(read for read in ctx.conn.reads if read[0] == "account")
    assert "company-A" in params.values() and "1300" in params.values()


def test_unknown_account_number_never_reaches_stock_writer(ctx, boundary):
    ctx.conn.accounts = []
    rf = caught(lambda: cc.preview_add_item(ctx, item(inventory_account="unknown")))
    assert rf.code == "account_unknown" and rf.data["field"] == "inventory_account"
    assert rf.data["choices"] == [{"label": "1300 Inventory", "ref": "1300"}]
    assert boundary["calls"] == [("list_warehouses", {}),
        ("preview_create_warehouse", {"name": "Main Warehouse", "company_default": True})]


def test_stale_account_question_can_replace_old_binding_with_a_new_owner_code(monkeypatch, ctx, boundary):
    session = resolve.Session(bindings={"add_item#0:kind": "stock", "add_item#0:valuation_method": "fifo",
                                        "add_item#0:inventory_account": "deleted"})
    ctx.conn.accounts = []
    inp = cc.AddItem(**resolved(monkeypatch, ctx, session))
    rf = caught(lambda: cc.preview_add_item(ctx, inp))
    owner_choice(rf, session, "add_item#0:", 0)
    args = resolved(monkeypatch, ctx, session, inventory_account="deleted")
    assert args["inventory_account"] == "1300"
    ctx.conn.accounts = [{"id": "@acct:replacement", "number": "1300", "name": "Inventory"}]
    boundary["warehouse_preview"]["account"]["account"] = "@acct:replacement"
    cc.preview_add_item(ctx, cc.AddItem(**args))
    assert boundary["calls"][-2][1]["account"] == "@acct:replacement"


@pytest.mark.parametrize("code", ["no_stock_account", "frozen_account", "not_inventory_account"])
def test_current_account_refusal_is_preserved_without_selecting_a_fallback(ctx, boundary, code):
    ctx.conn.accounts = []
    rf = Refusal("No usable inventory account.", code)
    boundary["refusals"]["preview_create_warehouse"] = rf
    assert caught(lambda: cc.preview_add_item(ctx, item(inventory_account="deleted"))) is rf


@pytest.mark.parametrize("change", ["account", "method", "warehouse", "create_intent"])
def test_card_token_binds_stock_dependencies(ctx, boundary, change):
    inp = item()
    before = cc.preview_add_item(ctx, inp).state_token
    if change == "account": boundary["warehouse_preview"]["account"]["account"] = "another-account"
    elif change == "method": inp = item(valuation_method="moving_average")
    elif change == "warehouse": boundary["warehouses"] = [warehouse("West Store", "@wh:west")]
    else: boundary["warehouses"] = [warehouse()]
    assert cc.preview_add_item(ctx, inp).state_token != before


@pytest.mark.parametrize("new_warehouse", [False, True])
def test_commit_calls_existing_services_in_order_and_never_passes_price_as_cost(monkeypatch, ctx, boundary, new_warehouse):
    if not new_warehouse: boundary["warehouses"] = [warehouse()]
    sequence = []
    original = cc.call_service
    def call(context, caller, name, args):
        sequence.append((name, copy.deepcopy(args)))
        return original(context, caller, name, args)
    monkeypatch.setattr(cc, "call_service", call)
    def create(context, name, price, unit):
        assert context is ctx and price == Decimal("12.00")
        sequence.append(("create_item", (name, price, unit)))
        return "new-item"
    monkeypatch.setattr(cc, "create_item", create)
    audits = []
    monkeypatch.setattr(cc, "audit", lambda *args, **kw: audits.append((args, kw)))
    result = cc.commit_add_item(ctx, item(), "operation")
    start = next(i for i, value in enumerate(sequence) if value[0] == "create_item")
    assert [x[0] for x in sequence[start:]] == (["create_item", "stock.create_warehouse", "stock.make_stock_item"]
                                               if new_warehouse else ["create_item", "stock.make_stock_item"])
    if new_warehouse:
        assert sequence[start+1][1] == {"name": "Main Warehouse", "account": "@acct:inv", "company_default": True}
    assert sequence[-1] == ("stock.make_stock_item", {"item": "new-item", "method": "fifo",
        "default_warehouse": "@wh:new" if new_warehouse else "@wh:main"})
    assert all(not ({"price", "cost", "qty", "value"} & args.keys()) for name,args in sequence if name.startswith("stock."))
    assert audits[0][1]["detail"]["kind"] == "stock"
    assert "Stock: none yet" in result.text


def test_service_item_never_calls_stock_even_when_stock_unavailable(monkeypatch, ctx, boundary):
    boundary["refusals"]["list_warehouses"] = Refusal("disabled", "plugin_not_enabled")
    inp = item(kind="service", valuation_method=None)
    preview = cc.preview_add_item(ctx, inp)
    assert "a service, not kept in stock" in json.dumps(asdict(preview.card))
    assert cc.read_set_add_item(ctx, inp) == [("company", "company-A")]
    monkeypatch.setattr(cc, "create_item", lambda *_: "service-item")
    monkeypatch.setattr(cc, "audit", lambda *_, **__: None)
    result = cc.commit_add_item(ctx, inp, "operation")
    assert result.text == "I added Copper Coil at 12.00 USD per meter."
    assert boundary["calls"] == []


def test_disabled_stock_has_the_original_owner_sentence_and_no_write(ctx, boundary):
    boundary["refusals"]["list_warehouses"] = Refusal("provider unavailable", "plugin_not_enabled")
    rf = caught(lambda: cc.preview_add_item(ctx, item()))
    assert (rf.code, rf.message) == ("plugin_not_enabled", "Keeping stock isn't switched on in these books.")
    assert boundary["calls"] == [("list_warehouses", {})]


@pytest.mark.parametrize("name", ["create_warehouse_bad_name_refused", "create_warehouse_same_name_refused",
    "create_warehouse_frozen_account_refused", "create_warehouse_not_inventory_refused",
    "create_warehouse_second_default_refused"])
def test_existing_setup_refusal_fixtures_are_not_renamed_or_ignored(ctx, boundary, name):
    data = fixture(name)["refusal"]
    code = data.pop("code")
    rf = Refusal("Producer refusal", code, **data)
    boundary["refusals"]["preview_create_warehouse"] = rf
    assert caught(lambda: cc.preview_add_item(ctx, item())) is rf
    assert all(not call[0].startswith("make_") for call in boundary["calls"])


@pytest.mark.parametrize("near_answer, code", [(None, "item_near_ambiguous"), (False, "item_exists")])
def test_near_name_protection_still_precedes_stock_setup(ctx, boundary, near_answer, code):
    ctx.conn.items = [{"id": "existing", "name": "Copper Wire"}]
    rf = caught(lambda: cc.preview_add_item(ctx, item(new_item=near_answer)))
    assert rf.code == code and boundary["calls"] == []


def test_exact_duplicate_still_refuses_before_stock_setup(ctx, boundary):
    ctx.conn.items = [{"id": "existing", "name": "Copper Coil"}]
    assert caught(lambda: cc.preview_add_item(ctx, item())).code == "item_exists"
    assert boundary["calls"] == []


def test_kind_change_does_not_silently_ignore_stock_only_choices(ctx, boundary):
    assert caught(lambda: cc.preview_add_item(ctx, item(kind="service"))).code == "stock_fields_for_service"
    assert boundary["calls"] == []


def test_stock_catalogue_guard_remains_planned_during_development():
    entries = json.loads((Path(cc.__file__).parent / "catalogue/planned.json").read_text())["entries"]
    entry = next(x for x in entries if x["id"] == "planned.stock_item")
    assert entry["instead_of"] == ["add_item"] and entry["installed_as"] == []

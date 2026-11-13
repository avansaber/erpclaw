"""Future registered-payment binding with recording transactions and statements.

These pure units exercise real Registry.fill, committing scope, Card/read-set
hashing and writer orchestration. They do not approve a live database card or
qualify SQL, atomic rollback, locks, migrations, public activation or replay.
"""
import copy
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erpclaw_core import approval_scope as scope, approvals, core_contracts, plugins
from erpclaw_core import supplier_payments as p
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Input, WriteContract, check_amount_names

from registry_fixtures import core_registry

D = Decimal
DAY = date(2026, 4, 8)


class FutureInput(Input):
    supplier_id: str
    bank_account_id: str
    amount: str
    date: date

    @property
    def posting_date(self):
        return self.date


class Transaction:
    def __init__(self, connection):
        self.connection, self.is_active, self._previous_nested = connection, True, None


class Rows:
    def __init__(self, rows=(), rowcount=1): self.rows, self.rowcount = list(rows), rowcount
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)


class Recording:
    def __init__(self):
        self.closed = self.invalidated = False
        self.root, self.nested = Transaction(self), Transaction(self)
        self.card, self.dependencies, self.events = None, {}, []
        self.outcome, self.existing_payment, self.cas_count = False, False, 1
        self.read_hook = None
    def get_transaction(self): return self.root
    def get_nested_transaction(self): return self.nested
    def begin(self): raise AssertionError("The caller owns the transaction")
    def begin_nested(self): raise AssertionError("The caller owns the savepoint")
    def commit(self): raise AssertionError("The writer must not commit")
    def rollback(self): raise AssertionError("The writer must not roll back")
    def execute(self, statement):
        params = statement.compile().params
        if statement.is_select:
            table = next(iter(statement.selected_columns)).table.name
            if self.read_hook: self.read_hook(table)
            if table == "approval": return Rows([self.card] if self.card is not None else [])
            if table == "operation_outcome": return Rows([("outcome",)] if self.outcome else [])
            if table == "supplier_payment": return Rows([("previous",)] if self.existing_payment else [])
            key = params.get("id_1")
            return Rows([self.dependencies[(table, key)]] if (table, key) in self.dependencies else [])
        table = statement.table.name
        kind = "insert" if statement.is_insert else "update"
        assert table in ("supplier_payment", "supplier_payment_allocation", "supplier_payable")
        assert statement.is_insert or statement.is_update
        self.events.append((kind, table, params, statement))
        if table == "supplier_payment": self.existing_payment = True
        return Rows(rowcount=self.cas_count)


@pytest.fixture
def world(monkeypatch):
    conn = Recording()
    ctx = Ctx(conn, "co", "Synthetic books", "USD", DAY, extra={})
    request = FutureInput(supplier_id="supplier", bank_account_id="bank", amount="600.00", date=DAY)
    supplier = dict(id="supplier", name="Copperline Metals", company_id="co", kind="supplier")
    bank = dict(id="bank", number="1100", name="Bank", company_id="co")
    payable = dict(id="ap", number="2100", name="Accounts Payable", company_id="co")
    one = p.Item("one", "receipt-one", "GR-1", date(2026,4,5), D(400), D(400), "post-one", "ap", ())
    two = p.Item("two", "receipt-two", "GR-2", date(2026,4,6), D(500), D(500), "post-two", "ap", ())
    conn.dependencies = {
        ("company", "co"): dict(id="co", name="Synthetic books", currency="USD"),
        ("party", "supplier"): supplier,
        ("account", "bank"): bank,
        ("account", "ap"): payable,
        **{("supplier_payable", item.id): dict(id=item.id, company_id="co", supplier_id="supplier", currency="USD",
             source_type=p.payables.DELIVERY, source_id=item.source_id, source_number=item.source_number,
             posting_date=item.posting_date, amount=item.original_amount, outstanding=item.outstanding, status="open")
           for item in (one, two)}}
    plan = p.PaymentPlan(request, supplier, bank, payable, D(600), D(900), D(900), D(300),
                         (one,two), (p.Allocation(one,D(400),D(0)),p.Allocation(two,D(200),D(300))),
                         tuple(sorted(conn.dependencies)), "native", None, {})
    w = SimpleNamespace(conn=conn, ctx=ctx, request=request, plan=plan)
    monkeypatch.setattr(p, "plan_payment", lambda context, inp: w.plan)
    contract = WriteContract("pay_supplier", "core", "write", "Future resolved supplier payment", FutureInput,
        preview=p.preview_payment, commit=p.commit_payment, read_set=p.payment_read_set,
        find_by_operation_key=lambda ctx,key: None, amounts=("amount", "outstanding"), moves_money=True)
    manifest = core_contracts.MANIFEST.model_copy(update={"contracts":{"pay_supplier":"write"}, "metrics":[], "uses":[]})
    # Model this source candidate's distribution version without installing it.
    # Loader version parsing/constraints and Registry.fill remain real; the reused
    # interpreter's installed metadata is not evidence of a candidate install.
    candidate_version = core_contracts.MANIFEST.version
    installed_version = plugins.version
    def distribution_version(name):
        return candidate_version if name == "erpclaw-core" else installed_version(name)
    monkeypatch.setattr(plugins, "version", distribution_version)
    monkeypatch.setattr(approvals, "version", distribution_version)
    # Revision inventory is static here; registry validation and callback wrapping are real.
    monkeypatch.setattr(plugins, "_core_revisions", lambda: {"0001"})
    registry = core_registry(monkeypatch, manifest, contract)
    monkeypatch.setattr(plugins, "REGISTRY", registry)
    monkeypatch.setattr(approvals, "REGISTRY", registry)
    w.registry, w.contract = registry, registry.contracts["pay_supplier"]
    w.plan_calls = []
    def plan_call(context, inp):
        w.plan_calls.append((context, inp))
        return w.plan
    monkeypatch.setattr(p, "plan_payment", plan_call)
    def number(context, prefix, day):
        conn.events.append(("number", prefix, day))
        return "SPAY-2026-0001"
    monkeypatch.setattr(p, "next_number", number)
    def post(context, day, kind, key, lines, **kwargs):
        conn.events.append(("posting", day, kind, key, lines, kwargs))
        return p.ledger.PostingResult("payment-posting", 7, "hash")
    monkeypatch.setattr(p.ledger, "post", post)
    monkeypatch.setattr(p, "audit", lambda *args, **kwargs: conn.events.append(("audit", args[1:], kwargs)))
    seal(w)
    w.plan_calls.clear()
    return w


def seal(w, date_source=None):
    card = dict(id="card", principal_id="owner", company_id="co", conversation_id="conversation",
        contract="pay_supplier", contract_version=w.registry.plugins["core"].version,
        policy_version="policy", impl_version=approvals.impl_version(w.contract), nonce="nonce", operation_key="operation",
        expires_at=datetime(2026,4,8,12,tzinfo=timezone.utc), status="consumed")
    args = w.request.model_dump(mode="json")
    preview = approvals._preview(w.ctx, w.contract, w.request, date_source)
    rows = approvals.read_set(w.conn, w.contract, w.ctx, w.request)
    card.update(args_json=json.dumps(args), card_json=json.dumps(preview), read_set_json=json.dumps(rows),
                digest=approvals.digest(approvals._fields(card,args,rows,preview)))
    w.conn.card = card


def run(w, *, owner="core", contract="pay_supplier", scope_key="operation", key="operation", ctx=None):
    # The private issuer is a test-only recording boundary, not a production approval shortcut.
    with scope._issue(w.ctx, "card", contract, scope_key, owner, w.conn.nested):
        return w.contract.commit(ctx or w.ctx, w.request, key)


def refused(w, code, action):
    with pytest.raises(Refusal) as error: action()
    assert error.value.code == code
    assert not w.conn.events


def test_registered_future_writer_orders_header_allocations_cas_posting_and_audit(world):
    w = world
    result = run(w)
    events = w.conn.events
    assert [e[0] for e in events] == ["number","insert","insert","update","insert","update","posting","audit"]
    assert events[0] == ("number", "SPAY", DAY)
    payment = events[1][2]
    assert {k:payment[k] for k in ("company_id","supplier_id","posting_date","currency","amount","unallocated",
             "account_id","status","operation_key")} == dict(company_id="co", supplier_id="supplier", posting_date=DAY,
             currency="USD", amount=D(600), unallocated=D(0), account_id="bank", status="posted", operation_key="operation")
    allocation_one, allocation_two = events[2][2], events[4][2]
    assert [(a["payable_id"],a["amount"],a["payment_id"],a["company_id"],a["currency"])
            for a in (allocation_one,allocation_two)] == [("one",D(400),payment["id"],"co","USD"),("two",D(200),payment["id"],"co","USD")]
    assert events[3][2]["status"] == "paid" and events[3][2]["outstanding"] == 0
    assert events[5][2]["status"] == "open" and events[5][2]["outstanding"] == D(300)
    for event,item in zip((events[3],events[5]), w.plan.items):
        params = event[2]
        assert {k:params[k] for k in ("company_id_1","id_1","supplier_id_1","currency_1","source_type_1",
                   "source_id_1","status_1","amount_1","outstanding_1")} == dict(company_id_1="co",id_1=item.id,
                   supplier_id_1="supplier",currency_1="USD",source_type_1=p.payables.DELIVERY,source_id_1=item.source_id,
                   status_1="open",amount_1=item.original_amount,outstanding_1=item.outstanding)
    posting = events[6]
    assert posting[1:4] == (DAY, "supplier_payment", payment["id"])
    assert [(l.account_id,l.debit,l.credit,l.party_id) for l in posting[4]] == [("ap",D(600),D(0),"supplier"),("bank",D(0),D(600),None)]
    assert posting[5] == {"operation_key":"operation"}
    assert result.data["posting_id"] == "payment-posting"
    assert result.data["id"] == payment["id"] and result.data["operation_key"] == "operation"
    assert len(w.plan_calls) == 1
    check_amount_names(w.contract, result.amounts)
    assert {a.what:a.value for a in result.amounts} == {"amount":"600.00","outstanding":"300.00"}
    assert approvals.committing(w.ctx) is None


def test_production_manifest_has_reversal_and_no_forward_payment_contract():
    assert "pay_supplier" not in core_contracts.MANIFEST.contracts
    assert core_contracts.MANIFEST.contracts["reverse_supplier_payment"] == "write"
    assert p.PAYMENT in core_contracts.MANIFEST.voucher_types


def test_absent_future_registration_refuses_even_with_recorded_scope(world):
    world.registry.contracts.clear()
    refused(world,"not_approved",lambda:run(world))


@pytest.mark.parametrize("legacy", [None,"card",True])
def test_legacy_flag_is_never_authority(world,legacy):
    world.ctx.extra["commit"] = legacy
    refused(world,"not_approved",lambda:p.commit_payment(world.ctx,world.request,"operation"))


@pytest.mark.parametrize("changes", [{"owner":"stock"},{"contract":"other"},{"scope_key":"other"},{"key":"other"}])
def test_exact_root_scope_identity_is_required(world,changes):
    refused(world,"not_approved",lambda:run(world,**changes))


def test_original_context_is_required(world):
    refused(world,"not_approved",lambda:run(world,ctx=copy.copy(world.ctx)))


def test_read_frame_cannot_call_writer(world):
    def preview(ctx, inp): return p.commit_payment(ctx,inp,"operation")
    wrapped = scope._wrap_contract(preview,"core","pay_supplier","preview")
    with scope._issue(world.ctx,"card","pay_supplier","operation","core",world.conn.nested):
        refused(world,"not_approved",lambda:wrapped(world.ctx,world.request))


@pytest.mark.parametrize("mutation", ["root","effect","closed","invalidated"])
def test_lost_transaction_refuses_before_effects(world,mutation):
    if mutation == "root": world.conn.root.is_active=False
    elif mutation == "effect": world.conn.nested.is_active=False
    else: setattr(world.conn,mutation,True)
    refused(world,"not_approved",lambda:run(world))


@pytest.mark.parametrize("change", [{"id":"wrong"},{"company_id":"wrong"},{"contract":"other"},
                                    {"status":"pending"},{"operation_key":"wrong"}])
def test_stored_approval_must_match_live_scope(world,change):
    world.conn.card.update(change)
    refused(world,"not_approved",lambda:run(world))


def test_missing_approval_refuses(world):
    world.conn.card=None
    refused(world,"not_approved",lambda:run(world))


@pytest.mark.parametrize("change", ["owner","effect","planned","declaration","voucher"])
def test_exact_registered_core_owner_and_write_contract(world,change):
    if change == "owner": world.registry.contract_plugin["pay_supplier"]="stock"
    elif change == "effect": world.contract.effect="read"
    elif change == "planned": world.contract.planned="future"
    elif change == "declaration": world.registry.plugins["core"].manifest.contracts["pay_supplier"]="read"
    else: world.registry.plugins["core"].manifest = world.registry.plugins["core"].manifest.model_copy(update={"voucher_types":[]})
    refused(world,"not_approved",lambda:run(world))


@pytest.mark.parametrize("existing,code", [("outcome","not_approved"),("existing_payment","duplicate_payment")])
def test_replay_is_left_to_core_not_reexecuted(world,existing,code):
    setattr(world.conn,existing,True)
    refused(world,code,lambda:run(world))


@pytest.mark.parametrize("change", ["amount","date","bank","supplier","destination","external_codes","allocation"])
def test_actual_approved_card_identity_cannot_drift(world,change):
    w=world
    if change=="amount": w.plan=replace(w.plan,amount=D(601))
    elif change=="date": w.plan=replace(w.plan,request=w.request.model_copy(update={"date":date(2026,4,9)}))
    elif change=="bank": w.plan=replace(w.plan,bank={**w.plan.bank,"name":"Different bank"})
    elif change=="supplier": w.plan=replace(w.plan,supplier={**w.plan.supplier,"name":"Different supplier"})
    elif change=="allocation":
        w.plan=replace(w.plan,allocations=(replace(w.plan.allocations[0],amount=D(399)),w.plan.allocations[1]))
    else:
        if change=="external_codes":
            w.plan=replace(w.plan,mode="external",external="LedgerOne",codes={"ap":"A","bank":"B"});seal(w)
        w.plan=replace(w.plan,mode="external",external="LedgerOne",codes={"ap":"CHANGED","bank":"B"})
    refused(w,"stale",lambda:run(w))


@pytest.mark.parametrize("change", ["row","missing_row","membership","version","artifact","digest","args","card_json","read_set_json"])
def test_real_core_row_and_digest_binding(world,change):
    w=world
    if change=="row": w.conn.dependencies[("account","bank")]["name"]="Changed"
    elif change=="missing_row": del w.conn.dependencies[("account","bank")]
    elif change=="membership": w.plan=replace(w.plan,read_set=w.plan.read_set[:-1])
    elif change=="version":
        owner = w.registry.plugins["core"]
        owner.manifest = owner.manifest.model_copy(update={"version":owner.version + ".post1"})
    elif change=="artifact": w.registry.plugins["core"].artifact_digest="changed-artifact"
    elif change=="digest": w.conn.card["digest"]="0"*64
    elif change=="args": w.conn.card["args_json"]='{"amount":"1"}'
    else: w.conn.card[change]="malformed"
    refused(w,"stale",lambda:run(w))


def test_real_date_source_card_envelope_is_preserved(world):
    seal(world,{"phrase":"8 April 2026","date":"2026-04-08"})
    assert run(world).data["posting_id"]=="payment-posting"


def test_scope_lost_during_plan_is_rechecked_before_first_effect(world,monkeypatch):
    def plan(ctx,inp):
        world.conn.nested.is_active=False
        return world.plan
    monkeypatch.setattr(p,"plan_payment",plan)
    refused(world,"not_approved",lambda:run(world))


def test_approval_changed_during_reads_is_rechecked_before_first_effect(world):
    def hook(table):
        if table=="account": world.conn.card={**world.conn.card,"nonce":"changed"}
    world.conn.read_hook=hook
    refused(world,"stale",lambda:run(world))


@pytest.mark.parametrize("count", [0,2])
def test_compare_and_swap_conflict_propagates_for_caller_rollback(world,count):
    world.conn.cas_count=count
    with pytest.raises(Refusal) as error: run(world)
    assert error.value.code=="conflict"
    assert [e[0] for e in world.conn.events]==["number","insert","insert","update"]
    # Recording objects cannot establish rollback; no posting/audit/outcome follows failure.


def test_posting_failure_propagates_without_audit_or_own_commit(world,monkeypatch):
    def fail(*args,**kwargs): raise Refusal("Posting conflict","conflict")
    monkeypatch.setattr(p.ledger,"post",fail)
    with pytest.raises(Refusal) as error: run(world)
    assert error.value.code=="conflict"
    assert not any(e[0] in ("audit","posting") for e in world.conn.events)


def test_second_internal_call_refuses_stored_operation(world):
    run(world)
    world.conn.events.clear()
    refused(world,"duplicate_payment",lambda:run(world))

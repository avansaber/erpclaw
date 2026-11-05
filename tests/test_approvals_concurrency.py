"""Cards under concurrency, crashes and the runtime role.

The PostgreSQL cases are ordered by locks: a commit is held at a hook right after it has taken every lock it
takes, and the competing session is seen waiting in pg_locks before the commit is let go. No deadlock (SQLSTATE
40P01) may appear anywhere.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from erpclaw_core import administration as admin
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.db import Runtime, make_engine
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as t

ARGS = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}


def invoices(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()


def person(engine, name, can):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, can, [company_ctx(conn).company_id])
        return pr.authenticate(conn, token)


@pytest.fixture
def hold(monkeypatch):
    """Held(...), always let go at teardown, so a failed assertion never leaves a commit holding its locks."""
    made = []
    yield lambda: made.append(Held(monkeypatch)) or made[-1]
    for h in made:
        h.go.set()


class Held:
    """Hold a card commit right after its locks (the principals, the card, the posting and document locks)."""

    def __init__(self, monkeypatch):
        self.locked, self.go = threading.Event(), threading.Event()
        real = approvals._lock_read_set

        def hold(conn, ctx, rs):
            real(conn, ctx, rs)
            self.locked.set()
            assert self.go.wait(30), "the test never let the commit go"

        monkeypatch.setattr(approvals, "_lock_read_set", hold)


def run(fn, box, key):
    def body():
        try:
            box[key] = fn()
        except Exception as e:  # noqa: BLE001  (asserted by the test)
            box[key] = e
    th = threading.Thread(target=body, daemon=True)  # a failing test must not hang the run
    th.start()
    return th


def a_session_waits(engine, timeout=10.0) -> bool:
    """True once some other session of this database waits for a lock it has not been granted."""
    deadline = time.time() + timeout
    with engine.connect() as watcher:
        while time.time() < deadline:
            n = watcher.execute(text(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database() AND l.pid <> pg_backend_pid()")).scalar_one()
            watcher.rollback()
            if n:
                return True
            time.sleep(0.02)
    return False


def no_deadlock(*results):
    for r in results:
        assert not (isinstance(r, DBAPIError) and getattr(r.orig, "sqlstate", None) == "40P01"), r


# --- the read set is locked before the recomputation ------------------------------------------------------------

def test_a_locked_read_set_row_cannot_change_under_a_commit(engine, hold):
    """A payment card locks the invoice it allocates to: a change by another connection waits for the commit
    and then applies to the committed state, so it cannot slip in between the recomputation and the effect."""
    card = approvals.propose(engine, REGISTRY.contracts["record_payment"],
                             {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"})
    held, box = hold(), {}
    with engine.connect() as conn:
        rs = json.loads(conn.execute(select(m.approval.c.read_set_json).where(
            m.approval.c.id == card.approval_id)).scalar_one())
    inv = next(k for tb, k, _ in rs if tb == "sales_invoice")
    th = run(lambda: approvals.approve(engine, card.approval_id, card.digest), box, "approve")
    assert held.locked.wait(30)

    def change():
        with engine.begin() as conn:
            conn.execute(text("UPDATE sales_invoice SET outstanding = outstanding WHERE id = :i"), {"i": inv})
        return "changed"

    other = run(change, box, "change")
    assert a_session_waits(engine)  # the change waits on the commit's row lock
    held.go.set()
    th.join(30), other.join(30)
    assert box["approve"].status == "committed" and box["change"] == "changed"
    no_deadlock(*box.values())


# --- the approver bound, and revocation racing approval --------------------------------------------------------

@pytest.mark.parametrize("who", ["approver", "drafter"])
def test_a_revocation_during_a_commit_waits_for_it(engine, hold, who):
    """The commit holds both principal rows FOR SHARE; a grant change needs FOR UPDATE, so it waits and the card
    commits before the revocation."""
    drafter, approver = person(engine, "dana", ["view", "draft"]), person(engine, "ann", ["view", "approve", "post"])
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS, principal=drafter)
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    held, box = hold(), {}
    th = run(lambda: approvals.approve(engine, card.approval_id, card.digest, principal=approver), box, "approve")
    assert held.locked.wait(30)
    name, perm = ("ann", "approve") if who == "approver" else ("dana", "draft")

    def revoke():
        with engine.begin() as conn:
            return admin.revoke(conn, name, [perm], cid)

    rv = run(revoke, box, "revoke")
    assert a_session_waits(engine)
    held.go.set()
    th.join(30), rv.join(30)
    assert box["approve"].status == "committed" and isinstance(box["revoke"], int)
    no_deadlock(*box.values())


@pytest.mark.parametrize("who", ["approver", "drafter"])
def test_a_revocation_that_commits_first_refuses_the_approval(engine, who):
    """The grant change holds the principal's row; the approval's FOR SHARE waits, then sees the new version."""
    drafter, approver = person(engine, "dana", ["view", "draft"]), person(engine, "ann", ["view", "approve", "post"])
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS, principal=drafter)
    n = invoices(engine)
    name, perm = ("ann", "approve") if who == "approver" else ("dana", "draft")
    revoker = engine.connect()
    try:
        revoker.begin()
        admin.revoke(revoker, name, [perm], company_ctx(revoker).company_id)
        box = {}
        th = run(lambda: approvals.approve(engine, card.approval_id, card.digest, principal=approver), box, "approve")
        assert a_session_waits(engine)  # the approval waits on the principal row
        revoker.commit()
    finally:
        revoker.close()  # an uncommitted revocation would hold its row lock past a failed assertion
    th.join(30)
    r = box["approve"]
    no_deadlock(r)
    if who == "approver":
        assert isinstance(r, Refusal) and "permissions changed" in r.message
    else:
        assert r.status == "invalidated"
        with engine.connect() as conn:
            assert conn.execute(select(m.approval.c.status).where(m.approval.c.id == card.approval_id)
                                ).scalar_one() == "invalidated"
    assert invoices(engine) == n


def test_a_drafter_revoked_while_approving_their_own_card_does_not_deadlock(engine, hold):
    owner_card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    held, box = hold(), {}
    th = run(lambda: approvals.approve(engine, owner_card.approval_id, owner_card.digest), box, "approve")
    assert held.locked.wait(30)

    def revoke():
        with engine.begin() as conn:
            return admin.revoke(conn, "owner", ["export"], cid)

    rv = run(revoke, box, "revoke")
    assert a_session_waits(engine)
    held.go.set()
    th.join(30), rv.join(30)
    no_deadlock(*box.values())
    assert box["approve"].status == "committed"


# --- one lock order for every writer -------------------------------------------------------------------------

@pytest.mark.parametrize("other", ["lock_through", "plain posting", "grant change", "second card"])
def test_a_card_commit_and_another_writer_never_deadlock(engine, hold, other):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    second = approvals.propose(engine, REGISTRY.contracts["record_payment"],
                               {"customer": "Pied Piper", "amount": "50", "oldest_first": True, "method": "check"})
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    held, box = hold(), {}
    th = run(lambda: approvals.approve(engine, card.approval_id, card.digest), box, "approve")
    assert held.locked.wait(30)

    def write():
        with engine.begin() as conn:
            ctx = company_ctx(conn)
            if other == "lock_through":
                return ledger.lock_through(ctx, date(2025, 1, 31))
            if other == "plain posting":
                a = lambda n: ledger.account_by_number(ctx, n)["id"]  # noqa: E731
                return ledger.post(ctx, date(2026, 10, 26), "journal", "j-1", [
                    ledger.Line(a("1100"), debit=Decimal("1.00")), ledger.Line(a("3100"), credit=Decimal("1.00"))])
            if other == "grant change":
                return admin.grant(conn, "owner", ["export"], cid)
        return None

    if other == "second card":
        ow = run(lambda: approvals.approve(engine, second.approval_id, second.digest), box, "other")
    else:
        ow = run(write, box, "other")
    assert a_session_waits(engine)  # it waits behind the commit's locks, in the same order
    held.go.set()
    th.join(60), ow.join(60)
    no_deadlock(*box.values())
    assert box["approve"].status == "committed"
    assert not isinstance(box["other"], Exception) or isinstance(box["other"], Refusal), box["other"]


# --- crashes: a real process kill (spike S2, ported) -----------------------------------------------------------

CHILD = """
import os, sys
from erpclaw_core import approvals
from erpclaw_core.db import make_engine
from erpclaw_core.plugins import REGISTRY, load_plugins
load_plugins()
engine = make_engine(sys.argv[1])
if sys.argv[4] == "before":
    contract = REGISTRY.contracts["create_invoice"]
    real = contract.commit
    def die(ctx, inp, key):
        real(ctx, inp, key)  # the effect is written, not yet committed
        os._exit(137)
    contract.commit = die
approvals.approve(engine, sys.argv[2], sys.argv[3])
os._exit(137)  # killed right after the commit, before anything records it
"""


def _url(engine):
    return engine.url.render_as_string(hide_password=False)


@pytest.mark.parametrize("when", ["after", "before"])
def test_a_process_killed_around_the_commit_leaves_one_effect_or_none(engine, when):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    n = invoices(engine)
    engine.dispose()  # the child gets the books to itself
    p = subprocess.run([sys.executable, "-c", CHILD, _url(engine), card.approval_id, card.digest, when],
                       env=os.environ.copy(), capture_output=True, text=True, timeout=120)
    assert p.returncode == 137, p.stderr
    if when == "after":
        assert invoices(engine) == n + 1
        replay = approvals.approve(engine, card.approval_id, card.digest)  # the resumed graph re-runs the node
        assert replay.status == "committed" and invoices(engine) == n + 1
    else:
        assert invoices(engine) == n and approvals.read_back(engine, card.operation_key).status == "unknown"
        assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
        assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
        assert invoices(engine) == n + 1


# --- the runtime role, and the read set's coupling to the policy ----------------------------------------------

def test_every_card_flow_commits_under_a_genuine_runtime_login(engine, runtime_login):
    rt = make_engine(runtime_login)
    try:
        def go(contract, args):
            card = approvals.propose(rt, REGISTRY.contracts[contract], args)
            out = approvals.approve(rt, card.approval_id, card.digest)
            assert out.status == "committed", out.text
            return out
        made = go("create_invoice", {**ARGS, "submit": False})
        with rt.connect() as conn:
            number = conn.execute(select(t.invoice.c.number).where(
                t.invoice.c.operation_key == made.operation_key)).scalar_one()
        go("submit_invoice", {"invoice": number})
        go("record_payment", {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"})
        go("cancel_invoice", {"invoice": number})
        card = approvals.propose(rt, REGISTRY.contracts["create_invoice"], ARGS)
        assert approvals.decline(rt, card.approval_id).status == "declined"
    finally:
        rt.dispose()


def test_the_commit_locks_every_read_set_table_the_runtime_role_may_change(engine, monkeypatch):
    """Spec 2.4's coupling: a read-set row of a table with an UPDATE grant is locked; one without is compared by
    digest only; and granting UPDATE on a master makes the commit lock it with no other change."""
    locked = []
    real = ledger.for_update

    def record(ctx, q):
        locked.append(q.get_final_froms()[0].name)
        return real(ctx, q)

    monkeypatch.setattr(ledger, "for_update", record)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        contract = REGISTRY.contracts["record_payment"]
        inp = contract.input_model.model_validate({"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"})
        rs = approvals.read_set(conn, contract, ctx, inp)
        approvals._lock_read_set(conn, ctx, rs)
        tables = {tb for tb, _, _ in rs}
        updatable = {tb for tb in tables if m.metadata.tables[tb].info["runtime"].update}
        assert updatable - {"fiscal_period"} <= set(locked) and "party" not in locked and "company" not in locked
        conn.rollback()
    locked.clear()
    monkeypatch.setitem(m.item.info, "runtime", Runtime(insert=True, update=("standard_rate",)))
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        contract = REGISTRY.contracts["create_invoice"]
        inp = contract.input_model.model_validate(ARGS)
        approvals._lock_read_set(conn, ctx, approvals.read_set(conn, contract, ctx, inp))
        conn.rollback()
    assert "item" in locked

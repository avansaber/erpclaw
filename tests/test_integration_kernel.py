"""The integration kernel."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from erpclaw_core import integration as ik, ledger, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY, EmptySlot, load_plugins
from erpclaw_core.setup import create_company

QB = "QuickBooks"
D = date(2026, 10, 1)


def company(engine, name, external=True, mapped=("1100", "1200", "3100", "4100", "6900")) -> str:
    create_company(engine, name)
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        if external:
            ik.set_system_of_record(ctx, "accounting", "external", QB)
        for number in mapped:
            ik.map_account(ctx, QB, A(ctx, number), f"QB-{number}")
    return name


def ctx_of(conn, name):
    return company_ctx(conn, company_name=name)


def A(ctx, number):
    return ledger.account_by_number(ctx, number)["id"]


def jp(ctx, debit="1100", credit="4100", amount="10.00", d=D):
    amt = Decimal(amount)
    return ledger.post(ctx, d, "journal", new_id(), [ledger.Line(A(ctx, debit), debit=amt),
                                                     ledger.Line(A(ctx, credit), credit=amt)])


def journals(conn, ctx, direction="out"):
    return [dict(r) for r in conn.execute(select(m.sync_journal).where(
        m.sync_journal.c.company_id == ctx.company_id, m.sync_journal.c.direction == direction)
        .order_by(m.sync_journal.c.at)).mappings()]


def refused_code(fn) -> str:
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


# --- posting when accounting is external ---------------------------------------

def test_an_external_company_posts_an_invoice_to_the_sub_ledger_and_stages_its_journal(engine):
    """An external company posts to the sub-ledger and stages its journal."""
    from conftest import drop_everything

    from erpclaw_sales import service as s

    # Books with this company alone: the pilot sales tables number invoices
    # uniquely across the books, so the demo company's numbers would collide
    # (a pilot limit).
    drop_everything(engine)
    name = company(engine, "External Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        create_party(ctx, "customer", "Acme Ext", "ap@acme.example")
        create_item(ctx, "Widget", Decimal("12.50"))
        cust = dict(conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id)).mappings().one())
        item = dict(conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id)).mappings().one())
        s.create_invoice(ctx, cust, [(item, Decimal("2"))], D)
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        posting = conn.execute(select(m.posting).where(m.posting.c.company_id == ctx.company_id)).mappings().one()
        assert posting["ledger"] == "sub"
        [j] = journals(conn, ctx)
        assert j["local_id"] == posting["id"] and j["status"] == "staged" and j["object_type"] == "posting"
        body = ik.payload(ctx, j["payload_digest"])
        assert {ln["account"] for ln in body["lines"]} == {"QB-1200", "QB-4100"}
        assert sum(Decimal(ln["debit"]) for ln in body["lines"]) == Decimal("25.00")
        assert ledger.verify_chain(conn, ctx.company_id) is None


def test_a_rollback_leaves_neither_the_posting_nor_the_journal(engine):
    name = company(engine, "Rollback Co")
    with pytest.raises(RuntimeError):
        with engine.begin() as conn:
            jp(ctx_of(conn, name))
            raise RuntimeError("abandon the transaction")
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        assert conn.execute(select(func.count()).select_from(m.posting).where(
            m.posting.c.company_id == ctx.company_id)).scalar_one() == 0
        assert journals(conn, ctx) == []


def test_an_unmapped_account_refuses_the_posting_and_writes_nothing(engine):
    name = company(engine, "Unmapped Co", mapped=("1100",))
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        with pytest.raises(Refusal, match="Account 4100 Sales has no QuickBooks account mapped yet") as e:
            jp(ctx)
        assert e.value.code == "unmapped_account"
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        assert conn.execute(select(func.count()).select_from(m.posting).where(
            m.posting.c.company_id == ctx.company_id)).scalar_one() == 0
        assert conn.execute(select(m.posting_chain_head.c.last_seq).where(
            m.posting_chain_head.c.company_id == ctx.company_id)).scalar_one() == 0


def test_a_native_company_posts_to_the_general_ledger_and_stages_nothing(engine):
    name = company(engine, "Native Co", external=False)
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx)
        assert conn.execute(select(m.posting.c.ledger).where(m.posting.c.company_id == ctx.company_id)).scalar_one() \
            == "general"
        assert journals(conn, ctx) == []


def test_accounting_mode_cannot_change_after_a_posting(engine):
    name = company(engine, "Settled Co", external=False)
    with engine.begin() as conn:
        jp(ctx_of(conn, name))
    with engine.begin() as conn:
        assert refused_code(lambda: ik.set_system_of_record(ctx_of(conn, name), "accounting", "external", QB)) \
            == "accounting_has_postings"
        ik.set_system_of_record(ctx_of(conn, name), "payroll", "external", "Gusto")  # other areas may change
        assert ik.system_of_record(ctx_of(conn, name), "payroll") == ("external", "Gusto")


def test_a_reversal_in_an_external_company_is_sub_and_stages_its_own_journal(engine):
    name = company(engine, "Reverse Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        r = jp(ctx)
        ledger.reverse(ctx, r.posting_id, D)
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        kinds = conn.execute(select(m.posting.c.ledger).where(m.posting.c.company_id == ctx.company_id)).scalars().all()
        assert kinds == ["sub", "sub"]
        js = journals(conn, ctx)
        assert len(js) == 2 and len({j["local_id"] for j in js}) == 2


# --- the hash and the migration --------------------------------------------------

def _rewrite_ledger_kind(engine, tmp_path, company_id, new_kind):
    """In a rolled-back transaction, with the append-only triggers out of the
    way, change one posting's ledger kind and report what verify_chain says."""
    with engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text("ALTER TABLE posting DISABLE TRIGGER USER"))
        conn.execute(text("UPDATE posting SET ledger = :k WHERE company_id = :c"), {"k": new_kind, "c": company_id})
        broken = ledger.verify_chain(conn, company_id)
        tx.rollback()
        return broken


@pytest.mark.parametrize("external,new_kind", [(True, "general"), (False, "sub")])
def test_the_hash_covers_the_ledger_kind_in_both_directions(engine, tmp_path, external, new_kind):
    name = company(engine, f"Hash Co {new_kind}", external=external)
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx)
        cid = ctx.company_id
        assert ledger.verify_chain(conn, cid) is None
    assert _rewrite_ledger_kind(engine, tmp_path, cid, new_kind) == 1


def test_a_baseline_with_postings_upgrades_without_changing_a_row(engine):
    """0002->0003 keeps the original row and hash; format-2 head refuses old books atomically."""
    from conftest import drop_everything
    from erpclaw_core.migrate import upgrade

    drop_everything(engine)
    cid, a1, a2, pid = new_id(), new_id(), new_id(), new_id()
    with engine.begin() as conn:
        upgrade(conn, "0002")
        conn.execute(text("INSERT INTO company (id,name,functional_currency,fiscal_year_start_month,created_at) "
                          "VALUES (:c,'Old Books','USD',1,now())"), {"c": cid})
        for aid, number, root in ((a1, "1100", "asset"), (a2, "4100", "income")):
            conn.execute(text("INSERT INTO account (id, company_id, number, name, root_type, account_type, is_group, "
                              "parent_id, frozen) VALUES (:id, :c, :n, :n, :r, NULL, false, NULL, false)"),
                         {"id": aid, "c": cid, "n": number, "r": root})
        header = {"company_id": cid, "seq": 1, "posting_date": "2025-05-01", "voucher_type": "journal",
                  "voucher_id": "v1", "memo": None, "reverses_posting_id": None, "operation_key": None,
                  "created_by": "old"}
        lines = [{"line_no": 1, "account_id": a1, "party_id": None, "debit": "5.00", "credit": "0.00", "currency": "USD"},
                 {"line_no": 2, "account_id": a2, "party_id": None, "debit": "0.00", "credit": "5.00", "currency": "USD"}]
        canonical = json.dumps({**header, "lines": lines}, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(ledger.GENESIS.encode() + canonical).hexdigest()
        conn.execute(text("INSERT INTO posting (id,company_id,seq,posting_date,voucher_type,voucher_id,memo,"
                          "reverses_posting_id,operation_key,created_at,created_by,prev_hash,hash) VALUES "
                          "(:p,:c,1,'2025-05-01','journal','v1',NULL,NULL,NULL,now(),'old',:prev,:hash)"),
                     {"p": pid, "c": cid, "prev": ledger.GENESIS, "hash": digest})
        for line in lines:
            conn.execute(text("INSERT INTO ledger_entry (id,company_id,posting_id,line_no,account_id,party_id,"
                              "debit,credit,currency) VALUES (:id,:company,:posting,:line_no,:account_id,"
                              ":party_id,:debit,:credit,:currency)"),
                         {"id": new_id(), "company": cid, "posting": pid, **line})
        conn.execute(text("INSERT INTO posting_chain_head (company_id,last_seq,last_hash) VALUES (:c,1,:h)"),
                     {"c": cid, "h": digest})
        before = dict(conn.execute(text("SELECT * FROM posting WHERE id=:p"), {"p": pid}).mappings().one())
        before_entries = conn.execute(text("SELECT * FROM ledger_entry WHERE posting_id=:p ORDER BY line_no"),
                                      {"p": pid}).mappings().all()
    with engine.begin() as conn:
        upgrade(conn, "0003")
    with engine.connect() as conn:
        after = dict(conn.execute(text("SELECT * FROM posting WHERE id=:p"), {"p": pid}).mappings().one())
        assert after.pop("ledger") == "general" and after == before
        assert conn.execute(text("SELECT * FROM ledger_entry WHERE posting_id=:p ORDER BY line_no"),
                            {"p": pid}).mappings().all() == before_entries
        legacy_lines = [{field: row[field] for field in ("line_no", "account_id", "party_id", "debit", "credit", "currency")}
                        for row in before_entries]
        legacy_bytes = json.dumps({**header, "lines": legacy_lines}, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(("0" * 64).encode() + legacy_bytes).hexdigest() == digest
        assert conn.execute(text("SELECT last_hash FROM posting_chain_head WHERE company_id=:c"),
                            {"c": cid}).scalar_one() == digest
    with pytest.raises(DBAPIError, match="disposable"):
        with engine.begin() as conn:
            upgrade(conn, "core@head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003"
        after_refusal = dict(conn.execute(text("SELECT * FROM posting WHERE id=:p"), {"p": pid}).mappings().one())
        assert after_refusal.pop("ledger") == "general" and after_refusal == before
        assert conn.execute(text("SELECT * FROM ledger_entry WHERE posting_id=:p ORDER BY line_no"),
                            {"p": pid}).mappings().all() == before_entries
        assert conn.execute(text("SELECT last_hash FROM posting_chain_head WHERE company_id=:c"),
                            {"c": cid}).scalar_one() == digest


# --- maps, payloads, inbound ---------------------------------------------------------

def test_external_ref_keys_refuse_duplicates(engine):
    name = company(engine, "Ref Co")
    with engine.begin() as conn:
        ik.link_external(ctx_of(conn, name), QB, "party", "local-1", "ext-1")
    for local, ext in (("local-1", "ext-2"), ("local-2", "ext-1")):
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                ik.link_external(ctx_of(conn, name), QB, "party", local, ext)


def test_two_companies_with_identical_payloads_each_keep_their_own(engine):
    a, b = company(engine, "Pay A"), company(engine, "Pay B")
    body = {"name": "Acme", "balance": "10.00"}
    with engine.begin() as conn:
        ra = ik.stage_inbound(ctx_of(conn, a), QB, "party", "ext-1", body)
        rb = ik.stage_inbound(ctx_of(conn, b), QB, "party", "ext-1", body)
    with engine.connect() as conn:
        assert ra["id"] != rb["id"]
        assert ik.payload(ctx_of(conn, a), ra["payload_digest"]) == body
        assert ik.payload(ctx_of(conn, b), rb["payload_digest"]) == body
        assert conn.execute(select(func.count()).select_from(m.sync_payload)).scalar_one() == 2


def test_inbound_staging_dedupes_per_record_and_never_overwrites(engine):
    name = company(engine, "Inbound Co")
    body1, body2 = {"name": "Acme", "v": 1}, {"name": "Acme", "v": 2}
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        assert refused_code(lambda: ik.stage_inbound(ctx, QB, "party", None, body1)) == "unidentifiable"
        first = ik.stage_inbound(ctx, QB, "party", "ext-1", body1)
        again = ik.stage_inbound(ctx, QB, "party", "ext-1", body1)
        assert again["id"] == first["id"]  # one exchange
        other = ik.stage_inbound(ctx, QB, "party", "ext-2", body1)  # a different record, identical payload
        assert other["id"] != first["id"]
        conn.execute(m.sync_journal.update().where(m.sync_journal.c.id == first["id"]).values(status="confirmed"))
        changed = ik.stage_inbound(ctx, QB, "party", "ext-1", body2)
        assert changed["status"] == "conflict"
        kept = conn.execute(select(m.sync_journal).where(m.sync_journal.c.id == first["id"])).mappings().one()
        assert kept["status"] == "confirmed" and kept["payload_digest"] == first["payload_digest"]
        assert len(journals(conn, ctx, "in")) == 3


def test_an_empty_connector_slot_is_reported_plainly(engine):
    load_plugins()
    with pytest.raises(EmptySlot, match="No plugin posts journals to an external system in these books"):
        REGISTRY.slot_filler("post_journal", {"core", "sales", "email_folder"})


# --- the setter against a first posting ------------------------------------------------

@pytest.mark.parametrize("first", ["setter", "posting"])
def test_the_setter_and_a_first_posting_in_either_order(engine, first):
    name = company(engine, f"Order {first}", external=False)
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        if first == "setter":
            ik.set_system_of_record(ctx, "accounting", "external", QB)
            jp(ctx)
            assert conn.execute(select(m.posting.c.ledger).where(m.posting.c.company_id == ctx.company_id)).scalar_one() == "sub"
        else:
            jp(ctx)
            assert refused_code(lambda: ik.set_system_of_record(ctx, "accounting", "external", QB)) == \
                "accounting_has_postings"


def test_the_setter_and_a_first_posting_race(engine, monkeypatch):
    checks = ledger._check_lines

    def slow(ctx, lines_):
        time.sleep(0.03)  # widen the window inside the posting's transaction
        return checks(ctx, lines_)

    monkeypatch.setattr(ledger, "_check_lines", slow)
    for i in range(6):
        name = company(engine, f"Race {i}", external=False)
        outcome: dict[str, str] = {}

        def setter():
            time.sleep(0.01 * (i % 3))
            try:
                with engine.begin() as conn:
                    ik.set_system_of_record(ctx_of(conn, name), "accounting", "external", QB)
                outcome["set"] = "ok"
            except Refusal as e:
                outcome["set"] = e.code

        def poster():
            with engine.begin() as conn:
                jp(ctx_of(conn, name))
            outcome["post"] = "ok"

        errors: list[BaseException] = []

        def wrap(fn):
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=wrap, args=(f,)) for f in (setter, poster)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors
        with engine.connect() as conn:
            ctx = ctx_of(conn, name)
            mode, _ = ik.system_of_record(ctx, "accounting")
            kinds = set(conn.execute(select(m.posting.c.ledger).where(m.posting.c.company_id == ctx.company_id)).scalars())
            assert kinds == ({"sub"} if mode == "external" else {"general"}), (mode, kinds, outcome)
            assert (outcome["set"] == "ok") == (mode == "external")


# --- integration regressions --------------------------------------------

@pytest.mark.parametrize("body", [{"amount": 0.1 + 0.2}, {"lines": [{"debit": 1.5}]}, {"deep": {"x": [[2.0]]}}])
def test_payloads_refuse_binary_floats_anywhere(engine, body):
    name = company(engine, "Float Co")
    with engine.begin() as conn:
        assert refused_code(lambda: ik.stage_inbound(ctx_of(conn, name), QB, "party", "ext-1", body)) == "float_in_payload"


def test_payload_decimals_travel_as_canonical_text(engine):
    name = company(engine, "Decimal Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        row = ik.stage_inbound(ctx, QB, "invoice", "ext-9", {"total": Decimal("10.50"), "qty": 3, "paid": False})
        assert ik.payload(ctx, row["payload_digest"]) == {"total": "10.50", "qty": 3, "paid": False}


def test_identical_inbound_records_staged_at_once_are_one_exchange(engine, monkeypatch):
    name = company(engine, "Concurrent Co")
    barrier = threading.Barrier(2, timeout=1)
    lookup = ik._payload_exists

    def synced(ctx, digest):
        found = lookup(ctx, digest)
        try:
            barrier.wait()  # both threads have looked before either inserts (when not serialized)
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(ik, "_payload_exists", synced)
    results, errors = [], []

    def stage():
        try:
            with engine.begin() as conn:
                results.append(ik.stage_inbound(ctx_of(conn, name), QB, "party", "ext-1", {"name": "Acme"})["id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=stage) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert len(results) == 2 and len(set(results)) == 1
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        assert len(journals(conn, ctx, "in")) == 1

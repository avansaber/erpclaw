"""Only an owner's approved card may requeue an unchanged failed/unknown email."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from erpclaw_core import administration, approvals, models as m, outbox, plugins, principals
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core.db import make_engine
from erpclaw_core.setup import create_company

NAME = "resend_message"


def seed_message(engine, status="unknown"):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        oid = outbox.enqueue(ctx, "send_message", "customer@example.test", "Invoice INV-2026-00007",
                             "Your approved invoice total is 125.00 USD.", new_id(),
                             sender=outbox.SenderSnapshot("books@example.test", "Company Books", "reply@example.test", None),
                             attachments=())
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(
            status=status, tries=4, next_try_at=now() + timedelta(hours=6), first_failed_at=now(),
            detail="the mail server could not be reached"))
    return oid


def row(engine, oid):
    with engine.connect() as conn:
        return dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())


def propose(engine, oid, **kwargs):
    return approvals.propose(engine, plugins.REGISTRY.contracts[NAME], {"message": oid}, **kwargs)


def approve(engine, card, **kwargs):
    return approvals.approve(engine, card.approval_id, card.digest, **kwargs)


@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_preview_is_read_only_and_shows_the_frozen_message(engine, monkeypatch, status):
    oid = seed_message(engine, status)
    before = row(engine, oid)
    monkeypatch.setattr(outbox, "deliver_pending", lambda *a: pytest.fail("preview sent mail"))
    card = propose(engine, oid, date_source={"not_applicable": True})
    assert row(engine, oid) == before
    lines = dict(card.card["lines"])
    assert lines.items() >= {"To": before["recipient"], "Subject": before["subject"],
        "Message-ID": before["message_id"], "Status": outbox.owner_text(before),
        "From address": before["from_address"], "From name": before["from_name"],
        "Reply-To": before["reply_to"]}.items()
    assert ("twice" in " ".join(card.card["warnings"])) == (status == "unknown")
    with engine.connect() as conn:
        stored = conn.execute(select(m.approval).where(m.approval.c.id == card.approval_id)).mappings().one()
        assert ["outbox", oid] in [r[:2] for r in json.loads(stored["read_set_json"])]
        assert conn.execute(select(m.outbox_attempt)).first() is None
    assert card.card["date_source"] == {"not_applicable": True}


@pytest.mark.parametrize("status", ["queued", "sending", "accepted"])
def test_other_statuses_never_offer_or_commit_a_resend(engine, status):
    oid = seed_message(engine, status)
    with pytest.raises(Refusal) as exc:
        propose(engine, oid)
    assert exc.value.code == "not_resendable"
    assert row(engine, oid)["status"] == status


@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_owner_requeues_same_identity_and_readback_is_idempotent(engine, status):
    oid = seed_message(engine, status)
    before = row(engine, oid)
    card = propose(engine, oid)
    result = approve(engine, card)
    assert result.status == "committed", result
    after = row(engine, oid)
    reset = {"status": "queued", "current_attempt_id": None, "tries": 0,
             "next_try_at": None, "first_failed_at": None}
    assert after == {**before, **reset}
    assert approvals.read_back(engine, card.operation_key).data == result.data
    assert approve(engine, card).data == result.data
    with engine.connect() as conn:
        contract = plugins.REGISTRY.contracts[NAME]
        assert contract.find_by_operation_key(company_ctx(conn), card.operation_key).data == result.data
        assert contract.find_by_operation_key(company_ctx(conn), "missing") is None
        assert len(conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == card.operation_key)).all()) == 1


@pytest.mark.parametrize("field,value", [
    ("recipient", "other@example.test"), ("subject", "Different subject"), ("body", "Different body"),
    ("from_address", "other@example.test"), ("from_name", "Different Books"),
    ("reply_to", "other@example.test"), ("message_id", "<different@example.test>"),
    ("slot", "different_connector"), ("status", "failed"),
])
def test_any_approved_message_drift_supersedes_without_requeue(engine, field, value):
    oid = seed_message(engine)
    card = propose(engine, oid)
    with engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values({field: value}))
    before = row(engine, oid)
    assert approve(engine, card).status == "superseded"
    assert row(engine, oid) == before


@pytest.mark.parametrize("status", ["queued", "sending", "accepted"])
def test_status_change_before_approval_is_refused_by_fixed_code(engine, status):
    oid = seed_message(engine)
    card = propose(engine, oid)
    with engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status=status))
    result = approve(engine, card)
    assert result.status == "superseded" and result.data["code"] == "not_resendable"
    assert row(engine, oid)["status"] == status


def test_missing_and_other_company_message_are_the_same_refusal(engine):
    oid = seed_message(engine)
    company = row(engine, oid)["company_id"]
    create_company(engine, "Other Books")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Books").company_id
    for message, kwargs in [(new_id(), {"company_id": company}), (oid, {"company_id": other})]:
        with pytest.raises(Refusal) as exc:
            propose(engine, message, **kwargs)
        assert exc.value.code == "no_message"


@pytest.mark.parametrize("grants", [["view", "draft"], list(m.PERMISSIONS)])
def test_non_owner_cannot_resend_even_with_all_grants_or_forged_actor(engine, grants):
    oid = seed_message(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        token = administration.add_principal(conn, "helper", grants, [ctx.company_id])
        helper = principals.authenticate(conn, token)
    card = propose(engine, oid)
    if "approve" not in grants or "post" not in grants:
        with pytest.raises(Refusal) as exc:
            approve(engine, card, principal=helper, actor="owner")
        assert exc.value.code == "forbidden"
        assert row(engine, oid)["status"] == "unknown"
        return
    result = approve(engine, card, principal=helper, actor="owner")
    assert result.status == "refused" and result.data["code"] == "forbidden"
    assert row(engine, oid)["status"] == "unknown"
    drafted = propose(engine, oid, principal=helper)
    result = approve(engine, drafted)
    assert result.status == "refused" and result.data["code"] == "forbidden"
    assert row(engine, oid)["status"] == "unknown"


def test_direct_commit_with_forged_context_is_not_approved(engine):
    oid = seed_message(engine)
    contract = plugins.REGISTRY.contracts[NAME]
    with engine.begin() as conn:
        ctx = company_ctx(conn, actor="owner")
        ctx.extra["commit"] = "forged"
        with pytest.raises(Refusal) as exc:
            contract.commit(ctx, contract.input_model(message=oid), "forged")
        assert exc.value.code == "not_approved"
    assert row(engine, oid)["status"] == "unknown"


def test_resend_and_outcome_rollback_together(engine):
    oid = seed_message(engine)
    card = propose(engine, oid)
    before = row(engine, oid)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO operation_outcome"):
            raise RuntimeError("forced outcome failure")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="forced outcome failure"):
            approve(engine, card)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert row(engine, oid) == before


def test_planned_preview_and_existing_card_cannot_requeue(engine, monkeypatch):
    oid = seed_message(engine)
    card = propose(engine, oid)
    monkeypatch.setattr(outbox, "PLANNED", "Core")
    with pytest.raises(Refusal) as exc:
        propose(engine, oid)
    assert exc.value.code == "planned" and exc.value.capability == "planned.messaging"
    result = approve(engine, card)
    assert result.status == "superseded" and result.data["code"] == "planned"
    assert row(engine, oid)["status"] == "unknown"


def test_registered_chat_capability_and_runtime_commit(engine, runtime_login):
    from erpclaw_core.catalogue import catalogue
    oid = seed_message(engine)
    with engine.connect() as conn:
        entries = catalogue(conn)
    assert next(e for e in entries if e.target == NAME).state == "installed"
    assert plugins.REGISTRY.plugins["core"].manifest.contracts[NAME] == "write"
    assert not {"send_again", "claim", "mark_data", "record_result", "recover", "deliver_pending", "enqueue"} & {e.target for e in entries}
    runtime = make_engine(runtime_login)
    try:
        assert approve(runtime, propose(runtime, oid)).status == "committed"
    finally:
        runtime.dispose()


def test_only_approved_resend_opens_a_new_attempt_with_the_same_message(engine, monkeypatch):
    from slot_support import replace_filler

    with engine.begin() as conn:
        plugins.disable_plugin(conn, "email_folder")
    plugins.install_plugin(engine, "email")
    seen = []
    def send(settings, msg, mark_data):
        assert mark_data()
        seen.append(msg)
        return outbox.SendOutcome("after_data", "accepted", 250, "accepted")
    replace_filler(monkeypatch, "send_message", send)
    oid = seed_message(engine)
    before = row(engine, oid)
    with engine.begin() as conn:
        old = new_id()
        conn.execute(m.outbox_attempt.insert().values(id=old, outbox_id=oid, started_at=now(),
            lease_until=now(), data_at=now()))
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(current_attempt_id=old))
    assert outbox.deliver_pending(engine) == []
    card = propose(engine, oid)
    assert outbox.deliver_pending(engine) == [] and seen == []
    assert approve(engine, card).status == "committed"
    assert seen == []
    assert outbox.deliver_pending(engine)[0]["status"] == "accepted"
    msg, = seen
    lines = dict(card.card["lines"])
    assert (msg.recipient, msg.subject, msg.message_id, msg.from_address, msg.from_name, msg.reply_to) == (
        lines["To"], lines["Subject"], lines["Message-ID"], lines["From address"], lines["From name"], lines["Reply-To"])
    assert msg.body == before["body"] and msg.attachments == ()
    assert row(engine, oid)["current_attempt_id"] != old
    assert outbox.deliver_pending(engine) == [] and len(seen) == 1

"""Approved invoice text, sender binding and queue atomicity on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from email import encoders, policy
from email.message import EmailMessage
from email.parser import BytesParser
import json
import smtplib
import socketserver
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import erpclaw_sales as sales
from erpclaw_core import approvals, ledger, models as m, outbox, plugins
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.db import make_engine
from erpclaw_sales import service as s, tables as t
from slot_support import replace_filler

ARGS = {"customer": "Wayne Enterprises", "items": [{"item": "Circuit Board", "quantity": "2"}], "email": True}
PLACEHOLDER = "[invoice number]"
NOTE = "The sent email will use this exact text with the invoice number filled in when the invoice is approved."
UNAVAILABLE = "Email isn't set up, so it won't be sent. You can download it instead."


def rows(engine, table):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


def counts(engine):
    return tuple(len(rows(engine, tb)) for tb in (t.invoice, m.posting, m.ledger_entry, m.outbox))


def propose(engine, **over):
    return approvals.propose(engine, plugins.REGISTRY.contracts["create_invoice"], {**ARGS, **over})


def approve(engine, card):
    return approvals.approve(engine, card.approval_id, card.digest)


def card_lines(card, label):
    return [v for k, v in card.card["lines"] if k == label]


def queued(engine, card):
    return next(r for r in rows(engine, m.outbox) if r["operation_key"] == card.operation_key)


@pytest.fixture
def mail(engine, monkeypatch):
    """The real email slots; a test sender backed by an existing digest-only table.

    The connector has no settings table yet. A separate party stands in for that
    row without changing a model, migration or runtime policy. Runtime cannot
    UPDATE party, so the sender's read_entry must work without a row lock.
    """
    sender_id = new_id()
    with engine.begin() as conn:
        plugins.disable_plugin(conn, "email_folder")
        conn.execute(m.party.insert().values(id=sender_id, company_id=company_ctx(conn).company_id,
                     kind="supplier", name="Approved Books", email="books@approved.example",
                     city="reply@approved.example", city_key="reply@approved.example"))
    plugins.install_plugin(engine, "email")

    def message_sender(ctx):
        row = ctx.conn.execute(select(m.party).where(m.party.c.id == sender_id)).mappings().first()
        if row is None:
            raise Refusal("No email account is set up for this company.", "no_email_sender")
        return outbox.SenderSnapshot(row["email"], row["name"], row["city"], ("party", sender_id))

    assert replace_filler(monkeypatch, "message_sender", message_sender) == "email"
    return SimpleNamespace(engine=engine, sender_id=sender_id, sender=message_sender)


def edit_sender(mail, **values):
    with mail.engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.id == mail.sender_id).values(**values))


def expected_email(engine, number):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        plan = sales._resolve_invoice(ctx, sales.CreateInvoice.model_validate(ARGS))
        return s.invoice_email(plan.customer, number, plan.priced, plan.total,
                               ctx.today + timedelta(days=s.PAYMENT_TERMS_DAYS), ctx)


def assert_template(engine, card):
    subject, body = expected_email(engine, PLACEHOLDER)
    assert card_lines(card, "Email subject") == [subject]
    assert "\n".join(card_lines(card, "Email body")) == body
    assert NOTE in [v for _, v in card.card["lines"]]
    assert "INV-" not in subject + body


def test_card_shows_exact_sender_template_and_read_set_without_allocating(mail, monkeypatch):
    before = counts(mail.engine), rows(mail.engine, m.number_series)

    def forbidden(*args, **kwargs):
        pytest.fail("Preview allocated an invoice number")

    with monkeypatch.context() as patch:
        patch.setattr(s, "next_number", forbidden)
        card = propose(mail.engine)
    assert_template(mail.engine, card)
    assert card_lines(card, "Email to") == ["ap@wayne.example"]
    assert card_lines(card, "Email from address") == ["books@approved.example"]
    assert card_lines(card, "Email from name") == ["Approved Books"]
    assert card_lines(card, "Email reply-to") == ["reply@approved.example"]
    stored = next(r for r in rows(mail.engine, m.approval) if r["id"] == card.approval_id)
    rs = json.loads(stored["read_set_json"])
    assert [tb for tb, key, _ in rs if key == "email"] == ["plugin_install"]
    assert any((tb, key) == ("party", mail.sender_id) for tb, key, _ in rs)
    assert (counts(mail.engine), rows(mail.engine, m.number_series)) == before
    # Email money is recorded against its own body lines too.
    body_amounts = [a for a in card.card["amounts"] if a["label"] == "Email body"]
    assert [(a["kind"], a["value"]) for a in body_amounts] == [
        ("unit_price", "45.00"), ("amount", "90.00"), ("amount", "90.00")]


def test_intervening_invoice_does_not_supersede_and_replay_queues_once(mail):
    card = propose(mail.engine)
    before = counts(mail.engine)
    other = approve(mail.engine, propose(mail.engine, email=False))
    assert other.status == "committed"
    result = approve(mail.engine, card)
    assert result.status == "committed", result.text
    inv = next(r for r in rows(mail.engine, t.invoice) if r["operation_key"] == card.operation_key)
    prior = next(r for r in rows(mail.engine, t.invoice) if r["operation_key"] == other.operation_key)
    assert int(inv["number"].rsplit("-", 1)[1]) == int(prior["number"].rsplit("-", 1)[1]) + 1
    row = queued(mail.engine, card)
    assert (row["subject"], row["body"]) == expected_email(mail.engine, inv["number"])
    assert card_lines(card, "Email subject")[0].replace(PLACEHOLDER, inv["number"]) == row["subject"]
    assert "\n".join(card_lines(card, "Email body")).replace(PLACEHOLDER, inv["number"]) == row["body"]
    assert row["slot"] == "send_message" and row["status"] == "queued"
    assert row["message_id"] == f"<{row['id']}@approved.example>"
    assert row["operation_key"] == card.operation_key
    assert result.text.endswith(" Email to ap@wayne.example queued.")
    assert "sent" not in result.text and "delivered" not in result.text
    after = counts(mail.engine)
    assert after == tuple(a + b for a, b in zip(before, (2, 2, 4, 1)))
    assert approve(mail.engine, card).text == result.text
    assert counts(mail.engine) == after
    assert rows(mail.engine, m.outbox_attachment) == []
    assert rows(mail.engine, m.file_object) == []


@pytest.mark.parametrize("change", ["email", "name", "city", "unshown"])
def test_sender_change_supersedes_with_digest_only_runtime_entry(mail, runtime_login, monkeypatch, change):
    runtime = make_engine(runtime_login)
    locked = []
    real = ledger.for_update

    def observe(ctx, query):
        locked.append(query.get_final_froms()[0].name)
        return real(ctx, query)

    monkeypatch.setattr(ledger, "for_update", observe)
    try:
        card = propose(runtime)
        before = counts(mail.engine)
        if change == "unshown":
            # All snapshot fields stay identical: only the whole-row digest changes.
            with mail.engine.begin() as conn:
                conn.execute(m.party.update().where(m.party.c.id == mail.sender_id).values(city_key="changed"))
        else:
            edit_sender(mail, **{change: {"email": "new@changed.example", "name": "Changed Books",
                                        "city": "new-reply@changed.example"}[change]})
        result = approve(runtime, card)
        assert result.status == "superseded" and result.card is not None
        assert counts(mail.engine) == before
        assert "party" not in locked and "plugin_install" in locked
        assert not m.party.info["runtime"].update
        with runtime.connect() as conn:
            assert not conn.execute(text("SELECT has_table_privilege(current_user, 'party', 'UPDATE')")).scalar_one()
    finally:
        runtime.dispose()


@pytest.mark.parametrize("missing", ["sender_refusal", "sender_slot", "customer_address", "send_slot", "old_slot_only"])
def test_missing_email_data_still_posts_without_queue(mail, monkeypatch, missing):
    if missing == "sender_refusal":
        with mail.engine.begin() as conn:
            conn.execute(m.party.delete().where(m.party.c.id == mail.sender_id))
    elif missing == "sender_slot":
        monkeypatch.delitem(plugins.REGISTRY.slots["message_sender"], "email")
    elif missing == "customer_address":
        with mail.engine.begin() as conn:
            conn.execute(m.party.update().where(m.party.c.name == ARGS["customer"]).values(email=None))
    elif missing == "send_slot":
        monkeypatch.delitem(plugins.REGISTRY.slots["send_message"], "email")
    else:
        with mail.engine.begin() as conn:
            plugins.disable_plugin(conn, "email")
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email_folder")
                         .values(status="installed"))
            assert outbox.slot_available(conn, "deliver_message")
            assert not outbox.slot_available(conn, "send_message")
    card = propose(mail.engine)
    warning = ("Wayne Enterprises has no email address on file, so it can't be emailed."
               if missing == "customer_address" else UNAVAILABLE)
    assert warning in card.card["warnings"]
    before = counts(mail.engine)
    result = approve(mail.engine, card)
    assert result.status == "committed" and "queued" not in result.text
    assert counts(mail.engine) == tuple(a + b for a, b in zip(before, (1, 1, 2, 0)))


@pytest.mark.parametrize("drift", ["disable", "remove", "version", "artifact_digest", "recipient", "sender_appears"])
def test_email_state_drift_supersedes_without_posting(mail, drift):
    if drift == "sender_appears":
        with mail.engine.begin() as conn:
            saved = dict(conn.execute(select(m.party).where(m.party.c.id == mail.sender_id)).mappings().one())
            conn.execute(m.party.delete().where(m.party.c.id == mail.sender_id))
    card = propose(mail.engine)
    before = counts(mail.engine)
    with mail.engine.begin() as conn:
        if drift == "disable":
            plugins.disable_plugin(conn, "email")
        elif drift == "remove":
            plugins.uninstall_plugin(conn, "email")
        elif drift in ("version", "artifact_digest"):
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email").values(
                **{drift: "0.0.2" if drift == "version" else "a" * 64}))
        elif drift == "recipient":
            conn.execute(m.party.update().where(m.party.c.name == ARGS["customer"]).values(email="other@example.test"))
        else:
            conn.execute(m.party.insert().values(**saved))
    result = approve(mail.engine, card)
    assert result.status == "superseded" and result.card is not None
    assert counts(mail.engine) == before and "queued" not in result.text
    if drift in ("disable", "remove"):
        assert UNAVAILABLE in result.card["warnings"]
    if drift == "sender_appears":
        assert card_lines(result, "Email from address") == ["books@approved.example"]


@pytest.mark.parametrize("existing", [False, True])
def test_install_or_enable_before_recompute_supersedes(mail, existing):
    with mail.engine.begin() as conn:
        if existing:
            plugins.disable_plugin(conn, "email")
        else:
            plugins.uninstall_plugin(conn, "email")
    card = propose(mail.engine)
    assert UNAVAILABLE in card.card["warnings"]
    rs = json.loads(next(r for r in rows(mail.engine, m.approval) if r["id"] == card.approval_id)["read_set_json"])
    with mail.engine.connect() as conn:
        assert next(d for tb, key, d in rs if (tb, key) == ("plugin_install", "email")) == approvals._row_digest(
            conn, "plugin_install", "email")
    before = counts(mail.engine)
    if existing:
        with mail.engine.begin() as conn:
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email").values(status="installed"))
    else:
        plugins.install_plugin(mail.engine, "email")
    result = approve(mail.engine, card)
    assert result.status == "superseded" and result.card is not None
    assert counts(mail.engine) == before
    assert card_lines(result, "Email to") == ["ap@wayne.example"]


def wait_for_lock(conn, pid, future, relation):
    """Observe the actual row-lock wait, including PostgreSQL's transactionid wait.

    PostgreSQL normally grants the tuple lock to the waiter and waits on the
    holder's transaction ID. There must be no wait on another relation.
    """
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        locks = conn.execute(text("SELECT locktype, relation::regclass::text AS relation, granted "
                                  "FROM pg_locks WHERE pid = :pid"), {"pid": pid}).mappings().all()
        waiting = [r for r in locks if not r["granted"]]
        if waiting:
            assert all(r["locktype"] == "transactionid" or
                       (r["locktype"] == "tuple" and r["relation"] == relation) for r in waiting)
            assert any(r["locktype"] == "tuple" and r["relation"] == relation for r in locks)
            assert not any(r["locktype"] == "tuple" and r["relation"] != relation for r in locks)
            assert conn.execute(text("SELECT pg_backend_pid() = ANY(pg_blocking_pids(:pid))"),
                                {"pid": pid}).scalar_one()
            assert not future.done()
            return
        if future.done():
            pytest.fail(f"Writer completed while approval held its locks: {future.result()}")
        time.sleep(0.01)
    pytest.fail("Writer never waited for the approval")


@pytest.mark.parametrize("initial", ["installed", "absent", "disabled"])
def test_email_row_or_absent_install_waits_through_last_read(mail, runtime_login, monkeypatch, initial):
    """Bare disable/enable prove the email row lock; first install proves the head lock."""
    with mail.engine.begin() as conn:
        if initial == "absent":
            plugins.uninstall_plugin(conn, "email")
        elif initial == "disabled":
            plugins.disable_plugin(conn, "email")
    runtime = make_engine(runtime_login)
    card = propose(runtime)
    before = counts(mail.engine)
    paused, release, writing = threading.Event(), threading.Event(), threading.Event()
    timed_out = threading.Event()
    state = {}
    real_create = s.create_invoice
    real_enqueue = outbox.enqueue

    def pause(ctx):
        state["holder"] = ctx.conn
        paused.set()
        assert release.wait(10), "Approval was never released"

    def hold_after_last_read(*args, **kwargs):
        result = real_create(*args, **kwargs)
        pause(args[0])
        return result

    def hold_before_enqueue(ctx, *args, **kwargs):
        pause(ctx)
        return real_enqueue(ctx, *args, **kwargs)

    if initial == "installed":
        monkeypatch.setattr(outbox, "enqueue", hold_before_enqueue)
    else:
        monkeypatch.setattr(s, "create_invoice", hold_after_last_read)

    def change(conn):
        if initial == "absent":
            plugins.install_into(conn, plugins.REGISTRY.plugins["email"])
        else:
            # Deliberately no chain lock: only the email read-set row can block this.
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email").values(
                status="disabled" if initial == "installed" else "installed"))

    def writer():
        with mail.engine.begin() as conn:
            state["pid"] = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            conn.exec_driver_sql("SET LOCAL lock_timeout = '750ms'")
            writing.set()
            try:
                with conn.begin_nested():
                    change(conn)
            except DBAPIError as exc:
                assert exc.orig.sqlstate == "55P03", exc
                timed_out.set()
                assert release.wait(10), "The writer was never released"
                conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                change(conn)  # same connection retries after the approval ends
                return "committed_after_approval"
        return "committed_too_soon"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            approving = pool.submit(approve, runtime, card)
            try:
                assert paused.wait(10)
                competing = pool.submit(writer)
                assert writing.wait(5)
                wait_for_lock(state["holder"], state["pid"], competing,
                              "posting_chain_head" if initial == "absent" else "plugin_install")
                assert timed_out.wait(5), "The writer did not time out on the approval's lock"
                assert not competing.done()
            finally:
                release.set()
            result = approving.result(timeout=10)
            assert competing.result(timeout=10) == "committed_after_approval"
        assert result.status == "committed", result.text
        send = initial == "installed"
        assert counts(mail.engine) == tuple(a + b for a, b in zip(before, (1, 1, 2, int(send))))
        assert ("queued" in result.text) == send
        if send:
            assert queued(mail.engine, card)["from_address"] == "books@approved.example"
        fresh = propose(runtime)
        assert (UNAVAILABLE in fresh.card["warnings"]) == send
        assert bool(card_lines(fresh, "Email to")) == (not send)
    finally:
        release.set()
        runtime.dispose()


@pytest.mark.parametrize("email", [False, True])
def test_books_equal_with_or_without_email(mail, email):
    card = propose(mail.engine, email=email)
    result = approve(mail.engine, card)
    assert result.status == "committed"
    inv = next(r for r in rows(mail.engine, t.invoice) if r["operation_key"] == card.operation_key)
    postings = [r for r in rows(mail.engine, m.posting) if r["voucher_id"] == inv["id"]]
    assert len(postings) == 1
    entries = [r for r in rows(mail.engine, m.ledger_entry) if r["posting_id"] == postings[0]["id"]]
    with mail.engine.connect() as conn:
        accounts = {r["id"]: r["number"] for r in conn.execute(select(m.account)).mappings()}
    assert [(accounts[r["account_id"]], r["debit"], r["credit"], r["party_id"]) for r in entries] == [
        ("1200", Decimal("90.00"), Decimal("0.00"), inv["customer_id"]),
        ("4100", Decimal("0.00"), Decimal("90.00"), None)]
    assert inv["total"] == inv["outstanding"] == Decimal("90.00")
    audits = [r for r in rows(mail.engine, m.audit_log) if r["document_id"] == inv["id"]]
    assert len(audits) == 1 and audits[0]["action"] == "submit_invoice"
    assert json.loads(audits[0]["detail"]) == {"number": inv["number"], "total": "90.00"}
    assert len(rows(mail.engine, m.outbox)) == int(email)


@pytest.mark.parametrize("bad", ["recipient", "sender", "reply_to", "subject"])
def test_enqueue_refusal_rolls_back_invoice_books_number_and_queue(mail, bad):
    if bad == "recipient":
        with mail.engine.begin() as conn:
            conn.execute(m.party.update().where(m.party.c.name == ARGS["customer"]).values(email="a@b.test,c@d.test"))
    elif bad == "sender":
        edit_sender(mail, name="Books\r\nBcc: hidden@example.test")
    elif bad == "reply_to":
        edit_sender(mail, city="not an address")
    else:
        with mail.engine.begin() as conn:
            conn.execute(m.company.update().values(name="Books\nBcc: hidden@example.test"))
    card = propose(mail.engine)
    before = counts(mail.engine), rows(mail.engine, m.number_series)
    result = approve(mail.engine, card)
    assert result.status == "refused", result.text
    assert result.data["code"] in ("not_one_address", "header_line_break")
    assert (counts(mail.engine), rows(mail.engine, m.number_series)) == before


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 test SMTP\r\n")
        while line := self.rfile.readline():
            verb = line.split(None, 1)[0].upper()
            if verb == b"DATA":
                self.wfile.write(b"354 send message\r\n")
                body = []
                while (line := self.rfile.readline()) != b".\r\n":
                    assert line
                    body.append(line[1:] if line.startswith(b"..") else line)
                self.server.messages.append(BytesParser(policy=policy.default).parsebytes(b"".join(body)))
                self.wfile.write(b"250 accepted\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


def test_approved_is_sent_through_delivery_driver_and_sender_stays_frozen(mail, monkeypatch):
    """The SMTP connector is still a stub: replace its transport only, through its registered slot."""
    with socketserver.TCPServer(("127.0.0.1", 0), SMTPHandler) as server:
        server.messages = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def send_message(settings, msg, mark_data):
            message = EmailMessage()
            message["From"] = f"{msg.from_name} <{msg.from_address}>"
            message["To"] = msg.recipient
            message["Reply-To"] = msg.reply_to
            message["Subject"] = msg.subject
            message["Message-ID"] = msg.message_id
            message.set_type("text/plain")
            message.set_param("charset", "utf-8")
            message.set_payload(msg.body.encode("utf-8"))
            encoders.encode_base64(message)  # Preserve even the absence of a final newline.
            assert msg.attachments == ()
            with smtplib.SMTP(*server.server_address, timeout=5) as smtp:
                smtp.ehlo()
                assert smtp.mail(msg.from_address)[0] == 250
                assert smtp.rcpt(msg.recipient)[0] == 250
                assert mark_data()
                assert smtp.data(message.as_bytes(policy=policy.SMTP))[0] == 250
            return outbox.SendOutcome("after_data", "accepted", 250, "accepted")

        replace_filler(monkeypatch, "send_message", send_message)
        monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", "0.02")
        try:
            card = propose(mail.engine)
            assert_template(mail.engine, card)
            result = approve(mail.engine, card)
            assert result.status == "committed"
            edit_sender(mail, name="Changed Books", email="changed@example.test", city="changed-reply@example.test")
            # Settings change before delivery starts, so reading live sender fields
            # at send time deterministically fails the decoded-message assertions.
            with outbox.delivery_driver(mail.engine):
                assert queued(mail.engine, card)["status"] == "accepted"
            (message,) = server.messages
            row = queued(mail.engine, card)
            inv = next(r for r in rows(mail.engine, t.invoice) if r["operation_key"] == card.operation_key)
            assert message["To"] == card_lines(card, "Email to")[0]
            assert message["From"].addresses[0].addr_spec == card_lines(card, "Email from address")[0]
            assert message["From"].addresses[0].display_name == card_lines(card, "Email from name")[0]
            assert message["Reply-To"].addresses[0].addr_spec == card_lines(card, "Email reply-to")[0]
            assert message["Subject"] == card_lines(card, "Email subject")[0].replace(PLACEHOLDER, inv["number"])
            assert message.get_content() == "\n".join(card_lines(card, "Email body")).replace(PLACEHOLDER, inv["number"])
            assert message["Message-ID"] == row["message_id"]
            assert not message.is_multipart()
            attempt = next(r for r in rows(mail.engine, m.outbox_attempt) if r["outbox_id"] == row["id"])
            assert attempt["data_at"] is not None and attempt["result"] == "accepted"
        finally:
            server.shutdown()
            worker.join(5)


@pytest.mark.parametrize("initially_available", [False, True])
def test_last_availability_read_supersedes_even_after_digest_check(mail, monkeypatch, initially_available):
    if not initially_available:
        with mail.engine.begin() as conn:
            plugins.disable_plugin(conn, "email")
    card = propose(mail.engine)
    before = counts(mail.engine), rows(mail.engine, m.number_series)
    available = outbox.slot_available
    create = s.create_invoice
    changed = False

    def availability(conn, slot):
        if changed and slot == "send_message":
            return not initially_available
        return available(conn, slot)

    def change_after_commit_decision(*args, **kwargs):
        nonlocal changed
        changed = True
        return create(*args, **kwargs)

    monkeypatch.setattr(outbox, "slot_available", availability)
    monkeypatch.setattr(s, "create_invoice", change_after_commit_decision)
    result = approve(mail.engine, card)
    assert result.status == "superseded" and result.card is not None
    assert (counts(mail.engine), rows(mail.engine, m.number_series)) == before


def test_last_sender_read_supersedes_instead_of_queueing_drift(mail, monkeypatch):
    card = propose(mail.engine)
    before = counts(mail.engine), rows(mail.engine, m.number_series)
    create = s.create_invoice
    changed = False

    def message_sender(ctx):
        snapshot = mail.sender(ctx)
        if changed:
            return outbox.SenderSnapshot("changed@example.test", "Changed", None, snapshot.read_entry)
        return snapshot

    def change_after_commit_decision(*args, **kwargs):
        nonlocal changed
        changed = True
        return create(*args, **kwargs)

    replace_filler(monkeypatch, "message_sender", message_sender)
    monkeypatch.setattr(s, "create_invoice", change_after_commit_decision)
    result = approve(mail.engine, card)
    assert result.status == "superseded" and result.card is not None
    assert (counts(mail.engine), rows(mail.engine, m.number_series)) == before
    assert card_lines(result, "Email from address") == ["changed@example.test"]


def test_template_preserves_multiline_item_data_and_its_money(mail):
    name = "Circuit Board\nfor repair"
    with mail.engine.begin() as conn:
        conn.execute(m.item.update().where(m.item.c.name == "Circuit Board").values(name=name))
    args = {**ARGS, "items": [{"item": name, "quantity": "2"}]}
    card = propose(mail.engine, items=args["items"])
    result = approve(mail.engine, card)
    assert result.status == "committed", result.text
    inv = next(r for r in rows(mail.engine, t.invoice) if r["operation_key"] == card.operation_key)
    with mail.engine.connect() as conn:
        ctx = company_ctx(conn)
        plan = sales._resolve_invoice(ctx, sales.CreateInvoice.model_validate(args))
        subject, body = s.invoice_email(plan.customer, inv["number"], plan.priced, plan.total,
                                       inv["due_date"], ctx)
    row = queued(mail.engine, card)
    assert (row["subject"], row["body"]) == (subject, body)
    assert "\n".join(card_lines(card, "Email body")).replace(PLACEHOLDER, inv["number"]) == body

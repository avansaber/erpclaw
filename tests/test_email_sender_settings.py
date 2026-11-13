"""Sender settings use the real migration, approval transaction and scoped reads."""
import json
from email import policy
from email.parser import BytesParser

import pytest
from pydantic import ValidationError
from sqlalchemy import event, inspect, select

import erpclaw_email as email
from erpclaw_core import administration, approvals, models as m, outbox, plugins, principals
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.db import make_engine
from erpclaw_core.setup import create_company
from erpclaw_email import smtp
from test_email_smtp_connector import assert_envelope, certificates, smtp_server, trusted_slot

ARGS = dict(from_address="books@bücher.example", display_name="Company Books", smtp_host="smtp.example",
            port=587, security="starttls", username="books", reply_to="reply@example.test")
STORED_FROM = "books@xn--bcher-kva.example"
NAME = "update_email_sender"


@pytest.fixture
def configured(engine):
    with engine.begin() as conn:
        plugins.disable_plugin(conn, "email_folder")
    plugins.install_plugin(engine, "email")
    return engine


def propose(engine, **over):
    return approvals.propose(engine, plugins.REGISTRY.contracts[NAME], {**ARGS, **over})


def approve(engine, card, **kwargs):
    return approvals.approve(engine, card.approval_id, card.digest, **kwargs)


def sender_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(email.tables.email_sender)).mappings()]


def test_plugin_migration_and_capability(configured):
    assert email.MANIFEST.owned_tables == ["email_sender"]
    assert email.MANIFEST.contracts == {NAME: "write"}
    assert set(email.PLUGIN.slots) == {"send_message", "message_sender"}
    assert [c.name for c in email.PLUGIN.contracts] == [NAME]
    assert email.PLUGIN.contracts[0].planned is None
    assert "password" not in email.PLUGIN.contracts[0].tool_schema()["function"]["parameters"]["properties"]
    with configured.connect() as conn:
        assert "email_0001" in conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
        columns = {c["name"] for c in inspect(conn).get_columns("email_sender")}
        assert columns == {"id", "company_id", *ARGS}
    assert email.tables.email_sender.info["owner"] == "email"
    assert not email.tables.email_sender.info["runtime"].delete


def test_missing_sender_uses_existing_refusal(configured):
    with configured.connect() as conn:
        with pytest.raises(Refusal, match="No email account") as exc:
            email.message_sender(company_ctx(conn))
    assert exc.value.code == "no_email_sender"


@pytest.mark.parametrize("secret", [None, "synthetic-smtp-secret", "pässwörd-✓"])
def test_preview_all_fields_read_set_and_no_secret(configured, monkeypatch, secret):
    with configured.connect() as conn:
        cid = company_ctx(conn).company_id
    monkeypatch.setattr(smtp, "company_password", lambda company: secret if company == cid else pytest.fail("scope"))
    card = propose(configured)
    lines = dict(card.card["lines"])
    assert lines.items() >= {"From address": STORED_FROM, "Display name": ARGS["display_name"],
        "SMTP host": ARGS["smtp_host"], "Port": "587", "Security": "starttls", "Username": "books",
        "Reply-To": ARGS["reply_to"], "Password": "set" if secret else "not set"}.items()
    assert sender_rows(configured) == []
    with configured.connect() as conn:
        row = conn.execute(select(m.approval).where(m.approval.c.id == card.approval_id)).mappings().one()
    rs = {(t, k) for t, k, _ in json.loads(row["read_set_json"])}
    assert {("company", cid), ("email_sender", cid)} <= rs
    assert "synthetic-smtp-secret" not in str(dict(row))
    if secret:
        assert secret not in str(dict(row)) + str(card.card)


def test_non_ascii_username_is_approved_and_stored(configured):
    username = "böoks-✓"
    card = propose(configured, username=username)
    assert dict(card.card["lines"])["Username"] == username
    assert approve(configured, card).status == "committed"
    assert sender_rows(configured)[0]["username"] == username


def test_create_update_replay_snapshot_and_readback(configured):
    first = propose(configured)
    assert approve(configured, first).status == "committed"
    assert len(sender_rows(configured)) == 1
    with configured.connect() as conn:
        ctx = company_ctx(conn)
        snap = email.message_sender(ctx)
    assert snap == outbox.SenderSnapshot(STORED_FROM, ARGS["display_name"], ARGS["reply_to"],
                                         ("email_sender", ctx.company_id))
    updated = propose(configured, smtp_host="other.example", reply_to=None, display_name="New Books")
    result = approve(configured, updated)
    assert result.status == "committed"
    assert approvals.read_back(configured, updated.operation_key).data == result.data
    assert approve(configured, updated).data == result.data
    assert len(sender_rows(configured)) == 1
    assert sender_rows(configured)[0]["smtp_host"] == "other.example"
    assert snap.from_name == "Company Books"
    with configured.connect() as conn:
        found = email.UPDATE_EMAIL_SENDER.find_by_operation_key(company_ctx(conn), updated.operation_key)
        assert found.data == result.data
        assert email.UPDATE_EMAIL_SENDER.find_by_operation_key(company_ctx(conn), "unknown") is None
        outcomes = conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == updated.operation_key)).all()
    assert len(outcomes) == 1


@pytest.mark.parametrize("initial", [False, True])
def test_unicode_domains_preview_and_store_idna_on_create_and_update(configured, initial):
    if initial:
        assert approve(configured, propose(configured, from_address="books@example.test")).status == "committed"
    card = propose(configured, reply_to="Reply+Books@bücher.example")
    lines = dict(card.card["lines"])
    assert lines["From address"] == STORED_FROM
    assert lines["Reply-To"] == "Reply+Books@xn--bcher-kva.example"
    assert approve(configured, card).status == "committed"
    row, = sender_rows(configured)
    assert row["from_address"] == lines["From address"]
    assert row["reply_to"] == lines["Reply-To"]
    with configured.connect() as conn:
        snap = outbox.sender(company_ctx(conn))
    assert snap.from_address == row["from_address"]
    assert snap.reply_to == row["reply_to"]


def test_unicode_sender_queues_and_sends_ascii_headers_and_envelope(
        configured, smtp_server, trusted_slot, monkeypatch):
    monkeypatch.setattr(smtp, "company_password", lambda cid: "synthetic-smtp-secret")
    with smtp_server(host="localhost") as fake:
        card = propose(configured, smtp_host="localhost", port=fake.port, reply_to="reply@bücher.example")
        assert approve(configured, card).status == "committed"
        with configured.begin() as conn:
            ctx = company_ctx(conn)
            oid = outbox.enqueue(ctx, "send_message", "to@example.test", "Subject", "Body", None,
                                 sender=outbox.sender(ctx), attachments=())
        assert outbox.deliver_pending(configured)[0]["status"] == "accepted"
    with configured.connect() as conn:
        row = conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one()
    assert row["message_id"] == f"<{oid}@xn--bcher-kva.example>"
    assert row["from_address"] == STORED_FROM
    assert row["reply_to"] == "reply@xn--bcher-kva.example"
    assert len(fake.messages) == 1
    wire = fake.messages[0]
    assert wire.isascii()
    message = BytesParser(policy=policy.default).parsebytes(wire)
    assert str(message["Message-ID"]) == row["message_id"]
    assert message["From"].addresses[0].addr_spec == STORED_FROM
    assert message["Reply-To"].addresses[0].addr_spec == row["reply_to"]
    assert_envelope(fake.envelope, STORED_FROM, "to@example.test")


@pytest.mark.parametrize("field", ["from_address", "reply_to"])
def test_non_ascii_local_part_refused_before_any_write(configured, field):
    with pytest.raises(ValidationError, match="give one plain email address"):
        propose(configured, **{field: "böoks@bücher.example"})
    assert sender_rows(configured) == []
    with configured.connect() as conn:
        assert conn.execute(select(m.approval).where(m.approval.c.contract == NAME)).first() is None
        assert conn.execute(select(m.operation_outcome)).first() is None


def test_ascii_sender_stored_row_is_byte_identical(configured):
    args = {**ARGS, "from_address": "Books+Ledger@EXAMPLE.test", "reply_to": "Reply+Books@EXAMPLE.test"}
    card = propose(configured, **args)
    assert dict(card.card["lines"])["From address"] == args["from_address"]
    assert dict(card.card["lines"])["Reply-To"] == args["reply_to"]
    assert approve(configured, card).status == "committed"
    row, = sender_rows(configured)
    expected = dict(id=row["company_id"], company_id=row["company_id"], **args)
    before = json.dumps(expected, sort_keys=True).encode("ascii")
    assert json.dumps(row, sort_keys=True).encode("ascii") == before
    assert approve(configured, propose(configured, **args)).status == "committed"
    assert json.dumps(sender_rows(configured)[0], sort_keys=True).encode("ascii") == before


@pytest.mark.parametrize("field,value", [
    ("from_address", "Name <a@example.test>"), ("from_address", "a@example.test,b@example.test"),
    ("from_address", "a@example.test\r\n"), ("reply_to", "Reply <a@example.test>"),
    ("reply_to", "a@example.test,b@example.test"), ("reply_to", "a@example.test\n"),
    ("from_address", "a@-bad.example"), ("from_address", "a..b@example.test"),
    ("smtp_host", "https://smtp.example"), ("smtp_host", "bad host"),
    ("smtp_host", "-bad.example"), ("smtp_host", "smtp.example\n"),
    ("smtp_host", "127.0.0.1"), ("port", 0), ("port", 65536), ("port", True),
    ("security", "none"), ("display_name", "Books\r\nBcc: hidden@example.test"),
    ("username", "books\n"),
])
def test_invalid_settings_refused_before_any_write(configured, field, value):
    with pytest.raises((ValidationError, Refusal)):
        propose(configured, **{field: value})
    assert sender_rows(configured) == []
    with configured.connect() as conn:
        assert conn.execute(select(m.approval).where(m.approval.c.contract == NAME)).first() is None


@pytest.mark.parametrize("initial", [False, True])
def test_stale_sender_or_absence_supersedes(configured, initial):
    if initial:
        assert approve(configured, propose(configured)).status == "committed"
    stale = propose(configured, display_name="Stale")
    assert approve(configured, propose(configured, display_name="Current")).status == "committed"
    assert approve(configured, stale).status == "superseded"
    assert sender_rows(configured)[0]["display_name"] == "Current"


def test_company_scoping_and_readctx(configured, monkeypatch):
    assert approve(configured, propose(configured)).status == "committed"
    create_company(configured, "Other Books")
    seen = []
    original = outbox.ReadCtx.one
    def observe(self, table, **where):
        seen.append((self.company_id, table))
        return original(self, table, **where)
    monkeypatch.setattr(outbox.ReadCtx, "one", observe)
    with configured.connect() as conn:
        ctx = company_ctx(conn, company_name="Other Books")
        with pytest.raises(Refusal) as exc:
            email.message_sender(ctx)
    assert exc.value.code == "no_email_sender"
    assert seen == [(ctx.company_id, "email_sender")]


def test_owner_only_even_with_all_existing_grants_and_forged_actor(configured):
    with configured.begin() as conn:
        ctx = company_ctx(conn)
        token = administration.add_principal(conn, "settings-helper", m.PERMISSIONS, [ctx.company_id])
        other = principals.authenticate(conn, token)
    card = propose(configured)
    result = approve(configured, card, principal=other, actor="owner")
    assert result.status == "refused" and "Only the owner" in result.text
    assert sender_rows(configured) == []
    drafted = approvals.propose(configured, email.UPDATE_EMAIL_SENDER, ARGS, principal=other)
    assert approve(configured, drafted).status == "refused"
    assert sender_rows(configured) == []


def test_direct_commit_requires_live_approval(configured):
    with configured.begin() as conn:
        ctx = company_ctx(conn)
        ctx.extra["commit"] = "forged"
        with pytest.raises(Refusal) as exc:
            email.UPDATE_EMAIL_SENDER.commit(ctx, email.UpdateEmailSender(**ARGS), "forged")
    assert exc.value.code == "not_approved"
    assert sender_rows(configured) == []


def test_settings_and_outcome_rollback_together(configured):
    card = propose(configured)
    def fail_outcome(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO operation_outcome"):
            raise RuntimeError("forced outcome failure")
    event.listen(configured, "before_cursor_execute", fail_outcome)
    try:
        with pytest.raises(RuntimeError, match="forced outcome failure"):
            approve(configured, card)
    finally:
        event.remove(configured, "before_cursor_execute", fail_outcome)
    assert sender_rows(configured) == []
    assert approve(configured, card).status == "committed"


def test_runtime_can_update_settings(configured, runtime_login):
    runtime = make_engine(runtime_login)
    try:
        assert approve(runtime, propose(runtime)).status == "committed"
        assert approve(runtime, propose(runtime, port=465, security="tls")).status == "committed"
    finally:
        runtime.dispose()
    assert sender_rows(configured)[0]["port"] == 465


@pytest.mark.parametrize("smtp_already_installed", [False, True])
def test_production_bootstrap_and_cli_install_grant_runtime_access(
        engine, runtime_login, monkeypatch, smtp_already_installed):
    """Both owner install paths grant table policies to an already existing runtime role."""
    from typer.testing import CliRunner

    from conftest import cli_urls, drop_everything
    from erpclaw_cli.main import app
    from erpclaw_core.db import metadata
    from erpclaw_core.grants import validate_runtime_session
    from erpclaw_core.setup import _schema

    # Bootstrap through the production CLI, with the role created before any plugin table.
    drop_everything(engine)
    if smtp_already_installed:
        # An interrupted bootstrap may have schema and installs but no company yet.
        with engine.begin() as conn:
            _schema(conn)
        plugins.install_plugin(engine, "email")  # bootstrap must supply the missing runtime grants
    cli_urls(monkeypatch, engine, runtime_login)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--own-books"])
    assert result.exit_code == 0, (result.output, result.exception)
    runtime = make_engine(runtime_login)
    try:
        assert validate_runtime_session(runtime) is None
        with runtime.connect() as conn:
            installed = set(conn.execute(select(m.plugin_install.c.name)).scalars())
            connector = "email" if smtp_already_installed else "email_folder"
            assert installed == {"sales", "stock", connector}
            if not smtp_already_installed:
                # Bootstrap deliberately leaves the real SMTP connector to the owner.
                assert not inspect(conn).has_table("email_sender")
            for name in installed:
                for table in plugins.REGISTRY.plugins[name].manifest.owned_tables:
                    conn.execute(select(metadata.tables[table]).limit(1)).all()

        if not smtp_already_installed:
            result = runner.invoke(app, ["plugins", "disable", "email_folder"])
            assert result.exit_code == 0, (result.output, result.exception)
            result = runner.invoke(app, ["plugins", "install", "email", "--yes"])
            assert result.exit_code == 0, (result.output, result.exception)
        assert validate_runtime_session(runtime) is None
        with runtime.connect() as conn:
            assert conn.execute(select(email.tables.email_sender)).all() == []
    finally:
        runtime.dispose()


def test_disabled_plugin_card_cannot_write(configured):
    card = propose(configured)
    with configured.begin() as conn:
        plugins.disable_plugin(conn, "email")
    assert approve(configured, card).status != "committed"
    assert sender_rows(configured) == []


def test_from_header_encodes_non_ascii_domain_before_formatting():
    msg = outbox.OutboundMessage("id", "company", "<id@example.test>", "to@example.test",
        ARGS["from_address"], "Bücher Books", None, "Subject", "Body")
    parsed = BytesParser(policy=policy.default).parsebytes(smtp._message(msg))
    assert parsed["From"].addresses[0].addr_spec == "books@xn--bcher-kva.example"
    assert parsed["From"].addresses[0].display_name == "Bücher Books"


def test_catalogue_installed_and_planned_states(configured, monkeypatch):
    from erpclaw_core.catalogue import catalogue, decide
    with configured.connect() as conn:
        entries = catalogue(conn)
    entry = next(e for e in entries if e.target == NAME)
    assert entry.state == "installed" and decide(entries, entry.id).outcome == "a"
    helpers = {"send_message", "message_sender", "sender", "enqueue", "ReadCtx", "deliver_pending",
               "claim", "mark_data", "record_result", "recover"}
    assert not helpers & {e.target for e in entries}
    monkeypatch.setattr(email.UPDATE_EMAIL_SENDER, "planned", "Core")
    with configured.connect() as conn:
        entries = catalogue(conn)
    entry = next(e for e in entries if e.target == NAME)
    assert entry.state == "planned" and decide(entries, entry.id).outcome == "c"
    with pytest.raises(Refusal) as exc:
        propose(configured)
    assert exc.value.code == "planned"


def test_environment_secret_is_only_shown_as_presence(configured, monkeypatch):
    with configured.connect() as conn:
        cid = company_ctx(conn).company_id
    secret = "synthetic-never-store-password"
    monkeypatch.setenv("ERPCLAW_SMTP_PASSWORD_" + cid.replace("-", "_"), secret)
    card = propose(configured)
    assert dict(card.card["lines"])["Password"] == "set"
    assert approve(configured, card).status == "committed"
    with configured.connect() as conn:
        for table in (email.tables.email_sender, m.approval, m.operation_outcome, m.audit_log):
            assert secret not in str(conn.execute(select(table)).mappings().all())


def test_real_sender_binds_invoice_and_queued_snapshot(configured):
    assert approve(configured, propose(configured)).status == "committed"
    invoice = plugins.REGISTRY.contracts["create_invoice"]
    args = {"customer": "Wayne Enterprises", "items": [{"item": "Circuit Board", "quantity": "2"}], "email": True}
    pending = approvals.propose(configured, invoice, args)
    # A connection field not displayed on the invoice still belongs to its read set.
    assert approve(configured, propose(configured, smtp_host="new.example")).status == "committed"
    assert approve(configured, pending).status == "superseded"
    fresh = approvals.propose(configured, invoice, args)
    assert approve(configured, fresh).status == "committed"
    assert approve(configured, propose(configured, from_address="new@example.test")).status == "committed"
    with configured.connect() as conn:
        row = conn.execute(select(m.outbox).where(m.outbox.c.operation_key == fresh.operation_key)).mappings().one()
    assert row["from_address"] == STORED_FROM
    assert row["from_name"] == ARGS["display_name"]
    assert row["reply_to"] == ARGS["reply_to"]


def test_password_presence_drift_supersedes(configured, monkeypatch):
    monkeypatch.setattr(smtp, "company_password", lambda cid: None)
    card = propose(configured)
    monkeypatch.setattr(smtp, "company_password", lambda cid: "synthetic-present")
    result = approve(configured, card)
    assert result.status == "superseded"
    assert dict(result.card["lines"])["Password"] == "set"
    assert sender_rows(configured) == []


def test_operation_http_readback(configured):
    from fastapi.testclient import TestClient
    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import ScriptedModel
    card = propose(configured)
    result = approve(configured, card)
    client = TestClient(create_app(configured, ScriptedModel([]), embedded_owner=True),
                        base_url="http://127.0.0.1")
    response = client.get("/v1/operations/" + card.operation_key)
    assert response.status_code == 200
    assert response.json()["status"] == "committed"
    assert response.json()["text"] == result.text


@pytest.mark.parametrize("field", ["from_address", "reply_to"])
def test_sender_addresses_fit_the_frozen_outbox_headers(field):
    address = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + ".example"
    assert len(address) == m.outbox.c[field].type.length == 200
    assert email.UpdateEmailSender(**{**ARGS, field: address})
    with pytest.raises(ValidationError):
        email.UpdateEmailSender(**{**ARGS, field: address + "s"})


@pytest.mark.parametrize("field", ["from_address", "reply_to"])
def test_idna_expansion_must_fit_the_frozen_outbox_headers(configured, field):
    address = "a" * 64 + "@" + ".".join(["ü" * 30] * 4)
    assert len(address) < 200 < len(smtp._envelope(address))
    with pytest.raises(ValidationError, match="the email address is too long"):
        propose(configured, **{field: address})
    assert sender_rows(configured) == []
    with configured.connect() as conn:
        assert conn.execute(select(m.approval).where(m.approval.c.contract == NAME)).first() is None

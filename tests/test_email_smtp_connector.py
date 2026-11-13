"""Real SMTP exchanges on loopback, with an ephemeral test CA and no external service."""
from __future__ import annotations

import base64
import hmac
import logging
import secrets
import socketserver
import ssl
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
import trustme

from erpclaw_core import outbox
from erpclaw_core.outbox import OutboundAttachment, OutboundMessage, SendOutcome
from erpclaw_email.smtp import TIMEOUT, company_password, send_message


MSG = OutboundMessage(
    id="6d1f3c2e-0000-4000-8000-000000000001", company_id="c0ffee00-0000-4000-8000-000000000001",
    message_id="<6d1f3c2e-0000-4000-8000-000000000001@stark.test>", recipient="accounts@acme.test",
    from_address="books@stark.test", from_name="Stark Books", reply_to="reply@stark.test",
    subject="Invoice INV-2026-00007 — approved", body="Approved plain text.\n.A dot at the start.\nThank you.\n",
)


class Settings:
    """ReadCtx.one's interface; this step adds no persistent sender settings or secrets."""

    def __init__(self, port, security="starttls"):
        self.row = dict(smtp_host="127.0.0.1", port=port, security=security, username="books")
        self.reads = []
        self.closed = False

    def one(self, table, **where):
        assert not self.closed, "settings were read after mark_data closed ReadCtx"
        self.reads.append((table, where))
        assert table == "email_sender" and not where
        return self.row.copy()


class WireServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


class WireHandler(socketserver.BaseRequestHandler):
    def handle(self):
        fake = self.server.fake
        sock = self.request
        sock.settimeout(3)
        reader = None
        tls = False
        fake.connections += 1
        try:
            if fake.implicit_tls:
                sock = fake.context.wrap_socket(sock, server_side=True)
                tls = True
            reader = sock.makefile("rb")
            sock.sendall(b"220 local SMTP fake\r\n")
            while line := reader.readline():
                command = line.split(b" ", 1)[0].strip().upper().decode("ascii")
                # Never retain AUTH's credential payload, even in this fake.
                fake.commands.append(command)
                fake.tls_at_command.append((command, tls))
                if command == "EHLO":
                    extensions = [b"250-local", b"250-AUTH " + fake.auth_mechanisms.encode("ascii")]
                    if fake.starttls and not tls:
                        extensions.append(b"250-STARTTLS")
                    sock.sendall(b"\r\n".join(extensions + [b"250 OK"]) + b"\r\n")
                elif command == "STARTTLS":
                    sock.sendall(b"220 Begin TLS\r\n")
                    reader.close()
                    reader = None
                    sock = fake.context.wrap_socket(sock, server_side=True)
                    tls = True
                    reader = sock.makefile("rb")
                elif command == "AUTH":
                    fake.auth_seen = True
                    assert tls, "connector exposed credentials before TLS"
                    mechanism = line.split()[1]
                    if mechanism == b"CRAM-MD5":
                        sock.sendall(b"334 " + base64.b64encode(b"local SMTP challenge") + b"\r\n")
                        response = reader.readline()
                        payload = base64.b64decode(response)
                    else:
                        payload = base64.b64decode(line.split()[2])
                    if mechanism == b"LOGIN":
                        sock.sendall(b"334 UGFzc3dvcmQ6\r\n")
                        response = reader.readline()
                        if response == b"QUIT\r\n":
                            sock.sendall(b"221 Bye\r\n")
                            return
                        payload += b"\0" + base64.b64decode(response)
                    if fake.auth_check is not None:
                        assert fake.auth_check(payload), "unexpected AUTH bytes"
                    sock.sendall(fake.reply("AUTH"))
                elif command in {"MAIL", "RCPT"}:
                    fake.envelope.append(line.decode("ascii").strip())
                    sock.sendall(fake.reply(command))
                elif command == "DATA":
                    fake.data_seen = True
                    if fake.data_command_code != 354:
                        sock.sendall(f"{fake.data_command_code} DATA refused\r\n".encode())
                        continue
                    sock.sendall(b"354 End with dot\r\n")
                    body = []
                    while (part := reader.readline()) not in {b".\r\n", b""}:
                        body.append(part[1:] if part.startswith(b"..") else part)
                    fake.messages.append(b"".join(body))
                    if fake.drop_after_data:
                        return
                    sock.sendall(fake.reply("DATA"))
                elif command == "QUIT":
                    sock.sendall(b"221 Bye\r\n")
                    return
                else:
                    sock.sendall(b"500 Unexpected command\r\n")
        except (ssl.SSLError, ConnectionError, TimeoutError):
            # A rejected TLS handshake is expected; never print the exception or request.
            pass
        finally:
            if reader is not None:
                reader.close()
            sock.close()


class FakeSMTP:
    def __init__(self, context, *, implicit_tls=False, starttls=True, replies=None, drop_after_data=False,
                 data_command_code=354, auth_check=None, auth_mechanisms="PLAIN"):
        self.context, self.implicit_tls, self.starttls = context, implicit_tls, starttls
        self.replies = replies or {}
        self.drop_after_data, self.data_command_code = drop_after_data, data_command_code
        self.commands, self.tls_at_command, self.envelope, self.messages = [], [], [], []
        self.connections = 0
        self.auth_seen = self.data_seen = False
        self.auth_check = auth_check
        self.auth_mechanisms = auth_mechanisms

    def reply(self, command):
        default = (235, "authenticated") if command == "AUTH" else (250, "OK")
        code, text = self.replies.get(command, default)
        lines = text.splitlines()
        return b"".join(f"{code}{' ' if i == len(lines) - 1 else '-'}{line}\r\n".encode()
                        for i, line in enumerate(lines))


def assert_envelope(envelope, sender, recipient):
    # CPython 3.12 smtplib emits FROM:/TO:, 3.14 emits from:/to:.
    # RFC 5321 makes SMTP command keywords case-insensitive, but mailbox
    # local-parts must keep their case. Keep the fake's raw wire capture.
    parsed = [(prefix.lower(), address) for prefix, _, address in
              (line.partition(":") for line in envelope)]
    assert parsed == [("mail from", f"<{sender}>"), ("rcpt to", f"<{recipient}>")]


@pytest.mark.parametrize("keyword_case", [str.upper, str.lower])
def test_envelope_keywords_allow_both_stdlib_forms_but_preserve_mailbox_case(keyword_case):
    envelope = [f"mail {keyword_case('from')}:<Books@example.test>",
                f"rcpt {keyword_case('to')}:<Accounts@example.test>"]
    assert_envelope(envelope, "Books@example.test", "Accounts@example.test")
    with pytest.raises(AssertionError):
        assert_envelope(envelope, "books@example.test", "Accounts@example.test")
    with pytest.raises(AssertionError):
        assert_envelope(envelope, "Books@example.test", "accounts@example.test")


@pytest.fixture
def certificates(tmp_path):
    ca = trustme.CA()

    def server_context(host):
        cert = ca.issue_cert(host)
        key, chain = tmp_path / f"{host}.key", tmp_path / f"{host}.pem"
        key.write_bytes(cert.private_key_pem.bytes())
        chain.write_bytes(b"".join(p.bytes() for p in cert.cert_chain_pems))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(chain, key)
        return context

    return ca.cert_pem.bytes().decode(), server_context


@pytest.fixture
def smtp_server(certificates):
    _, server_context = certificates

    @contextmanager
    def run(*, host="127.0.0.1", **kwargs):
        fake = FakeSMTP(server_context(host), **kwargs)
        with WireServer(("127.0.0.1", 0), WireHandler) as server:
            server.fake = fake
            fake.port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield fake
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive()
    return run


def call(fake, certificates, *, mark=None, msg=MSG, password=None, settings=None, trust=True):
    settings = settings or Settings(fake.port, "tls" if fake.implicit_tls else "starttls")
    secret = "synthetic-" + secrets.token_hex(16)
    result = send_message(settings, msg, mark or (lambda: True), password=password or (lambda: secret),
                          _test_ca=certificates[0] if trust else None)
    assert isinstance(result, SendOutcome)
    assert result.reason is None or result.reason in outbox.REASONS
    return result


@pytest.fixture
def trusted_slot(certificates, monkeypatch):
    from erpclaw_email import PLUGIN

    create_context = ssl.create_default_context
    monkeypatch.setattr("erpclaw_email.smtp.ssl.create_default_context",
                        lambda **kwargs: create_context(cadata=certificates[0]))
    return PLUGIN.slots["send_message"]


@pytest.mark.parametrize("implicit_tls", [False, True])
def test_success_one_session_mark_between_rcpt_and_data(smtp_server, certificates, implicit_tls):
    with smtp_server(implicit_tls=implicit_tls) as fake:
        settings = Settings(fake.port, "tls" if implicit_tls else "starttls")
        marks = []
        settings.row.update(from_address="changed@stark.test", from_name="Changed", reply_to="changed@stark.test")

        def mark():
            assert fake.commands[-1] == "RCPT" and not fake.data_seen
            settings.closed = True  # core closes its ReadCtx at the committed mark
            marks.append(True)
            return True

        result = call(fake, certificates, mark=mark, settings=settings)
        assert (result.stage, result.kind, result.code) == ("after_data", "accepted", 250)
        assert fake.connections == 1 and marks == [True]
        assert fake.commands == (["EHLO"] if implicit_tls else ["EHLO", "STARTTLS", "EHLO"]) + [
            "AUTH", "MAIL", "RCPT", "DATA", "QUIT"]
        assert all(tls for cmd, tls in fake.tls_at_command if cmd in {"AUTH", "MAIL", "RCPT", "DATA"})
        assert settings.reads == [("email_sender", {})]
        assert_envelope(fake.envelope, MSG.from_address, MSG.recipient)
        message = BytesParser(policy=policy.default).parsebytes(fake.messages[0])
        assert str(message["To"]) == MSG.recipient
        assert str(message["From"]) == "Stark Books <books@stark.test>"
        assert str(message["Reply-To"]) == MSG.reply_to
        assert str(message["Subject"]) == MSG.subject
        assert str(message["Message-ID"]) == MSG.message_id and message["Date"]
        assert message.get_content_type() == "text/plain" and not message.is_multipart()
        assert message.get_content().replace("\r\n", "\n") == MSG.body


@pytest.mark.parametrize("recipient,normalized", [
    ("Accounts+Invoices@bücher.example", "Accounts+Invoices@xn--bcher-kva.example"),
    ("Accounts+Invoices@EXAMPLE.test", "Accounts+Invoices@EXAMPLE.test"),
])
def test_recipient_header_matches_ascii_envelope_without_defects(
        smtp_server, certificates, recipient, normalized):
    with smtp_server() as fake:
        result = call(fake, certificates, msg=replace(MSG, recipient=recipient))
    assert result.kind == "accepted"
    assert_envelope(fake.envelope, MSG.from_address, normalized)
    (wire,) = fake.messages
    assert wire.split(b"\r\n", 1)[0] == b"To: " + normalized.encode("ascii")
    header = BytesParser(policy=policy.default).parsebytes(wire)["To"]
    assert header.defects == ()
    (address,) = header.addresses
    assert address.addr_spec == normalized
    assert address.display_name == ""


def test_ascii_recipient_message_bytes_unchanged(smtp_server, certificates, monkeypatch):
    monkeypatch.setattr("erpclaw_email.smtp.format_datetime", lambda value: "Sun, 08 Nov 2026 12:00:00 +0000")
    with smtp_server() as fake:
        result = call(fake, certificates, msg=replace(MSG, recipient="Accounts+Invoices@EXAMPLE.test"))
    assert result.kind == "accepted"
    assert fake.messages == [
        b"To: Accounts+Invoices@EXAMPLE.test\r\n"
        b"From: Stark Books <books@stark.test>\r\n"
        b"Reply-To: reply@stark.test\r\n"
        b"Subject: Invoice INV-2026-00007 =?utf-8?b?4oCU?= approved\r\n"
        b"Message-ID: <6d1f3c2e-0000-4000-8000-000000000001@stark.test>\r\n"
        b"Date: Sun, 08 Nov 2026 12:00:00 +0000\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
        b"Approved plain text.\r\n.A dot at the start.\r\nThank you.\r\n"
    ]


@pytest.mark.parametrize("recipient", ["Accounts+Invoices@bücher.example", "Accounts+Invoices@EXAMPLE.test"])
def test_approved_recipient_is_sent_in_normalized_form(
        engine, smtp_server, trusted_slot, monkeypatch, recipient):
    from sqlalchemy import select

    from erpclaw_core import approvals, models as m, plugins
    from erpclaw_email import smtp

    with engine.begin() as conn:
        plugins.disable_plugin(conn, "email_folder")
        conn.execute(m.party.update().where(m.party.c.name == "Wayne Enterprises").values(email=recipient))
    plugins.install_plugin(engine, "email")
    monkeypatch.setattr(smtp, "company_password", lambda cid: "synthetic-smtp-secret")
    with smtp_server(host="localhost") as fake:
        settings_card = approvals.propose(engine, plugins.REGISTRY.contracts["update_email_sender"], dict(
            from_address=MSG.from_address, display_name=MSG.from_name, reply_to=MSG.reply_to,
            smtp_host="localhost", port=fake.port, security="starttls", username="books"))
        assert approvals.approve(engine, settings_card.approval_id, settings_card.digest).status == "committed"
        card = approvals.propose(engine, plugins.REGISTRY.contracts["create_invoice"], dict(
            customer="Wayne Enterprises", items=[dict(item="Circuit Board", quantity="2")], email=True))
        approved = dict(card.card["lines"])["Email to"]
        assert approved == recipient
        assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
        (result,) = outbox.deliver_pending(engine)
        assert result["status"] == "accepted"
    local, domain = approved.rsplit("@", 1)
    normalized = local + "@" + domain.encode("idna").decode("ascii")
    assert_envelope(fake.envelope, MSG.from_address, normalized)
    (wire,) = fake.messages
    header = BytesParser(policy=policy.default).parsebytes(wire)["To"]
    assert header.defects == ()
    (address,) = header.addresses
    assert address.addr_spec == normalized
    assert address.display_name == ""
    with engine.connect() as conn:
        row = conn.execute(select(m.outbox).where(m.outbox.c.operation_key == card.operation_key)).mappings().one()
    assert row["recipient"] == approved and row["status"] == "accepted"


@pytest.mark.parametrize("mechanism", ["PLAIN", "LOGIN", "CRAM-MD5"])
@pytest.mark.parametrize("username,secret", [
    ("books", "synthetic-ascii-password"),
    ("books", "pässwörd-✓"),
    ("böoks-✓", "synthetic-ascii-password"),
    ("böoks-✓", "pässwörd-✓"),
])
@pytest.mark.parametrize("code", [235, 535])
def test_auth_uses_utf8_and_only_server_refusal_means_password_refused(
        smtp_server, certificates, caplog, capsys, mechanism, username, secret, code):
    caplog.set_level(logging.DEBUG)
    expected = (f"\0{username}\0{secret}" if mechanism == "PLAIN" else f"{username}\0{secret}").encode("utf-8")
    # ASCII credentials must retain smtplib's exact original wire bytes.
    if username.isascii() and secret.isascii():
        assert expected == (f"\0{username}\0{secret}" if mechanism == "PLAIN"
                            else f"{username}\0{secret}").encode("ascii")
    if mechanism == "CRAM-MD5":
        digest = hmac.new(secret.encode("utf-8"), b"local SMTP challenge", "md5").hexdigest()
        expected = username.encode("utf-8") + b" " + digest.encode("ascii")
    with smtp_server(auth_mechanisms=mechanism, auth_check=lambda payload: payload == expected,
                     replies={"AUTH": (code, "authenticated" if code == 235 else f"refused {secret}")}) as fake:
        settings = Settings(fake.port)
        settings.row["username"] = username
        mark = Mock(return_value=True)
        result = call(fake, certificates, password=lambda: secret, settings=settings, mark=mark)
        assert fake.auth_seen
        if code == 235:
            assert (result.stage, result.kind, result.code) == ("after_data", "accepted", 250)
            assert len(fake.messages) == 1
            mark.assert_called_once_with()
        else:
            assert (result.stage, result.kind, result.code, result.reason) == (
                "before_data", "permanent", 535, "password_refused")
            assert outbox.REASONS[result.reason] == "the mail account's password was refused"
            assert not fake.data_seen and not fake.messages
            mark.assert_not_called()
    captured = capsys.readouterr()
    observed = repr(result) + caplog.text + captured.out + captured.err
    for value in (secret, base64.b64encode(secret.encode("utf-8")).decode("ascii")):
        assert value not in observed


def test_local_auth_encoding_failure_is_sanitized_and_is_not_password_refusal(
        smtp_server, certificates, caplog, capsys):
    # A lone surrogate cannot be encoded as UTF-8; it is a local failure,
    # never evidence that the server refused the account's password.
    secret = "synthetic-" + secrets.token_hex(16) + "\ud800"
    caplog.set_level(logging.DEBUG)
    with smtp_server() as fake:
        mark = Mock(return_value=True)
        result = call(fake, certificates, password=lambda: secret, mark=mark)
        assert result.stage == "before_data" and result.code is None
        assert result.reason != "password_refused"
        assert not fake.auth_seen and not fake.data_seen and not fake.messages
        mark.assert_not_called()
    captured = capsys.readouterr()
    observed = repr(result) + caplog.text + captured.out + captured.err
    assert secret not in observed and repr(secret)[1:-1] not in observed


@pytest.mark.parametrize("failure,reason", [
    ("untrusted", "certificate_unverified"), ("wrong_host", "certificate_unverified"),
    ("no_starttls", "tls_unavailable"), ("untrusted_tls", "certificate_unverified"),
    ("wrong_host_tls", "certificate_unverified"),
])
def test_tls_failures_never_authenticate_or_send_data(smtp_server, certificates, failure, reason):
    with smtp_server(host="elsewhere.invalid" if failure.startswith("wrong_host") else "127.0.0.1",
                     implicit_tls=failure.endswith("_tls"), starttls=failure != "no_starttls") as fake:
        mark = Mock(return_value=True)
        password = Mock(return_value="never-exposed")
        result = call(fake, certificates, mark=mark, password=password, trust=not failure.startswith("untrusted"))
        assert (result.stage, result.kind, result.reason) == ("before_data", "permanent", reason)
        assert not fake.auth_seen and not fake.data_seen and not fake.messages
        mark.assert_not_called()
        password.assert_not_called()


@pytest.mark.parametrize("command,code,kind,reason", [
    ("RCPT", 450, "temporary", "recipient_refused"), ("RCPT", 550, "permanent", "recipient_refused"),
    ("MAIL", 451, "temporary", "recipient_refused"), ("MAIL", 553, "permanent", "recipient_refused"),
    ("AUTH", 454, "temporary", "password_refused"), ("AUTH", 535, "permanent", "password_refused"),
])
def test_pre_data_refusals(smtp_server, certificates, command, code, kind, reason):
    with smtp_server(replies={command: (code, "first line\nsecond line")}) as fake:
        mark = Mock(return_value=True)
        result = call(fake, certificates, mark=mark)
        assert (result.stage, result.kind, result.code, result.reason) == ("before_data", kind, code, reason)
        assert "second line" not in result.text
        assert not fake.data_seen
        mark.assert_not_called()


@pytest.mark.parametrize("code,kind", [(450, "temporary"), (550, "permanent")])
@pytest.mark.parametrize("at_command", [False, True])
def test_explicit_data_refusal(smtp_server, certificates, code, kind, at_command):
    with smtp_server(replies={"DATA": (code, "message refused")},
                     data_command_code=code if at_command else 354) as fake:
        mark = Mock(return_value=True)
        result = call(fake, certificates, mark=mark)
        assert (result.stage, result.kind, result.code, result.reason) == (
            "after_data", kind, code, "message_refused")
        mark.assert_called_once_with()


@pytest.mark.parametrize("mark_result", [False, None, 1, "yes"])
def test_lost_ownership_quits_without_data(smtp_server, certificates, mark_result):
    with smtp_server() as fake:
        mark = Mock(return_value=mark_result)
        result = call(fake, certificates, mark=mark)
        assert (result.stage, result.kind) == ("before_data", "lost_ownership")
        assert fake.commands[-2:] == ["RCPT", "QUIT"] and not fake.data_seen
        mark.assert_called_once_with()


def test_drop_after_data_has_no_answer(smtp_server, certificates):
    with smtp_server(drop_after_data=True) as fake:
        result = call(fake, certificates)
        assert (result.stage, result.kind, result.code) == ("after_data", "no_answer", None)
        assert len(fake.messages) == 1  # core maps no_answer to unknown, never automatic retry


@pytest.mark.parametrize("missing", [None, ""])
def test_missing_password_sends_no_auth_or_data(smtp_server, certificates, missing):
    with smtp_server() as fake:
        result = call(fake, certificates, password=lambda: missing)
        assert (result.stage, result.kind, result.reason) == ("before_data", "permanent", "password_missing")
        assert not fake.auth_seen and not fake.data_seen


@pytest.mark.parametrize("company_id", [MSG.company_id.upper(), MSG.company_id.replace("-", ""),
                                      MSG.company_id.replace("-", "_"), "{" + MSG.company_id + "}",
                                      "urn:uuid:" + MSG.company_id, "not-a-uuid", None, 123])
def test_noncanonical_company_is_refused_before_any_lookup(monkeypatch, company_id):
    lookup = Mock(side_effect=AssertionError("no environment lookup allowed"))
    runner = Mock(side_effect=AssertionError("no keychain lookup allowed"))
    password = Mock(side_effect=AssertionError("no accessor lookup allowed"))
    settings = Mock()
    mark = Mock()
    with monkeypatch.context() as patches:
        patches.setattr("erpclaw_email.smtp.os.environ", Mock(get=lookup))
        patches.setattr("erpclaw_email.smtp.sys.platform", "darwin")
        assert company_password(company_id, _runner=runner) is None
        result = send_message(settings, replace(MSG, company_id=company_id), mark, password=password)
    assert (result.stage, result.kind, result.reason) == ("before_data", "permanent", "password_missing")
    for operation in (lookup, runner, password, settings.one, mark):
        operation.assert_not_called()


@pytest.mark.parametrize("implicit_tls", [False, True])
@pytest.mark.parametrize("company_id", [MSG.company_id, "c0ffee00-0000-4000-8000-000000000002"])
def test_registered_slot_reads_distinct_company_env_secrets_at_send_time(
        smtp_server, trusted_slot, monkeypatch, company_id, implicit_tls):
    import inspect

    assert list(inspect.signature(trusted_slot).parameters) == ["settings", "msg", "mark_data"]
    ids = [MSG.company_id, "c0ffee00-0000-4000-8000-000000000002"]
    passwords = {cid: "synthetic-" + secrets.token_hex(16) for cid in ids}
    runner = Mock(side_effect=AssertionError("environment must take precedence over keychain"))
    monkeypatch.setattr("erpclaw_email.smtp.sys.platform", "darwin")
    monkeypatch.setattr("erpclaw_email.smtp.subprocess.run", runner)
    with smtp_server(implicit_tls=implicit_tls,
                     auth_check=lambda payload: payload == f"\0books\0{passwords[company_id]}".encode()) as fake:
        # Set after loading the plugin: every send must read the current environment.
        for cid, secret in passwords.items():
            monkeypatch.setenv("ERPCLAW_SMTP_PASSWORD_" + cid.replace("-", "_"), secret)
        result = trusted_slot(Settings(fake.port, "tls" if implicit_tls else "starttls"),
                              replace(MSG, company_id=company_id), lambda: True)
        assert result.kind == "accepted" and fake.auth_seen and fake.data_seen
    runner.assert_not_called()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("lookup_result", ["missing", "empty", "timeout", "error"])
def test_registered_slot_missing_password_sends_no_auth_or_data(
        smtp_server, trusted_slot, monkeypatch, platform, lookup_result, caplog, capsys):
    secret = "synthetic-" + secrets.token_hex(16)
    monkeypatch.delenv("ERPCLAW_SMTP_PASSWORD_" + MSG.company_id.replace("-", "_"), raising=False)
    monkeypatch.setattr("erpclaw_email.smtp.sys.platform", platform)
    runner = Mock(return_value=subprocess.CompletedProcess([], 1 if lookup_result == "missing" else 0,
                                                          stdout=secret if lookup_result == "missing" else ""))
    if lookup_result == "timeout":
        runner.side_effect = subprocess.TimeoutExpired("security", TIMEOUT, output=secret, stderr=secret)
    elif lookup_result == "error":
        runner.side_effect = OSError(secret)
    monkeypatch.setattr("erpclaw_email.smtp.subprocess.run", runner)
    mark = Mock(return_value=True)
    with smtp_server() as fake:
        result = trusted_slot(Settings(fake.port), MSG, mark)
        assert (result.stage, result.kind, result.reason) == ("before_data", "permanent", "password_missing")
        assert outbox.REASONS[result.reason] == "the mail account's password is not set up"
        assert not fake.auth_seen and not fake.data_seen
        mark.assert_not_called()
    assert runner.call_count == (1 if platform == "darwin" else 0)
    captured = capsys.readouterr()
    assert secret not in repr(result) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("company_id", [MSG.company_id, "c0ffee00-0000-4000-8000-000000000002"])
def test_keychain_uses_company_service_and_stdout_only(monkeypatch, company_id):
    secret = "synthetic-" + secrets.token_hex(16)
    monkeypatch.delenv("ERPCLAW_SMTP_PASSWORD_" + company_id.replace("-", "_"), raising=False)
    monkeypatch.setattr("erpclaw_email.smtp.sys.platform", "darwin")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=secret + "\n", stderr="ignored"))
    assert company_password(company_id, _runner=runner) == secret
    runner.assert_called_once_with(
        ["security", "find-generic-password", "-s", "erpclaw-v3-smtp-" + company_id, "-w"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT, check=False)
    assert secret not in repr(runner.call_args_list)


@pytest.mark.parametrize("where", ["accessor", "AUTH", "RCPT", "DATA", "mark"])
@pytest.mark.parametrize("non_ascii", [False, True])
def test_secret_never_reaches_outcome_logs_exceptions_or_subprocess(
        smtp_server, certificates, monkeypatch, caplog, capsys, where, non_ascii):
    secret = ("pässwörd-✓-" if non_ascii else "synthetic-") + secrets.token_hex(16)
    encoded = base64.b64encode(secret.encode()).decode()
    plain = base64.b64encode(f"\0books\0{secret}".encode()).decode()
    reflected = f"refused {secret} {encoded} {plain}"
    subprocess_call = Mock(side_effect=AssertionError("connector must not spawn a process"))
    monkeypatch.setattr("subprocess.Popen", subprocess_call)
    caplog.set_level(logging.DEBUG)

    def fail():
        raise RuntimeError(reflected)

    with smtp_server(replies={where: (535 if where == "AUTH" else 550, reflected)}) as fake:
        settings = Settings(fake.port)
        try:
            result = call(fake, certificates, password=fail if where == "accessor" else lambda: secret,
                          mark=fail if where == "mark" else None, settings=settings)
            observed = repr(result)
        except Exception as exc:
            observed = str(exc) + repr(exc)
            pytest.fail("connector propagated an exception")
        captured = capsys.readouterr()
        observed += caplog.text + captured.out + captured.err + repr(settings.row) + repr(subprocess_call.call_args_list)
        for value in (secret, encoded, plain):
            assert value not in observed
        subprocess_call.assert_not_called()


def test_attachments_are_refused_before_connecting(smtp_server, certificates):
    with smtp_server() as fake:
        attachment = OutboundAttachment("invoice.pdf", "application/pdf", "0" * 64, b"not a PDF")
        result = call(fake, certificates, msg=replace(MSG, attachments=(attachment,)))
        assert (result.stage, result.kind) == ("before_data", "permanent")
        assert fake.connections == 0


def test_plaintext_security_is_refused_before_connecting(smtp_server, certificates):
    with smtp_server() as fake:
        settings = Settings(fake.port, "plain")
        result = call(fake, certificates, settings=settings)
        assert (result.stage, result.kind, result.reason) == ("before_data", "permanent", "tls_unavailable")
        assert fake.connections == 0


@pytest.mark.parametrize("source", ["env", "keychain"])
@pytest.mark.parametrize("non_ascii", [False, True])
@pytest.mark.parametrize("command,code,status", [("AUTH", 535, "failed"), ("RCPT", 550, "failed"),
                                                 ("DATA", 250, "accepted"),
                                                 ("drop", None, "unknown")])
def test_real_mark_and_result_keep_rows_and_attempts_free_of_secrets(
        engine, smtp_server, trusted_slot, monkeypatch, caplog, capsys, command, code, status, source, non_ascii):
    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx

    secret = ("pässwörd-✓-" if non_ascii else "synthetic-") + secrets.token_hex(16)
    encoded = base64.b64encode(secret.encode()).decode()
    plain = base64.b64encode(f"\0books\0{secret}".encode()).decode()
    subprocess_call = Mock(side_effect=AssertionError("tests must never run security"))
    monkeypatch.setattr("subprocess.Popen", subprocess_call)
    caplog.set_level(logging.DEBUG)
    with engine.begin() as conn:
        oid = outbox.enqueue(company_ctx(conn), "send_message", MSG.recipient, MSG.subject, MSG.body, None,
                             sender=outbox.SenderSnapshot(MSG.from_address, MSG.from_name, MSG.reply_to, None))
    with engine.connect() as conn:
        queued = dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())
    msg = replace(MSG, id=oid, company_id=queued["company_id"], message_id=queued["message_id"])
    env_name = "ERPCLAW_SMTP_PASSWORD_" + msg.company_id.replace("-", "_")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=secret + "\n"))
    monkeypatch.setattr("erpclaw_email.smtp.sys.platform", "darwin")
    if source == "env":
        monkeypatch.setenv(env_name, secret)
    else:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr("erpclaw_email.smtp.company_password", lambda cid: company_password(cid, _runner=runner))
    aid = outbox.claim(engine, oid)
    assert aid is not None
    reflected = f"response {secret} {encoded} {plain}"
    replies = {} if command == "drop" else {command: (code, reflected)}
    with smtp_server(replies=replies, drop_after_data=command == "drop",
                     auth_check=lambda payload: payload == f"\0books\0{secret}".encode()) as fake:
        def mark():
            assert fake.commands[-1] == "RCPT" and not fake.data_seen
            assert outbox.mark_data(engine, oid, aid)
            # A new connection sees the mark: it really committed before DATA.
            with engine.connect() as conn:
                assert conn.execute(select(m.outbox_attempt.c.data_at).where(
                    m.outbox_attempt.c.id == aid)).scalar_one() is not None
            return True

        result = trusted_slot(Settings(fake.port), msg, mark)
        assert fake.auth_seen
    outbox.record_result(engine, oid, aid, result)
    with engine.connect() as conn:
        row = dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())
        attempt = dict(conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.id == aid)).mappings().one())
    assert row["status"] == status
    assert attempt["result"] == {"failed": "permanent", "accepted": "accepted", "unknown": "no_answer"}[status]
    assert attempt["code"] == code
    assert (attempt["data_at"] is not None) == (command not in {"AUTH", "RCPT"})
    captured = capsys.readouterr()
    persisted = (repr((queued, row, attempt, result, runner.call_args_list)) + caplog.text
                 + captured.out + captured.err + outbox.owner_text(row))
    for value in (secret, encoded, plain):
        assert value not in persisted
    assert "[redacted]" in attempt["text"] or command == "drop"
    assert "Delivery to the inbox is not confirmed" in outbox.owner_text(row) or status != "accepted"
    subprocess_call.assert_not_called()
    assert runner.call_count == (1 if source == "keychain" else 0)


@pytest.mark.parametrize("security", ["starttls", "tls"])
@pytest.mark.parametrize("failure,kind,status,reason", [
    (ssl.SSLCertVerificationError("untrusted or wrong hostname"), "permanent", "failed", "certificate_unverified"),
    (ssl.SSLError("protocol handshake failure"), "temporary", "queued", "unreachable"),
    (ConnectionResetError("reset during handshake"), "temporary", "queued", "unreachable"),
    (TimeoutError("timeout during handshake"), "temporary", "queued", "unreachable"),
])
def test_handshake_failures_record_permanent_certificate_error_or_temporary_backoff(
        engine, monkeypatch, security, failure, kind, status, reason):
    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx

    with engine.begin() as conn:
        oid = outbox.enqueue(company_ctx(conn), "send_message", MSG.recipient, MSG.subject, MSG.body, None,
                             sender=outbox.SenderSnapshot(MSG.from_address, MSG.from_name, MSG.reply_to, None))
    aid = outbox.claim(engine, oid)
    assert aid is not None
    client = Mock()
    client.ehlo.return_value = (250, b"OK")
    client.has_extn.return_value = True
    client.starttls.side_effect = failure
    constructor = Mock(return_value=client)
    if security == "tls":
        constructor.side_effect = failure
    monkeypatch.setattr("erpclaw_email.smtp.smtplib.SMTP" if security == "starttls"
                        else "erpclaw_email.smtp.smtplib.SMTP_SSL", constructor)
    password = Mock(return_value="never-exposed")
    mark = Mock(return_value=True)
    result = send_message(Settings(1, security), MSG, mark, password=password)
    assert (result.stage, result.kind, result.reason) == ("before_data", kind, reason)
    outbox.record_result(engine, oid, aid, result)
    with engine.connect() as conn:
        row = dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())
        attempt = dict(conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.id == aid)).mappings().one())
    assert row["status"] == status
    assert attempt["data_at"] is None and attempt["result"] == kind and attempt["code"] is None
    if status == "queued":
        assert row["tries"] == 1
        assert row["next_try_at"] == row["first_failed_at"] + outbox.BACKOFF[0]
    else:
        assert row["detail"] == "the mail server's certificate could not be verified"
    for command in (client.login, client.mail, client.rcpt, client.data, password):
        command.assert_not_called()
    mark.assert_not_called()

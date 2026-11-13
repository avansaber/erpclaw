"""One verified SMTP session, fenced by core's committed data mark.

Connection settings use ReadCtx.one('email_sender'): smtp_host, port, security,
username. No secret belongs in that table.
Passwords are resolved at send time from the company's environment or core's OS store.
"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import os
import smtplib
import ssl
import subprocess
import sys
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import Callable
from uuid import UUID

from erpclaw_core.outbox import OutboundMessage, ReadCtx, SendOutcome
from erpclaw_core.secrets import read_secret


TIMEOUT = 20  # comfortably below core's five-minute claim lease


def _canonical_company_id(company_id: str) -> bool:
    try:
        return isinstance(company_id, str) and str(UUID(company_id)) == company_id
    except ValueError:
        return False


def company_password(company_id: str, *, _runner=None) -> str | None:
    """Resolve one company's secret without exposing subprocess output or errors."""
    if not _canonical_company_id(company_id):
        return None
    secret = os.environ.get("ERPCLAW_SMTP_PASSWORD_" + company_id.replace("-", "_"))
    if secret is not None:
        return secret
    return read_secret("smtp_password", company_id, consumer="email", _runner=_runner)


def _text(reply: bytes | str, password: str, username: str) -> str:
    """First reply line only, with raw and SMTP AUTH encodings of the secret removed."""
    text = reply.decode("utf-8", errors="replace") if isinstance(reply, bytes) else reply
    if password:
        tokens = [password]
        for value in (password, f"\0{username}\0{password}", f"{username}\0{username}\0{password}"):
            try:
                tokens.append(base64.b64encode(value.encode("utf-8")).decode("ascii"))
            except UnicodeError:
                # Invalid local Unicode must not make error sanitization fail.
                pass
        for token in sorted(tokens, key=len, reverse=True):
            text = text.replace(token, "[redacted]")
    return (text.splitlines() or [""])[0][:200]


def _login_utf8(client: smtplib.SMTP, username: str, password: str) -> None:
    """Use smtplib's mechanism preference and fallback, with UTF-8 AUTH bytes.

    SMTP.auth/login encode callbacks as ASCII. Keep their initial responses,
    challenge limit and success codes, but encode credentials before base64.
    """
    user, secret = username.encode("utf-8"), password.encode("utf-8")
    if not client.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError("SMTP AUTH is not supported")
    advertised = client.esmtp_features["auth"].split()
    preferred = ["PLAIN", "LOGIN"]
    if getattr(smtplib, "_have_cram_md5_support", True):
        preferred.insert(0, "CRAM-MD5")
    mechanisms = [name for name in preferred if name in advertised]
    if not mechanisms:
        raise smtplib.SMTPNotSupportedError("No suitable SMTP AUTH mechanism")
    for mechanism in mechanisms:
        plain = b"\0" + user + b"\0" + secret
        initial = plain if mechanism == "PLAIN" else user
        arguments = mechanism
        if mechanism != "CRAM-MD5":
            arguments += " " + base64.b64encode(initial).decode("ascii")
        code, reply = client.docmd("AUTH", arguments)
        challenges = 0 if mechanism == "CRAM-MD5" else 1
        while code == 334:
            challenges += 1
            if mechanism == "CRAM-MD5":
                digest = hmac.new(secret, base64.decodebytes(reply), "md5").hexdigest()
                response = user + b" " + digest.encode("ascii")
            else:
                response = plain if mechanism == "PLAIN" else secret
            code, reply = client.docmd(base64.b64encode(response).decode("ascii"))
            if challenges > 5:
                raise smtplib.SMTPException("Too many SMTP AUTH challenges")
        if code in {235, 503}:
            return
        last_error = smtplib.SMTPAuthenticationError(code, reply)
    raise last_error


def _envelope(address: str) -> str:
    local, domain = address.rsplit("@", 1)
    return f"{local}@{domain.encode('idna').decode('ascii')}"


def _message(msg: OutboundMessage) -> bytes:
    mail = EmailMessage(policy=policy.SMTP)
    mail["To"] = _envelope(msg.recipient)
    mail["From"] = formataddr((msg.from_name or "", _envelope(msg.from_address)))
    if msg.reply_to:
        mail["Reply-To"] = _envelope(msg.reply_to)
    mail["Subject"] = msg.subject
    mail["Message-ID"] = msg.message_id
    mail["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
    mail.set_content(msg.body, cte="quoted-printable")
    return mail.as_bytes()


def send_message(settings: ReadCtx, msg: OutboundMessage, mark_data: Callable[[], bool], *,
                 password: Callable[[], str | None] | None = None, _test_ca: str | None = None) -> SendOutcome:
    """Send the approved text and headers; never build owner-facing explanations.

    password is a send-time secret accessor, not a row value. _test_ca is PEM
    trust material supplied only by local tests; no settings field can alter
    certificate or hostname verification. By default, resolve the company's secret.
    """
    client = None
    marked = False
    secret = ""
    username = ""
    reason = "unreachable"

    def outcome(kind: str, code: int | None = None, reply: bytes = b"", why: str | None = None) -> SendOutcome:
        return SendOutcome("after_data" if marked else "before_data", kind, code,
                           _text(reply, secret, username), reason=why)

    def refused(code: int, reply: bytes) -> SendOutcome:
        if 400 <= code < 600:
            return outcome("temporary" if code < 500 else "permanent", code, reply, reason)
        return outcome("no_answer" if marked else "permanent", why=reason)

    try:
        if msg.attachments:
            return outcome("permanent", why="message_refused")
        if not _canonical_company_id(msg.company_id):
            return outcome("permanent", why="password_missing")
        config = settings.one("email_sender")
        if config is None:
            return outcome("permanent", why="unreachable")
        security = config["security"]
        if security not in {"starttls", "tls"}:
            return outcome("permanent", why="tls_unavailable")
        host, port, username = config["smtp_host"], config["port"], config["username"]
        # Build before connecting: attachments, header errors and unsupported
        # envelopes must never consume a data mark or partially send a message.
        reason = "message_refused"
        payload = _message(msg)
        sender, recipient = _envelope(msg.from_address), _envelope(msg.recipient)
        sender.encode("ascii")
        recipient.encode("ascii")
        context = ssl.create_default_context(cadata=_test_ca)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        reason = "unreachable"
        # A fixed local EHLO name also avoids a host-name lookup by smtplib.
        if security == "tls":
            client = smtplib.SMTP_SSL(host, port, local_hostname="localhost", timeout=TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(host, port, local_hostname="localhost", timeout=TIMEOUT)
        code, reply = client.ehlo()
        if code != 250:
            return refused(code, reply)
        if security == "starttls":
            reason = "tls_unavailable"
            if not client.has_extn("starttls"):
                return outcome("permanent", why=reason)
            client.starttls(context=context)
            code, reply = client.ehlo()
            if code != 250:
                return refused(code, reply)
        # No AUTH or secret access can precede the verified TLS handshake.
        reason = "password_missing"
        try:
            secret = password() if password is not None else company_password(msg.company_id)
        except Exception:
            return outcome("permanent", why=reason)
        if not isinstance(secret, str) or not secret:
            secret = ""
            return outcome("permanent", why=reason)
        reason = "unreachable"
        try:
            _login_utf8(client, username, secret)
        except smtplib.SMTPAuthenticationError as exc:
            # Only a server AUTH reply can mean the password was refused.
            reason = "password_refused"
            return refused(exc.smtp_code, exc.smtp_error)
        reason = "recipient_refused"
        code, reply = client.mail(sender)
        if code != 250:
            return refused(code, reply)
        code, reply = client.rcpt(recipient)
        if code not in {250, 251}:
            return refused(code, reply)
        # A raised callback may have committed: conservatively treat it as
        # marked. A False return is the explicit fence that forbids DATA.
        marked = True
        if mark_data() is not True:
            marked = False
            return outcome("lost_ownership")
        reason = "message_refused"
        code, reply = client.data(payload)
        if code == 250:
            return outcome("accepted", code, reply)
        return refused(code, reply)
    except ssl.SSLCertVerificationError:
        return outcome("permanent", why="certificate_unverified")
    except ssl.SSLError:
        return outcome("no_answer" if marked else "temporary", why="unreachable")
    except smtplib.SMTPResponseException as exc:
        return refused(exc.smtp_code, exc.smtp_error)
    except (OSError, smtplib.SMTPServerDisconnected):
        return outcome("no_answer" if marked else "temporary", why="unreachable")
    except Exception:
        # Exceptions can contain passwords (including accessor failures and
        # reflected server text). Do not log, stringify or propagate them.
        return outcome("no_answer" if marked else "permanent", why=reason)
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass  # a QUIT failure cannot change the known DATA result
            finally:
                try:
                    client.close()
                except Exception:
                    pass

"""Measures, periods, email through the outbox."""
from datetime import date
from email.message import EmailMessage
from pathlib import Path
import re

import pytest
from sqlalchemy import select

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.core_contracts import GetMetric, get_metric
from erpclaw_core.outbox import SendOutcome, SenderSnapshot, deliver_pending
from erpclaw_core.periods import resolve
from erpclaw_core.plugins import REGISTRY, disable_plugin, install_plugin
from slot_support import replace_filler


@pytest.fixture
def email_delivery(engine, monkeypatch, tmp_path):
    """Capture the email transport's message locally, keeping the real outbox path."""
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    install_plugin(engine, "email")

    def message_sender(ctx):
        return SenderSnapshot("books@stark.example", "Stark Books", None, None)

    def send_message(settings, msg, mark_data):
        if not mark_data():
            return SendOutcome("before_data", "lost_ownership", None, "")
        message = EmailMessage()
        message["To"] = msg.recipient
        message["From"] = msg.from_address
        message["Subject"] = msg.subject
        message["Message-ID"] = msg.message_id
        message.set_content(msg.body)
        path = tmp_path / f"{msg.id}.eml"
        path.write_bytes(bytes(message))
        return SendOutcome("after_data", "accepted", 250, str(path))

    replace_filler(monkeypatch, "message_sender", message_sender)
    replace_filler(monkeypatch, "send_message", send_message)


def metric(engine, **kw):
    with engine.connect() as conn:
        return get_metric(company_ctx(conn), GetMetric(**kw))


def test_18_receivables_net_of_payments(engine):
    r = metric(engine, metric="receivables")
    assert r.data["value"] == "4870.00"
    assert "Pied Piper         3,170.00 USD" in r.text and "Acme" not in r.text


def test_19_no_rows_is_no_data_not_zero(engine):
    r = metric(engine, metric="sales", period="January 2026")
    assert r.data["value"] is None and "no data" in r.text and "0.00" not in r.text
    assert metric(engine, metric="sales", period="FY 2025").data["value"] == "8100.00"
    assert "won't guess" in metric(engine, metric="mrr").text


@pytest.mark.parametrize("phrase,start,end", [
    ("this month", date(2026, 10, 1), date(2026, 10, 26)),
    ("last month", date(2026, 9, 1), date(2026, 9, 30)),
    ("last 6 months", date(2026, 5, 1), date(2026, 10, 26)),
    ("FY 2025", date(2025, 1, 1), date(2025, 12, 31)),
    ("Q3 2026", date(2026, 7, 1), date(2026, 9, 30)),
    ("this year", date(2026, 1, 1), date(2026, 10, 26)),
    ("October 2026", date(2026, 10, 1), date(2026, 10, 31)),
    ("march", date(2026, 3, 1), date(2026, 3, 31)),
    ("last quarter", date(2026, 7, 1), date(2026, 9, 30)),
    ("calendar 2025", date(2025, 1, 1), date(2025, 12, 31)),
])
def test_20_period_phrases(phrase, start, end):
    p = resolve(phrase, date(2026, 10, 26))
    assert (p.start, p.end) == (start, end)


def test_20b_unknown_phrase_refused():
    with pytest.raises(Refusal):
        resolve("around easter", date(2026, 10, 26))
    p = resolve("FY 2025", date(2026, 10, 26), fy_start_month=4)
    assert (p.start, p.end) == (date(2024, 4, 1), date(2025, 3, 31))


def test_21_email_queued_then_handed_to_the_mail_server(engine, email_delivery):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Wayne Enterprises", "items": [{"item": "circuit boards", "quantity": 2}],
                              "email": True})
    assert ("Email to", "ap@wayne.example") in [tuple(x) for x in card.card["lines"]]
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert "90.00 USD" in out.text and "queued" in out.text
    with engine.connect() as conn:
        assert conn.execute(select(m.outbox.c.status)).scalar_one() == "queued"
    done = deliver_pending(engine)
    assert [d["status"] for d in done] == ["accepted"]  # the mail server took it; "delivered" is never claimed
    assert re.fullmatch(
        r"Handed to the mail server at [0-9]{2}:[0-9]{2} on [0-9]{2} [A-Z][a-z]{2} [0-9]{4}\. "
        r"Delivery to the inbox is not confirmed\.",
        done[0]["text"],
    )
    with engine.connect() as conn:
        path = conn.execute(select(m.outbox_attempt.c.text)).scalar_one()
    eml = Path(path).read_text()
    assert "To: ap@wayne.example" in eml and "Total: 90.00 USD" in eml


def test_18b_balance_never_claims_a_future_as_of_date(engine):
    # Today is pinned to 26 Oct 2026; "2026" runs to 31 Dec 2026.
    r = metric(engine, metric="receivables", period="2026")
    assert r.data["to"] == "2026-10-26"
    assert "as of 26 Oct 2026" in r.text and "as of 31 Dec 2026" not in r.text
    assert "runs to 31 Dec 2026" in r.text  # says why the date is today
    past = metric(engine, metric="receivables", period="last year")
    assert "as of 31 Dec 2025" in past.text and "runs to" not in past.text

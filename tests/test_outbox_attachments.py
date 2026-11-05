"""A message's attachments name stored files: enqueue takes only the caller's company's file whose sha256 the card
approved, under a name a header can carry; at send time each is read through the file store's own check, and a file
that cannot be read fails the attempt before any connection; a file a message names can never be retired."""
from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from erpclaw_core import files, models as m, outbox
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.outbox import Attachment, OutboundAttachment, deliver_pending, enqueue
from erpclaw_core.setup import create_company

from test_outbox_core import SENDER, row, smtp  # noqa: F401  (smtp: the fake connector fixture)

PDF = b"%PDF-1.7\n" + b"the approved invoice\n" * 30
SHA = hashlib.sha256(PDF).hexdigest()


def _store(engine, data=PDF, company=None, voucher_id="inv-0001") -> str:
    with engine.begin() as conn:
        return files.put(company_ctx(conn, company_name=company), data, "application/pdf",
                         ("sales_invoice", voucher_id))


def _queue(engine, attachments, company=None) -> str:
    with engine.begin() as conn:
        return enqueue(company_ctx(conn, company_name=company), "deliver_message", "ar@acme.example",
                       "Invoice INV-1", "Please find it attached.", None, sender=SENDER, attachments=attachments)


def _attachments(engine, oid) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(m.outbox_attachment).where(
            m.outbox_attachment.c.outbox_id == oid)).mappings()]


def _sent(smtp, monkeypatch) -> list:
    """Every message handed to the connector (the slot holds the fake server's method, bound at load)."""
    seen = []
    real = outbox._message

    def built(r, attachments=()):
        msg = real(r, attachments)
        seen.append(msg)
        return msg

    monkeypatch.setattr(outbox, "_message", built)
    return seen


# --- at enqueue ------------------------------------------------------------------------------------------------------

def test_an_approved_file_is_queued_with_its_name_type_and_sha256(engine):
    fid = _store(engine)
    oid = _queue(engine, (Attachment(fid, "INV-1.pdf", SHA),))
    [a] = _attachments(engine, oid)
    assert (a["file_id"], a["name"], a["media_type"], a["sha256"]) == (fid, "INV-1.pdf", "application/pdf", SHA)


def test_another_companys_file_id_is_refused_at_enqueue(engine):
    create_company(engine, "Beta Traders")
    theirs = _store(engine, company="Beta Traders")
    with pytest.raises(Refusal) as e:
        _queue(engine, (Attachment(theirs, "INV-1.pdf", SHA),), company=_first_company(engine))
    assert e.value.code == "attachment_not_found"


def _first_company(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(select(m.company.c.name).order_by(m.company.c.created_at)).scalars().first()


def test_a_sha256_other_than_the_approved_one_is_refused_at_enqueue(engine):
    fid = _store(engine)
    with pytest.raises(Refusal) as e:
        _queue(engine, (Attachment(fid, "INV-1.pdf", "0" * 64),))
    assert e.value.code == "attachment_not_approved"


@pytest.mark.parametrize("name", ["INV-1\r\n.pdf", "INV\n1.pdf", "INV-1\x00.pdf", "INV\x7f.pdf", "../INV-1.pdf",
                                  "a/b.pdf", "a\\b.pdf", "", "x" * 201])
def test_a_name_with_a_control_character_or_a_separator_or_out_of_length_is_refused(engine, name):
    fid = _store(engine)
    with pytest.raises(Refusal) as e:
        _queue(engine, (Attachment(fid, name, SHA),))
    assert e.value.code == "attachment_name"


def test_a_name_beyond_ascii_is_accepted_at_enqueue(engine):
    fid = _store(engine)
    oid = _queue(engine, (Attachment(fid, "Rechnung-Müller.pdf", SHA),))
    assert _attachments(engine, oid)[0]["name"] == "Rechnung-Müller.pdf"


def test_a_refused_attachment_queues_nothing(engine):
    fid = _store(engine)
    with pytest.raises(Refusal):
        _queue(engine, (Attachment(fid, "ok.pdf", SHA), Attachment(fid, "bad/name.pdf", SHA)))
    with engine.connect() as conn:
        assert conn.execute(select(m.outbox.c.id)).first() is None


def test_the_database_refuses_an_attachment_row_without_a_stored_file(engine):
    oid = _queue(engine, ())
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(m.outbox_attachment.insert().values(id="a" * 36, outbox_id=oid, name="x.pdf",
                                                         media_type="application/pdf", sha256=SHA, file_id=None))


# --- at send time ----------------------------------------------------------------------------------------------------

def test_the_connector_gets_each_attachment_as_name_type_sha256_and_bytes(engine, smtp, monkeypatch):
    fid = _store(engine)
    oid = _queue(engine, (Attachment(fid, "INV-1.pdf", SHA),))
    seen = _sent(smtp, monkeypatch)
    deliver_pending(engine)
    assert row(engine, oid)["status"] == "accepted"
    assert seen[0].attachments == (OutboundAttachment(name="INV-1.pdf", media_type="application/pdf", sha256=SHA,
                                                      data=PDF),)
    assert list(OutboundAttachment.__dataclass_fields__) == ["name", "media_type", "sha256", "data"]


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_a_file_altered_or_lost_after_enqueue_fails_the_attempt_before_any_connection(engine, smtp, monkeypatch,
                                                                                     damage):
    fid = _store(engine)
    oid = _queue(engine, (Attachment(fid, "INV-1.pdf", SHA),))
    with engine.connect() as conn:
        path = files.file_path(files.root_dir(conn), company_ctx(conn).company_id, SHA)
    if damage == "changed":
        path.write_bytes(PDF.replace(b"approved", b"altered!"))
    else:
        path.unlink()
    seen = _sent(smtp, monkeypatch)
    deliver_pending(engine)
    r = row(engine, oid)
    assert seen == [] and smtp.events == []  # the connector was never called: nothing connected
    assert r["status"] == "failed" and r["detail"] == outbox.REASONS["attachment_unavailable"]
    assert r["detail"] == "the attached file could not be read"


# --- retirement ------------------------------------------------------------------------------------------------------

def test_a_file_a_message_names_cannot_be_removed_even_past_its_date(engine):
    fid = _store(engine)
    with engine.connect() as conn:
        r = dict(conn.execute(select(m.file_object).where(m.file_object.c.id == fid)).mappings().one())
    dated = {**r, "id": fid[:-4] + "beef", "voucher_id": "inv-dated", "retention_until": dt.date(2000, 1, 1)}
    with engine.begin() as conn:
        conn.execute(m.file_object.insert().values(dated))
    _queue(engine, (Attachment(dated["id"], "old.pdf", SHA),))
    with engine.begin() as conn, pytest.raises(DBAPIError, match="foreign key"):
        conn.execute(m.file_object.delete().where(m.file_object.c.id == dated["id"]))

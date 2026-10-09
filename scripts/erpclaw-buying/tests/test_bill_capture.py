"""Local extraction is untrusted; separately reviewed fields create only drafts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import pytest

from buying_helpers import build_buying_env, call_action, init_all_tables, load_db_query, ns, seed_company
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


BUYING = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


def png_bill():
    """A synthetic bitmap made here, without a font or image-library dependency."""
    glyphs = {
        "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
        "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
        "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
        "0": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
        ".": ["00000", "00000", "00000", "00000", "00000", "00100", "00100"],
        " ": ["00000"] * 7,
    }
    scale, margin = 8, 32
    label = "BILL 500.00"
    width, height = len(label) * 6 * scale + margin * 2, 7 * scale + margin * 2
    pixels = [bytearray([255] * width) for _ in range(height)]
    for index, char in enumerate(label):
        for y, row in enumerate(glyphs[char]):
            for x, value in enumerate(row):
                if value == "1":
                    for dy in range(scale):
                        start = margin + index * 6 * scale + x * scale
                        pixels[margin + y * scale + dy][start:start + scale] = bytes(scale)
    raw = b"".join(b"\0" + bytes(row) for row in pixels)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def pdf_bill():
    stream = b"BT /F1 24 Tf 72 720 Td (SYNTHETIC BILL 500.00) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data, offsets = b"%PDF-1.4\n", [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"


@pytest.fixture
def capture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", str(path))
    init_all_tables(str(path))
    conn = get_connection(str(path))
    env = build_buying_env(conn)
    image = tmp_path / "synthetic.png"
    image.write_bytes(png_bill())
    bill = {"supplier_id": env["supplier"], "company_id": env["company_id"],
            "posting_date": "2026-06-20", "due_date": "2026-07-20",
            "items": [{"item_id": env["item1"], "qty": "3", "rate": "10.01"}]}
    args = ns(company_id=env["company_id"], capture_file=str(image),
              capture_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
              bill_json=json.dumps(bill))
    try:
        yield conn, home, image, bill, args
    finally:
        conn.close()


def snapshot(conn):
    result = {}
    for name in ("purchase_invoice", "purchase_invoice_item", "gl_entry",
                 "stock_ledger_entry", "payment_ledger_entry", "audit_log"):
        table = Table(name)
        result[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return result


@pytest.mark.parametrize("kind,tool", [("png", "tesseract"), ("pdf", "pdftotext")])
def test_real_local_capture_extracts_synthetic_bill_without_writes(capture, kind, tool):
    if shutil.which(tool, path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin") is None:
        pytest.skip(f"Optional local {tool} is not installed")
    conn, _, image, bill, args = capture
    if kind == "pdf":
        image.write_bytes(pdf_bill())
    before = snapshot(conn)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result["status"] == "ok", result
    assert "BILL" in result["text"] and "500" in result["text"]
    assert result["capture_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert result["format"] == kind and result["tool"] == tool
    assert result["review_required"] is True and result["untrusted"] is True
    assert result["draft_created"] is False and str(image) not in json.dumps(result)
    assert snapshot(conn) == before


def test_reviewed_draft_has_exact_amounts_and_capture_provenance(capture):
    conn, home, image, bill, args = capture
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result["status"] == "ok" and result["grand_total"] == "30.03", result
    rows = snapshot(conn)
    assert rows["purchase_invoice"][0]["status"] == "draft"
    assert rows["purchase_invoice"][0]["grand_total"] == "30.03"
    assert rows["purchase_invoice"][0]["currency"] == "USD"
    line = rows["purchase_invoice_item"][0]
    assert (line["quantity"], line["rate"], line["amount"]) == ("3.00", "10.01", "30.03")
    assert rows["gl_entry"] == rows["stock_ledger_entry"] == rows["payment_ledger_entry"] == []
    audit = json.loads(rows["audit_log"][-1]["new_values"])
    assert audit["intake_source"] == "local-capture"
    assert audit["capture_sha256"] == args.capture_sha256
    assert audit["source_message_id"] == "capture:" + args.capture_sha256
    assert str(image) not in json.dumps(audit) and "BILL 500.00" not in json.dumps(audit)
    with get_connection(str(home / "data.sqlite")) as reopened:
        assert snapshot(reopened) == rows


@pytest.mark.parametrize("field,value", [("supplier_id", "missing"), ("company_id", "other"),
                                         ("submit", True), ("source_message_id", "invented"),
                                         ("posting_date", "2026-02-30")])
def test_invalid_reviewed_fields_refuse_without_writes(capture, field, value):
    conn, _, _, bill, args = capture
    before = snapshot(conn)
    bill[field] = value
    args.bill_json = json.dumps(bill)
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result["status"] == "error", result
    conn.commit()
    assert snapshot(conn) == before


@pytest.mark.parametrize("rate", ["NaN", "0", "1e2", "10.001", 10.01, True])
def test_unreviewed_or_inexact_amount_refuses_without_writes(capture, rate):
    conn, _, _, bill, args = capture
    before = snapshot(conn)
    bill["items"][0]["rate"] = rate
    args.bill_json = json.dumps(bill)
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


def test_foreign_supplier_company_refuses_without_writes(capture):
    conn, _, _, bill, args = capture
    args.company_id = bill["company_id"] = seed_company(conn, name="Other capture company")
    args.bill_json = json.dumps(bill)
    before = snapshot(conn)
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result["status"] == "error" and "another company" in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("digest", [None, "A" * 64, "0" * 64])
def test_missing_wrong_or_changed_hash_refuses_before_draft(capture, digest):
    conn, _, _, _, args = capture
    before = snapshot(conn)
    args.capture_sha256 = digest
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


def test_file_changed_after_review_refuses_without_writes(capture):
    conn, _, image, _, args = capture
    before = snapshot(conn)
    image.write_bytes(pdf_bill())
    result = call_action(BUYING.add_captured_vendor_bill, conn, args)
    assert result == {"status": "error", "message": "Capture changed since review; extract and review the current file again"}
    assert snapshot(conn) == before


def test_unknown_capture_company_refuses_before_tool(capture, monkeypatch):
    conn, _, _, _, args = capture
    args.company_id = "missing"
    before = snapshot(conn)

    def unexpected(*a, **k):
        pytest.fail("Unknown company must refuse before invoking a tool")

    monkeypatch.setattr(BUYING.shutil, "which", unexpected)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result == {"status": "error", "message": "--company-id must identify an existing company"}
    assert snapshot(conn) == before


@pytest.mark.parametrize("kind", ["symlink", "directory", "empty", "unsupported", "huge", "pixels"])
def test_invalid_local_file_refuses_without_writes(capture, kind):
    conn, _, image, _, args = capture
    if kind == "symlink":
        link = image.with_name("linked.png")
        link.symlink_to(image)
        args.capture_file = str(link)
    elif kind == "directory":
        args.capture_file = str(image.parent)
    elif kind == "empty":
        image.write_bytes(b"")
    elif kind == "unsupported":
        image.write_bytes(b"untrusted bill text")
    elif kind == "huge":
        with image.open("wb") as target:
            target.truncate(10 * 1024 * 1024 + 1)
    else:
        data = png_bill()
        image.write_bytes(data[:16] + struct.pack(">II", 10000, 10000) + data[24:])
    before = snapshot(conn)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


def test_missing_dependency_refuses_honestly(capture, monkeypatch):
    conn, _, _, _, args = capture
    monkeypatch.setattr(BUYING.shutil, "which", lambda *a, **k: None)
    before = snapshot(conn)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result == {"status": "error", "message": "Local capture requires tesseract; no extraction or draft was created"}
    assert snapshot(conn) == before


def test_tool_timeout_is_bounded_and_refuses(capture, monkeypatch):
    conn, _, _, _, args = capture
    monkeypatch.setattr(BUYING.shutil, "which", lambda *a, **k: "/usr/bin/tesseract")

    def expired(command, **kwargs):
        assert kwargs["timeout"] == 30 and kwargs.get("shell", False) is False
        assert "ERPCLAW_DB_URL" not in kwargs["env"]
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(BUYING.subprocess, "run", expired)
    before = snapshot(conn)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result["status"] == "error" and "30 seconds" in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("output", [b"x" * 65537, b"\xff", b" \n"])
def test_oversized_invalid_or_empty_text_refuses(capture, monkeypatch, output):
    conn, _, _, _, args = capture
    monkeypatch.setattr(BUYING.shutil, "which", lambda *a, **k: "/usr/bin/tesseract")

    def extracted(command, **kwargs):
        Path(command[2] + ".txt").write_bytes(output)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(BUYING.subprocess, "run", extracted)
    before = snapshot(conn)
    result = call_action(BUYING.capture_vendor_bill, conn, args)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


def test_foundation_routes_capture_and_reviewed_draft(capture):
    if shutil.which("tesseract", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin") is None:
        pytest.skip("Optional local Tesseract is not installed")
    conn, home, image, bill, args = capture
    environment = dict(os.environ, ERPCLAW_HOME=str(home),
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    base = [sys.executable, str(SCRIPTS / "db_query.py"), "--company-id", args.company_id,
            "--capture-file", str(image)]
    before = snapshot(conn)
    read = subprocess.run(base + ["--action", "capture-vendor-bill"],
                          capture_output=True, text=True, env=environment, timeout=60)
    assert read.returncode == 0, (read.stdout, read.stderr)
    extracted = json.loads(read.stdout)
    assert "BILL" in extracted["text"] and "500" in extracted["text"]
    assert snapshot(conn) == before
    saved = subprocess.run(base + ["--action", "add-captured-vendor-bill", "--capture-sha256",
                           extracted["capture_sha256"], "--bill-json", json.dumps(bill)],
                           capture_output=True, text=True, env=environment, timeout=60)
    assert saved.returncode == 0, (saved.stdout, saved.stderr)
    assert json.loads(saved.stdout)["grand_total"] == "30.03"
    assert snapshot(conn)["purchase_invoice"][0]["status"] == "draft"

"""Envelope rule for ok(): top-level status stays "ok"; document state rides document_status."""
import io
import json

import pytest

from erpclaw_lib.response import ok


def _capture(fn, *args, **kwargs):
    import sys
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        with pytest.raises(SystemExit) as exc:
            fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return exc.value.code, json.loads(buf.getvalue().strip())


def test_ok_preserves_document_status(capsys):
    with pytest.raises(SystemExit):
        ok({"id": "x", "status": "draft"})
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["document_status"] == "draft"


def test_ok_leaves_plain_ok_alone(capsys):
    with pytest.raises(SystemExit):
        ok({"status": "ok", "n": 1})
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["status"] == "ok"
    assert "document_status" not in data


def test_ok_refuses_conflicting_document_status(capsys):
    with pytest.raises(SystemExit) as exc:
        ok({"status": "draft", "document_status": "paid"})
    assert exc.value.code == 1
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["status"] == "error"

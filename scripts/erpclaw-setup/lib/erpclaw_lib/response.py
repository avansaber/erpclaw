"""Standard response helpers for ERPClaw skill scripts.

Replaces the _ok(), _err(), and _row_to_dict() functions that were
duplicated across all 24 skill db_query.py files.
"""
import json
import sys


def ok(data: dict) -> None:
    """Send success response and exit.

    Top-level ``status`` is the envelope (``"ok"`` here); a document's own
    state, when present, is preserved under ``document_status``.
    """
    existing = data.get("status")
    if existing is not None and existing != "ok":
        if "document_status" in data and data["document_status"] != existing:
            err("response carries both status and a different document_status; refusing to clobber")
        data["document_status"] = existing
    data["status"] = "ok"
    print(json.dumps(data, indent=2, default=str))
    sys.exit(0)


def err(message: str, suggestion: str = None) -> None:
    """Send error response and exit."""
    data = {"status": "error", "message": message}
    if suggestion:
        data["suggestion"] = suggestion
    print(json.dumps(data, indent=2))
    sys.exit(1)


def row_to_dict(row) -> dict:
    """Convert sqlite3.Row to plain dict."""
    return dict(row) if row else {}


def rows_to_list(rows) -> list[dict]:
    """Convert list of sqlite3.Row to list of dicts."""
    return [dict(r) for r in rows] if rows else []

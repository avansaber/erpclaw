"""Fixed file refusals and redacted diagnostics at their raising sites."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from erpclaw_core import files
from erpclaw_core.common import Refusal
from owner_text import owner_text_violations

PRIVATE = "private-owner-name"
SAVE = "The file could not be saved. Please try making it again; nothing was changed."
ROOT = "The file could not be saved. Please ask for help restoring these books' files; nothing was changed."
TYPE = "Only PDF files can be saved. Please choose a PDF file; nothing was changed."
DOCUMENT = "The file could not be saved. Please choose the document it belongs to; nothing was changed."


def check(caplog, operation, code, sentence, *details):
    caplog.set_level(logging.WARNING, logger=files.__name__)
    with pytest.raises(Refusal) as caught:
        operation()
    refusal = caught.value
    assert refusal.code == code
    assert refusal.message == str(refusal) == sentence
    assert not owner_text_violations(sentence, code=code)
    for technical in (PRIVATE, "render_inputs", "ROOT.json", "application/pdf", "int", "list", "tuple", code):
        assert technical not in sentence
    records = [r for r in caplog.records if r.name == files.__name__]
    assert len(records) == 1
    diagnostic = records[0].getMessage()
    assert code in diagnostic
    assert all(detail in diagnostic for detail in details)
    assert PRIVATE not in repr(records[0].__dict__)
    assert records[0].exc_info is None


@pytest.mark.parametrize("case, detail", [
    ("other-books", "belong to other books"), ("missing-root", "not in <key>"),
    ("missing-marker", "marker is missing"), ("renamed", "renamed from <key> to <key>"),
])
def test_restore_old_marker_problem_exposes_folder_or_database(monkeypatch, tmp_path, caplog, case, detail):
    root = tmp_path / files.root_name(PRIVATE)
    root.mkdir()
    marker = root / files.ROOT_MARKER
    marker.write_text(json.dumps({"books_id": PRIVATE if case == "other-books" else "books",
                                  "database": PRIVATE if case == "renamed" else "database", "format": 1}))
    monkeypatch.setattr(files, "files_dir", lambda: tmp_path)
    monkeypatch.setattr(files, "root_name", lambda database: root.name)
    monkeypatch.setattr(files, "database_name", lambda conn: "database")
    monkeypatch.setattr(files, "books_id", lambda conn: "books")
    monkeypatch.setattr(files, "_sync_path", lambda path: None)
    if case == "missing-root":
        monkeypatch.setattr(type(root), "is_dir", lambda path: path != root)
    if case == "missing-marker":
        monkeypatch.setattr(files, "read_marker", lambda path: None)
    check(caplog, lambda: files._ensure_root(None), "files_not_these_books", ROOT, detail, "<key>")


@pytest.mark.parametrize("value, path, details", [
    ([PRIVATE], None, ("render_inputs", "mapping", "list")),
    ({(PRIVATE,): PRIVATE}, None, ("render_inputs.<key>", "key", "tuple")),
    ({PRIVATE: 2}, None, ("render_inputs.<key>", "int")),
    ({PRIVATE: {PRIVATE: [PRIVATE]}}, None, ("render_inputs.<key>.<key>", "list")),
    ([PRIVATE], PRIVATE, ("<key>", "list")),
])
def test_restore_old_render_f_string_exposes_path_key_or_type(caplog, value, path, details):
    operation = lambda: files._render_inputs(value) if path is None else files._render_inputs(value, path)
    check(caplog, operation, "render_inputs_shape", SAVE, *details)


@pytest.mark.parametrize("overrides, code, sentence, details", [
    ({"data": PRIVATE}, "file_not_bytes", SAVE, ("str",)),
    ({"media_type": PRIVATE}, "file_type_refused", TYPE, ("<key>", "application/pdf")),
    ({"data": b""}, "file_size_refused", "A stored file holds at least one byte and at most 10 MB.", ("size=0",)),
    ({"document": (PRIVATE,)}, "file_document", DOCUMENT, ("tuple",)),
])
def test_restore_old_put_text_exposes_input_or_technical_shape(caplog, overrides, code, sentence, details):
    args = {"data": b"pdf", "media_type": "application/pdf", "document": ("invoice", PRIVATE), **overrides}
    check(caplog, lambda: files.put(None, **args), code, sentence, *details)


def test_restore_old_home_log_exposes_owner_folder(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path / PRIVATE / "home"))
    check(caplog, files._home_ready, "files_home_missing",
          "ERPClaw's storage folder isn't set up yet, so the file could not be saved.", "parent <key>")


def test_remove_save_diagnostic_loses_failure_detail(monkeypatch, caplog):
    monkeypatch.setattr(files, "lock_bytes", lambda *args: None)
    ctx = SimpleNamespace(company_id=PRIVATE, conn=SimpleNamespace(execute=lambda *args: SimpleNamespace(scalar=lambda: None)))

    def fail(conn):
        raise PermissionError(PRIVATE)

    monkeypatch.setattr(files, "_ensure_root", fail)
    check(caplog, lambda: files.put(ctx, b"pdf", "application/pdf", ("invoice", PRIVATE)),
          "file_not_saved", "The file could not be saved safely.", "PermissionError", "<key>")


def test_remove_missing_row_diagnostic_loses_failure_detail(caplog):
    ctx = SimpleNamespace(company_id=PRIVATE, conn=SimpleNamespace(execute=lambda *args: SimpleNamespace(
        mappings=lambda: SimpleNamespace(first=lambda: None))))
    check(caplog, lambda: files.row(ctx, PRIVATE), "file_not_found", "No such stored file.", "<key>")


@pytest.mark.parametrize("lost", [False, True])
def test_remove_altered_file_diagnostic_loses_failure_detail(monkeypatch, tmp_path, caplog, lost):
    path = tmp_path / PRIVATE
    if not lost:
        path.write_bytes(b"altered")
    monkeypatch.setattr(files, "row", lambda *args: {"company_id": PRIVATE, "sha256": "expected", "size": 3})
    monkeypatch.setattr(files, "root_dir", lambda conn: tmp_path)
    monkeypatch.setattr(files, "file_path", lambda *args: path)
    check(caplog, lambda: files.get(SimpleNamespace(conn=None), PRIVATE), "file_altered",
          "The stored file has been altered or lost.", "FileNotFoundError" if lost else "size or sha256 mismatch", "<key>")

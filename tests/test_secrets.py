"""Credential boundaries, permissions, native adapters and the mail consumer."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets as random
import subprocess
import sys
import traceback
from unittest.mock import Mock

import pytest
from sqlalchemy import MetaData, event, select, update

from erpclaw_core import administration, models as m, principals, secrets
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.setup import create_company
from erpclaw_email import smtp


@pytest.fixture
def sentinel():
    return "".join(("boundary", "-", random.token_hex(20), "é"))


class MemoryStore:
    def __init__(self):
        self.items = {}
        self.writes = []

    def read(self, item):
        return self.items.get(item)

    def write(self, item, value):
        self.items[item] = value
        self.writes.append(("set", item))

    def delete(self, item):
        self.items.pop(item, None)
        self.writes.append(("removed", item))


@pytest.fixture
def memory(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(secrets, "_platform_store", lambda **kw: store)
    return store


@pytest.fixture
def books(engine):
    second = create_company(engine, "Other Books", "USD", 1)
    with engine.begin() as conn:
        first = conn.execute(select(m.company.c.id).where(m.company.c.id != second)).scalar_one()
        owner = principals.owner(conn)["id"]
        administration.add_principal(conn, "Mail admin", ["manage_secrets"], [first])
        admin = conn.execute(select(m.principal.c.id).where(m.principal.c.name == "Mail admin")).scalar_one()
    return engine, first, second, owner, admin


def dump_books(engine):
    with engine.connect() as conn:
        actual = MetaData()
        actual.reflect(conn)
        return {t.name: [dict(row) for row in conn.execute(select(t)).mappings()]
                for t in actual.sorted_tables}


def assert_clean(value, *outputs):
    for output in outputs:
        rendered = str(output)
        assert value not in rendered
        assert value[:8] not in rendered


@pytest.mark.parametrize("secret,target", [
    ("smtp_password", "company"), ("model_key", "muse"),
    ("model_key", "qwen3_5_9b_openrouter_primeintellect"),
    ("model_key", "qwen3_5_9b_openrouter_deepinfra"),
])
def test_save_remove_only_audit_metadata(books, memory, sentinel, caplog, capsys, secret, target):
    from erpclaw_assistant import store as assistant_store

    engine, cid, _, owner, _ = books
    target = cid if target == "company" else target
    before = dump_books(engine)
    assistant = assistant_store._open()
    try:
        assistant_before = list(assistant.iterdump())
    finally:
        assistant.close()
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with caplog.at_level(logging.DEBUG):
            saved = secrets.save_secret(engine, owner, secret, target, sentinel)
            assert memory.items
            removed = secrets.remove_secret(engine, owner, secret, target)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert saved == {"saved": True}
    assert removed == {"removed": True}
    assert memory.items == {}
    after = dump_books(engine)
    old_audit, new_audit = before.pop("audit_log"), after.pop("audit_log")
    assert before == after
    added = [r for r in new_audit if r["id"] not in {a["id"] for a in old_audit}]
    assert {r["action"] for r in added} == {"set_secret", "remove_secret"}
    assert all(r["actor"] == owner and r["at"] for r in added)
    assert all(json.loads(r["detail"]) == {"secret": secret, "for": target} for r in added)
    if secret == "smtp_password":
        assert all(r["company_id"] == cid for r in added)
    else:
        assert len(added) == 2
    assistant = assistant_store._open()
    try:
        assistant_after = list(assistant.iterdump())
    finally:
        assistant.close()
    assert assistant_before == assistant_after
    captured = capsys.readouterr()
    assert_clean(sentinel, saved, removed, added, after, assistant_after, sql, caplog.text,
                 captured, repr(memory), repr(secrets.SecretValue(sentinel)))


@pytest.mark.parametrize("operation", ["save", "remove"])
@pytest.mark.parametrize("secret,target,allowed", [
    ("smtp_password", "first", True), ("smtp_password", "second", False),
    ("model_key", "muse", False),
])
def test_company_permission_boundary(books, memory, sentinel, operation, secret, target, allowed):
    engine, first, second, _, admin = books
    target = {"first": first, "second": second}.get(target, target)
    args = (engine, admin, secret, target)
    call = (lambda: secrets.save_secret(*args, sentinel)) if operation == "save" else lambda: secrets.remove_secret(*args)
    before = dump_books(engine)
    if allowed:
        call()
        assert len(memory.writes) == 1
    else:
        with pytest.raises(Refusal) as exc:
            call()
        assert exc.value.code == "forbidden"
        assert memory.writes == [] and dump_books(engine) == before


def test_reloads_disabled_principal_and_grants(books, memory, sentinel):
    engine, first, _, owner, admin = books
    with engine.begin() as conn:
        administration.revoke(conn, "Mail admin", ["manage_secrets"], first)
        conn.execute(update(m.principal).where(m.principal.c.id == owner).values(disabled=True))
    for pid in (owner, admin):
        with pytest.raises(Refusal) as exc:
            secrets.save_secret(engine, pid, "smtp_password", first, sentinel)
        assert exc.value.code == "forbidden"
    assert memory.writes == []


@pytest.mark.parametrize("secret,target", [
    ("unlisted", "first"), ("smtp_password", "unknown"),
    ("smtp_password", "00000000-0000-4000-8000-000000000099"),
    ("model_key", "unknown"), ("model_key", "erpclaw-v3-openrouter"),
    ("model_key", "qwen3_5_9b_lmstudio"),
])
def test_unknown_refused_without_write(books, memory, sentinel, secret, target):
    engine, first, _, owner, _ = books
    target = first if target == "first" else target
    before = dump_books(engine)
    for call in (lambda: secrets.save_secret(engine, owner, secret, target, sentinel),
                 lambda: secrets.remove_secret(engine, owner, secret, target)):
        with pytest.raises(Refusal) as exc:
            call()
        assert exc.value.code == "not_found"
        assert_clean(sentinel, exc.value, repr(exc.value), exc.value.data)
    assert memory.writes == [] and dump_books(engine) == before


@pytest.mark.parametrize("kind", ["empty", "long", "not_text", "surrogate"])
def test_invalid_value_has_only_field_error(books, memory, sentinel, kind):
    engine, cid, _, owner, _ = books
    value = {"empty": "", "long": sentinel * 200, "not_text": {"value": sentinel},
             "surrogate": sentinel + chr(0xD800)}[kind]
    with pytest.raises(Refusal) as exc:
        secrets.save_secret(engine, owner, "smtp_password", cid, value)
    assert exc.value.code == "invalid_secret"
    assert exc.value.data == {"field": "value"}
    assert_clean(sentinel, repr(exc.value), exc.value.__dict__)
    assert memory.writes == []


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_backend_exception_is_never_exposed(books, memory, monkeypatch, sentinel, caplog, capsys, operation):
    engine, cid, _, owner, _ = books
    before = dump_books(engine)
    monkeypatch.setattr(memory, operation, Mock(side_effect=RuntimeError(sentinel)))
    if operation == "read":
        result = secrets.read_secret("smtp_password", cid, consumer="email")
        assert result is None
    else:
        with pytest.raises(Refusal) as exc:
            if operation == "write":
                secrets.save_secret(engine, owner, "smtp_password", cid, sentinel)
            else:
                secrets.remove_secret(engine, owner, "smtp_password", cid)
        assert exc.value.code == "secret_store_unavailable"
        assert exc.value.__context__ is None and exc.value.__cause__ is None
        assert_clean(sentinel, "".join(traceback.format_exception(exc.value)), exc.value.__dict__)
    assert dump_books(engine) == before
    assert_clean(sentinel, caplog.text, capsys.readouterr())


def test_email_uses_store_and_environment_stays_first(books, memory, monkeypatch, sentinel):
    engine, cid, _, owner, _ = books
    name = "ERPCLAW_SMTP_PASSWORD_" + cid.replace("-", "_")
    monkeypatch.delenv(name, raising=False)
    secrets.save_secret(engine, owner, "smtp_password", cid, sentinel)
    assert smtp.company_password(cid) == sentinel
    replacement = sentinel[::-1]
    monkeypatch.setenv(name, replacement)
    assert smtp.company_password(cid) == replacement
    monkeypatch.delenv(name)
    secrets.remove_secret(engine, owner, "smtp_password", cid)
    assert smtp.company_password(cid) is None


def test_consumer_cannot_read_other_secret(memory, sentinel):
    memory.items["erpclaw-v3-muse"] = sentinel
    with pytest.raises(Refusal) as exc:
        secrets.read_secret("model_key", "muse", consumer="email")
    assert exc.value.code == "forbidden"
    assert secrets.read_secret("model_key", "muse", consumer="assistant") == sentinel


@pytest.mark.parametrize("outcome", [True, False, "exception"])
def test_check_returns_fixed_text_and_keeps_value(books, memory, sentinel, caplog, outcome):
    engine, cid, _, owner, _ = books
    secrets.save_secret(engine, owner, "smtp_password", cid, sentinel)
    def probe(value):
        assert value == sentinel
        if outcome == "exception":
            raise RuntimeError(sentinel)
        return outcome
    before = dump_books(engine)
    result = secrets.check_secret(engine, owner, "smtp_password", cid, probe)
    assert result == {"works": outcome is True, "text": "It works." if outcome is True else "The mail account's password was refused. The saved password is kept."}
    assert memory.items and dump_books(engine) == before
    assert_clean(sentinel, result, caplog.text)


def test_missing_check_does_not_call_probe_or_write(books, memory):
    engine, cid, _, owner, admin = books
    probe = Mock()
    before = dump_books(engine)
    result = secrets.check_secret(engine, owner, "smtp_password", cid, probe)
    assert result == {"works": False, "text": "The mail account's password is not set up."}
    with pytest.raises(Refusal):
        secrets.check_secret(engine, admin, "model_key", "muse", probe)
    probe.assert_not_called()
    assert dump_books(engine) == before


def test_macos_existing_read_command(monkeypatch, sentinel):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, sentinel + "\r\n"))
    store = secrets._MacStore(runner=runner)
    assert store.read("erpclaw-v3-muse") == sentinel
    args, kwargs = runner.call_args
    assert args[0] == ["security", "find-generic-password", "-s", "erpclaw-v3-muse", "-w"]
    assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["timeout"] == 20
    assert_clean(sentinel, args, kwargs, repr(store))


def test_linux_write_refused_plainly(monkeypatch, sentinel):
    monkeypatch.setattr(sys, "platform", "linux")
    store = secrets._platform_store()
    with pytest.raises(Refusal, match="credential store") as exc:
        store.write("erpclaw-v3-muse", sentinel)
    assert_clean(sentinel, exc.value)
    assert store.read("erpclaw-v3-muse") is None


class FakeWin:
    def __init__(self):
        self.data = None
        self.freed = 0

    def CredWriteW(self, pointer, flags):
        credential = ctypes.cast(pointer, ctypes.POINTER(secrets._Credential)).contents
        assert credential.Type == 1 and credential.Persist == 2 and flags == 0
        self.target = credential.TargetName
        self.data = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return 1

    def CredReadW(self, item, kind, flags, output):
        assert (item, kind, flags) == (self.target, 1, 0)
        self.blob = ctypes.create_string_buffer(self.data)
        self.credential = secrets._Credential()
        self.credential.CredentialBlobSize = len(self.data)
        self.credential.CredentialBlob = ctypes.cast(self.blob, ctypes.POINTER(ctypes.c_ubyte))
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(secrets._Credential)))[0] = ctypes.pointer(self.credential)
        return 1

    def CredFree(self, pointer):
        self.freed += 1

    def CredDeleteW(self, item, kind, flags):
        assert (item, kind, flags) == (self.target, 1, 0)
        self.data = None
        return 1


def test_windows_generic_native_adapter(monkeypatch, sentinel):
    api = FakeWin()
    monkeypatch.setattr(secrets, "_win_library", lambda: api)
    store = secrets._WindowsStore()
    store.write("erpclaw-v3-muse", sentinel)
    assert api.data == sentinel.encode("utf-8")
    assert store.read("erpclaw-v3-muse") == sentinel
    assert api.freed == 1
    store.delete("erpclaw-v3-muse")
    assert api.data is None
    assert_clean(sentinel, repr(store))


def test_windows_native_byte_limit_refused_before_write(monkeypatch, sentinel):
    api = Mock()
    monkeypatch.setattr(secrets, "_win_library", lambda: api)
    store = secrets._WindowsStore()
    with pytest.raises(Refusal) as exc:
        store.write("erpclaw-v3-muse", sentinel * 100)
    api.CredWriteW.assert_not_called()
    assert_clean(sentinel, exc.value)


def test_profile_destinations_match_existing_readers():
    from erpclaw_assistant import muse, profiles

    shipped = profiles.load()
    assert secrets.MODEL_ITEMS["muse"] == muse.MUSE_ITEM_NAME
    for name, profile in shipped.items():
        if profile.key_item:
            assert secrets.MODEL_ITEMS[name] == profile.key_item


@pytest.mark.parametrize("platform,expected", [("win32", "_WindowsStore"), ("darwin", "_MacStore"),
                                               ("linux", "_UnavailableStore")])
def test_platform_selection(monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert type(secrets._platform_store()).__name__ == expected


@pytest.mark.parametrize("status", [1168, 5, 1312])
def test_windows_read_delete_error_codes(monkeypatch, status):
    api = Mock()
    api.CredReadW.return_value = api.CredDeleteW.return_value = 0
    monkeypatch.setattr(secrets, "_win_library", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: status, raising=False)
    store = secrets._WindowsStore()
    if status == 1168:
        assert store.read("erpclaw-v3-muse") is None
        store.delete("erpclaw-v3-muse")
    else:
        with pytest.raises(Refusal):
            store.read("erpclaw-v3-muse")
        with pytest.raises(Refusal):
            store.delete("erpclaw-v3-muse")
    api.CredFree.assert_not_called()


@pytest.mark.parametrize("exists", [False, True])
def test_macos_native_add_replace_delete_without_secret_arguments(monkeypatch, sentinel, exists):
    security, foundation = Mock(), Mock()
    def find(keychain, service_len, service, account_len, account, length, data, reference):
        assert length is None and data is None
        if exists:
            ctypes.cast(reference, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
        return 0 if exists else -25300
    security.SecKeychainFindGenericPassword.side_effect = find
    security.SecKeychainItemDelete.return_value = 0
    seen = []
    def add(keychain, size, service, account_size, account, length, blob, reference):
        assert service == b"erpclaw-v3-muse" and account_size == 0
        seen.append(ctypes.string_at(blob, length))
        return 0
    def modify(reference, attributes, length, blob):
        seen.append(ctypes.string_at(blob, length))
        return 0
    security.SecKeychainAddGenericPassword.side_effect = add
    security.SecKeychainItemModifyAttributesAndData.side_effect = modify
    monkeypatch.setattr(secrets, "_mac_libraries", lambda: (security, foundation))
    process = Mock(side_effect=AssertionError("writes must not spawn processes"))
    store = secrets._MacStore(runner=process)
    store.write("erpclaw-v3-muse", sentinel)
    store.delete("erpclaw-v3-muse")
    assert seen == [sentinel.encode("utf-8")]
    assert security.SecKeychainItemDelete.call_count == int(exists)
    assert foundation.CFRelease.call_count == 2 * int(exists)
    process.assert_not_called()


def test_audit_insert_failure_never_writes_store(books, memory, sentinel):
    engine, cid, _, owner, _ = books
    def refuse(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO audit_log"):
            raise RuntimeError("audit unavailable")
    event.listen(engine, "before_cursor_execute", refuse)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            secrets.save_secret(engine, owner, "smtp_password", cid, sentinel)
    finally:
        event.remove(engine, "before_cursor_execute", refuse)
    assert memory.writes == []


def test_next_read_observes_replacement_and_removal(books, memory, sentinel):
    engine, _, _, owner, _ = books
    for value in (sentinel, sentinel[::-1]):
        secrets.save_secret(engine, owner, "model_key", "muse", value)
        assert secrets.read_secret("model_key", "muse", consumer="assistant") == value
    secrets.remove_secret(engine, owner, "model_key", "muse")
    assert secrets.read_secret("model_key", "muse", consumer="assistant") is None


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Credential Manager")
def test_windows_real_account_roundtrip(sentinel):
    store = secrets._WindowsStore()
    item = "erpclaw-v3-test-" + random.token_hex(12)
    try:
        store.write(item, sentinel)
        assert store.read(item) == sentinel
    finally:
        store.delete(item)
    assert store.read(item) is None


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and a separate local account")
def test_windows_genuinely_separate_account_cannot_read(sentinel):
    # CI supplies an existing second local user, not another session of this user.
    username = os.environ.get("ERPCLAW_TEST_OTHER_USER")
    password = os.environ.get("ERPCLAW_TEST_OTHER_PASSWORD")
    if not username or not password:
        pytest.skip("a genuinely separate local account must be provisioned by Windows CI")
    from ctypes import wintypes

    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.LogonUserW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.LogonUserW.restype = wintypes.BOOL
    api.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
    api.ImpersonateLoggedOnUser.restype = wintypes.BOOL
    api.RevertToSelf.argtypes, api.RevertToSelf.restype = [], wintypes.BOOL
    api.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetUserNameW.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    def current_user():
        buffer, size = ctypes.create_unicode_buffer(256), wintypes.DWORD(256)
        assert api.GetUserNameW(buffer, ctypes.byref(size))
        return buffer.value.casefold()
    original = current_user()
    token = wintypes.HANDLE()
    assert api.LogonUserW(username, ".", password, 2, 0, ctypes.byref(token))
    store = secrets._WindowsStore()
    item = "erpclaw-v3-test-" + random.token_hex(12)
    impersonating = False
    try:
        store.write(item, sentinel)
        assert api.ImpersonateLoggedOnUser(token)
        impersonating = True
        assert current_user() != original
        # A fresh local account may have no credential set; either outcome
        # proves this account cannot obtain the original account's credential.
        try:
            value = store.read(item)
        except Refusal:
            value = None
        assert value is None
    finally:
        if impersonating:
            assert api.RevertToSelf()
        kernel.CloseHandle(token)
        store.delete(item)

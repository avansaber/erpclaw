"""The fixed credential boundary for mail and hosted models.

Values live only in memory and the current OS account's credential store. The
public mutation helpers own their books transaction; only metadata is audited.
First-party consumers are trusted in process: the consumer check prevents an
accidental cross-read, not impersonation by arbitrary Python in this process.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import select

from . import models as m, principals
from .common import Refusal, audit, company_ctx


SECRET_NAMES = ("smtp_password", "model_key")
# Reviewed, shipped profiles only; callers and plugins cannot supply store names.
MODEL_ITEMS = MappingProxyType({
    "muse": "erpclaw-v3-muse",
    "qwen3_5_9b_openrouter_primeintellect": "erpclaw-v3-openrouter",
    "qwen3_5_9b_openrouter_deepinfra": "erpclaw-v3-openrouter",
})


class SecretValue:
    """An in-memory value whose ordinary string/repr never reveals its contents."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def __repr__(self):
        return "SecretValue(<redacted>)"

    __str__ = __repr__


def _canonical_company(company_id) -> bool:
    try:
        return isinstance(company_id, str) and str(UUID(company_id)) == company_id
    except ValueError:
        return False


def _item(secret, target) -> str:
    if secret == "smtp_password" and _canonical_company(target):
        return "erpclaw-v3-smtp-" + target
    if secret == "model_key" and isinstance(target, str) and target in MODEL_ITEMS:
        return MODEL_ITEMS[target]
    raise Refusal("That secret or destination does not exist.", "not_found")


@dataclass(frozen=True)
class _Destination:
    secret: str
    target: str
    item: str
    company_ids: tuple[str, ...]
    principal_id: str


def check_manage_secrets(conn, principal_id: str, secret: str, target: str) -> _Destination:
    """Validate the fixed destination and current grants, before any store access.

    Routes may call this for a read-only preflight. Mutations call it again in
    their own transaction, reloading and locking the authenticated principal so
    revocation/disable cannot race a write. Model credentials require every
    company's grant, including companies whose setup is still pending.
    """
    item = _item(secret, target)
    companies = tuple(conn.execute(select(m.company.c.id).order_by(m.company.c.id)).scalars())
    if not companies or (secret == "smtp_password" and target not in companies):
        raise Refusal("That secret or destination does not exist.", "not_found")
    required = (target,) if secret == "smtp_password" else companies
    principal = principals.by_id(conn, principal_id, lock=True)
    for company in required:
        principals.require(conn, principal, company, "manage_secrets")
    return _Destination(secret, target, item, required, principal["id"])


def validate_secret_value(value) -> SecretValue:
    """Field-only validation for the future route's 422 handler; never echo input."""
    valid = isinstance(value, str) and 1 <= len(value) <= 4096
    encoded = None
    if valid:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            valid = False
    if not valid:
        raise Refusal("The value field must contain 1 to 4096 characters.", "invalid_secret", field="value")
    if sys.platform == "win32" and len(encoded) > 2560:
        raise Refusal("The value is too long for this operating system's credential store.",
                      "invalid_secret", field="value")
    return SecretValue(value)


def _unavailable():
    return Refusal("The operating system credential store is unavailable.",
                   "secret_store_unavailable")


def _store_operation(operation, *args):
    # Raise outside the exception handler: no captured backend exception (which
    # may contain a value) remains in the public exception's context or cause.
    try:
        return True, operation(*args)
    except Exception:
        return False, None


def _audit_change(conn, destination: _Destination, action: str):
    # A model item is shared across books. Use an existing company as its audit
    # anchor, as for other books-wide administration, and name the profile in detail.
    ctx = company_ctx(conn, actor=destination.principal_id, company_id=destination.company_ids[0])
    audit(ctx, action, "secret", detail={"secret": destination.secret, "for": destination.target})


def save_secret(engine, principal_id: str, secret: str, target: str, value) -> dict:
    """Route call: save_secret(engine, authenticated_id, secret, target, value).

    Unknown destinations and missing grants never access the store. Stage the
    metadata audit before the OS write so an insert refusal cannot change a
    credential. A failed OS write rolls the audit transaction back. OS stores
    and PostgreSQL cannot share an atomic commit; callers must report a commit
    failure as an uncertain save and never automatically retry it.
    """
    with engine.begin() as conn:
        destination = check_manage_secrets(conn, principal_id, secret, target)
        wrapped = validate_secret_value(value)
        _audit_change(conn, destination, "set_secret")
        ok, _ = _store_operation(lambda: _platform_store().write(destination.item, wrapped._value))
        if not ok:
            raise _unavailable()
    return {"saved": True}


def remove_secret(engine, principal_id: str, secret: str, target: str) -> dict:
    """Route call: remove_secret(engine, authenticated_id, secret, target)."""
    with engine.begin() as conn:
        destination = check_manage_secrets(conn, principal_id, secret, target)
        _audit_change(conn, destination, "remove_secret")
        ok, _ = _store_operation(lambda: _platform_store().delete(destination.item))
        if not ok:
            raise _unavailable()
    return {"removed": True}


def read_secret(secret: str, target: str, *, consumer: str, _runner=None) -> str | None:
    """Consumer-only access, with no cache; saves/removals apply at the next read.

    Readers receive canonical destinations from committed engine settings, not
    user input. Company existence and permissions are checked by the mutation
    helpers against books. This accessor never opens books or creates a store.
    """
    item = _item(secret, target)
    if consumer != ("email" if secret == "smtp_password" else "assistant"):
        raise Refusal("This consumer may not read that secret.", "forbidden")
    ok, value = _store_operation(lambda: _platform_store(runner=_runner).read(item))
    return value if ok and isinstance(value, str) else None


def check_secret(engine, principal_id: str, secret: str, target: str, probe) -> dict:
    """Run a trusted TLS/auth-only probe; it must not send mail or log values.

    A model probe may make one minimal call. Its return is strictly boolean;
    remote replies and exception text can never become a response. A failure
    does not remove the saved value. This helper never writes to books.
    """
    with engine.connect() as conn:
        destination = check_manage_secrets(conn, principal_id, secret, target)
        ok, value = _store_operation(lambda: _platform_store().read(destination.item))
        noun = "mail account's password" if secret == "smtp_password" else "model key"
        if not ok or not value:
            return {"works": False, "text": f"The {noun} is not set up."}
        ok, result = _store_operation(probe, value)
    works = ok and result is True
    failure = ("The mail account's password was refused. The saved password is kept."
               if secret == "smtp_password" else "The model key was refused. The saved key is kept.")
    return {"works": works, "text": "It works." if works else failure}


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime), ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _win_library():
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    pointer = ctypes.POINTER(_Credential)
    for name, arguments, result in (
        ("CredWriteW", [pointer, ctypes.c_uint32], ctypes.c_int),
        ("CredReadW", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.POINTER(pointer)], ctypes.c_int),
        ("CredDeleteW", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        ("CredFree", [ctypes.c_void_p], None),
    ):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return library


class _WindowsStore:
    """Generic credentials, persisted for the current owner's account on this machine."""

    def read(self, item):
        library = _win_library()
        pointer = ctypes.POINTER(_Credential)()
        if not library.CredReadW(item, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise _unavailable()
        try:
            return ctypes.string_at(pointer.contents.CredentialBlob,
                                    pointer.contents.CredentialBlobSize).decode("utf-8")
        finally:
            library.CredFree(pointer)

    def write(self, item, value):
        encoded = value.encode("utf-8")
        if len(encoded) > 2560:  # CRED_MAX_CREDENTIAL_BLOB_SIZE, measured in bytes
            raise Refusal("The value is too long for this operating system's credential store.",
                          "invalid_secret", field="value")
        blob = ctypes.create_string_buffer(encoded)
        credential = _Credential()
        credential.Type, credential.Persist = 1, 2  # GENERIC, LOCAL_MACHINE (same user only)
        credential.TargetName, credential.UserName = item, "ERPClaw"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        try:
            if not _win_library().CredWriteW(ctypes.byref(credential), 0):
                raise _unavailable()
        finally:
            ctypes.memset(blob, 0, len(blob))

    def delete(self, item):
        if not _win_library().CredDeleteW(item, 1, 0) and ctypes.get_last_error() != 1168:
            raise _unavailable()


def _mac_libraries():
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    for name, arguments in (
        ("SecKeychainFindGenericPassword", [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]),
        ("SecKeychainAddGenericPassword", [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        ("SecKeychainItemModifyAttributesAndData", [ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_uint32, ctypes.c_void_p]),
        ("SecKeychainItemDelete", [ctypes.c_void_p]),
    ):
        function = getattr(security, name)
        function.argtypes, function.restype = arguments, ctypes.c_int32
    foundation.CFRelease.argtypes, foundation.CFRelease.restype = [ctypes.c_void_p], None
    return security, foundation


class _MacStore:
    """Preserve the existing read command; write directly, never via process arguments."""

    def __init__(self, *, runner=None):
        self._runner = runner

    def read(self, item):
        result = (self._runner or subprocess.run)(
            ["security", "find-generic-password", "-s", item, "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20, check=False)
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else None

    def _change(self, item, value=None):
        security, foundation = _mac_libraries()
        service = item.encode("utf-8")
        reference = ctypes.c_void_p()
        # Search by service, just as the existing reader does. Do not read the
        # old password during a replacement or a removal.
        status = security.SecKeychainFindGenericPassword(None, len(service), service, 0, None,
                                                         None, None, ctypes.byref(reference))
        if status not in (0, -25300):  # errSecItemNotFound
            raise _unavailable()
        try:
            if value is None:
                if status == 0 and security.SecKeychainItemDelete(reference) != 0:
                    raise _unavailable()
                return
            encoded = value.encode("utf-8")
            blob = ctypes.create_string_buffer(encoded)
            try:
                if status == 0:
                    status = security.SecKeychainItemModifyAttributesAndData(reference, None, len(encoded), blob)
                else:
                    status = security.SecKeychainAddGenericPassword(None, len(service), service, 0, b"",
                                                                   len(encoded), blob, None)
                if status != 0:
                    raise _unavailable()
            finally:
                ctypes.memset(blob, 0, len(blob))
        finally:
            if reference.value:
                foundation.CFRelease(reference)

    def write(self, item, value):
        self._change(item, value)

    def delete(self, item):
        self._change(item)


class _UnavailableStore:
    def read(self, item):
        return None

    def write(self, item, value):
        raise _unavailable()

    def delete(self, item):
        raise _unavailable()


def _platform_store(*, runner=None):
    if sys.platform == "win32":
        return _WindowsStore()
    if sys.platform == "darwin":
        return _MacStore(runner=runner)
    # Secret Service support requires a later reviewed desktop integration.
    return _UnavailableStore()

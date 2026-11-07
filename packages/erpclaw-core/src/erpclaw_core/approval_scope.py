"""Core-owned approval identity and guards; returned IDs are ordinary data.

The private live cursor is shared across copied execution contexts. Tier-1 Python
is trusted; this is not protection against arbitrary imports or memory mutation.
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any


@dataclass(frozen=True)
class _Identity:
    approval_id: str
    company_id: str
    contract: str
    operation_key: str
    owner: str | None


@dataclass(frozen=True)
class _Frame:
    number: int
    owner: str | None
    kind: str
    readonly: bool
    eligible: bool


@dataclass
class _Scope:
    identity: _Identity
    ctx: Any
    conn: Any
    root: Any
    effect: Any
    execution: tuple
    active: bool = True
    cursor: _Frame | None = None
    sequence: int = 0
    checks: dict = field(default_factory=dict)


# ContextVars carry only a claim. _live owns the actual cursor and lifecycle.
_claim: ContextVar[tuple[_Scope, int] | None] = ContextVar("erpclaw_approval_observation", default=None)
_live: dict[tuple, _Scope] = {}


def _execution() -> tuple:
    try:
        task = asyncio.current_task()
    except RuntimeError:  # synchronous entry, no running loop
        task = None
    return os.getpid(), threading.current_thread(), task


def _current(ctx=None) -> _Scope | None:
    scope = _live.get(_execution())
    if scope is None or not scope.active or (ctx is not None and scope.ctx is not ctx):
        return None
    return scope


def _claimed(scope: _Scope) -> bool:
    value = _claim.get()
    return bool(scope.cursor is not None and value is not None
                and value[0] is scope and value[1] == scope.cursor.number)


def _transaction_live(scope: _Scope) -> bool:
    """No SQL. Unknown transaction information is ineligible, never guessed.

    SQLAlchemy's public current nested transaction does not expose ancestry.
    Its pinned NestedTransaction._previous_nested chain is used conservatively:
    a missing link or cycle refuses observation rather than accepting a sibling.
    """
    try:
        conn = scope.conn
        if conn.closed is not False or conn.invalidated is not False:
            return False
        if conn.get_transaction() is not scope.root or scope.root.is_active is not True:
            return False
        if scope.root.connection is not conn or scope.effect.connection is not conn:
            return False
        if scope.effect.is_active is not True:
            return False
        nested = conn.get_nested_transaction()
        seen = set()
        while nested is not None and id(nested) not in seen:
            seen.add(id(nested))
            if nested.is_active is not True or nested.connection is not conn:
                return False
            if nested is scope.effect:
                return True
            nested = nested._previous_nested
    except Exception:
        return False
    return False


def _validated_scope(ctx) -> _Scope | None:
    scope = _current(ctx)
    if scope is None or not _claimed(scope) or scope.cursor.owner is None:
        return None
    if ctx.conn is not scope.conn or ctx.company_id != scope.identity.company_id:
        return None
    if not _transaction_live(scope):
        scope.active = False  # transaction loss can never revive this record
        return None
    return scope


def committing(ctx) -> str | None:
    """Name this execution's eligible approval; an ID is never bearer authority."""
    scope = _validated_scope(ctx)
    if scope is None or scope.cursor.readonly or not scope.cursor.eligible:
        return None
    return scope.identity.approval_id


def _caller_owner(ctx) -> str | None:
    scope = _validated_scope(ctx)
    if scope is None:
        return None
    return scope.cursor.owner


def _can_register(ctx) -> bool:
    scope = _validated_scope(ctx)
    if scope is None:
        return False
    frame = scope.cursor
    # A check may add a later registration, preserving the existing snapshot.
    # Its read-only query stays None; that is different from no live approval.
    return frame.kind == "check" or (frame.kind in {"commit", "service"}
                                      and frame.eligible and not frame.readonly)


def _related_issued(ctx) -> bool:
    """Negative Trusted diagnostic, including copied/foreign execution contexts.

    Issued records stay here until core unwinds even if observation was revoked.
    This never grants authority and never blocks an unrelated bootstrap connection.
    """
    execution = _execution()
    claim = _claim.get()
    conn = getattr(ctx, "conn", None)
    for current, scope in list(_live.items()):
        if (current == execution or scope.ctx is ctx or scope.conn is conn
                or (claim is not None and claim[0] is scope)):
            return True
    return False


@contextmanager
def _issue(ctx, approval_id: str, contract: str, operation_key: str, owner: str | None, effect):
    """Core approve alone calls this, inside its existing effect savepoint."""
    execution = _execution()
    previous = _live.get(execution)
    if previous is not None:
        # Nested approvals remain legacy-compatible but cannot borrow observation.
        with _frame(ctx, None, "nested_approval", readonly=True, eligible=False):
            yield
        return
    try:
        root = ctx.conn.get_transaction()
    except Exception:
        root = None
    scope = _Scope(_Identity(approval_id, ctx.company_id, contract, operation_key, owner),
                   ctx, ctx.conn, root, effect, execution)
    _live[execution] = scope
    token = _claim.set(None)
    try:
        yield
    finally:
        scope.active = False
        scope.cursor = None
        scope.checks.clear()
        _live.pop(execution, None)
        _claim.reset(token)


@contextmanager
def _frame(ctx, owner: str | None, kind: str, *, readonly: bool, eligible: bool = True,
           root_contract: str | None = None, operation_key: str | None = None):
    scope = _current()
    if scope is None:
        yield
        return
    parent = scope.cursor
    entered_from_current = _claimed(scope)
    if parent is None:
        allowed = (root_contract == scope.identity.contract and owner == scope.identity.owner
                   and operation_key == scope.identity.operation_key and kind == "commit")
    else:
        allowed = entered_from_current and parent.eligible and root_contract is None
    scope.sequence += 1
    frame = _Frame(scope.sequence, owner, kind, readonly or bool(parent and parent.readonly),
                   eligible and allowed and ctx is scope.ctx)
    scope.cursor = frame
    token = _claim.set((scope, frame.number))
    try:
        yield
    finally:
        # Only normal core unwinding restores a still-live parent; saved copies
        # retain the exited unique number and cannot become current again.
        if scope.cursor is frame:
            scope.cursor = parent
        else:
            scope.active = False
        _claim.reset(token)


def _wrap_contract(fn, owner: str, contract: str, kind: str):
    original = getattr(fn, "_approval_scope_original", fn)
    @wraps(original)
    def observed(ctx, *args, **kwargs):
        operation_key = (args[1] if len(args) > 1 else kwargs.get("key")) if kind == "commit" else None
        with _frame(ctx, owner, kind, readonly=kind != "commit",
                    root_contract=contract if kind == "commit" else None, operation_key=operation_key):
            return original(ctx, *args, **kwargs)
    observed._approval_scope_original = original
    return observed


def _wrap_metric(fn, owner: str):
    original = getattr(fn, "_approval_scope_original", fn)
    @wraps(original)
    def observed(ctx, *args, **kwargs):
        with _frame(ctx, owner, "metric", readonly=True):
            return original(ctx, *args, **kwargs)
    observed._approval_scope_original = original
    return observed


@contextmanager
def _service(ctx, caller: str, owner: str, effect: str):
    scope = _current(ctx)
    caller_matches = bool(scope and scope.cursor and _claimed(scope) and scope.cursor.owner == caller)
    with _frame(ctx, owner, "service", readonly=effect != "write", eligible=caller_matches):
        yield


def _capture_check(ctx, key: str, fn) -> None:
    scope = _current(ctx)
    if scope is not None and key not in scope.checks:
        owner = scope.cursor.owner if _claimed(scope) else None
        scope.checks[key] = (fn, owner)


@contextmanager
def _check(ctx, key: str, fn):
    scope = _current(ctx)
    registered = scope.checks.get(key) if scope else None
    owner = registered[1] if registered and registered[0] is fn else None
    # Checks run after the commit frame exits. They cannot authorize writes,
    # but retain observational owner identity for nested reads/registration.
    with _frame(ctx, owner, "check", readonly=True, eligible=False):
        yield

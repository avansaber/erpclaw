"""Principals and grants, and the company's settings, changed as the owner only.

These run under the owner's credential (`init`, `erpclaw users`), never from
the long-running process: the runtime role may read principals and grants and
lock a principal's row, but not change either, so under its credential each
of these fails with permission denied. A grant change takes only the
principal's row FOR UPDATE and bumps its grants_version; cards are
invalidated lazily, so no card row is taken here.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select, update

from . import models as m
from .common import Refusal, audit, company_ctx, new_id, now
from .principals import OWNER, token_digest

OWNER_ONLY = True  # the runtime-policy scanner skips this module (tests/test_runtime_role.py)


def _locked(conn, name: str) -> dict:
    q = select(m.principal).where(m.principal.c.name == name)
    row = conn.execute(q.with_for_update()).mappings().first()
    if row is None:
        raise Refusal(f"There is no principal called {name}.", "no_principal")
    return dict(row)


def _check(permissions) -> list[str]:
    bad = sorted(set(permissions) - set(m.PERMISSIONS))
    if bad:
        raise Refusal(f"Unknown permission {bad[0]}; the permissions are {', '.join(m.PERMISSIONS)}.", "bad_permission")
    return sorted(set(permissions))


def ensure_owner(conn, company_id: str) -> str:
    """The owner principal (made with the first company), with every permission on this company."""
    row = conn.execute(select(m.principal.c.id).where(m.principal.c.name == OWNER)).first()
    pid = row.id if row else new_id()
    if row is None:
        conn.execute(m.principal.insert().values(id=pid, name=OWNER, kind="person", disabled=False, grants_version=1,
                                                 token_sha256=None, created_at=now()))
    have = set(conn.execute(select(m.principal_grant.c.permission).where(
        m.principal_grant.c.principal_id == pid, m.principal_grant.c.company_id == company_id)).scalars())
    for perm in m.PERMISSIONS:
        if perm not in have:
            conn.execute(m.principal_grant.insert().values(principal_id=pid, company_id=company_id, permission=perm))
    return pid


def issue_token(conn, name: str) -> str:
    """A new API token for the principal, returned once; only its SHA-256 is kept (the old one stops working)."""
    p = _locked(conn, name)
    token = secrets.token_urlsafe(32)
    conn.execute(update(m.principal).where(m.principal.c.id == p["id"]).values(token_sha256=token_digest(token)))
    return token


def add_principal(conn, name: str, permissions, company_ids: list[str]) -> str:
    """A new principal with the permissions on each company; returns its API token, shown once."""
    if conn.execute(select(m.principal.c.id).where(m.principal.c.name == name)).first():
        raise Refusal(f"A principal called {name} already exists.", "principal_exists")
    perms = _check(permissions)
    pid = new_id()
    conn.execute(m.principal.insert().values(id=pid, name=name, kind="person", disabled=False, grants_version=1,
                                             token_sha256=None, created_at=now()))
    for cid in company_ids:
        for perm in perms:
            conn.execute(m.principal_grant.insert().values(principal_id=pid, company_id=cid, permission=perm))
    return issue_token(conn, name)


def grant(conn, name: str, permissions, company_id: str) -> int:
    p = _locked(conn, name)
    g = m.principal_grant
    have = set(conn.execute(select(g.c.permission).where(g.c.principal_id == p["id"],
                                                         g.c.company_id == company_id)).scalars())
    for perm in _check(permissions):
        if perm not in have:
            conn.execute(g.insert().values(principal_id=p["id"], company_id=company_id, permission=perm))
    return _bump(conn, p)


def revoke(conn, name: str, permissions, company_id: str) -> int:
    p = _locked(conn, name)
    g = m.principal_grant
    conn.execute(g.delete().where(g.c.principal_id == p["id"], g.c.company_id == company_id,
                                  g.c.permission.in_(_check(permissions))))
    return _bump(conn, p)


def disable(conn, name: str) -> int:
    p = _locked(conn, name)
    conn.execute(update(m.principal).where(m.principal.c.id == p["id"]).values(disabled=True))
    return _bump(conn, p)


def _bump(conn, p: dict) -> int:
    """Every pending card this principal drafted becomes invalid (lazily, when next touched)."""
    version = p["grants_version"] + 1
    conn.execute(update(m.principal).where(m.principal.c.id == p["id"]).values(grants_version=version))
    return version


def set_date_order(conn, company_id: str, date_order: str, actor: str = OWNER) -> str | None:
    """How the owner writes figure dates ("MDY" or "DMY"), set or changed, with an audit row naming the old and new
    setting. It changes how dates in future requests are read, never a stored date. Returns the old setting."""
    if date_order not in m.DATE_ORDERS:
        raise Refusal(f"The date order is MDY (month first) or DMY (day first), not {date_order}.", "bad_date_order")
    ctx = company_ctx(conn, actor=actor, company_id=company_id)
    q = select(m.company.c.date_order).where(m.company.c.id == ctx.company_id)
    old = conn.execute(q.with_for_update()).scalar_one()
    if old != date_order:
        conn.execute(update(m.company).where(m.company.c.id == ctx.company_id).values(date_order=date_order))
        audit(ctx, "set_date_order", "company", ctx.company_id, detail={"from": old, "to": date_order})
    return old

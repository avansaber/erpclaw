"""Principals and their grants.

A principal is a person the engine knows; a grant is one permission on one
company (there is no global grant). The HTTP API authenticates every call by
the principal's bearer token, of which only a SHA-256 is stored. The embedded
CLI runs in-process as the owner principal: the OS account that runs the
engine is trusted. The model is never a principal.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import models as m
from .common import Refusal
from .ledger import _for_share

OWNER = "owner"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_digest(code: str) -> str:
    """Domain-separated SHA-256 of UTF-8 text; hash lookup gives no timing oracle on the code itself."""
    return hashlib.sha256(b"erpclaw:sign-in-code:v1\0" + code.encode("utf-8")).hexdigest()


def session_digest(secret: str) -> str:
    """Domain-separated SHA-256 of UTF-8 text; hash lookup gives no timing oracle on the secret itself."""
    return hashlib.sha256(b"erpclaw:session-secret:v1\0" + secret.encode("utf-8")).hexdigest()


def authenticate(conn: Connection, token: str | None) -> dict:
    """The principal a bearer token names; unknown, missing or disabled is refused."""
    if not token:
        raise Refusal("No API token was sent.", "unauthenticated")
    row = conn.execute(select(m.principal).where(m.principal.c.token_sha256 == token_digest(token))).mappings().first()
    if row is None or row["disabled"]:
        raise Refusal("That API token is not valid.", "unauthenticated")
    return dict(row)


def owner(conn: Connection) -> dict:
    row = conn.execute(select(m.principal).where(m.principal.c.name == OWNER)).mappings().first()
    if row is None:
        raise Refusal("These books have no owner principal; run `erpclaw init`.", "no_owner")
    return dict(row)


def by_id(conn: Connection, principal_id: str, *, lock: bool = False) -> dict:
    """A principal's row; with `lock`, FOR SHARE on PostgreSQL (the card commit's first lock)."""
    q = select(m.principal).where(m.principal.c.id == principal_id)
    row = conn.execute(_for_share(conn, q) if lock else q).mappings().first()
    if row is None:
        raise Refusal("That principal does not exist.", "no_principal")
    return dict(row)


def holds(conn: Connection, principal_id: str, company_id: str, *permissions: str) -> bool:
    """True when the principal holds every one of the permissions on the company."""
    g = m.principal_grant
    have = set(conn.execute(select(g.c.permission).where(
        g.c.principal_id == principal_id, g.c.company_id == company_id)).scalars())
    return set(permissions) <= have


def require(conn: Connection, principal: dict, company_id: str, *permissions: str) -> None:
    if principal["disabled"] or not holds(conn, principal["id"], company_id, *permissions):
        raise Refusal(f"You may not {' and '.join(permissions)} in this company.", "forbidden")

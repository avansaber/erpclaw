"""The authenticated caller and the API features this engine has built."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI
from sqlalchemy.engine import Engine

from erpclaw_assistant.model import Model
from erpclaw_core import models as m, principals as pr
from erpclaw_core.common import company_ctx
from erpclaw_core.db import read_only

FEATURES: list[str] = []


def add_feature(name: str) -> None:
    """Register a built feature once; names remain available throughout v1."""
    if name not in FEATURES:
        FEATURES.append(name)
        FEATURES.sort()


add_feature("turn_failure_codes")


def register_me(app: FastAPI, engine: Engine, model: Model, principal: Callable[..., dict]) -> None:
    @app.get("/v1/me")
    def me(who: dict = Depends(principal)):
        with read_only(engine) as conn:
            company = company_ctx(conn)
            permissions = [permission for permission in sorted(m.PERMISSIONS)
                           if pr.holds(conn, who["id"], company.company_id, permission)]
        return {
            "name": who["name"],
            "kind": who["kind"],
            "books": {"company": company.company_name, "currency": company.currency},
            "permissions": permissions,
            "model": model.name,
            "features": FEATURES.copy(),
            "principal_id": who["id"],
        }

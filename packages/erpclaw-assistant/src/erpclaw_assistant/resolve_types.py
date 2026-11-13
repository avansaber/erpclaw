"""Resolution types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import get_args
from pydantic import BaseModel
from sqlalchemy import select
from erpclaw_core import models as m
from erpclaw_core.common import Ctx


@dataclass(frozen=True)
class Found:
    name: str  # the stored name, raw
    start: int
    end: int



@dataclass(frozen=True)
class OutgoingAccountWords:
    start: int
    end: int
    unknown_extension: bool = False
    name: str | None = None



@dataclass
class Result:
    args: dict
    not_done: list[dict] = field(default_factory=list)
    future_invoice_date: bool = False
    internal: list[dict] = field(default_factory=list, repr=False, compare=False)



def marks(model: type[BaseModel]) -> dict[str, dict]:
    """The input model's field marks, in field order."""
    out = {}
    for name, f in model.model_fields.items():
        for md in f.metadata:
            if isinstance(md, dict) and "erpclaw" in md:
                out[name] = md["erpclaw"]
    return out



def _line_model(model: type[BaseModel], name: str) -> type[BaseModel]:
    annotation = model.model_fields[name].annotation
    # Delivery/credit lines may be omitted (the rest/everything); their row model is still marked.
    args = get_args(annotation)
    return next((a for a in args if isinstance(a, type) and issubclass(a, BaseModel)),
                next((b for a in args for b in get_args(a)
                      if isinstance(b, type) and issubclass(b, BaseModel)), None))



def _items(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id)).mappings()]



def reply_peers(calls: list[tuple[object, dict]]) -> dict[str, list]:
    """Every argument the reply's write calls give for a customer field and for each line's item, in order (one
    record per call)."""
    out: dict = {"customer": [], "item": [], "customer_calls": {}}  # customer_calls: by operation, for strictness
    for contract, args in calls:  # an argument left out is None: it counts, as the record it would resolve to
        for name, v in marks(contract.input_model).items():
            if v["kind"] == "customer":
                if args.get(name):
                    out["customer"].append(args[name])
                out["customer_calls"].setdefault(contract.name, []).append(args.get(name) or None)
            elif v["kind"] == "lines" and _lines_shape(args.get(name)):
                im = [n for n, fv in marks(_line_model(contract.input_model, name)).items() if fv["kind"] == "item"]
                for li in args.get(name) or [{}]:
                    out["item"] += [li.get(n) or None for n in im]
    return out



def _lines_shape(value) -> bool:
    """A lines value the step can read: absent, or a list of objects. Anything else is left to validation."""
    return value is None or (isinstance(value, list) and all(isinstance(li, dict) for li in value))



@dataclass(frozen=True)
class WarehouseWords:
    start: int
    end: int
    said: str
    item: str | None
    item_start: int | None



@dataclass
class Occurrence:
    """One recognised value phrase of the request: its kind, its words (start, end) and what it says."""
    kind: str  # figure, number, customer, item, document, date, rate, memo, draft, email, unclear
    start: int
    end: int
    said: str
    value: object = None

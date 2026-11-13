"""Resolution warehouse."""
from __future__ import annotations

import re
from datetime import date
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.masters import _norm
from erpclaw_core.plugins import call_service
from .resolve_text import _memo_phrases, table
from .resolve_words import _as_written, _raw_words, bounded_names, punct_after, words
from .resolve_types import Found, WarehouseWords
from .resolve_state import Session, _one_detail
from .resolve_dates import _ago, date_phrases


def _company_warehouse_names(ctx: Ctx) -> set[str]:
    try:
        rows = call_service(ctx, "assistant", "stock.list_warehouses", {})["warehouses"]
    except Refusal as refusal:
        if refusal.code != "plugin_not_enabled":
            raise
        return set()
    return {_norm(r["name"]) for r in rows}



def _trailing_date_start(text: str, start: int, end: int, today: date) -> int | None:
    """Drop leading words only while the date reader still reads the same trailing phrases."""
    reading, boundary = None, None
    for j in range(start, end):
        try:
            got = date_phrases(_as_written(text, (j, end)), today)
        except Refusal:
            continue
        if got and (reading is None or got == reading):
            reading, boundary = got, j
    if boundary is not None:
        t = table()
        joinable = t["date_touch"] | t["weekday_ahead"] | t["weekday_behind"] | \
            set(t["quantity"]) | set(t["scales"]) | {"on", "the"}
        ws = words(text)
        while boundary > start and ws[boundary - 1] in joinable:
            try:
                got = date_phrases(_as_written(text, (boundary - 1, end)), today)
            except Refusal:
                break
            if got != reading:
                break
            boundary -= 1
    return boundary



def warehouse_words(text: str, items: list[dict], custs: list[dict] = (),
                    ctx: Ctx | None = None) -> list[WarehouseWords]:
    """Shipment sources, matched against the company's warehouse names without party filtering."""
    return _warehouse_phrases(text, items, custs, ctx, table()["warehouse_from"])



def receipt_destination_phrases(text: str, items: list[dict], custs: list[dict] = (),
                                ctx: Ctx | None = None) -> list[WarehouseWords]:
    """A receipt's explicit destination; its supplier source is a separate field."""
    return _warehouse_phrases(text, items, custs, ctx, {"into", "to"})



def _warehouse_phrases(text: str, items: list[dict], custs: list[dict],
                       ctx: Ctx | None, cues: set[str] | frozenset[str]) -> list[WarehouseWords]:
    """A source phrase belongs to the nearest preceding item mention in its own sentence.
    Keep unknown stock sources; service sources must say warehouse or name one in the company.
    """
    t, ws, raw = table(), words(text), _raw_words(text)
    services = {r["name"] for r in items if r.get("kind") == "service"}
    warehouse_names = None
    today = ctx.today if ctx is not None else date.today()
    ago_runs = [(len(words(text[:mt.start()])), len(words(text[:mt.end()])))
                for mt in re.finditer(_ago(), text.lower())]
    found = bounded_names(ws, [r["name"] for r in items], item=True, bounded=False)
    protected = found + bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False)
    # Use the existing memo inventory's boundary; its owner text is not a stock-source instruction.
    for start, end, _ in _memo_phrases(text):
        protected.append(Found("memo", len(words(text[:start])), len(words(text[:end]))))
    breaks = punct_after(text)
    out = []
    for i, word in enumerate(ws):
        if word not in cues or any(f.start <= i < f.end for f in protected) or \
                any(p.start < i < p.end for p in out):
            continue
        before = [f for f in found if f.end <= i and
                  not any(re.search(r"[.;!?]$", raw[k]) for k in range(f.end - 1, i))]
        owner = max(before, key=lambda f: f.end) if before else None
        start = i + 1
        if start < len(ws) and ws[start] == "the":
            start += 1
        if start == len(ws):
            continue
        # As with the source reader, consult stock only for an actual warehouse phrase.
        if warehouse_names is None:
            warehouse_names = _company_warehouse_names(ctx) if ctx is not None else set()
        matches = [start + len(parts) for name in warehouse_names if (parts := words(name)) and
                   ws[start:start + len(parts)] == parts]
        longest_end = max(matches, default=start)
        end = start
        while end < len(ws):
            if end > start and (ws[end] in t["warehouse_stops"] or
                                (ws[end] in cues and end >= longest_end) or
                                (ws[end] in t["coordinators"] and end + 1 < len(ws) and
                                 not any(a <= end < b for a, b in ago_runs) and
                                 (ws[end + 1].isdigit() or ws[end + 1] in t["quantity"] or
                                  ws[end + 1] in t["write_verbs"]))):
                break
            end += 1
            if end - 1 in breaks and not (
                    raw[end - 1].endswith(".") and ws[end - 1] in t["months"] and
                    end < len(ws) and re.fullmatch(r"\d{1,2}", ws[end])):
                break
        if end == start:
            continue
        matched_end = max((j for j in matches if j <= end), default=start)
        boundary = _trailing_date_start(text, matched_end, end, today)
        if boundary is not None:
            reading = date_phrases(_as_written(text, (boundary, end)), today)
            while boundary > matched_end and ws[boundary - 1] in {"by", "at"}:
                try:
                    got = date_phrases(_as_written(text, (boundary - 1, end)), today)
                except Refusal:
                    break
                if got != reading:
                    break
                boundary -= 1
            end = boundary
        if end == start:
            continue
        said = _as_written(text, (start, end))
        # An unattached known warehouse must reach occurrence accounting, rather than disappear as a payer.
        if (owner is None or owner.name in services) and "warehouse" not in ws[start:end]:
            if _norm(said) not in warehouse_names:
                continue
        out.append(WarehouseWords(i, end, said,
                                  owner.name if owner else None, owner.start if owner else None))
    return out



def _warehouse(ctx: Ctx, item: dict, phrases: list[WarehouseWords],
               session: Session, path: str, mark: dict) -> str | None:
    if item.get("kind") != "stock" and session.bindings.get(path) is False:
        return None  # the owner's choice to omit this line's source; None remains "someone else"
    if path in session.bindings:
        said = session.bindings[path]
    elif len(phrases) == 1:
        said = phrases[0].said
    elif phrases:
        names = list(dict.fromkeys(p.said for p in phrases))
        raise _one_detail(path, session, "warehouses", names, "warehouse_ambiguous")
    else:
        return None  # a model's warehouse argument never supplies a source
    if not said:
        raise session.ask(path, "Which warehouse should this item come from?", "warehouse_ambiguous", [], [])
    if item.get("kind") != "stock":
        raise session.ask(path, f"{item['name']} is a service item; it cannot come from a warehouse.",
                          "warehouse_ambiguous", ["Invoice it without a warehouse"], [False])
    for msg in session.owner_messages:
        for span in msg["spans"]:
            if span["kind"] == "warehouse" and span.get("classification") == "name" and \
                    _norm(span["name"]) == "the " + _norm(said):
                said = span["name"]
                session.bindings[path] = said
    try:
        return mark["resolve"](ctx, item, said)["name"]
    except Refusal as refusal:
        if not refusal.code.startswith("warehouse_"):
            raise
        # Contract-provided references, never the decorated display labels, bind this exact line.
        choices = refusal.data.get("choices") or []
        raise session.ask(path, refusal.message, refusal.code,
                          [c["label"] for c in choices], [c.get("ref") for c in choices]) from None

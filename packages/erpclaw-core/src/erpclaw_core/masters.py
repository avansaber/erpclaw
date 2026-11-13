"""Customers, suppliers and items: resolution from the owner's words to records.

The model passes names exactly as the owner said them. The engine matches.
An unknown or ambiguous name never falls through to a guess.
"""
from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import func, select

from . import models as m
from .common import Ctx, Refusal, new_id
from .textkey import text_key

STOP = {"inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "ltd", "llc", "the", "company", "limited"}


def normalise_name(s: str) -> str:
    """Case-fold, remove punctuation and collapse runs of whitespace."""
    return " ".join(re.sub(r"[^\w\s]|_", "", s.casefold()).split())


_norm = normalise_name  # compatibility for existing core and plugin name readers


def _core_words(s: str) -> set[str]:
    return {w for w in _norm(s).split() if w not in STOP}


# A language's regular plural, as data: the first rule whose ending matches the singular applies; `drop` letters
# come off the end and `add` goes on. Only English ships; the logic below holds no English of its own.
PLURALS = {
    "en": [
        {"endings": ("s", "x", "z", "ch", "sh"), "drop": 0, "add": "es"},
        {"endings": ("by", "cy", "dy", "fy", "gy", "hy", "jy", "ky", "ly", "my", "ny", "py", "qy", "ry", "sy", "ty",
                     "vy", "wy", "xy", "zy"), "drop": 1, "add": "ies"},
        {"endings": ("",), "drop": 0, "add": "s"},
    ],
}
LANGUAGE = "en"


def regular_plural(word: str, rules: list[dict] | None = None) -> str:
    """The regular plural, generated from a singular by the language's rules, never guessed backwards from a plural
    (so "caps" and "capes" never meet)."""
    for rule in rules if rules is not None else PLURALS[LANGUAGE]:
        if word.endswith(rule["endings"]):
            return word[: len(word) - rule["drop"]] + rule["add"]
    return word


def same_word(a: str, b: str, rules: list[dict] | None = None) -> bool:
    """Equal, or one is exactly the other's generated plural."""
    return a == b or regular_plural(a, rules) == b or regular_plural(b, rules) == a


def _same_name(said: str, name: str, plural: bool) -> bool:
    """Every word equal, normalised; with `plural`, the final words may be a generated singular/plural pair."""
    s, n = _norm(said).split(), _norm(name).split()
    if not plural:
        return s == n
    return len(s) == len(n) > 0 and s[:-1] == n[:-1] and same_word(s[-1], n[-1])


def _words_within(want: list[str], have: list[str], plural: bool) -> bool:
    """Every word of `want` is a word of `have`; with `plural`, only the two final words may differ as a generated
    singular/plural pair ("glass cases" never meets "Glasses Case")."""
    rest = list(enumerate(have))  # (position in `have`, word): a plural pairs only the two names' real final words
    for i, w in enumerate(want):
        final = plural and i == len(want) - 1
        hit = next((p for p in rest if p[1] == w or (final and p[0] == len(have) - 1 and same_word(w, p[1]))), None)
        if hit is None:
            return False
        rest.remove(hit)
    return True


def _core_list(s: str) -> list[str]:
    return [w for w in _norm(s).split() if w not in STOP]


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _near(rows: list[dict], said: str, limit: int = 5, plural: bool = False) -> list[dict]:
    """At most `limit` records whose names are near what was said: most shared core words, then the smallest edit
    distance, then alphabetical; only those sharing a core word or within a third of the longer text."""
    want, s = _core_words(said), _norm(said)
    scored = []
    for r in rows:
        n = _norm(r["name"])
        have = _core_words(r["name"])
        shared = sum(1 for w in want if any(w == h or (plural and same_word(w, h)) for h in have))
        d = _distance(s, n)
        if shared or d <= max(len(s), len(n)) // 3:
            scored.append((-shared, d, r["name"], r))
    return [r for *_, r in sorted(scored, key=lambda x: x[:3])[:limit]]


def _references(rows: list[dict], said: str) -> list[dict]:
    """Customers whose stored email (one address said, any case) or city (normalised) is what was said."""
    text = said.strip().lower()
    if re.fullmatch(r"[^@\s]+@[^@\s]+", text):
        return [r for r in rows if (r.get("email") or "").lower() == text]
    return [r for r in rows if r.get("city") and _norm(r["city"]) == _norm(said)]


def _match(rows: list[dict], said: str, what: str, plural: bool = False, references: bool = False) -> dict:
    said_n = _norm(said)
    if not said_n:
        raise Refusal(f"Which {what}?", f"{what}_missing")
    exact = [r for r in rows if r["name"] == said]  # the stored name, raw: unique per company and kind
    if exact:
        return exact[0]
    named = [r for r in rows if _same_name(said, r["name"], plural)]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:  # names that normalise alike never resolve silently: only the raw name does
        raise Refusal(f"More than one {what} matches '{said}': {', '.join(sorted(r['name'] for r in named))}. "
                      "Which one?", f"{what}_ambiguous", options=[r["name"] for r in named])
    if references:  # an exact reference resolves: one customer's email, or a city only one customer has
        refs = _references(rows, said)
        if len(refs) == 1:
            return refs[0]
        if refs:
            raise Refusal(f"More than one {what} matches '{said}': {', '.join(sorted(r['name'] for r in refs))}. "
                          "Which one?", f"{what}_ambiguous", options=[r["name"] for r in refs])
    # A part of a name, or a name inside longer words, never resolves without the owner's answer: one candidate is
    # a confirm, several a question.
    want = _core_list(said)
    cands = [r for r in rows if want and _words_within(want, _core_list(r["name"]), plural)] or \
        [r for r in rows if (have := _core_list(r["name"])) and _words_within(have, want, plural)]
    if len(cands) == 1:
        raise Refusal(f"Do you mean {cands[0]['name']}?", f"{what}_ambiguous", options=[cands[0]["name"]],
                      confirm=True)
    if cands:
        names = ", ".join(sorted(r["name"] for r in cands))
        raise Refusal(f"More than one {what} matches '{said}': {names}. Which one?",
                      f"{what}_ambiguous", options=[r["name"] for r in cands])
    if not rows:
        raise Refusal(f"I can't find a {what} called '{said}'. There are no {what}s yet.", f"{what}_unknown",
                      options=[])
    near = _near(rows, said, plural=plural)
    if near:
        raise Refusal(f"I can't find a {what} called '{said}'. Did you mean: "
                      f"{', '.join(r['name'] for r in near)}?", f"{what}_unknown", options=[r["name"] for r in near])
    raise Refusal(f"I can't find a {what} called '{said}'. There are no {what}s with a similar name, and adding "
                  f"a {what} isn't available in chat yet.", f"{what}_unknown", options=[])


def customers(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(select(m.party).where(
        m.party.c.company_id == ctx.company_id, m.party.c.kind == "customer")).mappings()]


def find_customer(ctx: Ctx, said: str) -> dict:
    return _match(customers(ctx), said, "customer", references=True)


def suppliers(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(select(m.party).where(
        m.party.c.company_id == ctx.company_id, m.party.c.kind == "supplier")).mappings()]


def _filter_name_matches(needle: str, name: str) -> bool:
    words = _norm(name).split()
    compact = "".join(words)
    plural = "".join(words[:-1]) + regular_plural(words[-1]) if words else ""
    return bool(needle) and (needle in compact or needle == plural)


def master_page(ctx: Ctx, what: str, offset: int, limit: int,
                name_filter: str | None = None) -> tuple[int, list[dict]]:
    """Count and page this company's masters in raw-name and id order."""
    source = m.item if what == "item" else m.party
    conditions = [source.c.company_id == ctx.company_id]
    if what != "item":
        conditions.append(source.c.kind == what)
    if name_filter is not None:
        needle = _norm(name_filter).replace(" ", "")
        matches = (row for row in ctx.conn.execute(select(source).where(*conditions).order_by(
            func.convert_to(source.c.name, "UTF8"), func.convert_to(source.c.id, "UTF8"))).mappings()
                   if _filter_name_matches(needle, row["name"]))
        count, page = 0, []
        for row in matches:
            if offset <= count < offset + limit:
                page.append(dict(row))
            count += 1
        return count, page
    count = ctx.conn.execute(select(func.count()).select_from(source).where(*conditions)).scalar_one()
    rows = ctx.conn.execute(select(source).where(*conditions).order_by(
        func.convert_to(source.c.name, "UTF8"), func.convert_to(source.c.id, "UTF8"))
        .offset(offset).limit(limit)).mappings()
    return count, [dict(row) for row in rows]


def find_supplier(ctx: Ctx, said: str) -> dict:
    return _match(suppliers(ctx), said, "supplier", references=True)


def find_item(ctx: Ctx, said: str) -> dict:
    rows = [dict(r) for r in ctx.conn.execute(select(m.item).where(
        m.item.c.company_id == ctx.company_id)).mappings()]
    return _match(rows, said, "item", plural=True)


def customer_label(ctx: Ctx, customer: dict) -> str:
    """The customer's name, with its city (or email) when another customer's name is close: shares a core word,
    or is within two edits, so two similar names are told apart on a card."""
    words, n = _core_words(customer["name"]), _norm(customer["name"])
    close = any(r["id"] != customer["id"] and (words & _core_words(r["name"]) or _distance(n, _norm(r["name"])) <= 2)
                for r in customers(ctx))
    if not close:
        return customer["name"]
    where = customer.get("city") or customer.get("email")
    return f"{customer['name']} ({where})" if where else f"{customer['name']} (no city or email on file)"


def create_party(ctx: Ctx, kind: str, name: str, email: str | None = None, city: str | None = None) -> str:
    """Core writes its own master tables; plugins call this, never insert."""
    pid = new_id()
    ctx.conn.execute(m.party.insert().values(id=pid, company_id=ctx.company_id, kind=kind, name=name,
                                             email=email, city=city, city_key=text_key(city)))
    return pid


def mark_stock_item(ctx: Ctx, item_id: str) -> None:
    """The item is kept in stock from now on: its kind moves from service to stock, never back (the database refuses
    the way back). The stock plugin calls this when it makes an item a stock item; no plugin writes `item` itself."""
    res = ctx.conn.execute(m.item.update().where(m.item.c.company_id == ctx.company_id, m.item.c.id == item_id)
                           .values(kind="stock"))
    if res.rowcount != 1:
        raise Refusal(f"{ctx.company_name} has no such item.", "item_unknown", item=item_id)


def create_item(ctx: Ctx, name: str, standard_rate: Decimal, unit: str = "unit") -> str:
    iid = new_id()
    ctx.conn.execute(m.item.insert().values(id=iid, company_id=ctx.company_id, name=name, unit=unit,
                                            standard_rate=standard_rate))
    return iid

"""Resolution records."""
from __future__ import annotations

import re
from collections import Counter
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.masters import STOP, _near, _norm, customers, same_word
from .resolve_text import _say, table
from .resolve_words import _alternatives, _candidates, _collides, _core, _raw_in, _raw_words, bounded_names, punct_after, words
from .resolve_types import Found
from .resolve_amounts import _marks_at, _word_at, figures
from .resolve_state import Session


def _missing(what: str) -> str:
    return _say("customer_missing") if what == "customer" else _say("document_missing", what=what)


def _item_alternatives(ws: list[str], rows: list[dict]) -> list[Found]:
    """The adjacent item mentions connected by an 'or'."""
    found = sorted(bounded_names(ws, [r["name"] for r in rows], item=True, bounded=False),
                   key=lambda f: (f.start, f.end))
    group = []
    for left, right in zip(found, found[1:]):
        between = ws[left.end:right.start]
        if left.end <= right.start and any(w in table()["or"] for w in between) and "and" not in between:
            if not group:
                group.append(left)
            group.append(right)
        elif group:
            break
    return group



def _named_records(rows: list[dict], text: str, names: list[str]) -> list[str]:
    """Every customer the owner's words name exactly: a bounded full name, an email, or a city only one has."""
    out = set(names)
    for mail in re.findall(r"[^\s,;<>()]+@[^\s,;<>()]+\.[A-Za-z]+", text):
        out |= {r["name"] for r in rows if (r.get("email") or "").lower() == mail.lower()}
    joined = f" {' '.join(words(text))} "
    city = [r for r in rows if r.get("city") and _norm(r["city"]) and f" {_norm(r['city'])} " in joined]
    if len(city) == 1:
        out.add(city[0]["name"])
    return sorted(out)



def _pointed_elsewhere(rows: list[dict], ws: list[str], arg, chosen: str) -> list[str]:
    """The other records the model's argument names by the owner's own words, when it shares no word with the one
    bounded name found: that name is then never put in its place ("invoice Acme for 5 copper wire and Hooli for 5
    copper wire", called once with "Acme": Acme Corporation is asked about, not Hooli carded twice)."""
    said = {w for w in _norm(str(arg or "")).split() if w not in STOP} & set(ws)
    if not said or said & set(_norm(chosen).split()):
        return []
    return sorted(r["name"] for r in rows if r["name"] != chosen and said & set(_norm(r["name"]).split()))



def _owner_words_taken(ws: list[str], text: str, arg) -> str | None:
    """The longest run of the owner's words that the model's argument took, as the owner wrote them ("invoice Nobody",
    called with "Nobody At All": "Nobody"); None when it took none of them."""
    a = {w for w in _norm(str(arg or "")).split() if w not in STOP}
    best: list[str] = []
    cur: list[str] = []
    for w in ws:
        cur = cur + [w] if w in a else []
        if len(cur) > len(best):
            best = cur
    if not best:
        return None
    mt = re.search(r"\W+".join(re.escape(w) for w in best), text, re.I)
    return mt.group(0) if mt else " ".join(best)



def without_ruled_out(ctx: Ctx, text: str) -> str:
    """The owner's words without the customers they rule out: a negation word directly before a customer's name, or a
    core word of one, and that name. Anything else after the negation word stays ("do not submit" is the draft's)."""
    t, ws, raw = table(), words(text), _raw_words(text)
    names = [r["name"] for r in customers(ctx)]
    found = bounded_names(ws, names, item=False, bounded=False)
    core = {w for n in names for w in _core(n)}
    drop: set[int] = set()
    for i, w in enumerate(ws):
        if w in t["negation"] and i + 1 < len(ws):
            span = next(((f.start, f.end) for f in found if f.start == i + 1), None)
            if span is None and ws[i + 1] in core:
                span = (i + 1, i + 2)
            if span:
                drop.update(range(i, span[1]))
    return " ".join(tok for k, tok in enumerate(raw) if k not in drop) if drop else text



def _refers_to(rows: list[dict], named: list[str], arg) -> str | None:
    """The one named record the model's argument refers to: equal to its name or to its exact reference, else the one
    sharing the most name words with it (a stored name's filler, such as "Corp", aside); None when it refers to none
    or to several."""
    a = _norm(str(arg or "")).split()
    if not a:
        return None
    equal = [n for n in named if _norm(n).split() == a]
    if len(equal) == 1:
        return equal[0]
    refs = {r["name"] for r in rows if r["name"] in named
            for k in ("email", "city") if r.get(k) and _norm(r[k]).split() == a}
    if len(refs) == 1:
        return refs.pop()
    aw = [w for w in a if w not in STOP]
    shared = {n: sum(any(same_word(w, x) for x in aw) for w in set(_norm(n).split()) if w not in STOP) for n in named}
    top = max(shared.values())
    best = [n for n in named if shared[n] == top]
    return best[0] if top and len(best) == 1 else None



def _one_per_call(rows: list[dict], found: list[Found], named: list[str], arg, peers: list) -> str | None:
    """Several records named exactly (one record per call): each of the reply's calls takes the named record its
    argument refers to. It resolves only when every argument refers to one, every named record is taken, and none is
    taken by more calls than the owner wrote it; otherwise None, and the owner is asked."""
    mine = _refers_to(rows, named, arg)
    taken = [_refers_to(rows, named, p) for p in peers]
    if mine is None or None in taken:
        return None
    written = Counter(f.name for f in found)
    used = Counter(taken)
    if any(used[n] == 0 or used[n] > max(1, written[n]) for n in named):
        return None
    return mine



def resolve_record(rows: list[dict], what: str, text: str, arg, session: Session, path: str, *, item: bool,
                   peers: list | None = None, alike: list | None = None) -> str:
    """`_resolve_record`, then customers are strict (one record per call): no two calls of the same operation
    in a reply take the same customer, whether they name it, leave it out or give an exact reference."""
    got = _resolve_record(rows, what, text, arg, session, path, item=item, peers=peers)
    if item or not alike or len(alike) < 2:
        return got
    ws = words(text)
    named = _named_records(rows, text, sorted({f.name for f in bounded_names(ws, [r["name"] for r in rows],
                                                                               item=False, breaks=punct_after(text))}))
    taken = [a is None or _refers_to(rows, named or [got], a) == got
             or (len(named) <= 1 and not _refers_to(rows, named, a) and not _pointed_elsewhere(rows, ws, a, got))
             for a in alike]
    if sum(taken) > 1:
        raise session.ask(path, _say("twice", name=got), f"{what}_ambiguous", [], [])
    return got



def _resolve_record(rows: list[dict], what: str, text: str, arg, session: Session, path: str, *, item: bool,
                    peers: list | None = None) -> str:
    """The stored name, raw, of the customer or item the owner named; otherwise the field's question.

    `peers` are the arguments every call of the reply gives for a field of this kind (this one's included; for items,
    every line's). When the owner's words name several records exactly, each call takes the one its argument refers
    to, and the reply's calls must take each named record (one record per call); "or" or "either" between them
    always asks. One item named never takes more lines than the owner wrote it."""
    if path in session.bindings:
        if session.bindings[path] is not None:
            return session.bindings[path]
        raise session.ask(path, _missing(what), f"{what}_missing", [], [])
    ws = words(text)
    confirmed = {s["name"] for msg in session.owner_messages for s in msg["spans"]
                 if s["kind"] == what and s.get("classification") == "name"}
    named_whole = {f.name for f in bounded_names(ws, [r["name"] for r in rows], item=item, bounded=False)}
    if len(named_whole) == 1 and named_whole <= confirmed:
        return next(iter(named_whole))
    item_group = _item_alternatives(ws, rows) if item else []
    group_names = {f.name for f in item_group}
    selected = next((v for k, v in session.bindings.items()
                     if k.endswith('.item') and v in group_names), None) if item_group else None
    either = sorted(group_names) if item_group and not selected else ([] if item else _alternatives(ws, rows, item))
    if either:
        raise session.ask(path, _say("which", what=what, names=", ".join(either)), f"{what}_ambiguous", either, either)
    # A complete currency figure bounds a name just as a bare digit figure does. Check the original token:
    # normalisation alone would confuse US$100 with an alphanumeric id such as US100.
    figure_bounds = frozenset(_word_at(text, start) for _, start, end in figures(text)
                              if _marks_at(text, start, end))
    found = bounded_names(ws, [r["name"] for r in rows], item=item, breaks=punct_after(text),
                          figure_bounds=figure_bounds)
    if selected:
        excluded = {(f.name, f.start, f.end) for f in item_group if f.name != selected}
        found = [f for f in found if (f.name, f.start, f.end) not in excluded]
    names = sorted({f.name for f in found})
    named = names if item else _named_records(rows, text, names)
    chosen = None
    if len(named) > 1 and not all(_collides(n, named[0], item) for n in named):  # alike names: guard 1 below
        chosen = _one_per_call(rows, found, named, arg, [arg] if peers is None else peers)
        if chosen is None:
            raise session.ask(path, _say("which", what=what, names=", ".join(named)), f"{what}_ambiguous", named,
                              named)
    elif names:
        chosen = names[0]  # one name, or names that normalise alike: guard 1 below decides
    if chosen is not None:  # a stored name written in full but not bounded is a claimant: the owner says which
        held = {(f.name, f.start, f.end) for f in found}
        claimants = sorted({g.name for g in bounded_names(ws, [r["name"] for r in rows], item=item, bounded=False)
                            if (g.name, g.start, g.end) not in held and g.name != chosen
                            and not (selected and g in item_group)})
        if claimants:
            group = [chosen, *claimants]
            raise session.ask(path, _say("which", what=what, names=", ".join(group)), f"{what}_ambiguous", group, group)
    elsewhere = _pointed_elsewhere(rows, ws, arg, chosen) if chosen is not None else []
    if elsewhere:  # the model took other words of the owner's for this field: the owner says which
        group = [chosen, *elsewhere]
        raise session.ask(path, _say("which", what=what, names=", ".join(group)), f"{what}_ambiguous", group, group)
    if item and chosen is not None and len(named) <= 1:  # one item named: never on more lines than written
        taken = [p for p in ([arg] if peers is None else peers) if p is None or _refers_to(rows, [chosen], p)
                 or not _pointed_elsewhere(rows, ws, p, chosen)]
        if len(taken) > max(1, sum(f.name == chosen for f in found)):
            raise session.ask(path, _say("lines", name=chosen), f"{what}_ambiguous", [], [])
    if chosen is not None:  # guard 1: a name colliding with another stored name resolves only when written raw
        group = sorted({chosen, *(r["name"] for r in rows if _collides(r["name"], chosen, item))})
        if len(group) == 1:
            return chosen
        raw = [n for n in group if _raw_in(text, n)]
        if len(raw) == 1:
            return raw[0]
        raise session.ask(path, _say("which", what=what, names=", ".join(group)), f"{what}_ambiguous", group, group)
    if len(names) > 1:
        raise session.ask(path, _say("which", what=what, names=", ".join(names)), f"{what}_ambiguous", names, names)
    if what == "customer":  # an exact reference: one customer's email, or a city only one customer has
        for mail in re.findall(r"[^\s,;<>()]+@[^\s,;<>()]+\.[A-Za-z]+", text):
            hit = [r for r in rows if (r.get("email") or "").lower() == mail.lower()]
            if len(hit) == 1:
                return hit[0]["name"]
        joined = f" {' '.join(ws)} "
        city = [r for r in rows if r.get("city") and _norm(r["city"]) and f" {_norm(r['city'])} " in joined]
        if len(city) == 1:
            return city[0]["name"]
    said = str(arg or "").strip()
    if not (_norm(said) and f" {_norm(said)} " in f" {' '.join(ws)} "):
        said = _owner_words_taken(ws, text, arg) or ""  # only the owner's own words are ever quoted back
    if not rows and not (what == "customer" and said):
        raise session.ask(path, _say("no_records", what=what), f"{what}_unknown", [], [])
    # A model's grounded item phrase, when present, is the subject of a near-name question;
    # customer and instruction words elsewhere in the request are not item candidates.
    candidate_words = words(said) if item else ws
    cands = [r["name"] for r in _candidates(rows, candidate_words, item=item)]
    if item and not said and named_whole:
        cands = sorted(named_whole)
    if len(cands) == 1:
        rf = session.ask(path, _say("confirm", name=cands[0]), f"{what}_ambiguous",
                         [cands[0], _say("someone_else")], [cands[0], None], confirm=True)
        if item and said:
            session.pending["owner_phrase"] = said
        raise rf
    if cands:
        rf = session.ask(path, _say("which", what=what, names=", ".join(cands)), f"{what}_ambiguous", cands, cands)
        if item and said:
            session.pending["owner_phrase"] = said
        raise rf
    if said:
        if what == "customer":
            near = [r["name"] for r in _near(rows, said)]
            raise session.ask(path, f"{_say('unknown', what=what, said=said)} {_missing(what)}",
                              f"{what}_unknown", near, near)
        raise session.ask(path, _say("unknown", what=what, said=said), f"{what}_unknown", [], [])
    raise session.ask(path, _missing(what), f"{what}_missing", [], [])



def document_mentions(text: str, prefix: str, nouns: tuple[str, ...]) -> list[str]:
    """The owner's document numbers: a full number with the prefix ("INV-2026-00007"), or a bare number after the
    document's noun ("invoice 7", "invoice #7")."""
    out = [mt.group(0) for mt in re.finditer(rf"(?<![\w-]){prefix}-\d{{4}}-\d+(?![\w-])", text, re.I)]
    for noun in nouns:
        out += [mt.group(1) for mt in re.finditer(rf"\b{noun}\s*(?:no\.?|number)?\s*#?\s*(\d+)(?![\w-])", text, re.I)]
    return list(dict.fromkeys(out))



def _same_document(owner: str, arg) -> bool:
    o, a = re.findall(r"\d+", owner), re.findall(r"\d+", str(arg))
    if not o or not a or int(o[-1]) != int(a[-1]):
        return False
    return len(o) < 2 or len(a) < 2 or int(o[-2]) == int(a[-2])



def _alternative_documents(text: str, mentions: list[str]) -> bool:
    """"or" between two of the owner's document numbers, or "either" before them: the owner has not chosen."""
    t, low = table(), text.lower()
    places = sorted(m.start() for x in mentions for m in re.finditer(re.escape(x.lower()), low))
    if len(places) < 2:
        return False
    for mt in re.finditer(r"\b\w+\b", low):
        if (mt.group(0) in t["or"] and places[0] < mt.start() < places[-1]) or \
                (mt.group(0) in t["either"] and mt.start() < places[-1]):
            return True
    return False



def _canonical_document(ctx: Ctx, v: dict, said: str) -> str:
    """The stored number for one of the owner's mentions, when it names exactly one document; else the mention."""
    try:
        return v["resolve"](ctx, said)
    except Refusal:
        return said



def _document(ctx: Ctx, v: dict, name: str, presence: str, out: dict, text: str, session: Session,
              path: str | None = None, *, choose_written: bool = True) -> None:
    """An invoice or payment number: the owner's own number, made canonical by the plugin's resolver."""
    path = path or name
    if session.bindings.get(path):
        out[name] = session.bindings[path]
        return
    mentions = document_mentions(text, v["prefix"], tuple(v.get("nouns", ())))
    arg = out.pop(name, None)
    if len(mentions) > 1 and _alternative_documents(text, mentions):  # "either INV-3 or INV-6": the owner chooses
        raise session.ask(path, _say("which", what=v["what"], names=", ".join(mentions)), f"{v['what']}_ambiguous",
                          mentions, [{name: _canonical_document(ctx, v, m)} for m in mentions])
    if arg and not choose_written:
        try:
            v["resolve"](ctx, arg)
        except Refusal:
            # Keep the base's omission of an invalid model reference. The owner's document
            # remains in inventory and must still be claimed or asked about before a card.
            mentions = [x for x in mentions if _same_document(x, arg)]
    if choose_written and arg:
        mentions = [x for x in mentions if _same_document(x, arg)]
    if len(mentions) == 1:
        out[name] = v["resolve"](ctx, mentions[0])
    elif len(mentions) > 1:
        raise session.ask(path, _say("which", what=v["what"], names=", ".join(mentions)), f"{v['what']}_ambiguous",
                          mentions, [{name: _canonical_document(ctx, v, m)} for m in mentions])
    elif presence != "optional":  # a number the owner did not write is never used
        raise session.ask(path, _missing(v["what"]), f"{v['what']}_missing", [], [])

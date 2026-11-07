"""The capability catalogue and request outcomes.

The catalogue is generated, never kept by hand: the contracts and metrics of
the plugins installed in these books (installed), those of first-party plugins
these books have not installed (available), and the planned product areas in
`planned.json`, which is generated from the parity ledger outside the engine.
It also holds what ERPClaw never offers (unsupported), so such a request is
answered with the reason and the way to get the same result.
`find_capability` is a deterministic, local lexical search. The model picks one
candidate, none, or unsure; the engine decides the outcome, and the replies to
a planned, available, unsupported, unmatched or unsure request are composed
here from fixed text, so no figure can appear in them. Each refusal names the
catalogue id of what it refused (`Decision.capability`).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Connection

from ..common import check_capability
from ..contracts import FIGURES_ID, figures_planned_text, planned_capability, planned_reply, planned_text
from ..plugins import REGISTRY, enabled_plugins, load_plugins

PLANNED = Path(__file__).with_name("planned.json")
MAX_CANDIDATES = 5
MAX_CHOICES = 3
STOP = frozenset("a an the of for to in on at by and or me my our us we i you is are was be it this that what "
                 "how much many do does did can could please show tell give get from".split())
STATE_RANK = {"installed": 0, "available": 0, "planned": 1, "unsupported": 1}
NO_MATCH = "unsupported.no_match"  # outcome d with no entry: the request matched nothing ERPClaw has or plans
SETUP = "set_up_company"  # the one operation of books not set up yet: never something to ask for instead


@dataclass(frozen=True)
class Unsupported:
    """Something ERPClaw never offers, by design: its reply gives the reason and the way to the same result, and
    `remedy` names the operation that way runs through, so the reply can say when it is not switched on."""
    id: str
    name: str
    synonyms: tuple[str, ...]
    reply: str
    remedy: str


UNSUPPORTED = (
    Unsupported("unsupported.edit_posted", "editing a posted document in place",
                ("edit", "amend", "modify", "posted invoice", "edit invoice", "change invoice", "change posted",
                 "edit posted", "correct invoice", "rewrite invoice"),
                "A posted document can't be edited in place: the books never change a posted entry. To correct an "
                "invoice, cancel it and issue a new one.", "cancel_invoice"),
)


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str  # operation | metric | area
    name: str
    description: str
    synonyms: tuple[str, ...]
    state: str  # installed | available | planned | unsupported
    plugin: str | None = None
    wave: str | None = None
    target: str | None = None  # the contract or metric name an installed entry runs
    reply: str = ""  # an unsupported entry's fixed reply, for these books; a planned area's own answer, if any
    instead_of: tuple[str, ...] = ()  # a planned area's: the operations it answers in place of, when its words are said


def capability(entry: Entry) -> str:
    """The catalogue id a refusal of this entry carries: planned.*, unsupported.* or available.*."""
    if entry.state == "unsupported" or entry.kind == "area":  # their own ids are planned.* and unsupported.*
        return entry.id
    if entry.state == "planned":
        if entry.kind == "metric":
            return FIGURES_ID
        return planned_capability(REGISTRY.contracts[entry.target])
    return f"{entry.state}.{entry.id}"


def _on(contract, enabled: set[str]) -> bool:
    return REGISTRY.contract_plugin[contract.name] in enabled and not contract.planned


def stock_item_creation_on(enabled: set[str]) -> bool:
    """The item kind question and catalogue share the registered stock setup's availability."""
    contract = REGISTRY.contracts.get("add_item")
    return (contract is not None and _on(contract, enabled) and "stock" in enabled
            and "stock.make_stock_item" in REGISTRY.services)


def switched_on_names(conn: Connection) -> list[str]:
    """The plain names of the operations switched on in these books, in registry order, each once."""
    load_plugins()
    enabled = enabled_plugins(conn)
    return list(dict.fromkeys(c.plain or c.name.replace("_", " ") for c in REGISTRY.contracts.values()
                              if _on(c, enabled) and c.name != SETUP))


def switched_on(conn: Connection) -> str:
    """One sentence listing what is switched on in these books, from the enabled operations' plain names (no figure
    can appear in it): what the owner can ask for instead."""
    names = switched_on_names(conn)
    if not names:
        return "Nothing is switched on in these books yet."
    listed = names[0] if len(names) == 1 else (" and ".join(names) if len(names) == 2
                                               else ", ".join(names[:-1]) + ", and " + names[-1])
    return f"Right now I can {listed}."


def _unsupported_reply(u: Unsupported, enabled: set[str]) -> str:
    remedy = REGISTRY.contracts.get(u.remedy)
    if remedy is None or _on(remedy, enabled):
        return u.reply
    if remedy.planned:
        return f"{u.reply} {planned_reply(remedy)}"
    name = remedy.planned_name or remedy.name.replace("_", " ")
    return f"{u.reply} {name[0].upper()}{name[1:]} is not switched on in these books."


def words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [w for w in re.split(r"[^a-z0-9]+", folded) if w and w not in STOP]


def _planned() -> list[dict]:
    return json.loads(PLANNED.read_text()) if PLANNED.exists() else []


def catalogue(conn: Connection) -> list[Entry]:
    """Every entry for these books, sorted by id."""
    load_plugins()
    enabled = enabled_plugins(conn)
    stock_on = stock_item_creation_on(enabled)
    planned = _planned()
    stock_synonyms = tuple(s for a in planned if a["id"] == "planned.stock_item" for s in a["synonyms"])
    out: list[Entry] = []
    for c in REGISTRY.contracts.values():
        if c.name == "get_metric":  # metrics are entries of their own; get_metric is how they run
            continue
        plugin = REGISTRY.contract_plugin[c.name]
        synonyms = tuple(c.synonyms) + (stock_synonyms if c.name == "add_item" and stock_on else ())
        out.append(Entry(f"op.{c.name}", "operation", c.name.replace("_", " "), c.description, synonyms,
                         "planned" if c.planned else "installed" if plugin in enabled else "available", plugin,
                         wave=c.planned, target=c.name))
    for mt in REGISTRY.metrics.values():
        plugin = REGISTRY.metric_plugin[mt.name]
        out.append(Entry(f"metric.{mt.name}", "metric", mt.title, mt.definition, tuple(mt.synonyms),
                         "planned" if mt.planned else "installed" if plugin in enabled else "available", plugin,
                         wave=mt.planned, target=mt.name))
    for a in planned:
        if a["id"] == "planned.stock_item" and stock_on:
            continue  # these requests now go through add_item
        if a["installed_as"] and all(p in enabled for p in a["installed_as"]):
            continue  # delivered by plugins installed in these books
        out.append(Entry(a["id"], "area", a["name"], a["name"], tuple(a["synonyms"]), "planned", wave=a["wave"],
                         reply=a.get("reply", ""), instead_of=tuple(a.get("instead_of", ()))))
    for u in UNSUPPORTED:
        out.append(Entry(u.id, "operation", u.name, u.name, u.synonyms, "unsupported",
                         reply=_unsupported_reply(u, enabled)))
    return sorted(out, key=lambda e: e.id)


def _synonym_score(entry: Entry, request: list[str]) -> int:
    """The part of the score the entry's synonyms give: a whole synonym phrase 3, a one-word synonym 2."""
    text = " " + " ".join(request) + " "
    req = set(request)
    score = 0
    for syn in entry.synonyms:
        sw = words(syn)
        if len(sw) > 1 and f" {' '.join(sw)} " in text:
            score += 3
        elif len(sw) == 1 and sw[0] in req:
            score += 2
    return score


def _score(entry: Entry, request: list[str]) -> int:
    req = set(request)
    return _synonym_score(entry, request) + len(req & set(words(entry.name + " " + entry.description)))


def named_by_synonym(conn: Connection, said: str) -> list[tuple[Entry, int]]:
    """The entries the owner's own words name by one of their synonyms, ranked as `find_capability` ranks: words
    shared only with a name or description never count, since they are what makes unrelated candidates appear."""
    req = words(said)
    hits = [(e, _synonym_score(e, req)) for e in catalogue(conn)]
    hits = [(e, sc) for e, sc in hits if sc > 0]
    hits.sort(key=lambda es: (STATE_RANK[es[0].state], -es[1], es[0].id))
    return hits


def planned_instead(conn: Connection, operation: str, said: str) -> Decision | None:
    """The planned answer for a request whose own words name, by one of its synonyms, a planned area declared in place
    of this operation while it is off: the operation never runs for it. None otherwise."""
    if not any(operation in a.get("instead_of", ()) for a in _planned()):
        return None  # nothing is planned in its place: the catalogue is not read
    for e, _ in named_by_synonym(conn, said):
        if e.state == "planned" and operation in e.instead_of:
            return decide([e], e.id)
    return None


def find_capability(conn: Connection, request: str) -> list[Entry]:
    """At most five candidates: entries the owner's words name by a synonym first, then installed and available,
    then planned, by score, then id, so description-word matches never push a synonym-named entry out of the five."""
    req = words(request)
    scored = [(e, _score(e, req), _synonym_score(e, req) > 0) for e in catalogue(conn)]
    hits = [(e, s, named) for e, s, named in scored if s > 0]
    hits.sort(key=lambda h: (not h[2], STATE_RANK[h[0].state], -h[1], h[0].id))
    return [e for e, _, _ in hits[:MAX_CANDIDATES]]


@dataclass(frozen=True)
class Decision:
    outcome: str  # a | c | d | e (b lands when installs go through cards)
    entry: Entry | None
    text: str | None  # the fixed reply for c, d and e; None for a, which runs
    choices: tuple[str, ...] = ()
    capability: str | None = None  # the catalogue id a refusal (b, c or d) names; one cannot be made without it

    def __post_init__(self):
        if self.outcome in ("b", "c", "d"):
            check_capability(self.capability)


def decide(candidates: list[Entry], pick: str, on_now: str = "",
           named: list[tuple[Entry, int]] | None = None) -> Decision:
    """The engine's outcome for the model's pick among the candidates. `on_now` is the sentence listing what is
    switched on in these books (`switched_on`), which a reply to a request ERPClaw does not do ends with. `named`
    are the entries the owner's own words name by a synonym (`named_by_synonym`): a "none" pick over one that ERPClaw
    lacks gives that entry's own reply; over one installed entry, "none" stands."""
    tail = f" {on_now}" if on_now else ""
    if pick == "none" and named:
        first, top = named[0]
        tied = [e for e, sc in named if sc == top and STATE_RANK[e.state] == STATE_RANK[first.state]]
        if len(tied) > 1:  # several named alike: the owner says which
            return decide(tied, "unsure", on_now, [(e, top) for e in tied])
        if first.state != "installed":  # an installed one is never run from "none": "none" stands
            return decide([first], first.id, on_now)
    if pick == "none":
        return Decision("d", None, "ERPClaw does not do that yet. If you need it, just say so: requests like this "
                                   "are recorded, so it is clear what to build next." + tail, capability=NO_MATCH)
    chosen = next((e for e in candidates if e.id == pick), None)
    if pick == "unsure" or chosen is None:
        return _unsure(candidates, on_now, named)
    if chosen.state == "installed":
        return Decision("a", chosen, None)
    if chosen.state == "available":
        return Decision("c", chosen, f"That needs the {chosen.plugin} plugin, which is available but not installed "
                                     "in these books.", capability=capability(chosen))
    if chosen.state == "unsupported":
        return Decision("d", chosen, chosen.reply + tail, capability=capability(chosen))
    if chosen.kind == "metric":  # a figure is named as figures and reports, never by its own title
        text = figures_planned_text(chosen.wave)
    elif chosen.kind == "operation":
        text = planned_reply(REGISTRY.contracts[chosen.target])
    else:  # an area's own planned answer, if it has one
        text = chosen.reply or planned_text(chosen.name, chosen.wave)
    return Decision("c", chosen, text, capability=capability(chosen))


def _unsure(candidates: list[Entry], on_now: str, named: list[tuple[Entry, int]] | None) -> Decision:
    """No single pick: the entries the owner's own words name decide first (tied on score, whatever their states),
    unless that is one installed entry, else the search's candidates. One that ERPClaw lacks gives its own outcome;
    several that none can do yet each say so; otherwise a choice, never of one."""
    top = max((sc for _, sc in named), default=0) if named else 0  # the owner's strongest words, whatever the states
    pool = [e for e, sc in named if sc == top] if named else []
    if len(pool) == 1 and pool[0].state == "installed":
        pool = []  # one installed entry never decides alone: the search's candidates, as with nothing named
    named = named if pool else None
    pool = pool or list(candidates)
    if len(pool) == 1:
        if pool[0].state == "installed":
            pool = []  # never a choice of one: nothing to choose between
        else:
            return decide(pool, pool[0].id, on_now)
    if named and not any(e.state == "installed" for e in pool):
        parts = [decide([e], e.id) for e in pool]
        tail = f" {on_now}" if on_now and any(p.outcome == "d" for p in parts) else ""
        outcome = "d" if all(p.outcome == "d" for p in parts) else "c"
        lead = next(p for p in parts if p.outcome == outcome)
        return Decision(outcome, lead.entry, " ".join(p.text for p in parts) + tail, capability=lead.capability)
    names = tuple(e.name for e in pool[:MAX_CHOICES])
    if not names:  # nothing to choose between: ask, and claim nothing about support or demand
        return Decision("e", None, "I'm not sure what you would like me to do. Could you say it another way?")
    return Decision("e", None, "Which did you mean? " + "; ".join(f"({i}) {n}" for i, n in enumerate(names, 1)) + ".",
                    names)

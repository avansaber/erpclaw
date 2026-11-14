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
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.engine import Connection

from ..common import Refusal, check_capability
from ..contracts import FIGURES_ID, figures_planned_text, planned_capability, planned_reply, planned_text
from ..plugins import REGISTRY, enabled_plugins, load_plugins

PLANNED = Path(__file__).with_name("planned.json")
OVERRIDES = Path(__file__).with_name("overrides.json")
MAX_CANDIDATES = 5
MAX_CHOICES = 3
STOP = frozenset("a an the of for to in on at by and or me my our us we i you is are was be it this that what "
                 "how much many do does did can could please show tell give get from".split())
STATE_RANK = {"installed": 0, "available": 0, "planned": 1, "unsupported": 1}
NO_MATCH = "unsupported.no_match"  # outcome d with no entry: the request matched nothing ERPClaw has or plans
SETUP = "set_up_company"  # the one operation of books not set up yet: never something to ask for instead
ONE_CHANGE = "One change at a time, please: send each change as its own message."
# Purchasing exclusions for detecting two intents, independent of ownership
# exclusions such as 'invoice the customer', which is positive billing evidence.
MIXED_EXCLUSIONS = ("purchase order", "order", "quote", "request")


def __getattr__(name: str):
    # Core contracts imports this catalogue; resolve its text binding after initialization.
    if name == "say":
        from ..core_contracts import say
        globals()[name] = say
        return say
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    overrides: tuple[str, ...] = ()  # request-specific bindings from trusted data, set only by synonym lookup
    except_phrases: tuple[str, ...] = ()  # supported phrases belonging to a separate, as yet unregistered scope
    unresolved_area: Entry | None = None  # area to use when a bound planned operation was not picked


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


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    # Use the same noun for plurals and possessives in synonym and intent checks.
    folded = re.sub(r"\b(supplier|vendor)(?:['’]s|s['’]?)?(?![a-z0-9])", r"\1", folded)
    return re.findall(r"[a-z0-9]+", folded)


def words(text: str) -> list[str]:
    return [w for w in _tokens(text) if w not in STOP]


def _planned_data() -> dict:
    data = json.loads(PLANNED.read_text())
    policy = data.get("none_refusal") if isinstance(data, dict) else None
    if (not isinstance(policy, dict) or
            any(type(policy.get(key)) is not int or policy[key] < minimum
                for key, minimum in (("min_gap", 1), ("min_ratio", 1)))):
        raise ValueError("catalogue none_refusal requires positive integer min_gap and min_ratio")
    if not isinstance(data.get("entries"), list):
        raise ValueError("catalogue entries must be a list")
    return data


def _planned() -> list[dict]:
    return _planned_data()["entries"]


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
                         reply=a.get("reply", ""), instead_of=tuple(a.get("instead_of", ())),
                         except_phrases=tuple(a.get("except_phrases", ()))))
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
    if entry.id == "planned.invoice_fields":
        # Currency-rate phrases must not match the invoice area's description
        # through "rate" alone. Its explicit synonyms still use the full request.
        req = set(re.sub(r"\b(?:exchange|fx) rates?\b", "", " ".join(request)).split())
    return _synonym_score(entry, request) + len(req & set(words(entry.name + " " + entry.description)))


def _customer_billing(tokens: list[str], payment_intent: set[str]) -> bool:
    """Recognize billing verbs and invoice creation without resolving customer names."""
    determiners = {"a", "an", "the", "this", "that", "my", "our", "your", "their"}
    non_customer = {"supplier", "vendor", "for", "from", "to", "of", "and",
                    "or", "with", "on", "in", "at", "by", "is", "was", "has", "that", "which"}
    for i, token in enumerate(tokens):
        if token not in {"bill", "invoice"}:
            continue
        preceding, following = tokens[:i], tokens[i + 1:]
        if preceding and preceding[-1] in {"new", "another", "customer"}:
            preceding = preceding[:-1]
        if preceding and preceding[-1] in determiners:
            preceding = preceding[:-1]
            # 'send Acme an invoice': the recipient precedes the document noun.
            send_at = max((j for j, word in enumerate(preceding) if word == "send"), default=-1)
            recipient = preceding[send_at + 1:]
            if send_at >= 0 and recipient and not set(recipient) & (non_customer | determiners | payment_intent):
                return True
        if (preceding and preceding[-1] in {"create", "make", "send", "raise", "draft"}
                and following and following[0] in {"for", "to"}):
            # 'create an invoice for Acme' is customer billing even though
            # 'invoice' itself is a noun. Supplier/vendor recipients stay out.
            recipient = following[1:]
            if recipient and recipient[0] in determiners:
                recipient = recipient[1:]
            if recipient and recipient[0] not in non_customer:
                return True
        if i and tokens[i - 1] in ({"supplier", "vendor"} | determiners | payment_intent):
            continue  # a noun: 'pay the supplier invoice INV-7', not the verb 'invoice Acme'
        if following and following[0] in determiners:
            following = following[1:]
        if following and following[0] not in non_customer:
            return True
    return False


def is_mixed(original: str) -> bool:
    """Both supplier-payment intent and customer billing in the owner's original message.

    The binding's triggers and payment words remain authoritative. Its ownership
    exclusions are separate: customer billing must not veto detection of both.
    """
    req, tokens = words(original), _tokens(original)
    for record in json.loads(OVERRIDES.read_text()):
        if record["area"] != "planned.purchasing":
            continue
        intent = set(record.get("intent", ()))
        trigger = Entry("", "area", "", "", tuple(record["triggers"]), "planned")
        excluded = replace(trigger, synonyms=MIXED_EXCLUSIONS)
        return bool(_synonym_score(trigger, req) and intent.intersection(req)
                    and not _synonym_score(excluded, req) and _customer_billing(tokens, intent))
    return False


def _bindings(entries: list[Entry], said: str) -> Iterator[tuple[Entry, int]]:
    """Bind overlap words to their delivering entry only for the declared intent.

    An inactive binding retains the planned reply and protects it from unrelated
    operations sharing the words. No registry entry, state or arguments are made
    here. Customer billing and purchasing intent veto supplier-payment intent,
    including messages carrying both intents.
    """
    by_id = {e.id: e for e in entries}
    req = words(said)
    # Keep stop words here: 'bill for Copperline' is not the verb 'bill Copperline'.
    tokens = _tokens(said)
    records = json.loads(OVERRIDES.read_text())
    for record in records:
        area = by_id.get(record["area"])
        if area is None or area.state != "planned":
            continue
        followed = set(record.get("triggers_followed_by_token", ()))
        trigger = replace(area, synonyms=tuple(t for t in record["triggers"] if t not in followed))
        score = _synonym_score(trigger, req)
        for phrase in followed:
            phrase_tokens = _tokens(phrase)
            if phrase_tokens and any(tokens[i:i + len(phrase_tokens)] == phrase_tokens
                                     and i + len(phrase_tokens) < len(tokens) for i in range(len(tokens))):
                score = max(score, 3)
        if not score or (record.get("intent") and not set(record["intent"]) & set(req)):
            continue
        if not set(record.get("require", ())) <= set(req):
            continue
        excluded = replace(area, synonyms=tuple(record.get("exclude", ())))
        if _synonym_score(excluded, req):
            continue
        if record.get("exclude_customer_billing") and _customer_billing(tokens, set(record.get("intent", ()))):
            continue
        target = by_id.get(record["target"])
        protected = tuple(record.get("protect_from", ()))
        if target is not None and target.state in record["states"]:
            preserve_area = record.get("preserve_area_pick", False)
            overrides = protected if preserve_area else (area.id, *protected)
            yield replace(target, overrides=tuple(dict.fromkeys(overrides)),
                          unresolved_area=area if preserve_area else None), record.get("score", score)
        else:
            yield replace(area, overrides=protected), score


def _in_scope(entries: list[Entry], said: str) -> list[Entry]:
    req = words(said)
    return [e for e in entries if not e.except_phrases or
            not _synonym_score(replace(e, synonyms=e.except_phrases), req)]


def _named(entries: list[Entry], said: str) -> list[tuple[Entry, int]]:
    req = words(said)
    entries = _in_scope(entries, said)
    hits = {e.id: (e, sc) for e in entries if (sc := _synonym_score(e, req)) > 0}
    for entry, score in _bindings(entries, said):
        hits[entry.id] = (entry, max(score, hits.get(entry.id, (entry, 0))[1]))
    hits = list(hits.values())
    hits.sort(key=lambda es: (STATE_RANK[es[0].state], -es[1], es[0].id))
    return hits


def named_by_synonym(conn: Connection, said: str) -> list[tuple[Entry, int]]:
    """Entries named by synonyms or an intent-qualified binding of those synonyms.

    Name/description words never count. Colliding lexical matches remain in the
    inventory; request-specific bindings tell `decide` which reply owns them.
    """
    return _named(catalogue(conn), said)


def unshared_unavailable(named: list[tuple[Entry, int]], said: str) -> Entry | None:
    """Return the uniquely strongest unavailable entry with unshared binding evidence."""
    def singular(word: str) -> str:
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes", "uses")):
            return word[:-2]
        if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            return word[:-1]
        return word

    request = words(said)
    folded_request = [singular(word) for word in request]
    tokens = _tokens(said)
    records = json.loads(OVERRIDES.read_text())

    def evidence(entry: Entry, *, fold_request: bool) -> set[tuple[str, ...]]:
        result = set()
        synonyms = [(synonym, False) for synonym in entry.synonyms]
        for record in records:
            protected = tuple(record.get("protect_from", ()))
            target_overrides = protected if record.get("preserve_area_pick", False) else (
                record["area"], *protected)
            target_overrides = tuple(dict.fromkeys(target_overrides))
            bound = (entry.id == record["area"] and entry.overrides == protected or
                     entry.id == record["target"] and entry.overrides == target_overrides)
            if not bound or not entry.overrides:
                continue
            followed = set(record.get("triggers_followed_by_token", ()))
            for trigger in record["triggers"]:
                if trigger in followed:
                    phrase_tokens = _tokens(trigger)
                    if not any(tokens[i:i + len(phrase_tokens)] == phrase_tokens and
                               i + len(phrase_tokens) < len(tokens) for i in range(len(tokens))):
                        continue
                synonyms.append((trigger, True))
        for synonym, qualified in synonyms:
            phrase = words(synonym)
            if not phrase:
                continue
            source = folded_request if fold_request else request
            if fold_request:
                phrase = [singular(word) for word in phrase]
            if (len(phrase) == 1 and phrase[0] in source or
                    len(phrase) > 1 and any(source[i:i + len(phrase)] == phrase
                                            for i in range(len(source) - len(phrase) + 1))):
                # Qualified triggers can distinguish intent with stop words,
                # as in "payment to". Ordinary synonyms bind using words().
                evidence_words = _tokens(synonym) if qualified else words(synonym)
                result.add(tuple(singular(word) for word in evidence_words))
        return result

    shared = set().union(*(evidence(entry, fold_request=True) for entry, _ in named
                           if entry.state == "installed"))
    qualifying = [(entry, score) for entry, score in named if entry.state in ("planned", "unsupported")
                  and evidence(entry, fold_request=False) - shared]
    if not qualifying:
        return None
    top = max(score for _, score in qualifying)
    winners = [entry for entry, score in qualifying if score == top]
    return winners[0] if len(winners) == 1 else None


def planned_instead(conn: Connection, operation: str, said: str, *, mixed: bool | None = None) -> Decision | None:
    """The planned answer for a request whose own words name, by one of its synonyms, a planned area declared in place
    of this operation while it is off. A bound available target gives its availability reply; an installed target
    requires its own call, never transferring arguments from an unrelated operation. None otherwise."""
    mixed = is_mixed(said) if mixed is None else mixed
    if mixed:
        return decide([], "unsure", mixed=mixed)
    synonyms = tuple(s for r in json.loads(OVERRIDES.read_text())
                     if f"op.{operation}" in r.get("protect_from", ()) for s in r["triggers"])
    synonyms += tuple(s for a in _planned() if operation in a.get("instead_of", ()) for s in a["synonyms"])
    # Only the static synonyms can make this guard relevant. Read the books for
    # activation state and intent-qualified bindings only after one is named.
    possible = Entry("", "area", "", "", synonyms, "planned")
    if not _synonym_score(possible, words(said)):
        return None
    for e, _ in named_by_synonym(conn, said):
        if f"op.{operation}" in e.overrides and e.state in ("installed", "available"):
            if e.state == "available":
                return decide([e], e.id)
            raise Refusal(f"Please choose {e.name} for this request.", "capability_mismatch", options=[e.name])
        if e.state == "planned" and (operation in e.instead_of or f"op.{operation}" in e.overrides):
            return decide([e], e.id)
    return None


def find_capability(conn: Connection, request: str) -> list[Entry]:
    """At most five candidates: entries the owner's words name by a synonym first, then installed and available,
    then planned, by score, then id, so description-word matches never push a synonym-named entry out of the five."""
    req = words(request)
    entries = _in_scope(catalogue(conn), request)
    named = {e.id: (e, sc) for e, sc in _named(entries, request)}
    # A singular master followed by its name is a read request, even when the
    # name also matches other catalogue entries and crowds the list out.
    tokens = _tokens(request)
    match = re.match(r"^(?:(?:please|kindly) |(?:can|could|would) you (?:please )?)?"
                     r"(?:show|list|find|view|display|get)(?: me)?(?: my| our| the| a)? "
                     r"(customer|supplier|item)(?: |$)", " ".join(tokens))
    if match:
        wanted = f"op.list_{match.group(1)}s"
        entry = next((e for e in entries if e.id == wanted), None)
        if entry is not None:
            named[wanted] = (entry, 1000)
    scored = [(named.get(e.id, (e, 0))[0], max(_score(e, req), named.get(e.id, (e, 0))[1]), e.id in named)
              for e in entries]
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
           named: list[tuple[Entry, int]] | None = None, *, mixed: bool = False) -> Decision:
    """The engine's outcome for the model's pick among the candidates. `on_now` is the sentence listing what is
    switched on in these books (`switched_on`), which a reply to a request ERPClaw does not do ends with. `named`
    are the entries the owner's own words name by a synonym (`named_by_synonym`): a "none" pick over one that ERPClaw
    lacks gives that entry's own reply; over one installed entry, "none" stands with an honest reply.
    `mixed` is evaluated on the original message before any candidate filtering."""
    if mixed:
        return _mixed_reply()
    chosen = next((e for e in candidates if e.id == pick), None)
    if named and (pick in ("none", "unsure") or chosen is None):
        # A binding may specify an area for unresolved choices while its
        # operation is planned. Keep its operation for direct-call guards.
        resolved = {}
        for entry, score in named:
            if entry.state == "planned" and entry.unresolved_area is not None:
                entry = replace(entry.unresolved_area, overrides=entry.overrides)
            if entry.id not in resolved or score > resolved[entry.id][1]:
                resolved[entry.id] = (entry, score)
        named = list(resolved.values())
    if named:
        shadowed = {entry_id for e, _ in named for entry_id in e.overrides}
        named = [(e, score) for e, score in named if e.id not in shadowed]
    tail = f" {on_now}" if on_now else ""
    if pick == "none" and named:
        first, top = named[0]
        tied = [e for e, sc in named if sc == top and STATE_RANK[e.state] == STATE_RANK[first.state]]
        if len(tied) > 1:  # several named alike: the owner says which
            return decide(tied, "unsure", on_now, [(e, top) for e in tied], mixed=mixed)
        if first.state != "installed":  # an installed one is never run from "none": "none" stands
            return decide([first], first.id, on_now, mixed=mixed)
        refusals = [(entry, score) for entry, score in named if entry.state in ("unsupported", "planned")]
        if refusals:
            strongest, score = max(refusals, key=lambda item: item[1])
            runner_up = max((other_score for entry, other_score in named if entry is not strongest), default=0)
            policy = _planned_data()["none_refusal"]
            if score - runner_up >= policy["min_gap"] and score >= policy["min_ratio"] * runner_up:
                return decide([strongest], strongest.id, on_now, mixed=mixed)
        from ..core_contracts import say

        if len(named) == 1:
            contract = REGISTRY.contracts.get(first.target) if first.kind == "operation" else None
            name = contract.plain if contract and contract.plain else first.name
            action = f"see {name}" if first.kind == "metric" else name
            next_step = say("none_installed_action", action=action)
        else:
            next_step = say("none_installed_multiple")
        return Decision("d", None, f"{say('none_installed_not_done')} {next_step}", capability=NO_MATCH)
    if pick == "none":
        return Decision("d", None, "ERPClaw does not do that yet. If you need it, just say so: requests like this "
                                   "are recorded, so it is clear what to build next." + tail, capability=NO_MATCH)
    if pick == "unsure" or chosen is None:
        return _unsure(candidates, on_now, named, mixed=mixed)
    for preferred, _ in named or ():
        if chosen.id in preferred.overrides:
            # A model pick and the bound target must both be real candidates.
            # Never run an invented id or manufacture arguments for a redirect.
            target = next((e for e in candidates if e.id == preferred.id), None)
            if target is None:
                return _unsure(candidates, on_now, named, mixed=mixed)
            return decide([target], target.id, on_now, mixed=mixed)
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


def _mixed_reply() -> Decision:
    """The purchasing area's existing reply followed by the one-change sentence."""
    area = next(a for a in _planned() if a["id"] == "planned.purchasing")
    entry = Entry(area["id"], "area", area["name"], area["name"], tuple(area["synonyms"]), "planned",
                  wave=area["wave"], reply=area.get("reply", ""))
    planned = decide([entry], entry.id)
    return replace(planned, text=f"{planned.text} {ONE_CHANGE}")


def _unsure(candidates: list[Entry], on_now: str, named: list[tuple[Entry, int]] | None,
            *, mixed: bool = False) -> Decision:
    """No single pick: the entries the owner's own words name decide first (tied on score, whatever their states),
    unless that is one installed entry, else the search's candidates. One that ERPClaw lacks gives its own outcome;
    several that none can do yet each say so; otherwise a choice, never of one."""
    if mixed:
        return _mixed_reply()
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
            return decide(pool, pool[0].id, on_now, mixed=mixed)
    if named and not any(e.state == "installed" for e in pool):
        parts = [decide([e], e.id, mixed=mixed) for e in pool]
        tail = f" {on_now}" if on_now and any(p.outcome == "d" for p in parts) else ""
        outcome = "d" if all(p.outcome == "d" for p in parts) else "c"
        lead = next(p for p in parts if p.outcome == outcome)
        return Decision(outcome, lead.entry, " ".join(p.text for p in parts) + tail, capability=lead.capability)
    names = tuple(e.name for e in pool[:MAX_CHOICES])
    if not names:  # nothing to choose between: ask, and claim nothing about support or demand
        return Decision("e", None, "I'm not sure what you would like me to do. Could you say it another way?")
    return Decision("e", None, "Which did you mean? " + "; ".join(f"({i}) {n}" for i, n in enumerate(names, 1)) + ".",
                    names)

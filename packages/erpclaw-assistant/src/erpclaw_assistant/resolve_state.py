"""Resolution state."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from erpclaw_core.common import Refusal
from erpclaw_core.masters import _norm
from .resolve_text import _say, table
from .resolve_words import _occurrence_ranges, words
from .resolve_types import Occurrence


class Planned(Refusal):
    """A part the engine will not do that would change the money written: the whole request gets the planned reply,
    no card."""

    def __init__(self, message: str, route: str):
        super().__init__(message, "planned", capability=route, route=route)
        self.route = route



@dataclass
class Session:
    """The current request's questions and answers. `serial` numbers questions across the conversation, so a code is
    never reused; the pending question and the bindings end with the request (the turn loop carries this between
    turns and ends it)."""
    serial: int = 0
    pending: dict | None = None  # {"id", "field", "choices": [{"code", "label", "ref"}]}
    bindings: dict = field(default_factory=dict)  # field path -> canonical reference (None: "someone else")

    owner_messages: list[dict] = field(default_factory=list)
    offers: dict = field(default_factory=dict, repr=False, compare=False)

    def offer(self, path: str, kind: str, text: str, occurrences: list[Occurrence]) -> None:
        """Keep a field's offered occurrences through a question raised by its preview."""
        self.offers[path] = {"kind": kind, "spans": [[o.start, o.end] for o in occurrences],
                             "chars": _occurrence_ranges(text, occurrences)}

    def answer(self, code: str) -> bool:
        """Bind the pending question's field by a choice's full code; any other code binds nothing. A choice whose
        reference names fields ({"invoice": ...} or {"oldest_first": true}) binds each of them."""
        if not self.pending:
            return False
        for c in self.pending["choices"]:
            if c["code"] == code:
                if self.pending.get("span_id"):
                    for msg in self.owner_messages:
                        for span in msg["spans"]:
                            if span["id"] == self.pending["span_id"]:
                                span["classification"] = c["ref"]
                elif isinstance(c["ref"], dict):  # each field it names, within the question's own write
                    scope = self.pending["field"].rpartition(":")[0]
                    self.bindings.update({f"{scope}:{k}" if scope else k: val for k, val in c["ref"].items()})
                else:
                    if self.pending["field"].rpartition(":")[2] == "memo_date" and c["ref"] is True:
                        # The newly classified date needs its own answer, not an earlier date's ruling.
                        scope = self.pending["field"][:-len("memo_date")]
                        self.bindings.pop(f"{scope}date", None)
                    self.bindings[self.pending["field"]] = c["ref"]
                    if c["ref"] is not None and self.pending.get("owner_phrase"):
                        self.bindings["item_phrase:" + self.pending["field"]] = self.pending["owner_phrase"]
                if self.pending.get("payment_kind"):
                    self.bindings["settled:" + self.pending["field"]] = {
                        "kind": self.pending["payment_kind"], "spans": self.pending.get("payment_spans", []),
                        "chars": self.pending.get("payment_chars", []), "through": self.owner_messages[-1]["id"] if self.owner_messages else None}
                self.pending = None
                return True
        return False

    def record_free_answer(self, path: str, occurrences: list[Occurrence] | None = None) -> None:
        """Keep the question identity when a resolver consumes a free answer's value."""
        if self.owner_messages and self.pending and self.pending["field"] == path and not self.pending["choices"]:
            self.owner_messages[-1].update(question_id=self.pending["id"], field=path)
            if occurrences is not None:
                self.bindings["settled:" + path] = {"kind": "method",
                    "spans": [[o.start, o.end] for o in occurrences],
                    "chars": self.offers.get(path, {}).get("chars", []), "through": self.owner_messages[-1]["id"]}

    def control(self, name: str) -> None:
        """A control action at the pending question: "someone_else" leaves its field bound to no candidate, so the
        full question is asked next; the question itself is answered."""
        if self.pending and name == "someone_else":
            self.bindings[self.pending["field"]] = None
            self.pending = None

    def end(self) -> None:
        self.pending, self.bindings = None, {}
        self.offers.clear()

    def dump(self) -> dict:
        """Plain data, for the turn's saved state; the references stay in the engine's state, never in an event."""
        return {"serial": self.serial, "pending": self.pending, "bindings": dict(self.bindings),
                **({"owner_messages": self.owner_messages} if self.owner_messages else {})}

    @classmethod
    def load(cls, data: dict | None) -> "Session":
        data = data or {}
        return cls(int(data.get("serial", 0)), data.get("pending"), dict(data.get("bindings") or {}),
                   list(data.get("owner_messages") or []))

    def ask(self, path: str, text: str, code: str, labels: list[str], refs: list, *,
            occurrences: list[Occurrence] | None = None, **data) -> Refusal:
        """The field's question: its id, and one single-use code per choice; the event carries no reference."""
        self.serial += 1
        qid = f"q{self.serial}"
        choices = [{"code": f"{qid}-{i}", "label": label, "ref": ref}
                   for i, (label, ref) in enumerate(zip(labels, refs), 1)]
        self.pending = {"id": qid, "field": path, "choices": choices}
        if code.startswith(("account_", "method_")):
            offer = self.offers.get(path, {})
            offered_spans = [[o.start, o.end] for o in occurrences] if occurrences is not None else offer.get("spans", [])
            self.pending.update(payment_kind=offer.get("kind", "method" if code.startswith("method_") else "account"),
                                payment_spans=offered_spans, payment_chars=offer.get("chars", []))
        return Refusal(text, code, options=list(labels), field=path, question_id=qid,
                       choices=[{"code": c["code"], "label": c["label"]} for c in choices], **data)



CONTROLS = ("someone_else", "cancel")  # the control actions, coded "<question id>-<name>"



def _acknowledgements(said: str, entries: frozenset[str]) -> bool:
    """Whether the whole message is a run of acknowledgements ("ok thanks"), each entry a unit, nothing left over."""
    one = "|".join(re.escape(e) for e in sorted(entries, key=len, reverse=True))
    return re.fullmatch(rf"(?:{one})(?: (?:{one}))*", said) is not None



def keeps_card(text: str) -> bool:
    """With a card pending and the model answering in text, whether the owner's message leaves the card approvable:
    the whole message is acknowledgements, or an approval word with only filler beside it. Anything else ("ok, but
    make it Wayne") is re-proposed by the engine. Commas and full stops count as spaces."""
    said = " ".join(re.sub(r"[,.]", " ", text.lower()).split()).strip(" !?")
    t = table()
    if _acknowledgements(said, t["answers"]["acknowledge"]):
        return True
    rest = re.sub(t["approval_words"], " ", said)
    if rest == said:
        return False
    words = [w.strip("!?;:'\"()") for w in rest.split()]  # every word counts, a figure or a reference too
    return not [w for w in words if w and w not in t["answers"]["approval_filler"]]



def match_answer(pending: dict | None, text: str, choice: str | None = None,
                 card_pending: bool = False) -> tuple[str, str] | None:
    """The owner's message as an answer to the pending question, before the model runs: ("choice", code) to bind, or
    ("control", name) for a control action; None when it answers nothing (the question stays and the message goes to
    the model as the owner's next words). In order: a choice code the client sent; a control action by its words; a
    label exactly as displayed (ignoring case and surrounding spaces); the one label equal after normalisation; a
    position, except at a free question (no choices), where a number is the field's answer. With no question pending
    but a card, only the words of "cancel" match: the request ends and its card is declined."""
    said = " ".join(text.strip().lower().split()).strip(" .!?")
    if not pending:
        return ("control", "cancel") if card_pending and said in table()["answers"]["cancel"] else None
    choices = pending.get("choices") or []
    if pending.get("span_id"):
        if choice is not None:
            if any(c["code"] == choice for c in choices):
                return ("choice", choice)
            return ("control", "cancel") if choice == f"{pending['id']}-cancel" else None
        if said in table()["answers"]["cancel"]:
            return ("control", "cancel")
        exact = [c for c in choices if c["label"].strip().casefold() == text.strip().casefold()]
        return ("choice", exact[0]["code"]) if len(exact) == 1 else None
    if choice is not None:
        if any(c["code"] == choice for c in choices):
            return ("choice", choice)
        return next((("control", name) for name in CONTROLS if choice == f"{pending['id']}-{name}"), None)
    t = table()["answers"]
    if said in t["cancel"]:
        return ("control", "cancel")
    if said in t["someone_else"]:
        return ("control", "someone_else")
    named = [c for c in choices if c.get("ref") is not None]  # a confirm: one candidate, then "someone else"
    if said in t["yes"] and len(named) == 1:
        return ("choice", named[0]["code"])
    exact = [c for c in choices if c["label"].strip().lower() == said]
    if len(exact) == 1:
        return ("choice", exact[0]["code"])
    normal = [c for c in choices if _norm(c["label"]) and _norm(c["label"]) == _norm(said)]
    if len(normal) == 1:
        return ("choice", normal[0]["code"])
    # Warehouse labels include on-hand quantities; their explicit references are still the owner's answer.
    if pending.get("field", "").rsplit(".", 1)[-1] == "warehouse":
        refs = [c for c in choices if isinstance(c.get("ref"), str) and _norm(c["ref"]) == _norm(said)]
        if len(refs) == 1:
            return ("choice", refs[0]["code"])
    if choices and said in t["positions"]:
        k = t["positions"][said]
        if k == -1:
            return ("choice", choices[-1]["code"])
        if 1 <= k <= len(choices):
            return ("choice", choices[k - 1]["code"])
    numbered = re.fullmatch(r"(\d+)\s*[-:.)]\s*(.+)", said)
    if numbered and 1 <= int(numbered[1]) <= len(choices):
        selected = choices[int(numbered[1]) - 1]
        if _norm(numbered[2]) == _norm(selected["label"]):
            return ("choice", selected["code"])
    return None



def _one_detail(path: str, session: Session, kind: str, names: list[str], code: str,
                occurrences: list[Occurrence] | None = None):
    """Two or more of one kind written: the owner picks one, and nothing is drafted until then."""
    count = table()["text"]["counts"].get(len(names), str(len(names)))
    return session.ask(path, _say("one_detail", count=count, kind=kind, names=", ".join(names)), code, names, names,
                       occurrences=occurrences)

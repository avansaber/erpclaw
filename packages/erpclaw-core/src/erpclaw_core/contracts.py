"""Typed contracts: one per operation.

A contract's Pydantic input model is the single source of:
  - the tool schema the model sees (business fields only; the engine fills ids,
    rates, accounts and company), and
  - the approval card the owner sees (built by `preview` from engine values).
Reads `run`; writes `preview` then `commit` under an operation key.

A service is not a contract: another plugin calls it through core's
`call_service`, inside the caller's transaction. It is never offered to the
model, listed in the catalogue or reached from the API.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import KW_ONLY, asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_serializer, field_validator, model_validator
from typing_extensions import Annotated

from .common import Ctx
from .money import MINOR_UNITS, fmt, fmt_rate, quantize, to_decimal


def __getattr__(name: str):
    # Core contracts imports these types; resolve its text binding after initialization.
    if name == "say":
        from .core_contracts import say
        globals()[name] = say
        return say
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


Effect = Literal["read", "draft", "write", "destructive"]  # unclassified fails closed


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ServiceModel(BaseModel):
    """A base for a service's input and output models: unknown fields are refused. Decimals cross as canonical
    strings; core refuses a float anywhere in what crosses."""
    model_config = ConfigDict(extra="forbid", frozen=True)


def _exact(v):
    if isinstance(v, (float, bool)):
        raise ValueError("an amount must be exact (text or integer), never floating point")
    return v


CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


class Money(ServiceModel):
    """An amount with its currency, as it crosses a service: {"value": "140.00", "currency": "USD"}. The value is
    within the currency's minor units; whether the currency is one of the books' is checked where it crosses."""
    value: Annotated[Decimal, BeforeValidator(_exact)]
    currency: str

    @field_validator("currency")
    @classmethod
    def _code(cls, v: str) -> str:
        if not CURRENCY_CODE.match(v):
            raise ValueError("a currency is three upper-case letters")
        return v

    @model_validator(mode="after")
    def _scale(self):
        if not self.value.is_finite():
            raise ValueError("an amount must be finite")
        places = MINOR_UNITS.get(self.currency)
        if places is not None and self.value != self.value.quantize(Decimal(1).scaleb(-places)):
            raise ValueError(f"more than {places} decimal places for {self.currency}")
        return self

    @field_serializer("value")
    def _text(self, v: Decimal) -> str:
        places = MINOR_UNITS.get(self.currency)
        return format(v if places is None else v.quantize(Decimal(1).scaleb(-places)), "f")


# Every name a result's or an answer's amount may carry; a contract declares the ones it returns.
AMOUNT_WHAT = ("total", "amount", "applied", "credit", "outstanding", "invoice_total", "invoice_outstanding", "value",
               "row", "reopened", "credit_removed", "rate")
CARD_STRINGS = ("title", "lines", "books", "warnings", "footer")
CARD_TITLE_MAX = 200
CARD_LINES_MAX, CARD_LABEL_MAX, CARD_VALUE_MAX = 100, 120, 500
CARD_BOOKS_MAX, CARD_BOOK_MAX = 50, 300
CARD_WARNINGS_MAX, CARD_WARNING_MAX = 50, 300
CARD_FOOTER_MAX = 500
CARD_BYTES_MAX = 65_536


@dataclass(frozen=True)
class CardAmount:
    """One money figure a card string shows: the string it is in, the figure's kind and its exact value."""
    where: str
    index: int
    label: str
    kind: str  # "amount" at the currency's scale, or "unit_price" as stored
    value: str
    currency: str


@dataclass(frozen=True)
class Amount:
    """One money figure a result's or an answer's text shows, named by its contract."""
    what: str
    of: str | None
    value: str
    currency: str
    kind: str = "amount"  # the same kinds as CardAmount


class Shown:
    """The one place a card's or a result's money is formatted. `amount` and `unit_price` return exactly the text
    `money.fmt` and `money.fmt_rate` return and record the figure; each string the card is built from takes the
    figures recorded since the string before it, in order. A result's figures are recorded with their name, so the
    strings and the amounts come from the same call."""

    def __init__(self, currency: str):
        self.currency = currency
        self.amounts: list[Amount] = []
        self._pending: list[tuple[str, str]] = []
        self._title, self._footer = "", ""
        self._lines: list[tuple[str, str]] = []
        self._books: list[str] = []
        self._warnings: list[str] = []
        self._card: list[CardAmount] = []

    def amount(self, value, what: str | None = None, of: str | None = None) -> str:
        """A money figure at the currency's scale; with `what`, a figure of a result's text."""
        if what is not None:
            self.amounts.append(Amount(what, of, format(quantize(value, self.currency), "f"), self.currency))
        else:
            self._pending.append(("amount", format(quantize(value, self.currency), "f")))
        return fmt(value, self.currency)

    def unit_price(self, value, what: str | None = None, of: str | None = None) -> str:
        """An item's price per unit, as stored (up to four places): every place it has, and at least the currency's,
        as the card reads it; with `what`, a named figure of a result's text."""
        shown = fmt_rate(value, self.currency)
        if what is not None:
            self.amounts.append(Amount(what, of, shown.replace(",", ""), self.currency, kind="unit_price"))
        else:
            self._pending.append(("unit_price", shown.replace(",", "")))
        return shown

    def _attach(self, where: str, index: int, label: str = "") -> None:
        self._card += [CardAmount(where, index, label, k, v, self.currency) for k, v in self._pending]
        self._pending = []

    def title(self, text: str) -> str:
        self._attach("title", 0)
        self._title = text
        return text

    def line(self, label: str, text: str) -> None:
        self._attach("lines", len(self._lines), label)
        self._lines.append((label, text))

    def book(self, text: str) -> None:
        self._attach("books", len(self._books))
        self._books.append(text)

    def warning(self, text: str) -> None:
        self._attach("warnings", len(self._warnings))
        self._warnings.append(text)

    def footer(self, text: str) -> None:
        self._attach("footer", 0)
        self._footer = text

    def card(self) -> Card:
        if self._pending:  # a figure formatted and never shown: a programming error, never a card
            raise RuntimeError(f"{len(self._pending)} money figure(s) recorded without a card string")
        return Card(self._title, list(self._lines), list(self._books), list(self._warnings), self._footer,
                    self.currency, list(self._card))


@dataclass
class Card:
    """What the owner approves. Every value comes from the engine. `amounts` names each money figure its strings
    show; `currency` is the books' functional currency."""
    title: str
    lines: list[tuple[str, str]]
    books: list[str]
    warnings: list[str] = field(default_factory=list)
    footer: str = ""
    currency: str = ""
    amounts: list[CardAmount] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"title": self.title, "lines": [list(x) for x in self.lines], "books": self.books,
                "warnings": self.warnings, "footer": self.footer, "currency": self.currency,
                "amounts": [asdict(a) for a in self.amounts]}


def _figure(text: str) -> re.Pattern:
    """The figure as a whole number in a string: never a part of a longer one ("40.00" is not in "140.00")."""
    return re.compile(rf"(?<![\d.,]){re.escape(text)}(?![\d]|[.,]\d)")


def _shown(kind: str, value: str, currency: str) -> str | None:
    """How a recorded figure reads in its string, or None when the figure is not one a string can show."""
    if currency not in MINOR_UNITS:
        return None
    try:
        d = to_decimal(value)
    except ValueError:
        return None
    if kind == "amount":
        return fmt(d, currency) if d == quantize(d, currency) else None  # an amount is at the currency's scale
    if kind == "unit_price":
        return fmt_rate(d, currency)
    return None


def _in_order(text: str, figures: list[str]) -> bool:
    at = 0
    for f in figures:
        hit = _figure(f).search(text, at)
        if hit is None:
            return False
        at = hit.end()
    return True


def check_card_bounds(card: dict) -> None:
    """Bound the complete card as stored: Unicode characters per string and compact UTF-8 JSON bytes overall."""
    from .common import Refusal

    lines, books, warnings = card["lines"], card["books"], card["warnings"]
    if (len(card["title"]) > CARD_TITLE_MAX or len(card["footer"]) > CARD_FOOTER_MAX
            or len(lines) > CARD_LINES_MAX
            or any(len(k) > CARD_LABEL_MAX or len(v) > CARD_VALUE_MAX for k, v in lines)
            or len(books) > CARD_BOOKS_MAX or any(len(b) > CARD_BOOK_MAX for b in books)
            or len(warnings) > CARD_WARNINGS_MAX or any(len(w) > CARD_WARNING_MAX for w in warnings)
            or len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > CARD_BYTES_MAX):
        raise Refusal("That card is too large to show for approval; nothing was proposed.", "card_too_large")


def check_card_amounts(card: dict) -> None:
    """Every amount points at an existing string of the card, and its value, as its kind reads, appears in that
    string in order of appearance. Bounds apply here after the engine adds its own strings, before any card is
    stored or shown. It checks the amounts that are there; that none is missing is the recorder's."""
    from .common import Refusal

    check_card_bounds(card)
    wrong = Refusal("That card's amounts do not match its lines; nothing was proposed.", "card_amounts")
    strings: dict[tuple[str, int], tuple[str, str]] = {("title", 0): ("", card["title"]),
                                                       ("footer", 0): ("", card["footer"])}
    strings |= {("lines", i): (k, v) for i, (k, v) in enumerate(card["lines"])}
    strings |= {(w, i): ("", s) for w in ("books", "warnings") for i, s in enumerate(card[w])}
    wanted: dict[tuple[str, int], list[str]] = {}
    for a in card.get("amounts", []):
        key = (a.get("where"), a.get("index"))
        shown = _shown(a.get("kind"), a.get("value"), a.get("currency"))
        if key not in strings or strings[key][0] != a.get("label") or shown is None:
            raise wrong
        wanted.setdefault(key, []).append(shown)
    if not all(_in_order(strings[key][1], figures) for key, figures in wanted.items()):
        raise wrong


def check_amount_names(contract: "Contract", amounts: list[Amount]) -> None:
    """A result or an answer names only amounts its contract declares, from the core list."""
    from .common import Refusal

    if any(a.what not in AMOUNT_WHAT or a.what not in contract.amounts for a in amounts):
        raise Refusal("That answer named an amount it does not declare; nothing was shown.", "amount_name")


def check_answer(contract: "Contract", result: Result) -> list[dict]:
    """A read's answer: its amounts named as its contract declares, and each one in its text, in order. The amounts
    as they cross."""
    from .common import Refusal

    check_amount_names(contract, result.amounts)
    at = 0
    for a in result.amounts:
        shown = _shown(a.kind, a.value, a.currency)
        # A stored unit price may retain trailing zeros and omit grouping. Both
        # representations carry precisely the same value, without rounding.
        variants = [shown] if shown is not None else []
        if shown is not None and a.kind == "unit_price":
            variants.append(a.value)
        hits = [hit for value in variants if (hit := _figure(value).search(result.text, at))]
        if not hits:
            raise Refusal("That answer's amounts do not match its text; nothing was shown.", "answer_amounts")
        at = min(hits, key=lambda hit: hit.start()).end()
    return amount_dicts(result.amounts)


def amount_dicts(amounts: list[Amount]) -> list[dict]:
    """Keep the original amount shape; only a non-default kind needs to cross."""
    return [{k: v for k, v in asdict(a).items() if k != "kind" or v != "amount"} for a in amounts]


@dataclass
class Preview:
    card: Card
    state_token: str  # changes when anything the write depends on changes


@dataclass
class Result:
    text: str                     # owner-facing answer, rendered by code
    data: dict = field(default_factory=dict)
    figures: list[str] = field(default_factory=list)  # every figure shown, for the number check
    amounts: list[Amount] = field(default_factory=list)  # every money figure the text shows, named


@dataclass
class Contract:
    name: str
    plugin: str
    effect: Effect
    description: str
    input_model: type[Input]
    run: Callable[[Ctx, Any], Result] | None = None
    preview: Callable[[Ctx, Any], Preview] | None = None
    commit: Callable[[Ctx, Any, str], Result] | None = None
    find_by_operation_key: Callable[[Ctx, str], Result | None] | None = None
    schema_hook: Callable[[dict], dict] | None = None  # e.g. fill an enum from the registry
    # a write's read set: the rows its preview reads that its outcome depends on,
    # as (table, primary key); the card stores each row's digest and the commit recomputes them
    read_set: Callable[[Ctx, Any], list[tuple[str, str]]] | None = None
    # the release it is planned for; until then the operation is off: not offered to the model, listed in the
    # catalogue as planned, and refused as planned wherever it is asked for
    planned: str | None = None
    synonyms: tuple[str, ...] = ()  # the owner's words for it, for the capability catalogue
    # what it does in the owner's words, after "I can" ("draft invoices"): how the engine lists what is switched on
    plain: str = ""
    # its name in a planned reply, as the owner understands it ("recording a payment"; FIGURES for an operation that
    # gives figures or reports); empty: its name in words
    planned_name: str = ""
    amounts: tuple[str, ...] = ()  # the names (AMOUNT_WHAT) its results' or answers' amounts may carry

    def tool_schema(self) -> dict:
        schema = _inline_refs(self.input_model.model_json_schema())
        schema.pop("title", None)
        if self.schema_hook:
            schema = self.schema_hook(schema)
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": schema}}


NO_DATE_GENERIC = "this change posts nothing to the books."  # an older write's line: true of any write
NO_DATE_REASON_MAX = 120


@dataclass
class WriteContract(Contract):
    """A write that declares plugin/4: whether it moves money, and why it takes no date when its input takes none, as
    its own sentence per language. The card reads that sentence, never text from the input the model built."""
    _: KW_ONLY
    moves_money: bool
    no_date_reason: dict[str, str] | None = None


def moves_money(contract: Contract) -> bool:
    """A write's own declaration; a write built as a plain Contract (plugin/1 to plugin/3) counts as moving money."""
    return contract.moves_money if isinstance(contract, WriteContract) else True


def no_date_sentence(contract: Contract) -> str:
    """The sentence a card gives when the write takes no date: the contract's own, in the books' language, or the
    generic one for an older write (never another write's)."""
    from .masters import LANGUAGE

    reason = contract.no_date_reason if isinstance(contract, WriteContract) else None
    return reason[LANGUAGE] if reason else NO_DATE_GENERIC


def no_date_reason_problem(contract: Contract) -> str | None:
    """What is wrong with a WriteContract's no-date declaration, or None (checked when its plugin loads)."""
    from .masters import LANGUAGE

    if not isinstance(contract, WriteContract):
        return None
    dateless = hasattr(contract.input_model, "takes_no_date")
    reason = contract.no_date_reason
    if reason is None:
        return "takes no date but declares no no_date_reason" if dateless else None
    if not dateless:
        return "declares a no_date_reason, but its input always takes a date"
    for lang in (LANGUAGE,):
        text = reason.get(lang)
        if not isinstance(text, str):
            return f"has no no_date_reason sentence for {lang}"
        if not text.strip():
            return f"has a no_date_reason sentence for {lang} that is empty"
        if len(text) > NO_DATE_REASON_MAX:
            return f"has a no_date_reason sentence for {lang} that is longer than {NO_DATE_REASON_MAX} characters"
        if any(ch.isdigit() for ch in text):
            return f"has a no_date_reason sentence for {lang} that holds a digit"
        if not text.endswith("."):
            return f"has a no_date_reason sentence for {lang} that ends without a full stop"
    return None


def _inline_refs(schema: dict) -> dict:
    """Small models do better with flat schemas: inline $defs, drop titles."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
                return walk(target)
            out = {}
            for k, v in node.items():
                if k == "title":
                    continue
                out[k] = walk(v)
            if "anyOf" in out:  # Optional[X] -> X, not required
                non_null = [a for a in out["anyOf"] if a.get("type") != "null"]
                if len(non_null) == 1:
                    merged = {**non_null[0], **{k: v for k, v in out.items() if k != "anyOf"}}
                    out = merged
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def planned_text(name: str, wave: str, plural: bool = False) -> str:
    """The plain reply for something planned and not yet in ERPClaw; no figure can appear in it."""
    say = __getattr__("say")
    return say("planned_plural" if plural else "planned_singular", name=f"{name[0].upper()}{name[1:]}")


# How a planned reply names figures and reports, whichever measure was asked for: never by a measure's own title,
# which can be a word for something switched on ("Sales", "Invoices"); and the one catalogue id their refusal carries.
FIGURES = "figures and reports like revenue"
FIGURES_ID = "planned.reports"


def figures_planned_text(wave: str) -> str:
    return planned_text(FIGURES, wave, plural=True)


def planned_reply(contract: Contract) -> str:
    """The reply to a request for a switched-off operation, naming it as the owner understands it."""
    if contract.planned_name == FIGURES:
        return figures_planned_text(contract.planned)
    return planned_text(contract.planned_name or contract.name.replace("_", " "), contract.planned)


def planned_capability(contract: Contract) -> str:
    """The catalogue id a refusal of a switched-off operation carries: figures and reports as one, any other
    operation its catalogue entry's id, as planned."""
    return FIGURES_ID if contract.planned_name == FIGURES else f"planned.op.{contract.name}"


@dataclass
class Service:
    """An operation a plugin offers other plugins, declared in its manifest's `services`. `fn` runs on the caller's
    `Ctx` (its connection, transaction and company) with the validated input model, and returns the output model."""
    name: str
    effect: Literal["read", "write"]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[[Ctx, Any], Any]

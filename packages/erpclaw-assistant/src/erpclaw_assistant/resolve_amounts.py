"""Resolution amounts."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Iterable
from erpclaw_core.common import Refusal
from erpclaw_core.masters import _norm, regular_plural, same_word
from .resolve_text import _say, table
from .resolve_words import bounded_names, punct_after, words
from .resolve_types import Found, Occurrence
from .resolve_state import Session


ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE
CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ
GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD
MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU
XTS XUA XXX YER ZAR ZMW ZWG ZWL""".split())



CODE_WORDS = frozenset({"ALL", "TOP", "CUP", "MAD", "PEN", "TRY", "BOB", "MOP", "SOS", "BAM", "CUC", "GEL", "NAD"})



DOLLARS = frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "TWD", "JMD", "BSD", "BBD", "BZD", "BMD", "KYD", "TTD",
                     "XCD", "FJD", "SBD", "LRD", "NAD", "GYD", "SRD", "BND", "ZWL"})



SYMBOLS = {"$": DOLLARS, "US$": {"USD"}, "CA$": {"CAD"}, "C$": {"CAD"}, "A$": {"AUD"}, "AU$": {"AUD"},
           "NZ$": {"NZD"}, "HK$": {"HKD"}, "S$": {"SGD"}, "€": {"EUR"}, "£": {"GBP"}, "¥": {"JPY", "CNY"},
           "₹": {"INR"}, "₩": {"KRW"}, "₽": {"RUB"}, "₺": {"TRY"}, "₪": {"ILS"}, "₦": {"NGN"}, "₱": {"PHP"},
           "฿": {"THB"}, "₫": {"VND"}}



_DINARS = {"KWD", "BHD", "JOD", "IQD", "LYD", "RSD", "TND", "DZD"}



_PESOS = {"MXN", "ARS", "CLP", "COP", "PHP"}



_RUPEES = {"INR", "PKR", "LKR", "NPR"}



CURRENCY_WORDS = {"dollar": DOLLARS, "dollars": DOLLARS, "buck": DOLLARS, "bucks": DOLLARS, "euro": {"EUR"},
                  "euros": {"EUR"}, "pound": {"GBP"}, "pounds": {"GBP"}, "quid": {"GBP"}, "sterling": {"GBP"},
                  "rupee": _RUPEES, "rupees": _RUPEES, "yen": {"JPY"}, "yuan": {"CNY"}, "renminbi": {"CNY"},
                  "dinar": _DINARS, "dinars": _DINARS, "peso": _PESOS, "pesos": _PESOS, "franc": {"CHF", "XOF", "XAF"},
                  "francs": {"CHF", "XOF", "XAF"}, "real": {"BRL"}, "reais": {"BRL"}, "rand": {"ZAR"},
                  "krona": {"SEK", "ISK"}, "kronor": {"SEK"}, "krone": {"NOK", "DKK"}, "kroner": {"NOK", "DKK"},
                  "won": {"KRW"}, "ruble": {"RUB"}, "rubles": {"RUB"}, "rouble": {"RUB"}, "roubles": {"RUB"},
                  "lira": {"TRY"}, "dirham": {"AED", "MAD"}, "dirhams": {"AED", "MAD"}, "riyal": {"SAR", "QAR"},
                  "riyals": {"SAR", "QAR"}, "shekel": {"ILS"}, "shekels": {"ILS"}, "baht": {"THB"},
                  "ringgit": {"MYR"}, "naira": {"NGN"}}



def _is_code(tok: str) -> bool:
    up = tok.upper()
    return up in ISO_4217 and (tok == up or up not in CODE_WORDS)



def _money_closing_end(text: str) -> int:
    """Whitespace and closing punctuation between a figure and its following marker."""
    end = 0
    while end < len(text) and (text[end].isspace() or text[end] in ",.;:)'\"]}" or
                              unicodedata.category(text[end]) in {"Pe", "Pf"}):
        end += 1
    return end



def _marks_at(text: str, start: int, end: int) -> list[tuple[str, set[str] | None]]:
    """The currency marks written against the figure at text[start:end], on either side, with or without a space,
    each with the currencies it names (None: a currency sign the engine has no row for, still a mark)."""
    before, after = text[:start], text[end:]
    # Closing punctuation belongs to the sentence, never to a currency attachment.
    after = after[_money_closing_end(after):]
    found: list[tuple[str, set[str] | None]] = []
    mb = re.search(r"(?<!\w)([A-Za-z]{1,3}\$|[^\w\s.,])\s*$", before)
    if mb:
        tok = mb.group(1)
        if tok in SYMBOLS:
            found.append((tok, set(SYMBOLS[tok])))
        elif len(tok) == 1 and unicodedata.category(tok) == "Sc":
            found.append((tok, None))
    mb = re.search(r"(?<![\w])([A-Za-z]+)\s*$", before)
    if mb and _is_code(mb.group(1)):
        found.append((mb.group(1).upper(), {mb.group(1).upper()}))
    elif mb and mb.group(1).lower() in CURRENCY_WORDS:
        found.append((mb.group(1).lower(), set(CURRENCY_WORDS[mb.group(1).lower()])))
    ma = re.match(r"\s*([A-Za-z]{1,3}\$|[^\w\s.,])(?!\w)", after)
    if ma:
        tok = ma.group(1)
        if tok in SYMBOLS:
            found.append((tok, set(SYMBOLS[tok])))
        elif len(tok) == 1 and unicodedata.category(tok) == "Sc":
            found.append((tok, None))
    ma = re.match(r"\s*([A-Za-z]+)(?!\w)", after)
    if ma:
        w = ma.group(1)
        if len(w) == 3 and _is_code(w):
            found.append((w.upper(), {w.upper()}))
        elif w.lower() in CURRENCY_WORDS:
            found.append((w.lower(), set(CURRENCY_WORDS[w.lower()])))
    return found



def figures(text: str) -> list[tuple[Decimal, int, int]]:
    """Each number the owner wrote in digits, as a decimal, with its place in the text."""
    spans = []
    for mt in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        start, end = mt.start(), mt.end()
        while end > start and text[end - 1] == ",":
            end -= 1
        # Inspect the complete adjacent run. A suffix or prefix inside an id cannot manufacture an amount.
        currency_chars = "".join(SYMBOLS) + "".join(ch for ch in text if unicodedata.category(ch) == "Sc")
        run = r"[\w" + re.escape(currency_chars) + r"]+"
        left = re.search(run + r"$", text[:start])
        right = re.match(run, text[end:])
        prefix, suffix = left.group() if left else "", right.group() if right else ""
        def mark(token):
            return token in SYMBOLS or _is_code(token) or token.lower() in CURRENCY_WORDS or (
                len(token) == 1 and unicodedata.category(token) == "Sc")
        if prefix and not mark(prefix):
            continue
        if suffix and suffix.lower() not in {"st", "nd", "rd", "th"} and not mark(suffix):
            continue
        if start and text[start - 1] == ".":
            continue
        spans.append((start, end))
    out = []
    for start, end in sorted(set(spans)):
        try:
            d = Decimal(text[start:end].replace(",", ""))
        except InvalidOperation:
            continue
        if start and text[start - 1] == "-" and (start == 1 or not text[start - 2].isalnum()):
            d = -d  # "-5": the sign is the owner's; "INV-2026" and "2026-12-01" are not signs
        out.append((d, start, end))
    return out



def _word_at(text: str, offset: int) -> int:
    """Word index of a character, including a figure glued to a currency prefix."""
    tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
    return next((i for i, mt in enumerate(tokens) if mt.start() <= offset < mt.end()), len(tokens))



def _amount_marker_ranges(text: str) -> list[tuple[int, int]]:
    """Markers belong to their original figure, before any other field is reserved or replaced."""
    ranges = []
    for _, start, end in figures(text):
        after_start = end + _money_closing_end(text[end:])
        for token, _ in _marks_at(text, start, end):
            before = re.search(r"(?<!\w)" + re.escape(token) + r"\s*$", text[:start], re.I)
            after = re.match(re.escape(token) + r"(?!\w)", text[after_start:], re.I)
            if before:
                ranges.append((before.start(), before.start() + len(token)))
            if after:
                ranges.append((after_start, after_start + after.end()))
        before = re.search(r"%\s*$", text[:start])
        after = text[after_start:].startswith("%")
        if before:
            ranges.append((before.start(), before.start() + 1))
        if after:
            ranges.append((after_start, after_start + 1))
    return ranges



def _amount_marker_words(text: str) -> set[int]:
    """Standalone marker words cannot be owned by a name, method or other field.

    A marker glued to a number shares that number's word; preserve numeric-name handling there.
    """
    ranges = _amount_marker_ranges(text)
    return {i for i, token in enumerate(mt for mt in re.finditer(r"\S+", text) if words(mt.group()))
            if not figures(token.group()) and any(a < token.end() and token.start() < b for a, b in ranges)}



def _has_amount_marker(text: str, start: int, end: int) -> bool:
    return bool(_marks_at(text, start, end) or re.search(r"%\s*$", text[:start]) or
                text[end + _money_closing_end(text[end:]):].startswith("%"))



def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None



def literal_figure(value, text: str, currency: str, session: Session, path: str, ask: tuple[str, list[str]],
                   occurrences: list[Occurrence] | None = None) -> str:
    """The figure equals a number the owner wrote, in the company's currency; otherwise the field's
    question, or the refusal for another currency."""
    v = _decimal(value)
    hits = [(s, e) for f, s, e in figures(text) if v is not None and f == v and
            (occurrences is None or any(o.kind == "figure" and o.value == f and
             o.start <= _word_at(text, s) < o.end for o in occurrences))]
    if not hits:
        raise session.ask(path, ask[0], "amount_missing", ask[1], [None] * len(ask[1]))
    for s, e in hits:
        for tok, names in _marks_at(text, s, e):
            if names is None or currency not in names:
                theirs = tok if names is None or len(names) != 1 else next(iter(names))
                raise Refusal(_say("foreign_currency", ours=currency, theirs=theirs), "foreign_currency",
                              capability="planned.multi_currency")
    return str(v)



def number_words(ws: list[str]) -> Decimal | None:
    """The value of a whole run of number words ("five hundred", "one hundred and five", "twenty five", "a dozen"),
    or None when the run is not one number as written ("two three", "five and six"): never a part of it."""
    t = table()
    total = current = 0
    last = None  # "unit", "ten", "scale" or "and": what the previous word was
    for w in ws:
        if w in t["number_and"]:
            if last != "scale":
                return None
            last = "and"
        elif w in t["quantity"]:
            v = t["quantity"][w]
            kind = "ten" if v >= 20 and v % 10 == 0 else "unit"
            if last == "unit" or (last == "ten" and (kind == "ten" or v >= 10)):
                return None  # "two three", "twenty thirty", "twenty eleven": not one number
            current += v
            last = kind
        elif w in t["scales"]:
            if last == "and":
                return None
            if w == "thousand":
                total, current = total + (current or 1) * 1000, 0
            else:
                current = (current or 1) * t["scales"][w]
            last = "scale"
        else:
            return None
    if not ws or last == "and":
        return None
    return Decimal(total + current)



UNCLEAR = "unclear"  # a quantity written in a form the engine will not guess at: the field's question



def _figure_before(text: str, k: int) -> Decimal | str | None:
    """The figure the owner wrote as the request's word k-1, read from the raw text, so a decimal point and a minus
    written against it are kept ("1.5", "-5"). UNCLEAR when that word is digits no figure accounts for (".5"), when
    another figure stands just before it ("1 500"), or when a currency mark is written against it ("$5")."""
    figs = figures(text)
    for n, (d, st, en) in enumerate(figs):
        if len(words(text[:en])) != k or len(words(text[:st])) != k - 1:
            continue
        if n and not text[figs[n - 1][2]:st].strip() or _marks_at(text, st, en):
            return UNCLEAR
        return d  # figures() keeps a written sign
    return UNCLEAR



def _quantity_before(ws: list[str], run: Found, text: str) -> Decimal | str | None:
    """The quantity written just before an item's words: a figure, a run of number words ("five hundred"), or a
    figure with scale words ("2 dozen"); UNCLEAR for a form read no further (a figure and other number words)."""
    k = run.start
    if not k:
        return None
    if any(ch.isdigit() for ch in ws[k - 1]):
        return _figure_before(text, k)
    t, j = table(), k
    while j and (ws[j - 1] in t["quantity"] or ws[j - 1] in t["scales"] or
                 (ws[j - 1] in t["number_and"] and j >= 2 and ws[j - 2] in t["scales"])):
        j -= 1
    said = ws[j:k]
    if not said:
        return None
    if number_words(said) is None and not (j and any(ch.isdigit() for ch in ws[j - 1])):
        return UNCLEAR  # number words that are not one number as written: asked, never a part of them
    if j and any(ch.isdigit() for ch in ws[j - 1]):
        lead = _figure_before(text, j)
        if not isinstance(lead, Decimal) or not all(w in t["scales"] for w in said):
            return UNCLEAR
        for w in said:
            lead *= t["scales"][w]
        return lead
    return number_words(said)



def _plural(name: str) -> str:
    nw = _norm(name).split()
    return " ".join([*nw[:-1], regular_plural(nw[-1])])



def _quantity_before_unit(ws: list[str], run: Found, unit: str | None, text: str) -> Decimal | str | None:
    """ "two months of support retainer": a quantity before the item's own stored unit and "of"."""
    if run.start < 3 or ws[run.start - 1] != "of" or not (
            ws[run.start - 2] in table()["generic_quantity_units"] or
            (unit and same_word(ws[run.start - 2], _norm(unit)))):
        return None
    return _quantity_before(ws, Found(run.name, run.start - 2, run.end), text)



def literal_quantity(value, text: str, item_name: str, session: Session, path: str, unit: str | None = None,
                     names: list[str] | None = None) -> str:
    """2.4: a number the owner wrote in digits, or the number word directly before the item's words (or before the
    item's own unit and "of": "two months of support retainer"); an omitted quantity is that one, if written."""
    def taken(value):
        if session.owner_messages and any(f == _decimal(value) for f, _, _ in figures(session.owner_messages[-1]["text"])):
            session.record_free_answer(path)
        return str(value)

    ws = words(text)
    # bounded among every stored item (`names`), so the guard sees a longer name; the item's own runs kept
    runs = [r for r in bounded_names(ws, names or [item_name], item=True, breaks=punct_after(text))
            if r.name == item_name]
    before = [q for q in [*(_quantity_before(ws, r, text) for r in runs),
                          *(_quantity_before_unit(ws, r, unit, text) for r in runs)] if q is not None]
    v = _decimal(value)
    if UNCLEAR in before:
        pass  # never guessed at: asked
    elif v is None:
        if len(set(before)) == 1:
            return taken(before[0])
    elif before:  # the model's figure must be the one written before the item, never another of the owner's
        if v in before:
            return taken(v)
    elif any(f == v and not _marks_at(text, st, en) for f, st, en in figures(text)):
        return taken(v)  # no quantity before the item's words: a figure the owner wrote, not a sum of money
    raise session.ask(path, _say("quantity_missing", plural=_plural(item_name)), "quantity_missing", [], [])



_MONEY_LOOKALIKES = str.maketrans({
    "А": "A", "а": "a", "Е": "E", "е": "e", "О": "O", "о": "o",
    "Р": "P", "р": "p", "С": "C", "с": "c", "Х": "X", "х": "x",
    "Т": "T", "т": "t", "Ѕ": "S", "ѕ": "s", "І": "I", "і": "i",
    "Α": "A", "α": "a", "Β": "B", "β": "b", "Ε": "E", "ε": "e",
    "Ο": "O", "ο": "o", "Τ": "T", "τ": "t", "Χ": "X", "χ": "x",
})



_MONEY_NUMBER = (
    r"(?:\d+(?: \d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?: (?:one|two|three|four|five|six|seven|eight|nine))?|(?:one )?hundred|half)"
)



_MONEY_PERCENT = r"(?:%|per cent\b|percent\b|pct\b|pc\b)"



MONEY_PATTERNS = (
    ("tax", re.compile(
        r"\b(?:tax(?:es)?|v ?a ?t|g ?s ?t|h ?s ?t)\b|"
        r"\b(?:tax|vat|gst|hst)(?:inclusive|exclusive)\b|"
        r"\b(?:incl|ex)(?:tax|vat|gst|hst)\b|"
        r"\b\d+(?: \d+)? *% *(?:sales )?tax\b|\bplus tax\b")),
    ("discount", re.compile(
        r"\bdiscount(?:s|ed)?\b|"
        rf"\b{_MONEY_NUMBER} *{_MONEY_PERCENT} *off\b|"
        rf"\b(?:less|minus) +{_MONEY_NUMBER} *{_MONEY_PERCENT}|"
        r"\bhalf (?:price|off)\b|\boff list\b|\ba reduction of\b")),
)



def mask_money_spans(text: str, ranges: Iterable[tuple[int, int]]) -> str:
    """Blank exempt ranges in original code points, preserving every other character and its position."""
    masked = list(text)
    for start, end in ranges:
        if not 0 <= start <= end <= len(text):
            raise ValueError("A name range must lie inside its original message")
        masked[start:end] = " " * (end - start)
    return "".join(masked)



def normalise_money(text: str) -> str:
    """Normalise only for money matching, keeping punctuation as word separators."""
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    folded = unicodedata.normalize("NFKC", visible).translate(_MONEY_LOOKALIKES)
    separated = "".join(c if c.isalpha() or c.isdigit() or c == "%" or c.isspace() else " "
                        for c in folded)
    return " ".join(separated.casefold().split())



def money_area(text: str, ranges: Iterable[tuple[int, int]] = ()) -> str | None:
    """The first tax or discount instruction outside explicitly exempt original-text ranges."""
    normal = normalise_money(mask_money_spans(text, ranges))
    matches = [(match.start(), index, area) for index, (area, pattern) in enumerate(MONEY_PATTERNS)
               if (match := pattern.search(normal)) is not None]
    return min(matches)[2] if matches else None

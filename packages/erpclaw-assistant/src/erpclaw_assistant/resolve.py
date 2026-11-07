"""The resolution step: every field of a write that names a record, a figure or a date is resolved by the engine from
the owner's own words in the current request, before the arguments are validated and a card is proposed. The model's
argument only chooses among names the owner wrote in full; it never adds, drops or replaces a word.

A record resolves without a question only when the owner wrote its whole stored name, bounded on both sides by the
start or end of the message, a figure in digits, or a word of the language's closed bounding table (for an item, also
a quantity word just before it), or an exact email or city of one customer. Everything else is a question, a confirm
when one record is a candidate. A figure must be a number the owner wrote, in the company's currency. A date comes
from the owner's one date phrase. A part of the request the engine will not do is said, never dropped: a part that
would change the money written refuses the whole request with the planned reply; one that changes nothing written is a
`not_done` event beside the card.

Fields are marked in the plugin's input models with `Annotated` metadata, a dict under the key "erpclaw", so the marks
stay out of the tool schema the model sees. Language rules are data, one table per language; English ships.
"""
from __future__ import annotations

import re
import secrets
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, get_args

from pydantic import BaseModel
from sqlalchemy import select

from erpclaw_core import models as m
from erpclaw_core.approvals import takes_no_date
from erpclaw_core.catalogue import planned_instead
from erpclaw_core.common import Ctx, Refusal, is_question
from erpclaw_core.masters import STOP, _distance, _norm, customers, regular_plural, same_word
from erpclaw_core.money import fmt_rate
from erpclaw_core.plugins import call_service

# ---------- the language tables (English ships; another language is a new table, never code) ----------

TABLES = {
    "en": {
        # table version 2, closed: a word not listed does not bound a name. Version 2
        # added the words that start another field or clause: dated, memo, note, reference, due, in, on.
        "bound": frozenset({"invoice", "bill", "charge", "send", "email", "paid", "pay", "received", "for", "to",
                            "from", "of", "by", "at", "and", "with", "please", "the", "our", "my", "customer",
                            "client", "dated", "memo", "note", "reference", "due", "in", "on"}),
        # between the mentions of two records of a field: the owner has not chosen (one record per call)
        # table version 3: a day word just after a name ends it (never before one); the guard below still applies,
        # and the word stays the date rules' to read
        "bound_after": frozenset({"today", "yesterday", "tomorrow", "monday", "tuesday", "wednesday", "thursday",
                                  "friday", "saturday", "sunday"}),
        # a day word followed by one of these is part of a name ("Acme Sunday Ltd"), never its end
        "company_suffix": frozenset({"ltd", "limited", "inc", "llc", "co", "corp", "corporation", "plc", "gmbh",
                                     "pty", "llp"}),
        "or": frozenset({"or"}),
        # before a customer's name: the owner rules it out ("Wayne, not Acme"); it is no claimant and is not asked about
        "negation": frozenset({"not"}),
        "either": frozenset({"either"}),
        # an owner's typed answer to the engine's pending question, matched before the model runs; the control
        # actions are matched by these words, or by their codes
        "answers": {
            "yes": frozenset({"yes", "y", "yeah", "yep", "correct", "right"}),
            "someone_else": frozenset({"someone else", "none of these", "neither"}),
            "cancel": frozenset({"cancel", "never mind", "nevermind", "stop"}),
            # with a card pending and the model answering in text: the whole message, never a part of it, keeps the
            # card approvable; so does an approval word with only these beside it
            "acknowledge": frozenset({"thanks", "thank you", "ok", "okay", "great", "looks good", "perfect", "yes",
                                     "yes please", "yeah", "sure"}),
            "approval_filler": frozenset({"yes", "yeah", "ok", "okay", "please", "it", "that", "this", "the", "card",
                                          "now", "just", "sure"}),
            # a position among the choices; -1 is the last
            "positions": {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
                          "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1,
                          "the first": 1, "the second": 2, "the third": 3, "the fourth": 4, "the fifth": 5,
                          "the last": -1, "the first one": 1, "the second one": 2, "the last one": -1},
        },
        # how a drafted or pending change is named in the one-change-at-a-time notice, from canonical arguments only
        "drafts": {"create_invoice": "the invoice for {customer}", "record_payment": "the payment from {customer}",
                   "cancel_invoice": "cancelling {invoice}", "reverse_payment": "reversing {payment}",
                   "submit_invoice": "submitting {invoice}"},
        # directly before an item name: its quantity, and a bound for the item field only
        "quantity": {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                     "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                     "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                     "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90},
        # number words that multiply what comes before them ("five hundred", "two dozen")
        "scales": {"dozen": 12, "hundred": 100, "thousand": 1000},
        # "one" before an item, but an article after a name ("send Wayne Enterprises an invoice"): never a bound there
        "articles": frozenset({"a", "an"}),
        # a write intent is one of these verbs with its document (the one-write gate); "invoice" after another intent
        # verb, an article or a determiner is that document, not a second intent
        "write_verbs": frozenset({"invoice", "bill", "charge", "submit", "cancel", "void", "reverse", "record", "raise",
                                  "create", "send", "issue", "draft", "pay", "paid"}),
        "determiners": frozenset({"the", "this", "that", "these", "those", "another", "new", "first", "second", "next",
                                  "last", "my", "our", "their", "its", "his", "her", "your"}),
        # a verb aimed at the document already in hand is the same write ("and submit it")
        "same_document": frozenset({"it", "this", "them", "that"}),
        # the nouns a document is named by, before its number ("invoice 8", "payment 3")
        "document_nouns": frozenset({"invoice", "inv", "payment", "pay"}),
        # "one hundred and five": "and" joins a number phrase only straight after a scale word
        "number_and": frozenset({"and"}),
        # an invoice is saved as a draft (submit false) only when the owner's words ask for one
        "draft": r"\b(?:draft|don'?t submit|do not submit|save it as a draft|not yet)\b",
        # a payment applied to the oldest open invoices first only when the owner's words say so
        "oldest_first": r"\boldest\s+(?:ones?\s+|invoices?\s+)?first\b",
        # the owner approving or confirming a card by typing: a conversation message never approves (the card's own
        # action does), so the reply in such a turn is the engine's
        "approval_words": r"\b(?:approve[sd]?|confirm(?:ed)?|go ahead|save it|do it|yes,? (?:do|save|go))\b",
        # a model's answer claiming a change to the books: replaced, since a turn never commits
        "completion_claims": (
            r"^\W*(?:all\s+)?done\b",
            r"\b(?:i|we)(?:'ve|\s+have)\s+(?:just\s+|now\s+)?(?:saved|recorded|created|invoiced|submitted|posted|"
            r"approved|paid|applied|cancell?ed|voided|reversed|sent|emailed|booked)\b",
            r"\b(?:has|have)\s+been\s+(?:saved|recorded|created|invoiced|submitted|posted|approved|paid|applied|"
            r"cancell?ed|voided|reversed|sent|emailed|booked)\b",
            r"\bis\s+now\s+(?:saved|recorded|created|invoiced|submitted|posted|approved|paid|applied|cancell?ed|"
            r"voided|reversed|sent|emailed|booked)\b",
        ),
        # a payer the owner wrote, recognised only for a write whose customer is a payer: "<payer> paid",
        # "paid by <payer>", "from <payer>", "<payer>'s payment"
        "payer_paid": "paid",
        "payer_by": "by",
        "payer_from": frozenset({"from"}),
        "payer_possessive": r"['’]s\s+payment\b",
        "possessive_suffix": r"['’]s",  # comes off a payer written as "<payer>'s payment"
        "payer_skip": frozenset({"has", "have", "had", "already", "just", "finally", "fully", "partly", "partially",
                                 "also"}),  # between a payer and "paid"
        "passive": frozenset({"was", "were", "is", "are", "been", "be", "being", "got", "get", "gets"}),
        "pronouns": frozenset({"we", "i", "they", "he", "she", "you", "it", "us", "me", "them", "someone",
                               "somebody", "everyone"}),
        "methods": frozenset({"check", "cheque", "cash", "wire", "transfer", "card", "bank", "ach", "credit", "debit",
                              "direct", "deposit"}),
        # words that end a payer or account phrase, beside the bounding words
        "phrase_stop": frozenset({"against", "towards", "toward", "via", "into", "today", "yesterday", "this",
                                  "that", "these", "those", "account", "acct", "oldest", "first", "invoices",
                                  "balance"}),
        # a payment method the owner wrote: one of these, then method words ("by bank transfer", "in cash")
        "method_cues": frozenset({"by", "via", "with", "in"}),
        "method_nouns": frozenset({"method"}),
        "method_unknown_cues": frozenset({"by", "via"}),  # "via Zelle": a method-like phrase the table does not know  # "by method Zelle", "payment method: check"
        "coordinators": frozenset({"and", "or", "plus"}),  # "Nobody and Hooli paid": every payer is the owner's
        # the account the money went into: "into <account>", "<account> account", "account <number>"
        "account_into": frozenset({"into", "to"}),
        "account_nouns": frozenset({"account", "acct"}),
        # A line's stock source; these cues never change general record-name bounds.
        "warehouse_from": frozenset({"from"}),
        "warehouse_stops": frozenset({"from", "dated", "memo", "note", "reference", "due", "please"}),
        "today": frozenset({"today"}),
        "yesterday": frozenset({"yesterday"}),
        "days_ago": r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(day|week)s?\s+ago\b",
        # a word that names a time unit or a calendar position: one no rule reads asks, unless it is a unit or a rate
        "calendar_words": frozenset({"day", "days", "week", "weeks", "month", "months", "year", "years", "previous",
                                     "following", "ago", "yearend", "monthend"}),
        "calendar_not": frozenset({"may", "march", "mar"}),  # month names that are mostly ordinary words
        "unit_after": frozenset({"per", "each", "every"}),  # "per day": a rate
        "unit_before": frozenset({"of"}),  # "two months of support retainer": a quantity's unit
        "weekday_back": ("before", "last"),  # "Friday before last": a week before the last Friday
        "weekday_on": ("after", "next"),  # "Friday after next": a week after the coming Friday
        "weekday_relative": frozenset({"before", "after"}),  # any other "Friday before ...": asked
        "weekdays": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        "ahead": frozenset({"next", "this", "coming"}),
        "months": {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
                   "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
                   "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
                   "dec": 12},
        # a date the engine cannot read as one day: a question, never a guess
        "vague_dates": r"\b(last|this|next)\s+(week|month|year)\b|\bthe other day\b|\brecently\b|\bsometime\b|"
                       r"\ba few days ago\b|"
                       # date-like words no rule here reads: asked about, never dropped
                       r"\bfortnight\w*|\bweekends?\b|\bthe day (?:after|before)\b(?!\s+(?:tomorrow|yesterday))|"
                       r"\beo[dwyq]\b|\bend of (?:the )?(?:day|week|year|quarter)\b|\b(?:start|beginning|middle) of "
                       r"(?:the )?(?:week|month|year|quarter)\b|\bmid-?month\b|\b(?:next|last|this) quarter\b|"
                       r"\bnext (?:business|working) day\b|\b(?:at )?month[- ]end\b|"
                       r"\bin (?:a|an|one|two|three|four|\d+) (?:days?|weeks?|months?|years?)\b|\byear[- ]end\b|"
                       r"\bnew years?\b|\b(?:last|next|this|in|early|late|over)\s+(?:spring|fall)\b",
        "tomorrow": frozenset({"tomorrow"}),
        # whole phrases read before any word inside them ("day before yesterday" is not "yesterday"), as day offsets
        "relative_days": ((r"\b(?:the\s+)?day\s+before\s+yesterday\b", -2),
                          (r"\b(?:the\s+)?day\s+after\s+tomorrow\b", 2),
                          (r"\blast\s+night\b", -1),
                          (r"\btonight\b|\bthis\s+(?:morning|afternoon|evening)\b", 0)),
        "month_end": r"\bend of (?:the )?month\b|\beom\b",  # the month's last day
        # date words with no other sense: one no rule above read asks, never dropped
        "date_vocab": frozenset({"night", "morning", "afternoon", "evening", "overnight", "quarter", "weekend",
                                 "weekends", "fortnight", "fortnightly", "eod", "eow", "eom", "eoy", "eoq",
                                 "midmonth", "winter", "summer", "autumn", "christmas", "xmas", "easter",
                                 "thanksgiving", "diwali", "hanukkah", "eid", "newyear"}),
        # what may not touch a date reading: left beside it, the reading is not the owner's whole phrase, so it asks
        "date_touch": frozenset({"half", "quarter", "third", "couple", "few", "next", "last", "this", "coming",
                                 "past", "previous", "following", "early", "late", "mid", "before", "after", "and"}),
        "weekday_ahead": frozenset({"next", "this", "coming"}),  # read with the weekday as the coming one
        # beside a month and day, either side: not read (which year is meant), so asked
        "day_qualifiers": frozenset({"next", "last", "this", "coming", "previous", "following", "past"}),
        "weekday_behind": frozenset({"last", "past"}),  # read with the weekday as the last one
        # "May" and "March" are dates only when qualified ("last May", "the end of March") or beside a day
        "month_qualifiers": frozenset({"last", "next", "this", "in", "of", "early", "late", "mid", "since", "until",
                                       "during"}),
        "stock": frozenset({"stock", "inventory", "warehouse"}),
        "tax": frozenset({"tax", "vat", "gst", "hst"}),
        "tax_phrase": r"\d+(?:\.\d+)?\s*%\s*(?:sales\s+)?tax\b|\bplus\s+tax\b",
        "discount": frozenset({"discount", "discounted"}),
        "discount_phrase": r"\d+(?:\.\d+)?\s*%\s*off\b",  # never a bare "off" ("kick-off workshop")
        "discount_words": (("off", "list"),),
        "terms_words": (("payment", "terms"),),
        "net": "net",  # "net 30"
        "due": "due",  # followed by a date or "in N days"
        "in_days": ("in", "days"),
        "email": frozenset({"send", "email", "mail"}),
        "rate": r"(?:\bpriced\s+at\b|\bat\b|@)\s*[^\w\s]?\s*(\d[\d,]*(?:\.\d+)?)|"
                r"(\d[\d,]*(?:\.\d+)?)\s*(?:\beach\b|\bper\b|\ba\s+piece\b|\bapiece\b)",
        "memo": r"\b(?:note|memo|reference|po)\b\s*[:#]?\s*\S",
        # Display metadata only: the recognition expression and retained request words stay unchanged.
        "memo_qualifiers": {"note": ("note", "nd_memo"), "memo": ("memo", "nd_memo"),
                            "reference": ("reference", "nd_reference"), "po": ("PO number", "nd_po")},
        "memo_qualifier_default": "memo",
        "memo_qualifier_cues": ((r"memo\b\s*[:#]?\s*po\b", "po"),),
        "text": {
            "customer_missing": "Which customer is it for?",
            "no_records": "There are no {what}s yet.",
            "unknown": "I can't find a {what} called '{said}'.",
            "confirm": "Do you mean {name}?",
            "someone_else": "someone else",
            # a question's control actions as a client lists them, by name (CONTROLS): coded "<question id>-<name>"
            "controls": {"someone_else": "Someone else", "cancel": "Cancel"},
            "which": "Which {what}: {names}?",
            "figure_unused": "What is the {figure} for? I couldn't place it in this request.",
            "numeral_unclear": "I can't read '{figure}' as one number. Could you write it again, like 1.5 or 1,500?",
            "which_draft": "Which of these invoices should be saved as a draft?",
            "one_change": "One change at a time, please: send each change as its own message.",
            "lines": "I'd put {name} on more lines than you wrote it. How many lines of {name} should there be?",
            "one_at_a_time": ("One change at a time: I've drafted {drafted}. Ask again for {pending} once you've "
                              "approved or declined it."),
            "twice": ("I'd use {name} for more than one change here. Please say it again with the customer for "
                      "each one."),
            "quantity_missing": "How many {plural}?",
            "amount_missing": "How much was it?",
            "document_missing": "Which {what}?",
            "date_unknown": "Which date do you mean?",
            "date_ambiguous": "Do you mean {a} or {b}?",
            "date_which": "Which date: {dates}?",
            "nothing_saved": "Nothing has been saved yet.",
            "cancelled": "Cancelled; nothing was saved.",
            # a message while a turn holds the conversation: working (it renewed lately) or stopped partway
            "busy": "I'm still working on your last message.",
            "busy_stopped": "Your last message stopped partway, and nothing was saved from it. Please send it again in "
                            "a few minutes.",
            # a takeover set aside a card of a turn that lost its claim: true whether that turn died or stalled
            "turn_unfinished": "An earlier card in this request was set aside because its turn didn't finish; "
                               "nothing was saved from it.",
            "approve_card": "Approve the card to save it.",
            # a message re-proposed the pending card and changed nothing: the card is set aside, never left approvable
            "card_unchanged": ("I didn't change that card, so I've set it aside; nothing was saved. Tell me what to "
                               "change, or ask again to get it back."),
            "method_unknown": "I don't know the payment method '{said}'. How was it paid?",
            "one_detail": "One detail at a time: I see {count} {kind} ({names}). Which one?",
            "counts": {2: "two", 3: "three", 4: "four"},
            "foreign_currency": ("Your books are in {ours}, so a payment in {theirs} can't be recorded: the exchange "
                                 "difference is not something ERPClaw can book yet."),
            "stock": "Selling from stock isn't available yet.",
            "tax": "Sales tax isn't available yet, so I haven't drafted this invoice.",
            "discount": "Discounts aren't available yet, so I haven't drafted this invoice.",
            "terms": "Setting a due date or payment terms isn't available yet, so I haven't drafted this invoice.",
            "nd_email": "I've drafted the invoice; emailing it isn't available yet, so it won't be sent.",
            "nd_rate": ("You said {said} each for {item}; the card uses the list price, {list}. Setting a rate isn't "
                        "available yet."),
            "nd_date": "Invoices are dated today; choosing an invoice date isn't available yet.",
            "date_today_or_leave": "Shall I date it today, or leave it?",
            "date_today": "Date it today",
            "rate_list_or_leave": "Shall I use the list price, or leave it?",
            "rate_list": "Use the list price",
            "memo_none_or_leave": "Shall I draft it without the {qualifier}, or leave it?",
            "memo_none": "Leave out the {qualifier}",
            "leave_it": "Leave it",
            "nd_no_date": "Cancelling a draft changes no books, so it takes no date.",
            "nd_memo": "Adding a {qualifier} to an invoice isn't available yet; the invoice will have none.",
            "nd_po": "A customer's PO number cannot be recorded yet; the invoice will have none.",
            "nd_reference": "Recording a reference on an invoice isn't available yet; the invoice will have none.",
            "setup_first": "Let's set up your books first; it takes a minute.",
            "setup_name_missing": "What's your business called?",
            "setup_currency_missing": "Which currency do you keep your books in?",
            "setup_currency_planned": ("I can keep books in US dollars only for now; other currencies come with the "
                                       "country packs (3.1). Nothing was set up."),
            "setup_fy_start_missing": "When does your financial year start? January, or another month?",
            "setup_date_order_missing": "How do you write dates: 03/04 for 3 April, or for 4 March?",
            "books_not_set_up": "Your books aren't set up yet.",
            "customer_exists": "{name} is already a customer.",
            "customer_near": "Did you mean {name} ({city}), already a customer, or is {said} a new one?",
            "item_unit_missing": "What unit do you sell {name} in: each, hour, meter, or another?",
            "item_price_missing": "At what price do you usually sell {name}?",
            "item_exists": "{name} is already an item.",
            "item_near": "Did you mean {name}, already an item, or is {said} a new one?",
            "item_stock_not_yet": ("Adding an item you keep in stock isn't available yet; I can add it as a service "
                                   "item, or you can add it later."),
        },
    },
}
LANGUAGE = "en"
# Between a request's messages in its joined words: never a word an owner writes, it ends every name and phrase, so
# one message's words never run on into the next's
MESSAGE_BREAK = "zzmessagebreakzz"
for _t in TABLES.values():
    _t["bound"] = _t["bound"] | {MESSAGE_BREAK}
    _t["phrase_stop"] = _t["phrase_stop"] | {MESSAGE_BREAK}


def table(lang: str | None = None) -> dict:
    return TABLES[lang or LANGUAGE]


def _say(key: str, **kw) -> str:
    return table()["text"][key].format(**kw)


# ---------- currency marks ----------

ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE
CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ
GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD
MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU
XTS XUA XXX YER ZAR ZMW ZWG ZWL""".split())
# codes that are also everyday English words count as a mark only when written in capitals ("all 500" is not lek)
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
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _is_code(tok: str) -> bool:
    up = tok.upper()
    return up in ISO_4217 and (tok == up or up not in CODE_WORDS)


def _marks_at(text: str, start: int, end: int) -> list[tuple[str, set[str] | None]]:
    """The currency marks written against the figure at text[start:end], on either side, with or without a space,
    each with the currencies it names (None: a currency sign the engine has no row for, still a mark)."""
    before, after = text[:start], text[end:]
    found: list[tuple[str, set[str] | None]] = []
    mb = re.search(r"([A-Za-z]{1,3}\$|[^\w\s.,])\s?$", before)
    if mb:
        tok = mb.group(1)
        if tok in SYMBOLS:
            found.append((tok, set(SYMBOLS[tok])))
        elif len(tok) == 1 and unicodedata.category(tok) == "Sc":
            found.append((tok, None))
    mb = re.search(r"(?<![A-Za-z])([A-Za-z]{3})\s?$", before)
    if mb and _is_code(mb.group(1)):
        found.append((mb.group(1).upper(), {mb.group(1).upper()}))
    ma = re.match(r"\s?([^\w\s.,])", after)
    if ma:
        tok = ma.group(1)
        if tok in SYMBOLS:
            found.append((tok, set(SYMBOLS[tok])))
        elif unicodedata.category(tok) == "Sc":
            found.append((tok, None))
    ma = re.match(r"\s?([A-Za-z]+)", after)
    if ma:
        w = ma.group(1)
        if len(w) == 3 and _is_code(w):
            found.append((w.upper(), {w.upper()}))
        elif w.lower() in CURRENCY_WORDS:
            found.append((w.lower(), set(CURRENCY_WORDS[w.lower()])))
    return found


def figures(text: str) -> list[tuple[Decimal, int, int]]:
    """Each number the owner wrote in digits, as a decimal, with its place in the text."""
    spans = [(mt.start(), mt.end()) for mt in NUMBER.finditer(text)]
    # a figure written against a currency code with no space ("EUR500") is still the owner's figure
    spans += [mt.span(2) for mt in re.finditer(r"(?<![A-Za-z])([A-Za-z]{3}|[A-Za-z]{1,3}\$)(\d[\d,]*(?:\.\d+)?)", text)
              if _is_code(mt.group(1)) or mt.group(1) in SYMBOLS]
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


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


# ---------- the request's words and names ----------

def words(text: str) -> list[str]:
    """The request's words, normalised as stored names are (`masters._norm`): punctuation is removed and never
    bounds; the word itself decides."""
    return _norm(text).split()


def punct_after(text: str) -> frozenset[int]:
    """The positions in `words(text)` that a punctuation mark follows ("tomorrow;" or "tomorrow ;"): a mark never
    bounds a name, but a day word before one ends it (table version 3)."""
    out, n = set(), -1
    for token in text.split():
        if not _norm(token):  # a mark alone
            if n >= 0:
                out.add(n)
            continue
        if re.match(r"[^\w\s]", token) and n >= 0:
            out.add(n)
        n += 1
        if re.search(r"[^\w\s]$", token):
            out.add(n)
    return frozenset(out)


@dataclass(frozen=True)
class Found:
    name: str  # the stored name, raw
    start: int
    end: int


def bounded_names(ws: list[str], names: list[str], *, item: bool, lang: str | None = None,
                  bounded: bool = True, breaks: frozenset[int] = frozenset()) -> list[Found]:
    """Each stored name whose whole normalised words occur in the request as consecutive words, bounded on both sides
    ; a name whose run lies inside another found name's run is set aside. For items the final word may be the
    generated singular or plural of the stored one (guard 2). `breaks`: the positions a punctuation mark follows
    (`punct_after`).

    The continuation guard: a bounding word ends a name only if no stored name of the kind continues past it with
    that same word ("in" after Acme does not bound when "Acme in Leeds Ltd" is stored), on either side, an item's
    words compared as the name is found (Copper Wire continues into "Copper Wires Monday Pack"). A day word ends a
    name only before the end, a mark, a figure or a bounding word (never a company suffix), and not where a stored
    name starts with it or goes on from the found one with the word after it. It only ever turns a silent match into
    a question, never the reverse."""
    t = table(lang)
    stored = [_norm(n).split() for n in names]

    def same(run: list[str], nw: list[str]) -> bool:  # an item's final word as it is found: singular or plural
        return run[:-1] == nw[:-1] and (run[-1] == nw[-1] or (item and same_word(run[-1], nw[-1])))

    def continues(nw: list[str], w: str, before: bool) -> bool:
        k = len(nw)
        return any(len(s) > k and ((s[-k - 1] == w and same(s[-k:], nw)) if before else (same(s[:k], nw) and s[k] == w))
                   for s in stored)

    def bounds(nw: list[str], pos: int, before: bool) -> bool:
        w = ws[pos] if 0 <= pos < len(ws) else None
        if not bounded or w is None or w.isdigit():
            return True
        if not before and w in t["weekday_ahead"] | t["weekday_behind"] and \
                pos + 1 < len(ws) and ws[pos + 1] in t["weekdays"]:
            # A qualified weekday has the same name guards as a bare day, including its qualifier.
            if continues(nw, w, before) or any(s and s[0] == w for s in stored):
                return False
            pos += 1
            w = ws[pos]
        after = ws[pos + 1] if pos + 1 < len(ws) else None
        then = ws[pos + 2] if pos + 2 < len(ws) else None
        marked = pos in breaks
        if (w in t["quantity"] or w in t["scales"]) and ((item and before) or (not before and w not in t["articles"])):
            edge = nw[0] if before else nw[-1]  # a number word after a name, or before an item: a quantity
            return not (edge in t["quantity"] or edge in t["scales"]) and not continues(nw, w, before)
        if w in t["bound"]:
            return not continues(nw, w, before)
        if not before and w in t["bound_after"]:  # table version 3, with the word after the day word
            k = len(nw)
            # the next word (marks between or not) is never a company suffix, nor the word after a figure ("Monday,
            # Inc.", "Monday 2000 Ltd"); then the end, a mark, a figure or a bounding word ends the name
            suffix = t["company_suffix"]
            ends = after not in suffix and not (after is not None and after.isdigit() and then in suffix) and \
                (after is None or marked or after.isdigit() or after in t["bound"])
            if not ends or any(s and s[0] == w for s in stored) or \
                    any(len(s) > k and same(s[:k], nw) and s[k] == after for s in stored):
                return False
            return not continues(nw, w, before)
        return False

    found = []
    for name in names:
        nw = _norm(name).split()
        k = len(nw)
        if not k:
            continue
        for i in range(len(ws) - k + 1):
            run = ws[i:i + k]
            if run[:-1] != nw[:-1] or not (run[-1] == nw[-1] or (item and same_word(run[-1], nw[-1]))):
                continue
            if bounds(nw, i - 1, True) and bounds(nw, i + k, False):
                found.append(Found(name, i, i + k))
    return [f for f in found if not any(g.start <= f.start and f.end <= g.end and (g.end - g.start) > (f.end - f.start)
                                        for g in found)]


def _collides(a: str, b: str, item: bool) -> bool:
    x, y = _norm(a).split(), _norm(b).split()
    if x == y:
        return True
    return item and len(x) == len(y) and x[:-1] == y[:-1] and same_word(x[-1], y[-1])


def _raw_in(text: str, name: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


def _candidates(rows: list[dict], ws: list[str], *, item: bool, limit: int = 5) -> list[dict]:
    """The records sharing a word with the owner's words, or near one (by edit distance), nearest first; words of
    the bounding and quantity tables, stop words and figures are not compared."""
    t = table()
    said = [w for w in ws if w not in t["bound"] and w not in t["quantity"] and w not in STOP and not w.isdigit()]
    scored = []
    for r in rows:
        have = [w for w in _norm(r["name"]).split() if w not in STOP]
        shared = sum(1 for h in have if any(h == w or (item and same_word(w, h)) for w in said))
        near = min((_distance(w, h) for w in said for h in have
                    if len(w) >= 3 and _distance(w, h) <= max(len(w), len(h)) // 3), default=None)
        if shared or near is not None:
            scored.append((-shared, near if near is not None else 0, r["name"], r))
    return [r for *_, r in sorted(scored, key=lambda x: x[:3])[:limit]]


# ---------- questions, choices and the request's state ----------

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
                    self.bindings[self.pending["field"]] = c["ref"]
                self.pending = None
                return True
        return False

    def record_free_answer(self, path: str) -> None:
        """Keep the question identity when a resolver consumes a free answer's value."""
        if self.owner_messages and self.pending and self.pending["field"] == path and not self.pending["choices"]:
            self.owner_messages[-1].update(question_id=self.pending["id"], field=path)

    def control(self, name: str) -> None:
        """A control action at the pending question: "someone_else" leaves its field bound to no candidate, so the
        full question is asked next; the question itself is answered."""
        if self.pending and name == "someone_else":
            self.bindings[self.pending["field"]] = None
            self.pending = None

    def end(self) -> None:
        self.pending, self.bindings = None, {}

    def dump(self) -> dict:
        """Plain data, for the turn's saved state; the references stay in the engine's state, never in an event."""
        return {"serial": self.serial, "pending": self.pending, "bindings": dict(self.bindings),
                **({"owner_messages": self.owner_messages} if self.owner_messages else {})}

    @classmethod
    def load(cls, data: dict | None) -> "Session":
        data = data or {}
        return cls(int(data.get("serial", 0)), data.get("pending"), dict(data.get("bindings") or {}),
                   list(data.get("owner_messages") or []))

    def ask(self, path: str, text: str, code: str, labels: list[str], refs: list, **data) -> Refusal:
        """The field's question: its id, and one single-use code per choice; the event carries no reference."""
        self.serial += 1
        qid = f"q{self.serial}"
        choices = [{"code": f"{qid}-{i}", "label": label, "ref": ref}
                   for i, (label, ref) in enumerate(zip(labels, refs), 1)]
        self.pending = {"id": qid, "field": path, "choices": choices}
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
    return None


SINGLE_FIELDS = ("customer", "date", "document")  # one per write: a later mention replaces every earlier one


def _leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def request_words(ctx: Ctx, messages: list[str], calls: list[tuple[object, dict]]) -> str:
    """The request's words for the resolution step, its messages in order, each ended by MESSAGE_BREAK. The latest
    message that mentions a field supplies it: a customer, a date or a document number in a later message drops every
    earlier mention of its kind; an earlier figure or item drops out when the calls no longer use it and do use a later
    one of its kind. Anything else stays, so a later mention the calls ignore is still the owner's and is asked about."""
    if len(messages) < 2:
        return messages[0] if messages else ""
    items = _items(ctx)
    occs = [inventory(ctx, text, items) for text in messages]
    leaves = list(_leaves([a for _, a in calls]))
    figures_used = {d for d in (_decimal(v) for v in leaves if not isinstance(v, bool)) if d is not None}
    names_used = {_norm(str(v)) for v in leaves if isinstance(v, str)}

    def used(o: Occurrence) -> bool:
        if o.kind == "figure":
            return o.value in figures_used
        return _norm(str(o.value or o.said)) in names_used

    kept = []
    for n, text in enumerate(messages):
        later = [o for found in occs[n + 1:] for o in found]
        drop: set[int] = set()
        for o in occs[n]:
            same = [x for x in later if x.kind == o.kind]
            if not same:
                continue
            if o.kind in SINGLE_FIELDS or (o.kind in ("figure", "item") and not used(o) and any(used(x) for x in same)):
                drop.update(range(o.start, o.end))
        kept.append(" ".join(w for k, w in enumerate(_raw_words(text)) if k not in drop) if drop else text)
    return f" . {MESSAGE_BREAK} ".join(kept)  # a sentence ends with each message, so a memo ends there too


# ---------- records ----------

def _missing(what: str) -> str:
    return _say("customer_missing") if what == "customer" else _say("document_missing", what=what)


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


def _raw_words(text: str) -> list[str]:
    """The request's words as the owner wrote them, one per entry of `words(text)`."""
    return [tok for tok in text.split() if _norm(tok)]


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


def _phrase_stops() -> frozenset:
    t = table()
    return t["bound"] | t["phrase_stop"] | t["determiners"] | t["pronouns"] | t["methods"] | t["write_verbs"] | \
        t["document_nouns"] | t["today"] | t["yesterday"] | frozenset(t["weekdays"]) | frozenset(t["months"]) | \
        t["coordinators"]


def _run(ws: list[str], raw: list[str], i: int, step: int, stops: frozenset) -> tuple[int, int] | None:
    """The run of phrase words from word i in direction `step` (1 right, -1 left), up to a stopping word, a word with
    a figure in it, or a mark that ends a clause; its span (start, end), or None when it is empty."""
    got: list[int] = []
    while 0 <= i < len(ws) and len(got) < 6:
        w = ws[i]
        if w in stops or any(ch.isdigit() for ch in w):
            break
        if step < 0 and got and re.search(r"[,.;:!?]$", raw[i]):
            break  # leftwards, a word closing an earlier clause is not the payer's
        got.append(i)
        if step > 0 and re.search(r"[,.;:!?]$", raw[i]):
            break
        i += step
    if not got:
        return None
    return min(got), max(got) + 1


def payer_phrases(text: str) -> list[str]:
    """The payers the owner wrote, as written, in order: "<payer> paid", "paid by <payer>", "from <payer>" and
    "<payer>'s payment". A passive "was paid" names none; a pronoun, a payment method or a document is no payer."""
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = _phrase_stops()
    spans: list[tuple[int, int]] = []

    def coordinated(span: tuple[int, int], step: int) -> None:
        """The payers joined to this one by "and" or "or", in the same direction."""
        a, b = span
        while True:
            k = a - 1 if step < 0 else b
            if not (0 <= k < len(ws)) or ws[k] not in t["coordinators"]:
                return
            nxt = _run(ws, raw, k + step, step, stops)
            if not nxt or (nxt[1] != k if step < 0 else nxt[0] != k + 1):
                return
            spans.append(nxt)
            a, b = nxt

    for i, w in enumerate(ws):
        if w == t["payer_paid"]:
            j = i - 1
            while j >= 0 and ws[j] in t["payer_skip"]:
                j -= 1
            if j >= 0 and ws[j] not in t["passive"]:
                span = _run(ws, raw, j, -1, stops)
                if span and span[1] == j + 1:
                    spans.append(span)
                    coordinated(span, -1)
            if i + 1 < len(ws) and ws[i + 1] == t["payer_by"]:
                span = _run(ws, raw, i + 2, 1, stops)
                if span:
                    spans.append(span)
                    coordinated(span, 1)
        elif w in t["payer_from"]:
            span = _run(ws, raw, i + 1, 1, stops)
            if span:
                spans.append(span)
                coordinated(span, 1)
    for mt in re.finditer(t["payer_possessive"], text, re.I):
        j = len(words(text[:mt.start()])) - 1
        span = _run(ws, raw, j, -1, stops) if j >= 0 else None
        if span and span[1] == j + 1:
            spans.append(span)
    out: list[str] = []
    for a, b in sorted(set(spans)):
        said = re.sub(rf"(?:{t['possessive_suffix']})?[,.;:!?]*$", "", " ".join(raw[a:b]))
        if said and said not in out:
            out.append(said)
    return out


def account_phrases(text: str) -> list[tuple[int, int]]:
    """The spans of the accounts the owner named the money going into: "into <account>", "<account> account" and
    "account <number>". A payment method ("by bank transfer") names none."""
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = t["bound"] | t["phrase_stop"] | t["determiners"] | t["pronouns"] | t["coordinators"]
    out: list[tuple[int, int]] = []
    for i, w in enumerate(ws):
        if w in t["account_into"]:
            j = i + 1
            while j < len(ws) and ws[j] in t["determiners"]:
                j += 1
            if j < len(ws) and ws[j] in t["account_nouns"]:  # "into account Savings"
                j += 1
            if j < len(ws) and ws[j].isdigit():
                out.append((j, j + 1))
            elif j < len(ws) and not any(ch.isdigit() for ch in ws[j]):
                span = _run(ws, raw, j, 1, stops)
                if span and span[0] == j:
                    out.append(span)
                    while span[1] + 1 < len(ws) and ws[span[1]] in t["coordinators"]:  # "into Bank and Savings"
                        nxt = _run(ws, raw, span[1] + 1, 1, stops)
                        if not nxt or nxt[0] != span[1] + 1:
                            break
                        out.append(nxt)
                        span = nxt
        elif w in t["account_nouns"]:
            after = _run(ws, raw, i + 1, 1, stops) if i + 1 < len(ws) else None
            if i + 1 < len(ws) and any(ch.isdigit() for ch in ws[i + 1]):
                out.append((i + 1, i + 2))
            elif after and after[0] == i + 1:  # "to account Missing", "account Savings"
                out.append(after)
            elif i:
                span = _run(ws, raw, i - 1, -1, stops)
                if span and span[1] == i:
                    out.append(span)
    return sorted(set(out))


def method_phrases(text: str) -> list[str]:
    """The payment methods the owner wrote, as written: a method cue, then a run of method words."""
    t, ws, raw = table(), words(text), _raw_words(text)
    out: list[str] = []
    for i, w in enumerate(ws):
        if w in t["method_cues"] or w in t["method_nouns"]:
            j = i
            while True:  # one method, then any joined to it ("cash or wire")
                k = j + 1
                while k < len(ws) and ws[k] in t["methods"]:
                    k += 1
                    if re.search(r"[,.;:!?]$", raw[k - 1]):
                        break
                said = re.sub(r"[,.;:!?]+$", "", " ".join(raw[j + 1:k]))
                if k == j + 1:
                    break
                if said not in out:
                    out.append(said)
                if k < len(ws) and ws[k] in t["coordinators"]:
                    j = k
                    continue
                break
    return out


def unknown_methods(text: str) -> list[str]:
    """An explicit method the owner named ("by method Zelle") that the table does not know, as written."""
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = _phrase_stops()
    out: list[str] = []
    for i, w in enumerate(ws):
        cue = w in t["method_unknown_cues"] and not (i and ws[i - 1] == t["payer_paid"]) and not (  # "paid by X":
            i + 1 < len(ws) and ws[i + 1] in t["method_nouns"])  # a payer; "by method X": the noun's own rule
        if w in t["method_nouns"] or cue:
            span = _run(ws, raw, i + 1, 1, stops - t["methods"])
            if span and span[0] == i + 1 and any(x not in t["methods"] for x in ws[span[0]:span[1]]):
                said = _as_written(text, span)
                if said not in out:
                    out.append(said)
    return out


def _as_written(text: str, span: tuple[int, int]) -> str:
    """The owner's own words over a span, as written, without a closing mark."""
    a, b = span
    return re.sub(r"[,.;:!?]+$", "", " ".join(_raw_words(text)[a:b]))


def _written_span(ws: list[str], value) -> tuple[int, int] | None:
    """Where the value's words stand together in the owner's words, or None."""
    vw = _norm(str(value or "")).split()
    for i in range(len(ws) - len(vw) + 1):
        if vw and ws[i:i + len(vw)] == vw:
            return i, i + len(vw)
    return None


def _unit(ws: list[str], i: int) -> bool:
    """A calendar word used as a unit or a rate, not a date: "two months of retainer", "per day", "150 a day"."""
    t = table()
    if i + 1 < len(ws) and ws[i + 1] in t["unit_before"]:
        return True
    if i and ws[i - 1] in t["unit_after"]:
        return True
    return i >= 2 and ws[i - 1] in t["articles"] and bool(re.fullmatch(r"[\d.,]+", ws[i - 2]))


def _calendar(ws: list[str], i: int) -> bool:
    """A calendar word at position i: "May" and "March" only when qualified or beside a day."""
    t, w = table(), ws[i]
    if w in t["calendar_not"]:
        return (i > 0 and ws[i - 1] in t["month_qualifiers"]) or (i + 1 < len(ws) and ws[i + 1].isdigit())
    return w in t["calendar_words"] or w in t["months"] or w in t["weekdays"]


def _ago() -> str:
    """'<number> days ago' or '<number> weeks ago', the number a whole run of number words or digits."""
    t = table()
    num = "|".join(sorted(set(t["quantity"]) | set(t["scales"]), key=len, reverse=True))
    return rf"\b((?:(?:{num})[- ](?:and[- ])?)*(?:{num})|\d+)\s+(day|week)s?\s+ago\b"


def _touched(ws: list[str], before: int, after: int) -> bool:
    """A word left beside a date reading that belongs to it: a number word (never digits: an amount precedes
    dates), a fraction, a qualifier or a calendar word. Then the reading is not the owner's whole phrase."""
    t = table()
    near = [k for k in (before, after) if 0 <= k < len(ws)]
    numbers = set(t["quantity"]) - t["articles"] | set(t["scales"])
    return any(ws[k] in t["date_touch"] or ws[k] in numbers or _calendar(ws, k) for k in near)


def _touched_text(rest: str, a: int, b: int) -> bool:
    """`_touched` for a reading matched in the text from character a to b: the words beside it, and a hyphen joined to
    it ("twenty-" before "one days ago")."""
    if (a and rest[a - 1] == "-") or (b < len(rest) and rest[b] == "-"):
        return True
    left, right = words(rest[:a])[-1:], words(rest[b:])[:1]
    ws = left + ["\x00"] + right
    return _touched(ws, len(left) - 1 if left else -1, len(left) + 1 if right else len(ws))


def without_names(text: str, spans: list) -> str:
    """The owner's words without the stored names found in them, so a calendar word inside a name
    ("Weekend Support", "September Labs") is never read as a date."""
    if not spans:
        return text
    drop = {k for f in spans for k in range(f.start, f.end)}
    return " ".join(tok for k, tok in enumerate(_raw_words(text)) if k not in drop)


def _alternatives(ws: list[str], rows: list[dict], item: bool) -> list[str]:
    """The records whose mentions "or" stands between, or "either" stands before ("either Acme Corporation or Hooli", "5
    copper wire or circuit board"): the owner has not chosen, so the field always asks (one record per call).
    Mentions here need no bounds; a word inside a mention never counts."""
    t = table()
    found = bounded_names(ws, [r["name"] for r in rows], item=item, bounded=False)
    if len({f.name for f in found}) < 2:
        return []
    inside = {k for f in found for k in range(f.start, f.end)}
    first, last = min(f.start for f in found), max(f.start for f in found)
    for k, w in enumerate(ws):
        if k not in inside and ((w in t["or"] and first < k < last) or (w in t["either"] and k < last)):
            return sorted({f.name for f in found})
    return []


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
    either = _alternatives(ws, rows, item)
    if either:
        raise session.ask(path, _say("which", what=what, names=", ".join(either)), f"{what}_ambiguous", either, either)
    found = bounded_names(ws, [r["name"] for r in rows], item=item, breaks=punct_after(text))
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
                            if (g.name, g.start, g.end) not in held and g.name != chosen})
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
    if not rows:
        raise session.ask(path, _say("no_records", what=what), f"{what}_unknown", [], [])
    cands = [r["name"] for r in _candidates(rows, ws, item=item)]
    if len(cands) == 1:
        raise session.ask(path, _say("confirm", name=cands[0]), f"{what}_ambiguous",
                          [cands[0], _say("someone_else")], [cands[0], None], confirm=True)
    if cands:
        raise session.ask(path, _say("which", what=what, names=", ".join(cands)), f"{what}_ambiguous", cands, cands)
    said = str(arg or "").strip()
    if not (_norm(said) and f" {_norm(said)} " in f" {' '.join(ws)} "):
        said = _owner_words_taken(ws, text, arg) or ""  # only the owner's own words are ever quoted back
    if said:
        raise session.ask(path, _say("unknown", what=what, said=said), f"{what}_unknown", [], [])
    raise session.ask(path, _missing(what), f"{what}_missing", [], [])


# ---------- figures and quantities ----------

def literal_figure(value, text: str, currency: str, session: Session, path: str, ask: tuple[str, list[str]]) -> str:
    """The figure equals a number the owner wrote, in the company's currency; otherwise the field's
    question, or the refusal for another currency."""
    v = _decimal(value)
    hits = [(s, e) for f, s, e in figures(text) if v is not None and f == v]
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
    if not unit or run.start < 3 or ws[run.start - 1] != "of" or not same_word(ws[run.start - 2], _norm(unit)):
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


# ---------- dates ----------

def _recent(today: date, month: int, day: int, year: int | None) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d <= today else date(today.year - 1, month, day)
    except ValueError:
        return None


def date_phrases(text: str, today: date, date_order: str | None = None) -> list[list[date]]:
    """Each date phrase in the request, as its readings (one when clear, two for an unclear figure date); a phrase the
    engine cannot read as one day raises `date_unknown`. Digits and ISO dates read in any language; words only in a
    shipped table."""
    t, low = table(), text.lower()
    out: list[list[date]] = []
    if re.search(t["vague_dates"], low):
        raise Refusal(_say("date_unknown"), "date_unknown")
    for mt in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", low):
        try:
            out.append([date(int(mt[1]), int(mt[2]), int(mt[3]))])
        except ValueError:
            raise Refusal(_say("date_unknown"), "date_unknown") from None
    clear = None  # the order of the owner's own earlier figure date whose order was clear
    for mt in re.finditer(r"(?<![\w./])(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\w/])|"
                          r"(?<![\w./])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\w.])", low):
        a, b, y = (int(mt[1]), int(mt[2]), mt[3]) if mt[1] else (int(mt[4]), int(mt[5]), mt[6])
        year = None if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
        dmy, mdy = _recent(today, b, a, year), _recent(today, a, b, year)
        if a > 12 and dmy:
            out.append([dmy])
            clear = clear or "DMY"
        elif b > 12 and mdy:
            out.append([mdy])
            clear = clear or "MDY"
        elif dmy and mdy:
            order = clear or date_order
            out.append([dmy] if order == "DMY" or dmy == mdy else [mdy] if order == "MDY" else [dmy, mdy])
        else:
            raise Refusal(_say("date_unknown"), "date_unknown")
    rest = low
    for pattern, delta in t["relative_days"]:
        for mt in re.finditer(pattern, rest):
            if _touched_text(rest, mt.start(), mt.end()):
                raise Refusal(_say("date_unknown"), "date_unknown")
            out.append([today + timedelta(days=delta)])
        rest = re.sub(pattern, lambda mt: " " * len(mt.group(0)), rest)
    for mt in re.finditer(t["month_end"], rest):
        if _touched_text(rest, mt.start(), mt.end()):
            raise Refusal(_say("date_unknown"), "date_unknown")
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        out.append([nxt - timedelta(days=1)])
    rest = re.sub(t["month_end"], lambda mt: " " * len(mt.group(0)), rest)
    if set(words(rest)) & t["date_vocab"]:
        raise Refusal(_say("date_unknown"), "date_unknown")
    for mt in re.finditer(_ago(), rest):
        n = int(mt[1]) if mt[1].isdigit() else number_words(re.split(r"[- ]+", mt[1]))
        if n is None or _touched_text(rest, mt.start(), mt.end()):
            raise Refusal(_say("date_unknown"), "date_unknown")  # not one number as written, or a part of a phrase
        out.append([today - timedelta(days=int(n) * (7 if mt[2] == "week" else 1))])
    rest = re.sub(_ago(), lambda mt: " " * len(mt.group(0)), rest)
    ws = words(rest)
    read: set[int] = set()
    for i, w in enumerate(ws):
        if w in t["weekdays"] and i + 1 < len(ws) and ws[i + 1] in t["weekday_relative"]:
            k, after = t["weekdays"].index(w), tuple(ws[i + 1:i + 3])
            if after == t["weekday_back"]:
                out.append([today - timedelta(days=((today.weekday() - k) % 7 or 7) + 7)])
            elif after == t["weekday_on"]:
                out.append([today + timedelta(days=((k - today.weekday()) % 7 or 7) + 7)])
            else:
                raise Refusal(_say("date_unknown"), "date_unknown")
            read.update(range(i, i + 3))
            continue
        if w in t["today"] or w in t["yesterday"] or w in t["tomorrow"] or w in t["weekdays"]:
            read.add(i)
            own = i - 1 if (w in t["weekdays"] and i and ws[i - 1] in t["weekday_ahead"] | t["weekday_behind"]) else i
            read.add(own)
            if _touched(ws, own - 1, i + 1):
                raise Refusal(_say("date_unknown"), "date_unknown")
        if w in t["today"]:
            out.append([today])
        elif w in t["yesterday"]:
            out.append([today - timedelta(days=1)])
        elif w in t["tomorrow"]:
            out.append([today + timedelta(days=1)])
        elif w in t["weekdays"]:
            k = t["weekdays"].index(w)
            if i and ws[i - 1] in t["ahead"]:
                out.append([today + timedelta(days=(k - today.weekday()) % 7 or 7)])
            else:
                out.append([today - timedelta(days=(today.weekday() - k) % 7 or 7)])
        elif w in t["months"]:  # a month name counts only with a day beside it ("May Smith" is not a date)
            mon, nxt, prv = t["months"][w], ws[i + 1] if i + 1 < len(ws) else "", ws[i - 1] if i else ""
            y_after = ws[i + 2] if i + 2 < len(ws) and len(ws[i + 2]) == 4 and ws[i + 2].isdigit() else None
            y_before = nxt if len(nxt) == 4 and nxt.isdigit() else None
            after = _recent(today, mon, int(nxt), int(y_after) if y_after else None) \
                if nxt.isdigit() and len(nxt) <= 2 else None
            before = _recent(today, mon, int(prv), int(y_before) if y_before else None) \
                if prv.isdigit() and len(prv) <= 2 else None
            if after or before:
                a = i - 1 if before else i
                b = (i + 3 if y_after else i + 2) if after else (i + 2 if y_before else i + 1)
                if (a and ws[a - 1] in t["day_qualifiers"]) or (b < len(ws) and ws[b] in t["day_qualifiers"]):
                    raise Refusal(_say("date_unknown"), "date_unknown")  # "next November 3": asked, never read
            if after and before and after != before:
                out.append([before, after])
            elif after or before:
                out.append([after or before])
            if after:
                read.update({i, i + 1} | ({i + 2} if y_after else set()))
            if before:
                read.update({i - 1, i} | ({i + 1} if y_before else set()))
    if any(_calendar(ws, i) and i not in read and not _unit(ws, i) for i in range(len(ws))):
        raise Refusal(_say("date_unknown"), "date_unknown")  # a calendar word no rule read: asked, never dropped
    return out


def _iso(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return date.min  # a date the engine cannot read is still a date the model claimed


def resolve_date(text: str, today: date, session: Session, path: str, model_value=None,
                 date_order: str | None = None) -> date | None:
    """The one date the owner wrote, or None when they wrote none; several, or an unclear one, is a question."""
    if session.bindings.get(path):
        return date.fromisoformat(session.bindings[path])
    try:
        phrases = date_phrases(text, today, date_order)
    except Refusal as rf:
        raise session.ask(path, rf.message, rf.code, [], []) from None
    readings = sorted({d for p in phrases for d in p}) if len(phrases) != 1 else list(dict.fromkeys(phrases[0]))
    if not phrases:
        mv = _iso(model_value)
        if mv is not None and mv != today:  # the model read a date the engine cannot: ask, never guess
            raise session.ask(path, _say("date_unknown"), "date_unknown", [], [])
        return None
    if len(readings) == 1:
        return readings[0]
    one = len(phrases) == 1 and len(readings) == 2
    labels = [f"{d.day} {d:%B}" if one else f"{d.day} {d:%b %Y}" for d in readings]
    ask = _say("date_ambiguous", a=labels[0], b=labels[1]) if one else _say("date_which", dates=" or ".join(labels))
    raise session.ask(path, ask, "date_ambiguous", labels, [d.isoformat() for d in readings])


# ---------- documents: invoice and payment numbers ----------

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
              path: str | None = None) -> None:
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
    if arg:
        mentions = [x for x in mentions if _same_document(x, arg)]
    if len(mentions) == 1:
        out[name] = v["resolve"](ctx, mentions[0])
    elif len(mentions) > 1:
        raise session.ask(path, _say("which", what=v["what"], names=", ".join(mentions)), f"{v['what']}_ambiguous",
                          mentions, [{name: _canonical_document(ctx, v, m)} for m in mentions])
    elif presence != "optional":  # a number the owner did not write is never used
        raise session.ask(path, _missing(v["what"]), f"{v['what']}_missing", [], [])


# ---------- the parts the engine will not do ----------

def _outside(ws: list[str], spans: list[Found]) -> set[str]:
    return {w for k, w in enumerate(ws) if not any(f.start <= k < f.end for f in spans)}


def _has_run(ws: list[str], run: tuple[str, ...]) -> bool:
    return any(tuple(ws[i:i + len(run)]) == run for i in range(len(ws) - len(run) + 1))


def _due_date_asked(ws: list[str], text: str, today: date) -> bool:
    """ "due" followed by a date phrase or "in N days", or "net N": a due date the card would write differently."""
    t = table()
    for i, w in enumerate(ws):
        rest = ws[i + 1:i + 5]
        # Hyphenated terms (net-60) become one word; resolved names are already excluded.
        if re.fullmatch(re.escape(t["net"]) + r"\d+", w):
            return True
        if w == t["net"] and rest and rest[0].isdigit():
            return True
        if w != t["due"] or not rest:
            continue
        if rest[0] == t["in_days"][0] and len(rest) > 2 and rest[1].isdigit() and rest[2].rstrip("s") == "day":
            return True
        after = " ".join(rest)
        try:
            if date_phrases(after, today) or re.match(r"(on |by )?\d", after):
                return True
        except Refusal:
            return True  # a date the engine cannot read is still a date asked for
    return False


# Guard matching has its own alphabet; the stored message and the card keep the owner's text.
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
        r"\b(?:tax|v ?a ?t|g ?s ?t|h ?s ?t)\b|"
        r"\b(?:tax|vat|gst|hst)(?:inclusive|exclusive)\b|"
        r"\b(?:incl|ex)(?:tax|vat|gst|hst)\b|"
        r"\b\d+(?: \d+)? *% *(?:sales )?tax\b|\bplus tax\b")),
    ("discount", re.compile(
        r"\bdiscount(?:ed)?\b|"
        rf"\b{_MONEY_NUMBER} *{_MONEY_PERCENT} *off\b|"
        rf"\bless +{_MONEY_NUMBER} *{_MONEY_PERCENT}|"
        r"\bhalf price\b|\boff list\b")),
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


def consumed_ranges(text: str, name: str, *, item: bool = False, bounded: bool = False,
                    runs: list[Found] | None = None) -> list[tuple[int, int]]:
    """Project the resolver's matched word runs onto their original code points."""
    found = runs if runs is not None else bounded_names(words(text), [name], item=item, bounded=bounded,
                                                       breaks=punct_after(text))
    tokens = [(mt.start(), mt.end()) for mt in re.finditer(r"\S+", text) if words(mt.group())]
    raw = list(re.finditer(re.escape(name), text, re.I))
    out = []
    for f in found:
        exact = next((mt for mt in raw if len(words(text[:mt.start()])) == f.start and
                      len(words(text[:mt.end()])) == f.end), None)
        if exact:
            out.append((exact.start(), exact.end()))
        elif f.end <= len(tokens):
            a, b = tokens[f.start][0], tokens[f.end - 1][1]
            while a < b and not text[a].isalnum():
                a += 1
            while b > a and not text[b - 1].isalnum():
                b -= 1
            out.append((a, b))
    return out


def record_message_names(ctx: Ctx, session: Session, custs: list[dict], items: list[dict],
                         customer_arg=None, item_args: list | None = None, resolution_text: str | None = None) -> None:
    """Resolve name occurrences for each unfinished original message, never rewriting an earlier result."""
    warehouses = None
    for msg in session.owner_messages:
        if msg.get("completed"):
            continue
        text = msg["text"]
        msg["spans"] = []
        money_answer = str(msg.get("field") or "").startswith("money_span:")
        if confirmation_choice_message(msg):
            msg["completed"] = True
            continue
        groups = [("customer", custs, False), ("item", items, True)]
        sources = warehouse_words(text, items, custs, ctx)
        if sources or str(msg.get("field") or "").endswith("warehouse"):
            if warehouses is None:
                try:
                    warehouses = call_service(ctx, "core", "stock.list_warehouses", {})["warehouses"]
                except Refusal as rf:
                    if rf.code != "plugin_not_enabled":
                        raise
                    warehouses = []
            groups.append(("warehouse", warehouses, False))
        for kind, rows, is_item in groups:
            # Choice answers consume only the chosen reference, even if another stored name occurs beside it.
            chosen = msg.get("answer_ref") if not money_answer else None
            choice_answer = msg.get("question_id") and not money_answer
            if choice_answer and (chosen is None or not str(msg.get("field") or "").endswith(kind)):
                continue
            if choice_answer:
                consumed = [chosen] if chosen is not None else []
            elif kind == "warehouse":
                consumed = []
                for source in sources:
                    matches = [r["name"] for r in rows if _norm(r["name"]) == _norm(source.said) or
                               (_norm(r["name"]) == "the " + _norm(source.said) and _raw_in(text, r["name"]))]
                    if len(matches) == 1:
                        consumed.extend(matches)
            else:
                consumed = []
                arguments = item_args if is_item else [customer_arg]
                for argument in arguments or []:
                    try:
                        resolving = resolution_text if resolution_text is not None else text
                        if kind == "customer":
                            source_spans = [Found(p.said, p.start, p.end)
                                            for p in warehouse_words(resolving, items, custs, ctx)]
                            resolving = without_names(resolving, source_spans)
                        consumed.append(_resolve_record(rows, kind, resolving, argument, Session(), kind,
                                                        item=is_item, peers=arguments))
                    except Refusal as rf:
                        # A full name awaiting confirmation can use the same choice to establish its meaning.
                        if rf.data.get("confirm"):
                            consumed.extend(n for n in rf.options if any(r["name"] == n for r in rows)
                                            and _raw_in(text, n))
            found = bounded_names(words(text), [r["name"] for r in rows], item=is_item,
                                  bounded=not choice_answer and kind != "warehouse",
                                  breaks=punct_after(text))
            found = [f for f in found if f.name in consumed]
            for name in set(consumed):
                if isinstance(name, str) and money_area(name) and not any(f.name == name for f in found):
                    found.extend(bounded_names(words(text), [name], item=is_item, bounded=False))
            for f in found:
                if chosen is not None and f.name != chosen:
                    continue
                for start, end in consumed_ranges(text, f.name, item=is_item, runs=[f]):
                    if kind == "warehouse" and not choice_answer and not any(
                            p.start <= len(words(text[:start])) < p.end for p in sources):
                        continue
                    if any(s["kind"] == kind and s["start"] == start and s["end"] == end for s in msg["spans"]):
                        continue
                    row = next(r for r in rows if r["name"] == f.name)
                    area = money_area(text[start:end])
                    msg["spans"].append({"id": secrets.token_hex(16), "start": start, "end": end,
                                         "kind": kind, "record_id": row["warehouse"] if kind == "warehouse" else row["id"],
                                         "name": row["name"],
                                         "area": area, "question": None, "classification": None})
        msg["spans"].sort(key=lambda s: (s["start"], s["end"]))
        if session.pending and not session.pending["choices"] and msg is session.owner_messages[-1]:
            path = session.pending["field"]
            if any(path.endswith(s["kind"]) for s in msg["spans"]):
                session.record_free_answer(path)
        msg["completed"] = True


def confirmation_choice_message(msg: dict) -> bool:
    """Only the choice label itself (or an empty coded answer) is consumed as confirmation text."""
    return str(msg.get("field") or "").startswith("money_span:") and (
        not msg["text"].strip() or msg["text"].strip().casefold() == str(msg.get("choice_label") or "").casefold())


def guard_messages(session: Session) -> None:
    """Read only recorded ranges and original text, including every answer in the request."""
    for msg in session.owner_messages:
        if confirmation_choice_message(msg):
            continue
        area = money_area(msg["text"], [(s["start"], s["end"]) for s in msg["spans"]
                                      if s.get("classification") != "instruction"])
        if area:
            raise Planned(_say("tax" if area == "tax" else "discount"),
                          "planned.tax_engine" if area == "tax" else "planned.discounts")
    for msg in session.owner_messages:
        for span in msg["spans"]:
            if not span.get("area") or span.get("classification"):
                continue
            if span.get("question"):
                pending = span["question"]
                session.pending = pending
                raise Refusal(pending["text"], "money_name_ambiguous", [c["label"] for c in pending["choices"]],
                              field=pending["field"], question_id=pending["id"], span_id=span["id"],
                              choices=[{k: c[k] for k in ("code", "label")} for c in pending["choices"]])
            rf = session.ask(f"money_span:{span['id']}", f"Do you mean the {span['kind']} {span['name']}?",
                             "money_name_ambiguous", [span["name"], "no, I mean tax or a discount"],
                             ["name", "instruction"], span_id=span["id"])
            session.pending.update(span_id=span["id"], text=rf.message)
            span["question"] = dict(session.pending)
            raise rf


def planned_money(text: str, spans: list[Found], today: date, *, check_money: bool = True) -> None:
    """The parts that would make the card write something other than what the owner asked (amounts, dates):
    the whole request gets the planned reply. Words inside a resolved name never count."""
    t = table()
    outside = [w for k, w in enumerate(words(text)) if not any(f.start <= k < f.end for f in spans)]
    if check_money:
        ranges = [r for f in spans for r in consumed_ranges(text, f.name, item=True, runs=[f])]
        area = money_area(text, ranges)
        if area:
            raise Planned(_say("tax" if area == "tax" else "discount"),
                          "planned.tax_engine" if area == "tax" else "planned.discounts")
    if any(_has_run(outside, r) for r in t["terms_words"]) or _due_date_asked(outside, text, today):
        raise Planned(_say("terms"), "planned.payment_terms")


def planned_stock(text: str, spans: list[Found]) -> None:
    if _outside(words(text), spans) & table()["stock"]:
        raise Planned(_say("stock"), "planned.stock")


# each part's catalogue id
NOT_DONE = {"email": "planned.messaging", "rate": "planned.invoice_fields", "date": "planned.invoice_fields",
            "memo": "planned.invoice_fields"}


def _date_phrase(text: str, today: date, d: date) -> str | None:
    """The owner's own words that read as the date `d`: the shortest run of their words that reads exactly that date,
    taking in a qualifier beside it while the reading stays the same; cut from the text as written."""
    t = table()
    toks = [(mt.start(), mt.end()) for mt in re.finditer(r"\S+", text)]

    def reads(a: int, b: int) -> bool:
        try:
            got = date_phrases(text[toks[a][0]:toks[b - 1][1]], today)
        except Refusal:
            return False
        return len(got) == 1 and d in got[0]
    joinable = t["date_touch"] | t["weekday_ahead"] | t["weekday_behind"] | {"on", "the"}
    for size in range(1, min(7, len(toks)) + 1):
        for a in range(len(toks) - size + 1):
            b = a + size
            if not reads(a, b):
                continue
            while a and _norm(text[toks[a - 1][0]:toks[a - 1][1]]) in joinable and reads(a - 1, b):
                a -= 1
            while b < len(toks) and _norm(text[toks[b][0]:toks[b][1]]) in joinable and reads(a, b + 1):
                b += 1
            return re.sub(r"[,.;:!?]+$", "", text[toks[a][0]:toks[b - 1][1]])
    return None


def date_source_of(ctx: Ctx, contract, inp, text: str) -> tuple[dict, list[dict]]:
    """A drafted card's date source, from what the step read, and any row said beside the card: "not applicable" for
    a write that takes no date (with a row when the owner wrote one anyway, never silently ignored); the owner's
    phrase for a date read from their words; otherwise the default."""
    if takes_no_date(ctx, inp):
        try:
            written = bool(date_phrases(text, ctx.today))
        except Refusal:
            written = True
        rows = [{"type": "not_done", "part": "date", "capability": NOT_DONE["date"], "text": _say("nd_no_date")}]
        return {"not_applicable": True}, rows if written else []
    d = getattr(inp, "date", None)
    phrase = _date_phrase(text, ctx.today, d) if d is not None else None
    if phrase:
        return {"phrase": phrase, "date": d.isoformat()}, []
    return {"default": True}, []


def memo_qualifier(text: str) -> tuple[str, str]:
    """Display label and notice key for the latest supported memo occurrence, without changing field resolution.

    The recognition expression consumes the first character of its value. A language-table cue can identify an
    explicit qualifier inside that occurrence, without an earlier occurrence renaming a later one.
    """
    t = table()
    mentions = list(re.finditer(t["memo"], text.lower()))
    kind = t["memo_qualifier_default"]
    if mentions:
        occurrence = re.split(r"[.;!?](?:\s|$)", text.lower()[mentions[-1].start():], maxsplit=1)[0]
        kind = re.match(r"\w+", occurrence).group(0)
        kind = next((value for pattern, value in t["memo_qualifier_cues"] if re.match(pattern, occurrence)), kind)
    return t["memo_qualifiers"][kind]


def invoice_not_done(ctx: Ctx, text: str, spans: list[Found], lines: list[dict], items: list[dict],
                     emailed: bool, *, reads_date: bool = False) -> list[dict]:
    """Parts of an invoice request that change nothing written: one event each, beside the card."""
    t, low, out = table(), text.lower(), []
    if emailed or _outside(words(text), spans) & t["email"]:
        out.append({"type": "not_done", "part": "email", "capability": NOT_DONE["email"], "text": _say("nd_email")})
    rates = {r["name"]: r["standard_rate"] for r in items}
    for mt in re.finditer(t["rate"], low):
        said = _decimal(mt.group(1) or mt.group(2))
        before = words(text[:mt.start()])
        found = bounded_names(before, [r["name"] for r in items], item=True,  # every stored item: the guard sees all
                              breaks=punct_after(text[:mt.start()]))
        ends = {ln["item"]: max((f.end for f in found if f.name == ln["item"]), default=-1) for ln in lines}
        named = [n for n, e in ends.items() if e >= 0]
        if said is None or not named:
            continue
        name = max(named, key=lambda n: ends[n])  # the nearest item named before the rate
        if said != rates[name]:
            out.append({"type": "not_done", "part": "rate", "capability": NOT_DONE["rate"], "text": _say(
                "nd_rate", said=fmt_rate(said, ctx.currency), list=fmt_rate(rates[name], ctx.currency),
                item=_plural(name))})
    if not reads_date:
        try:
            dated = any(d != ctx.today for reading in date_phrases(without_names(text, spans), ctx.today)
                        for d in reading)  # today: the default
        except Refusal:
            dated = True
        if dated:
            out.append({"type": "not_done", "part": "date", "capability": NOT_DONE["date"], "text": _say("nd_date")})
    if re.search(t["memo"], low):
        qualifier, notice = memo_qualifier(text)
        out.append({"type": "not_done", "part": "memo", "capability": NOT_DONE["memo"],
                    "text": _say(notice, qualifier=qualifier)})
    return out


# ---------- one write's arguments ----------

@dataclass
class Result:
    args: dict
    not_done: list[dict] = field(default_factory=list)


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


def _company_warehouse_names(ctx: Ctx) -> set[str]:
    try:
        rows = call_service(ctx, "core", "stock.list_warehouses", {})["warehouses"]
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
    """A source phrase belongs to the nearest preceding item mention in its own sentence.
    Keep unknown stock sources; service sources must say warehouse or name one in the company.
    """
    t, ws, raw = table(), words(text), _raw_words(text)
    services = {r["name"] for r in items if r.get("kind") == "service"}
    warehouse_names = None
    date_words = t["today"] | t["yesterday"] | t["tomorrow"] | set(t["weekdays"]) | \
        t["date_vocab"] | t["calendar_words"] | t["weekday_ahead"] | t["weekday_behind"] | set(t["months"])
    today = ctx.today if ctx is not None else date.today()
    ago_runs = [(len(words(text[:mt.start()])), len(words(text[:mt.end()])))
                for mt in re.finditer(_ago(), text.lower())]
    found = bounded_names(ws, [r["name"] for r in items], item=True, bounded=False)
    protected = found + bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False)
    # Use the existing memo inventory's boundary; its owner text is not a stock-source instruction.
    for memo in re.finditer(t["memo"], text.lower()):
        stop = re.search(r"[.;!?](?:\s|$)", text[memo.start():])
        end = len(words(text[:memo.start() + stop.start()])) if stop else len(ws)
        protected.append(Found("memo", len(words(text[:memo.start()])), end))
    breaks = punct_after(text)
    out = []
    for i, word in enumerate(ws):
        if word not in t["warehouse_from"] or any(f.start <= i < f.end for f in protected):
            continue
        before = [f for f in found if f.end <= i and
                  not any(re.search(r"[.;!?]$", raw[k]) for k in range(f.end - 1, i))]
        owner = max(before, key=lambda f: f.end) if before else None
        start = i + 1
        if start < len(ws) and ws[start] == "the":
            start += 1
        end = start
        while end < len(ws):
            if end > start and (ws[end] in t["warehouse_stops"] or
                                (ws[end] in t["coordinators"] and end + 1 < len(ws) and
                                 not any(a <= end < b for a, b in ago_runs) and
                                 (ws[end + 1].isdigit() or ws[end + 1] in t["quantity"] or
                                  ws[end + 1] in t["write_verbs"]))):
                break
            end += 1
            if end - 1 in breaks:
                break
        if end == start:
            continue
        dates = [j for j in range(start + 1, end) if ws[j] in date_words]
        date_start = _trailing_date_start(text, start + 1, end, today)
        if dates or date_start is not None or \
                ((owner is None or owner.name in services) and "warehouse" not in ws[start:end]):
            if warehouse_names is None:
                warehouse_names = _company_warehouse_names(ctx) if ctx is not None else set()
        if dates or date_start is not None:
            # A stored name wins over calendar words inside it; dates after it remain the date reader's.
            known_end = max((j for j in range(start + 1, end + 1)
                             if _norm(_as_written(text, (start, j))) in warehouse_names), default=start)
            date_start = _trailing_date_start(text, max(start + 1, known_end), end, today)
            if date_start is not None:
                dates.append(date_start)
            end = min((j for j in dates if j >= known_end), default=end)
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


def _one_detail(path: str, session: Session, kind: str, names: list[str], code: str):
    """Two or more of one kind written: the owner picks one, and nothing is drafted until then."""
    count = table()["text"]["counts"].get(len(names), str(len(names)))
    return session.ask(path, _say("one_detail", count=count, kind=kind, names=", ".join(names)), code, names, names)


def _payer(cust_rows: list[dict], text: str, v: dict, session: Session, path: str) -> str | None:
    """The payer the owner wrote, for a field whose customer is a payer: each payer phrase resolves from its own words
    alone, and two different payers are the owner's to pick. None when the owner wrote no payer phrase."""
    if not v.get("payer") or path in session.bindings:
        return None
    payers = payer_phrases(text)
    if not payers:
        return None
    said = f" {' '.join(words(text))} "

    def whole(p: str) -> str:  # a stored name the phrase is the end of ("September Labs" for "Labs"), written whole
        full = [r["name"] for r in cust_rows if _norm(r["name"]).endswith(_norm(p)) and f" {_norm(r['name'])} " in said]
        return full[0] if len(full) == 1 else p
    got = list(dict.fromkeys(resolve_record(cust_rows, "customer", whole(p), whole(p), session, path, item=False)
                             for p in payers))
    if len(got) > 1:
        raise _one_detail(path, session, "payers", got, "customer_ambiguous")
    return got[0]


def resolve_args(ctx: Ctx, contract, args: dict, text: str, session: Session, peers: dict | None = None,
                 scope: str = "") -> Result:
    """The call's arguments made canonical from the owner's words, with the parts not done; or exactly one question
    (a `Refusal` carrying its question id and choices), or the planned reply (`Planned`). `peers` are the reply's
    arguments for customers and items (`reply_peers`); by default, this call's own. `scope` names this write in the
    reply ("create_invoice#0:"): its questions and answers belong to it alone.

    Support: a field the step has no rule for is never taken from the model; it is removed, so the contract's default
    applies, unless the owner's answer to a question bound it."""
    instead = planned_instead(ctx.conn, contract.name, text)
    if instead is not None:  # the owner's words name what is planned in its place: its answer, whatever was called
        raise Planned(instead.text, instead.capability)
    mk = marks(contract.input_model)
    if not mk:
        return Result(dict(args))
    peers = reply_peers([(contract, args)]) if peers is None else peers
    out = {k: val for k, val in args.items() if k in mk}  # nothing unmarked is taken from the model
    for key, bound in session.bindings.items():  # an answered contract question binds a field the step leaves alone
        k = key[len(scope):] if scope and key.startswith(scope) else (key if not scope else None)
        if k in contract.input_model.model_fields and k not in mk:
            out[k] = bound
    ws = words(text)
    cust_rows = customers(ctx) if any(v["kind"] == "customer" for v in mk.values()) else []
    lines_field = next((n for n, v in mk.items() if v["kind"] == "lines"), None)
    item_rows = _items(ctx) if lines_field else []
    spans = bounded_names(ws, [r["name"] for r in cust_rows], item=False, breaks=punct_after(text)) + \
        bounded_names(ws, [r["name"] for r in item_rows], item=True, breaks=punct_after(text))
    source_spans = [Found(p.said, p.start, p.end) for p in warehouse_words(text, item_rows, cust_rows, ctx)]
    spans += source_spans
    customer_text = without_names(text, source_spans) if lines_field else text
    emailed = False
    warehouse_not_done = []
    for name, v in mk.items():  # a switched-off option is removed, never refused for the whole request
        if v["kind"] == "off":
            emailed = bool(out.pop(name, False)) or emailed
    lm = marks(_line_model(contract.input_model, lines_field)) if lines_field else {}
    item_field = next((k for k, v in lm.items() if v["kind"] == "item"), None)
    customer_arg = next((out.get(n) for n, v in mk.items() if v["kind"] == "customer"), None)
    item_args = [line.get(item_field) for line in out.get(lines_field, []) if isinstance(line, dict)] \
        if isinstance(out.get(lines_field), list) else []
    if lines_field and (out.get(lines_field) == [] or (
            out.get(lines_field) is None and mk[lines_field].get("presence") != "optional")):
        item_args = [None]  # the implicit line resolves its item from the owner's words, just like an explicit line
    record_message_names(ctx, session, cust_rows, item_rows, customer_arg, item_args, text)
    if lines_field:
        guard_messages(session)
        planned_money(text, spans, ctx.today, check_money=not bool(session.owner_messages))
        # the planned stock reply, before any item question: a stock word, and no item's full name anywhere in
        # the owner's words (bounded or not), so an item question could only steer the owner to a service item
        if not bounded_names(ws, [r["name"] for r in item_rows], item=True, bounded=False):
            planned_stock(text, spans)
    # a document comes first: a derived field (the payer) is read from it; then the rest in the model's order
    order = [n for n, v in mk.items() if v["kind"] == "document"] + \
        [n for n, v in mk.items() if v.get("presence") == "derived"] + \
        [n for n, v in mk.items() if v["kind"] != "document" and v.get("presence") != "derived"]
    cust_names = {r["name"] for r in cust_rows}
    for name in order:
        v = mk[name]
        kind, presence = v["kind"], v.get("presence", "required")
        path = f"{scope}{name}"
        if kind == "flag":  # true or false only from the owner's words or answer; otherwise the contract's default
            out.pop(name, None)
            if path in session.bindings:
                out[name] = session.bindings[path]
            elif re.search(table()[v["words"]], text.lower()):
                out[name] = v["when"]
            continue
        if presence == "derived":
            # taken from the document only when the owner's words name no customer at all: not exactly, not by any
            # words the model took for one, not by a word of any customer's name ("Acme paid 100 against INV-6" asks),
            # and not by a payer phrase, which is resolved as the field's own words whatever the model passed
            payer = _payer(cust_rows, text, v, session, path)
            if payer:
                out[name] = payer
                continue
            named = bool(_named_records(cust_rows, text, [f.name for f in spans if f.name in cust_names])) \
                or path in session.bindings or bool(out.get(name) and _owner_words_taken(ws, text, out.get(name))) \
                or any((set(_norm(r["name"]).split()) - STOP) & set(ws) for r in cust_rows)
            got = None if named else v["derive"](ctx, out)
            out[name] = got if got is not None else resolve_record(cust_rows, "customer", customer_text, out.get(name),
                                                                   session, path, item=False,
                                                                   peers=peers["customer"],
                                                                   alike=peers["customer_calls"].get(contract.name))
        elif kind == "customer":
            payer = _payer(cust_rows, text, v, session, path)
            if payer:
                out[name] = payer
                continue
            if presence == "optional" and not out.get(name) and \
                    not _named_records(cust_rows, text, [f.name for f in spans if f.name in cust_names]):
                continue
            out[name] = resolve_record(cust_rows, "customer", customer_text, out.get(name), session, path, item=False,
                                       peers=peers["customer"], alike=peers["customer_calls"].get(contract.name))
        elif kind == "lines":
            if presence == "optional" and out.get(name) is None:
                out.pop(name, None)  # delivery/credit: omitted means the rest/everything, not an empty item.
                continue
            if not _lines_shape(out.get(name)):
                continue  # not a list of objects: validation sends it back to the model
            new = []
            for i, li in enumerate(out.get(name) or [{}]):
                li = {k: val for k, val in (li or {}).items() if k in lm}  # nothing unmarked from the model
                for fname, fv in lm.items():
                    path = f"{scope}{name}.{i}.{fname}"
                    if fv["kind"] == "item":
                        li[fname] = resolve_record(item_rows, "item", text, li.get(fname), session, path, item=True,
                                                   peers=peers["item"])
                    elif fv["kind"] == "quantity":
                        unit = next((r.get("unit") for r in item_rows if r["name"] == li[item_field]), None)
                        li[fname] = literal_quantity(li.get(fname), text, li[item_field], session, path, unit,
                                                     names=[r["name"] for r in item_rows])
                    elif fv["kind"] == "warehouse":
                        item = next(r for r in item_rows if r["name"] == li[item_field])
                        # Match repeated names in line order, just as occurrence accounting claims each item.
                        mentions = [f for f in bounded_names(ws, [r["name"] for r in item_rows],
                                                            item=True, bounded=False) if f.name == item["name"]]
                        ordinal = sum(previous[item_field] == item["name"] for previous in new)
                        item_start = mentions[ordinal].start if ordinal < len(mentions) else None
                        phrases = [p for p in warehouse_words(text, item_rows, cust_rows, ctx)
                                   if p.item == item["name"] and item_start is not None and p.item_start == item_start]
                        chosen = _warehouse(ctx, item, phrases, session, path, fv)
                        li.pop(fname, None)
                        if chosen is not None:
                            li[fname] = chosen
                        elif session.bindings.get(path) is False:
                            source = ", ".join(p.said for p in phrases)
                            warehouse_not_done.append({"type": "not_done", "part": "warehouse",
                                                       "text": f"The warehouse source {source} was not used for {item['name']}."})
                new.append(li)
            out[name] = new
        elif kind == "figure":
            asker: Callable | None = v.get("ask")
            ask = asker(ctx, out) if asker else (_say("amount_missing"), [])
            out[name] = literal_figure(out.get(name), text, ctx.currency, session, path, ask)
        elif kind == "date":
            order = getattr(ctx, "date_order", None) or ctx.extra.get("date_order")
            d = resolve_date(without_names(text, spans), ctx.today, session, path, out.get(name), order)
            if d is None and contract.input_model.model_fields[name].is_required():
                d = ctx.today  # a required date has no contract default to supply today
            if d is None:
                out.pop(name, None)  # absent: the contract's default (today)
            else:
                out[name] = d.isoformat()
        elif kind == "document":
            _document(ctx, v, name, presence, out, text, session, path)
        elif kind == "text":  # free text only as the owner wrote it; otherwise the contract's default
            if path in session.bindings:
                out[name] = session.bindings[path]
                continue
            span = _written_span(ws, out.get(name))
            if v.get("words") == "methods":  # a method phrase the owner wrote decides, whatever the model passed
                written, unknown = method_phrases(text), unknown_methods(text)
                given = _as_written(text, span) if span else None
                if unknown:
                    raise session.ask(path, _say("method_unknown", said=unknown[0]), f"{name}_unknown", [], [])
                if len(written) > 1:
                    raise _one_detail(path, session, "payment methods", written, f"{name}_ambiguous")
                if given in written:
                    out[name] = given
                elif len(written) == 1:
                    out[name] = written[0]
                elif written:
                    raise session.ask(path, _say("which", what="payment method", names=", ".join(written)),
                                      f"{name}_ambiguous", written, written)
                elif given:
                    out[name] = given
                else:
                    out.pop(name, None)
            elif span:
                out[name] = _as_written(text, span)
            else:
                out.pop(name, None)
        elif kind == "account":  # only an account the owner named the money going into; otherwise the default
            if path in session.bindings:
                out[name] = session.bindings[path]
                continue
            raw = _raw_words(text)
            said = list(dict.fromkeys(re.sub(r"[,.;:!?]+$", "", " ".join(raw[a:b])) for a, b in account_phrases(text)))
            if len(said) > 1:
                raise _one_detail(path, session, "accounts", said, "account_ambiguous")
            if said:
                out[name] = said[0]
            else:
                out.pop(name, None)
    events = invoice_not_done(ctx, text, spans, out[lines_field], item_rows, emailed,
                              reads_date=any(v["kind"] == "date" for v in mk.values())) \
        if lines_field and _lines_shape(out.get(lines_field)) else []
    return Result(out, events + warehouse_not_done)


# ---------- several writes in one reply: one card, in the owner's order ----------

def owner_order(ctx: Ctx, text: str, calls: list[tuple[object, dict]]) -> list[int]:
    """The calls' indexes in the order the owner wrote their requests: each call sits at the earliest mention of a
    part only it has (its customer, or that customer's email or city; a document number; an item; an amount), parts
    shared with another call aside; a call with no mention found keeps its place after those found, in call order."""
    ws = words(text)
    cust = {r["name"]: r for r in customers(ctx)}

    def at(phrase: str | None) -> int | None:
        if not phrase:
            return None
        pw = _norm(str(phrase)).split()
        for i in range(len(ws) - len(pw) + 1):
            if pw and ws[i:i + len(pw)] == pw:
                return i
        return None

    def parts(contract, args) -> dict[tuple, int | None]:
        out: dict[tuple, int | None] = {}
        for name, v in marks(contract.input_model).items():
            val = args.get(name)
            if v["kind"] == "customer" and val:
                r = cust.get(val, {})
                found = [p for p in (at(val), at(r.get("email")), at(r.get("city"))) if p is not None]
                out[("customer", val)] = min(found, default=None)
            elif v["kind"] == "document" and val:
                num = re.findall(r"\d+", str(val))
                hits = [len(words(text[:mt.start()])) for mt in re.finditer(r"\d+", text)
                        if num and int(mt.group(0)) == int(num[-1])]
                out[("document", val)] = min(hits, default=None)
            elif v["kind"] == "figure" and val is not None:
                hits = [len(words(text[:st])) for d, st, _ in figures(text) if d == _decimal(val)]
                out[("figure", str(val))] = min(hits, default=None)
            elif v["kind"] == "lines" and _lines_shape(val):
                im = [n for n, fv in marks(_line_model(contract.input_model, name)).items() if fv["kind"] == "item"]
                for li in val or []:
                    for n in (li.get(k) for k in im):
                        if isinstance(n, str):
                            out[("item", n)] = min((f.start for f in bounded_names(ws, [n], item=True, bounded=False)),
                                                   default=None)
        return out

    every = [parts(c, a) for c, a in calls]
    keyed = []
    for k, ps in enumerate(every):
        own = {p: i for p, i in ps.items() if not any(p in o for j, o in enumerate(every) if j != k)}
        found = [i for i in (own or ps).values() if i is not None]
        keyed.append((min(found) if found else len(ws) + k, k))
    return [k for _, k in sorted(keyed)]


def describe(contract, args: dict) -> str:
    """A drafted or pending change, named from its canonical arguments only."""
    form = table()["drafts"].get(contract.name)
    try:
        return form.format(**args) if form else contract.name.replace("_", " ")
    except (KeyError, IndexError):
        return contract.name.replace("_", " ")


def one_at_a_time(drafted: tuple[object, dict], pending: tuple[object, dict]) -> dict:
    """The notice for a write the reply did not run: the reply drafts one card."""
    return {"type": "not_done", "part": "one_at_a_time", "capability": "planned.several_changes",
            "text": _say("one_at_a_time", drafted=describe(*drafted), pending=describe(*pending))}


def with_codes(rf: Refusal, session: Session, scope: str = "") -> Refusal:
    """A contract's own question, given an id and single-use codes like the step's, so every ask has one shape and an
    answer by code binds what the choice names. Not a question, or already coded: unchanged."""
    if not is_question(rf.code) or rf.data.get("question_id"):
        return rf
    data = dict(rf.data)
    # without references from the contract, only a customer's or item's label is its stored name, so only those bind;
    # any other choice gets its code but binds nothing (the field is asked again)
    stored = rf.code.startswith(("customer_", "item_"))
    choices = data.pop("choices", None) or [{"label": o, "ref": o if stored else None} for o in rf.options]
    field = scope + (data.pop("field", None) or rf.code.rsplit("_", 1)[0])
    return session.ask(field, rf.message, rf.code, [c["label"] for c in choices], [c.get("ref") for c in choices],
                       **data)


def strict_customers(calls: list[tuple[object, dict]], session: Session, scopes: list[str] | None = None) -> None:
    """Customers are strict across the reply's calls of one operation, by each call's final customer, whether the
    owner named it, the call left it out, or it was taken from a document."""
    seen: set = set()
    for k, (contract, args) in enumerate(calls):
        for name, v in marks(contract.input_model).items():
            if v["kind"] == "customer" and args.get(name):
                if (contract.name, args[name]) in seen:
                    raise session.ask(f"{scopes[k] if scopes else ''}{name}", _say("twice", name=args[name]),
                                      "customer_ambiguous", [], [])
                seen.add((contract.name, args[name]))


def write_scopes(ctx: Ctx, text: str, calls: list[tuple[object, dict]]) -> list[str]:
    """Each write's name in the reply: its operation and its place among the reply's writes of that operation, in the
    owner's order ("create_invoice#1:"), so a question and its answer belong to one write."""
    rank = owner_order(ctx, text, calls)
    seen: dict[str, int] = {}
    out = [""] * len(calls)
    for k in rank:
        name = calls[k][0].name
        out[k] = f"{name}#{seen.get(name, 0)}:"
        seen[name] = seen.get(name, 0) + 1
    return out


@dataclass
class Occurrence:
    """One recognised value phrase of the request: its kind, its words (start, end) and what it says."""
    kind: str  # figure, number, customer, item, document, date, rate, memo, draft, email, unclear
    start: int
    end: int
    said: str
    value: object = None


NUMERAL = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")  # the exact written forms of a figure


def _core(name: str) -> list[str]:
    """A stored name's core words: its words without stop words, bounding words and number words."""
    t = table()
    skip = STOP | t["bound"] | set(t["quantity"]) | set(t["scales"]) | t["number_and"]
    return [w for w in _norm(name).split() if w not in skip]


def inventory(ctx: Ctx, text: str, items: list[dict]) -> list[Occurrence]:
    """Every recognised value phrase of the request, each once, larger phrases first."""
    t, low, ws = table(), text.lower(), words(text)
    taken: set[int] = set()
    out: list[Occurrence] = []

    def at(ch: int) -> int:
        return len(words(text[:ch]))

    def add(kind: str, a: int, b: int, said: str, value=None) -> None:
        if b > a and not any(i in taken for i in range(a, b)):
            taken.update(range(a, b))
            out.append(Occurrence(kind, a, b, said, value))

    def chars(kind: str, pattern: str, value=None) -> None:
        for mt in re.finditer(pattern, low):
            add(kind, at(mt.start()), max(at(mt.end()), at(mt.start()) + 1), text[mt.start():mt.end()], value)

    for memo in re.finditer(t["memo"], low):  # each memo's own words, to the end of its sentence
        stop = re.search(r"[.;!?](?:\s|$)", text[memo.start():])
        add("memo", at(memo.start()), at(memo.start() + stop.start()) if stop else len(ws), text[memo.start():])
    custs = customers(ctx)
    for p in warehouse_words(text, items, custs, ctx):
        add("warehouse", p.start, p.end, p.said, {"item": p.item, "item_start": p.item_start})
    for mt in re.finditer(r"[^\s,;<>()]+@[^\s,;<>()]+\.[a-z]+", low):  # a customer's email is a mention of it;
        hit = [r["name"] for r in custs if (r.get("email") or "").lower() == mt.group(0)]  # another is emailing
        a = at(mt.start())
        target = set(ws[max(0, a - 3):a]) & t["email"]  # "email it to ap@...": the emailing phrase's own target
        reference = len(hit) == 1 and not target
        add("customer" if reference else "email", a, max(at(mt.end()), a + 1), text[mt.start():mt.end()],
            hit[0] if reference else None)
    chars("document", r"\b[a-z]{2,4}-\d{4}-\d+\b")
    for mt in re.finditer(r"\b(?:invoice|inv|payment|pay)\s*(?:no\.?|number)?\s*#?\s*(\d+)\b", low):
        add("document", at(mt.start(1)), at(mt.end(1)), mt.group(1))
    chars("date", r"\b\d{4}-\d{2}-\d{2}\b")
    chars("date", r"(?<![\w./])\d{1,2}/\d{1,2}(?:/(?:\d{2}|\d{4}))?(?![\w/])")  # as date_phrases reads them
    chars("date", r"(?<![\w./])\d{1,2}\.\d{1,2}\.\d{4}(?![\w.])")
    chars("date", _ago())
    for mt in re.finditer(t["rate"], low):
        add("rate", at(mt.start()), max(at(mt.end()), at(mt.start()) + 1), text[mt.start():mt.end()])
    for f in bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False):
        add("customer", f.start, f.end, " ".join(ws[f.start:f.end]), f.name)
    for r in custs:  # a unique city is a reference to its customer
        city = r.get("city")
        if city and sum(1 for x in custs if x.get("city") == city) == 1:
            cw = _norm(city).split()
            for i in range(len(ws) - len(cw) + 1):
                if cw and ws[i:i + len(cw)] == cw:
                    add("customer", i, i + len(cw), city, r["name"])
    for f in bounded_names(ws, [r["name"] for r in items], item=True, bounded=False):
        add("item", f.start, f.end, " ".join(ws[f.start:f.end]), f.name)
    # date phrases of words, after the stored names: a name holding a calendar word ("Year-End Review") is the name's
    for pattern, delta in t["relative_days"]:
        chars("date", pattern, "today" if delta == 0 else None)
    chars("date", t["vague_dates"])
    chars("date", t["month_end"])
    for i, w in enumerate(ws):
        if w in t["date_vocab"]:
            add("date", i, i + 1, w)
    months = t["months"]
    for i, w in enumerate(ws):
        if w in months:
            a, b = i, i + 1
            while a and (ws[a - 1].isdigit() or re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)", ws[a - 1])):
                a -= 1
            while b < len(ws) and (ws[b].isdigit() or re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)", ws[b])):
                b += 1
            if b - a > 1:
                add("date", a, b, " ".join(ws[a:b]))
        elif w in t["today"] or w in t["yesterday"] or w in t["tomorrow"] or w in t["weekdays"]:
            add("date", i, i + 1, w, "today" if w in t["today"] else None)
    for d, st, en in figures(text):
        while en > st and text[en - 1] == ",":
            en -= 1
        a = at(en) - 1
        if a not in taken:
            add("figure", a, a + 1, text[st:en], d)
    i = 0
    while i < len(ws):  # runs of number words; an article only directly before an item
        j = i
        while j < len(ws) and (ws[j] in t["quantity"] or ws[j] in t["scales"] or
                               (ws[j] in t["number_and"] and j > i and ws[j - 1] in t["scales"])):
            j += 1
        if j > i:
            run = ws[i:j]
            article = all(w in t["articles"] for w in run)
            item_next = any(o.kind == "item" and o.start == j for o in out)
            if not article or item_next:
                add("number", i, j, " ".join(run), number_words(run))
            i = j
        else:
            i += 1
    cust_core = {w: r["name"] for r in custs for w in _core(r["name"])}
    item_core = [(w, r["name"]) for r in items for w in _core(r["name"])]
    for i, w in enumerate(ws):  # a core word of a stored name, outside every other occurrence
        if i in taken:
            continue
        if w in cust_core:
            add("customer", i, i + 1, w, None)
        elif any(w == x or same_word(w, x) for x, _ in item_core):
            add("item", i, i + 1, w, None)
    for i, w in enumerate(ws):  # a calendar word nothing else took, and no unit or rate
        if i not in taken and _calendar(ws, i) and not _unit(ws, i):
            add("date", i, i + 1, w)
    chars("draft", t["draft"])
    for i, w in enumerate(ws):
        if w in t["email"]:
            add("email", i, i + 1, w)
    return sorted(out, key=lambda o: o.start)


def unclear_numerals(text: str) -> list[str]:
    """Figures written in no exact form: "1,5", "1,50", ".5", "- 5", "1 500"."""
    out = []
    for mt in re.finditer(r"(?<![\w-])(-\s+)?([.\d][\d.,]*\d|\d)(?=\s+\d|[^\w]|$)", text):
        raw = mt.group(2).rstrip(".,")
        spaced = bool(mt.group(1)) and (mt.start() == 0 or not text[mt.start() - 1].isalnum())
        after = re.match(r"\s+(\d[\d.,]*)", text[mt.end():])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/(?:\d{2}|\d{4}))?|\d{1,2}\.\d{1,2}\.\d{4}", raw):
            continue  # a figure date
        before = words(text[:mt.start()])
        month = table()["months"].get(before[-1]) if before else None
        year = after.group(1).rstrip(".,") if after else ""
        if not spaced and month and raw.isdigit() and len(raw) <= 2 and len(year) == 4 and year.isdigit() and \
                _recent(date.min, month, int(raw), int(year)) is not None:
            continue  # a named month's day and year, as date_phrases reads them
        if spaced or not NUMERAL.match(raw) or (after and not re.search(r"[a-zA-Z]", text[mt.end():mt.end() + 1])
                                               and NUMERAL.match(after.group(1).rstrip(".,"))):
            out.append(text[mt.start():mt.end()] + (after.group(0) if after else ""))
    return out


def _unclaimed(session: Session, path: str, occ: Occurrence, items: list[dict], customers_: list[dict]) -> None:
    """An occurrence no part of the reply claimed: the owner is asked. One hook, so a test can show the
    accounting carries the weight."""
    if occ.kind == "item":
        cands = [r["name"] for r in _candidates(items, [occ.said], item=True)]
        if len(cands) == 1:
            raise session.ask(path, _say("confirm", name=cands[0]), "item_ambiguous",
                              [cands[0], _say("someone_else")], [cands[0], None], confirm=True)
        if cands:
            raise session.ask(path, _say("which", what="item", names=", ".join(cands)), "item_ambiguous", cands, cands)
    raise session.ask(path, _say("figure_unused", figure=occ.said), f"{occ.kind}_ambiguous", [], [])


def account(ctx: Ctx, text: str, calls: list[tuple[object, dict]], results: list, lead: int | None,
            session: Session, scopes: list[str]) -> None:
    """Coverage by occurrence accounting: each recognised occurrence is claimed by one part of the reply that
    actually appears in it, exactly; anything left asks. `results` are the writes' resolutions, `lead` the write the
    reply drafts (only its not_done rows are emitted)."""
    if not any(marks(c.input_model) for c, _ in calls):
        return  # no write the step resolves: the accounting covers only those
    if any(v["kind"] == "lines" and not _lines_shape(a.get(n)) for c, a in calls for n, v in marks(c.input_model).items()):
        return  # lines the step cannot read go back to the model first (validation); accounting follows their retry
    first = scopes[lead] if lead is not None else scopes[0]
    bad = unclear_numerals(text)
    if bad:
        raise session.ask(f"{first}figure", _say("numeral_unclear", figure=bad[0].strip()), "figure_ambiguous", [], [])
    custs = customers(ctx)
    lined = any(v["kind"] == "lines" for c, _ in calls for v in marks(c.input_model).values())
    items = _items(ctx) if lined else []  # item names are value phrases only where a write has lines
    occs = inventory(ctx, text, items)
    ws = words(text)
    claimed: set[int] = set()

    def claim(kind: str, test) -> Occurrence | None:
        for n, o in enumerate(occs):
            if n not in claimed and o.kind == kind and test(o):
                claimed.add(n)
                return o
        return None

    def mentions(name: str, o: Occurrence) -> bool:
        if o.value is not None:
            return o.value == name
        return o.said in _core(name) or any(same_word(o.said, w) for w in _core(name))

    lines_of: dict[int, str] = {}  # an item occurrence a line claimed, and that line's item
    for k, (contract, args) in enumerate(calls):
        mk = marks(contract.input_model)
        flagged = [n for n, v in mk.items() if v["kind"] == "flag" and n in args]
        for name, v in mk.items():
            val = args.get(name)
            if v["kind"] == "customer" and val:  # one mention of it, if the owner wrote one (else answered or derived)
                claim("customer", lambda o, val=val: mentions(val, o))
                confirmed_mentions = sum(s["kind"] == "customer" and s["name"] == val and
                                         s.get("classification") == "name"
                                         for msg in session.owner_messages for s in msg["spans"])
                for _ in range(max(0, confirmed_mentions - 1)):
                    claim("customer", lambda o, val=val: mentions(val, o))
            elif v["kind"] == "document" and val:
                claim("document", lambda o, val=val: _same_document(o.said, val) or o.said == str(val))
                party = v.get("party")
                if party:  # a mention of the document's own customer is the document's; another customer's is not
                    who = party(ctx, val)
                    claim("customer", lambda o, who=who: mentions(who, o))
            elif v["kind"] == "figure" and val is not None:  # "at 80": a rate phrase, the figure's own
                if not claim("figure", lambda o, val=val: o.value == _decimal(val)):
                    claim("rate", lambda o, val=val: _rate_figure(o.said) == _decimal(val))
            elif v["kind"] == "date" and val:
                claim("date", lambda o: True)
            elif v["kind"] in ("text", "account") and val:  # what lies inside its own phrase is its own
                span = _written_span(ws, val)
                if span:
                    a, b = span
                    for n, o in enumerate(occs):
                        if n not in claimed and ((a <= o.start and o.end <= b) or
                                                 (o.kind == "memo" and (o.start + 1, o.end) == (a, b))):
                            claimed.add(n)
            elif v["kind"] == "lines" and _lines_shape(val):
                lm = marks(_line_model(contract.input_model, name))
                im = next((n for n, fv in lm.items() if fv["kind"] == "item"), None)
                qm = next((n for n, fv in lm.items() if fv["kind"] == "quantity"), None)
                wm = next((n for n, fv in lm.items() if fv["kind"] == "warehouse"), None)
                for line_index, li in enumerate(val or []):
                    it = claim("item", lambda o, li=li: mentions(li.get(im), o))
                    warehouse_path = f"{scopes[k]}{name}.{line_index}.{wm}"
                    if wm and (li.get(wm) is not None or session.bindings.get(warehouse_path) is False):
                        bound = warehouse_path in session.bindings
                        claim("warehouse", lambda o, li=li, it=it, bound=bound:
                              o.value["item"] == li.get(im) and it is not None and
                              o.value["item_start"] == it.start and (bound or _norm(o.said) == _norm(li[wm])))
                    q = _decimal(li.get(qm)) if qm else None
                    if q is None:
                        continue
                    if it is None:  # an answered or default item: the quantity still needs the owner's own figure
                        if not claim("figure", lambda o: o.value == q and not _before_an_item(o, occs)):
                            raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                              "quantity_missing", [], [])
                        continue
                    lines_of[it.start] = li.get(im)
                    places = {it.start}  # the quantity written just before its own item (or its unit and "of")
                    if it.start >= 2 and ws[it.start - 1] == "of":
                        places.add(it.start - 2)
                    there = [n for n, o in enumerate(occs) if o.kind in ("figure", "number") and o.end in places]
                    if there:
                        o = occs[there[0]]
                        scale = o.kind == "number" and o.value is not None and any(
                            p.kind == "figure" and p.end == o.start for p in occs)
                        lead_fig = next((p for p in occs if p.kind == "figure" and p.end == o.start), None) if scale \
                            else None
                        if there[0] not in claimed and o.value == q:
                            claimed.add(there[0])
                            continue
                        if lead_fig and lead_fig.value * o.value == q and there[0] not in claimed \
                                and occs.index(lead_fig) not in claimed:
                            claimed.update({there[0], occs.index(lead_fig)})  # "2 dozen"
                            continue
                        raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                          "quantity_missing", [], [])
                    if not claim("figure", lambda o: o.value == q and not _before_an_item(o, occs)):
                        raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                          "quantity_missing", [], [])
        if flagged:
            same = sum(1 for c, _ in calls if c.name == contract.name)
            if same > 1 and any(o.kind == "draft" for o in occs):
                raise session.ask(f"{scopes[k]}{flagged[0]}", _say("which_draft"), "draft_ambiguous", [], [])
            claim("draft", lambda o: True)
        if k == lead:  # the rows the reply emits claim their phrases
            for e in results[k].not_done:
                kind = {"email": "email", "date": "date", "rate": "rate", "memo": "memo"}.get(e.get("part"))
                while kind and claim(kind, lambda o: True):
                    pass
    if any(v["kind"] == "lines" for c, _ in calls for v in marks(c.input_model).values()):
        while claim("date", lambda o: o.value == "today"):
            pass  # a new invoice is dated today: "today" restates that default (a submit keeps its own date: asked)
    price = {r["name"]: Decimal(r["standard_rate"]) for r in items}

    def own_price(o: Occurrence) -> bool:  # the rate's own item, the nearest a line claimed before it
        before = [p for p in lines_of if p < o.start]
        return bool(before) and _rate_figure(o.said) == price.get(lines_of[max(before)])
    while claim("rate", own_price):
        pass  # a rate equal to its own item's list price restates the default the card uses
    left = [o for n, o in enumerate(occs) if n not in claimed]
    rank = {"item": 0, "customer": 1, "document": 2}  # the question about a record first, then the rest
    for o in sorted(left, key=lambda o: (rank.get(o.kind, 3), o.start)):
        _unclaimed(session, f"{first}{o.kind}", o, items, custs)


def _rate_figure(said: str) -> Decimal | None:
    found = figures(said)
    return found[0][0] if found else None



def _before_an_item(o: Occurrence, occs: list[Occurrence]) -> bool:
    """A figure written just before an item's words is that item's quantity, never another line's."""
    return any(p.kind == "item" and p.start == o.end for p in occs)


def write_intents(text: str) -> int:
    """How many changes the owner asked for: each write verb with its document, once ("send Wayne an invoice" is one;
    "and submit it" is the same write; "and submit invoice 8" is another)."""
    t, ws = table(), words(text)
    count = 0
    for i, w in enumerate(ws):
        if w not in t["write_verbs"]:
            continue
        near = ws[max(0, i - 3):i]
        if i and ws[i - 1] in (t["articles"] | t["determiners"]):
            continue  # "an invoice", "as a draft": a noun, not a second intent
        if w == "invoice" and set(near) & t["write_verbs"]:
            continue  # "send Wayne an invoice", "submit invoice 8": the document of the verb before it
        if i + 1 < len(ws) and ws[i + 1] in t["same_document"] and count and not _names_a_document(ws, i + 2):
            continue  # "and submit it": the write already asked for ("submit this invoice 8" names another)
        count += 1
    return count


def _names_a_document(ws: list[str], j: int) -> bool:
    """Whether the words from j name a document by its number: "invoice 8", "INV-2026-00008"."""
    def digits(k: int) -> bool:
        return k < len(ws) and any(ch.isdigit() for ch in ws[k])
    return digits(j) or (j < len(ws) and ws[j] in table()["document_nouns"] and digits(j + 1))

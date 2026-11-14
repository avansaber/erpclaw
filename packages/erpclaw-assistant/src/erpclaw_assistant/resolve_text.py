"""Resolution text."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import field
from contextvars import ContextVar
from functools import cache
from sqlalchemy import select
from erpclaw_core import models as m
from erpclaw_core.catalogue import ONE_CHANGE
from erpclaw_core.common import Ctx, Refusal


TABLES = {
    "en": {
        # table version 2, closed: a word not listed does not bound a name. Version 2
        # added the words that start another field or clause: dated, memo, note, reference, due, in, on.
        "bound": frozenset({"invoice", "bill", "charge", "send", "email", "paid", "pay", "received", "for", "to",
                            "from", "of", "by", "at", "and", "with", "please", "the", "our", "my", "customer",
                            "client", "dated", "memo", "note", "reference", "due", "in", "on", "delivered", "shipped"}),
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
        # Correction cues take precedence over read wording on a pending card.
        "correction_cue": r"\b(?:no|not|meant|instead)\b",
        "marked_price_prefix": r"\s*(?:actually|make\s+it|no\s*,)\s*",
        "marked_rate_tail": r"(?:\s+each|\s+piece)",
        "marked_rate_tail_needs_per": "piece",
        "marked_rate_per_ending": "per",
        "marked_rate_tail_needs_currency": "each",
        # A self-contained read remains a read when a card is pending.
        "read_request": (r"\b(?:check|show|list|display)\b.*"
                         r"\b(?:customers?|suppliers?|items?|products?|services?|invoices?|payments?|"
                         r"orders?|records?|sales|receivables|details)\b|"
                         r"\b(?:who|what|which)\s+(?:(?:are|is)\s+(?:our|the)\s+)?"
                         r"(?:customers|suppliers|items|products|services|invoices|payments|orders|records)\b"
                         r"(?:\s+(?:do|are|have)\b|(?=\s*[?.!]*\s*$))|"
                         r"\b(?:what(?:['’]s|\s+is)|which|where(?:['’]s|\s+is))\s+[^?.!,]+?['’]s\s+\w+\b|"
                         r"\b(?:what(?:['’]s|\s+is)|which|where(?:['’]s|\s+is))\s+(?:the\s+)?\w+\s+of\s+[^?.!,]+|"
                         r"\b(?:show|display)\s+me\s+[^?.!,]+|"
                         r"\bwho\s+(?:owes?|owed)\b|"
                         r"\b(?:how\s+much|what)\s+(?:did|do|have|has)\b.*\b(?:sell|sold)\b"),
        "either": frozenset({"either"}),
        # an owner's typed answer to the engine's pending question, matched before the model runs; the control
        # actions are matched by these words, or by their codes
        "answers": {
            "continue_invoice": frozenset({"yes", "y", "yeah", "yep", "yes please", "continue", "continue please", "go ahead", "go on"}),
            "decline_invoice": frozenset({"no", "no thanks", "no thank you", "cancel", "stop", "never mind", "nevermind"}),
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
        "model_labels": {"lines": "goods or services", "figure": "amount", "quantity": "number",
                         "item": "goods or service", "document": "document", "date": "date",
                         "customer": "customer", "supplier": "supplier", "account": "account",
                         "warehouse": "warehouse", "text": "detail", "flag": "choice", "off": "choice",
                         "invoice_line": "line", "default": "detail"},
        "model_field_labels": {"name": "name", "currency": "currency", "email": "email",
                               "city": "city", "unit": "unit", "price": "price", "reason": "reason",
                               "customer": "customer", "supplier": "supplier", "items": "goods or services",
                               "item": "item", "quantity": "number", "warehouse": "warehouse",
                               "submit": "submit", "amount": "amount", "invoice": "invoice",
                               "oldest_first": "oldest invoices first", "date": "date", "account": "account",
                               "method": "payment method", "reference": "reference", "payment": "payment",
                               "delivery_date": "delivery date", "order": "order", "line": "line",
                               "everything": "everything", "lines": "lines", "bank_lines": "bank balances",
                               "supplier_lines": "supplier balances", "counted": "counted", "rate": "rate",
                               "old_reference": "old reference", "due_date": "due date"},
        "model_field_formats": {"quantity": "quantity", "counted": "quantity", "amount": "figure",
                                "price": "figure", "rate": "figure"},
        # directly before an item name: its quantity, and a bound for the item field only
        "quantity": {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                     "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                     "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                     "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90},
        # number words that multiply what comes before them ("five hundred", "two dozen")
        "scales": {"dozen": 12, "hundred": 100, "thousand": 1000},
        "record_count_suffix": {
            "set_up_company": r"(?:(?:separate|legal)\s+)*companies\b",
        },
        "record_count_reference": r"\bthe\s+({number})\s+is\s+for\s+$",
        # "one" before an item, but an article after a name ("send Wayne Enterprises an invoice"): never a bound there
        "articles": frozenset({"a", "an"}),
        # a write intent is one of these verbs with its document (the one-write gate); "invoice" after another intent
        # verb, an article or a determiner is that document, not a second intent
        "write_verbs": frozenset({"invoice", "bill", "charge", "submit", "cancel", "void", "reverse", "record", "raise",
                                  "create", "send", "issue", "draft", "pay", "paid", "post"}),
        "invoice_completion": frozenset({"post", "submit"}),
        "determiners": frozenset({"the", "this", "that", "these", "those", "another", "new", "first", "second", "next",
                                  "last", "my", "our", "their", "its", "his", "her", "your"}),
        # a verb aimed at the document already in hand is the same write ("and submit it")
        "same_document": frozenset({"it", "this", "them", "that"}),
        "idiomatic_one_before": frozenset({"this", "the", "that"}),
        "idiomatic_one_after": ("more", "time"),
        "absent_one": r"\b(?:don'?t|do\s+not)\s+have\s+one\b",
        "absent_email": r"\b(?:(?:no|without)\s+(?:an?\s+)?email|(?:don'?t|do\s+not)\s+have\s+(?:an?\s+)?email|"
                        r"skip\s+(?:the\s+)?email|leave\s+(?:the\s+)?email\s+(?:blank|out)|"
                        r"email\s*[:,;\-]?\s*(?:none|leave\s+it\s+(?:blank|out)|skip\s+it|without\s+it|"
                        r"(?:don'?t|do\s+not)\s+have\s+it)|"
                        r"(?:leave\s+it\s+(?:blank|out)|skip\s+it|without\s+it)\s*[,;:\-]?\s*(?:the\s+)?email)\b",
        "generic_quantity_units": frozenset({"unit", "units"}),
        # Payment wording and the link to its numbered invoice in a single clause.
        "payment_intent_verbs": frozenset({"record", "log", "pay", "paid"}),
        "payment_explicit_verbs": frozenset({"pay", "paid"}),
        "payment_nouns": frozenset({"payment"}),
        "payment_reference_prepositions": frozenset({"on", "against", "for", "to"}),
        "intent_clause_links": frozenset({"and", "then"}),
        "intent_separator": r";\s*|\.\s+|\s+[-–—]\s+",
        "intent_separator_link": "then",
        # the nouns a document is named by, before its number ("invoice 8", "payment 3")
        "document_nouns": frozenset({"invoice", "inv", "payment", "pay"}),
        # "one hundred and five": "and" joins a number phrase only straight after a scale word
        "number_and": frozenset({"and"}),
        # an invoice is saved as a draft (submit false) only when the owner's words ask for one
        "draft": r"\b(?:draft|don'?t submit|do not submit|save it as a draft|not yet)\b",
        # a payment applied to the oldest open invoices first only when the owner's words say so
        "oldest_first": r"\boldest\s+(?:ones?\s+|invoices?\s+)?first\b",
        "everything": r"\beverything\b",
        "paid_now": r"\b(?:paid(?:\s+now)?|paying\s+now)\b",
        "still_owed": r"\b(?:still\s+owe|still\s+owed|on\s+credit|unpaid)\b",
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
        "method_cues": frozenset({"by", "via", "with", "in", "using"}),
        "method_nouns": frozenset({"method"}),
        "method_unknown_cues": frozenset({"by", "via", "with", "in", "using"}),  # "via Zelle": a method-like phrase the table does not know  # "by method Zelle", "payment method: check"
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
        "rate": r"(?:(?:\bpriced\s+at\b|\bat\b|@)\s*[^\w\s]?\s*|(?<!\w)(?:<currency_mark>)\s*)(\d[\d,]*(?:\.\d+)?)(?![\w,])|"
                r"(\d[\d,]*(?:\.\d+)?)\s*(?:(?:<currency_mark>|<currency_word>)|\beach\b|\bper\b|\ba\s+(?:piece|unit)\b|\bapiece\b)",
        "ordinary_rate": r"(?:\bpriced\s+at\b|\bat\b|@)\s*[^\w\s]?\s*(\d[\d,]*(?:\.\d+)?)|"
                         r"(\d[\d,]*(?:\.\d+)?)\s*(?:\beach\b|\bper\b|\ba\s+piece\b|\bapiece\b)",
        "percent_figure": r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent\b|per\s+cent\b|pct\b|pc\b)",
        "percent_price": r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent\b|per\s+cent\b|pct\b|pc\b)\s*(?:off\b|of\s+(?:the\s+)?list\b)",
        "compact_currency_rate": r"(?<!\w)(?:[A-Za-z]{3}|[^\w\s])\s*(\d[\d,]*(?:\.\d+)?)(?=each\b)",
        "delivery_date_clause": (
            r"\b(?:to\s+be\s+(?:delivered|shipped)|delivered|deliver|ship|shipped)\s+by\s+"
            r"(?:(?:next|this|coming|last)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"tomorrow|today|yesterday|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
            r"\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+(?:<months>)(?:\s+\d{4})?|"
            r"(?:<months>)\s+\d{1,2}(?:\s*,?\s+\d{4})?)\b"),
        "negated_invoice_day": r"\bnot\s+(?:today|yesterday|tomorrow)\b",
        "memo": r"\b(?:note|memo|reference|po)\b(?=[\s:#.;!?]|$)[ \t]*[:#]?[ \t]*"
                r"(?:[^\s.;!?]|(?=[.;!?]?(?:\s|$)))",
        "memo_document": r"\bcredit\s+note\b",
        "memo_date_choice_code": "memo_date_choice_missing",
        # Display metadata for the recognised occurrence.
        "memo_qualifiers": {"note": ("note", "nd_memo"), "memo": ("memo", "nd_memo"),
                            "reference": ("reference", "nd_reference"), "po": ("PO number", "nd_po")},
        "memo_qualifier_default": "memo",
        "memo_qualifier_cues": ((r"memo\b\s*[:#]?\s*po\b", "po"),),
        "po_memo_prefixes": ("po ", "po:", "po#"),
        "text": {
            "add_offer_one": "I can't find a {kind} called '{name}'. Which {kind} is it for, or shall I add it first? After approving each card, say continue to proceed with the invoice.",
            "add_offer_many": "{names} aren't in your books yet. Add them first? After approving each card, say continue to proceed with the invoice.",
            "add_offer_it": "Add it",
            "add_offer_them": "Add them",
            "add_offer_no": "No thanks",
            "add_continue": "Ready to continue the invoice.",
            "planned_singular": "{name} is not in ERPClaw yet.",
            "planned_plural": "{name} are not in ERPClaw yet.",
            "none_installed_not_done": "I didn't do anything with that.",
            "none_installed_action": "If you want to {action}, say so with the details.",
            "none_installed_multiple": "If you want to do one of those, say so with the details.",
            "none_found_actions": "I didn't do anything with that. I can {actions}.",
            "none_found_metric": "see {name}",
            "records_one_at_a_time": "ERPClaw handles these one at a time. Let's start with the first.",
            "step_list_invoices_asked": "List {status} invoices{scope}.",
            "step_list_invoices_returned": "Found {count}{amounts}.",
            "step_invoice_none": "none",
            "step_invoice_one": "one invoice",
            "step_invoice_many": "{count} invoices",
            "step_ledger_entry_one": "1 ledger entry",
            "step_ledger_entry_many": "{count} ledger entries",
            "step_invoice_amounts": "; total {total}, still to collect {outstanding}",
            "step_status": {"open": "open", "paid": "paid", "draft": "draft", "cancelled": "cancelled", "all": "all"},
            "step_for": " for {name}",
            "step_get_metric_asked": "Read {label}{scope}.",
            "step_get_metric_returned": "{headline}.",
            "step_metric_empty": "No submitted invoices in the period",
            "step_metric_value": "{label} {value}, across {invoices}{rows}",
            "step_metric_rows": "; leading customers: {rows}",
            "step_find_capability_asked": "Find a matching capability.",
            "step_find_capability_returned": "{outcome}.",
            "step_found": "Found {names}",
            "step_no_match": "No matching capability found",
            "step_choose_capability_asked": "Choose a matching capability.",
            "step_choose_capability_returned": "{outcome}.",
            "step_chosen": "Chose {name}",
            "step_unsure": "No single capability chosen",
            "step_write_asked": "{operation}{scope}.",
            "step_write_returned": "Drafted {title} for approval{parts}.",
            "step_write_operations": {"create_invoice": "Draft an invoice", "record_payment": "Draft a payment",
                                      "cancel_invoice": "Draft an invoice cancellation",
                                      "submit_invoice": "Draft an invoice submission",
                                      "reverse_payment": "Draft a payment reversal",
                                      "add_customer": "Draft a customer", "add_item": "Draft an item",
                                      "create_sales_order": "Draft a sales order",
                                      "deliver_order": "Draft an order delivery",
                                      "create_credit_note": "Draft a credit note"},
            "step_write_titles": {"create_invoice": "the invoice card", "record_payment": "the payment card",
                                  "cancel_invoice": "the invoice cancellation card",
                                  "submit_invoice": "the invoice submission card",
                                  "reverse_payment": "the payment reversal card",
                                  "add_customer": "the customer card", "add_item": "the item card",
                                  "create_sales_order": "the sales order card", "deliver_order": "the delivery card",
                                  "create_credit_note": "the credit note card"},
            "step_parts": {"email": "emailing left out", "rate": "custom rate left out",
                           "memo": "memo left out", "date": "chosen date left out",
                           "warehouse": "warehouse left out"},
            "step_set_up_company_asked": "Prepare the company setup{scope}.",
            "step_set_up_company_returned": "{outcome}.",
            "step_setup_fields": {"name": "business name", "currency": "currency",
                                  "fiscal_year_start_month": "financial year start", "date_order": "date order"},
            "step_setup_field": " with the {field}",
            "step_setup_next": "The {field} is needed next",
            "step_setup_drafted": "Drafted the company setup card for approval",
            "customer_missing": "Which customer is it for?",
            "correction_missing": "What should I change on the card?",
            "no_records": "There are no {what}s yet.",
            "master_no_match": "No {plural} match \"{name_filter}\".",
            "master_all_one": "There is 1 {what} in all.",
            "master_all": "There are {count} {plural} in all.",
            "master_total": "Total {plural}: {count}.",
            "master_page": "{plural_capitalised}, page {page}:",
            "master_empty_page": "There are no {plural} on this page.",
            "master_name": "{name}",
            "master_item": "{name}: {price} {currency} per {unit}",
            "master_more": "Ask for page {next_page} to see more, or ask for a {what} by name.",
            "master_add": "You can add {article} {what}.",
            "unknown": "I can't find a {what} called '{said}'.",
            "confirm": "Do you mean {name}?",
            "someone_else": "someone else",
            # a question's control actions as a client lists them, by name (CONTROLS): coded "<question id>-<name>"
            "controls": {"someone_else": "Someone else", "cancel": "Cancel"},
            "which": "Which {what}: {names}?",
            "figure_unused": "What is the {figure} for? I couldn't place it in this request.",
            "numeral_unclear": "I can't read '{figure}' as one number. Could you write it again, like 1.5 or 1,500?",
            "which_draft": "Which of these invoices should be saved as a draft?",
            "one_change": ONE_CHANGE,
            "model_steps": "I can only do one step at a time here. Which should I do first: {choices}?",
            "model_values": "Which values should I use: {choices}?",
            "model_field": "{field}: {value}",
            "model_join": " or ",
            "model_list_join": ", ",
            "model_line": "{quantity} {item}",
            "model_missing": "not specified",
            "model_yes": "yes",
            "model_no": "no",
            "lines": "I'd put {name} on more lines than you wrote it. How many lines of {name} should there be?",
            "one_at_a_time": ("One change at a time: I've drafted {drafted}. Ask again for {pending} once you've "
                              "approved or declined it."),
            "twice": ("I'd use {name} for more than one change here. Please say it again with the customer for "
                      "each one."),
            "quantity_missing": "How many {plural}?",
            "amount_missing": "How much was it?",
            "document_missing": "Which {what}?",
            "date_unknown": "Which date do you mean?",
            "invoice_future_date": "An invoice can't be dated in the future.",
            "invoice_rate_unavailable": "What rate should I use for this invoice?",
            "invoice_rate_conflict": "You said {values} each for {item}: which should I use?",
            "date_ambiguous": "Do you mean {a} or {b}?",
            "date_which": "Which date: {dates}?",
            "nothing_saved": "Nothing has been saved yet.",
            "cancelled": "Cancelled; nothing was saved.",
            # a message while a turn holds the conversation: working (it renewed lately) or stopped partway
            "busy": "I'm still working on your last message.",
            "turn_lapsed": "That took too long, so I stopped before finishing your last message. "
                           "Please check what is already there, and send it again if it is still missing.",
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
            "tax_draft_next": ("Sales tax isn't available yet, so I haven't drafted this invoice. "
                               "If you send the same invoice without tax words, I will draft it with no tax line and no tax is calculated."),
            "tax_submit_next": ("Sales tax isn't available yet, so I haven't submitted this invoice. "
                                "If you send the same invoice without tax words, I will submit it without tax."),
            "discount": "Discounts aren't available yet, so I haven't drafted this invoice.",
            "terms": "Setting a due date or payment terms isn't available yet, so I haven't drafted this invoice.",
            "nd_email": "I've drafted the invoice; emailing it isn't available yet, so it won't be sent.",
            "nd_rate": ("You said {said} each for {item}; the card uses the list price, {list}. Setting a rate isn't "
                        "available yet."),
            "nd_date": "Invoices are dated today; choosing an invoice date isn't available yet.",
            "nd_delivery_date": "A delivery date cannot be set yet.",
            "date_today_or_leave": "Shall I date it today, or leave it?",
            "date_today": "Date it today",
            "rate_list_or_leave": "Shall I use the list price, or leave it?",
            "rate_list": "Use the list price",
            "memo_date_question": "Is '{said}' part of the memo, or the invoice date?",
            "memo_date_part": "Part of the memo",
            "memo_date_invoice": "The invoice date",
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
            "supplier_exists": "{name} is already a supplier.",
            "supplier_near": "Did you mean {name} ({city}), already a supplier, or is {said} a new one?",
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



MESSAGE_BREAK = "zzmessagebreakzz"



for _t in TABLES.values():
    _t["bound"] = _t["bound"] | {MESSAGE_BREAK}
    _t["phrase_stop"] = _t["phrase_stop"] | {MESSAGE_BREAK}



@cache
def _filled_rate(pattern: str) -> str:
    # Import lazily: the currency catalogue uses this module for language text.
    from .resolve_amounts import CODE_WORDS, ISO_4217, SYMBOLS
    marks = sorted(SYMBOLS, key=len, reverse=True)
    codes = sorted(ISO_4217 - CODE_WORDS)
    currency = "|".join([*(re.escape(mark.lower()) for mark in marks),
                         *(code.lower() + r"\b" for code in codes)])
    words = "|".join(code.lower() + r"\b" for code in sorted(CODE_WORDS))
    return pattern.replace("<currency_mark>", currency).replace("<currency_word>", words)


_invoice_rate_context = ContextVar("invoice_rate_context", default=False)


def table(lang: str | None = None) -> dict:
    t = TABLES[lang or LANGUAGE]
    invoice_rate = _filled_rate(t["rate"])
    return {**t, "invoice_rate": invoice_rate,
            "rate": invoice_rate if _invoice_rate_context.get() else t["ordinary_rate"]}


def _memo_phrases(text: str):
    """Cue and value spans confined to their sentence and owner message, including empty cues."""
    low = text.lower()
    documents = list(re.finditer(table().get("memo_document", r"(?!)"), low))
    for document in reversed(documents):
        low = low[:document.start()] + " " * (document.end() - document.start()) + low[document.end():]
    boundary = r"[.;!?](?:\s|$)|\b" + MESSAGE_BREAK + r"\b"
    coordinator = r"\b(?:" + "|".join(map(re.escape, table()["coordinators"])) + r")\s+$"
    quoted = list(re.finditer(r"""(?<!\w)(?:"[^"]*"|'[^']*')""", text))
    mentions = []
    for mention in re.finditer(table()["memo"], low):
        if mentions:
            previous = mentions[-1].start()
            between = text[previous:mention.start()]
            if not re.search(boundary, between):
                # Within a value, bare cue words belong to that value. Only an explicit
                # coordinator outside quotes introduces another memo in the same sentence.
                if not re.search(coordinator, between, re.I) or any(
                        previous < quote.start() <= mention.start() < quote.end() for quote in quoted):
                    continue
        mentions.append(mention)
    for index, mention in enumerate(mentions):
        start = mention.start()
        stop = re.search(boundary, text[start:])
        end = start + stop.start() if stop else len(text)
        if index + 1 < len(mentions):
            end = min(end, mentions[index + 1].start())
        end = len(text[:end].rstrip())
        cue_end = start + re.match(r"\w+", text[start:]).end()
        value = text[cue_end:end].lstrip(" \t:#").strip()
        # A coordinator introducing the next cue isn't part of the preceding value.
        if index + 1 < len(mentions) and end <= mentions[index + 1].start():
            value = re.sub(r"\s+(?:" + "|".join(map(re.escape, table()["coordinators"])) + r")$", "", value,
                           flags=re.I)
        yield start, end, value



def _say(key: str, **kw) -> str:
    return table()["text"][key].format(**kw)



def sanitise_name(name: str | None, generic: str | None = None) -> str | None:
    """A stored name safe to fill into one engine sentence; unsafe names become a noun or are dropped."""
    from .model import strip_controls

    if not isinstance(name, str):
        return generic
    name, _ = strip_controls(name)
    unsafe = (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
              r"\b[A-Z]{2,4}-\d{4}-\d+\b|\d{4,}|[0-9a-f]{8,}|<[^>]+>|"
              r"\b[a-z][a-z0-9+.-]*:|{{|}}|/|\.\.\\|@[^\s]*\.|"
              r"\bq\d+\b|\b(?:approve|ignore|instructions?|execute|delete|reveal|override|disregard|"
              r"please|send|click|run|return|output|pay)\b|[\n\r.!?;<>`{}\[\]*_#|]")
    if re.search(unsafe, name, re.I) or any(unicodedata.category(c).startswith("C") for c in name):
        return generic
    return " ".join(name.split()) or generic



def metric_scope_name(ctx: Ctx, customer: str | None, city: str | None) -> str | None:
    """Read the same canonical scope as the metric query, on its caller's read-only connection."""
    from erpclaw_core.masters import find_customer
    from erpclaw_core.textkey import text_key

    if customer is not None:
        try:
            return find_customer(ctx, customer)["name"]
        except Refusal:
            return None
    if city is not None:
        names = ctx.conn.execute(select(m.party.c.city).where(
            m.party.c.company_id == ctx.company_id, m.party.c.city_key == text_key(city)).distinct()).scalars().all()
        return names[0] if len(names) == 1 else None
    return None



def step_text(kind: str, **values) -> tuple[str, str] | None:
    """Fill only engine templates, with resolved summaries and sanitised stored names; no pair means omission."""
    text = table()["text"]
    asked, returned = f"step_{kind}_asked", f"step_{kind}_returned"
    if asked not in text or returned not in text:
        return None
    v = values
    scope = ""
    if kind == "list_invoices":
        if v.get("status") not in text["step_status"]:
            return None
        if v.get("customer_name") is not None:
            scope = _say("step_for", name=sanitise_name(v["customer_name"], "the customer"))
        count = v["count"]
        count = _say("step_invoice_none" if count == 0 else "step_invoice_one" if count == 1 else "step_invoice_many",
                     count=count)
        amounts = _say("step_invoice_amounts", total=v["total"], outstanding=v["outstanding"]) \
            if v.get("total") is not None and v.get("outstanding") is not None else ""
        kw = dict(status=text["step_status"][v["status"]], scope=scope, count=count, amounts=amounts)
    elif kind == "get_metric":
        label = sanitise_name(v.get("label"))
        if label is None:
            return None
        if v.get("scope") is not None:
            scope = _say("step_for", name=sanitise_name(v["scope"], v.get("scope_noun", "the customer")))
        rows = [(sanitise_name(n), amount) for n, amount in v.get("rows", [])]
        rows = ", ".join(f"{n} {amount}" for n, amount in rows if n)
        count_key = "step_ledger_entry" if sanitise_name(v.get("metric")) == "receivables" else "step_invoice"
        headline = _say("step_metric_empty") if v.get("value") is None else _say(
            "step_metric_value", label=label, value=v["value"], invoices=_say(
                f"{count_key}_one" if v["row_count"] == 1 else f"{count_key}_many", count=v["row_count"]),
            rows=_say("step_metric_rows", rows=rows) if rows else "")
        kw = dict(label=label, scope=scope, headline=headline)
    elif kind == "find_capability":
        names = [n for d in v.get("descriptions", []) if (n := sanitise_name(d.partition(". ")[0].rstrip(".")))]
        if v.get("descriptions") and not names:
            return None
        kw = dict(outcome=_say("step_found", names=", ".join(names)) if names else _say("step_no_match"))
    elif kind == "choose_capability":
        description = v.get("description")
        name = sanitise_name(description.partition(". ")[0].rstrip(".")) if description else None
        if v.get("description") and name is None:
            return None
        kw = dict(outcome=_say("step_chosen", name=name) if name else _say(
            "step_no_match" if v.get("pick") == "none" else "step_unsure"))
    elif kind == "write":
        operation = sanitise_name(v.get("operation"))
        if operation is None:
            return None
        names = [n for name in v.get("names", []) if (n := sanitise_name(name))]
        if names:
            scope = _say("step_for", name=", ".join(dict.fromkeys(names)))
        elif len(v.get("names", [])) == 1:
            scope = _say("step_for", name=v.get("name_noun", "the customer"))
        parts = [text["step_parts"][p] for p in dict.fromkeys(v.get("parts", [])) if p in text["step_parts"]]
        kw = dict(operation=operation, scope=scope, title=sanitise_name(v.get("title"), "the card"),
                  parts="; " + ", ".join(parts) if parts else "")
    elif kind == "set_up_company":
        field = text["step_setup_fields"].get(v.get("field"))
        if not field and not v.get("drafted"):
            return None
        kw = dict(scope=_say("step_setup_field", field=field) if field else "", outcome=(
            _say("step_setup_drafted") if v.get("drafted") else _say("step_setup_next", field=field)))
    else:
        return None
    return _say(asked, **kw), _say(returned, **kw)

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

The domain modules own the implementation; this module preserves the import surface.
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
from erpclaw_core.masters import STOP, _distance, _near, _norm, customers, regular_plural, same_word
from erpclaw_core.money import fmt_rate
from erpclaw_core.plugins import call_service
from .resolve_text import (
    TABLES,
    LANGUAGE,
    MESSAGE_BREAK,
    _t,
    table,
    _say,
    sanitise_name,
    metric_scope_name,
    step_text,
)
from .resolve_words import (
    _occurrence_ranges,
    words,
    punct_after,
    bounded_names,
    _collides,
    _raw_in,
    _candidates,
    _leaves,
    _raw_words,
    _as_written,
    _written_span,
    without_names,
    _alternatives,
    _outside,
    _has_run,
    consumed_ranges,
    _core,
)
from .resolve_types import (
    Found,
    OutgoingAccountWords,
    Result,
    marks,
    _line_model,
    _items,
    reply_peers,
    _lines_shape,
    WarehouseWords,
    Occurrence,
)
from .resolve_amounts import (
    ISO_4217,
    CODE_WORDS,
    DOLLARS,
    SYMBOLS,
    _DINARS,
    _PESOS,
    _RUPEES,
    CURRENCY_WORDS,
    _is_code,
    _money_closing_end,
    _marks_at,
    figures,
    _word_at,
    _amount_marker_ranges,
    _amount_marker_words,
    _has_amount_marker,
    _decimal,
    literal_figure,
    number_words,
    UNCLEAR,
    _figure_before,
    _quantity_before,
    _plural,
    _quantity_before_unit,
    literal_quantity,
    _MONEY_LOOKALIKES,
    _MONEY_NUMBER,
    _MONEY_PERCENT,
    MONEY_PATTERNS,
    mask_money_spans,
    normalise_money,
    money_area,
)
from .resolve_state import (
    Planned,
    Session,
    CONTROLS,
    _acknowledgements,
    keeps_card,
    match_answer,
    _one_detail,
)
from .resolve_dates import (
    _unit,
    _calendar,
    _ago,
    _touched,
    _touched_text,
    _recent,
    date_phrases,
    _iso,
    resolve_date,
)
from .resolve_records import (
    _missing,
    _named_records,
    _pointed_elsewhere,
    _owner_words_taken,
    without_ruled_out,
    _refers_to,
    _one_per_call,
    resolve_record,
    _resolve_record,
    document_mentions,
    _same_document,
    _alternative_documents,
    _canonical_document,
    _document,
)
from .resolve_payment import (
    _phrase_stops,
    _run,
    payer_phrases,
    account_phrases,
    payment_name_spans,
    guard_payment_names,
    payment_accounts,
    outgoing_account_phrases,
    method_spans,
    method_phrases,
    unknown_methods,
)
from .resolve_warehouse import (
    _company_warehouse_names,
    _trailing_date_start,
    warehouse_words,
    receipt_destination_phrases,
    _warehouse_phrases,
    _warehouse,
)
from .resolve_inventory import (
    NUMERAL,
    inventory,
    unclear_numerals,
    _unclaimed,
    account as _account,
    _rate_figure,
    _before_an_item,
)
from .resolve_policy import (
    _due_date_asked,
    record_message_names,
    confirmation_choice_message,
    guard_messages,
    planned_money,
    planned_stock,
    NOT_DONE,
    _date_phrase,
    _payment_date_text,
    date_source_of,
    memo_qualifier,
    invoice_not_done,
)
from .resolve_args import (
    SINGLE_FIELDS,
    request_words,
    _payer,
    payment_binding,
    _outgoing_account,
    resolve_args,
    owner_order,
    describe,
    one_at_a_time,
    with_codes,
    strict_customers,
    write_scopes,
    write_intents,
    _names_a_document,
)


def account(ctx, text, calls, results, lead, session, scopes):
    """Enable currency-marked rate occurrences only while accounting for invoice lines."""
    from .resolve_text import _invoice_rate_context
    invoice_only = bool(calls) and all(c.name in {"create_invoice", "submit_invoice"} for c, _ in calls)
    token = _invoice_rate_context.set(invoice_only)
    try:
        return _account(ctx, text, calls, results, lead, session, scopes)
    finally:
        _invoice_rate_context.reset(token)

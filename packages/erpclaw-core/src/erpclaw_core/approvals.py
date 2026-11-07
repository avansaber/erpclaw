"""Approval cards and operation keys.

A card names its drafter, the drafter's grants version, the engine and plugin
versions, the normalised arguments, the read set (each row's digest), the
preview the owner sees, a random nonce, its expiry and its operation key; its
digest covers all of them. Propose needs `draft`; a conversation holds at most
one pending card, so a new card supersedes the conversation's pending one,
whatever its operation, and says so.

A conversation's fence decides which turn may make or
approve a card: a generation that only grows and a live token, set by
`activate` under the fence row's lock. A card made with a request token is
fenced: `propose` and `approve` refuse it unless its token is live. A card that
ends without a commit keeps its decision, so a repeated approval returns
the same events and writes nothing.

Approve and commit are one request and one transaction, by an authenticated
principal holding `approve` and `post`, sending the card's digest. Locks are
taken in the engine's one order: principal rows by id, the card,
the company's chain head, the fiscal years and periods of the read set, then
the read set's document rows, by table then id. The preview and the read set
are recomputed under those locks; any difference supersedes the card and
proposes a fresh one. The effect runs under a savepoint: the contract's
commit, then every check its write services registered on the commit's
context, in registration order. A refusal from either rolls back the effect
but not the claim, and the operation outcome, committed or refused (a refusal
keeps its code, options and data), is written in the same transaction as the
effect: both exist or neither does. A replay of the same card returns the
stored outcome. The fence comes before the card in that order: principals,
the conversation fence, the card, then the rest.
"""
from __future__ import annotations

from . import approval_scope

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from . import ledger, models as m
from . import principals as pr
from .common import Ctx, Refusal, audit, company_ctx, new_id, now
from .contracts import (Contract, Result, amount_dicts, check_amount_names, check_card_amounts, no_date_sentence,
                        planned_capability, planned_reply)
from .db import metadata, read_only
from .ledger import _for_share, _for_update
from .plugins import REGISTRY, ensure_enabled

VALID_FOR = timedelta(minutes=10)
FOOTER = "Valid 10 minutes. Rechecked before saving."


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class Outcome:
    status: str  # card | committed | refused | superseded | expired | declined | invalidated | unknown
    #              | replaced_card_ended (propose's `replaces` found the card already ended)
    text: str
    approval_id: str | None = None
    card: dict | None = None
    operation_key: str | None = None
    figures: list[str] | None = None
    digest: str | None = None
    data: dict = field(default_factory=dict)
    amounts: list[dict] = field(default_factory=list)  # a committed result's amounts, as they cross


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp(dt: datetime) -> str:
    return _aware(dt).astimezone(timezone.utc).isoformat(timespec="microseconds")


def impl_version(contract: Contract) -> str:
    """The engine's version and the contract's plugin artifact: a card is bound to the code that previewed it."""
    p = REGISTRY.plugins[REGISTRY.contract_plugin[contract.name]]
    return f"{version('erpclaw-core')}+{(p.artifact_digest or 'core')[:16]}"


def _row_digest(conn: Connection, table: str, key: str) -> str:
    t = metadata.tables[table]
    [pk] = list(t.primary_key.columns)
    row = conn.execute(select(t).where(pk == key)).mappings().first()
    body = None if row is None else {k: (None if v is None else str(v)) for k, v in row.items()}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def read_set(conn: Connection, contract: Contract, ctx, inp) -> list[list[str]]:
    """The contract's declared read set with each row's digest, sorted (table, then key)."""
    rows = sorted(set(contract.read_set(ctx, inp)))
    return [[t, k, _row_digest(conn, t, k)] for t, k in rows]


def digest(fields: dict) -> str:
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


def _fields(card: dict, args: dict, rs: list, preview: dict) -> dict:
    """Everything the digest covers: every card field except its status and timestamps other than expiry."""
    return {"id": card["id"], "principal": card["principal_id"], "company": card["company_id"],
            "conversation": card["conversation_id"], "contract": card["contract"],
            "contract_version": card["contract_version"], "policy_version": card["policy_version"],
            "impl_version": card["impl_version"], "args": args, "read_set": rs, "preview": preview,
            "nonce": card["nonce"], "operation_key": card["operation_key"], "expires_at": _stamp(card["expires_at"])}


DATE_SOURCE_MAX = 80  # the longest phrase of the owner's a card quotes as its date's source


def _date_source_line(source: dict, no_date: str) -> list[str]:
    """The card's line for its date source: the owner's phrase as quoted data, never markup; for a write that takes no
    date, `no_date`, its contract's own sentence."""
    if source.get("not_applicable"):
        return ["Date source", f"No date applies: {no_date}"]
    if source.get("default"):
        return ["Date source", "No date was read from your words; the card uses its default date."]
    quoted = source["phrase"].replace("\\", "\\\\").replace("'", "\\'")
    return ["Date source", f"From your words: '{quoted}' ({date.fromisoformat(source['date']):%d %b %Y})"]


def takes_no_date(ctx, inp) -> bool:
    """Whether this write, for this input, takes no date at all (cancelling a draft changes no books): an input model
    says so with its own `takes_no_date(ctx)`; otherwise every input takes a date or its default."""
    own = getattr(inp, "takes_no_date", None)
    return bool(own and own(ctx))


DOCUMENT_CHANGED = "That document has changed since this card was shown; ask again."


def _recheck_date_source(ctx, inp, stored: dict | None) -> dict | None:
    """A stored date source checked against the books now, at approval's recompute and for a fresh card made then:
    a write that now takes no date, or now takes one, or whose date moved, ends the card instead (no fresh card), so
    a card never states a source its document no longer has. The card's other lines are rebuilt from the books."""
    if stored is None:
        return None
    dateless = takes_no_date(ctx, inp)
    same = (stored == {"not_applicable": True}) == dateless and (
        "phrase" not in stored or getattr(inp, "date", None) == date.fromisoformat(stored["date"]))
    if not same:
        raise Refusal(DOCUMENT_CHANGED, "document_changed")
    return stored


def check_date_source(ctx, contract: Contract, inp, source: dict, owner_words: str | None) -> None:
    """A card's date source, checked by the engine before any card exists: exactly one of its three forms, a phrase
    in the owner's own words and within its cap, a real date equal to the card's own, and "not applicable" exactly
    when the write takes no date."""
    bad = Refusal("That date source can't go on a card.", "date_source_invalid")
    dateless = takes_no_date(ctx, inp)
    if source in ({"default": True}, {"not_applicable": True}):
        if (source == {"not_applicable": True}) != dateless:
            raise bad
        return
    if set(source) != {"phrase", "date"} or dateless or not isinstance(source["phrase"], str) \
            or not isinstance(source["date"], str):
        raise bad
    phrase = source["phrase"]
    if not phrase.strip() or len(phrase) > DATE_SOURCE_MAX or phrase.lower() not in (owner_words or "").lower():
        raise bad
    try:
        said = date.fromisoformat(source["date"])
    except ValueError:
        raise bad from None
    if getattr(inp, "date", None) != said:  # the date the card itself carries
        raise bad


def _preview(ctx, contract: Contract, inp, date_source: dict | None = None) -> dict:
    """The card the owner sees: the contract's preview, with the company first and the validity footer last,
    and its date's source last among its lines when the assistant drafted it."""
    card = contract.preview(ctx, inp).card.as_dict()
    card["lines"] = [["Company", ctx.company_name], *[list(x) for x in card["lines"]]]
    card["footer"] = (card["footer"] + " " if card["footer"] else "") + FOOTER
    card["currency"] = ctx.currency  # the books' functional currency; each amount names its own
    card["amounts"] = [{**a, "index": a["index"] + 1} if a["where"] == "lines" else a for a in card["amounts"]]
    if date_source is not None:  # at the end: no money index moves, and the line carries none
        card["date_source"] = date_source
        card["lines"].append(_date_source_line(date_source, no_date_sentence(contract)))
    check_card_amounts(card)  # a card whose amounts disagree with what the owner reads is never stored or shown
    return card


def _owner(conn: Connection) -> dict:
    return pr.owner(conn)


def _write_contract(contract: Contract) -> None:
    if contract.planned:  # switched off until its release
        raise Refusal(planned_reply(contract), "planned", capability=planned_capability(contract))
    if contract.effect == "read" or contract.read_set is None:
        raise Refusal(f"{contract.name} is not a write with a declared read set.", "not_a_write")


GONE = {"expired": "That card expired after 10 minutes. Ask again and I'll prepare a fresh one.",
        "invalidated": "That card was made before a change to who may do what, so it can no longer be approved. "
                       "Ask again for a fresh one.",
        "declined": "That card was declined; nothing was saved.",
        "superseded": "That card was replaced by a newer one; nothing was saved from it."}
ENDED = "That card belonged to a request that has ended; nothing was saved."
FRESH = "Something changed since that card was made, so nothing was saved. Here is a fresh card."


def _decision(status: str, text: str, refusal: Refusal | None = None, fresh_approval_id: str | None = None) -> str:
    """What ended a card without a commit: a refusal keeps its code, options and complete data (a
    question's field, id and choices with their references), so a repeat restores the question exactly."""
    d = {"status": status, "text": text}
    if refusal is not None:
        d |= refusal_data(refusal)
    if fresh_approval_id:
        d["fresh_approval_id"] = fresh_approval_id
    return canonical(d)


def _end(conn: Connection, approval_id: str, status: str, decision: str, unclaim: bool = False) -> bool:
    """End a pending card with its decision; a conditional update, so it ends a card at most once. `unclaim` ends a
    card whose claim this transaction holds (consumed, not yet committed): it was never approved."""
    a = m.approval
    if unclaim:
        res = conn.execute(update(a).where(a.c.id == approval_id, a.c.status == "consumed").values(
            status=status, decided_at=now(), decision_json=decision, approved_by=None, approver_grants_version=None))
    else:
        res = conn.execute(update(a).where(a.c.id == approval_id, a.c.status == "pending").values(
            status=status, decided_at=now(), decision_json=decision))
    return res.rowcount == 1


def _fence_row(conn: Connection, conversation_id: str, *, share: bool, lock: bool = True) -> dict | None:
    """The conversation's fence row, locked FOR SHARE or FOR UPDATE; unlocked only where a read-only transaction
    shows a card (PostgreSQL takes no row lock in one)."""
    q = select(m.conversation_fence).where(m.conversation_fence.c.conversation_id == conversation_id)
    if lock:
        q = _for_share(conn, q) if share else _for_update(conn, q)
    row = conn.execute(q).mappings().first()
    return dict(row) if row else None


def _token_is_live(fence: dict | None, token: str) -> bool:
    return fence is not None and fence["live_token"] is not None and hmac.compare_digest(fence["live_token"], token)


def _fenced_live(conn: Connection, card: dict, lock: bool = True) -> bool:
    """A card is shown or approved only if it has no token, or its token is its conversation's live one."""
    token = card.get("request_card_token")
    if not token:
        return True
    return _token_is_live(_fence_row(conn, card["conversation_id"], share=True, lock=lock), token)


@dataclass
class Activation:
    status: str  # active | stale
    generation: int  # the fence's generation after the call: the turn's when active, the row's when stale
    set_aside: list[str] = field(default_factory=list)  # pending cards of other tokens, superseded


class _Stale(Exception):
    def __init__(self, generation: int):
        self.generation = generation


def activate(engine: Engine, conversation_id: str, company_id: str, generation: int, token: str) -> Activation:
    """Set the conversation's fence to this turn's generation and token, only if the generation is higher than the
    fence's (a compare-and-set), superseding in the same transaction every pending card of the
    conversation whose token is not this one. A stale generation changes nothing and returns the fence's."""
    if not token:
        raise ValueError("an activation needs a request token")
    try:
        with engine.begin() as conn:
            conn.execute(pg_insert(m.conversation_fence).values(
                conversation_id=conversation_id, company_id=company_id, generation=0, live_token=None,
                updated_at=now()).on_conflict_do_nothing(index_elements=["conversation_id"]))
            fence = _fence_row(conn, conversation_id, share=False)
            if fence["company_id"] != company_id:
                raise Refusal("That conversation belongs to another company.", "fence_company")
            if generation <= fence["generation"]:
                raise _Stale(fence["generation"])  # rolls back: a failed activation writes nothing
            a = m.approval
            others = conn.execute(_for_update(conn, select(a.c.id).where(
                a.c.conversation_id == conversation_id, a.c.status == "pending",
                a.c.request_card_token.is_distinct_from(token)).order_by(a.c.id))).scalars().all()
            for aid in others:
                _end(conn, aid, "superseded", _decision("superseded", ENDED))
            conn.execute(update(m.conversation_fence).where(
                m.conversation_fence.c.conversation_id == conversation_id,
                m.conversation_fence.c.generation < generation).values(
                generation=generation, live_token=token, updated_at=now()))
    except _Stale as st:
        return Activation("stale", st.generation)
    return Activation("active", generation, list(others))


def end_request(engine: Engine, conversation_id: str, generation: int, token: str) -> bool:
    """A request ended: its token is no longer live, at the same generation. Only the fence's own generation and
    token can end it; anything else changes nothing."""
    with engine.begin() as conn:
        fence = _fence_row(conn, conversation_id, share=False)
        if fence is None or fence["generation"] != generation or not _token_is_live(fence, token):
            return False
        conn.execute(update(m.conversation_fence).where(
            m.conversation_fence.c.conversation_id == conversation_id).values(live_token=None, updated_at=now()))
    return True


def set_aside(engine: Engine, conversation_id: str, token: str, approval_id: str, text: str,
              refusal: Refusal | None = None) -> Outcome:
    """A turn that ends with a question, a confirm, a refusal or an answer while a card is pending sets that card
    aside: superseded, with the turn's own decision stored (its question's code, options and choices, or its text),
    so a repeated approval returns that decision. Under the turn's live token, and only while the card is still
    pending, in the lock order fence, then card; an approval that won the card's row is reported, never undone."""
    with engine.begin() as conn:
        if not token or not _token_is_live(_fence_row(conn, conversation_id, share=True), token):
            raise Refusal("That request has ended; ask again.", "fenced")
        row = conn.execute(_for_update(conn, select(m.approval).where(m.approval.c.id == approval_id))
                           ).mappings().first()
        if row is None or row["conversation_id"] != conversation_id \
                or (row["request_card_token"] and row["request_card_token"] != token):
            raise Refusal("That card isn't part of this conversation; nothing was changed.", "replaces_mismatch")
        a = dict(row)
        if a["status"] == "pending":  # expired or invalidated untouched: it ends as that now
            now_status = _status_now(conn, a)
            if now_status != "pending":
                _end(conn, a["id"], now_status, _decision(now_status, GONE[now_status]))
                a["status"] = now_status
        if a["status"] == "pending":
            _end(conn, a["id"], "superseded", _decision("superseded", text, refusal))
            return Outcome("set_aside", text, a["id"], data={"title": _title(a)})
        ended = _ended_text(conn, a)
    return Outcome("replaced_card_ended", ended, approval_id,
                   data={"predecessor": {"approval_id": approval_id, "status": a["status"]}})


def _title(card: dict) -> str:
    return json.loads(card["card_json"]).get("title") or card["contract"].replace("_", " ")


def _ended_text(conn: Connection, a: dict) -> str:
    """What happened to a card that `replaces` found already ended, in the owner's words."""
    if a["status"] == "consumed":
        o = conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == a["operation_key"])
                         ).mappings().first()
        r = json.loads(o["result_json"]) if o else {}
        if o is not None and o["status"] == "committed":
            return f"That card was already approved: {r.get('text') or 'it was saved.'} Your change wasn't applied."
        return f"That card was already approved, and {r.get('text') or 'nothing was saved.'} Your change wasn't applied."
    return f"{GONE.get(a['status'], 'That card can no longer be approved.')} Your change wasn't applied."


def propose(engine: Engine, contract: Contract, args: dict, conversation_id: str | None = None,
            principal: dict | None = None, company_id: str | None = None, request_card_token: str | None = None,
            replaces: str | None = None, date_source: dict | None = None, owner_words: str | None = None) -> Outcome:
    """Build the exact preview and its read set and store a pending card; writes no business row. Needs `draft`.
    In a conversation the new card supersedes the conversation's pending card, whatever its operation, and names it
    in `data["set_aside"]`. With a request token the conversation's fence must hold it live. With
    `replaces`, the named card must be this conversation's and still pending, else no card is made and its outcome
    is returned; the new card takes the named card's token, if it has one, and passes that token's fence.
    The fence and the named card are checked before the preview. While the company is pending (`init --own-books`),
    only its setup is accepted."""
    from .core_contracts import SETUP, say as own_books_say

    if contract.name != SETUP:
        with read_only(engine) as conn:
            q = select(m.company.c.id).where(m.company.c.status == "pending")
            if conn.execute(q.where(m.company.c.id == company_id) if company_id else q).first() is not None:
                raise Refusal(own_books_say("books_not_set_up"), "books_not_set_up")
    return _propose(engine, contract, args, conversation_id, principal, company_id, request_card_token, replaces,
                    date_source=date_source, owner_words=owner_words)


def _propose(engine: Engine, contract: Contract, args: dict, conversation_id: str | None, principal: dict | None,
             company_id: str | None, token: str | None, replaces: str | None, stale_of: dict | None = None,
             date_source: dict | None = None, owner_words: str | None = None) -> Outcome:
    """`stale_of` is approve's stale path: the fresh card replaces that card, which the engine itself chose,
    inherits its token and passes the fence like any tokened proposal."""
    _write_contract(contract)
    if token and conversation_id is None:
        raise ValueError("a request token needs a conversation")
    inp = contract.input_model.model_validate(args)
    norm = inp.model_dump(mode="json")
    with engine.begin() as conn:
        who = principal or _owner(conn)
        ctx = company_ctx(conn, company_id=company_id)
        who = pr.by_id(conn, who["id"])
        pr.require(conn, who, ctx.company_id, "draft")
        ensure_enabled(conn, REGISTRY.contract_plugin[contract.name], contract.name)
        pred_id = replaces or (stale_of or {}).get("id")
        if replaces is not None:
            # its scope first, from columns never updated: the same reply whatever the reason, so nothing of another
            # conversation's card is disclosed
            row = conn.execute(select(m.approval.c.company_id, m.approval.c.conversation_id,
                                      m.approval.c.request_card_token).where(m.approval.c.id == replaces)).first()
            if row is None or row.company_id != ctx.company_id or conversation_id is None \
                    or row.conversation_id != conversation_id or (token and row.request_card_token
                                                                  and row.request_card_token != token):
                raise Refusal("That card isn't part of this conversation; nothing was changed.", "replaces_mismatch")
            token = row.request_card_token or token  # a replacement inherits its predecessor's token
        # the lock order: the conversation's fence, then the predecessor card, then the conversation's pending card;
        # all before the preview, so a card that already ended is reported as it ended, never re-previewed
        if token and not _token_is_live(_fence_row(conn, conversation_id, share=True), token):
            if stale_of is None:
                raise Refusal("That request has ended; ask again.", "fenced")
            _end(conn, stale_of["id"], "superseded", _decision("superseded", ENDED))
            return Outcome("superseded", ENDED, stale_of["id"])
        pred = None
        if pred_id is not None:
            pred = dict(conn.execute(_for_update(conn, select(m.approval).where(m.approval.c.id == pred_id)))
                        .mappings().one())
            if replaces is not None and pred["status"] == "pending":  # expired or invalidated untouched: it ends now
                status = _status_now(conn, pred)
                if status != "pending":
                    _end(conn, pred["id"], status, _decision(status, GONE[status]))
                    pred["status"] = status
            if pred["status"] != "pending":
                if stale_of is not None:  # claimed or ended meanwhile: its own outcome, after this transaction
                    return Outcome("ended", "", pred["id"])
                text = _ended_text(conn, pred)
                return Outcome("replaced_card_ended", text, pred["id"],
                               data={"predecessor": {"approval_id": pred["id"], "status": pred["status"]}})
        if stale_of is not None:  # a fresh card keeps the date source the owner was shown, if the books still agree
            date_source = _recheck_date_source(ctx, inp, json.loads(stale_of["card_json"]).get("date_source"))
        elif date_source is not None:
            check_date_source(ctx, contract, inp, date_source, owner_words)
        preview = _preview(ctx, contract, inp, date_source)
        rs = read_set(conn, contract, ctx, inp)
        new = new_id()
        set_aside = None
        if stale_of is not None:
            _end(conn, pred["id"], "superseded", _decision("superseded", FRESH, fresh_approval_id=new))
        if conversation_id is not None:  # one pending card per conversation, whatever its operation
            a = m.approval
            for row in conn.execute(_for_update(conn, select(a).where(
                    a.c.conversation_id == conversation_id, a.c.status == "pending").order_by(a.c.id))).mappings().all():
                _end(conn, row["id"], "superseded", _decision("superseded", GONE["superseded"]))
                set_aside = {"approval_id": row["id"], "title": _title(row)}
        p = REGISTRY.plugins[REGISTRY.contract_plugin[contract.name]]
        card = {"id": new, "principal_id": who["id"], "company_id": ctx.company_id,
                "conversation_id": conversation_id, "contract": contract.name, "contract_version": p.version,
                "policy_version": who["grants_version"], "impl_version": impl_version(contract),
                "nonce": secrets.token_hex(16), "operation_key": new_id(), "expires_at": now() + VALID_FOR}
        d = digest(_fields(card, norm, rs, preview))
        conn.execute(m.approval.insert().values(
            **card, args_json=canonical(norm), read_set_json=canonical(rs), card_json=canonical(preview), digest=d,
            status="pending", created_at=now(), request_card_token=token))
    data = {}
    if set_aside:
        data["set_aside"] = set_aside
    if pred is not None or set_aside:
        data["replaces"] = pred["id"] if pred is not None else set_aside["approval_id"]
    return Outcome("card", preview["title"], card["id"], preview, card["operation_key"], digest=d, data=data)


def card_events(o: Outcome) -> list[dict]:
    """A proposal's events: the notice naming a card it set aside, then its card with
    the card it replaced; a proposal that made no card, what happened instead."""
    if o.card is None:
        return [{"type": "refused", "code": o.status, "text": o.text}]
    ev = []
    if o.data.get("set_aside"):
        ev.append({"type": "notice", "code": "card_set_aside",
                   "text": f"Your pending card ({o.data['set_aside']['title']}) was set aside; nothing was saved "
                           "from it."})
    card = {"type": "card", "approval_id": o.approval_id, "digest": o.digest, "card": o.card}
    if o.data.get("replaces"):
        card["replaces"] = o.data["replaces"]
    return [*ev, card]


def _card(conn: Connection, approval_id: str) -> dict:
    row = conn.execute(select(m.approval).where(m.approval.c.id == approval_id)).mappings().first()
    if row is None:
        raise Refusal("That approval card does not exist.", "no_card")
    return dict(row)


def _status_now(conn: Connection, a: dict) -> str:
    """A card's live status, writing nothing: a pending card is expired past its time, invalidated when its drafter's
    grants changed since (lazy invalidation)."""
    if a["status"] != "pending":
        return a["status"]
    drafter = pr.by_id(conn, a["principal_id"])
    return ("expired" if now() >= _aware(a["expires_at"]) else
            "invalidated" if drafter["grants_version"] != a["policy_version"] or drafter["disabled"] else "pending")


def _touch(engine: Engine, a: dict) -> str:
    """A pending card's live status, recorded with its decision the first time it is touched."""
    if a["status"] != "pending":
        return a["status"]
    with read_only(engine) as conn:
        status = _status_now(conn, a)
    if status != "pending":
        with engine.begin() as conn:
            _end(conn, a["id"], status, _decision(status, GONE[status]))
    return status


def decline(engine: Engine, approval_id: str, principal: dict | None = None) -> Outcome:
    """Any principal with `draft` or `approve` on the card's company; writes no business row."""
    with read_only(engine) as conn:
        a = _card(conn, approval_id)
        who = pr.by_id(conn, (principal or _owner(conn))["id"])
        if who["disabled"] or not (pr.holds(conn, who["id"], a["company_id"], "draft")
                                   or pr.holds(conn, who["id"], a["company_id"], "approve")):
            raise Refusal("You may not decline cards in this company.", "forbidden")
    status = _touch(engine, a)
    if status != "pending":
        return Outcome(status, GONE.get(status, f"That card is {status}."), approval_id)
    with engine.begin() as conn:
        done = _end(conn, approval_id, "declined", _decision("declined", GONE["declined"]))
    if not done:
        return Outcome("unknown", "That card is no longer waiting for a decision.", approval_id)
    return Outcome("declined", "Not saved. Nothing changed in your books.", approval_id)


reject = decline  # the pilot's name


class _Changed(Exception):
    pass


class _NotClaimed(Exception):
    pass


class _Invalid(Exception):
    pass


def _lock_read_set(conn: Connection, ctx, rs: list) -> None:
    """Lock in the one order: the chain head, the fiscal years and periods of the read
    set (periods by start date), then its document rows the runtime role may change, by table then id. Rows of
    tables it may not change (company, party, item) are compared by digest only."""
    ledger.lock_chain(ctx)
    periods = [k for t, k, _ in rs if t == "fiscal_period"]
    if periods:
        fp, fy = m.fiscal_period, m.fiscal_year
        years = sorted(set(conn.execute(select(fp.c.fiscal_year_id).where(fp.c.id.in_(periods))).scalars()))
        for y in years:
            conn.execute(ledger.for_update(ctx, select(fy.c.id).where(fy.c.id == y))).all()
        conn.execute(ledger.for_update(ctx, select(fp.c.id).where(fp.c.id.in_(periods)).order_by(fp.c.start_date))).all()
    for t, k, _ in rs:
        if t in ("fiscal_period", "fiscal_year", "posting_chain_head"):
            continue
        table = metadata.tables[t]
        if table.info["runtime"].update:
            [pk] = list(table.primary_key.columns)
            conn.execute(ledger.for_update(ctx, select(pk).where(pk == k))).all()  # read-set lock


def conversation_of(engine: Engine, approval_id: str) -> str | None:
    """The conversation a card was proposed in (its outcome is noted there, whatever conversation a caller names)."""
    with read_only(engine) as conn:
        row = conn.execute(select(m.approval.c.conversation_id).where(m.approval.c.id == approval_id)).first()
    return row[0] if row else None


def refusal_data(r: Refusal) -> dict:
    """What an outcome keeps of a refusal: its code, its options and its data (a question's field, question id and
    choices), so a question reaches the owner as the engine's question on every path; and the catalogue id of
    what it refused, when that is not offered."""
    out = {"code": r.code, "options": list(r.options), "data": dict(r.data)}
    if r.capability:
        out["capability"] = r.capability
    return out


def _ended(approval_id: str, r: Refusal) -> Outcome:
    """A card that can no longer be approved as it stands: superseded, nothing saved, the refusal's code, options
    and data kept, so a question reaches the owner as the engine's question."""
    return Outcome("superseded", f"Something changed since that card was made, so nothing was saved: {r.message}",
                   approval_id, data=refusal_data(r))


def _end_before_claim(engine: Engine, a, r: Refusal, seen: dict) -> Outcome:
    """End a pending card refused before its claim (its operation switched off as planned): a conditional update;
    if it loses, the card was claimed meanwhile, and its current outcome is returned."""
    ended = _ended(a["id"], r)
    with engine.begin() as conn:
        done = _end(conn, a["id"], "superseded", _decision("superseded", ended.text, r))
    if done:
        return ended
    with read_only(engine) as conn:
        now_card = _card(conn, a["id"])
    if now_card["status"] == "consumed":
        return read_back(engine, a["operation_key"], seen)
    return Outcome(now_card["status"], GONE.get(now_card["status"], "That card can no longer be approved."), a["id"])


def committing(ctx: Ctx) -> str | None:
    """Name the current live Core approval; the returned ID is ordinary data, not authority."""
    return approval_scope.committing(ctx)


def approve(engine: Engine, approval_id: str, digest_sent: str | None, principal: dict | None = None,
            actor: str = "owner") -> Outcome:
    """Approve and commit in one transaction. `principal` is the authenticated approver as seen at
    authentication (its grants_version is rechecked under lock); the owner when called in-process."""
    with read_only(engine) as conn:
        a = _card(conn, approval_id)
        seen = principal or _owner(conn)
        pr.require(conn, pr.by_id(conn, seen["id"]), a["company_id"], "view")  # before any result is shown
    if not digest_sent or not hmac.compare_digest(str(digest_sent), a["digest"]):
        raise Refusal("That approval does not match the card (its digest differs); nothing was saved.", "digest")
    if a["status"] == "consumed":  # the same card again: the stored outcome, nothing written
        return read_back(engine, a["operation_key"], seen)
    if a["status"] != "pending":  # ended without a commit: its stored decision, nothing written
        return _decided(engine, a, seen)
    status = _touch(engine, a)
    if status != "pending":
        return Outcome(status, GONE.get(status, f"That card is {status} and cannot be approved."), approval_id)
    contract = REGISTRY.contracts[a["contract"]]
    try:
        _write_contract(contract)  # a card made before its operation was switched off does not commit
    except Refusal as r:
        if r.code != "planned":
            raise
        return _end_before_claim(engine, a, r, seen)  # it ends, rather than refusing until it expires
    args = json.loads(a["args_json"])
    inp = contract.input_model.model_validate(args)
    key = a["operation_key"]
    ended: Refusal | None = None
    try:
        with engine.begin() as conn:
            # 1. the principals first, by id, FOR SHARE: a grant change either committed before (refused here)
            # or waits until this transaction ends
            ids = sorted({a["principal_id"], seen["id"]})
            rows = {i: pr.by_id(conn, i, lock=True) for i in ids}
            drafter, approver = rows[a["principal_id"]], rows[seen["id"]]
            if approver["grants_version"] != seen["grants_version"] or approver["disabled"]:
                raise Refusal("Your permissions changed since you signed in; nothing was saved.", "forbidden")
            if not pr.holds(conn, approver["id"], a["company_id"], "approve", "post"):
                raise Refusal("You may not approve and post in this company; nothing was saved.", "forbidden")
            if drafter["grants_version"] != a["policy_version"] or drafter["disabled"] \
                    or not pr.holds(conn, drafter["id"], a["company_id"], "draft"):
                raise _Invalid()
            # 1a. the conversation's fence, FOR SHARE, before the card: a card whose request has
            # ended is superseded, never claimed
            if a["request_card_token"] and not _fenced_live(conn, a):
                if not _end(conn, approval_id, "superseded", _decision("superseded", ENDED)):
                    raise _NotClaimed()
                return Outcome("superseded", ENDED, approval_id)
            # 2. claim the card: compare-and-set, so one card commits once
            res = conn.execute(update(m.approval).where(
                m.approval.c.id == approval_id, m.approval.c.status == "pending",
                m.approval.c.expires_at > now(), m.approval.c.digest == a["digest"]).values(
                status="consumed", approved_by=approver["id"], approver_grants_version=approver["grants_version"],
                decided_at=now()))
            if res.rowcount != 1:
                raise _NotClaimed()
            ctx = company_ctx(conn, actor=actor, company_id=a["company_id"])
            # 3 and 4. the posting locks, then the document locks; then recompute under them. A refusal here (the
            # plugin no longer enabled, the request no longer allowed or now ambiguous) ends the card inside this
            # transaction, the claim still held, so no other approval can claim it in between: no outcome, no
            # effect, no fresh card.
            try:
                ensure_enabled(conn, REGISTRY.contract_plugin[contract.name], contract.name)
                stored_rs = json.loads(a["read_set_json"])
                _lock_read_set(conn, ctx, stored_rs)
                preview = _preview(ctx, contract, inp,
                                   _recheck_date_source(ctx, inp, json.loads(a["card_json"]).get("date_source")))
                rs = read_set(conn, contract, ctx, inp)
            except Refusal as r:
                ended = r
                _end(conn, approval_id, "superseded", _decision("superseded", _ended(approval_id, r).text, r),
                     unclaim=True)
            if ended is not None:
                return _ended(approval_id, ended)
            p = REGISTRY.plugins[REGISTRY.contract_plugin[contract.name]]
            running = {**a, "contract_version": p.version, "impl_version": impl_version(contract)}
            if digest(_fields(running, args, rs, preview)) != a["digest"]:  # also a version change since the card
                raise _Changed()
            # 5. the effect under a savepoint: the commit, then the checks its write services registered, in
            # registration order; the outcome in the same transaction. Only this context may run a write service.
            effect = conn.begin_nested()
            ctx.extra["commit"] = approval_id
            with approval_scope._issue(ctx, approval_id, contract.name, key,
                                       REGISTRY.contract_plugin.get(contract.name), effect):
                try:
                    result: Result = contract.commit(ctx, inp, key)
                    check_amount_names(contract, result.amounts)
                    for check_key, check in list(ctx.checks.items()):
                        with approval_scope._check(ctx, check_key, check):
                            check(ctx)
                    audit(ctx, f"commit:{contract.name}", approval_id=approval_id,
                          detail={"operation_key": key, **result.data})
                    effect.commit()
                    stored = {"text": result.text, "data": result.data, "figures": result.figures,
                              "amounts": amount_dicts(result.amounts)}
                    outcome = "committed"
                except Refusal as r:
                    effect.rollback()
                    stored = {"text": f"Nothing was saved: {r.message}", **refusal_data(r)}
                    outcome = "refused"
                finally:
                    ctx.extra.pop("commit", None)
            conn.execute(m.operation_outcome.insert().values(
                key=key, company_id=a["company_id"], approval_id=approval_id, card_digest=a["digest"],
                status=outcome, result_json=canonical(stored), committed_at=now()))
    except _Changed:
        # the fresh card replaces this one in one transaction, with this card's token and through its fence
        with read_only(engine) as conn:
            drafter = pr.by_id(conn, a["principal_id"])
        try:
            fresh = _propose(engine, contract, args, a["conversation_id"], drafter, a["company_id"],
                             a["request_card_token"], None, stale_of=a)
        except Refusal as r:  # the fresh preview itself refused: the card ends with that refusal
            ended = _ended(approval_id, r)
            with engine.begin() as conn:
                if _end(conn, approval_id, "superseded", _decision("superseded", ended.text, r)):
                    return ended
            return _now_outcome(engine, approval_id, seen)
        if fresh.status == "ended":  # claimed or ended meanwhile
            return _now_outcome(engine, approval_id, seen)
        if fresh.card is not None:
            fresh.status, fresh.text = "superseded", FRESH
        return fresh
    except _Invalid:
        with engine.begin() as conn:
            _end(conn, approval_id, "invalidated", _decision("invalidated", GONE["invalidated"]))
        return Outcome("invalidated", GONE["invalidated"], approval_id)
    except _NotClaimed:  # another attempt claimed it first, or it expired meanwhile
        return _now_outcome(engine, approval_id, seen)
    if outcome == "refused":  # the refusal as stored, its catalogue id included when it has one
        return Outcome("refused", stored["text"], approval_id, operation_key=key,
                       data={k: stored[k] for k in ("code", "options", "data", "capability") if k in stored})
    return Outcome("committed", result.text, approval_id, operation_key=key, figures=result.figures, data=result.data,
                   amounts=stored["amounts"])


def _now_outcome(engine: Engine, approval_id: str, seen: dict) -> Outcome:
    """A card's outcome as it stands now: committed or refused from its outcome row, else its stored decision."""
    with read_only(engine) as conn:
        a = _card(conn, approval_id)
    if a["status"] == "consumed":
        return read_back(engine, a["operation_key"], seen)
    if a["status"] != "pending":
        return _decided(engine, a, seen)
    status = _touch(engine, a)
    return Outcome(status, GONE.get(status, "That card can no longer be approved."), approval_id)


def _decided(engine: Engine, a: dict, seen: dict, depth: int = 0) -> Outcome:
    """A repeated approval of a card that ended without a commit, writing nothing: its stored decision
    as the same events; a fresh card while it is pending and its token live, else that card's own outcome. A card
    ended before decisions were stored (older books) gives the plain message of its status."""
    if not a["decision_json"]:
        return Outcome(a["status"], GONE.get(a["status"], f"That card is {a['status']} and cannot be approved."),
                       a["id"])
    d = json.loads(a["decision_json"])
    fresh_id = d.get("fresh_approval_id")
    if fresh_id and depth < 20:
        with read_only(engine) as conn:
            f = _card(conn, fresh_id)
            status, live = _status_now(conn, f), _fenced_live(conn, f, lock=False)
        if status == "pending" and live:
            return Outcome("superseded", d["text"], f["id"], json.loads(f["card_json"]), f["operation_key"],
                           digest=f["digest"], data={"replaces": a["id"]})
        if status == "pending":  # its request has ended: approving it would end it so
            return Outcome("superseded", ENDED, f["id"])
        if status == "consumed":
            return read_back(engine, f["operation_key"], seen)
        if f["status"] != "pending":
            return _decided(engine, f, seen, depth + 1)
        return Outcome(status, GONE.get(status, "That card can no longer be approved."), f["id"])
    return Outcome(d["status"], d["text"], a["id"],
                   data={k: d[k] for k in ("code", "options", "data", "capability") if k in d})


def read_back(engine: Engine, key: str, principal: dict | None = None) -> Outcome:
    """What the books show for an operation key, from its outcome row; never retries anything.
    The caller must hold `view` on the card's company (the owner in-process)."""
    with read_only(engine) as conn:
        o = conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == key)).mappings().first()
        card = conn.execute(select(m.approval).where(m.approval.c.operation_key == key)).mappings().first()
        if card is not None:
            pr.require(conn, pr.by_id(conn, (principal or _owner(conn))["id"]), card["company_id"], "view")
    if o is None:
        if card is None:
            return Outcome("unknown", "No card has that operation key, so nothing was saved.", operation_key=key)
        return Outcome("unknown", "That operation did not commit, so nothing was saved"
                       + (" (its card is still waiting for approval)." if card["status"] == "pending" else "."),
                       card["id"], operation_key=key)
    r = json.loads(o["result_json"])
    # outcomes carried over by revision 0005 from older books may hold a code and no text
    if o["status"] == "refused":
        return Outcome("refused", r.get("text") or "That write did not happen; nothing was saved.", o["approval_id"],
                       operation_key=key, data={"code": r.get("code"), "options": r.get("options") or [],
                                                "data": r.get("data") or {},
                                                **({"capability": r["capability"]} if r.get("capability") else {})})
    # an outcome stored before amounts existed has none: never an invented amount
    return Outcome("committed", r.get("text") or "That write was saved.", o["approval_id"], operation_key=key,
                   figures=r.get("figures"), data=r.get("data") or {}, amounts=r.get("amounts") or [])


def books_currency(engine: Engine, approval_id: str | None = None, key: str | None = None) -> str | None:
    """The functional currency of the company a card (by its id or its operation key) belongs to, or of the books'
    one company; None when there is none (no books yet, or an unknown card)."""
    with read_only(engine) as conn:
        if approval_id is None and key is None:
            try:
                return company_ctx(conn).currency
            except Refusal:
                return None
        a = m.approval
        where = a.c.id == approval_id if approval_id is not None else a.c.operation_key == key
        return conn.execute(select(m.company.c.functional_currency).join(a, a.c.company_id == m.company.c.id)
                            .where(where)).scalar()


def with_currency(events: list[dict], currency: str | None) -> list[dict]:
    """Each event names the books' functional currency (a card's is on the card itself); every figure in it carries
    its own currency where it has an amount."""
    return [e if e.get("type") not in ("result", "ask", "refused", "answer") else {"currency": currency, **e}
            for e in events]


def pending_card(engine: Engine, conversation_id: str) -> dict | None:
    """The conversation's pending card, only if it has no token or its token is live, as on every route that
    returns a card."""
    with read_only(engine) as conn:
        row = conn.execute(select(m.approval).where(
            m.approval.c.conversation_id == conversation_id, m.approval.c.status == "pending"
        ).order_by(m.approval.c.created_at.desc())).mappings().first()
        if row is None or not _fenced_live(conn, dict(row), lock=False):
            return None
    return dict(row)

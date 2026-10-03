"""Ledger-write classifier: which ledger tables a SQL statement writes."""

import functools
import uuid
from dataclasses import dataclass

from erpclaw_lib import authority_gate
from erpclaw_lib import authority_readiness

LEDGER_SINKS = {
    "gl_entry": "gl",
    "gl_chain_head": "gl",
    "stock_ledger_entry": "stock",
    "stock_fifo_layer": "stock",
    "payment_ledger_entry": "payment",
}

ENFORCED_FAMILIES = frozenset({"gl"})

LEDGER_WRITE_UNAUTHORIZED = "LEDGER_WRITE_UNAUTHORIZED"

SUGGESTIONS = {
    LEDGER_WRITE_UNAUTHORIZED: (
        "This change writes to the ledger. Once authority is active, "
        "a ledger write needs a single-use authorization issued for this "
        "exact call and consumed in the same transaction; pass its id as "
        "--authorization-id. Nothing was written."
    )
}


class LedgerWriteRefused(authority_gate.AuthorityRefusal):
    pass


_ALL_SINKS = frozenset(LEDGER_SINKS)

_READ_FIRST = frozenset({"select", "values", "with"})
_TXN_FIRST = frozenset(
    {"begin", "commit", "end", "rollback", "savepoint", "release"}
)
_DML_FIRST = frozenset({"insert", "replace", "update", "delete", "merge", "with"})


def _is_ident_char(ch):
    return ch.isalnum() or ch == "_" or ch == "$"


def _dollar_open(text, i):
    n = len(text)
    if text[i] != "$":
        return None
    j = i + 1
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    if j >= n or text[j] != "$":
        return None
    tag = text[i + 1:j]
    if tag == "":
        return text[i:j + 1]
    if not (tag[0].isalpha() or tag[0] == "_"):
        return None
    return text[i:j + 1]


def _skip_normal_string(text, i):
    n = len(text)
    assert text[i] == "'"
    j = i + 1
    buf = []
    while j < n:
        ch = text[j]
        if ch == "'":
            if j + 1 < n and text[j + 1] == "'":
                buf.append("'")
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(ch)
        j += 1
    return "".join(buf), n


def _skip_escape_string(text, i):
    n = len(text)
    assert text[i + 1] == "'"
    j = i + 2
    buf = []
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n:
            buf.append(text[j + 1])
            j += 2
            continue
        if ch == "'":
            if j + 1 < n and text[j + 1] == "'":
                buf.append("'")
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(ch)
        j += 1
    return "".join(buf), n


def _skip_quoted_ident(text, i):
    n = len(text)
    assert text[i] == '"'
    j = i + 1
    buf = []
    while j < n:
        ch = text[j]
        if ch == '"':
            if j + 1 < n and text[j + 1] == '"':
                buf.append('"')
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(ch)
        j += 1
    return "".join(buf), n


def _split_statements(sql):
    parts = []
    start = 0
    depth = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "$":
            tag = _dollar_open(sql, i)
            if tag is not None:
                close = sql.find(tag, i + len(tag))
                i = n if close == -1 else close + len(tag)
                continue
        if ch in "eE" and i + 1 < n and sql[i + 1] == "'":
            prev = sql[i - 1] if i > 0 else " "
            if not (prev.isalnum() or prev in "_$"):
                _, i = _skip_escape_string(sql, i)
                continue
        if ch == "'":
            _, i = _skip_normal_string(sql, i)
            continue
        if ch in "uU" and i + 1 < n and sql[i + 1] == "&":
            if i + 2 < n and sql[i + 2] == '"':
                prev = sql[i - 1] if i > 0 else " "
                if not (prev.isalnum() or prev in "_$"):
                    _, i = _skip_quoted_ident(sql, i + 2)
                    continue
        if ch == '"':
            _, i = _skip_quoted_ident(sql, i)
            continue
        if ch == "`":
            j = sql.find("`", i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == "[":
            j = sql.find("]", i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if ch == ";" and depth == 0:
            parts.append(sql[start:i])
            start = i + 1
            i += 1
            continue
        i += 1
    parts.append(sql[start:])
    return parts


def _tokenize(stmt):
    tokens = []
    strings = []
    i = 0
    n = len(stmt)
    while i < n:
        ch = stmt[i]
        if ch in " \t\n\r\f\v":
            i += 1
            continue
        if ch == "-" and i + 1 < n and stmt[i + 1] == "-":
            j = stmt.find("\n", i + 2)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and i + 1 < n and stmt[i + 1] == "*":
            j = stmt.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "$":
            tag = _dollar_open(stmt, i)
            if tag is not None:
                close = stmt.find(tag, i + len(tag))
                if close == -1:
                    strings.append(stmt[i + len(tag):])
                    i = n
                else:
                    strings.append(stmt[i + len(tag):close])
                    i = close + len(tag)
                continue
            tokens.append(("sym", ch))
            i += 1
            continue
        if ch in "eE" and i + 1 < n and stmt[i + 1] == "'":
            prev = stmt[i - 1] if i > 0 else " "
            if not (prev.isalnum() or prev in "_$"):
                body, i = _skip_escape_string(stmt, i)
                strings.append(body)
                continue
        if ch == "'":
            body, i = _skip_normal_string(stmt, i)
            strings.append(body)
            continue
        if ch in "uU" and i + 1 < n and stmt[i + 1] == "&":
            if i + 2 < n and stmt[i + 2] == '"':
                prev = stmt[i - 1] if i > 0 else " "
                if not (prev.isalnum() or prev in "_$"):
                    _, i = _skip_quoted_ident(stmt, i + 2)
                    tokens.append(("uescape", ""))
                    continue
        if ch == '"':
            body, i = _skip_quoted_ident(stmt, i)
            tokens.append(("word", body))
            continue
        if ch == "`":
            j = stmt.find("`", i + 1)
            if j == -1:
                tokens.append(("word", stmt[i + 1:]))
                i = n
            else:
                tokens.append(("word", stmt[i + 1:j]))
                i = j + 1
            continue
        if ch == "[":
            j = stmt.find("]", i + 1)
            if j == -1:
                tokens.append(("word", stmt[i + 1:]))
                i = n
            else:
                tokens.append(("word", stmt[i + 1:j]))
                i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and _is_ident_char(stmt[j]):
                j += 1
            tokens.append(("word", stmt[i:j]))
            i = j
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and (_is_ident_char(stmt[j]) or stmt[j] == "."):
                j += 1
            tokens.append(("num", stmt[i:j]))
            i = j
            continue
        if ch == "?":
            tokens.append(("qmark", ch))
            i += 1
            continue
        if ch == ".":
            tokens.append(("dot", ch))
            i += 1
            continue
        if ch == "(" or ch == ")":
            tokens.append(("paren", ch))
            i += 1
            continue
        i += 1
    return tokens, strings


def _word_at(tokens, i):
    if 0 <= i < len(tokens) and tokens[i][0] == "word":
        return tokens[i][1].lower()
    return None


def _has_word(hay, needle):
    start = 0
    span = len(needle)
    while True:
        pos = hay.find(needle, start)
        if pos == -1:
            return False
        before = hay[pos - 1] if pos > 0 else ""
        after = hay[pos + span] if pos + span < len(hay) else ""
        if (before.isalnum() or before in "_$") or (
            after.isalnum() or after in "_$"
        ):
            start = pos + 1
            continue
        return True


def _named_anywhere(tokens, strings, has_uescape):
    if has_uescape:
        return set(LEDGER_SINKS)
    found = set()
    for kind, value in tokens:
        if kind == "word" and value.lower() in LEDGER_SINKS:
            found.add(value.lower())
        elif kind == "uescape":
            return set(LEDGER_SINKS)
    for body in strings:
        low = body.lower()
        for sink in LEDGER_SINKS:
            if sink not in low:
                continue
            if _has_word(low, sink):
                found.add(sink)
    return found


def _read_target(tokens, i):
    n = len(tokens)
    if i >= n:
        return None, "unknown"
    kind, value = tokens[i]
    if kind == "uescape":
        return None, "uescape"
    if kind != "word":
        return None, "unknown"
    if value.lower() == "u" and i + 1 < n and tokens[i + 1] == ("sym", "&"):
        return None, "uescape"
    k = i
    while (
        k + 2 < n
        and tokens[k + 1][0] == "dot"
        and tokens[k + 2][0] == "word"
    ):
        k += 2
    return tokens[k][1].lower(), "known"


def _prev_words(tokens, i, count):
    out = []
    j = i - 1
    while j >= 0 and len(out) < count:
        if tokens[j][0] == "word":
            out.append(tokens[j][1].lower())
        j -= 1
    return out


def _classify_one(raw):
    if raw.strip() == "":
        return frozenset()
    low_raw = raw.lower()
    has_uescape = "u&\"" in low_raw
    tokens, strings = _tokenize(raw)
    first = None
    for kind, value in tokens:
        if kind == "paren" and value == "(":
            continue
        if kind == "word":
            first = value.lower()
            break
        if kind in ("qmark", "num", "dot", "sym", "uescape"):
            if kind == "uescape":
                first = None
                break
            continue
    if first is None or first in _TXN_FIRST or first in ("select", "values"):
        return frozenset()
    if first not in _DML_FIRST:
        # Every other statement kind (DDL, COPY, DO, EXPLAIN, CALL, unknown) is protected when
        # it names a sink anywhere, string and dollar bodies included; a DML verb inside it
        # (a trigger or rule body) never exempts it.
        return frozenset(_named_anywhere(tokens, strings, has_uescape))
    targets = []
    unknown = False
    uescape_write = False
    n = len(tokens)
    i = 0
    while i < n:
        if tokens[i][0] != "word":
            i += 1
            continue
        w = tokens[i][1].lower()
        if w == "insert":
            prev = _prev_words(tokens, i, 1)
            if prev and prev[0] == "then":
                i += 1
                continue
            j = i + 1
            nxt = _word_at(tokens, j)
            if nxt == "or":
                if _word_at(tokens, j + 1) is None:
                    i += 1
                    continue
                j += 2
            if _word_at(tokens, j) != "into":
                i += 1
                continue
            k = j + 1
            if _word_at(tokens, k) == "only":
                k += 1
            name, kind = _read_target(tokens, k)
            if kind == "known":
                targets.append(name)
            elif kind == "uescape":
                uescape_write = True
            else:
                unknown = True
            i = k + 1
            continue
        if w == "replace":
            nxt_kind = tokens[i + 1][0] if i + 1 < n else None
            nxt_val = tokens[i + 1][1] if i + 1 < n else None
            if nxt_kind == "paren" and nxt_val == "(":
                i += 1
                continue
            if _word_at(tokens, i + 1) != "into":
                i += 1
                continue
            k = i + 2
            if _word_at(tokens, k) == "only":
                k += 1
            name, kind = _read_target(tokens, k)
            if kind == "known":
                targets.append(name)
            elif kind == "uescape":
                uescape_write = True
            else:
                unknown = True
            i = k + 1
            continue
        if w == "update":
            prev = _prev_words(tokens, i, 3)
            if prev and prev[0] in ("do", "then", "for"):
                i += 1
                continue
            if len(prev) >= 3 and prev[0] == "key" and prev[1] == "no" and prev[2] == "for":
                i += 1
                continue
            j = i + 1
            nxt = _word_at(tokens, j)
            if nxt == "or":
                if _word_at(tokens, j + 1) is None:
                    i += 1
                    continue
                j += 2
            if _word_at(tokens, j) == "only":
                j += 1
            name, kind = _read_target(tokens, j)
            if kind == "known":
                targets.append(name)
            elif kind == "uescape":
                uescape_write = True
            else:
                unknown = True
            i = j + 1
            continue
        if w == "delete":
            prev = _prev_words(tokens, i, 1)
            if prev and prev[0] == "then":
                i += 1
                continue
            if _word_at(tokens, i + 1) != "from":
                i += 1
                continue
            k = i + 2
            if _word_at(tokens, k) == "only":
                k += 1
            name, kind = _read_target(tokens, k)
            if kind == "known":
                targets.append(name)
            elif kind == "uescape":
                uescape_write = True
            else:
                unknown = True
            i = k + 1
            continue
        if w == "merge":
            if _word_at(tokens, i + 1) != "into":
                i += 1
                continue
            k = i + 2
            if _word_at(tokens, k) == "only":
                k += 1
            name, kind = _read_target(tokens, k)
            if kind == "known":
                targets.append(name)
            elif kind == "uescape":
                uescape_write = True
            else:
                unknown = True
            i = k + 1
            continue
        i += 1
    wrote = bool(targets) or unknown or uescape_write
    if wrote:
        hit = {t for t in targets if t in LEDGER_SINKS}
        if uescape_write:
            return _ALL_SINKS
        if hit:
            return frozenset(hit)
        if unknown:
            return frozenset(_named_anywhere(tokens, strings, has_uescape))
        return frozenset()
    return frozenset()


@functools.lru_cache(maxsize=4096)
def classify(sql):
    """Return the set of ledger tables the SQL text writes."""
    if not isinstance(sql, str):
        sql = str(sql)
    low = sql.lower()
    for sink in LEDGER_SINKS:
        if sink in low:
            break
    else:
        if "u&\"" not in low:
            return frozenset()
    out = set()
    for part in _split_statements(sql):
        out |= set(_classify_one(part))
    return frozenset(out)


@dataclass(frozen=True)
class AuthorityTxn:
    authorization_id: str
    action: str
    txn_id: str
    install_id: str
    label: str


_TXN_ATTR = "_erpclaw_authority_txn"
_TOKEN_ATTR = "_erpclaw_authority_token"
_REFUSED_ATTR = "_erpclaw_authority_refused"

_ABSENT = object()


def _lookup(conn, name):
    try:
        return object.__getattribute__(conn, name)
    except AttributeError:
        return _ABSENT


def current(conn):
    value = _lookup(conn, _TXN_ATTR)
    if value is _ABSENT:
        return None
    return value


def _poison(conn):
    value = _lookup(conn, _REFUSED_ATTR)
    if value is _ABSENT:
        return None
    return value


def bind(conn, txn):
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    from erpclaw_lib.authorization_consumption import (
        _checked_wrapper as _check_wrapper,
    )
    if not isinstance(txn, AuthorityTxn):
        raise ValueError(INPUT_INVALID)
    _check_wrapper(conn)
    existing = _lookup(conn, _TXN_ATTR)
    if existing is not _ABSENT:
        raise authority_gate.AuthorityRefusal(
            authority_gate.AUTHORIZATION_REFUSED)
    token = uuid.uuid4().hex
    object.__setattr__(conn, _TXN_ATTR, txn)
    object.__setattr__(conn, _TOKEN_ATTR, token)
    return token


def unbind(conn, token):
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    bound_txn = _lookup(conn, _TXN_ATTR)
    bound_token = _lookup(conn, _TOKEN_ATTR)
    if (bound_txn is _ABSENT or bound_token is _ABSENT
            or bound_token != token):
        raise ValueError(INPUT_INVALID)
    try:
        object.__delattr__(conn, _TXN_ATTR)
    except AttributeError:
        pass


def clear(conn, token):
    last = _lookup(conn, _TOKEN_ATTR)
    if last is _ABSENT or last != token:
        return False
    for name in (_TXN_ATTR, _REFUSED_ATTR):
        try:
            object.__delattr__(conn, name)
        except AttributeError:
            pass
    return True


def _is_exempt(sql_text):
    upper = sql_text.strip().upper()
    if upper.startswith("ROLLBACK"):
        return True
    normalized = " ".join(upper.split())
    if authority_gate._PROBE_RE.fullmatch(normalized) is not None:
        return True
    return False


def guard_statement(wrapper, sql):
    code = _poison(wrapper)
    if code is None:
        return
    text = sql if isinstance(sql, str) else str(sql)
    if _is_exempt(text):
        return
    raise LedgerWriteRefused(code)


def commit_wrapper(wrapper):
    code = _poison(wrapper)
    raw = object.__getattribute__(wrapper, "_conn")
    if code is not None:
        try:
            raw.rollback()
        except Exception:
            pass
        raise LedgerWriteRefused(code)
    return raw.commit()


def rollback_wrapper(wrapper):
    raw = object.__getattribute__(wrapper, "_conn")
    return raw.rollback()


def close_wrapper(wrapper):
    for name in (_TXN_ATTR, _TOKEN_ATTR, _REFUSED_ATTR):
        try:
            object.__delattr__(wrapper, name)
        except AttributeError:
            pass
    raw = object.__getattribute__(wrapper, "_conn")
    return raw.close()


def exit_wrapper(wrapper, args):
    code = _poison(wrapper)
    raw = object.__getattribute__(wrapper, "_conn")
    if code is None:
        return raw.__exit__(*args)
    try:
        raw.rollback()
    except Exception:
        pass
    exc_type = args[0] if len(args) > 0 else None
    if exc_type is None:
        raise LedgerWriteRefused(code)
    return False


def _refuse(wrapper, code, tables):
    object.__setattr__(wrapper, _REFUSED_ATTR, code)
    ctx = current(wrapper)
    action = ctx.action if ctx is not None else "-"
    authority_gate._LOG.warning(
        "ledger write refused: code=%s table=%s action=%s"
        % (code, ",".join(sorted(tables)), action))
    raise LedgerWriteRefused(code)


def _verify_context(wrapper, ctx, install_id, targets):
    from erpclaw_lib.db import db_error_types
    from erpclaw_lib.query import Field, P, Q, Table
    missing, _base = db_error_types()
    auth_table = Table("operation_authorization")
    auth_sql = Q.from_(auth_table).select(
        Field("consumed_txn"), Field("action"),
        Field("install_id")).where(
        Field("id") == P()).get_sql()
    result_table = Table("operation_authorization_result")
    result_sql = Q.from_(result_table).select(
        Field("authorization_id")).where(
        Field("authorization_id") == P()).get_sql()
    try:
        try:
            auth_row = wrapper.execute(
                auth_sql, (ctx.authorization_id,)).fetchone()
        except Exception as exc:
            if isinstance(exc, missing):
                _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        if auth_row is None:
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        try:
            row_consumed = auth_row["consumed_txn"]
            row_action = auth_row["action"]
            row_install = auth_row["install_id"]
        except Exception:
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        if (row_consumed != ctx.txn_id
                or row_action != ctx.action
                or row_install != install_id):
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        try:
            result_row = wrapper.execute(
                result_sql, (ctx.authorization_id,)).fetchone()
        except Exception as exc:
            if isinstance(exc, missing):
                _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        if result_row is not None:
            _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
        return
    except LedgerWriteRefused:
        raise
    except Exception:
        _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)


def check_statement(wrapper, sql):
    text = sql if isinstance(sql, str) else str(sql)
    targets = {t for t in classify(text) if LEDGER_SINKS[t] in ENFORCED_FAMILIES}
    if not targets:
        return
    from erpclaw_lib.db import PgConnectionWrapper
    if type(wrapper) is PgConnectionWrapper:
        raw = object.__getattribute__(wrapper, "_conn")
        import psycopg2.extensions as _extensions
        if (raw.get_transaction_status()
                == _extensions.TRANSACTION_STATUS_INERROR):
            return
    try:
        phase, install_id = authority_gate.install_phase(wrapper)
    except authority_gate.AuthorityRefusal:
        _refuse(wrapper, authority_gate.AUTHORITY_NOT_READY, targets)
    if phase == "STAGED":
        return
    if not authority_readiness.is_ready(wrapper):
        _refuse(wrapper, authority_gate.AUTHORITY_NOT_READY, targets)
    ctx = current(wrapper)
    if ctx is None:
        _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
    if ctx.install_id != install_id:
        _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)
    _verify_context(wrapper, ctx, install_id, targets)


def check_executescript(wrapper, script):
    text = script if isinstance(script, str) else str(script)
    targets = {t for t in classify(text) if LEDGER_SINKS[t] in ENFORCED_FAMILIES}
    if not targets:
        return
    try:
        phase, install_id = authority_gate.install_phase(wrapper)
    except authority_gate.AuthorityRefusal:
        _refuse(wrapper, authority_gate.AUTHORITY_NOT_READY, targets)
    if phase == "STAGED":
        return
    if not authority_readiness.is_ready(wrapper):
        _refuse(wrapper, authority_gate.AUTHORITY_NOT_READY, targets)
    _refuse(wrapper, LEDGER_WRITE_UNAUTHORIZED, targets)

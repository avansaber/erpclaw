"""Shared setup for the single-use authorization tests."""
import os
import sys
import uuid

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402

OWNER = "owner-1"
SERVICE = "svc-1"
OTHER_SERVICE = "svc-2"
DELEGATION = "del-1"
ACTION = "add-uom"
CURRENCY = "USD"
SCALE = 2
PER_OPERATION = "75.00"
AGGREGATE_LIMIT = "100.00"
DAY_MS = 86_400_000


def _insert_row(conn, table_name, data):
    from erpclaw_lib.query import P, Q, Table
    table = Table(table_name)
    query = Q.into(table).columns(*data.keys()).insert(
        *[P() for _ in data]).get_sql()
    conn.execute(query, list(data.values()))


def _install_id(conn):
    from erpclaw_lib.query import Field, Q, Table
    table = Table("authority_install")
    query = Q.from_(table).select(Field("install_id")).get_sql()
    return conn.execute(query).fetchone()["install_id"]


def seed_company(db_path, name="Test Co", abbr="TC"):
    from erpclaw_lib.db import get_connection
    cid = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        _insert_row(conn, "company", {
            "id": cid,
            "name": "%s %s" % (name, cid[:6]),
            "abbr": "%s%s" % (abbr, cid[:4]),
            "default_currency": "USD",
            "country": "United States",
            "fiscal_year_start_month": 1,
        })
        conn.commit()
    finally:
        conn.close()
    return cid


def seed_authority(db_path, company_id):
    from erpclaw_lib import authority_clock
    from erpclaw_lib.db import get_connection
    now = authority_clock.now_ms()
    window_start = now - DAY_MS
    window_end = now + 10 * DAY_MS
    issued = now - 3_600_000
    expires = now + 10 * DAY_MS
    conn = get_connection(db_path)
    try:
        install_id = _install_id(conn)
        _insert_row(conn, "authority_principal", {
            "install_id": install_id, "id": OWNER,
            "kind": "human", "disabled_at": None})
        _insert_row(conn, "authority_principal", {
            "install_id": install_id, "id": SERVICE,
            "kind": "service", "disabled_at": None})
        _insert_row(conn, "authority_principal", {
            "install_id": install_id, "id": OTHER_SERVICE,
            "kind": "service", "disabled_at": None})
        _insert_row(conn, "authority_membership", {
            "install_id": install_id, "principal_id": SERVICE,
            "company_id": company_id, "effect": "allow"})
        _insert_row(conn, "authority_membership", {
            "install_id": install_id, "principal_id": OWNER,
            "company_id": company_id, "effect": "allow"})
        _insert_row(conn, "authority_right", {
            "install_id": install_id, "principal_id": SERVICE,
            "company_id": company_id, "resource_kind": "company",
            "resource_id": company_id, "action": ACTION,
            "effect": "allow"})
        _insert_row(conn, "authority_delegation", {
            "install_id": install_id, "id": DELEGATION,
            "issuer_id": OWNER, "grantee_id": SERVICE,
            "issued_at": issued, "expires_at": expires,
            "revoked_at": None})
        _insert_row(conn, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": DELEGATION,
            "company_id": company_id, "resource_kind": "company",
            "resource_id": company_id, "action": ACTION})
        _insert_row(conn, "authority_delegation_cap", {
            "install_id": install_id, "delegation_id": DELEGATION,
            "action": ACTION, "currency": CURRENCY, "scale": SCALE,
            "per_operation": PER_OPERATION,
            "aggregate_limit": AGGREGATE_LIMIT,
            "window_start": window_start, "window_end": window_end})
        conn.commit()
    finally:
        conn.close()
    return {
        "install_id": install_id,
        "company_id": company_id,
        "now": now,
        "window_start": window_start,
        "window_end": window_end,
        "delegation_issued_at": issued,
        "delegation_expires_at": expires,
    }


def make_active(db_path):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table("authority_install")
        query = Q.update(table).set(
            Field("phase"), P()).get_sql()
        conn.execute(query, ("ACTIVE",))
        conn.commit()
    finally:
        conn.close()


def _update_where(conn, table_name, values, filters):
    from erpclaw_lib.query import Field, P, Q, Table
    table = Table(table_name)
    query = Q.update(table)
    params = []
    for column, value in values.items():
        query = query.set(Field(column), P())
        params.append(value)
    for column, value in filters.items():
        query = query.where(Field(column) == P())
        params.append(value)
    conn.execute(query.get_sql(), params)


def set_delegation_revoked(db_path, delegation_id, revoked_at):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_delegation",
                      {"revoked_at": revoked_at},
                      {"id": delegation_id})
        conn.commit()
    finally:
        conn.close()


def set_delegation_expiry(db_path, delegation_id, expires_at):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_delegation",
                      {"expires_at": expires_at},
                      {"id": delegation_id})
        conn.commit()
    finally:
        conn.close()


def set_delegation_issuer(db_path, delegation_id, issuer_id):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_delegation",
                      {"issuer_id": issuer_id},
                      {"id": delegation_id})
        conn.commit()
    finally:
        conn.close()


def delete_delegation_rights(db_path, delegation_id):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table("authority_delegation_right")
        query = Q.from_(table).delete().where(
            Field("delegation_id") == P()).get_sql()
        conn.execute(query, (delegation_id,))
        conn.commit()
    finally:
        conn.close()


def delete_principal_rights(db_path, principal_id):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table("authority_right")
        query = Q.from_(table).delete().where(
            Field("principal_id") == P()).get_sql()
        conn.execute(query, (principal_id,))
        conn.commit()
    finally:
        conn.close()


def set_principal_disabled(db_path, principal_id, disabled_at):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_principal",
                      {"disabled_at": disabled_at},
                      {"id": principal_id})
        conn.commit()
    finally:
        conn.close()


def set_cap_per_operation(db_path, delegation_id, action, currency, value):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_delegation_cap",
                      {"per_operation": value},
                      {"delegation_id": delegation_id,
                       "action": action, "currency": currency})
        conn.commit()
    finally:
        conn.close()


def set_cap_aggregate(db_path, delegation_id, action, currency, value):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "authority_delegation_cap",
                      {"aggregate_limit": value},
                      {"delegation_id": delegation_id,
                       "action": action, "currency": currency})
        conn.commit()
    finally:
        conn.close()


def insert_usage(db_path, install_id, delegation_id, action, currency,
                 window_start, window_end, used):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _insert_row(conn, "authority_delegation_usage", {
            "install_id": install_id, "delegation_id": delegation_id,
            "action": action, "currency": currency,
            "window_start": window_start, "window_end": window_end,
            "used": used})
        conn.commit()
    finally:
        conn.close()


def rename_company(db_path, company_id, name):
    from erpclaw_lib.db import get_connection
    conn = get_connection(db_path)
    try:
        _update_where(conn, "company", {"name": name},
                      {"id": company_id})
        conn.commit()
    finally:
        conn.close()


def read_rows(db_path, table_name, columns):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table(table_name)
        query = Q.from_(table).select(
            *[Field(column) for column in columns]).get_sql()
        return [dict(record)
                for record in conn.execute(query).fetchall()]
    finally:
        conn.close()


def make_declaration(action_class="transaction", currency="USD"):
    """a test declaration, not a product declaration"""
    def _derive(conn, action, pairs):
        from erpclaw_lib.authorization_consumption import INPUT_INVALID
        from erpclaw_lib.query import Field, P, Q, Table
        values = {}
        for name, value in pairs:
            values[name] = value
        company_id = values.get("company-id")
        amount_text = values.get("amount")
        if type(company_id) is not str or not company_id:
            raise ValueError(INPUT_INVALID)
        if type(amount_text) is not str or not amount_text:
            raise ValueError(INPUT_INVALID)
        from decimal import Decimal
        try:
            number = Decimal(amount_text)
            shaped = number.quantize(Decimal(1).scaleb(-SCALE))
        except Exception:
            raise ValueError(INPUT_INVALID)
        if shaped != number:
            raise ValueError(INPUT_INVALID)
        value = format(shaped, "f")
        table = Table("company")
        query = Q.from_(table).select(
            Field("id"), Field("name"), Field("abbr")).where(
            Field("id") == P()).get_sql()
        row = conn.execute(query, (company_id,)).fetchone()
        if row is None:
            raise ValueError(INPUT_INVALID)
        import hashlib
        import json
        text = json.dumps(
            {"abbr": row["abbr"], "id": row["id"],
             "name": row["name"]},
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {
            "company_ids": [company_id],
            "targets": [{"kind": "company", "id": company_id,
                         "state_digest": digest}],
            "amounts": [{"currency": currency, "value": value,
                         "scale": SCALE}],
        }

    def _result(payload):
        return ("uom", payload["uom_id"], "created")

    return {
        "class": action_class,
        "money": ("amount",),
        "json_args": (),
        "money_json_paths": {},
        "bound_args": ("company-id",),
        "derive": _derive,
        "result": _result,
    }


def make_handler(name="Crate"):
    mod = setup_helpers.load_db_query()

    def _run(proxy):
        mod.add_uom(
            proxy,
            setup_helpers.ns(name=name, must_be_whole_number=False))

    return _run


def standard_argv(company_id, amount="60.00", name="Crate"):
    return ["--company-id", company_id, "--amount", amount,
            "--name", name]

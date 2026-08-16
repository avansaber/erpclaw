"""Part A — M114: the elimination-duplication surplus is visible and correctable.

An install that ran the pre-M95 defect holds surplus `ic_elimination` rows with
no source intercompany transaction. Measured on this fixture's books: the
consolidated trial balance reports 79,650.00 where 29,650.00 is true (ic-only:
79,150.00 vs 29,150.00 — the M114 row's exact shape). Three contracts pinned:

  1. SURFACED — the trial balance names the surplus (count + amount + warning),
     and `list-elimination-surplus` lists exactly the decidable predicate's rows
     (`entry_type='ic_elimination' AND source_ic_transaction_id IS NULL`);
  2. CORRECTED, CONSENTED, AUDITED — `remove-elimination-surplus` is report-only
     by default; with --confirm it deletes exactly those rows, one audit_log row
     per deletion carrying the full removed row, and is idempotent;
  3. NOTHING ELSE MOVES — linked ic_elimination rows and `currency_translation`
     rows (legitimately unlinked) survive untouched, and the immutable GL is
     never involved (these rows live in the consolidation layer only).
"""
import uuid

import pytest

from advacct_helpers import call_action, get_conn, ns, load_db_query

dq = load_db_query()
import consolidation as consol  # noqa: E402  (loaded via load_db_query's sys.path)

PERIOD = "2026-06-30"


@pytest.fixture
def defect_books(conn, db_path):
    """The pre-M95 shape: 3 linked eliminations (29,150) + 2 unlinked surplus
    duplicates (50,000) + 1 currency_translation row (500, unlinked by design)."""
    cid, gid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, 'Parent', 'P')", (cid,))
    conn.execute(
        "INSERT INTO advacct_consolidation_group (id, name, parent_company_id, "
        "consolidation_currency, group_status, company_id) "
        "VALUES (?, 'Group', ?, 'USD', 'active', ?)", (gid, cid, cid))
    for nm in ("Sub A", "Sub B"):
        conn.execute(
            "INSERT INTO advacct_group_entity (id, group_id, entity_company_id, "
            "entity_name, ownership_pct, consolidation_method, is_active, company_id) "
            "VALUES (?, ?, ?, ?, '100', 'full', 1, ?)",
            (str(uuid.uuid4()), gid, str(uuid.uuid4()), nm, cid))

    def ic_txn(amount):
        tid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO advacct_ic_transaction (id, from_company_id, to_company_id, "
            "transaction_type, amount, ic_status, company_id) "
            "VALUES (?,?,?,?,?, 'posted', ?)", (tid, cid, cid, "sale", amount, cid))
        return tid

    def elim(amount, etype, source):
        conn.execute(
            "INSERT INTO advacct_elimination_entry (id, group_id, period_date, "
            "debit_account, credit_account, amount, description, entry_type, "
            "source_ic_transaction_id, company_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), gid, PERIOD, "IC Revenue", "IC Expense", amount,
             "Elimination: sale from Sub A to Sub B", etype, source, cid))

    for a in ("25000.00", "4000.00", "150.00"):
        elim(a, "ic_elimination", ic_txn(a))
    for _ in range(2):
        elim("25000.00", "ic_elimination", None)      # the duplication residue
    elim("500.00", "currency_translation", None)      # legitimately unlinked
    conn.commit()
    return {"group_id": gid, "company_id": cid}


def _counts(conn):
    return dict(conn.execute(
        "SELECT entry_type, COUNT(*) FROM advacct_elimination_entry "
        "GROUP BY entry_type").fetchall())


def test_trial_balance_surfaces_the_surplus(conn, db_path, defect_books):
    r = call_action(consol.ACTIONS["consolidation-trial-balance-report"], conn,
                    ns(group_id=defect_books["group_id"], period_date=PERIOD,
                       db_path=db_path))
    assert r["total_eliminations"] == "79650.00", "the defect shape must reproduce"
    u = r["unlinked_ic_eliminations"]
    assert u["count"] == 2 and u["total_amount"] == "50000.00"
    assert "warning" in u and "remove-elimination-surplus" in u["warning"], \
        "the number an operator reads must carry the route to the fix"


def test_list_names_exactly_the_predicate_rows(conn, db_path, defect_books):
    r = call_action(consol.ACTIONS["list-elimination-surplus"], conn,
                    ns(group_id=defect_books["group_id"], period_date=None,
                       db_path=db_path))
    assert r["surplus_count"] == 2 and r["surplus_total"] == "50000.00"
    assert all(row["entry_type"] == "ic_elimination"
               and row["source_ic_transaction_id"] is None for row in r["rows"])


def test_remove_is_report_only_by_default(conn, db_path, defect_books):
    before = _counts(conn)
    r = call_action(consol.ACTIONS["remove-elimination-surplus"], conn,
                    ns(group_id=defect_books["group_id"], period_date=None,
                       confirm=False, db_path=db_path))
    assert r.get("report_only") and r["would_remove"] == 2
    assert _counts(conn) == before, "report-only must write nothing"
    audit_n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='remove-elimination-surplus'"
    ).fetchone()[0]
    assert audit_n == 0, "no audit rows for a run that removed nothing"


def test_confirm_removes_exactly_the_surplus_audited(conn, db_path, defect_books):
    gid = defect_books["group_id"]
    r = call_action(consol.ACTIONS["remove-elimination-surplus"], conn,
                    ns(group_id=gid, period_date=None, confirm=True,
                       db_path=db_path))
    assert r["removed"] == 2 and r["surplus_total_removed"] == "50000.00"

    after = _counts(conn)
    assert after == {"currency_translation": 1, "ic_elimination": 3}, \
        "linked rows and the CTA row must survive untouched"

    audits = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='remove-elimination-surplus'"
    ).fetchone()[0]
    assert audits == 2, "one audit row per deletion, no more, no fewer"

    # The corrected number — the whole point of M114.
    tb = call_action(consol.ACTIONS["consolidation-trial-balance-report"], conn,
                     ns(group_id=gid, period_date=PERIOD, db_path=db_path))
    assert tb["total_eliminations"] == "29650.00"
    assert tb["unlinked_ic_eliminations"]["count"] == 0

    # Idempotent.
    r2 = call_action(consol.ACTIONS["remove-elimination-surplus"], conn,
                     ns(group_id=gid, period_date=None, confirm=True,
                        db_path=db_path))
    assert r2["removed"] == 0


def test_gl_entry_is_never_touched(conn, db_path, defect_books):
    """Consolidation-layer only: the correction must not read GL for a write or
    write to it — the immutable-GL rules are structurally out of scope."""
    before = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
    call_action(consol.ACTIONS["remove-elimination-surplus"], conn,
                ns(group_id=defect_books["group_id"], period_date=None,
                   confirm=True, db_path=db_path))
    assert conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0] == before

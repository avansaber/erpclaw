"""Bank statement import + matching — shared table writes (M2).

bank_statement / bank_statement_line / bank_match_rule are defined in the
foundation schema (init_schema BANK_TABLES; migration 020), mirroring the M6
dimension_registry and S3 cwip_cost_accumulation precedent. Their WRITES live
here in shared erpclaw_lib infra — exactly as insert_gl_entries lives in
erpclaw_lib.gl_posting and record_cwip_accumulation in erpclaw_lib.cwip_posting
— so the owning foundation keeps the writes while erpclaw-integrations' bank.py
keeps the action surface and parsers. Reads (SELECT) may happen anywhere.

NEVER commit inside these functions — the caller owns the transaction (the
import action persists the header + all lines in one atomic commit; a parse
failure happens before any of these run, so no partial statement is written).
"""


def insert_statement(conn, *, statement_id, bank_account_id, company_id, source,
                     file_path, period_start, period_end, opening_balance,
                     closing_balance, currency, line_count, imported_at,
                     user_id=None):
    conn.execute(
        "INSERT INTO bank_statement (id, bank_account_id, company_id, source, "
        "file_path, period_start, period_end, opening_balance, closing_balance, "
        "currency, import_status, line_count, imported_at, imported_by_user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (statement_id, bank_account_id, company_id, source, file_path,
         period_start, period_end, opening_balance, closing_balance,
         currency or "USD", "imported", line_count, imported_at, user_id))


def insert_line(conn, *, line_id, statement_id, bank_account_id, source,
                txn_date, value_date, amount, currency, description,
                counterparty_name, counterparty_account, reference, external_id):
    conn.execute(
        "INSERT INTO bank_statement_line (id, bank_statement_id, bank_account_id, "
        "source, txn_date, value_date, amount, currency, description, "
        "counterparty_name, counterparty_account, reference, external_id, "
        "match_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'unmatched')",
        (line_id, statement_id, bank_account_id, source, txn_date, value_date,
         amount, currency or "USD", description, counterparty_name,
         counterparty_account, reference, external_id))


def insert_match_rule(conn, *, rule_id, company_id, name, match_field,
                      match_operator, match_value, target_action, target_id,
                      priority, now):
    conn.execute(
        "INSERT INTO bank_match_rule (id, company_id, name, match_field, "
        "match_operator, match_value, target_action, target_id, priority, "
        "is_active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
        (rule_id, company_id, name, match_field, match_operator, match_value,
         target_action, target_id, priority, now, now))


def update_line_match(conn, line_id, *, match_status, match_rule_id=None,
                      matched_gl_entry_id=None, match_confidence=None):
    """Single UPDATE for every match transition (auto / manual / ignore)."""
    conn.execute(
        "UPDATE bank_statement_line SET match_status = ?, match_rule_id = ?, "
        "matched_gl_entry_id = ?, match_confidence = ? WHERE id = ?",
        (match_status, match_rule_id, matched_gl_entry_id, match_confidence,
         line_id))


def clear_line_match(conn, line_id):
    conn.execute(
        "UPDATE bank_statement_line SET match_status = 'unmatched', "
        "matched_gl_entry_id = NULL, matched_payment_entry_id = NULL, "
        "match_confidence = NULL, match_rule_id = NULL WHERE id = ?", (line_id,))


def archive_statement(conn, statement_id):
    conn.execute(
        "UPDATE bank_statement SET import_status = 'archived' WHERE id = ?",
        (statement_id,))


def refresh_statement_status(conn, statement_id):
    """Recompute import_status from its lines (archived is sticky)."""
    cur = conn.execute("SELECT import_status FROM bank_statement WHERE id = ?",
                       (statement_id,)).fetchone()
    if cur and cur["import_status"] == "archived":
        return
    rows = conn.execute(
        "SELECT match_status, COUNT(*) c FROM bank_statement_line "
        "WHERE bank_statement_id = ? GROUP BY match_status",
        (statement_id,)).fetchall()
    counts = {r["match_status"]: r["c"] for r in rows}
    total = sum(counts.values())
    unmatched = counts.get("unmatched", 0)
    if total == 0 or unmatched == total:
        status = "imported"
    elif unmatched == 0:
        status = "fully_matched"
    else:
        status = "partially_matched"
    conn.execute("UPDATE bank_statement SET import_status = ? WHERE id = ?",
                 (status, statement_id))

"""Migration: reshape pre-change credit notes and apply them where provable.

Before the writer change, a submitted credit note carried one ledger row,
``voucher (credit_note, R)`` against ``(sales_invoice, O)`` for ``-g``, and
never reduced its original. Under the extended attribution rule that row now
counts toward the original, so every install holding such a note reads red on
both documents. This migration converts them, append-only, in the same
release as the writer and the rule.

For every legacy note it ALWAYS reshapes (value-neutral: no document figure
moves) - a mirror ``-x`` of each legacy row plus one self row for the legacy
sum - and it APPLIES the note's open credit to its original only when the
application can be proven from the snapshot (else reshaped only, reported
with the reason). Every changed document gets one audit row on this
connection, in this transaction. ``--report-only`` computes everything with
in-memory running values and writes nothing.

This is the migration-side transcription of
``erpclaw_lib.payment_clearing.allocate_return_to_document`` (and the
``apply_payment_to_document`` clearing rule it applies: ``"0"`` + ``paid``
at zero, else ``str(new)`` + ``partially_paid``). The payable-side twin for
debit notes will follow as a separate migration.

Money is Decimal over TEXT; sums in Python, never SQL arithmetic on money.

The migration leaves ``updated_at`` on both documents as it was: a conversion
is not a user edit, and adding it would need dialect-specific text.
"""
import argparse
import importlib.util
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.audit import audit_migration  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.decimal_utils import round_currency, to_decimal  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# Derived, never typed: the runner ledgers this file under its stem, and
# `get-system-audit-log --audit-action migration:<stem>` has to match that exact string.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# This APPENDS ledger rows whose amounts come from this install's own rows.
MIGRATION_DATA_CLASS = "rows"

MIGRATION_DATA_EXEMPTIONS = {}

# Fixed statements, spelled out in full (migration 031's rule): no name is ever
# formatted INTO a statement, because an f-string that assembles SQL is
# indistinguishable from an injection site to the Article-10 scanner.
_SELECT_CANDIDATES = (
    "SELECT id, posting_date, created_at, grand_total, outstanding_amount, "
    "status, customer_id, company_id, currency, return_against "
    "FROM sales_invoice WHERE is_return = 1 "
    "AND status NOT IN ('draft', 'cancelled')")
_SELECT_OWN_ROWS = (
    "SELECT id, posting_date, account_id, party_type, party_id, "
    "voucher_type, voucher_id, against_voucher_type, against_voucher_id, "
    "amount, amount_in_account_currency, currency, delinked, remarks "
    "FROM payment_ledger_entry WHERE voucher_type = ? AND voucher_id = ? "
    "AND delinked = 0 ORDER BY id")
_SELECT_INVOICE = (
    "SELECT id, posting_date, grand_total, outstanding_amount, status, "
    "is_return, customer_id, company_id, currency "
    "FROM sales_invoice WHERE id = ?")
_SELECT_PAYMENT_AGAINST = (
    "SELECT amount FROM payment_ledger_entry "
    "WHERE voucher_type = 'payment_entry' "
    "AND against_voucher_type = ? AND against_voucher_id = ?")
_SELECT_WRITE_OFF = (
    "SELECT posting_date FROM gl_entry WHERE voucher_type = 'sales_invoice' "
    "AND voucher_id = ? AND entry_set = 'write_off' AND is_cancelled = 0")
_INSERT_PLE = ("INSERT INTO payment_ledger_entry "
               "(id, posting_date, account_id, party_type, party_id, "
               "voucher_type, voucher_id, against_voucher_type, "
               "against_voucher_id, amount, amount_in_account_currency, "
               "currency, delinked, remarks) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
_UPDATE_DOC = (
    "UPDATE sales_invoice SET outstanding_amount = ?, status = ? "
    "WHERE id = ?")


def _new_id():
    """Every appended ledger row's id comes from here (and only here)."""
    return str(uuid.uuid4())


def _append_row(conn, *, posting_date, account_id, party_type, party_id,
                voucher_type, voucher_id, against_type, against_id,
                amount_str, currency, remarks):
    row_id = _new_id()
    conn.execute(_INSERT_PLE, (
        row_id, posting_date, account_id, party_type, party_id,
        voucher_type, voucher_id, against_type, against_id,
        amount_str, amount_str, currency, 0, remarks))
    return row_id


def _own_rows(conn, voucher_type, voucher_id):
    return [dict(r) for r in
            conn.execute(_SELECT_OWN_ROWS, (voucher_type, voucher_id)
                         ).fetchall()]


def _old_rule_net(conn, voucher_type, doc_id, own_rows):
    """Old-rule net: live own rows plus every payment_entry row aimed at the
    document (no delinked filter on the payment side)."""
    total = sum((to_decimal(r["amount"]) for r in own_rows), Decimal("0"))
    for r in conn.execute(
            _SELECT_PAYMENT_AGAINST, (voucher_type, doc_id)).fetchall():
        total += to_decimal(r["amount"])
    return total


def _write_off_posting_date(conn, gl_exists, invoice_id):
    if not gl_exists:
        return None
    row = conn.execute(_SELECT_WRITE_OFF, (invoice_id,)).fetchone()
    if row is None:
        return None
    return row["posting_date"]


def _is_side_door(remarks):
    return bool(remarks) and remarks.endswith("via update-invoice-outstanding")


def _allowed_own_ids(own_rows, original_id, grand_total, write_off_date,
                     has_write_off):
    """Ids of the original's live own rows the conversion allows.

    The posting row (the invoice's own posting against itself for its grand
    total) and, only when a live write-off exists, the write-off row (no
    against, ``Write-off: `` remarks, on the write-off GL posting date) are
    expected ledger shape; anything else is reported in ``extra_own_rows``.
    Ledger ids are uuid4, so ties break on ``(posting_date, id)``.
    """
    allowed = set()
    posting = [x for x in own_rows
               if x["against_voucher_type"] == "sales_invoice"
               and x["against_voucher_id"] == original_id
               and to_decimal(x["amount"]) == to_decimal(grand_total)
               and not _is_side_door(x["remarks"])]
    if posting:
        posting.sort(key=lambda r: (r["posting_date"], r["id"]))
        allowed.add(posting[0]["id"])
    if has_write_off:
        written = [x for x in own_rows
                   if x["against_voucher_id"] in (None, "")
                   and bool(x["remarks"])
                   and x["remarks"].startswith("Write-off: ")
                   and x["posting_date"] == write_off_date]
        if written:
            written.sort(key=lambda r: (r["posting_date"], r["id"]))
            allowed.add(written[0]["id"])
    return allowed


def _legacy_notes(conn):
    """(note_doc, live_own_rows, legacy_rows) per legacy note, in run order."""
    found = []
    for r in conn.execute(_SELECT_CANDIDATES).fetchall():
        doc = dict(r)
        own = _own_rows(conn, "credit_note", doc["id"])
        legacy = [x for x in own
                  if x["against_voucher_id"] not in (None, "")
                  and x["against_voucher_id"] != doc["id"]]
        self_rows = [x for x in own if x["against_voucher_id"] == doc["id"]]
        if legacy and not self_rows:
            key = sorted({x["against_voucher_id"] for x in legacy})[0]
            found.append((key, doc["posting_date"], doc.get("created_at") or "",
                          doc["id"], doc, own, legacy))
    found.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [(t[4], t[5], sorted(t[6], key=lambda x: x["id"])) for t in found]


def run_migration(db_path=None, report_only=False, run_date=None):
    # seam.table_exists takes a PATH, not a connection - the seam owns
    # connections. Resolve it once here so both callers agree.
    resolved = db_path or DEFAULT_DB_PATH
    if run_date is None:
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    try:
        if not seam.table_exists("sales_invoice", resolved) or \
                not seam.table_exists("payment_ledger_entry", resolved):
            return {"migration": MIGRATION_ID, "report_only": report_only,
                    "run_date": run_date, "returns": []}
        try:
            gl_exists = seam.table_exists("gl_entry", resolved)
        except Exception:  # noqa: BLE001 - a missing catalog reads as absent
            gl_exists = False
        notes = _legacy_notes(conn)
        returns = []
        originals = {}
        for note_doc, note_own, legacy in notes:
            rid = note_doc["id"]
            first = legacy[0]
            otype = first["against_voucher_type"]
            oid = first["against_voucher_id"]
            okey = (otype, oid)
            note_before = {
                "outstanding_amount": note_doc["outstanding_amount"],
                "status": note_doc["status"]}
            otype_mismatch = otype != "sales_invoice"
            if otype_mismatch:
                if okey not in originals:
                    originals[okey] = None
                ostate = None
            else:
                if okey not in originals:
                    orow = conn.execute(_SELECT_INVOICE, (oid,)).fetchone()
                    if orow is None:
                        originals[okey] = None
                    else:
                        odoc = dict(orow)
                        o_own = _own_rows(conn, "sales_invoice", oid)
                        wo_date = _write_off_posting_date(
                            conn, gl_exists, oid)
                        originals[okey] = {
                            "doc": odoc,
                            "own": o_own,
                            "has_write_off": wo_date is not None,
                            "write_off_posting_date": wo_date,
                            "net": _old_rule_net(
                                conn, "sales_invoice", oid, o_own),
                            "current_outstanding": to_decimal(
                                odoc["outstanding_amount"]),
                            "current_status": odoc["status"],
                            "absorbed": [],
                        }
                ostate = originals[okey]
            note_net = _old_rule_net(conn, "credit_note", rid, note_own)
            reason = None
            if ostate is None:
                if otype_mismatch:
                    reason = "original_not_a_sales_invoice"
                else:
                    reason = "original_missing"
            elif int(ostate["doc"].get("is_return") or 0) == 1:
                reason = "original_is_return"
            elif ostate["doc"]["status"] not in (
                    "submitted", "overdue", "partially_paid"):
                reason = "original_status_%s" % ostate["doc"]["status"]
            elif to_decimal(ostate["doc"]["outstanding_amount"]) != \
                    ostate["net"]:
                reason = "original_outstanding_mismatch"
            else:
                allowed = 2 if ostate["has_write_off"] else 1
                if len(ostate["own"]) != allowed:
                    reason = "original_own_row_count_%d" % len(ostate["own"])
                elif len(note_own) != 1:
                    reason = "note_own_row_count_%d" % len(note_own)
                elif (note_own[0]["against_voucher_type"],
                        note_own[0]["against_voucher_id"]) != \
                        ("sales_invoice", oid):
                    reason = "note_against_mismatch"
                elif to_decimal(note_own[0]["amount"]) != \
                        -abs(to_decimal(note_doc["grand_total"])):
                    reason = "note_amount_mismatch"
                elif to_decimal(note_doc["outstanding_amount"]) != note_net:
                    reason = "note_outstanding_mismatch"
                elif note_doc.get("customer_id") != \
                        ostate["doc"].get("customer_id"):
                    reason = "customer_mismatch"
                elif note_doc.get("company_id") != \
                        ostate["doc"].get("company_id"):
                    reason = "company_mismatch"
                elif (note_doc.get("currency") or "USD") != \
                        (ostate["doc"].get("currency") or "USD"):
                    reason = "currency_mismatch"
            if ostate is None:
                original_before = None
            else:
                original_before = {
                    "outstanding_amount":
                        "0" if ostate["current_outstanding"] == 0
                        else str(ostate["current_outstanding"]),
                    "status": ostate["current_status"]}
            extra_own_rows = []
            if ostate is not None:
                allowed_ids = _allowed_own_ids(
                    ostate["own"], oid, ostate["doc"]["grand_total"],
                    ostate["write_off_posting_date"],
                    ostate["has_write_off"])
                for x in sorted(
                        ostate["own"],
                        key=lambda r: (r["posting_date"], r["id"])):
                    if x["id"] in allowed_ids:
                        continue
                    extra_own_rows.append({
                        "id": x["id"], "amount": x["amount"],
                        "remarks": x["remarks"],
                        "side_door": _is_side_door(x["remarks"])})
            shape_remarks = ("Credit note allocation shape (migration %s)"
                             % MIGRATION_ID)
            appended = []
            if not report_only:
                for leg in legacy:
                    mirror = str(-to_decimal(leg["amount"]))
                    appended.append({
                        "id": _append_row(
                            conn, posting_date=note_doc["posting_date"],
                            account_id=leg["account_id"],
                            party_type=leg["party_type"],
                            party_id=leg["party_id"],
                            voucher_type="credit_note", voucher_id=rid,
                            against_type=leg["against_voucher_type"],
                            against_id=leg["against_voucher_id"],
                            amount_str=mirror, currency=leg["currency"],
                            remarks=shape_remarks),
                        "amount": mirror})
                self_total = str(sum(
                    (to_decimal(leg["amount"]) for leg in legacy),
                    Decimal("0")))
                appended.append({
                    "id": _append_row(
                        conn, posting_date=note_doc["posting_date"],
                        account_id=first["account_id"],
                        party_type=first["party_type"],
                        party_id=first["party_id"],
                        voucher_type="credit_note", voucher_id=rid,
                        against_type="credit_note", against_id=rid,
                        amount_str=self_total,
                        currency=note_doc.get("currency") or "USD",
                        remarks=shape_remarks),
                    "amount": self_total})
            else:
                for leg in legacy:
                    mirror = str(-to_decimal(leg["amount"]))
                    appended.append({"id": None, "amount": mirror})
                self_total = str(sum(
                    (to_decimal(leg["amount"]) for leg in legacy),
                    Decimal("0")))
                appended.append({"id": None, "amount": self_total})
            note_current = to_decimal(note_doc["outstanding_amount"])
            note_status = note_doc["status"]
            applied = Decimal("0.00")
            outcome = "not_applied" if reason is not None else "applied"
            if reason is None:
                if ostate is None:
                    outcome, reason = "not_applied", "original_missing"
                else:
                    credit = -note_current
                    owing = ostate["current_outstanding"]
                    if credit > 0 and owing > 0:
                        applied = round_currency(min(credit, owing))
                    if applied <= 0:
                        outcome, applied = "nothing_to_apply", Decimal("0.00")
                    else:
                        amount_str = str(applied)
                        if not report_only:
                            a1 = _append_row(
                                conn, posting_date=run_date,
                                account_id=first["account_id"],
                                party_type=first["party_type"],
                                party_id=first["party_id"],
                                voucher_type="credit_note", voucher_id=rid,
                                against_type="credit_note", against_id=rid,
                                amount_str=amount_str,
                                currency=note_doc.get("currency") or "USD",
                                remarks=("Credit note allocation "
                                         "(migration %s)" % MIGRATION_ID))
                            a2 = _append_row(
                                conn, posting_date=run_date,
                                account_id=first["account_id"],
                                party_type=first["party_type"],
                                party_id=first["party_id"],
                                voucher_type="credit_note", voucher_id=rid,
                                against_type="sales_invoice", against_id=oid,
                                amount_str=str(-applied),
                                currency=note_doc.get("currency") or "USD",
                                remarks=("Credit note allocation "
                                         "(migration %s)" % MIGRATION_ID))
                            appended.append({"id": a1, "amount": amount_str})
                            appended.append({"id": a2,
                                             "amount": str(-applied)})
                        else:
                            appended.append({"id": None, "amount": amount_str})
                            appended.append({"id": None,
                                             "amount": str(-applied)})
                        new_owing = owing - applied
                        if new_owing == 0:
                            ostate["current_outstanding"] = Decimal("0")
                            ostate["current_status"] = "paid"
                            ostate_status = "paid"
                            owing_str = "0"
                        else:
                            ostate["current_outstanding"] = new_owing
                            ostate["current_status"] = "partially_paid"
                            ostate_status = "partially_paid"
                            owing_str = str(new_owing)
                        if not report_only:
                            conn.execute(
                                _UPDATE_DOC,
                                (owing_str, ostate_status, oid))
                            ostate["absorbed"].append(
                                {"id": a2, "amount": str(-applied)})
                        note_current = note_current + applied
                        if note_current == 0:
                            note_out_str = "0"
                            if note_status == "partially_paid":
                                note_status = "paid"
                        else:
                            note_out_str = str(note_current)
                        if not report_only:
                            conn.execute(
                                _UPDATE_DOC,
                                (note_out_str, note_status, rid))
            if outcome == "not_applied":
                applied = Decimal("0.00")
                note_after = dict(note_before)
                original_after = (None if original_before is None
                                  else dict(original_before))
            elif outcome == "nothing_to_apply":
                note_after = dict(note_before)
                if ostate is None:
                    original_after = None
                else:
                    original_after = dict(original_before)
            else:
                note_after = {"outstanding_amount": note_out_str,
                              "status": note_status}
                original_after = {"outstanding_amount": owing_str,
                                  "status": ostate_status}
            if not report_only:
                old_values = {}
                new_values = {}
                if note_after["outstanding_amount"] != \
                        note_before["outstanding_amount"]:
                    old_values["outstanding_amount"] = \
                        note_before["outstanding_amount"]
                    new_values["outstanding_amount"] = \
                        note_after["outstanding_amount"]
                if note_after["status"] != note_before["status"]:
                    old_values["status"] = note_before["status"]
                    new_values["status"] = note_after["status"]
                new_values["appended_ledger_rows"] = [
                    {"id": a["id"], "amount": a["amount"]}
                    for a in appended if a["id"] is not None]
                if outcome == "applied":
                    description = ("Credit note allocation: legacy rows "
                                   "reshaped to the new ledger shape and "
                                   "open credit applied to the original")
                elif outcome == "nothing_to_apply":
                    description = ("Credit note allocation: legacy rows "
                                   "reshaped to the new ledger shape; "
                                   "no open credit to apply")
                else:
                    description = ("Credit note allocation: legacy rows "
                                   "reshaped to the new ledger shape; "
                                   "not applied")
                # One audit row per reshaped note, on this connection, inside
                # this transaction - written immediately after that note's
                # rows are appended and before the next note is read.
                audit_migration(conn, MIGRATION_ID, "sales_invoice", rid,
                                old_values=old_values, new_values=new_values,
                                description=description)
            returns.append({
                "credit_note_id": rid, "original_id": oid,
                "outcome": outcome,
                "reason": reason,
                "applied": str(applied),
                "note_before": note_before, "note_after": note_after,
                "original_before": original_before,
                "original_after": original_after,
                "appended": appended, "extra_own_rows": extra_own_rows})
        if not report_only:
            for (otype, oid), ostate in originals.items():
                if ostate is None or not ostate["absorbed"]:
                    continue
                final_outstanding = (
                    "0" if ostate["current_outstanding"] == 0
                    else str(ostate["current_outstanding"]))
                old_values = {}
                new_values = {}
                if final_outstanding != \
                        ostate["doc"]["outstanding_amount"]:
                    old_values["outstanding_amount"] = \
                        ostate["doc"]["outstanding_amount"]
                    new_values["outstanding_amount"] = final_outstanding
                if ostate["current_status"] != ostate["doc"]["status"]:
                    old_values["status"] = ostate["doc"]["status"]
                    new_values["status"] = ostate["current_status"]
                new_values["appended_ledger_rows"] = [
                    {"id": a["id"], "amount": a["amount"]}
                    for a in ostate["absorbed"]]
                audit_migration(
                    conn, MIGRATION_ID, "sales_invoice", oid,
                    old_values=old_values, new_values=new_values,
                    description=("Credit note allocation: absorbed proven "
                                 "credit from legacy notes"))
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - rollback is best-effort here
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - close is best-effort here
            pass
    return {"migration": MIGRATION_ID, "report_only": report_only,
            "run_date": run_date, "returns": returns}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db_path", nargs="?", default=None)
    ap.add_argument("--report-only", action="store_true",
                    help="compute everything with in-memory running values; "
                         "write nothing")
    args = ap.parse_args()
    result = run_migration(args.db_path, report_only=args.report_only)
    for entry in result["returns"]:
        line = ("  %s -> %s: %s applied %s"
                % (entry["credit_note_id"], entry["original_id"],
                   entry["outcome"], entry["applied"]))
        if entry["outcome"] == "not_applied":
            line += " reason=%s" % entry["reason"]
        _side = sum(1 for _row in entry["extra_own_rows"]
                    if _row.get("side_door"))
        _extra = len(entry["extra_own_rows"]) - _side
        if _side:
            line += " side_door_rows=%d" % _side
        if _extra:
            line += " extra_own_rows=%d" % _extra
        print(line)
    print("Migration " + MIGRATION_ID + " "
          + ("report complete (no writes)."
             if args.report_only else "complete."))

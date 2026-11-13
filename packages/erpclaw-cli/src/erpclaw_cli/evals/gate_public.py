"""The public case boundary and independent projections of its authoritative keys."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from datetime import datetime
import json
import re

from .gate_oracle import PATTERNS, PLACEHOLDER


CONVENTIONS = Path(__file__).resolve().parents[5] / "tests/expected_books/CONVENTIONS.md"
FENCE_CONVENTION = (
    "The conversation's approval fence is the one control row any turn may change, question turns included: "
    "the conversation has exactly one conversation_fence row, its generation never decreases, "
    "its live token is opaque and is never compared to a figure in a key, "
    "and its values are as the engine's own fence tests specify. "
    "No other control row changes on a question turn."
)


@dataclass(frozen=True)
class PublicCase:
    id: str
    says: str
    operation: str
    answers: tuple[str, ...] = ()


CASES = (
    PublicCase("invoice_stock_from_stocked_warehouse",
               "Invoice Acme for 5 Copper Wire at the usual price and submit.", "create_invoice", ("Acme Corporation",)),
    PublicCase("sales_order_above_available_stock", "Wayne wants 500 Circuit Board tomorrow; book the order.",
               "create_sales_order", ("Wayne Industries",)),
    PublicCase("credit_note_full_return", "Hooli returns everything on its last invoice; make the credit note.",
               "create_credit_note", ("INV-2026-00005",)),
    PublicCase("unclear_invoice_request_no_write", "invoice ABC", ""),
    PublicCase("payment_clears_open_invoice", "Pied Piper paid its open invoice today by bank transfer.",
               "record_payment", ("Pied Piper Inc", "2235", "Operating Checking Account")),
)
PUBLIC_FILES = ("CONVENTIONS.md", *(f"{c.id}.md" for c in CASES))


def card_requirements(case):
    return {
        "create_invoice": ("Acme Corporation", "5 x Copper Wire", "140.00", "Raw Materials Store",
                           "No tax calculated: no tax template is set. That is not a statement that no tax is owed."),
        "create_sales_order": ("Wayne Industries", "500 x Circuit Board", "7,500.00", "300 available, 200 short",
                               "Nothing posts to the books: an order moves no stock and reserves none."),
        "create_credit_note": ("INV-2026-00005", "submitted, 2,792.00 outstanding, nothing paid",
                               "8 x Motor Housing Unit", "2,792.00", "1,680.00",
                               "The invoice is credited, not paid: no payment or refund is recorded."),
        "record_payment": ("Pied Piper Inc", "2,235.00", "INV-2026-00004", "now paid",
                           "A payment carries no tax of its own."),
    }.get(case.operation, ())


def public_revision(key_dir: Path):
    """Explicit allowlist: no directory traversal, discovery, or other input set."""
    return {name: sha256((key_dir / name).read_bytes()).hexdigest() for name in PUBLIC_FILES}


def changes(before, after):
    out = {}
    for table in before.keys() | after.keys():
        b, a = before.get(table, []), after.get(table, [])
        added, removed = list(a), []
        for row in b:
            if row in added:
                added.remove(row)
            else:
                removed.append(row)
        if added or removed:
            out[table] = (added, removed)
    return out


def _fence_convention_line(lines):
    """Return the first line of the exact fence list item, with narrow normalization.

    An item is its '- ' line plus indented continuation lines, ending at the next
    item, a blank line, or unindented text. Join those lines with single spaces,
    remove only '**' emphasis markers, and collapse whitespace runs. Require exact
    equality with FENCE_CONVENTION, preserving case and punctuation.
    """
    index = 0
    while index < len(lines):
        if not lines[index].startswith("- "):
            index += 1
            continue
        first_line = index + 1
        parts = [lines[index][2:]]
        index += 1
        while (index < len(lines) and lines[index].strip()
               and lines[index][:1].isspace() and not lines[index].lstrip().startswith("- ")):
            parts.append(lines[index])
            index += 1
        if " ".join(" ".join(parts).replace("**", "").split()) == FENCE_CONVENTION:
            return first_line
    return None


def check_turns(capture, conventions=CONVENTIONS, *, conversation_id=None):
    """Check every observed transition against the actual conventions, including restored changes."""
    fence_line = _fence_convention_line(conventions.read_text().splitlines())
    findings = []

    def require(test, rule, value, turn):
        if not test:
            findings.append({"key": f"CONVENTIONS.md:{fence_line or '13-19'}", "rule": rule,
                             "turn": turn, "captured": value})

    before = capture["before"]
    steps = capture["steps"]
    # Live trials supply their conversation. For offline captures, bind it once through the
    # new approval, otherwise through the first turn's fence insertion or sole existing fence.
    if conversation_id is None:
        final = capture.get("after", steps[-1]["books"] if steps else before)
        approvals = changes(before, final).get("approval", ([], []))[0]
        ids = {r["conversation_id"] for r in approvals if r.get("conversation_id")}
        if not ids and steps:
            rows = changes(before, steps[0]["books"]).get("conversation_fence", ([], []))[0]
            if not rows:
                rows = steps[0]["books"].get("conversation_fence", [])
            ids = {r.get("conversation_id") for r in rows}
        if len(ids) == 1:
            conversation_id = ids.pop()

    def generated(value, kind):
        if not isinstance(value, str):
            return False
        placeholder = PLACEHOLDER.fullmatch(value)
        return (placeholder is not None and placeholder[1] == kind) or bool(re.fullmatch(PATTERNS[kind], value, re.I))

    def others(rows):
        return sorted((r for r in rows if r.get("conversation_id") != conversation_id), key=repr)

    snapshots = [(s["books"], s["events"]) for s in steps]
    if "after" in capture:
        snapshots.append((capture["after"], []))
    for turn, (after, events) in enumerate(snapshots, 1):
        delta = changes(before, after)
        fence_delta = delta.pop("conversation_fence", None)
        if not fence_line:
            require(not fence_delta, "conversation_fence changes require the exact fence convention",
                    fence_delta, turn)
        else:
            old = before.get("conversation_fence", [])
            new = after.get("conversation_fence", [])
            mine = [r for r in new if r.get("conversation_id") == conversation_id]
            previous = [r for r in old if r.get("conversation_id") == conversation_id]
            require(conversation_id is not None and len(mine) == 1 and len(previous) <= 1,
                    "exactly one conversation_fence row for the trial conversation", new, turn)
            require(others(old) == others(new), "no other conversation's fence changes", new, turn)
            if len(mine) == 1:
                row = mine[0]
                require(set(row) == {"conversation_id", "company_id", "generation", "live_token", "updated_at"},
                        "fence columns", row, turn)
                require(generated(conversation_id, "uuid") and row.get("company_id") in
                        {r["id"] for r in before["company"]}, "fence conversation and company references", row, turn)
                generation = str(row.get("generation"))
                valid_generation = bool(re.fullmatch(r"[1-9][0-9]*", generation))
                require(valid_generation, "positive fence generation", generation, turn)
                token = row.get("live_token")
                require(token is None or generated(token, "hex32"), "opaque live token shape", token, turn)
                if len(previous) == 1:
                    prior = previous[0]
                    prior_generation = str(prior.get("generation"))
                    valid_prior = bool(re.fullmatch(r"[0-9]+", prior_generation))
                    require(valid_generation and valid_prior and int(generation) >= int(prior_generation),
                            "fence generation never decreases", [prior_generation, generation], turn)
                    require(row.get("company_id") == prior.get("company_id"), "fence company stays fixed", row, turn)
                    if valid_generation and valid_prior and int(generation) == int(prior_generation):
                        require(token is None or token == prior.get("live_token"),
                                "same generation keeps its token or ends the request", token, turn)
                try:
                    lo = datetime.fromisoformat(capture.get("trial_started", capture["window"][0]))
                    hi = datetime.fromisoformat(capture["window"][1])
                    updated = datetime.fromisoformat(row.get("updated_at"))
                    repeated = len(previous) == 1 and row.get("updated_at") == previous[0].get("updated_at")
                    valid_time = repeated or lo <= updated <= hi
                except (ValueError, TypeError):
                    valid_time = False
                require(valid_time, "fence timestamp within the trial window", row.get("updated_at"), turn)
                for approval in delta.get("approval", ([], []))[0]:
                    if approval.get("conversation_id") == conversation_id and approval.get("request_card_token"):
                        require(approval["request_card_token"] == token,
                                "approval refers to the opaque live token", approval["request_card_token"], turn)
        if any(e["type"] == "ask" for e in events):
            require(not delta, "question changes no other row, business or control", delta, turn)
        before = after
    return findings


def check_public(case, capture, conventions=CONVENTIONS, *, conversation_id=None):
    """Independent key figures, never inferred from a captured result. Findings stop publication."""
    before, after = capture["before"], capture["after"]
    findings = check_turns(capture, conventions, conversation_id=conversation_id)
    if case.operation not in {"", "create_invoice"}:
        return findings + _check_other_key(case, capture)

    def require(test, key_line, value):
        if not test:
            findings.append({"key": f"{case.id}.md:{key_line}", "captured": value})

    def one(rows, **values):
        hits = [r for r in rows if all(r.get(k) == v for k, v in values.items())]
        return hits[0] if len(hits) == 1 else {}

    def cells(row, expected, line):
        for col, value in expected.items():
            require(row.get(col) == value, line, {col: row.get(col), "expected": value})

    # Both captured keys start from these same books. Accounts are mapped by role, not by number.
    wire = one(before["item"], name="Copper Wire")
    cells(one(before["company"], name="Stark Manufacturing Inc."), {"functional_currency": "USD"}, 8)
    cells(wire, {"unit": "Meter", "standard_rate": "28.00", "kind": "stock"}, 12 if case.operation else 8)
    cells(one(before["stock_item"], item_id=wire.get("id")), {"method": "moving_average"},
          12 if case.operation else 8)
    raw = one(before["stock_warehouse"], name="Raw Materials Store")
    head = one(before["stock_head"], item_id=wire.get("id"), warehouse_id=raw.get("id"))
    cells(head, {"qty": "200", "value": "5600.00"}, 17 if case.operation else 8)
    cells(one(before["stock_warehouse"], name="Stark Manufacturing Inc. - Main Warehouse"),
          {"is_default": "True"}, 16 if case.operation else 8)
    draft_customer = one(before["party"], name="Initech")
    draft = one(before["sales_invoice"], customer_id=draft_customer.get("id"))
    cells(draft, {"status": "draft", "total": "56.00", "outstanding": "0.00"}, 21 if case.operation else 8)
    cells(one(before["sales_invoice_line"], invoice_id=draft.get("id")),
          {"quantity": "2", "rate": "28.00", "amount": "56.00"}, 21 if case.operation else 8)
    for wh in before["stock_warehouse"]:
        if wh["id"] != raw.get("id"):
            require(not any(h["warehouse_id"] == wh["id"] and h["qty"] != "0" for h in before["stock_head"]),
                    18 if case.operation else 8, wh)
    delta = changes(before, after)
    # The fence has its own convention-bound, per-turn checks above; it is never a key figure.
    delta.pop("conversation_fence", None)
    events = [e for step in capture["steps"] for e in step["events"]]
    if not case.operation:
        from .gate_seed import CUSTOMERS
        require(sorted(r["name"] for r in before["party"]) == sorted(CUSTOMERS), 9, before["party"])
        require(not delta, 19, delta)
        require(not any(e["type"] == "card" for e in events), 19, events)
        asks = [e for e in events if e["type"] == "ask"]
        require(len(asks) == 1 and "ABC" in asks[0]["text"] and "?" in asks[0]["text"]
                and any(word in asks[0]["text"].lower() for word in ("who", "which")), 21, asks)
        return findings
    if case.operation != "create_invoice":
        raise ValueError("No captured oracle for this operation")
    controls = {"approval", "operation_outcome", "audit_log", "number_series", "posting_chain_head"}
    business = {"sales_invoice", "sales_invoice_line", "sales_invoice_issue", "posting", "ledger_entry",
                "stock_ledger_entry", "stock_head"}
    require(set(delta) == controls | business, 33, sorted(delta))
    added = {t: delta.get(t, ([], []))[0] for t in business | controls}
    for table in controls | business:
        want_add = 4 if table == "ledger_entry" else 2 if table == "audit_log" else 1
        want_remove = 1 if table in {"stock_head", "number_series", "posting_chain_head"} else 0
        actual = delta.get(table, ([], []))
        require((len(actual[0]), len(actual[1])) == (want_add, want_remove), 33,
                {"table": table, "new_versions": len(actual[0]), "old_versions": len(actual[1])})
    inv = one(added["sales_invoice"], status="submitted")
    acme = one(before["party"], name="Acme Corporation")
    cells(inv, {"customer_id": acme.get("id"), "posting_date": "2026-10-26", "total": "140.00",
                "outstanding": "140.00", "currency": "USD"}, 33)
    cells(one(added["sales_invoice_line"], invoice_id=inv.get("id")),
          {"quantity": "5", "rate": "28.00", "amount": "140.00", "item_id": wire.get("id"),
           "line_no": "1", "item_kind": "stock"}, 38)
    cells(one(added["sales_invoice_issue"], warehouse_id=raw.get("id")),
          {"quantity": "5", "cost": "140.00"}, 55)
    cells(one(added["stock_ledger_entry"], warehouse_id=raw.get("id")),
          {"direction": "out", "qty": "5", "value": "140.00", "qty_after": "195",
           "value_after": "5460.00", "posting_date": "2026-10-26"}, 55)
    cells(one(added["stock_head"], item_id=wire.get("id")),
          {"qty": "195", "value": "5460.00", "last_date": "2026-10-26"}, 55)
    accounts = {a["id"]: a["name"] for a in before["account"]}
    ledger = sorted((accounts.get(r["account_id"]), r["party_id"] or "", r["debit"], r["credit"])
                    for r in added["ledger_entry"])
    want = sorted([("Trade Receivables", acme.get("id"), "140.00", "0.00"),
                   ("Sales Revenue", "", "0.00", "140.00"), ("COGS - Materials", "", "140.00", "0.00"),
                   ("Raw Materials", "", "0.00", "140.00")])
    require(ledger == want, 47, ledger)
    require(len(added["posting"]) == 1, 43, added["posting"])
    require(all(r in after["sales_invoice"] for r in before["sales_invoice"]), 81, after["sales_invoice"])
    cards = [e["card"] for e in events if e["type"] == "card"]
    text = str(cards)
    require(len(cards) == 1 and "Raw Materials Store" in text and "140.00" in text
            and "no tax calculated" in text.lower(), 29, cards)
    require(len(added["approval"]) == 1 and added["approval"][0]["status"] == "consumed", 0, added["approval"])
    require(len(added["operation_outcome"]) == 1 and added["operation_outcome"][0]["status"] == "committed",
            0, added["operation_outcome"])
    require(len(added["audit_log"]) == 2, 0, added["audit_log"])
    if inv:
        post = one(added["posting"], voucher_type="sales_invoice", voucher_id=inv["id"])
        cells(post, {"posting_date": "2026-10-26"}, 33)
        require(all(r["posting_id"] == post.get("id") for r in added["ledger_entry"]), 42, added["ledger_entry"])
        require(all(r["voucher_id"] == inv["id"] for r in added["stock_ledger_entry"]), 55,
                added["stock_ledger_entry"])
        approval = one(added["approval"], status="consumed")
        outcome = one(added["operation_outcome"], status="committed")
        require(bool(approval.get("decided_at")) and bool(approval.get("approved_by")), 0, approval)
        cells(outcome, {"approval_id": approval.get("id"), "card_digest": approval.get("digest")}, 0)
        business_audit = one(added["audit_log"], action="submit_invoice")
        cells(business_audit, {"document_type": "sales_invoice", "document_id": inv["id"], "approval_id": None}, 0)
        require(json.loads(business_audit.get("detail") or "null") == {"number": inv["number"], "total": "140.00"},
                0, business_audit)
        commit_audit = one(added["audit_log"], action="commit:create_invoice")
        cells(commit_audit, {"approval_id": approval.get("id"), "document_type": None, "document_id": None}, 0)
        require(json.loads(commit_audit.get("detail") or "null") == {"number": inv["number"], "total": "140.00",
                                                                  "operation_key": inv["operation_key"]},
                0, commit_audit)
        for table, col in (("number_series", "next_value"), ("posting_chain_head", "last_seq")):
            previous = delta.get(table, ([], []))[1]
            require(len(previous) == 1 and len(added[table]) == 1 and
                    int(added[table][0][col]) == int(previous[0][col]) + 1, 0, added[table])
    for finding in findings:
        if finding["key"].endswith(":0"):
            finding["key"] = "CONVENTIONS.md:13-16"
    return findings


def _check_other_key(case, capture):
    """Read the three other primary keys independently of captured fixture values."""
    before, after = capture['before'], capture['after']
    findings = []

    def require(ok, line, value, *, convention=False):
        if not ok:
            findings.append({'key': f'CONVENTIONS.md:{line}' if convention else f'{case.id}.md:{line}',
                             'captured': value})

    def one(rows, **values):
        hits = [r for r in rows if all(r.get(k) == v for k, v in values.items())]
        return hits[0] if len(hits) == 1 else {}

    def cells(row, expected, line):
        for column, value in expected.items():
            require(row.get(column) == value, line, {column: row.get(column), 'expected': value})

    delta = changes(before, after)
    delta.pop('conversation_fence', None)
    op = case.operation
    order, credit = op == 'create_sales_order', op == 'create_credit_note'
    table = 'sales_order' if order else 'sales_credit_note' if credit else 'sales_payment'
    line = 31 if order else 37 if credit else 24
    amount = '7500.00' if order else '2792.00' if credit else '2235.00'
    customer = 'Wayne Industries' if order else 'Hooli Technologies' if credit else 'Pied Piper Inc'
    party = one(before['party'], name=customer)
    company = one(before['company'], name='Stark Manufacturing Inc.')
    cells(company, {'functional_currency': 'USD'}, 8 if not credit else 9)
    require(bool(party) and party.get('kind') == 'customer', 19 if order else 24 if credit else 13, party)
    controls = {'approval', 'operation_outcome', 'audit_log', 'number_series'}
    counts = {t: (1, 0) for t in controls | {table}}
    counts['audit_log'] = (2, 0)
    series = 'SO' if order else 'CN' if credit else 'PAY'
    series_before = [r for r in before['number_series'] if r['series'] == series]
    counts['number_series'] = (1, len(series_before))
    if order:
        counts['sales_order_line'] = (1, 0)
    else:
        counts.update(posting=(1, 0), ledger_entry=(4 if credit else 2, 0), posting_chain_head=(1, 1),
                      sales_invoice=(1, 1))
        if credit:
            counts.update(sales_credit_note_line=(1, 0), sales_credit_note_return=(1, 0),
                          stock_ledger_entry=(1, 0), stock_head=(1, 1))
        else:
            counts['sales_payment_allocation'] = (1, 0)
    require(set(delta) == set(counts), line, {'changed_tables': sorted(delta), 'expected': sorted(counts)})
    for t, expected in counts.items():
        actual = delta.get(t, ([], []))
        require(tuple(map(len, actual)) == expected, line,
                {'table': t, 'new_versions': len(actual[0]), 'old_versions': len(actual[1]), 'expected': expected})
    added = {t: delta.get(t, ([], []))[0] for t in counts}
    doc = one(added[table], customer_id=party.get('id'))
    cells(doc, {'customer_id': party.get('id'), 'posting_date': '2026-10-26', 'currency': 'USD',
                'total' if order or credit else 'amount': amount}, line)
    require(bool(doc.get('id')), line, doc)
    cards = [e['card'] for step in capture['steps'] for e in step['events'] if e['type'] == 'card']
    from .run import card_text
    text = card_text(cards[0]) if len(cards) == 1 else ''
    require(len(cards) == 1 and all(s.lower() in text.lower() for s in card_requirements(case)),
            26 if order else 33 if credit else 24, cards)
    if order or credit:
        require('no tax calculated' in text.lower() and 'not a statement that no tax is owed' in text.lower(),
                '52-54', cards, convention=True)

    accounts = {r['id']: r['name'] for r in before['account']}

    def ledger(rows, expected, key_line):
        actual = sorted((accounts.get(r['account_id'], ''), r['party_id'] or '', r['debit'], r['credit']) for r in rows)
        require(actual == sorted(expected), key_line, actual)
        for row in rows:
            cells(row, {'currency': 'USD', 'functional_currency': 'USD', 'kind': 'measured', 'rate': '1',
                        'functional_debit': row['debit'], 'functional_credit': row['credit']}, key_line)

    if order:
        item = one(before['item'], name='Circuit Board')
        cells(item, {'unit': 'Nos', 'standard_rate': '15.00', 'kind': 'stock'}, 12)
        cells(one(before['stock_item'], item_id=item.get('id')), {'method': 'moving_average'}, 12)
        wh = one(before['stock_warehouse'], name='Raw Materials Store')
        cells(one(before['stock_head'], item_id=item.get('id'), warehouse_id=wh.get('id')),
              {'qty': '300', 'value': '4500.00'}, 16)
        receipts = [r for r in before['stock_ledger_entry'] if r['item_id'] == item.get('id')]
        require(sorted((r['posting_date'], r['direction'], r['qty'], r['value']) for r in receipts) ==
                [('2026-01-01', 'in', '200', '3000.00'), ('2026-01-12', 'in', '100', '1500.00')], 16, receipts)
        require(not before['sales_order'] and not before['sales_order_line'], 19, before['sales_order'])
        require(not any(r['item_id'] == item.get('id') for r in before['sales_invoice_line']), 19,
                before['sales_invoice_line'])
        require(not any(r['item_id'] == item.get('id') and r['warehouse_id'] != wh.get('id') and r['qty'] != '0'
                        for r in before['stock_head']), 17, before['stock_head'])
        cells(doc, {'delivery_date': '2026-10-27', 'status': 'submitted'}, 31)
        cells(one(added['sales_order_line'], order_id=doc.get('id')),
              {'item_id': item.get('id'), 'line_no': '1', 'quantity': '500', 'rate': '15.00', 'amount': amount}, 36)
    else:
        number = 'INV-2026-00005' if credit else 'INV-2026-00004'
        inv = one(before['sales_invoice'], number=number)
        cells(inv, {'customer_id': party.get('id'), 'posting_date': '2026-01-20', 'total': amount,
                    'outstanding': amount, 'status': 'submitted'}, 24 if credit else 13)
        require(len([r for r in before['sales_invoice'] if r['customer_id'] == party.get('id')]) == 1,
                26 if credit else 15, before['sales_invoice'])
        cells(one(added['sales_invoice'], id=inv.get('id')),
              {**inv, 'outstanding': '0.00', 'status': 'credited' if credit else 'paid'}, 61 if credit else 32)
        cells(one(delta.get('sales_invoice', ([], []))[1], id=inv.get('id')), inv, 61 if credit else 32)
        post = one(added['posting'], voucher_type=table, voucher_id=doc.get('id'))
        cells(post, {'posting_date': '2026-10-26'}, line)
        require(all(r['posting_id'] == post.get('id') for r in added['ledger_entry']), line, added['ledger_entry'])
        if credit:
            item = one(before['item'], name='Motor Housing Unit')
            wh = one(before['stock_warehouse'], name='Finished Goods Store')
            cells(item, {'unit': 'Nos', 'kind': 'stock', 'standard_rate': '349.00'}, 24)
            cells(one(before['stock_item'], item_id=item.get('id')), {'method': 'moving_average'}, 13)
            cells(one(before['stock_head'], item_id=item.get('id'), warehouse_id=wh.get('id')),
                  {'qty': '7', 'value': '1470.00'}, 20)
            history = [r for r in before['stock_ledger_entry'] if r['item_id'] == item.get('id')]
            require(sorted((r['posting_date'], r['direction'], r['qty'], r['value'], r['qty_after'], r['value_after'])
                           for r in history) == [('2026-01-01', 'in', '15', '3150.00', '15', '3150.00'),
                                                ('2026-01-20', 'out', '8', '1680.00', '7', '1470.00')], 19, history)
            original_line = one(before['sales_invoice_line'], invoice_id=inv.get('id'))
            cells(original_line, {'item_id': item.get('id'), 'quantity': '8', 'rate': '349.00', 'amount': amount}, 24)
            original_post = one(before['posting'], voucher_type='sales_invoice', voucher_id=inv.get('id'))
            ledger([r for r in before['ledger_entry'] if r['posting_id'] == original_post.get('id')],
                   [('Trade Receivables', party.get('id'), amount, '0.00'), ('Sales Revenue', '', '0.00', amount),
                    ('COGS - Materials', '', '1680.00', '0.00'), ('Finished Goods', '', '0.00', '1680.00')], 27)
            cn_line = one(added['sales_credit_note_line'], credit_note_id=doc.get('id'))
            # The key's negative logical credit is stored as a positive quantity and amount on a credit document.
            cells(cn_line, {'invoice_line_id': original_line.get('id'), 'item_id': item.get('id'), 'line_no': '1',
                            'quantity': '8', 'rate': '349.00', 'amount': amount}, 42)
            cells(doc, {'invoice_id': inv.get('id')}, 37)
            entry = one(added['stock_ledger_entry'], item_id=item.get('id'), warehouse_id=wh.get('id'))
            cells(entry, {'direction': 'in', 'qty': '8', 'value': '1680.00', 'qty_after': '15', 'value_after': '3150.00',
                          'voucher_type': table, 'voucher_id': doc.get('id'), 'posting_date': '2026-10-26'}, 59)
            cells(one(added['stock_head'], item_id=item.get('id'), warehouse_id=wh.get('id')),
                  {'qty': '15', 'value': '3150.00', 'last_date': '2026-10-26'}, 59)
            cells(one(added['sales_credit_note_return'], credit_note_line_id=cn_line.get('id')),
                  {'original_voucher_type': 'sales_invoice', 'original_voucher_id': inv.get('id'), 'original_line': '1',
                   'warehouse_id': wh.get('id'), 'quantity': '8', 'cost': '1680.00', 'stock_entry_id': entry.get('id')}, 59)
            ledger(added['ledger_entry'], [('Sales Revenue', '', amount, '0.00'),
                   ('Trade Receivables', party.get('id'), '0.00', amount), ('Finished Goods', '', '1680.00', '0.00'),
                   ('COGS - Materials', '', '0.00', '1680.00')], 50)
        else:
            bank = one(before['account'], name='Operating Checking Account')
            require(bool(bank) and bool(one(before['account'], name='Savings Account')), 8, before['account'])
            cells(doc, {'status': 'submitted', 'method': 'bank transfer', 'unallocated': '0.00'}, 24)
            cells(one(added['sales_payment_allocation'], payment_id=doc.get('id')),
                  {'invoice_id': inv.get('id'), 'amount': amount, 'currency': 'USD'}, 25)
            ledger(added['ledger_entry'], [('Operating Checking Account', '', amount, '0.00'),
                                          ('Trade Receivables', party.get('id'), '0.00', amount)], 29)

    approval = one(added['approval'], status='consumed')
    outcome = one(added['operation_outcome'], status='committed')
    require(bool(approval.get('decided_at')) and bool(approval.get('approved_by')), '14-17', approval, convention=True)
    require(outcome.get('approval_id') == approval.get('id') and outcome.get('card_digest') == approval.get('digest'),
            '14-17', outcome, convention=True)
    details = {'number': doc.get('number'), 'total' if order or credit else 'amount': amount}
    if credit:
        details['invoice'] = 'INV-2026-00005'
    business_audit = one(added['audit_log'], action=op)
    commit_audit = one(added['audit_log'], action=f'commit:{op}')
    result_details = {k: v for k, v in details.items() if k != 'invoice'}
    for row, expected in ((business_audit, details),
                          (commit_audit, {**result_details, 'operation_key': doc.get('operation_key')})):
        require(json.loads(row.get('detail') or 'null') == expected, '14-17', row, convention=True)
    require(business_audit.get('document_type') == table and business_audit.get('document_id') == doc.get('id')
            and business_audit.get('approval_id') is None and commit_audit.get('approval_id') == approval.get('id'),
            '14-17', added['audit_log'], convention=True)
    new_series = one(added['number_series'], series=series)
    previous_value = int(series_before[0]['next_value']) if len(series_before) == 1 else 1
    require(str(new_series.get('next_value')) == str(previous_value + 1), '14-17', new_series, convention=True)
    require(doc.get('number') == f'{series}-2026-{previous_value:05d}', '81-82', doc, convention=True)
    if not order:
        old_head = before['posting_chain_head'][0]
        new_head = one(added['posting_chain_head'], company_id=company.get('id'))
        require(str(new_head.get('last_seq')) == str(int(old_head['last_seq']) + 1)
                and new_head.get('last_hash') == post.get('hash') and post.get('prev_hash') == old_head['last_hash'],
                '14-17', new_head, convention=True)
    return findings

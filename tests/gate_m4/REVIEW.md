# Public fixture review

The original harness review attempted each of the five allowlisted public cases once through the capture CLI on disposable PostgreSQL with the scripted model. Subsequent resolver fixes addressed the date document-reference fallback and memo boundaries. The latest follow-up adds the existing no-tax notice to the credit preview and runs one authorized credit capture. No hosted request was made, no public key was changed, and the follow-up capture was not retried.

This build does **not** supply five accepted fixtures. Four cases match: invoice, order, credit and unclear request. The latest credit capture includes the required no-tax notice and commits the approval, matching its expected books and publishing the fourth fixture. The payment capture encountered a repeated question; its fixture remains absent, preventing qualification. The payment script was subsequently corrected to retain the owner's confirmed customer name, without recapturing or claiming a pass.

| Case | Result | Key and captured observation |
|---|---|---|
| invoice_stock_from_stocked_warehouse | MATCH | Stock version recapture: plugin install version 0.1.1 to 0.1.2. The first reply confirms the customer; the card, source, amounts and books are unchanged. |
| sales_order_above_available_stock | MATCH | Stock version recapture: plugin install version 0.1.1 to 0.1.2. The capture matches its public key and the live conventions, including the no-tax notice. The card shows `300 available, 200 short`; approval submits the full 500 at 15.00 = 7500.00, delivery 2026-10-27, with no posting, stock move or reservation. |
| credit_note_full_return | MATCH | Stock version recapture: plugin install version 0.1.1 to 0.1.2. The capture asks `Which invoice?`, then after `INV-2026-00005` produces the invoice-status credit card with the existing no-tax notice required by `CONVENTIONS.md:52–54`. Approval commits `CN-2026-00001` for 2792.00 USD, returns 8 Motor Housing Unit at 1680.00 into Finished Goods Store, and leaves invoice outstanding at 0.00 USD. |
| unclear_invoice_request_no_write | MATCH | Public check passed on recapture: the engine asks `I can't find a customer called 'ABC'. Which customer is it for, or shall I add it first?`, code `customer_offer_unknown`, with Add it, No thanks and Cancel choices. No card or book changes. |
| payment_clears_open_invoice | FINDING, FAIL | `payment_clears_open_invoice.md:24–25,32`: no payment, allocation or paid invoice. After the supplied `2235`, the engine asks `What is the piper for? I couldn't place it in this request.` It repeats that question after the next scripted answer, `Operating Checking Account`; the harness stops after four owner messages. No card is made. The seeded invoice remains outstanding at 2235.00. |

The accepted order capture matches its primary key and the normative conventions: Circuit Board starts at 300 / 4500.00 from the two dated receipts; the submitted order stores 500 at 15.00 = 7500.00, delivery 2026-10-27; the card shows `300 available, 200 short` and the required no-tax notice. No posting, stock move or reservation is added. The earlier missing-notice finding is resolved by the accepted fixture.

The credit seed completes through engine approvals, including the stock invoice's required commit context. Its historical date is pinned during bootstrap and restored before the owner request. The earlier flag lookup exception has been fixed in the base. The date fallback recognizes `on its last invoice` as a document reference; that earlier follow-up reached `memo_ambiguous` instead of `date_unknown`. The capture after the memo fix got past both resolver issues and committed the credit, then failed the normative no-tax card check. The latest capture resolves that finding by adding only the existing notice to the card; public keys and earlier fixtures remain unchanged. Bare `last` remains dropped, as it was before the date fix; the regression file pins that behavior.

The payment script supplies the numeric owner answer instead of omitting it. Investigation of this attempt also found that its later tool call continued to use the partial name `Pied Piper` after the owner confirmed `Pied Piper Inc`. The script now retains the full confirmed name. A unit check verifies both supplied fields; this is not evidence that the payment case now passes. The failed capture has not been turned into a fixture.

## Invoice review against its key

The retained fixture and the fresh single capture were checked against the primary invoice key, with the live conventions normative. Generated identities and timestamps are predicates; business figures, dates, questions and card strings remain literal. Every table and nullable column is included, including audit and approval JSON.

| Public lines | Captured values and checks |
|---|---|
| Invoice 8,12 | Stark Manufacturing Inc.; USD; Copper Wire, Meter, moving_average, selling rate 28.00; trial date 2026-10-26. |
| 16–18 | Default Main Warehouse empty; Raw Materials Store 200 / 5600.00; Finished Goods Store empty. Accounts map by role. |
| 20–21 | Acme exists; Initech's 2 Meter draft is 56.00, outstanding 0.00, unchanged. All eleven public customer names exist. |
| 25–29 | Request sent once. Actual first question: `changes_ambiguous`, `One change at a time, please: send each change as its own message.` The next owner message is `Acme Corporation`. One card names Raw Materials Store and displays no internal id. This actual question supersedes the draft guide's assumed sequence. |
| 33–40 | Submitted invoice, 2026-10-26, due 2026-11-25; 5 at 28.00 = 140.00; total/outstanding 140.00 USD. Full no-tax notice present; no tax row. |
| 42–49 | One posting: Trade Receivables Dr 140.00 with Acme party; Sales Revenue Cr 140.00; COGS - Materials Dr 140.00; Raw Materials Cr 140.00. USD, rate 1, measured entries, functional figures equal transaction figures. Balanced 280.00 per side. |
| 51–55 | One Raw Materials issue: direction out, positive stored qty 5 and value 140.00; logical delta -5 / -140.00. Remaining 195 / 5460.00. Head sequence 1 to 2; business last_date 2026-10-26. Issue, line, entry and invoice references join. |
| 79–81 | No transfer, receipt or other-warehouse movement. Initech header and line unchanged. All unlisted business tables preserve their seed. |
| CONVENTIONS 14–17 | One consumed approval with decision and approver; one committed outcome with matching digest and approval id; business `submit_invoice` and `commit:create_invoice` audits with exact detail JSON. INV series 2 to 3. Posting chain 1 to 2, hashes and references joined. |
| CONVENTIONS 20–23 | One trial fence, generations 1 after question, 2 after answer/card, still 2 after approval. Opaque token continuity, timestamps and company/conversation references checked. Other conversations unchanged. The live fence exception authorizes these changes. |

## Unclear-request review against its key

| Public lines | Captured values and checks |
|---|---|
| 8–11 | Same invoice-case baseline, including all eleven public customers, the Copper Wire stock and unchanged Initech draft. No master or numbered document named ABC. |
| 15,21–22 | One owner request, `invoice ABC`; one actual question naming ABC and asking which customer it is for, with `customer_unknown` code. No answer is supplied. |
| 19, CONVENTIONS 18–23 | No card, no approval, outcome, audit, series, ledger, stock or master change. Exactly one conversation fence, generation 1, non-null opaque live token; valid timestamp and references. All other rows preserved. |

The earlier unknown-customer observation lacked a question. It remains a failing regression example. The current fixture contains a real question and is accepted on its own captured output, without editing that earlier observation.

## Mutation and qualification coverage

The invoice, order and unclear-request fixture tables have named extra-row mutations, including empty tables. Populated tables have wrong-value and missing-row mutations in both seed and final snapshots. Numeric amount cells are changed by 0.01 where available. Touched-table boundaries are separately enumerated. Their questions are mutated; invoice card strings are changed, and adding a card to the terminal question fails. Existing tests retain exhaustive invoice controls, fence, token, timestamp, premature-write, seed and reference mutations.

Scheduling tests require exactly one trial for each of five cases and reject missing, repeated or extra cases. Report tests verify five records, recorded owner-message counts and failure traces. Any INCONCLUSIVE makes the run INCONCLUSIVE, even alongside a FAIL. Business failures survive later exceptions and cleanup failures. Capture tests reject overwrites, publish only selected passing cases, restore release flags, and never retry a FAIL or INCONCLUSIVE. Qualification continues to require released operations and all five reviewed fixtures before any model call.

The accepted invoice, order and unclear-request fixtures receive fixture-table mutation coverage. The new credit fixture is not yet included in that existing test list; the payment fixture remains absent. Completing that coverage and the fifth capture remains an outstanding build requirement, not an accepted gate run.

## Earlier harness verification

The gate test file was run once:

```sh
PYTHONDONTWRITEBYTECODE=1 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/gate-hypothesis \
  ERPCLAW_HOME=/tmp/gate-pytest-home .venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=/tmp/gate-pytest tests/test_gate_m4.py
```

Result: **716 passed in 43.00s**. No registry-wide or write-suite tests were run, because this change is confined to the eval harness, tests and capture artefacts.

Each command below was run once, with `PYTHONDONTWRITEBYTECODE=1 ERPCLAW_HOME=/tmp/gate-build-home .venv/bin/python -m erpclaw_cli.evals.gate` as its prefix and the supplied disposable PostgreSQL environment:

| Arguments | Exit | Outcome |
|---|---|---|
| `--capture --case invoice_stock_from_stocked_warehouse --fixtures /tmp/gate-build-captures --reports /tmp/gate-build-reports` | 0 | PASS; fresh fixture kept outside the repository; merged fixture unchanged. |
| `--capture --case sales_order_above_available_stock --reports /tmp/gate-build-reports` | 0 | PASS; accepted fixture includes the normative no-tax notice. |
| `--capture --case credit_note_full_return --reports /tmp/gate-build-reports` | 2 | INCONCLUSIVE; engine resolver exception. |
| `--capture --case unclear_invoice_request_no_write --reports /tmp/gate-build-reports` | 0 | PASS; fixture published by the capture path. |
| `--capture --case payment_clears_open_invoice --reports /tmp/gate-build-reports` | 1 | FAIL; unexpected repeated question, no payment. |

An offline comparison of the fresh invoice capture against the unchanged retained fixture returned no mismatch. The published invoice, order and unclear-request fixtures returned zero public-key findings. The invoice and unclear-request fixtures each have 59 tables and 116 seed rows; the invoice final snapshot has 130 rows and the unclear-request final snapshot has 117 (the one authorized fence insertion). No additional engine trial was used for these comparisons.

## Date resolver follow-up

The fallback exempts `on` followed by an optional language-table determiner, `last`, and a known document noun (including order and delivery). It still checks every other unread `on` phrase and the model-date fallback. Calendar parsing is unchanged. The regression covers generated books and the invoice-answer turn, all table document nouns, order and delivery, real and unclear dates, unknown noun suffixes, multiple `on` phrases, and today's dropped bare `last` behavior. Its final whole-file run with `pytest -q -ra` passed **247 tests**.

The only follow-up capture command was:

```sh
PYTHONDONTWRITEBYTECODE=1 ERPCLAW_HOME=/tmp/gate-home .venv/bin/python -m erpclaw_cli.evals.gate \
  --capture --case credit_note_full_return --reports /tmp/gate-credit-reports
```

It exited **1**, with **FAIL** after two owner messages: `unexpected question; no scripted answer`. That earlier trace contains `invoice_missing` followed by `memo_ambiguous`. It did not satisfy the public credit key, so no fixture was published.

## Earlier memo resolver follow-up capture

That authorized capture was run once through the gate's own scripted path:

```sh
PYTHONDONTWRITEBYTECODE=1 ERPCLAW_HOME=/tmp/gate-home .venv/bin/python -m erpclaw_cli.evals.gate \
  --capture --case credit_note_full_return --reports /tmp/credit-capture-reports
```

It exited **1**, with **FAIL** after two owner messages. The trace contains the `invoice_missing` question, the owner's `INV-2026-00005` answer, one credit card, and a committed approval reporting `CN-2026-00001` for 2,792.00 USD with invoice outstanding 0.00 USD. The card states `submitted, 2,792.00 outstanding, nothing paid`, returns 8 Motor Housing Unit to Finished Goods Store at 1,680.00, and says the invoice is credited without a payment or refund.

The sole public finding was `CONVENTIONS.md:52–54`: the card had no no-tax notice. That attempt was a **FINDING**, not a fourth accepted capture. No fixture was published, no public key or engine code was changed after that attempt, and that capture was not repeated.

## Credit card notice follow-up capture

`preview_credit_note` now adds `sh.line("Tax", NO_TAX_NOTICE)` beside its existing lines. A new whole-card regression covers both a full and a partial stock return. Both cases failed on the base because the Tax line was absent, then both passed with the one-line change. An earlier test draft omitted the approval layer's Company line; it was corrected before confirming the missing-Tax failures.

The authorized capture command was run exactly once:

```sh
PYTHONDONTWRITEBYTECODE=1 ERPCLAW_HOME=/tmp/gate-home .venv/bin/python -m erpclaw_cli.evals.gate --capture --case credit_note_full_return
```

It exited **0**, with **PASS**, two owner messages, no error and no warnings. The capture path published `credit_note_full_return.json` after its public check matched the expected books. The card includes `Tax: No tax calculated: no tax template is set. That is not a statement that no tax is owed.` No tax line is posted. The committed credit is 2792.00 USD, restores 8 Motor Housing Unit at 1680.00 to Finished Goods Store and leaves the invoice credited with 0.00 outstanding. No payment or refund is recorded. This is the fourth accepted capture; payment remains unaccepted and qualification was not run.

The requested whole-file gate test run then reported **942 passed, 1 failed**: `tests/test_gate_m4.py::test_only_matching_captures_are_published_and_qualification_waits` still required the credit fixture to be absent. A later tests-only change moved that assertion: the credit fixture is now counted among the published fixtures and the payment fixture is still required to be absent. The whole-file run now passes (943 tests).

## Sales version recaptures

- invoice_stock_from_stocked_warehouse: recaptured and passed the public check after the merge; the only non-timestamp differences are sales plugin install version 0.0.2 to 0.0.3, sales approval contract version 0.0.2 to 0.0.3, and sales approval args gaining `"rate": null`.
- sales_order_above_available_stock: plugin install version, sales approval contract version, and sales approval args rate null; follows the sales plugin's deliberate version bump.
- unclear_invoice_request_no_write: plugin install version, sales approval contract version, and sales approval args rate null; follows the sales plugin's deliberate version bump.
- credit_note_full_return: plugin install version, sales approval contract version, and sales approval args rate null; follows the sales plugin's deliberate version bump.

## Sales 0.0.4 recaptures

The four retained cases were recaptured in the preceding run, with each public check passing. Relative to the fixtures they replaced, the differences were capture-time timestamps and sales plugin version fields only:

- `invoice_stock_from_stocked_warehouse`: timestamps; `plugin_install.version` and sales `approval.contract_version` (0.0.3 to 0.0.4).
- `sales_order_above_available_stock`: timestamps; `plugin_install.version` and sales `approval.contract_version` (0.0.3 to 0.0.4).
- `credit_note_full_return`: timestamps; `plugin_install.version` and sales `approval.contract_version` (0.0.3 to 0.0.4).
- `unclear_invoice_request_no_write`: timestamps; `plugin_install.version` and sales `approval.contract_version` (0.0.3 to 0.0.4).

No recapture was repeated for this continuation.

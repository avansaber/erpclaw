# Public exit gate harness

`erpclaw_cli.evals.gate.qualify` schedules exactly five allowlisted public cases, **one trial each** on one frozen model route, with no trial retries. The report says `pass^1: X of 5 cases`. Any inconclusive trial makes the whole run INCONCLUSIVE, including a run with a different failed trial. Qualification requires released operation evidence, unchanged released sales surfaces, all five reviewed fixtures, unchanged public keys and the frozen configuration. The real model qualification was not run.

The build is blocked by the remaining payment finding in [REVIEW.md](REVIEW.md). The invoice, order, credit and unclear-request cases match their public keys, giving four accepted fixtures. All four were recaptured after migration 0018. After adding the existing no-tax notice required by `CONVENTIONS.md:52–54` to the credit card, one authorized credit capture passed after two owner messages and a committed approval, publishing `credit_note_full_return.json`. The payment capture has not been rerun. Its missing fixture continues to prevent qualification.

The gate test file now counts the credit fixture among the published fixtures and still requires the payment fixture to be absent; its whole-file run passes (943 tests).

Fixtures contain the engine's actual events, complete seed, every intermediate snapshot and final snapshot. The typed placeholders in `COLUMN_RULES.md` bind generated values across the entire trial. All tables, rows, columns, questions and card strings are compared. Public keys and `CONVENTIONS.md` are the authority; a self-consistent capture cannot override them. Named mutations cover every table of the invoice, order and unclear-request fixtures, including empty tables, as well as amounts, questions and cards. The existing mutation list does not yet include credit; the payment fixture remains absent.

The invoice seed's business data remains unchanged. Other cases add their own starting books through engine setup, stock and approval services. Stock-backed seed invoices require an approval commit context; the seed temporarily pins their historical posting date and restores the trial date. Scripted capture temporarily enables only the gate's implemented operations in memory, restoring their release flags even after an exception. Qualification never enables operations and still checks release evidence.

Capture is immutable for each selected case. Existing fixtures for other cases do not block capture. To check a retained fixture, use a fresh destination outside the repository; never overwrite a reviewed fixture. Reports and assistant conversation storage also belong outside the repository. Capture does not construct a hosted client.

```sh
# Use the disposable PostgreSQL environment supplied by the build.
PYTHONDONTWRITEBYTECODE=1 ERPCLAW_HOME=/tmp/gate-capture-home .venv/bin/python -m erpclaw_cli.evals.gate \
  --capture --case invoice_stock_from_stocked_warehouse \
  --fixtures /tmp/gate-fresh-invoice --reports /tmp/gate-capture-reports
# For a case without a fixture, omit --fixtures to publish only after its public check passes.
```

Do not rerun a failed case. Investigate it, report the exact public key line and observation, and stop that case. Never alter a key, captured observation or engine code to obtain a match. INCONCLUSIVE is never a pass. The capture exit codes are 0 for all PASS, 1 for FAIL, and 2 for INCONCLUSIVE or unavailable operations.

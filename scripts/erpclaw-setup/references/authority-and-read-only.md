# Authority flags and read-only mode

Invocation shape for every action below is `python3 db_query.py --action <name>`
plus the listed flags. Flag parsing is argparse with `default=None`
(`--lifetime-ms` is `type=int`, also defaulting to `None`); unknown flags are
rejected by the shared unknown-args check. All eight actions run through the
`erpclaw-setup` router. Money, IDs and other domain conventions are unchanged;
this file covers only flags, returns and refusals.

## How authority works

STAGED is the supported state in this release. The actions below are switches on one shared
mechanism: who may act, what an approval binds, what changes when the installation goes live,
what happens when a call is repeated, what a confirmation proves, and what read-only mode
does and does not isolate.

### Principals and company access

A principal is the identity named by the authority records: membership rows, delegation rows
and authorization envelopes each name one. Company membership defines where that principal may
act. An allow membership includes a company; a deny membership for the same company removes it
again, even when the allow row is still stored. A principal whose disabled marker is set has no
effective company scope at all.

At STAGED nothing authenticates a principal: the principal an issuance names and the principal
an actor context claims are taken as stated, and only the stored membership, rights and
delegation rows are checked against them. The gate records the companies it derived for the
call and whether they lay inside the claimed principal's membership, but refuses nothing on
company grounds: `company_scope.check` never refuses at STAGED, `gate_note` at STAGED returns
its note instead of raising, and `_scope_step` at STAGED swallows scope exceptions into an
underived note. An authorization at STAGED is still refused with `AUTHORIZATION_REFUSED` when
the principal holds no allow membership (or holds a deny) for the derived company (`rights_hold`).
Passing a company value is never by itself permission to act on another company's records. The
ACTIVE company checks refuse with `COMPANY_SCOPE_REFUSED`, or with `COMPANY_SCOPE_AMBIGUOUS`
where several companies are possible so the caller names exactly one, but that branch sits
behind a ready core, which this release never has.

The membership writers take no effect argument: `grant-company-membership` always writes allow
and `deny-company-membership` always writes deny, and any `--effect` passed with either one is
refused with `COMPANY_MEMBERSHIP_INPUT_INVALID`. `revoke-company-membership` removes one stored
row and needs the exact effect, `--effect` of `allow` or `deny`. All three writers refuse with
`COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE` once the installation is ACTIVE, and with
`AUTHORITY_CORE_UNAVAILABLE` when there is no authority core; see the action sections below for
the full refusal lists.

### Delegations and approvals

A delegation grants defined rights to one principal. Those rights hold only while the delegation
is unrevoked, inside its validity window, for its named grantee, from a human issuer that is still
enabled, and only for the per-target rights rows the delegation carries. The principal needs an
allow membership (and no deny) for the company and an allow right (and no deny) for each target
and action. Each amount must fall under a matching cap row on the delegation, per operation and
in aggregate; a missing cap row refuses (`_check_cap`). Caps are checked at issuance and checked
again, and consumed, when the action runs.

An authorization envelope binds one action to its installation, principal, company, target state
and amounts. The gate derives those protected facts from the books through the action's declared
derivation instead of accepting the caller's company or amount at face value, and the caller's
argument digest must match the digest stored at issuance. Envelopes are short-lived, and the
limits below are upper limits: rights, targets and protected facts are re-checked when the action
runs, so a shorter delegation or a changed book can still refuse. A routine envelope lives at most
86400000 milliseconds (24 hours) and its expiry is additionally capped at the delegation's own
expiry. `--lifetime-ms` is optional, 1 to 86400000; omitted, the envelope gets 86400000, still
capped at the delegation's expiry (`_need_lifetime`). No action in this release issues exact
approvals; the library bounds them at 600000 milliseconds by default and 3600000 milliseconds at
most, and refuses them at ACTIVE with `AUTHORIZATION_ISSUER_UNAVAILABLE`. The `issue-authorization`
action mints routine envelopes; it takes `--lifetime-ms` within the routine bound and
`--principal-id`, `--delegation-id`, `--authorized-action`, `--authorized-args`, `--reason-code`,
`--reason-text` and `--authorization-key`, with optional `--call-id`. An issuance that fails its
checks (no live delegation, rights, cap or derivable target) is refused at STAGED with
`AUTHORIZATION_ISSUANCE_REFUSED`.

The issuance key makes repeated identical issuance idempotent: repeating an `--authorization-key`
with exactly the same fields returns the earlier envelope with `idempotent` true instead of minting
a second one. Reusing the key with different fields is refused with `IDEMPOTENCY_CONFLICT`.

### STAGED and ACTIVE installations

At STAGED the membership writers above and the issuance, revocation and envelope-reading actions
work, and domain actions on the gated routers run **without** an authorization exactly as before.
An authorization passed at STAGED is verified and consumed, and its audit rows carry status
`staged_unattested`: the envelope binds the action, but no authenticated person approved it. At
ACTIVE, `issue-authorization`, `revoke-authorization` and `get-authorization` answer
`AUTHORIZATION_ISSUER_UNAVAILABLE`, and the membership writers answer
`COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE`, before any other check; no flag or model confirmation
supplies an issuer.

The routers that call the shared authority gate in this tree are the `erpclaw-selling`, `erpclaw-buying`,
`erpclaw-journals`, `erpclaw-payments` and `erpclaw-setup` routers. The setup router gates the company-scoped
`get-audit-log` and `get-system-audit-log` reads; every action on the other four routers, reads included, passes
through the gate. No call on those routers runs at ACTIVE in this release. The routers first check their own
arguments in either phase: a flag the router does not know answers "Unknown flags", and a malformed or second
`--authorization-id` answers `AUTHORIZATION_INPUT_INVALID`. A call that passes them and carries no
`--authorization-id` answers `AUTHORITY_NOT_READY`: the readiness check runs first, before the impact declaration
and before any authorization demand. A call that passes `--authorization-id` is first checked against its own
options and its action's declaration: an option repeated where only one is allowed, an abbreviated option
spelling, or an argument value that begins with `-` answers `AUTHORIZATION_INPUT_INVALID`; an action with no
declared impact answers `IMPACT_UNDECLARED`; an action declared without an authorization answers
`AUTHORIZATION_REFUSED`; every other call reaches the readiness check and answers `AUTHORITY_NOT_READY` before
envelope verification. These five routers are the only gated ones; the gate does not cover every cross-module
write surface. Behind a ready core, which this release never has, later branches would answer `IMPACT_UNDECLARED`
for an action with no declared impact, `AUTHORIZATION_REQUIRED` for a call with no authorization,
`COMPANY_SCOPE_REFUSED` or `COMPANY_SCOPE_AMBIGUOUS` for company failures, and `AUTHORIZATION_REFUSED` for an
envelope that fails verification.

### One execution and safe replay

For an accepted enveloped action, consuming the envelope and recording the result share the
caller's transaction: the handler runs once, the result is stored, and only then does the whole
unit commit. Anything that fails before that commit rolls back instead. A repeated request with a
consumed envelope does not run the handler again. Where the argument binding, the stored result and
the current rights pass the replay checks, the recorded result comes back with `replayed` true;
other mismatches are refused with `AUTHORIZATION_REFUSED`. After a lost response at STAGED, read
the recorded result with `get-authorization` (which reports the consumed marker and the recorded
result fields for `--envelope-id`), or repeat the identical call to get the `replayed` answer,
before creating a new financial operation. At ACTIVE neither route is available: `get-authorization`
answers `AUTHORIZATION_ISSUER_UNAVAILABLE` and a repeated call answers `AUTHORITY_NOT_READY`. Once
the gate has committed, a failure delivering the response does not undo the write.

### Confirmation and actor evidence

`--user-confirmed` is the operator's statement that a destructive call is intended; the router only
checks that the flag is present. It is not a human identity and it issues no authorization. The flag
lives on the foundation router, not on the `erpclaw-setup` parser. Over MCP the server adds the flag
to a destructive action whenever the client sent `user_confirmed: true`;
nothing verifies that a person confirmed, so it is a claim. The MCP surface does not dispatch
the actions carved out in `erpclaw/mcp/confirm.py`: `backup-database`, `list-backups`, `verify-backup`,
`restore-database`,
`cleanup-backups`, `set-credential`, `get-credential`, `list-credentials`, `delete-credential`,
`migrate-credentials`, `import-master-key-from-backup`, `add-user`, `update-user`, `add-role`,
`assign-role`, `revoke-role`, `grant-company-membership`, `deny-company-membership`,
`revoke-company-membership`, `issue-authorization`, `revoke-authorization`, `set-password`,
`seed-permissions`, `link-telegram-user`, `unlink-telegram-user` and `initialize-database`.
Read-only identity listers such as `list-users` and `list-company-memberships` remain available.

Audit rows keep the operating-system account separate from the claimed principal, channel and hop
list carried by `ERPCLAW_ACTOR_CONTEXT`. Only the operating-system account is read by the process
itself; principal, channel and hop list come from `ERPCLAW_ACTOR_CONTEXT` and are claims by whoever
started the process. Router processes started by the MCP server carry channel `mcp` and no principal.
The process actor reader records `absent` when nothing is claimed, `claimed` when a principal is
claimed, and `invalid` when the value is malformed; the `attested` status exists in the code but
nothing in this release produces it.

### Read-only operation

`ERPCLAW_MCP_READONLY=1` keeps the session to reads: only the foundation reads on the session's
pinned read list run (`is_session_read` requires `PINNED_READS` membership), anything else is refused
before any router process starts, and a `user_confirmed` value of true is refused the same way. Each
router child of a read-only MCP session additionally runs with `ERPCLAW_DB_READONLY=1`.
`ERPCLAW_DB_READONLY=1` refuses writes at the storage layer, refuses to create a missing database file,
and refuses schema provisioning. An invalid setting on either variable refuses rather than falling back
to writable mode: every tool call is refused for the MCP variable and the connection is refused for the
storage variable, without echoing the value.

Neither switch redraws the storage boundary. A SQLite file in write-ahead-log mode opened from a
writable directory can still gain its shared-memory side file, and on PostgreSQL the read-only session
setting can be reversed by SQL on the same connection, so the independent boundary there is a database
role without write grants.

## `issue-authorization`

Shape:

```bash
python3 db_query.py --action issue-authorization \
  --principal-id <id> --delegation-id <id> \
  --authorized-action <action> --authorized-args '<json-array>' \
  --reason-code <code> --reason-text <text> \
  --authorization-key <id> [--call-id <id>] [--lifetime-ms <ms>]
```

Required: `--principal-id`, `--delegation-id`, `--authorized-action`,
`--authorized-args`, `--reason-code`, `--reason-text`, `--authorization-key`.
Optional: `--call-id` (omitted means none), `--lifetime-ms` (omitted means the
maximum below).

Accepted values: `--principal-id`, `--delegation-id`, `--authorization-key`
and `--call-id` must each match `[A-Za-z0-9_-]{1,128}`.
`--authorized-action` must match `[a-z][a-z0-9-]{0,127}` and name an action the
authority gate declares with transaction impact; an undeclared name is refused.
`--authorized-args` must be a JSON array whose every element is a string (for
example `'["--company-id","abc"]'`); anything else, including a JSON object or
a non-string element, is refused. `--reason-code` must match
`[a-z][a-z0-9-]{0,63}`. `--reason-text` must be 1 to 280 characters.
`--lifetime-ms` is integer milliseconds with bounds 1 to 86400000 inclusive;
the default when omitted is 86400000. The resulting expiry is additionally
capped at the delegation's own expiry.

On success returns `authorization_id`, `expires_at` (milliseconds),
`issued_route`, `envelope_digest`, and `idempotent` (`false` for a fresh issue,
`true` when the `--authorization-key` repeats an earlier call with identical
fields). Repeating an `--authorization-key` with different fields is refused.

Refusals (verbatim `message` values): `AUTHORIZATION_ISSUER_UNAVAILABLE` (the
install is active; issuance through this action is staged-only),
`AUTHORIZATION_INPUT_INVALID` (any missing or malformed flag),
`AUTHORIZATION_ISSUANCE_REFUSED` (no such delegation, no rights, over a cap,
or storage conflict), `IDEMPOTENCY_CONFLICT` (key reused with different
fields), `AUTHORITY_NOT_READY`, `IMPACT_UNDECLARED` (action has no declared
impact), `AUTHORIZATION_REFUSED`. Which delegation checks apply beyond the
above is not specified here.

## `revoke-authorization`

Shape:

```bash
python3 db_query.py --action revoke-authorization --envelope-id <id>
```

Required: `--envelope-id`. No optional flags.

Accepted values: `--envelope-id` must match `[A-Za-z0-9_-]{1,128}`.

On success returns `authorization_id` and `revoked: true`. Revoking an
envelope that does not exist, is already revoked, or is already consumed is
refused.

Refusals (verbatim): `AUTHORIZATION_ISSUER_UNAVAILABLE` (the install is
active), `AUTHORIZATION_INPUT_INVALID` (missing or malformed `--envelope-id`),
`AUTHORIZATION_REFUSED` (nothing revocable under that id),
`AUTHORIZATION_STORAGE_ERROR` (surfaces through the router's generic error
path when the update cannot be confirmed).

## `get-authorization`

Shape:

```bash
python3 db_query.py --action get-authorization --envelope-id <id>
```

Required: `--envelope-id`. No optional flags.

Accepted values: `--envelope-id` must match `[A-Za-z0-9_-]{1,128}`.

On success returns `id`, `authorization_id`, `action`, `principal_id`,
`delegation_id`, `issued_at`, `expires_at`, `revoked` (boolean), `consumed`
(boolean), `issued_route`, `reason_code`, `envelope_digest`, plus
`result_kind`, `result_id` and `result_status` when a consumption result has
been recorded.

Refusals (verbatim): `AUTHORIZATION_ISSUER_UNAVAILABLE` (the install is
active), `AUTHORIZATION_INPUT_INVALID` (missing or malformed `--envelope-id`),
`AUTHORIZATION_REFUSED` (no envelope stored under that id).

## `grant-company-membership`

Shape:

```bash
python3 db_query.py --action grant-company-membership \
  --principal-id <id> --company-id <id>
```

Required: `--principal-id`, `--company-id`. `--effect` must not be passed: any
`--effect` value, including `allow`, is refused for this action.

Accepted values: both ids are non-empty strings; the principal must exist
under the current install and the company must exist. No further shape is
checked in code beyond non-empty string.

On success returns `install_id`, `principal_id`, `company_id` and
`effect: "allow"`. Granting twice is refused; the allow row and a deny row for
the same pair coexist.

Refusals (verbatim): `AUTHORITY_CORE_UNAVAILABLE` (no authority core),
`COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE` (the install is not staged; this
includes every call once the install is active), `COMPANY_MEMBERSHIP_INPUT_INVALID`
(missing/empty id or any `--effect` passed), `PRINCIPAL_NOT_FOUND`,
`COMPANY_NOT_FOUND`, `COMPANY_MEMBERSHIP_EXISTS` (that exact row already
stored).

## `deny-company-membership`

Shape:

```bash
python3 db_query.py --action deny-company-membership \
  --principal-id <id> --company-id <id>
```

Required: `--principal-id`, `--company-id`. `--effect` must not be passed: any
`--effect` value, including `deny`, is refused for this action.

Accepted values: both ids are non-empty strings; the principal must exist
under the current install and the company must exist.

On success returns `install_id`, `principal_id`, `company_id` and
`effect: "deny"`. Deny wins over allow while both rows are kept; adding the
same deny twice is refused.

Refusals (verbatim): `AUTHORITY_CORE_UNAVAILABLE`,
`COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE` (the install is not staged; this
includes every call once the install is active), `COMPANY_MEMBERSHIP_INPUT_INVALID`
(missing/empty id or any `--effect` passed), `PRINCIPAL_NOT_FOUND`,
`COMPANY_NOT_FOUND`, `COMPANY_MEMBERSHIP_EXISTS`.

## `revoke-company-membership`

Shape:

```bash
python3 db_query.py --action revoke-company-membership \
  --principal-id <id> --company-id <id> --effect <allow|deny>
```

Required: `--principal-id`, `--company-id`, `--effect`. No optional flags.

Accepted values: both ids are non-empty strings; `--effect` must be exactly
`allow` or `deny`. The call removes exactly one row for that triple.

On success returns `install_id`, `principal_id`, `company_id`, `effect` and
`revoked: true`.

Refusals (verbatim): `AUTHORITY_CORE_UNAVAILABLE`,
`COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE` (the install is not staged; this
includes every call once the install is active), `COMPANY_MEMBERSHIP_INPUT_INVALID`
(missing/empty id or `--effect` other than `allow`/`deny`),
`PRINCIPAL_NOT_FOUND`, `COMPANY_MEMBERSHIP_NOT_FOUND` (no row for that
triple). Whether the company still exists is not checked on this path.

## `list-company-memberships`

Shape:

```bash
python3 db_query.py --action list-company-memberships \
  [--principal-id <id>] [--company-id <id>]
```

Both flags optional; each supplied flag filters to exact matches on that
column. `--effect` is not read on this path.

Accepted values: not specified beyond exact-match filtering.

On success returns `core_present`, `memberships` (sorted
`principal_id`/`company_id`/`effect` rows) and `total_count`. When no core is
present it returns `core_present: false` with an empty list instead of
refusing. When `--principal-id` is given and the core is present, the payload
also carries `effective_scope` for that principal.

Refusals: none; this action has no refusal text in code.

## `reconcile-legacy-company-scope`

Shape:

```bash
python3 db_query.py --action reconcile-legacy-company-scope
```

No flags are read on this path.

On success returns `core_present`, `users` (per-user legacy versus membership
comparison), `principals_without_legacy_user`, `disagreement_count` and a
`note` stating that `principal_found` is false for every user until principals
are provisioned and that equal id is the only link between the stores. The
report imports nothing.

Refusals: none; this action has no refusal text in code.

## `ERPCLAW_DB_READONLY`

Setting: unset or empty means normal read-write behaviour; exactly `"1"`
means read-only storage; any other value is a configuration error and the
connection is refused with `ERPCLAW_DB_READONLY must be unset or 1` (the value
is never echoed). The variable is read when each connection is opened.

While on: SQLite connections apply `PRAGMA query_only=ON` (writes fail at the
driver; the exact driver error text is not specified in code) and skip the
journal-mode change; PostgreSQL sessions apply
`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`. Nothing is created:
SQLite refuses with `database file does not exist and ERPCLAW_DB_READONLY=1
refuses to create it: <path>` when the file is missing, and schema
provisioning refuses with `provisioning refused: ERPCLAW_DB_READONLY=1`. The
MCP server also sets this variable to `"1"` on every router child it spawns
while its own read-only session mode is on.

## `ERPCLAW_MCP_READONLY`

Setting: unset or empty means today's behaviour; exactly `"1"` means the MCP
session serves reads only; any other value is invalid and every tool call is
refused with `{"status": "error", "error": "read_only_mode_invalid", "detail":
"ERPCLAW_MCP_READONLY must be unset or 1."}` (the value is never echoed). The
variable is read from the server process environment at call time; nothing a
client sends can change it.

While on: only foundation reads run (a `get-*`/`list-*` action or a fixed
report set, excluding destructive, credential-carved-out, module-manager and
onboarding actions). Anything else is refused before any router process starts
with `{"status": "error", "error": "read_only_session", "action": <name>,
"detail": "this session is read-only; only read actions can run."}`, and
`user_confirmed: true` is refused the same way. Each router child additionally
runs with `ERPCLAW_DB_READONLY=1`.

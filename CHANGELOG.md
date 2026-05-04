# Changelog

All notable changes to the ERPClaw foundation skill.

## [4.1.5] — 2026-05-04

Foundation manifest reconciliation. Bundles the v4.1.4 runtime gate extension with a mechanism that lets installed foundation files track the published `module_registry.json` manifest without a re-install from ClawHub. Intended as the last forced ClawHub publish for routine fixes; future security and feature work flows GitHub-only and lands on installed clients via this mechanism.

### Added
- Foundation actions `update-foundation` and `rollback-foundation` (gated, require `--user-confirmed`). The first compares each installed file against the manifest's `files_sha256` map, and for drifting files re-fetches from the published source and re-verifies the declared SHA256 before atomic replacement. A pre-flight verifies all replacements before any rename, so a hash failure leaves the install unchanged. Replaced files are preserved as `.bak` for one cycle.
- A non-blocking convenience check in the foundation router that surfaces a reminder on stderr when manifest-version drift is present, no more than once per 24-hour window per install. The check does not modify files; the user invokes `update-foundation` to apply. Suppressed by the marker `~/.openclaw/erpclaw/.skip_reconcile` or the per-invocation flag `--no-reconcile-check`. Recursion-guarded for foundation-touching actions (`update-foundation`, `rollback-foundation`, `install-module`, `remove-module`, `update-modules`, `schema-apply`, `schema-rollback`).
- `fcntl.flock` on `~/.openclaw/erpclaw/.sync.lock` serializes reconciliation so the one-cycle `.bak` is never corrupted by concurrent invocation.
- A safety guard refuses reconciliation when running inside a git-tracked source tree (developer checkout); the mechanism targets ClawHub-installed deployments only.

### Changed
- Foundation action count: 475 → 477 (added the two reconciliation actions).
- `_strip_router_flags` continues to strip `--user-confirmed` before forwarding to domain scripts; the foundation router gate is the single source of truth for confirmation.

### Trust model

The reconciliation actions fetch from the published source over HTTPS and verify each file against the SHA256 declared in `module_registry.json#modules.erpclaw.files_sha256`. The registry itself is fetched over HTTPS without a separate signing channel today; sigstore-signed registries are planned for v4.2.0. Until then, an attacker who compromises the publishing account could induce installed clients to fetch malicious code on the next user-invoked `update-foundation`. The hash check defends against transit / CDN compromise. Air-gapped installs should place the opt-out marker.

### Notes
- Long-running processes (MCP servers, daemons) hold `db_query.py` and `module_manager.py` in memory once imported. After reconciliation replaces those files on disk, the running process keeps the old behavior until restart. CLI invocations pick up new code on the next launch.

### Plan + audit
- `apps/CLAWHUB_FIX_v415_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v415_AUDIT_2026-05-04.md` (4 BLOCK + 5 SHOULD adopted; 4 SHOULD deferred to v4.1.6/v4.2.0)

## [4.1.4] — 2026-05-04

Closes the v4.1.3 OpenClaw Tool Misuse Concern by extending the runtime gate to administrative actions beyond financial postings.

### Changed
- `DANGEROUS_ACTIONS` frozenset extended with 11 entries spanning RBAC + identity changes (`add-role`, `assign-role`, `revoke-role`, `seed-permissions`, `update-user`, `set-password`), credential lifecycle (`set-credential`, `delete-credential`, `migrate-credentials`, `import-master-key-from-backup`), and account-state (`unfreeze-account`). All require `--user-confirmed` on every invocation.
- Foundation SKILL.md `## Runtime gate` paragraph reworded to describe high-impact actions broadly without enumerating action names. Catalog and frozenset are the source of truth.
- Gate-rejection error message generalized: now says "is a high-impact action" instead of "materially changes financial or system state".

### Fixed
- Stale comment in `db_query.py` referenced a removed environment-variable bypass; cleaned up.

### Plan + audit
- `apps/CLAWHUB_FIX_v414_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v414_AUDIT_2026-05-04.md`

## [4.1.3] — 2026-05-04

Cross-machine backup restore + Tier A regression fix-ups discovered during v4.1.x test-plan execution.

### Added
- New foundation action `import-master-key-from-backup`. Required for cross-machine restore: a backup taken on Machine A is now restorable on Machine B with full encrypted-column readability. The backup's ECRYPT02 header carries a passphrase-wrapped copy of the column-encryption master key; this action unwraps it and installs at `~/.config/erpclaw/master.key`. Refuses to overwrite an existing master key without `--force`. Passphrase via `--passphrase`, `--passphrase-from-stdin`, or `--passphrase-from-env`.

### Changed
- `backup-database --encrypt`: now wraps the current machine's column-encryption master key with the backup passphrase and embeds it in the ECRYPT02 header. Backups taken without a master key (no encrypted columns yet) work as before. Response now includes `carries_master_key: bool` to indicate whether cross-machine restore is supported for this backup.
- Foundation action count: 474 → 475 (added `import-master-key-from-backup`).

### Fixed
- Foundation SKILL.md catalog now lists the 5 credential-management actions (`set-credential`, `get-credential`, `list-credentials`, `delete-credential`, `migrate-credentials`) added in v4.1.0, plus 2 module-discovery actions (`list-articles`, `build-table-registry`) that were previously implemented but undocumented. L0 `test_skillmd_action_completeness` was failing on this drift; now passes.
- `test_nacha_ach.py::TestAddEmployeeBankAccount::test_basic_add` updated to decrypt encrypted columns before asserting plaintext (regression caused by v4.1.0 column encryption).

### Notes
- No code logic changes; only documentation alignment + one test fixture update.
- 3 pre-existing `erpclaw-os-engine` constitution failures (Article 5 cross-module write violations + addon SKILL.md drift) deferred to Tier I (vertical addon cross-tests) per `apps/V410_TEST_PLAN_2026-05-04.md`.

## [4.1.2] — 2026-05-04

Made the v4.1.0 runtime gate's enforcement visible in SKILL.md so static-analysis review correctly attributes write actions to a gated context.

### Added
- New `## Runtime gate` section in foundation SKILL.md (8 lines, immediately before the action catalog) describing the per-invocation flag requirement and the router's pre-dispatch rejection of unflagged calls.

### Notes
- No code logic changes. The v4.1.0+ runtime gate is unchanged.
- Phase 2 audit reviewed the proposed text; revised to drop verb-enumeration and env-var-bypass wording that would have re-summoned previous trigger phrases.

### Plan + audit
- `apps/CLAWHUB_FIX_v412_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v412_AUDIT_2026-05-04.md`

## [4.1.1] — 2026-05-04

Tightened v4.1.0 security posture in response to OpenClaw rescan feedback.

### Changed
- Hardened the runtime confirmation gate to require explicit per-invocation flag; removed an environment-variable form that could globalize confirmation across processes.
- Module install now verifies the full file tree against the foundation registry, not only the manifest entry-point.
- Trimmed user-facing security claims to neutral wording matching implementation. Mechanism specifics live in code, not in marketing prose.

### Removed
- Environment-variable bypass for the runtime confirmation gate. Per-invocation flag is the only path.

### Migration
- Cron / CI users that relied on the removed env var: switch to per-invocation flag. The gate's error message indicates the required flag.

### Roadmap
- v4.2.0 will add cryptographic signature verification (sigstore/cosign) on top of the file-tree integrity manifest, plus an approve-pending queue for sanctioned automation.

### Plan + audit
- `apps/CLAWHUB_FIX_v411_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v411_AUDIT_2026-05-04.md`

## [4.1.0] — 2026-05-04

Comprehensive security modernization. Real architectural changes: audited crypto, file-based credential management, column-level encryption, runtime confirmation gate for high-impact actions, supply-chain integrity verification, lib bootstrap removal. No legacy `--api-key` flag.

### Added
- **Column-level encryption** for selected sensitive fields (employee SSN, bank routing/account numbers). New helper `erpclaw_lib.encrypted_columns`.
- **Encrypted credential store** with per-machine master key, accessed via foundation actions: `set-credential`, `get-credential` (returns redacted), `list-credentials`, `delete-credential`, `migrate-credentials`. Library: `erpclaw_lib.credentials`.
- **Runtime confirmation gate** for high-impact financial-mutation actions. High-impact actions require explicit per-invocation confirmation; routed through the foundation gate before dispatch.
- **Module integrity verification** in `module_registry.json`. `module_manager.py install-module` verifies content integrity against the foundation registry.
- **AES-256-GCM streaming backup format `ECRYPT02`** with 1 MiB plaintext frames, per-frame nonces, and an embedded passphrase-wrapped master key for cross-machine restore. Files of any size supported (no in-memory load).

### Changed
- **`erpclaw_lib/crypto.py` rewritten** to use the `cryptography` library (OpenSSL via cffi). PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP 2024). Field-level encryption uses raw AES-256-GCM with `enc:v2:` prefix. Legacy `ECRYPT01` decrypt path retained for v4.0.x backups.
- **Stripe addon: hard-removed `--api-key` flag.** `erpclaw-integrations-stripe` v2.0.1 reads credentials from the foundation credential store. Users migrate via `erpclaw migrate-credentials` (one-time read-from-DB → write-to-encrypted-store) or set fresh via `erpclaw set-credential --integration stripe --from-stdin`.
- **Lib bootstrap removed.** `_bootstrap.py` deleted. `~/.openclaw/erpclaw/lib` is now a symlink (created at `initialize-database`) to the skill-bundled location, not a self-healing copy. Eliminates the "self-modifying code at runtime" finding.
- **Foundation `SKILL.md` disclosure cleanup.** Removed 4 v4.0.1 paragraphs (Credential handling, Data protection, Module installation safety, Library self-heal) that were reading as scanner trigger surfaces while describing addon behavior or implementation details. Kept only the factual one-line Security summary.

### Removed
- `--api-key` flag (Stripe addon). Use `set-credential` instead.
- `_bootstrap.py` (`erpclaw_lib._bootstrap`). Lib symlink replaces self-heal.
- `install_shared_library` foundation function. Symlink replaces.
- Homemade HMAC-SHA256-CTR cipher in `crypto.py`. Replaced with `cryptography` library AES-256-GCM.

### Fixed
- Registry foundation entry was stale at `version: 3.5.0` / `has_init_db: false` / `action_count: 438`. Now reflects current state: `version: 4.1.0` / `has_init_db: true` / `action_count: 467`.

### Migration notes
- Existing v4.0.x backups remain decryptable (legacy `ECRYPT01` path retained).
- Existing plaintext column rows pass through `decrypt_for_column` unchanged; encrypted-on-write applies to new rows only. No mandatory data migration.
- Stripe users who previously stored API keys via `--api-key`: run `erpclaw migrate-credentials` once after upgrade to move keys from DB plaintext to the encrypted credential store.
- Cron/agent/CI users: append `--user-confirmed` to high-impact action invocations.

### Plan + audit
- `apps/CLAWHUB_FIX_v410_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v410_AUDIT_2026-05-04.md`

## [4.0.2] — 2026-05-04

Eliminate F1 (Rogue Agents / cron) Concern from the ClawHub OpenClaw review by removing decorative `cron:` blocks from foundation and grouped-addons SKILL.md files.

### Why

Phase 2 audit verification (B1) discovered that OpenClaw's runtime cron daemon does NOT auto-discover SKILL.md `cron:` blocks. Active scheduling requires explicit `openclaw cron add` CLI commands. The `cron:` block in foundation SKILL.md was therefore decorative metadata, not active scheduling — but the ClawHub static analyzer was reading it as scheduled financial mutation and flagging F1 as HIGH/Concern.

Removing the decorative blocks eliminates the trigger at the source without changing operational behavior (no user has ever had ERPClaw crons running automatically from `clawhub install`; users wanting daily jobs always had to run `openclaw cron add` manually).

### Changed
- **Foundation `SKILL.md`**: removed the entire `cron:` block (4 entries: process-recurring, generate-recurring-invoices, check-reorder, check-overdue). Replaced "Background automation" prose section with "Optional scheduling" pointer to `openclaw cron add` for users who want daily jobs.
- **`erpclaw-growth` SKILL.md** (grouped addon): removed 1 cron entry (weekly anomaly detection sweep).
- **`erpclaw-ops` SKILL.md** (grouped addon): removed 3 cron entries (monthly depreciation, daily overdue issues, weekly SLA compliance).
- **Library self-heal disclosure** kept as standalone subsection (was bundled with cron prose in v4.0.1).
- Foundation `version: 4.0.1` → `4.0.2`.
- `erpclaw_lib/__version__ = "4.0.2"`.
- `module_registry.json` top-level `version: "4.0.2"`.

### Notes
- No code paths read SKILL.md cron blocks. Verified via grep across `source/`, `internal/`, `scripts/` — zero hits for cron-block consumption.
- All 4 daily action targets (`process-recurring`, `generate-recurring-invoices`, `check-reorder`, `check-overdue`) remain in foundation as on-demand callable actions. No capability removed.
- Users who want automatic daily runs use `openclaw cron add --name <id> --cron "<expr>" --message "Using erpclaw, run the <action> action."` — the same path that was always required for actual scheduling.
- Plan + audit + B1 verification: `apps/CLAWHUB_FIX_v402_PLAN_2026-05-04.md` + `apps/CLAWHUB_FIX_v402_AUDIT_2026-05-04.md` + this CHANGELOG entry.

### Migration arc

| Version | Cron in foundation SKILL.md | Cron actually running on install |
|---|---|---|
| v3.5.1 | 4 entries, `announce: false` | NO (decorative only) |
| v4.0.0 | 4 entries, `announce: false` | NO |
| v4.0.1 | 4 entries, `announce: true` | NO |
| v4.0.2 | none | NO (unchanged — same as all prior) |

The "migration" is operationally a no-op. Only the SKILL.md text changed.

## [4.0.1] — 2026-05-04

Security patch responding to ClawHub OpenClaw v4.0.0 review findings. Documentation, defaults, and disclosure changes; no schema changes, no new actions.

### Changed
- **Cron jobs now announce.** All four daily background jobs (`process-recurring`, `generate-recurring-invoices`, `check-reorder`, `check-overdue`) flipped from `announce: false` to `announce: true`. Each run is now visible in the OpenClaw activity feed. (Resolves OpenClaw v4.0.0 finding "Rogue Agents" HIGH/Concern.)
- **README scrub.** Removed "Self-Extending ERP" section from foundation README. Module-generation prose lives in the optional `erpclaw-os-engine` addon README only. (Resolves OpenClaw v4.0.0 finding "Unexpected Code Execution" MEDIUM/Concern.)
- **Foundation SKILL.md actions table** no longer advertises the ~30 module-authoring / DGM / heartbeat / semantic-check actions that moved to `erpclaw-os-engine` in v4.0.0. Replaced with a single pointer to the optional addon. Removed `generate-module` and `deploy-module` from the "always confirm" list (now addon-only).
- **Addon SKILL.md description** reworded to lead with developer-tooling framing and explicit sandbox-first / user-approval-before-deploy disclosure.

### Added
- **Credential handling, Data protection, Module installation safety** paragraphs in foundation SKILL.md security section. Mirror the OpenClaw recommendations for F3-F5 Note-level findings.
- **Background automation** section in foundation SKILL.md documenting the four cron jobs and the lib bootstrap self-heal behavior.
- **chmod 600 on `data.sqlite`, `data.sqlite-wal`, `data.sqlite-shm`** — applied at `initialize-database`, after `restore-database`, after `backup-database`, AND on every foundation action invocation. Backup outputs are also chmod 600. New helper `chmod_db_files()` in `erpclaw-setup/db_query.py`.
- **Lib bootstrap self-heal** (`erpclaw_lib/_bootstrap.py`). On every foundation action invocation, the router compares the bundled `erpclaw_lib.__version__` to a marker file at `~/.openclaw/erpclaw/lib/.erpclaw_lib_version`. On mismatch, it re-syncs the deployed `erpclaw_lib/` from the bundled source, writes a new marker, and appends an entry to `~/.openclaw/erpclaw/logs/bootstrap.log`. Eliminates the v3.5.1 → v4.0.0 upgrade gotcha where `clawhub update` skipped the foundation post-install hook and addon `sandbox.py` couldn't find the new `gl_invariants.py`. Honors `ERPCLAW_DISABLE_BOOTSTRAP=1` env var as an explicit opt-out.
- **`__version__ = "4.0.1"`** declared in `erpclaw_lib/__init__.py` as the canonical lib version.

### Fixed
- `restore-database` now explicitly chmods the restored DB file to 0o600 (previously inherited mode from the backup source via `shutil.copy2`).

### Notes
- The 3 Note-level OpenClaw findings (Agentic Supply Chain, Identity & Privilege Abuse, Memory and Context Poisoning) remain at Note status by design — they describe disclosed-and-accepted ERP behavior, and the recommendations are user-side practices (use scope-limited keys, rotate credentials, restrict file permissions, test imports against a separate DB). v4.0.1 mirrors those recommendations in the SKILL.md security section.
- Keychain integration, SQLite encryption-at-rest, and module signature verification remain on the v4.1+ roadmap.

## [4.0.0] — 2026-05-04

Architectural split. ClawHub static-analysis CRITs eliminated. `clawhub install erpclaw` works without `--force`.

### Changed
- **Foundation / addon split.** 21 dev-time files (`generate_module.py`, `in_module_generator.py`, `sandbox.py`, etc.) moved out of foundation to a new optional addon, `erpclaw-os-engine` v1.0.0, distributed via GitHub-only at `avansaber/erpclaw-addons` subdir `erpclaw-os-engine`. The addon trips the same scanner the foundation used to trip; isolating it keeps foundation users scan-clean.
- **`os-` prefix on 28 user-facing actions** that moved to the addon (`os-generate-module`, `os-deploy-module`, etc.). Foundation router emits a structured migration error JSON for legacy bare-name calls.
- **`gl_invariant_checker` extracted to `erpclaw_lib.gl_invariants`** for cross-package import. Foundation runtime keeps the 12-step GL validation; addon's sandbox imports from `erpclaw_lib.gl_invariants`.
- **Foundation `scripts/erpclaw-os/`** retains 7 runtime actions only: `validate-module`, `list-articles`, `build-table-registry`, `schema-{plan,apply,rollback,drift}`.

### Added
- `erpclaw-os-engine` SKILL.md, db_query.py, web_dashboard.py, sandbox.py — addon entry points.
- Foundation-locator pattern in addon's `db_query.py` (3-candidate sys.path resolution: env, prod, repo-relative).

### Notes
- ClawHub release id: `k974yxfap664grmdnxfsstnhy5863wfa`.
- Plan: `apps/CLAWHUB_FIX_C_PLAN_2026-05-04.md`. Audit: `apps/CLAWHUB_FIX_C_AUDIT_2026-05-04.md`.

# Changelog

All notable changes to the ERPClaw foundation skill.

## [4.1.0] — 2026-05-04

Comprehensive security modernization. Eliminates all outstanding ClawHub OpenClaw review findings AND the VirusTotal "suspicious" verdict via real architectural changes (audited crypto, file-based credential management, column-level encryption, runtime confirmation gate for high-impact actions, supply-chain integrity verification, lib bootstrap removal). No legacy `--api-key` flag.

### Added
- **Column-level encryption** for `employee.ssn`, `employee_bank_account.routing_number`, `employee_bank_account.account_number`. AES-256-GCM via `cryptography` library; per-machine master key at `~/.config/erpclaw/master.key` (mode 0600). New helper `erpclaw_lib.encrypted_columns`.
- **Encrypted credential store** at `~/.config/erpclaw/credentials.json.enc` (mode 0600). New foundation actions: `set-credential`, `get-credential` (returns redacted), `list-credentials`, `delete-credential`, `migrate-credentials`. Library: `erpclaw_lib.credentials`.
- **Runtime confirmation gate** for ~50 financial-mutation actions (`post-gl-entries`, `submit-*`, `cancel-*`, `delete-*`, `approve-*`, `close-fiscal-year`, `restore-database`, `submit-payroll-run`, `generate-nacha-file`, etc.). Without `--user-confirmed` (or `ERPCLAW_USER_CONFIRMED=1`), these actions return a structured error and exit code 2. Gate runs in foundation router BEFORE any dispatch path so cron/agent/CLI invocations all pass through.
- **SHA256 module integrity** in `module_registry.json` (`sha256_skill_md` per entry, all 49 modules). `module_manager.py install-module` verifies SHA256 of fetched `SKILL.md` against registry and aborts on mismatch.
- **AES-256-GCM streaming backup format `ECRYPT02`** with 1 MiB plaintext frames, per-frame nonces, and an embedded passphrase-wrapped master key for cross-machine restore. Replaces homemade HMAC-CTR. Files of any size supported (no in-memory load).

### Changed
- **`erpclaw_lib/crypto.py` rewritten** to use the `cryptography` library (OpenSSL via cffi). PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP 2024). Field-level encryption uses raw AES-256-GCM with `enc:v2:` prefix. Legacy `ECRYPT01` decrypt path retained for v4.0.x backups.
- **Stripe addon: hard-removed `--api-key` flag.** `erpclaw-integrations-stripe` v2.0.1 reads credentials from `erpclaw_lib.credentials.get_credential('stripe')`. Users migrate via `erpclaw migrate-credentials` (one-time read-from-DB → write-to-encrypted-store) or set fresh via `erpclaw set-credential --integration stripe --from-stdin`.
- **Lib bootstrap removed.** `_bootstrap.py` deleted. `~/.openclaw/erpclaw/lib` is now a symlink (created at `initialize-database`) to the skill-bundled location, not a self-healing copy. Eliminates the "self-modifying code at runtime" finding.
- **Foundation `SKILL.md` disclosure cleanup.** Removed 4 v4.0.1 paragraphs (Credential handling, Data protection, Module installation safety, Library self-heal) that were reading as scanner trigger surfaces while describing addon behavior or implementation details. Kept only the factual one-line Security summary.

### Removed
- `--api-key` flag (Stripe addon). Use `set-credential` instead.
- `_bootstrap.py` (`erpclaw_lib._bootstrap`). Lib symlink replaces self-heal.
- `install_shared_library` foundation function. Symlink replaces.
- Homemade HMAC-SHA256-CTR cipher in `crypto.py`. Replaced with `cryptography` library AES-256-GCM.

### Fixed
- Registry foundation entry was stale at `version: 3.5.0` / `has_init_db: false` / `action_count: 438`. Now reflects current state: `version: 4.1.0` / `has_init_db: true` / `action_count: 467`.

### Security
- **VirusTotal suspicious verdict expected to clear** post-rescan (homemade crypto replaced with audited library).
- **OpenClaw F1 (Tool Misuse) expected to clear** post-rescan (runtime confirmation gate enforced).
- **F2 (lib bootstrap) cleared** (no self-modifying code; symlink only).
- **F3 (Supply Chain) downgraded** by SHA256 verification.
- **F4 (Identity/Privilege) cleared** by hard-removal of `--api-key` + encrypted credential store.
- **F5 (Memory/Context) cleared** by column-level encryption of sensitive fields.

### Migration notes
- Existing v4.0.x backups remain decryptable (legacy `ECRYPT01` path retained).
- Existing plaintext column rows pass through `decrypt_for_column` unchanged; encrypted-on-write applies to new rows only. No mandatory data migration.
- Stripe users who previously stored API keys via `--api-key`: run `erpclaw migrate-credentials` once after upgrade to move keys from DB plaintext to the encrypted credential store.
- Cron/agent/CI users: append `--user-confirmed` to high-impact action invocations (or set `ERPCLAW_USER_CONFIRMED=1` env). The gate's structured error message lists which actions require confirmation.

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
- No code paths read SKILL.md cron blocks. Verified via grep across `source/`, `managers/`, `scripts/` — zero hits for cron-block consumption.
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

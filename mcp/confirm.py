"""ADR-0018 confirm-class mapping for the MCP protocol layer (ADR-0024 sub-dec 2).

The router already gates every member of its ``DANGEROUS_ACTIONS`` frozenset
behind ``--user-confirmed``. This module lifts that same gate into the MCP
protocol so a well-behaved client must supply ``user_confirmed: true`` before
``erpclaw_action`` will dispatch a destructive action — and so destructive
actions advertise ``destructiveHint: true`` in their tool annotations.

Two hard rules carried from ADR-0018, preserved verbatim here:

1. **No blanket auto-confirm.** The server NEVER appends ``--user-confirmed``
   on its own. It passes the flag through ONLY when the client supplied
   ``user_confirmed: true`` — a genuine confirmation, not a mechanical one.
2. **Credential carve-out (ADR-0017 S0c).** A fixed set of credential / backup /
   master-key actions is not dispatchable over MCP in v1 at all.

The destructive set is read live from the router's own ``DANGEROUS_ACTIONS``
(via skill_reader.dangerous_actions) so the protocol layer and the router can
never disagree about what is gated. This module only ADDS the credential
carve-out on top.

Fail-closed: when the router gate cannot be read (RouterGateUnavailable),
confirmation is always required and dispatch must return
gate_unavailable_response() instead of executing.
"""
import os

from .skill_reader import RouterGateUnavailable, dangerous_actions

# ADR-0017 S0c credential carve-out: encrypted-credential, backup/restore, and
# master-key actions are not exposed over MCP in v1. Superset of the router's
# credential-class DANGEROUS members plus the read-side backup/credential
# listers (nothing credential-adjacent is reachable). Kept explicit (not derived)
# so the carve-out boundary is auditable in one place.
# m242 containment: extends the same auditable set to the identity mutations
# present in this input plus initialize-database (forced or unforced, it can
# reset install identity). These remain available on the direct operator CLI
# under its existing gate. Read-only get/list identity actions stay exposed.
# This is NOT human approval: a model-supplied user_confirmed=true on a
# non-carved-out destructive action retains current behavior.
CREDENTIAL_CARVE_OUT = frozenset({
    # Backup lifecycle (touches the encrypted backup + embedded master key)
    "backup-database", "list-backups", "verify-backup", "restore-database",
    "cleanup-backups",
    # Encrypted credential management (SMTP, integration tokens, etc. all flow
    # through these generic credential actions)
    "set-credential", "get-credential", "list-credentials", "delete-credential",
    "migrate-credentials",
    # Master-key lifecycle
    "import-master-key-from-backup",
    # Identity mutations (m242): user/role/password/permission writers plus
    # telegram link state. Read-only list-users/get-user/list-roles and the
    # permission check check-telegram-permission stay exposed.
    "add-user", "update-user", "add-role", "assign-role", "revoke-role",
    "grant-company-membership", "deny-company-membership",
    "revoke-company-membership",
    "issue-authorization", "revoke-authorization",
    "set-password", "seed-permissions",
    "link-telegram-user", "unlink-telegram-user",
    # Install identity reset (m242): both forced and unforced spellings refuse
    # over MCP; the direct operator CLI keeps its existing --force gate.
    "initialize-database",
})

_LAST_GATE_ERROR = None


def last_gate_error():
    """The most recent RouterGateUnavailable seen by confirmation_required."""
    return _LAST_GATE_ERROR


def is_credential_carved_out(action_name: str) -> bool:
    """True if the action must not be dispatched over MCP in v1."""
    return action_name in CREDENTIAL_CARVE_OUT


def is_destructive(action_name: str) -> bool:
    """True if the action is in the router's DANGEROUS_ACTIONS gate.

    Credential carve-out actions are excluded (they are not dispatchable at all,
    so the destructive flag is moot for them). RouterGateUnavailable propagates
    to the caller (fail closed, never fail open to False).
    """
    if is_credential_carved_out(action_name):
        return False
    return action_name in dangerous_actions()


def confirmation_required(action_name: str, user_confirmed: bool) -> bool:
    """True when the call must be REFUSED pending confirmation.

    A destructive action with ``user_confirmed`` not true is refused — the MCP
    layer returns a confirmation-request object instead of executing. When the
    router gate is unavailable the answer is fail-closed True, and the reason
    is recorded on ``_LAST_GATE_ERROR`` for dispatch to report.
    """
    global _LAST_GATE_ERROR
    try:
        return is_destructive(action_name) and not user_confirmed
    except RouterGateUnavailable as exc:
        _LAST_GATE_ERROR = exc
        return True


def confirmation_request(action_name: str) -> dict:
    """The structured confirmation-request object returned in lieu of executing.

    Mirrors the router's own gate message shape so the client/model sees a
    consistent, machine-readable refusal — never a silent execution.
    """
    return {
        "status": "confirmation_required",
        "action": action_name,
        "destructive": True,
        "message": (
            f"'{action_name}' is a destructive/high-impact action. Re-invoke "
            f"erpclaw_action with user_confirmed=true (reflecting a genuine user "
            f"confirmation) to proceed. The server will not confirm on your "
            f"behalf (ADR-0018 / ADR-0024)."
        ),
    }


def credential_refusal(action_name: str) -> dict:
    """The structured refusal for a credential-carve-out action (not in v1)."""
    return {
        "status": "error",
        "action": action_name,
        "error": (
            f"'{action_name}' is not available over MCP in v1 (credential "
            f"carve-out, ADR-0017 S0c). Use the OpenClaw or Hermes path for "
            f"credential / backup / master-key operations."
        ),
    }


def gate_unavailable_response(exc, action_name=None) -> dict:
    """The structured fail-closed error when the destructive gate is unreadable."""
    detail = getattr(exc, "reason", str(exc))
    path = getattr(exc, "path", None)
    action = action_name if action_name is not None else getattr(exc, "action", None)
    return {
        "status": "error",
        "error": "destructive_gate_unavailable",
        "detail": detail,
        "path": path,
        "action": action,
    }


# ── Read-only session mode (m261b) ───────────────────────────────────────────
# An agent session started in read-only mode can discover, describe and run
# every foundation read (any ``get-*``/``list-*`` action plus the fixed
# ``READ_REPORTS`` set) and nothing else. The mode is chosen by whoever starts
# the server, through the server process environment ``ERPCLAW_MCP_READONLY``;
# nothing a model sends can turn it on or off. Unset or empty means today's
# behaviour exactly; exactly ``"1"`` means read-only; any other value is
# invalid and refuses every tool call without echoing the value.


class ReadonlyModeInvalid(Exception):
    """``ERPCLAW_MCP_READONLY`` holds something other than unset/``""``/``"1"``."""
    pass


# The fixed report surface a read-only session may run, in addition to every
# ``get-*``/``list-*`` foundation read. Pinned verbatim by the m261b product
# rule; extend only by changing this set and its tests together.
READ_REPORTS = frozenset({
    "trial-balance",
    "general-ledger",
    "balance-sheet",
    "profit-and-loss",
    "cash-flow",
    "ar-aging",
    "ap-aging",
    "party-ledger",
    "gl-summary",
    "payment-summary",
    "tax-summary",
    "comparative-pl",
    "budget-vs-actual",
    "budget-variance",
    "dimension-balance-report",
    "multi-dim-trial-balance",
    "stock-balance",
    "stock-balance-report",
    "stock-ledger-report",
    "status",
})


# The reads a read-only session may run: exactly the pass set of the
# writes-nothing sweep (testing/readonly_sweep.py), which runs every
# is_read_action candidate against a seeded read-only snapshot and keeps the
# ones that changed no file, hit no read-only refusal and answered
# status ok at least once. Candidates that write nothing but never answered
# stay out, each with its reason in the sweep's NOT_PINNED. Canonical names and
# read aliases both appear; an alias passes the gate through its target.
# Extend only by re-running the sweep and pasting its pass set here; an L0
# test holds this set equal to the sweep's result.
PINNED_READS = frozenset({
    "accounting-adv-status",
    "ap-aging",
    "ar-aging",
    "balance-sheet",
    "billing-status",
    "budget-variance",
    "budget-vs-actual",
    "buying-status",
    "cash-flow",
    "comparative-pl",
    "dimension-balance-report",
    "general-ledger",
    "get-account",
    "get-account-balance",
    "get-amendment-history",
    "get-audit-log",
    "get-best-alternative-for-item",
    "get-billing-period",
    "get-billing-run",
    "get-blanket-order",
    "get-blanket-po",
    "get-company",
    "get-custom-field-values",
    "get-customer",
    "get-delivery-note",
    "get-employee",
    "get-employee-document",
    "get-exchange-rate",
    "get-garnishment",
    "get-item",
    "get-item-price",
    "get-journal-entry",
    "get-landed-cost-voucher",
    "get-lease",
    "get-leave-balance",
    "get-material-request",
    "get-meter",
    "get-outstanding",
    "get-packing-slip",
    "get-payment",
    "get-prepaid-balance",
    "get-projected-qty",
    "get-purchase-invoice",
    "get-purchase-order",
    "get-purchase-receipt",
    "get-quotation",
    "get-rate-plan",
    "get-recurring-template",
    "get-revenue-contract",
    "get-salary-slip",
    "get-salary-structure",
    "get-sales-invoice",
    "get-sales-order",
    "get-schema-version",
    "get-stock-balance",
    "get-stock-entry",
    "get-stock-revaluation",
    "get-supplier",
    "get-tax-template",
    "get-unallocated-payments",
    "get-user",
    "get-withholding-details",
    "gl-status",
    "gl-summary",
    "hr-status",
    "inventory-status",
    "journals-status",
    "list-account-types",
    "list-accounts",
    "list-articles",
    "list-attendance",
    "list-batches",
    "list-billing-periods",
    "list-billing-runs",
    "list-blanket-orders",
    "list-blanket-pos",
    "list-budgets",
    "list-companies",
    "list-company-memberships",
    "list-consolidation-groups",
    "list-cost-centers",
    "list-credit-notes",
    "list-currencies",
    "list-custom-fields",
    "list-customers",
    "list-delivery-notes",
    "list-departments",
    "list-designations",
    "list-dimensions",
    "list-dunning-runs",
    "list-elimination-surplus",
    "list-employee-bank-accounts",
    "list-employee-documents",
    "list-employees",
    "list-exchange-rates",
    "list-expense-claims",
    "list-fiscal-years",
    "list-garnishments",
    "list-gl-entries",
    "list-ic-transactions",
    "list-intercompany-account-maps",
    "list-intercompany-invoices",
    "list-item-alternatives",
    "list-item-groups",
    "list-item-suppliers",
    "list-item-variants",
    "list-items",
    "list-journal-entries",
    "list-landed-cost-vouchers",
    "list-leases",
    "list-leave-applications",
    "list-leave-types",
    "list-material-requests",
    "list-meter-readings",
    "list-meters",
    "list-open-advances",
    "list-packing-slips",
    "list-payment-terms",
    "list-payments",
    "list-performance-obligations",
    "list-purchase-invoices",
    "list-purchase-orders",
    "list-purchase-receipts",
    "list-putaway-rules",
    "list-quotations",
    "list-rate-plans",
    "list-recurring-bill-templates",
    "list-recurring-invoice-templates",
    "list-recurring-templates",
    "list-reservations",
    "list-revenue-contracts",
    "list-rfqs",
    "list-roles",
    "list-salary-assignments",
    "list-salary-components",
    "list-salary-slips",
    "list-salary-structures",
    "list-sales-invoices",
    "list-sales-orders",
    "list-sales-partners",
    "list-serial-numbers",
    "list-shift-assignments",
    "list-shift-types",
    "list-stock-entries",
    "list-stock-revaluations",
    "list-supplier-quotations",
    "list-suppliers",
    "list-tax-categories",
    "list-tax-rules",
    "list-tax-templates",
    "list-transfer-price-rules",
    "list-uoms",
    "list-users",
    "list-variable-considerations",
    "list-voucher-types",
    "list-warehouses",
    "multi-dim-trial-balance",
    "party-ledger",
    "payment-summary",
    "payments-status",
    "payroll-status",
    "profit-and-loss",
    "reports-status",
    "selling-status",
    "status",
    "stock-balance",
    "stock-balance-report",
    "stock-ledger-report",
    "tax-status",
    "tax-summary",
    "trial-balance",
})


def session_readonly() -> bool:
    """True when this server process runs a read-only session.

    Reads ``ERPCLAW_MCP_READONLY`` at call time: unset or ``""`` is False,
    exactly ``"1"`` is True, and anything else raises ``ReadonlyModeInvalid``.
    """
    value = os.environ.get("ERPCLAW_MCP_READONLY")
    if value is None or value == "":
        return False
    if value == "1":
        return True
    raise ReadonlyModeInvalid(
        "ERPCLAW_MCP_READONLY must be unset or 1.")


def is_read_action(name, *, dangerous, module_actions, onboarding_actions) -> bool:
    """True only when ``name`` is a foundation read and nothing else.

    A read starts with ``get-``/``list-`` or is in ``READ_REPORTS``, AND is not
    in the router's ``dangerous`` set, not credential carved out, and not in
    the module-manager or onboarding sets. A set that is ``None`` or cannot be
    tested for membership refuses: the gate never passes a check it could not
    make.
    """
    if not isinstance(name, str) or not name:
        return False
    if dangerous is None or module_actions is None or onboarding_actions is None:
        return False
    if not (name.startswith("get-") or name.startswith("list-")
            or name in READ_REPORTS):
        return False
    try:
        if name in dangerous:
            return False
    except TypeError:
        return False
    if is_credential_carved_out(name):
        return False
    try:
        if name in module_actions:
            return False
    except TypeError:
        return False
    try:
        if name in onboarding_actions:
            return False
    except TypeError:
        return False
    return True


def is_session_read(name, *, dangerous, module_actions, onboarding_actions) -> bool:
    """True only when ``name`` passes ``is_read_action`` AND is pinned.

    This is the gate a read-only session applies. ``is_read_action`` names
    the candidates; ``PINNED_READS`` keeps only the candidates the
    writes-nothing sweep proved write nothing, so a new ``get-*``/``list-*``
    action stays refused in a read-only session until the sweep is re-run and
    the pin extended.
    """
    if not is_read_action(name, dangerous=dangerous,
                          module_actions=module_actions,
                          onboarding_actions=onboarding_actions):
        return False
    return name in PINNED_READS


def refusal(action_name: str) -> dict:
    """The structured refusal for a non-read action in a read-only session."""
    return {
        "status": "error",
        "error": "read_only_session",
        "action": action_name,
        "detail": "this session is read-only; only read actions can run.",
    }


def invalid_mode() -> dict:
    """The structured refusal when ``ERPCLAW_MCP_READONLY`` is invalid.

    Never echoes the offending value.
    """
    return {
        "status": "error",
        "error": "read_only_mode_invalid",
        "detail": "ERPCLAW_MCP_READONLY must be unset or 1.",
    }

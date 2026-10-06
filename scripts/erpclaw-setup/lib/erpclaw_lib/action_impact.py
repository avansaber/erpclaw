"""Foundation action impact declarations.

Maps every routed foundation action name to a fixed declaration of its
effect class, money arguments, JSON arguments, clock- or environment-bound
arguments, and company source. A company source names one argument
(``arg:<name>``), one target row (``row:<table>``, with ``target_arg`` naming
the flag that carries its id), several target rows (``rows:<t1>,...``, with a
``target_arg`` tuple naming one flag per table), nothing (``none``), or not
yet known (``unknown``). Pure data plus a shape validator; imports
only the standard library, reads nothing, and has no run-time callers yet.
"""
import copy
import re

CLASSES = ("read", "draft", "transaction", "sensitive")

SENSITIVE = frozenset({
    "close-fiscal-year",
    "restore-database",
    "install-module",
    "rollback-foundation",
    "generate-nacha-file",
    "initialize-database",
    "remove-module",
    "schema-rollback",
    "cleanup-backups",
    "write-off-invoice",
    "legal-write-off-invoice",
    "onboard",
    "set-password",
    "add-role",
    "assign-role",
    "revoke-role",
    "seed-permissions",
    "add-user",
    "update-user",
    "link-telegram-user",
    "unlink-telegram-user",
    "set-credential",
    "delete-credential",
    "migrate-credentials",
    "import-master-key-from-backup",
    "update-modules",
    "update-foundation",
    "schema-apply",
    "migrate",
    "issue-authorization",
    "revoke-authorization",
    "grant-company-membership",
    "deny-company-membership",
    "revoke-company-membership",
})

TARGET_PROJECTIONS = frozenset({"journal-entry-state", "payment-entry-state", "sales-invoice-state", "purchase-invoice-state"})
RESULT_PROJECTIONS = frozenset({"journal-entry-result", "payment-entry-result", "sales-invoice-result", "purchase-invoice-result"})

IMPACT = {
    "add-account": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-account-type": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-batch": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-billing-adjustment": {"domain": "billing", "class": "transaction", "enveloped": True, "money": ("amount",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-blanket-order": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-blanket-po": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-budget": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:fiscal_year", "target_arg": "fiscal-year-id", "target_projection": None, "result_projection": None},
    "add-consolidation-group": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-cost-center": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-currency": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-currency-translation": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-custom-field": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-customer": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-department": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-designation": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-dimension": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-dunning-level": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-elimination-rule": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-employee": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-employee-bank-account": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-employee-document": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-exchange-rate": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-expense-claim": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-fiscal-year": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-garnishment": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-group-entity": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-holiday-list": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-ic-transaction": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-income-tax-slab": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-intercompany-account-map": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-item": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-item-alternative": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-item-attribute": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-item-group": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-item-price": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-item-supplier": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:supplier", "target_arg": "supplier-id", "target_projection": None, "result_projection": None},
    "add-item-tax-template": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "add-journal-entry": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-landed-cost-voucher": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": ("charges", "purchase-receipt-ids"), "money_json_paths": {"charges": (("*", "amount"),)}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-lease": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-leave-allocation": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-leave-application": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-leave-type": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-material-consumption": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-material-request": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-meter": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "add-meter-reading": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-overtime-policy": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-packing-slip": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:delivery_note", "target_arg": "delivery-note-id", "target_projection": None, "result_projection": None},
    "add-payment": {"domain": "payments", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-payment-terms": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-performance-obligation": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-pick-list-item": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:pick_list", "target_arg": "pick-list-id", "target_projection": None, "result_projection": None},
    "add-prepaid-credit": {"domain": "billing", "class": "transaction", "enveloped": True, "money": ("amount",), "json_args": (), "money_json_paths": {}, "bound_args": ("rate-plan-id",), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "add-price-list": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-pricing-rule": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-purchase-order": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-putaway-rule": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-quotation": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-rate-plan": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-recurring-bill-template": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-recurring-template": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-regularization-rule": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-repack-stock-entry": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-reservation": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:warehouse", "target_arg": "warehouse-id", "target_projection": None, "result_projection": None},
    "add-revenue-contract": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-rfq": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-role": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-salary-assignment": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "add-salary-component": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-salary-structure": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-sales-order": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-sales-partner": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-serial-number": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-shift-type": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-state-tax-slab": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-stock-entry": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-stock-reconciliation": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-supplier": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-supplier-quotation": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:request_for_quotation", "target_arg": "rfq-id", "target_projection": None, "result_projection": None},
    "add-tax-category": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-tax-rule": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "add-tax-template": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-tax-withholding-category": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-transfer-price-rule": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-uom": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-uom-conversion": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-usage-event": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-usage-events-batch": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "add-user": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-variable-consideration": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-vendor-bill-intake": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "add-voucher-type": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "add-warehouse": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "allocate-payment": {"domain": "payments", "class": "transaction", "enveloped": True, "money": ("allocated-amount",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": None, "result_projection": None},
    "amend-journal-entry": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": ("lines",), "money_json_paths": {"lines": (("*", "debit"), ("*", "credit"))}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": "journal-entry-state", "result_projection": "journal-entry-result"},
    "amend-sales-order": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": ("items",), "money_json_paths": {"items": (("*", "rate"),)}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "ap-aging": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "apply-advance-to-invoice": {"domain": "payments", "class": "transaction", "enveloped": True, "money": ("allocated-amount",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": None, "result_projection": None},
    "apply-attendance-regularization": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "apply-putaway-on-receipt": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_entry", "target_arg": "stock-entry-id", "target_projection": None, "result_projection": None},
    "approve-expense-claim": {"domain": "hr", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:expense_claim", "target_arg": "expense-claim-id", "target_projection": None, "result_projection": None},
    "approve-ic-transaction": {"domain": "accounting-adv", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "approve-leave": {"domain": "hr", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "ar-aging": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "assign-role": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "assign-shift": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "available-modules": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "backup-database": {"domain": "setup", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": ("backup-path",), "company_source": "none", "target_projection": None, "result_projection": None},
    "balance-sheet": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "bank-reconciliation": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "bank-account-id", "target_projection": None, "result_projection": None},
    "budget-variance": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "budget-vs-actual": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "build-table-registry": {"domain": "os", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "bulk-mark-attendance": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "calculate-lease-liability": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "id", "target_projection": None, "result_projection": None},
    "calculate-overtime": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "calculate-retro-pay": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "calculate-revenue-schedule": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_performance_obligation", "target_arg": "obligation-id", "target_projection": None, "result_projection": None},
    "calculate-rou-asset": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "id", "target_projection": None, "result_projection": None},
    "calculate-tax": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "cancel-delivery-note": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:delivery_note", "target_arg": "delivery-note-id", "target_projection": None, "result_projection": None},
    "cancel-intercompany-invoice": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "cancel-journal-entry": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": "journal-entry-state", "result_projection": "journal-entry-result"},
    "cancel-landed-cost-voucher": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:landed_cost_voucher", "target_arg": "landed-cost-voucher-id", "target_projection": None, "result_projection": None},
    "cancel-payment": {"domain": "payments", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": "payment-entry-state", "result_projection": "payment-entry-result"},
    "cancel-payroll-run": {"domain": "payroll", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payroll_run", "target_arg": "payroll-run-id", "target_projection": None, "result_projection": None},
    "cancel-pick-list": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:pick_list", "target_arg": "id", "target_projection": None, "result_projection": None},
    "cancel-purchase-invoice": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_invoice", "target_arg": "purchase-invoice-id", "target_projection": "purchase-invoice-state", "result_projection": "purchase-invoice-result"},
    "cancel-purchase-order": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_order", "target_arg": "purchase-order-id", "target_projection": None, "result_projection": None},
    "cancel-purchase-receipt": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_receipt", "target_arg": "purchase-receipt-id", "target_projection": None, "result_projection": None},
    "cancel-sales-invoice": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "sales-invoice-id", "target_projection": "sales-invoice-state", "result_projection": "sales-invoice-result"},
    "cancel-sales-order": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "cancel-stock-entry": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_entry", "target_arg": "stock-entry-id", "target_projection": None, "result_projection": None},
    "cancel-stock-revaluation": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_revaluation", "target_arg": "revaluation-id", "target_projection": None, "result_projection": None},
    "cash-flow": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "check-credit-limit": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "check-expiring-documents": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "check-gl-integrity": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "check-installation": {"domain": "meta", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "check-overdue": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "check-reorder": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "check-telegram-permission": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "classify-lease": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "id", "target_projection": None, "result_projection": None},
    "cleanup-backups": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "close-fiscal-year": {"domain": "gl", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:fiscal_year", "target_arg": "fiscal-year-id", "target_projection": None, "result_projection": None},
    "close-purchase-order": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_order", "target_arg": "purchase-order-id", "target_projection": None, "result_projection": None},
    "close-sales-order": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "comparative-pl": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "compare-supplier-quotations": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:request_for_quotation", "target_arg": "rfq-id", "target_projection": None, "result_projection": None},
    "complete-pick-list": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:pick_list", "target_arg": "id", "target_projection": None, "result_projection": None},
    "consolidation-summary": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_consolidation_group", "target_arg": "group-id", "target_projection": None, "result_projection": None},
    "consolidation-trial-balance-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_consolidation_group", "target_arg": "group-id", "target_projection": None, "result_projection": None},
    "continuous-close-readiness": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "convert-quotation-to-so": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:quotation", "target_arg": "quotation-id", "target_projection": None, "result_projection": None},
    "create-billing-period": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "create-credit-note": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "against-invoice-id", "target_projection": None, "result_projection": None},
    "create-debit-note": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_invoice", "target_arg": "against-invoice-id", "target_projection": None, "result_projection": None},
    "create-delivery-note": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "create-drop-ship-order": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "create-intercompany-invoice": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "create-intercompany-je": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "create-item-variant": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "create-payment-ledger-entry": {"domain": "payments", "class": "transaction", "enveloped": True, "money": ("amount",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "create-payroll-run": {"domain": "payroll", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "create-pick-list": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "create-po-from-blanket": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "create-po-from-material-request": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:material_request", "target_arg": "material-request-id", "target_projection": None, "result_projection": None},
    "create-po-from-so": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "create-purchase-invoice": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "create-purchase-receipt": {"domain": "buying", "class": "transaction", "enveloped": True, "money": ("subcontract-charge-rate",), "json_args": ("items",), "money_json_paths": {}, "bound_args": ("posting-date",), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "create-sales-invoice": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "create-so-from-blanket": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "create-stock-ledger-entries": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "deactivate-account-type": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "deactivate-dimension": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "deactivate-voucher-type": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "delete-credential": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "delete-journal-entry": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": None, "result_projection": None},
    "delete-payment": {"domain": "payments", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": None, "result_projection": None},
    "delete-putaway-rule": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:putaway_rule", "target_arg": "id", "target_projection": None, "result_projection": None},
    "delete-recurring-template": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:recurring_journal_template", "target_arg": "template-id", "target_projection": None, "result_projection": None},
    "delete-sales-invoice": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "sales-invoice-id", "target_projection": None, "result_projection": None},
    "delete-tax-template": {"domain": "tax", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "deny-company-membership": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "dimension-balance-report": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "duplicate-journal-entry": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": None, "result_projection": None},
    "fetch-exchange-rates": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "flux-variance-narrative": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "freeze-account": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "general-ledger": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-1099-data": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-amortization-schedule": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "lease-id", "target_projection": None, "result_projection": None},
    "generate-elimination-entries": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "generate-invoices": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "generate-item-variants": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "generate-nacha-file": {"domain": "payroll", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payroll_run", "target_arg": "payroll-run-id", "target_projection": None, "result_projection": None},
    "generate-recurring-bills": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": ("as-of-date",), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-recurring-invoices": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": ("as-of-date",), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-revenue-entries": {"domain": "accounting-adv", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_performance_obligation", "target_arg": "obligation-id", "target_projection": None, "result_projection": None},
    "generate-salary-slips": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payroll_run", "target_arg": "payroll-run-id", "target_projection": None, "result_projection": None},
    "generate-w2-data": {"domain": "payroll", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-form941-data": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "generate-form940-data": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "get-account": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "get-account-balance": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "get-amendment-history": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "get-audit-log": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-audit-checkpoint": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-authorization": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-best-alternative-for-item": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-billing-period": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:billing_period", "target_arg": "billing-period-id", "target_projection": None, "result_projection": None},
    "get-billing-run": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:billing_run", "target_arg": "run-id", "target_projection": None, "result_projection": None},
    "get-blanket-order": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "get-blanket-po": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "get-company": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "get-credential": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-custom-field-values": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "get-customer": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "get-delivery-note": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:delivery_note", "target_arg": "delivery-note-id", "target_projection": None, "result_projection": None},
    "get-employee": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "get-employee-document": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "get-exchange-rate": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-garnishment": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:wage_garnishment", "target_arg": "garnishment-id", "target_projection": None, "result_projection": None},
    "get-ic-transaction": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_ic_transaction", "target_arg": "id", "target_projection": None, "result_projection": None},
    "get-item": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "get-item-price": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-journal-entry": {"domain": "journals", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": None, "result_projection": None},
    "get-landed-cost-voucher": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:landed_cost_voucher", "target_arg": "landed-cost-voucher-id", "target_projection": None, "result_projection": None},
    "get-lease": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "id", "target_projection": None, "result_projection": None},
    "get-leave-balance": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "get-material-request": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:material_request", "target_arg": "material-request-id", "target_projection": None, "result_projection": None},
    "get-meter": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:meter", "target_arg": "meter-id", "target_projection": None, "result_projection": None},
    "get-outstanding": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "get-packing-slip": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:packing_slip", "target_arg": "packing-slip-id", "target_projection": None, "result_projection": None},
    "get-payment": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": None, "result_projection": None},
    "get-prepaid-balance": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "get-projected-qty": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:warehouse", "target_arg": "warehouse-id", "target_projection": None, "result_projection": None},
    "get-purchase-invoice": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_invoice", "target_arg": "purchase-invoice-id", "target_projection": None, "result_projection": None},
    "get-purchase-order": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_order", "target_arg": "purchase-order-id", "target_projection": None, "result_projection": None},
    "get-purchase-receipt": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_receipt", "target_arg": "purchase-receipt-id", "target_projection": None, "result_projection": None},
    "get-quotation": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:quotation", "target_arg": "quotation-id", "target_projection": None, "result_projection": None},
    "get-rate-plan": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-recurring-template": {"domain": "journals", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:recurring_journal_template", "target_arg": "template-id", "target_projection": None, "result_projection": None},
    "get-revenue-contract": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_revenue_contract", "target_arg": "id", "target_projection": None, "result_projection": None},
    "get-salary-slip": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:salary_slip", "target_arg": "salary-slip-id", "target_projection": None, "result_projection": None},
    "get-salary-structure": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:salary_structure", "target_arg": "salary-structure-id", "target_projection": None, "result_projection": None},
    "get-sales-invoice": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "sales-invoice-id", "target_projection": None, "result_projection": None},
    "get-sales-order": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "get-schema-version": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-stock-balance": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:warehouse", "target_arg": "warehouse-id", "target_projection": None, "result_projection": None},
    "get-stock-entry": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_entry", "target_arg": "stock-entry-id", "target_projection": None, "result_projection": None},
    "get-stock-revaluation": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_revaluation", "target_arg": "revaluation-id", "target_projection": None, "result_projection": None},
    "get-supplier": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:supplier", "target_arg": "supplier-id", "target_projection": None, "result_projection": None},
    "get-tax-template": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "get-unallocated-payments": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "get-user": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "get-withholding-details": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "gl-summary": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "grant-company-membership": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "ic-elimination-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "ic-reconciliation-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "import-chart-of-accounts": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "import-customers": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "import-items": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "import-master-key-from-backup": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "import-opening-balances": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "import-suppliers": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "initialize-database": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "install-guide": {"domain": "meta", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "evaluate-rule": {"domain": "meta", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "install-module": {"domain": "module-manager", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "inventory-demand-forecast": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "issue-authorization": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "lease-disclosure-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "lease-maturity-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "lease-summary": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "legal-write-off-invoice": {"domain": "legalclaw", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:legalclaw_invoice", "target_arg": "invoice-id", "target_projection": None, "result_projection": None},
    "link-billing-period-invoice": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "link-telegram-user": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-account-types": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-accounts": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-all-actions": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-articles": {"domain": "os", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-attendance": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-backups": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-batches": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-billing-periods": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-billing-runs": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-blanket-orders": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-blanket-pos": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-budgets": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-companies": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-company-memberships": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-consolidation-groups": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-cost-centers": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-credentials": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-credit-notes": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-currencies": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-custom-fields": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-customers": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-delivery-notes": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-departments": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-designations": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-dimensions": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-dunning-runs": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-elimination-entries": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-elimination-rules": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-elimination-surplus": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_consolidation_group", "target_arg": "group-id", "target_projection": None, "result_projection": None},
    "list-employee-bank-accounts": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "list-employee-documents": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "list-employees": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-exchange-rates": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-expense-claims": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-fiscal-years": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-garnishments": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-gl-entries": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-ic-transactions": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-intercompany-account-maps": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-intercompany-invoices": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-item-alternatives": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-item-groups": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-item-suppliers": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-item-variants": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-items": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-journal-entries": {"domain": "journals", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-landed-cost-voucher-anomalies": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-landed-cost-vouchers": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-leases": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-leave-applications": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-leave-types": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-material-requests": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-meter-readings": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:meter", "target_arg": "meter-id", "target_projection": None, "result_projection": None},
    "list-meters": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-modules": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-open-advances": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-packing-slips": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-payment-terms": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-payments": {"domain": "payments", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-performance-obligations": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-profiles": {"domain": "onboarding", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-purchase-invoices": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-purchase-orders": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-purchase-receipts": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-putaway-rules": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-quotations": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-rate-plans": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-recurring-bill-templates": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-recurring-templates": {"domain": "journals", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-reservations": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-revenue-contracts": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-rfqs": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-roles": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-salary-assignments": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-salary-components": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-salary-slips": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-salary-structures": {"domain": "payroll", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-sales-invoices": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-sales-orders": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-sales-partners": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-serial-numbers": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-shift-assignments": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-shift-types": {"domain": "hr", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-stock-entries": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-stock-revaluations": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-supplier-quotations": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "list-suppliers": {"domain": "buying", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-tax-categories": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-tax-rules": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-tax-templates": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-transfer-price-rules": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-uoms": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-users": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-variable-considerations": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "list-voucher-types": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "list-warehouses": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "mark-attendance": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "mark-picked": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:pick_list", "target_arg": "pick-list-id", "target_projection": None, "result_projection": None},
    "migrate": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "migrate-credentials": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "modify-contract": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_revenue_contract", "target_arg": "id", "target_projection": None, "result_projection": None},
    "module-status": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "multi-dim-trial-balance": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "next-series": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "nonprofit-statement-set": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "onboard": {"domain": "onboarding", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "onboarding-step": {"domain": "setup", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "party-ledger": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "payment-summary": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "place-customer-on-hold": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "post-gl-entries": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": ("entries",), "money_json_paths": {"entries": (("*", "debit"), ("*", "credit"))}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "post-ic-transaction": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_ic_transaction", "target_arg": "id", "target_projection": None, "result_projection": None},
    "process-recurring": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": ("as-of-date",), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "profit-and-loss": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "rate-consumption": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "rebuild-action-cache": {"domain": "module-manager", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "recognize-schedule-entry": {"domain": "accounting-adv", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_revenue_schedule", "target_arg": "id", "target_projection": None, "result_projection": None},
    "reconcile-legacy-company-scope": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "reconcile-payments": {"domain": "payments", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "record-1099-payment": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "record-lease-payment": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "lease-id", "target_projection": None, "result_projection": None},
    "record-lifecycle-event": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "record-withholding-entry": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "regenerate-skill-md": {"domain": "module-manager", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "reject-expense-claim": {"domain": "hr", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:expense_claim", "target_arg": "expense-claim-id", "target_projection": None, "result_projection": None},
    "reject-leave": {"domain": "hr", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "release-reservation": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_reservation_entry", "target_arg": "id", "target_projection": None, "result_projection": None},
    "remove-custom-field": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "remove-elimination-surplus": {"domain": "accounting-adv", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "remove-item-alternative": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "remove-module": {"domain": "module-manager", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "reopen-fiscal-year": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:fiscal_year", "target_arg": "fiscal-year-id", "target_projection": None, "result_projection": None},
    "resolve-item": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "resolve-tax-template": {"domain": "tax", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "restore-database": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "resume-billing-run": {"domain": "billing", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:billing_run", "target_arg": "run-id", "target_projection": None, "result_projection": None},
    "revalue-foreign-balances": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "revalue-stock": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": ("new-rate",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:warehouse", "target_arg": "warehouse-id", "target_projection": None, "result_projection": None},
    "revenue-recognition-summary": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "revenue-waterfall-report": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "reverse-gl-entries": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": ("posting-date",), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "reverse-stock-ledger-entries": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "revoke-authorization": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "revoke-company-membership": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "revoke-role": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "rollback-foundation": {"domain": "module-manager", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "run-billing": {"domain": "billing", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "run-consolidation": {"domain": "accounting-adv", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "run-dunning-cycle": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "run-follow-up-cycle": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "run-elimination": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "satisfy-performance-obligation": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_performance_obligation", "target_arg": "id", "target_projection": None, "result_projection": None},
    "schema-apply": {"domain": "os", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "schema-drift": {"domain": "os", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "schema-plan": {"domain": "os", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "schema-rollback": {"domain": "os", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "search-modules": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "seed-defaults": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "seed-demo-data": {"domain": "meta", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "seed-naming-series": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "seed-permissions": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "set-advance-account": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "set-credential": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "set-custom-field-value": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "set-follow-up-threshold": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "set-item-purchase-uom": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "set-password": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "setup-chart-of-accounts": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "setup-company": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "setup-web-dashboard": {"domain": "meta", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "standards-compliance-dashboard": {"domain": "accounting-adv", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "status": {"domain": "billing", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "stock-balance": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "stock-balance-report": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "stock-ledger-report": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "standard-cost-variance-report": {"domain": "inventory", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "submit-blanket-order": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "submit-blanket-po": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:blanket_order", "target_arg": "blanket-order-id", "target_projection": None, "result_projection": None},
    "submit-delivery-note": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:delivery_note", "target_arg": "delivery-note-id", "target_projection": None, "result_projection": None},
    "submit-expense-claim": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:expense_claim", "target_arg": "expense-claim-id", "target_projection": None, "result_projection": None},
    "submit-journal-entry": {"domain": "journals", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": "journal-entry-state", "result_projection": "journal-entry-result"},
    "submit-material-request": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:material_request", "target_arg": "material-request-id", "target_projection": None, "result_projection": None},
    "submit-payment": {"domain": "payments", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": "payment-entry-state", "result_projection": "payment-entry-result"},
    "submit-payroll-run": {"domain": "payroll", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payroll_run", "target_arg": "payroll-run-id", "target_projection": None, "result_projection": None},
    "submit-pick-list": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:pick_list", "target_arg": "id", "target_projection": None, "result_projection": None},
    "submit-purchase-invoice": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_invoice", "target_arg": "purchase-invoice-id", "target_projection": "purchase-invoice-state", "result_projection": "purchase-invoice-result"},
    "submit-purchase-order": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_order", "target_arg": "purchase-order-id", "target_projection": None, "result_projection": None},
    "submit-purchase-receipt": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_receipt", "target_arg": "purchase-receipt-id", "target_projection": None, "result_projection": None},
    "submit-quotation": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:quotation", "target_arg": "quotation-id", "target_projection": None, "result_projection": None},
    "submit-rfq": {"domain": "buying", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:request_for_quotation", "target_arg": "rfq-id", "target_projection": None, "result_projection": None},
    "submit-sales-invoice": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "sales-invoice-id", "target_projection": "sales-invoice-state", "result_projection": "sales-invoice-result"},
    "submit-sales-order": {"domain": "selling", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "submit-stock-entry": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_entry", "target_arg": "stock-entry-id", "target_projection": None, "result_projection": None},
    "submit-stock-reconciliation": {"domain": "inventory", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:stock_reconciliation", "target_arg": "stock-reconciliation-id", "target_projection": None, "result_projection": None},
    "sync-billing-period-status": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "tax-summary": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "trial-balance": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "tutorial": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "unfreeze-account": {"domain": "gl", "class": "transaction", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "unlink-billing-period-invoice": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "unlink-telegram-user": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-account": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:account", "target_arg": "account-id", "target_projection": None, "result_projection": None},
    "update-company": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "update-customer": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:customer", "target_arg": "customer-id", "target_projection": None, "result_projection": None},
    "update-dimension": {"domain": "gl", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-employee": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "update-employee-state-config": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:employee", "target_arg": "employee-id", "target_projection": None, "result_projection": None},
    "update-expense-claim-status": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:expense_claim", "target_arg": "expense-claim-id", "target_projection": None, "result_projection": None},
    "update-fica-config": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-foundation": {"domain": "module-manager", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-futa-suta-config": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-garnishment": {"domain": "payroll", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:wage_garnishment", "target_arg": "garnishment-id", "target_projection": None, "result_projection": None},
    "update-ic-transaction": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_ic_transaction", "target_arg": "id", "target_projection": None, "result_projection": None},
    "update-invoice-outstanding": {"domain": "selling", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-item": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-journal-entry": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:journal_entry", "target_arg": "journal-entry-id", "target_projection": None, "result_projection": None},
    "update-lease": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_lease", "target_arg": "id", "target_projection": None, "result_projection": None},
    "update-meter": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "update-modules": {"domain": "module-manager", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-payment": {"domain": "payments", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:payment_entry", "target_arg": "payment-entry-id", "target_projection": None, "result_projection": None},
    "update-performance-obligation": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_performance_obligation", "target_arg": "id", "target_projection": None, "result_projection": None},
    "update-purchase-invoice": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_invoice", "target_arg": "purchase-invoice-id", "target_projection": None, "result_projection": None},
    "update-purchase-order": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:purchase_order", "target_arg": "purchase-order-id", "target_projection": None, "result_projection": None},
    "update-putaway-rule": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:putaway_rule", "target_arg": "id", "target_projection": None, "result_projection": None},
    "update-quotation": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:quotation", "target_arg": "quotation-id", "target_projection": None, "result_projection": None},
    "update-rate-plan": {"domain": "billing", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-receipt-tolerance": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "update-recurring-bill-template": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:recurring_bill_template", "target_arg": "template-id", "target_projection": None, "result_projection": None},
    "update-recurring-template": {"domain": "journals", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
    "update-regional-settings": {"domain": "setup", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "update-revenue-contract": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_revenue_contract", "target_arg": "id", "target_projection": None, "result_projection": None},
    "update-sales-invoice": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_invoice", "target_arg": "sales-invoice-id", "target_projection": None, "result_projection": None},
    "update-sales-order": {"domain": "selling", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:sales_order", "target_arg": "sales-order-id", "target_projection": None, "result_projection": None},
    "update-schedule-amounts": {"domain": "accounting-adv", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:advacct_performance_obligation", "target_arg": "obligation-id", "target_projection": None, "result_projection": None},
    "update-shift-type": {"domain": "hr", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:shift_type", "target_arg": "shift-type-id", "target_projection": None, "result_projection": None},
    "update-supplier": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:supplier", "target_arg": "supplier-id", "target_projection": None, "result_projection": None},
    "update-tax-template": {"domain": "tax", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:tax_template", "target_arg": "tax-template-id", "target_projection": None, "result_projection": None},
    "update-three-way-match-policy": {"domain": "buying", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "update-user": {"domain": "setup", "class": "sensitive", "enveloped": True, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "update-warehouse": {"domain": "inventory", "class": "draft", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:warehouse", "target_arg": "warehouse-id", "target_projection": None, "result_projection": None},
    "validate-module": {"domain": "os", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "validate-period-close": {"domain": "gl", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "row:fiscal_year", "target_arg": "fiscal-year-id", "target_projection": None, "result_projection": None},
    "validate-registry-completeness": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "verify-backup": {"domain": "setup", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "verify-trust-root": {"domain": "module-manager", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "none", "target_projection": None, "result_projection": None},
    "weekly-digest": {"domain": "reports", "class": "read", "enveloped": False, "money": (), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "arg:company-id", "target_projection": None, "result_projection": None},
    "write-off-invoice": {"domain": "payments", "class": "sensitive", "enveloped": True, "money": ("write-off-amount",), "json_args": (), "money_json_paths": {}, "bound_args": (), "company_source": "unknown", "target_projection": None, "result_projection": None},
}

_KEYS = ("domain", "class", "enveloped", "money", "json_args",
         "money_json_paths", "bound_args", "company_source",
         "target_projection", "result_projection")
_OPTIONAL_KEYS = ("target_arg",)
_DOMAINS = ("gl", "journals", "payments", "tax", "setup", "meta", "os",
            "reports", "billing", "selling", "buying", "inventory",
            "hr", "payroll", "accounting-adv",
            "module-manager",
            "onboarding", "legalclaw")
_DETAIL = ("money", "json_args", "money_json_paths", "bound_args")
_NAME_RX = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_COMPANY_RX = re.compile(r"^(arg:[a-z0-9-]+|row:[a-z0-9_]+|rows:[a-z0-9_]+(,[a-z0-9_]+)*|none|unknown)$")


def _check_tuple(tag, problems, decl, key):
    value = decl.get(key)
    if isinstance(value, list):
        problems.append(tag + ": " + key + " is a list, want a tuple")
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    else:
        problems.append(tag + ": " + key + " is not a tuple")
        return ()
    seen = set()
    for item in items:
        if not isinstance(item, str) or not _NAME_RX.match(item):
            problems.append(tag + ": " + key + " entry is not lowercase hyphenated: " + str(item))
        elif item in seen:
            problems.append(tag + ": " + key + " contains a duplicate: " + item)
        else:
            seen.add(item)
    return items


def _check_paths(tag, problems, decl, json_args):
    value = decl.get("money_json_paths")
    if isinstance(value, list):
        problems.append(tag + ": money_json_paths is a list, want a dict")
        return
    if not isinstance(value, dict):
        problems.append(tag + ": money_json_paths is not a dict")
        return
    for key, paths in value.items():
        if not isinstance(key, str) or not _NAME_RX.match(key):
            problems.append(tag + ": money_json_paths key is not lowercase hyphenated: " + str(key))
        elif key not in (json_args or ()):
            problems.append(tag + ": money_json_paths key not in json_args: " + key)
        if not isinstance(paths, tuple):
            problems.append(tag + ": money_json_paths paths for " + str(key) + " is not a tuple")
            continue
        for path in paths:
            if (not isinstance(path, tuple) or not path
                    or any(not isinstance(bit, str) or not bit for bit in path)):
                problems.append(tag + ": money path for " + str(key) + " is not a non-empty tuple of non-empty strings")


def validate(impact=IMPACT):
    problems = []
    if not isinstance(impact, dict):
        return ["impact table is not a dict"]
    for action in sorted(impact, key=str):
        tag = str(action)
        decl = impact[action]
        if not isinstance(action, str) or not _NAME_RX.match(action):
            problems.append(tag + ": name is not lowercase hyphenated")
            if not isinstance(decl, dict):
                problems.append(tag + ": declaration is not a dict")
            continue
        if not isinstance(decl, dict):
            problems.append(tag + ": declaration is not a dict")
            continue
        for key in decl:
            if key not in _KEYS and key not in _OPTIONAL_KEYS:
                problems.append(tag + ": unknown key " + str(key))
        for key in _KEYS:
            if key not in decl:
                problems.append(tag + ": missing key " + key)
        cls = decl.get("class")
        if cls not in CLASSES:
            problems.append(tag + ": class is not one of " + "/".join(CLASSES) + ": " + str(cls))
        if decl.get("enveloped") is not (cls in ("transaction", "sensitive")):
            problems.append(tag + ": enveloped is inconsistent with class " + str(cls))
        if decl.get("domain") not in _DOMAINS:
            problems.append(tag + ": domain is not a known surface: " + str(decl.get("domain")))
        json_args = ()
        if cls in ("read", "draft"):
            for key in _DETAIL:
                value = decl.get(key)
                if value != (() if key != "money_json_paths" else {}):
                    problems.append(tag + ": " + key + " must be empty for class " + str(cls))
        else:
            _check_tuple(tag, problems, decl, "money")
            json_args = _check_tuple(tag, problems, decl, "json_args")
            _check_tuple(tag, problems, decl, "bound_args")
            _check_paths(tag, problems, decl, json_args)
        if cls in ("read", "draft"):
            for key in ("money", "json_args", "bound_args"):
                value = decl.get(key)
                if isinstance(value, (tuple, list)) and not isinstance(value, tuple):
                    problems.append(tag + ": " + key + " is a list, want a tuple")
        company = decl.get("company_source")
        if not isinstance(company, str) or not _COMPANY_RX.match(company):
            problems.append(tag + ": company_source is not arg:<name>, row:<table>, rows:<table>[,<table>...], none or unknown: " + str(company))
        has_target = "target_arg" in decl
        target = decl.get("target_arg")
        if isinstance(company, str) and _COMPANY_RX.match(company) and (company.startswith("row:") or company.startswith("rows:")):
            if company.startswith("rows:"):
                tables = company[len("rows:"):].split(",")
                if not has_target:
                    problems.append(tag + ": target_arg is required for company_source " + company)
                elif not isinstance(target, tuple):
                    problems.append(tag + ": target_arg is not a tuple for company_source " + company + ": " + str(target))
                elif len(target) != len(tables):
                    problems.append(tag + ": target_arg length does not match company_source " + company + ": " + str(target))
                else:
                    seen = set()
                    for entry in target:
                        if not isinstance(entry, str) or not _NAME_RX.match(entry):
                            problems.append(tag + ": target_arg entry is not lowercase hyphenated: " + str(entry))
                        elif entry in seen:
                            problems.append(tag + ": target_arg contains a duplicate: " + entry)
                        else:
                            seen.add(entry)
            else:
                if not has_target:
                    problems.append(tag + ": target_arg is required for company_source " + company)
                elif not isinstance(target, str) or not _NAME_RX.match(target):
                    problems.append(tag + ": target_arg is not a lowercase hyphenated argument name: " + str(target))
        elif has_target:
            problems.append(tag + ": target_arg must be absent for company_source " + str(company))
        target_proj = decl.get("target_projection")
        result_proj = decl.get("result_projection")
        if target_proj is not None and (type(target_proj) is not str or target_proj not in TARGET_PROJECTIONS):
            problems.append(tag + ": target_projection is not a known projection: " + str(target_proj))
        if result_proj is not None and (type(result_proj) is not str or result_proj not in RESULT_PROJECTIONS):
            problems.append(tag + ": result_projection is not a known projection: " + str(result_proj))
        if (target_proj is None) != (result_proj is None):
            problems.append(tag + ": target_projection and result_projection must be set together")
        if (target_proj is not None or result_proj is not None) and decl.get("enveloped") is not True:
            problems.append(tag + ": projection needs enveloped True")
        if action in SENSITIVE and cls != "sensitive":
            problems.append(tag + ": member of SENSITIVE must have class sensitive")
        if action not in SENSITIVE and cls == "sensitive":
            problems.append(tag + ": class sensitive is reserved for SENSITIVE members")
    return problems


def declaration(action):
    decl = IMPACT.get(action)
    if decl is None:
        return None
    return copy.deepcopy(decl)

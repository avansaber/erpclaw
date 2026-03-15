"""ERPClaw OS Pattern Library -- reusable module patterns extracted from existing modules.

Each pattern defines a common business entity type with:
- Standard schema fields (column definitions)
- Standard actions (add, update, get, list, etc.)
- Source modules that demonstrate the pattern
- GL requirements (direct, delegated, or none)

Used by generate_module.py to map business entities to code templates.
"""

PATTERNS = {
    "crud_entity": {
        "name": "CRUD Entity",
        "description": "Basic entity with add, update, get, list actions",
        "source_modules": ["legalclaw", "healthclaw-vet"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "name TEXT NOT NULL",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "status TEXT DEFAULT 'active'",
            "notes TEXT",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["add", "update", "get", "list"],
        "requires_gl": False,
    },
    "appointment_booking": {
        "name": "Appointment/Booking",
        "description": "Scheduled event with date, time, status lifecycle",
        "source_modules": ["healthclaw", "healthclaw-vet"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "date TEXT NOT NULL",
            "start_time TEXT",
            "end_time TEXT",
            "status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','confirmed','in_progress','completed','cancelled'))",
            "notes TEXT",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["add", "update", "list"],
        "requires_gl": False,
    },
    "service_record": {
        "name": "Service Record",
        "description": "Record of a service performed, linked to an entity and appointment",
        "source_modules": ["healthclaw-vet", "automotiveclaw"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "service_type TEXT NOT NULL",
            "description TEXT",
            "amount TEXT DEFAULT '0'",
            "notes TEXT",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["add", "list"],
        "requires_gl": False,
    },
    "invoice_delegation": {
        "name": "Invoice Delegation",
        "description": "Create invoice via cross_skill (never direct GL)",
        "source_modules": ["groomingclaw", "tattooclaw", "storageclaw"],
        "schema_fields": [],
        "actions": ["create-invoice"],
        "requires_gl": True,  # but delegated, not direct
    },
    "compliance_tracking": {
        "name": "Compliance/Licensing",
        "description": "Track certifications, inspections, compliance records",
        "source_modules": ["legalclaw", "tattooclaw", "constructclaw"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "record_type TEXT NOT NULL",
            "status TEXT DEFAULT 'active'",
            "expiry_date TEXT",
            "notes TEXT",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["add", "list", "get"],
        "requires_gl": False,
    },
    "prepaid_package": {
        "name": "Prepaid Package/Credit",
        "description": "Prepaid sessions or credits with usage tracking",
        "source_modules": ["groomingclaw", "erpclaw-billing"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "total_sessions INTEGER NOT NULL",
            "used_sessions INTEGER NOT NULL DEFAULT 0",
            "amount TEXT DEFAULT '0'",
            "status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','exhausted','expired','cancelled'))",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["add", "use-session", "get-balance"],
        "requires_gl": False,
    },
    "recurring_billing": {
        "name": "Recurring Monthly Billing",
        "description": "Monthly billing with late fees and delinquency",
        "source_modules": ["storageclaw", "propertyclaw"],
        "schema_fields": [
            "id TEXT PRIMARY KEY",
            "company_id TEXT NOT NULL REFERENCES company(id)",
            "amount TEXT NOT NULL DEFAULT '0'",
            "billing_date TEXT",
            "status TEXT NOT NULL DEFAULT 'pending'",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ],
        "actions": ["generate-invoices", "apply-late-fees"],
        "requires_gl": True,
    },
}


# Mapping of common business terms to patterns — used for entity extraction
TERM_TO_PATTERN = {
    # CRUD entity terms
    "client": "crud_entity",
    "customer": "crud_entity",
    "member": "crud_entity",
    "pet": "crud_entity",
    "vehicle": "crud_entity",
    "property": "crud_entity",
    "item": "crud_entity",
    "product": "crud_entity",
    "employee": "crud_entity",
    "staff": "crud_entity",
    "contact": "crud_entity",
    "vendor": "crud_entity",
    "supplier": "crud_entity",
    "unit": "crud_entity",
    "location": "crud_entity",
    "category": "crud_entity",
    "type": "crud_entity",
    # Appointment/booking terms
    "appointment": "appointment_booking",
    "booking": "appointment_booking",
    "reservation": "appointment_booking",
    "session": "appointment_booking",
    "visit": "appointment_booking",
    "schedule": "appointment_booking",
    # Service record terms
    "service": "service_record",
    "treatment": "service_record",
    "inspection": "service_record",
    "maintenance": "service_record",
    "repair": "service_record",
    "job": "service_record",
    # Compliance terms
    "license": "compliance_tracking",
    "certification": "compliance_tracking",
    "permit": "compliance_tracking",
    "compliance": "compliance_tracking",
    "audit": "compliance_tracking",
    # Prepaid terms
    "package": "prepaid_package",
    "credit": "prepaid_package",
    "subscription": "prepaid_package",
    "plan": "prepaid_package",
    "membership": "prepaid_package",
    # Invoice delegation
    "invoice": "invoice_delegation",
    "billing": "invoice_delegation",
}


def get_pattern(name: str) -> dict | None:
    """Return a specific pattern by name, or None if not found."""
    return PATTERNS.get(name)


def list_patterns() -> list[dict]:
    """Return all patterns as a list with keys included."""
    result = []
    for key, pat in PATTERNS.items():
        entry = {"key": key}
        entry.update(pat)
        result.append(entry)
    return result


def suggest_pattern(term: str) -> str | None:
    """Suggest a pattern key for a business term."""
    return TERM_TO_PATTERN.get(term.lower())

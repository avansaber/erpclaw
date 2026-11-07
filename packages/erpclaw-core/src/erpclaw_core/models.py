"""Core tables. Plugins declare their own tables on the same metadata, prefixed
with the plugin name, and never write core tables except through core services.

Ledger rows have no updated_at: they are written once and never changed.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from .db import APPEND, READ, Money, MoneyText, Runtime, metadata, rate_check, scale_check

currency = Table(
    "currency",
    metadata,
    Column("code", String(3), primary_key=True),
    Column("name", String(60), nullable=False),
    Column("minor_units", Integer, nullable=False),
    CheckConstraint("minor_units between 0 and 4", name="ck_currency_minor_units"),
)

DATE_ORDERS = ("MDY", "DMY")

company = Table(
    "company",
    metadata,
    Column("id", String(36), primary_key=True),
    # name, currency and fiscal year start are null only while the company is pending (ck_company_ready)
    Column("name", String(200), nullable=True, unique=True),
    Column("functional_currency", String(3), ForeignKey("currency.code"), nullable=True),
    Column("fiscal_year_start_month", Integer, nullable=True, default=1),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # How the owner writes figure dates: month first (MDY) or day first (DMY); null when unset (then the date is
    # asked). Chosen by the owner, never inferred from the currency.
    Column("date_order", String(3), nullable=True),
    # pending: made by `init --own-books`, its business choices not made yet; ready: set up, those choices fixed
    Column("status", String(10), nullable=False, server_default="ready"),
    CheckConstraint("fiscal_year_start_month between 1 and 12", name="ck_company_fy_month"),
    CheckConstraint("date_order in ('MDY','DMY')", name="ck_company_date_order"),
    UniqueConstraint("id", "functional_currency", name="uq_company_functional_currency"),
    CheckConstraint("status in ('pending','ready')", name="ck_company_status"),
    CheckConstraint("status = 'pending' or (name is not null and functional_currency is not null "
                    "and fiscal_year_start_month is not null)", name="ck_company_ready"),
)


def _company_fk(cols: list[str], table: str, name: str) -> ForeignKeyConstraint:
    """A reference that cannot cross companies: (company_id, x_id) -> (company_id, id)."""
    return ForeignKeyConstraint(["company_id", *cols], [f"{table}.company_id", f"{table}.id"], name=name)


fiscal_year = Table(
    "fiscal_year",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("label", Integer, nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("status", String(10), nullable=False, default="open"),
    UniqueConstraint("company_id", "id", name="uq_fiscal_year_company_id"),
    UniqueConstraint("company_id", "label", name="uq_fiscal_year_label"),
    CheckConstraint("end_date > start_date", name="ck_fiscal_year_dates"),
    CheckConstraint("status in ('open','closed')", name="ck_fiscal_year_status"),
)

fiscal_period = Table(
    "fiscal_period",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("fiscal_year_id", String(36), nullable=False),
    Column("period_no", Integer, nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("status", String(10), nullable=False, default="open"),
    _company_fk(["fiscal_year_id"], "fiscal_year", "fk_period_year"),
    UniqueConstraint("company_id", "id", name="uq_fiscal_period_company_id"),
    UniqueConstraint("company_id", "fiscal_year_id", "period_no", name="uq_fiscal_period_no"),
    CheckConstraint("period_no between 1 and 12", name="ck_period_no"),
    CheckConstraint("end_date >= start_date", name="ck_period_dates"),
    CheckConstraint("status in ('open','locked','closed')", name="ck_period_status"),
)

DEBIT_ROOTS = ("asset", "expense")  # an account of these roots is debit-normal unless it says otherwise

# The roles the stock and exchange postings find accounts by, each fixed to its root type and normal side. The
# older roles below take any root and side.
ROLE_SIDES = {
    "stock": ("asset", "debit"),
    "cost_of_goods_sold": ("expense", "debit"),
    "stock_received_not_billed": ("liability", "credit"),  # owed to the supplier for goods received, not billed
    "sales_returns": ("income", "debit"),  # a contra-income account
    "exchange_gain_loss_realised": ("expense", "debit"),
    "exchange_gain_loss_unrealised": ("expense", "debit"),
    "opening_balance": ("equity", "credit"),  # opening stock's offset
    "stock_adjustment": ("expense", "debit"),  # a count's gain or loss
    "stock_write_off": ("expense", "debit"),  # a write-off
}
ACCOUNT_TYPES = ("receivable", "payable", "bank", "rounding", "retained_earnings", "income", *ROLE_SIDES)


def _quoted(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def _side_of_root(context) -> str:
    """The normal side of a row that leaves it out, from that row's own root type."""
    return "debit" if context.get_current_parameters().get("root_type") in DEBIT_ROOTS else "credit"


account = Table(
    "account",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(20), nullable=False),
    Column("name", String(200), nullable=False),
    Column("root_type", String(10), nullable=False),
    Column("account_type", String(40), nullable=True),
    Column("is_group", Boolean, nullable=False, default=False),
    Column("parent_id", String(36), nullable=True),
    Column("frozen", Boolean, nullable=False, default=False),
    Column("normal_side", String(6), nullable=False, default=_side_of_root),
    _company_fk(["parent_id"], "account", "fk_account_parent"),
    UniqueConstraint("company_id", "id", name="uq_account_company_id"),
    UniqueConstraint("company_id", "number", name="uq_account_number"),
    CheckConstraint("root_type in ('asset','liability','equity','income','expense')", name="ck_root_type"),
    CheckConstraint(f"account_type is null or account_type in ({_quoted(ACCOUNT_TYPES)})", name="ck_account_type"),
    CheckConstraint("normal_side in ('debit','credit')", name="ck_account_normal_side"),
    CheckConstraint(f"account_type is null or account_type not in ({_quoted(ROLE_SIDES)}) or "
                    + " or ".join(f"(account_type = '{role}' and root_type = '{root}' and normal_side = '{side}')"
                                  for role, (root, side) in ROLE_SIDES.items()), name="ck_account_role_side"),
)

party = Table(
    "party",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("kind", String(10), nullable=False),
    Column("name", String(200), nullable=False),
    Column("email", String(200), nullable=True),
    Column("city", String(100), nullable=True),
    Column("city_key", Text, nullable=True),  # textkey.text_key(city): a case-insensitive city match compares this
    UniqueConstraint("company_id", "id", name="uq_party_company_id"),
    UniqueConstraint("company_id", "kind", "name", name="uq_party_name"),
    CheckConstraint("kind in ('customer','supplier')", name="ck_party_kind"),
    CheckConstraint("(city is null) = (city_key is null)", name="ck_party_city_key"),
)

item = Table(  # rebuilt in revision 0004: a company key, the name per company, a canonical rate
    "item",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("name", String(200), nullable=False),
    Column("unit", String(20), nullable=False, default="unit"),
    Column("standard_rate", Money, nullable=False),
    # revision 0013: a service, or kept in stock; it moves only to stock (masters.mark_stock_item), never back
    Column("kind", String(8), nullable=False, default="service", server_default="service"),
    UniqueConstraint("company_id", "id", name="uq_item_company_id"),
    UniqueConstraint("company_id", "name", name="uq_item_name"),
    CheckConstraint(rate_check("standard_rate"), name="ck_item_rate"),
    CheckConstraint("kind in ('service','stock')", name="ck_item_kind"),
)

posting_chain_head = Table(
    "posting_chain_head",
    metadata,
    Column("company_id", String(36), ForeignKey("company.id"), primary_key=True),
    Column("last_seq", Integer, nullable=False, default=0),
    Column("last_hash", String(64), nullable=False),
)

posting = Table(
    "posting",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("voucher_type", String(40), nullable=False),
    Column("voucher_id", String(36), nullable=False),
    Column("memo", Text, nullable=True),
    Column("reverses_posting_id", String(36), nullable=True, unique=True),
    Column("operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(100), nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    Column("hash_format", Integer, nullable=False),
    CheckConstraint("hash_format = 2", name="ck_posting_hash_format"),
    # general, or sub when the company's accounting is external
    Column("ledger", String(8), nullable=False, server_default="general"),
    _company_fk(["reverses_posting_id"], "posting", "fk_posting_reverses"),
    UniqueConstraint("company_id", "id", name="uq_posting_company_id"),
    UniqueConstraint("company_id", "seq", name="uq_posting_seq"),
    UniqueConstraint("company_id", "voucher_type", "voucher_id", name="uq_posting_voucher"),
    CheckConstraint("seq >= 1", name="ck_posting_seq"),
    CheckConstraint("ledger in ('general','sub')", name="ck_posting_ledger"),
)

dimension_set = Table(
    "dimension_set", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("content", Text, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "id", name="uq_dimension_set_company_id"),
    UniqueConstraint("company_id", "digest", name="uq_dimension_set_digest"),
    CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_dimension_set_digest"),
    CheckConstraint("length(content) > 1", name="ck_dimension_set_content"),
)

ledger_entry = Table(
    "ledger_entry",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("posting_id", String(36), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("account_id", String(36), nullable=False),
    Column("party_id", String(36), nullable=True),
    Column("debit", MoneyText, nullable=False),
    Column("credit", MoneyText, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("rate", MoneyText, nullable=True),
    Column("functional_currency", String(3), nullable=False),
    Column("functional_debit", MoneyText, nullable=False),
    Column("functional_credit", MoneyText, nullable=False),
    Column("kind", String(10), nullable=False),
    Column("dimension_set_id", String(36), nullable=True),
    ForeignKeyConstraint(["company_id", "functional_currency"], ["company.id", "company.functional_currency"],
                         name="fk_entry_functional_currency"),
    _company_fk(["dimension_set_id"], "dimension_set", "fk_entry_dimension_set"),
    CheckConstraint("dimension_set_id IS NULL", name="ck_entry_no_dimension_v30"),
    CheckConstraint("kind IN ('measured','clearing','adjustment')", name="ck_entry_kind"),
    CheckConstraint("(kind = 'adjustment' AND rate IS NULL) OR (kind <> 'adjustment' AND rate IS NOT NULL AND "
                    "rate ~ '^(0|[1-9][0-9]*)(\\.[0-9]{0,8}[1-9])?$' AND rate ~ '[1-9]')", name="ck_entry_rate"),
    CheckConstraint("currency = functional_currency AND rate IS NOT NULL AND rate ~ '^1(\\.0+)?$' AND "
                    "kind = 'measured' AND functional_debit = debit AND functional_credit = credit",
                    name="ck_entry_functional_v30"),
    *[CheckConstraint(scale_check(col, "functional_currency"), name=f"ck_entry_{col}_scale")
      for col in ("functional_debit", "functional_credit")],
    _company_fk(["posting_id"], "posting", "fk_entry_posting"),
    _company_fk(["account_id"], "account", "fk_entry_account"),
    _company_fk(["party_id"], "party", "fk_entry_party"),
    UniqueConstraint("posting_id", "line_no", name="uq_entry_line"),
    *[CheckConstraint(scale_check(col, "currency"), name=f"ck_entry_{col}_scale") for col in ("debit", "credit")],
    # Not negative, and exactly one side non-zero (a zero amount has no digit 1-9).
    CheckConstraint("debit not like '-%' and credit not like '-%' and "
                    "((debit ~ '[1-9]') <> (credit ~ '[1-9]'))", name="ck_entry_one_side"),
)

# Append-only: these refuse every UPDATE and DELETE.
APPEND_ONLY = ("posting", "ledger_entry", "dimension_set")

fiscal_year_close = Table(  # one row per closed year, written once by close_year
    "fiscal_year_close",
    metadata,
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("fiscal_year_id", String(36), nullable=False),
    Column("closing_posting_id", String(36), nullable=True),  # none when every P&L balance was zero
    Column("closed_by", String(100), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("company_id", "fiscal_year_id", name="pk_fiscal_year_close"),
    _company_fk(["fiscal_year_id"], "fiscal_year", "fk_fiscal_year_close_year"),
    _company_fk(["closing_posting_id"], "posting", "fk_fiscal_year_close_posting"),
)

# --- the integration kernel ---------------

AREAS = ("accounting", "payroll", "hr", "stock", "crm")

system_of_record = Table(
    "system_of_record",
    metadata,
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("area", String(20), nullable=False),
    Column("mode", String(10), nullable=False),
    Column("external_system", String(60), nullable=True),
    Column("set_at", DateTime(timezone=True), nullable=False),
    Column("set_by", String(100), nullable=False),
    PrimaryKeyConstraint("company_id", "area", name="pk_system_of_record"),
    CheckConstraint("area in ('accounting','payroll','hr','stock','crm')", name="ck_sor_area"),
    CheckConstraint("(mode = 'native' and external_system is null) or "
                    "(mode = 'external' and external_system is not null)", name="ck_sor_mode"),
)

external_ref = Table(
    "external_ref",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("external_system", String(60), nullable=False),
    Column("object_type", String(40), nullable=False),
    Column("local_id", String(36), nullable=False),
    Column("external_id", String(200), nullable=False),
    Column("last_synced_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("company_id", "external_system", "object_type", "local_id", name="uq_external_ref_local"),
    UniqueConstraint("company_id", "external_system", "object_type", "external_id", name="uq_external_ref_external"),
)

account_map = Table(
    "account_map",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("external_system", String(60), nullable=False),
    Column("account_id", String(36), nullable=False),
    Column("external_code", String(100), nullable=False),
    _company_fk(["account_id"], "account", "fk_account_map_account"),
    UniqueConstraint("company_id", "external_system", "account_id", name="uq_account_map"),
)

sync_payload = Table(
    "sync_payload",
    metadata,
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("body", Text, nullable=False),
    PrimaryKeyConstraint("company_id", "digest", name="pk_sync_payload"),
)

sync_journal = Table(
    "sync_journal",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("external_system", String(60), nullable=False),
    Column("direction", String(3), nullable=False),
    Column("object_type", String(40), nullable=False),
    Column("local_id", String(36), nullable=True),
    Column("external_id", String(200), nullable=True),
    Column("operation_key", String(120), nullable=True),
    Column("record_identity", String(240), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("status", String(10), nullable=False),
    Column("attempt_id", String(36), nullable=True),
    Column("at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "payload_digest"], ["sync_payload.company_id", "sync_payload.digest"],
                         name="fk_sync_journal_payload"),
    UniqueConstraint("company_id", "external_system", "direction", "object_type", "record_identity",
                     "payload_digest", name="uq_sync_journal_exchange"),
    CheckConstraint("direction in ('in','out')", name="ck_sync_direction"),
    CheckConstraint("status in ('staged','approved','sent','confirmed','unknown','conflict','failed')",
                    name="ck_sync_status"),
)

number_series = Table(
    "number_series",
    metadata,
    Column("company_id", String(36), ForeignKey("company.id"), primary_key=True),
    Column("series", String(20), primary_key=True),
    Column("fiscal_year", Integer, primary_key=True),
    Column("next_value", Integer, nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(100), nullable=False),
    Column("action", String(100), nullable=False),
    Column("document_type", String(40), nullable=True),
    Column("document_id", String(36), nullable=True),
    Column("approval_id", String(36), nullable=True),
    Column("detail", Text, nullable=True),
)

PERMISSIONS = ("view", "draft", "approve", "post", "reverse", "close_period", "export", "administer_users",
               "install_plugins", "manage_secrets")  # the engine's named operations
CARD_STATUS = ("pending", "approved", "consumed", "declined", "expired", "superseded", "invalidated")

principal = Table(  # who may act; only a SHA-256 of the API token is kept
    "principal",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("kind", String(10), nullable=False),
    Column("disabled", Boolean, nullable=False),
    Column("grants_version", Integer, nullable=False),
    Column("token_sha256", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("kind in ('person')", name="ck_principal_kind"),
    CheckConstraint("grants_version >= 1", name="ck_principal_grants_version"),
    UniqueConstraint("name", name="uq_principal_name"),
    UniqueConstraint("token_sha256", name="uq_principal_token"),
)

principal_sign_in = Table(  # only the code's hash, never the bearer code
    "principal_sign_in",
    metadata,
    Column("code_sha256", String(64), primary_key=True),
    Column("principal_id", String(36), ForeignKey("principal.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(100), nullable=False),
    Column("run_id", String(32), nullable=True),
    PrimaryKeyConstraint("code_sha256", name="pk_principal_sign_in"),
    CheckConstraint("expires_at > created_at", name="ck_principal_sign_in_expiry"),
)

principal_session = Table(  # only the session secret's hash
    "principal_session",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("secret_sha256", String(64), nullable=False),
    Column("principal_id", String(36), ForeignKey("principal.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("ended_why", String(20), nullable=True),
    Column("user_agent", String(200), nullable=True),
    PrimaryKeyConstraint("id", name="pk_principal_session"),
    UniqueConstraint("secret_sha256", name="uq_principal_session_secret"),
    CheckConstraint("ended_why is null or ended_why in "
                    "('sign_out','expired','disabled','restored','engine_start','sign_out_all')",
                    name="ck_principal_session_end_reason"),
    CheckConstraint("(ended_at is null) = (ended_why is null)", name="ck_principal_session_end_pair"),
    CheckConstraint("idle_expires_at <= expires_at", name="ck_principal_session_idle"),
)

principal_grant = Table(  # a permission on one company; there is no global grant
    "principal_grant",
    metadata,
    Column("principal_id", String(36), ForeignKey("principal.id"), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), primary_key=True),
    Column("permission", String(20), primary_key=True),
    CheckConstraint("permission in (" + ", ".join(repr(p) for p in PERMISSIONS) + ")",
                    name="ck_principal_grant_permission"),
)

approval = Table(  # the card: bound by its digest to everything it was built from
    "approval",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("principal_id", String(36), ForeignKey("principal.id"), nullable=False),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("conversation_id", String(36), nullable=True),
    Column("contract", String(100), nullable=False),
    Column("contract_version", String(20), nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("impl_version", String(120), nullable=False),
    Column("args_json", Text, nullable=False),
    Column("read_set_json", Text, nullable=False),
    Column("card_json", Text, nullable=False),
    Column("nonce", String(32), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("status", String(12), nullable=False),
    Column("operation_key", String(36), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("approved_by", String(36), ForeignKey("principal.id"), nullable=True),
    Column("approver_grants_version", Integer, nullable=True),
    Column("decision_json", Text, nullable=True),  # what ended it without a commit, returned on a repeat
    Column("request_card_token", String(64), nullable=True),  # the turn's request that made it; never updated
    CheckConstraint("status in (" + ", ".join(repr(x) for x in CARD_STATUS) + ")", name="ck_approval_status"),
    UniqueConstraint("operation_key", name="uq_approval_operation_key"),
    Index("uq_approval_one_pending", "conversation_id", unique=True,  # one pending card per conversation
          postgresql_where=text("status = 'pending' AND conversation_id IS NOT NULL")),
)

conversation_fence = Table(  # which turn of a conversation may make or approve a card
    "conversation_fence",
    metadata,
    Column("conversation_id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("live_token", String(64), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("generation >= 0", name="ck_conversation_fence_generation"),
)

operation_outcome = Table(  # written in the same transaction as the effect
    "operation_outcome",
    metadata,
    Column("key", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("approval_id", String(36), ForeignKey("approval.id"), nullable=False),
    Column("card_digest", String(64), nullable=False),
    Column("status", String(12), nullable=False),
    Column("result_json", Text, nullable=False),
    Column("committed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status in ('committed','refused')", name="ck_operation_outcome_status"),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("slot", String(40), nullable=False),
    Column("recipient", String(200), nullable=False),
    Column("subject", String(300), nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(12), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("delivered_at", DateTime(timezone=True), nullable=True),  # read only: delivery is never claimed now
    Column("detail", Text, nullable=True),
    # the approved message, as the owner approved it, and its one current attempt
    Column("message_id", String(300), nullable=True),
    Column("from_address", String(200), nullable=True),
    Column("from_name", String(200), nullable=True),
    Column("reply_to", String(200), nullable=True),
    Column("current_attempt_id", String(36), nullable=True),
    Column("next_try_at", DateTime(timezone=True), nullable=True),
    Column("tries", Integer, nullable=False, server_default="0"),
    Column("first_failed_at", DateTime(timezone=True), nullable=True),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status in ('queued','sending','accepted','unknown','failed')", name="ck_outbox_status"),
)

outbox_attempt = Table(
    "outbox_attempt",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("outbox_id", String(36), ForeignKey("outbox.id"), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("lease_until", DateTime(timezone=True), nullable=False),
    Column("data_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("code", Integer, nullable=True),
    Column("result", String(16), nullable=True),
    Column("text", String(200), nullable=True),
    CheckConstraint("result is null or result in ('accepted','temporary','permanent','no_answer','lost_ownership',"
                    "'abandoned')", name="ck_outbox_attempt_result"),
)

file_object = Table(  # a stored file of a document: immutable, content-addressed (files.py)
    "file_object",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("voucher_type", String(40), nullable=False),
    Column("voucher_id", String(36), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("retention_until", Date, nullable=True),  # NULL: kept as long as the books
    Column("render_inputs", Text, nullable=True),  # canonical JSON, the exact bytes
    CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_file_object_sha256"),
    CheckConstraint("size > 0 AND size <= 10485760", name="ck_file_object_size"),
    CheckConstraint("media_type = 'application/pdf'", name="ck_file_object_media_type"),
    CheckConstraint("render_inputs IS NULL OR render_inputs::json IS NOT NULL", name="ck_file_object_render_inputs"),
    UniqueConstraint("company_id", "voucher_type", "voucher_id", "sha256", name="uq_file_object_document"),
    Index("ix_file_object_sha256", "sha256", "company_id"),
)

outbox_attachment = Table(
    "outbox_attachment",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("outbox_id", String(36), ForeignKey("outbox.id"), nullable=False),
    Column("name", String(200), nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("file_id", String(36), ForeignKey("file_object.id", ondelete="RESTRICT", name="fk_outbox_attachment_file"),
           nullable=False),  # a file a message names can never be removed
)

plugin_install = Table(
    "plugin_install",
    metadata,
    Column("name", String(60), primary_key=True),
    Column("version", String(20), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("installed_at", DateTime(timezone=True), nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("artifact_digest", String(64), nullable=False),
    Column("after_core_revision", String(40), nullable=False),
    Column("status", String(12), nullable=False),
    CheckConstraint("status in ('installed','disabled')", name="ck_plugin_install_status"),
)

# Core's tables, a fixed set: the loader checks marks against it, so neither a
# table registered early nor a changed mark can make a table core's or take
# one away (plugin interface: ownership is a mark, not import order).
CORE_TABLES = frozenset({"currency", "company", "fiscal_year", "fiscal_period", "account", "party", "item",
                         "posting_chain_head", "posting", "ledger_entry", "dimension_set", "number_series", "audit_log", "approval",
                         "principal", "principal_grant", "principal_sign_in", "principal_session",
                         "operation_outcome", "conversation_fence", "outbox",
                         "outbox_attempt", "outbox_attachment", "file_object", "plugin_install", "erpclaw_books", "books_identity", "fiscal_year_close",
                         "system_of_record", "external_ref", "account_map", "sync_payload", "sync_journal"})
from .payable_tables import PAYABLE_TABLES  # noqa: E402  (core's supplier payables tables)
CORE_TABLES = CORE_TABLES | PAYABLE_TABLES
for _name in CORE_TABLES:
    metadata.tables[_name].info["owner"] = "core"

# The PostgreSQL runtime role's privileges on each core table.
# UPDATE names only the columns the engine sets; DELETE only where it
# deletes; a SELECT ... FOR UPDATE needs an UPDATE privilege on its table, so the
# locked tables are among them. tests/test_runtime_role.py derives each from the code.
RUNTIME = {
    "currency": READ, "erpclaw_books": READ, "books_identity": READ,
    # the setup card sets a pending company's choices; a trigger refuses the runtime role's change once it is ready
    "company": Runtime(insert=True, update=("name", "functional_currency", "fiscal_year_start_month", "date_order",
                                            "status")),
    "account": APPEND, "party": APPEND, "item": Runtime(insert=True, update=("kind",)),
    "posting": APPEND, "ledger_entry": APPEND, "dimension_set": APPEND, "fiscal_year_close": APPEND, "audit_log": APPEND,
    "external_ref": APPEND, "account_map": APPEND, "sync_payload": APPEND, "sync_journal": APPEND,
    "posting_chain_head": Runtime(insert=True, update=("last_seq", "last_hash")),
    "fiscal_year": Runtime(insert=True, update=("status",)),
    "fiscal_period": Runtime(insert=True, update=("status",)),
    "number_series": Runtime(insert=True, update=("next_value",)),
    "approval": Runtime(insert=True, update=("status", "decided_at", "approved_by", "approver_grants_version",
                                             "decision_json")),
    "conversation_fence": Runtime(insert=True, update=("generation", "live_token", "updated_at")),
    "operation_outcome": APPEND,
    # read and locked FOR SHARE at commit, never written by the runtime: grants change as the owner only
    "principal": Runtime(lock=("grants_version",)), "principal_grant": READ,
    "principal_sign_in": Runtime(update=("used_at",)),
    "principal_session": Runtime(insert=True, update=("last_seen_at", "idle_expires_at", "ended_at", "ended_why")),
    "outbox": Runtime(insert=True, update=("status", "detail", "current_attempt_id", "next_try_at", "tries",
                                           "first_failed_at", "accepted_at")),
    "outbox_attempt": Runtime(insert=True, update=("data_at", "finished_at", "code", "result", "text")),
    "outbox_attachment": APPEND,
    "file_object": APPEND,  # removed only by the retention job, as the owner
    "plugin_install": Runtime(update=("status",), delete=True),  # installing is an owner command
    "system_of_record": Runtime(insert=True, delete=True),
}
for _name, _policy in RUNTIME.items():
    metadata.tables[_name].info["runtime"] = _policy

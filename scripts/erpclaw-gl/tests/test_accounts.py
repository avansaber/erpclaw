"""Tests for erpclaw-gl account management actions.

Actions tested:
  - setup-chart-of-accounts
  - add-account
  - update-account
  - list-accounts
  - get-account
  - freeze-account / unfreeze-account
"""
import pytest
from decimal import Decimal
from gl_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_account, load_db_query,
)

mod = load_db_query()


class TestSetupChartOfAccounts:
    def test_us_gaap_template(self, conn):
        """Load US GAAP chart of accounts."""
        cid = seed_company(conn)
        result = call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=cid, template="us_gaap",
        ))
        assert is_ok(result)
        assert result["accounts_created"] > 0
        assert result["template"] == "us_gaap"

        # Verify accounts exist in DB
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM account WHERE company_id=?", (cid,)
        ).fetchone()["cnt"]
        assert count == result["accounts_created"]

    def test_auto_detect_company(self, conn):
        """If only one company exists, auto-detect it."""
        cid = seed_company(conn)
        result = call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=None, template=None,
        ))
        assert is_ok(result)
        assert result["accounts_created"] > 0

    def test_creates_root_accounts(self, conn):
        """US GAAP chart should have all 5 root types."""
        cid = seed_company(conn)
        call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=cid, template="us_gaap",
        ))
        for root_type in ["asset", "liability", "equity", "income", "expense"]:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM account WHERE company_id=? AND root_type=?",
                (cid, root_type)
            ).fetchone()
            assert row["cnt"] > 0, f"Missing accounts for root_type={root_type}"


class TestAddAccount:
    def test_basic_create(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Cash in Bank", company_id=cid,
            root_type="asset", account_type="bank",
            account_number="1010", parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_ok(result)
        assert result["name"] == "Cash in Bank"
        assert "account_id" in result

    def test_correct_balance_direction(self, conn):
        """Asset/expense accounts should be debit_normal, liability/equity/income credit_normal."""
        cid = seed_company(conn)
        for root_type, expected_dir in [
            ("asset", "debit_normal"),
            ("expense", "debit_normal"),
            ("liability", "credit_normal"),
            ("equity", "credit_normal"),
            ("income", "credit_normal"),
        ]:
            result = call_action(mod.add_account, conn, ns(
                name=f"Test {root_type}", company_id=cid,
                root_type=root_type, account_type=None,
                account_number=f"BD-{root_type[:3]}", parent_id=None,
                currency=None, is_group=False,
            ))
            row = conn.execute(
                "SELECT balance_direction FROM account WHERE id=?",
                (result["account_id"],)
            ).fetchone()
            assert row["balance_direction"] == expected_dir

    def test_missing_name_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name=None, company_id=cid,
            root_type="asset", account_type=None,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result)

    def test_missing_root_type_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Bad Account", company_id=cid,
            root_type=None, account_type=None,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result)

    def test_group_account(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Current Assets", company_id=cid,
            root_type="asset", account_type=None,
            account_number="1000", parent_id=None,
            currency=None, is_group=True,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT is_group FROM account WHERE id=?",
                           (result["account_id"],)).fetchone()
        assert row["is_group"] == 1

    def test_leaf_only_type_rejects_group(self, conn):
        """Account types like tax, receivable, payable must be leaf (posting) accounts."""
        cid = seed_company(conn)
        for acct_type in ("tax", "receivable", "payable", "bank", "cash",
                          "cost_of_goods_sold", "stock"):
            result = call_action(mod.add_account, conn, ns(
                name=f"Bad Group {acct_type}", company_id=cid,
                root_type="asset", account_type=acct_type,
                account_number=None, parent_id=None,
                currency=None, is_group=True,
            ))
            assert is_error(result), f"is_group=True should fail for account_type={acct_type}"

    def test_leaf_only_type_allows_non_group(self, conn):
        """Leaf-only types should work fine when is_group=False."""
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Tax Payable", company_id=cid,
            root_type="liability", account_type="tax",
            account_number="2100", parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_ok(result)

    def test_child_account_depth(self, conn):
        """Child account should have depth = parent depth + 1."""
        cid = seed_company(conn)
        parent = call_action(mod.add_account, conn, ns(
            name="Parent Group", company_id=cid,
            root_type="asset", account_type=None,
            account_number="1000", parent_id=None,
            currency=None, is_group=True,
        ))
        child = call_action(mod.add_account, conn, ns(
            name="Child Account", company_id=cid,
            root_type="asset", account_type="bank",
            account_number="1010", parent_id=parent["account_id"],
            currency=None, is_group=False,
        ))
        row = conn.execute("SELECT depth FROM account WHERE id=?",
                           (child["account_id"],)).fetchone()
        assert row["depth"] >= 1


def _upd(**kwargs):
    """update-account flags with the CLI's own defaults for the ones not set.

    argparse supplies every flag on a real invocation; a Namespace built by hand
    does not, so the defaults live here rather than as getattr() defensiveness in
    the action.
    """
    base = {"account_id": None, "name": None, "account_number": None,
            "parent_id": None, "is_frozen": None, "account_type": None,
            "reclassify_posted": False}
    base.update(kwargs)
    return ns(**base)


class TestUpdateAccount:
    def test_update_name(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Old Name")
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, name="New Name"))
        assert is_ok(result)
        row = conn.execute("SELECT name FROM account WHERE id=?", (aid,)).fetchone()
        assert row["name"] == "New Name"

    def test_freeze_via_update(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, is_frozen="true"))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 1


class TestUpdateAccountType:
    """Retyping an account (M94).

    Before M94 `--account-type` was a real flag that `update_account` simply
    dropped: passing it alongside any other field returned `status: ok` and left
    the type unchanged, and the `has_entries` probe written for this exact
    restriction was computed and never read. M94's migration retypes accounts, so
    the manual remedy has to exist. Plan home: planning/pending_items.md row M94.
    """

    def _seed_posted_entry(self, conn, cid, account_id):
        """One non-cancelled gl_entry against `account_id`, inserted directly.

        The guard reads `gl_entry` and nothing else, so a bare row is the exact
        precondition; routing a whole voucher through the posting helper would
        test the helper, not the guard.
        """
        import uuid
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, is_cancelled) "
            "VALUES (?, '2026-01-31', ?, '100.00', '0', 'journal_entry', ?, 0)",
            (str(uuid.uuid4()), account_id, str(uuid.uuid4())))
        conn.commit()

    def test_an_empty_account_retypes_freely(self, conn):
        """Nothing has been classified under the old type, so nothing is
        restated. This is the typo-fix case and it must not need a ceremony."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Gain on Asset Disposal",
                           root_type="income", account_type="revenue")
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss"))
        assert is_ok(result), result
        assert result["updated_fields"] == ["account_type"]
        assert result["reclassified_gl_entries"] == 0
        row = conn.execute("SELECT account_type FROM account WHERE id=?",
                           (aid,)).fetchone()
        assert row["account_type"] == "disposal_gain_loss"

    def test_account_type_alone_is_enough_to_update(self, conn):
        """`--account-type` on its own used to hit "No fields to update"."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, root_type="income", account_type="revenue")
        assert is_ok(call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss")))

    def test_retyping_an_account_with_postings_needs_the_acknowledgement(self, conn):
        """A posted account is a different safety case: every report that filters
        on account_type re-reads its whole history under the new type. Not one
        ledger row changes, but the books read differently, so it takes a
        deliberate keystroke."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Gain on Asset Disposal",
                           root_type="income", account_type="revenue")
        self._seed_posted_entry(conn, cid, aid)

        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss"))
        assert is_error(result), result
        assert "1 posted GL entry" in result["message"]
        assert "reclassifies that history" in result["message"]
        assert "--reclassify-posted" in result["suggestion"]
        row = conn.execute("SELECT account_type FROM account WHERE id=?",
                           (aid,)).fetchone()
        assert row["account_type"] == "revenue", "refused, and unchanged"

    def test_the_acknowledgement_lets_the_retype_through_and_reports_the_reach(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Gain on Asset Disposal",
                           root_type="income", account_type="revenue")
        self._seed_posted_entry(conn, cid, aid)
        self._seed_posted_entry(conn, cid, aid)

        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss",
            reclassify_posted=True))
        assert is_ok(result), result
        assert result["reclassified_gl_entries"] == 2
        row = conn.execute("SELECT account_type FROM account WHERE id=?",
                           (aid,)).fetchone()
        assert row["account_type"] == "disposal_gain_loss"

    def test_a_cancelled_entry_does_not_count_as_history(self, conn):
        """Cancelled entries were reversed; they classify nothing."""
        import uuid
        cid = seed_company(conn)
        aid = seed_account(conn, cid, root_type="income", account_type="revenue")
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, is_cancelled) "
            "VALUES (?, '2026-01-31', ?, '100.00', '0', 'journal_entry', ?, 1)",
            (str(uuid.uuid4()), aid, str(uuid.uuid4())))
        conn.commit()

        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss"))
        assert is_ok(result), result
        assert result["reclassified_gl_entries"] == 0

    def test_an_unregistered_type_is_refused(self, conn):
        """Same registry validity add-account enforces, so the two surfaces
        cannot disagree about what a legal type is."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, root_type="income", account_type="revenue")
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="not_a_real_type"))
        assert is_error(result)
        assert "not a registered, active type" in result["message"]

    def test_a_deactivated_type_is_refused(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, root_type="income", account_type="revenue")
        conn.execute("UPDATE account_type_registry SET is_active = 0 "
                     "WHERE account_type = 'disposal_gain_loss'")
        conn.commit()
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss"))
        assert is_error(result)
        assert "not a registered, active type" in result["message"]

    def test_a_group_account_may_not_take_a_leaf_only_type(self, conn):
        """add-account refuses to CREATE a group account typed `bank`; retyping
        must not be the way around that."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Cash and Cash Equivalents",
                           root_type="asset", account_type=None, is_group=1)
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="bank"))
        assert is_error(result)
        assert "posting (leaf) account" in result["message"]

    def test_retyping_to_the_same_type_is_not_a_reclassification(self, conn):
        """A no-op retype must not demand the acknowledgement — the caller is
        setting the value it already has, usually alongside another field."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Sales", root_type="income",
                           account_type="revenue")
        self._seed_posted_entry(conn, cid, aid)
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, name="Sales Revenue", account_type="revenue"))
        assert is_ok(result), result
        assert result["updated_fields"] == ["name"]

    def test_root_type_is_still_not_updatable(self, conn):
        """Out of M94's scope and stated so: root_type flips balance_direction and
        moves an account between the balance sheet and the P&L."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, root_type="income", account_type="revenue")
        args = _upd(account_id=aid, name="Renamed")
        args.root_type = "expense"
        result = call_action(mod.update_account, conn, args)
        assert is_ok(result)
        assert "root_type" not in result["updated_fields"]
        row = conn.execute("SELECT root_type FROM account WHERE id=?",
                           (aid,)).fetchone()
        assert row["root_type"] == "income"


class TestRootTypeCoherence:
    """account_type must be able to live on the account's root_type (M94 rider R6).

    The registry answers "is this type legal" and has no root_type column, so
    nothing answered "legal WHERE". A `bank` account retyped to a P&L type keeps
    root_type='asset', passes every existing check, and leaves the
    `account_type IN ('bank','cash')` population that cash flow is built on while
    still holding real cash history. Measured on a seeded install before this
    check existed: cash-flow closing balance 14,000.00 -> 0.00, operating
    9,000.00 -> -5,000.00.

    The tests come in two halves on purpose. The refusals prove the check bites;
    the acceptances prove it does not bite our own charts, which is the failure
    mode a coherence rule actually has.
    """

    # ── it refuses ───────────────────────────────────────────────────────────

    def test_a_bank_account_may_not_be_retyped_to_a_pl_type(self, conn):
        """The measured case. root_type is not updatable, so this retype is a
        one-way door: the account would sit on the asset side of the books for
        good while every account_type-filtered report read it as a P&L line."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Operating Checking",
                           root_type="asset", account_type="bank")
        result = call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss",
            reclassify_posted=True))
        assert is_error(result), result
        assert "belongs on a root_type of expense or income" in result["message"]
        assert "has root_type 'asset'" in result["message"]
        assert "root_type is not updatable" in result["suggestion"]
        row = conn.execute("SELECT account_type FROM account WHERE id=?",
                           (aid,)).fetchone()
        assert row["account_type"] == "bank", "refused, and unchanged"

    def test_the_acknowledgement_flag_does_not_buy_an_incoherent_retype(self, conn):
        """--reclassify-posted acknowledges restating history under a type the
        account CAN carry. It is not a general override, and a check that a flag
        could switch off would not be a check."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Petty Cash",
                           root_type="asset", account_type="cash")
        for flag in (True, False):
            result = call_action(mod.update_account, conn, _upd(
                account_id=aid, account_type="revenue", reclassify_posted=flag))
            assert is_error(result), (flag, result)
            assert "belongs on a root_type of income" in result["message"]

    def test_add_account_refuses_the_same_combination(self, conn):
        """Both surfaces or neither: a combination that cannot be created must
        not be reachable by creating it elsewhere and retyping, and vice versa."""
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Sales on the Balance Sheet", company_id=cid,
            root_type="asset", account_type="revenue",
            account_number="9001", parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result), result
        assert "belongs on a root_type of income" in result["message"]
        assert "--root-type" in result["suggestion"]
        assert conn.execute(
            "SELECT COUNT(*) c FROM account WHERE company_id=? AND account_number='9001'",
            (cid,)).fetchone()["c"] == 0

    @pytest.mark.parametrize("account_type,root_type", [
        ("bank", "income"),
        ("payable", "asset"),
        ("receivable", "liability"),
        ("revenue", "expense"),
        ("cost_of_goods_sold", "income"),
        ("depreciation", "asset"),
        ("equity", "liability"),
        ("fixed_asset", "expense"),
        ("disposal_gain_loss", "asset"),
    ])
    def test_incoherent_pairs_are_refused_on_creation(self, conn, account_type,
                                                      root_type):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name=f"{account_type} on {root_type}", company_id=cid,
            root_type=root_type, account_type=account_type,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result), (account_type, root_type, result)
        assert "belongs on a root_type of" in result["message"]

    # ── it must not over-refuse ──────────────────────────────────────────────

    @pytest.mark.parametrize("name,account_type,root_type", [
        # every one of these is a row in a chart we ship, or the type's own
        # two-sided reality. A coherence rule that refuses these is the M91
        # first-draft mistake repeated.
        ("Prepaid Insurance", "expense", "asset"),          # us_gaap 1141
        ("Rent Expense", "expense", "expense"),             # us_gaap 5220
        ("Sales Tax Payable", "tax", "liability"),          # us_gaap 2121
        ("Input GST Credit", "tax", "asset"),               # indian_coa
        ("Stock Received Not Billed", "stock_received_not_billed", "asset"),
        ("Goods Received Not Billed", "stock_received_not_billed", "liability"),
        ("Opening Balance Equity", "temporary", "equity"),  # us_gaap 3150
        ("Suspense", "temporary", "asset"),                 # indian_coa
        ("Gain on Asset Disposal", "disposal_gain_loss", "income"),
        ("Loss on Asset Disposal", "disposal_gain_loss", "expense"),
        ("Exchange Gain", "exchange_gain_loss", "income"),
        ("Exchange Loss", "exchange_gain_loss", "expense"),
        ("Rounding Adjustment", "rounding", "expense"),
        ("Rounding Gain", "rounding", "income"),
        ("Stock Adjustment", "stock_adjustment", "expense"),
    ])
    def test_coherent_pairs_are_accepted_on_creation(self, conn, name,
                                                     account_type, root_type):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name=name, company_id=cid,
            root_type=root_type, account_type=account_type,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_ok(result), (name, account_type, root_type, result)

    def test_the_disposal_retype_this_row_exists_for_still_works(self, conn):
        """M94's own remedy is a retype from `revenue` to `disposal_gain_loss` on
        an income account. Coherent, and it must stay free."""
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Gain on Asset Disposal",
                           root_type="income", account_type="revenue")
        assert is_ok(call_action(mod.update_account, conn, _upd(
            account_id=aid, account_type="disposal_gain_loss")))

    def test_a_type_the_map_has_never_measured_is_unconstrained(self, conn):
        """A module that registers its own account_type at runtime gets no
        opinion from us about where it belongs. Refusing what we have not
        measured is how a coherence rule starts refusing real charts."""
        conn.execute("INSERT INTO account_type_registry "
                     "(account_type, skill_name, label, is_active) "
                     "VALUES ('clinic_receipts', 'healthclaw', 'Clinic Receipts', 1)")
        conn.commit()
        assert "clinic_receipts" not in mod.ACCOUNT_TYPE_ROOT_TYPES
        cid = seed_company(conn)
        for root_type in ("asset", "liability", "equity", "income", "expense"):
            assert is_ok(call_action(mod.add_account, conn, ns(
                name=f"Clinic {root_type}", company_id=cid,
                root_type=root_type, account_type="clinic_receipts",
                account_number=None, parent_id=None,
                currency=None, is_group=False,
            )))

    # ── the map is derived, and stays derived ────────────────────────────────

    def test_the_coherence_map_accepts_every_pair_our_own_charts_ship(self):
        """The anti-over-refusal guard, re-derived from the shipped JSON on every
        run rather than restated here. If a chart ever gains a pair the map does
        not allow, this fails and the map (or the chart) is wrong — before an
        install discovers it by having its own accounts refused."""
        import json
        import os
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(mod.__file__)))))
        charts = [
            os.path.join(src, "erpclaw", "scripts", "erpclaw-gl", "assets",
                         "charts", "us_gaap.json"),
            os.path.join(src, "erpclaw", "scripts", "erpclaw-setup", "assets",
                         "us_gaap.json"),
            os.path.join(src, "erpclaw-regions", "erpclaw-region-in", "assets",
                         "indian_coa.json"),
            os.path.join(src, "erpclaw-regions", "erpclaw-region-ca", "assets",
                         "ca_coa_aspe.json"),
        ]
        seen = 0
        for path in charts:
            if not os.path.isfile(path):
                continue
            for row in json.load(open(path)):
                at, rt = row.get("account_type"), row.get("root_type")
                allowed = mod.ACCOUNT_TYPE_ROOT_TYPES.get(at)
                if not at or not rt or allowed is None:
                    continue
                seen += 1
                assert rt in allowed, (
                    f"{path}: {row.get('name')} ships account_type={at!r} on "
                    f"root_type={rt!r}, which this map refuses")
        assert seen > 100, f"only {seen} chart pairs checked; the charts moved"

    def test_the_map_accepts_every_root_the_os_engine_infers(self):
        """erpclaw-os-engine's ACCOUNT_TYPE_ROOT_MAP answers a different question
        (which root to INFER when a caller gave none). Every root it infers must
        be one this map accepts, or configure-module would build accounts that
        add-account then refuses."""
        import os
        import re
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(mod.__file__)))))
        path = os.path.join(src, "erpclaw-addons", "erpclaw-os-engine", "scripts",
                            "industry_configs.py")
        if not os.path.isfile(path):
            pytest.skip("erpclaw-os-engine not present beside this checkout")
        text = open(path).read()
        block = text.split("ACCOUNT_TYPE_ROOT_MAP = {")[1].split("}")[0]
        pairs = re.findall(r'"(\w+)"\s*:\s*"(\w+)"', block)
        assert len(pairs) >= 15, pairs
        for at, rt in pairs:
            allowed = mod.ACCOUNT_TYPE_ROOT_TYPES.get(at)
            if allowed is None:
                continue
            assert rt in allowed, (
                f"os-engine infers root_type={rt!r} for account_type={at!r}, "
                f"which this map refuses")


class TestListAccounts:
    def test_list_with_company(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Account A", root_type="asset")
        seed_account(conn, cid, name="Account B", root_type="liability")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type=None, account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2

    def test_filter_by_root_type(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Asset Acc", root_type="asset")
        seed_account(conn, cid, name="Liability Acc", root_type="liability")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type="asset", account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search=None, limit=None, offset=None,
        ))
        for acct in result["accounts"]:
            assert acct["root_type"] == "asset"

    def test_search_by_name(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Accounts Receivable", root_type="asset",
                     account_type="receivable")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type=None, account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search="Receivable", limit=None, offset=None,
        ))
        assert result["total_count"] >= 1


class TestGetAccount:
    def test_get_by_id(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Cash", account_number="1001")
        result = call_action(mod.get_account, conn, ns(
            account_id=aid, as_of_date=None,
        ))
        assert is_ok(result)
        assert result["account"]["name"] == "Cash"

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_account, conn, ns(
            account_id="fake-id", as_of_date=None,
        ))
        assert is_error(result)

    def test_get_with_balance(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Cash", account_number="1001",
                           root_type="asset")
        result = call_action(mod.get_account, conn, ns(
            account_id=aid, as_of_date="2026-12-31",
        ))
        assert is_ok(result)
        acct = result["account"]
        assert "balance" in acct
        assert Decimal(acct["balance"]) == Decimal("0")


class TestFreezeUnfreezeAccount:
    def test_freeze(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        result = call_action(mod.freeze_account, conn, ns(account_id=aid))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 1

    def test_unfreeze(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        call_action(mod.freeze_account, conn, ns(account_id=aid))
        result = call_action(mod.unfreeze_account, conn, ns(account_id=aid))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 0

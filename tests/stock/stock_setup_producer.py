# SPDX-License-Identifier: GPL-3.0-only
"""Database producer harness for stock/2, composed with the frozen stock/1 utilities.

These are integration helpers, not pure units. Writes use real Core approvals;
starting books use the existing explicit bootstrap functions. No stock movement
or ledger posting is invented for a setup service.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Literal
from uuid import UUID

from sqlalchemy import MetaData, Table, inspect, select

import bookdiff
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core.common import Ctx, new_id
from erpclaw_core.contracts import Contract, Input, Preview, Result, Shown
from erpclaw_core.plugins import REGISTRY, Plugin, call_service
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t
from service_plugins import manifest
from stock_fixtures import Books, EXTRA_STOCK_ACCOUNTS
from stock_setup_fixtures import FixtureError, SERVICES

CONSUMER = "stocksetupuser"
WRITES = {"create_warehouse", "make_stock_item"}
PREVIEW_OF = {"create_warehouse": "preview_create_warehouse",
              "make_stock_item": "preview_make_stock_item"}


class SetupWrite(Input):
    service: Literal["create_warehouse", "make_stock_item"]
    input: dict


def _preview_call(ctx: Ctx, inp: SetupWrite) -> dict:
    return call_service(ctx, CONSUMER, f"stock.{PREVIEW_OF[inp.service]}", inp.input)


def _preview(ctx: Ctx, inp: SetupWrite) -> Preview:
    out = _preview_call(ctx, inp)
    shown = Shown(ctx.currency)
    shown.title(f"Stock setup fixture: {inp.service}")
    # Bind what the read service resolved, as well as the exact approved input.
    shown.line("Input", approvals.canonical(inp.input))
    shown.line("Preview", approvals.canonical(out))
    return Preview(shown.card(), approvals.canonical(out))


def _read_set(ctx: Ctx, inp: SetupWrite) -> list[tuple[str, str]]:
    out = _preview_call(ctx, inp)
    rows = [("company", ctx.company_id), *(tuple(x) for x in out["read_set"])]
    default = inp.input.get("default_warehouse")
    if default is not None:
        rows.append(("stock_warehouse", default))
    if inp.service == "create_warehouse":
        # Existing names/defaults are part of this setup choice too.
        rows.extend(("stock_warehouse", row["warehouse"]) for row in
                    call_service(ctx, CONSUMER, "stock.list_warehouses", {})["warehouses"])
    return rows


def _commit(ctx: Ctx, inp: SetupWrite, key: str) -> Result:
    return Result("Done.", {"out": call_service(ctx, CONSUMER, f"stock.{inp.service}", inp.input)})


SETUP_WRITE = Contract(
    "stock_setup_fixture_write", CONSUMER, "write", "Run one stock setup fixture through its approval.",
    SetupWrite, preview=_preview, commit=_commit, find_by_operation_key=lambda ctx, key: None,
    read_set=_read_set,
)


def consumer() -> Plugin:
    # stock_on_hand is the unchanged stock/1 read required by after.on_hand.
    return Plugin(manifest=manifest(
        CONSUMER, engine=">=0.0.2", after_core_revision="0015", depends={"stock": ">=0.1.1"},
        contracts={SETUP_WRITE.name: "write"},
        uses=[f"stock.{name}" for name in sorted(SERVICES | {"stock_on_hand"})]),
        kind="business-module", contracts=[SETUP_WRITE], artifact_digest="0" * 64)


def full_snapshot(engine) -> dict:
    """Reuse bookdiff and include its omitted control/migration tables as well.

    No table-wide exemption: every physical table must have a primary key and
    contributes to the same row comparison. No backup/restore operation is run.
    """
    out = bookdiff.snapshot(engine)
    with engine.connect() as conn:
        for name in sorted(set(inspect(conn).get_table_names()) - set(out)):
            table = Table(name, MetaData(), autoload_with=conn)
            pk = [c.name for c in table.primary_key.columns]
            assert pk, f"Cannot compare rows without a primary key: {name}"
            rows = [json.loads(line) for line in bookdiff.bk._dump(conn, table)]
            out[name] = {tuple(row[k] for k in pk): row for row in rows}
    return out


class SetupBooks(Books):
    """Extend only the starting-book fields/symbols added by stock/2."""

    def __init__(self, engine, case: dict):
        if case["before"]:
            raise FixtureError("setup fixtures must not contain stock movements")
        starting = copy.deepcopy(case)
        defaults = [key for key, row in starting["books"]["warehouses"].items()
                    if row.get("company_default", False)]
        if len(defaults) > 1:
            raise FixtureError("starting books have multiple default warehouses")
        if defaults:
            starting["books"]["default_warehouse"] = defaults[0]
        super().__init__(engine, starting)
        self.case = case
        with engine.begin() as conn:
            ctx = self.ctx(conn)
            self.ids["@acct:bank"] = ledger.account_by_number(ctx, "1100")["id"]
            parent = conn.execute(select(m.account.c.parent_id).where(
                m.account.c.id == self.ids["@acct:inv"])).scalar_one()
            for sym in case["books"].get("accounts", []):
                if sym not in self.ids:
                    number, name = EXTRA_STOCK_ACCOUNTS[sym]
                    self.ids[sym] = new_id()
                    conn.execute(m.account.insert().values(
                        id=self.ids[sym], company_id=self.company, number=number, name=name,
                        root_type="asset", account_type="stock", is_group=False,
                        parent_id=parent, normal_side="debit"))
            for sym in case["books"].get("service_items", []):
                assert sym not in self.ids
                self.ids[sym] = new_id()
                conn.execute(m.item.insert().values(
                    id=self.ids[sym], company_id=self.company,
                    name=sym.split(":", 1)[1].replace("_", " ").title(),
                    unit="unit", standard_rate="1", kind="service"))
            for sym in case["books"].get("frozen", []):
                result = conn.execute(m.account.update().where(
                    m.account.c.company_id == self.company, m.account.c.id == self.ids[sym]).values(frozen=True))
                assert result.rowcount == 1
        self.proposal = None
        self.proposed_snapshot = None
        self.read_digests = None
        self.started = self.finished = None

    def lookup(self, value: str) -> str:
        if value.startswith("@wh:") and value not in self.ids:
            if value.startswith("@wh:new#"):
                raise FixtureError("new warehouse symbols must be bound from the successful result")
            self.ids[value] = new_id()  # deliberately absent, never a fabricated warehouse
        return super().lookup(value)

    def snapshot(self) -> dict:
        return full_snapshot(self.engine)

    def read(self, service: str, inp: dict) -> dict:
        with self.engine.begin() as conn:
            return call_service(self.ctx(conn), CONSUMER, f"stock.{service}", self.resolve(inp))

    def call_setup(self) -> dict:
        service, inp = self.case["service"], self.resolve(self.case["input"])
        self.started = datetime.now(timezone.utc)
        if service not in WRITES:
            return self.read(service, inp)
        self.proposal = approvals.propose(
            self.engine, REGISTRY.contracts[SETUP_WRITE.name], {"service": service, "input": inp},
            company_id=self.company)
        assert self.proposal.status == "card", self.proposal
        self.proposed_snapshot = self.snapshot()
        with self.engine.connect() as conn:
            self.read_digests = [[table, key, approvals._row_digest(conn, table, key)]
                                 for table, key in sorted(self.expected_refs(self.proposed_snapshot))]
        result = approvals.approve(self.engine, self.proposal.approval_id, self.proposal.digest)
        self.finished = datetime.now(timezone.utc)
        assert result.status == "committed", (result.status, result.text, result.data)
        if service == "create_warehouse":
            self.ids["@wh:new#1"] = result.data["out"]["warehouse"]
        return result.data["out"]

    def assert_after(self) -> None:
        after = self.case["after"]
        if "warehouses" in after:
            assert self.read("list_warehouses", {}) == {"warehouses": self.resolve(after["warehouses"])}
        with self.engine.connect() as conn:
            if "stock_items" in after:
                got = {row["item_id"]: {"method": row["method"], "default_warehouse": row["default_warehouse_id"]}
                       for row in conn.execute(select(t.stock_item).where(
                           t.stock_item.c.company_id == self.company)).mappings()}
                expected = {self.resolve(sym): self.resolve(fields)
                            for sym, fields in after["stock_items"].items()}
                assert got == expected
            for sym, kind in after.get("kind", {}).items():
                assert conn.execute(select(m.item.c.kind).where(
                    m.item.c.company_id == self.company, m.item.c.id == self.resolve(sym))).scalar_one() == kind
        for sym, expected in after.get("on_hand", {}).items():
            assert self.read("stock_on_hand", {"item": self.resolve(sym)}) == self.resolve(expected)
        with self.engine.connect() as conn:
            assert stock.verify_stock(self.ctx(conn)) == []
        # The assertions themselves may not create even a head or control row.

    def _generated(self, row: dict, stamp: str, *, identifier: str | None = "id") -> dict:
        if identifier:
            assert str(UUID(row[identifier])) == row[identifier]
        when = datetime.fromisoformat(row[stamp].replace("Z", "+00:00"))
        assert self.started <= when <= self.finished
        return {k: row[k] for k in (identifier, stamp) if k is not None}

    def expected_refs(self, before: dict) -> set[tuple[str, str]]:
        """Fixture inputs independently name every row this setup card must bind."""
        inp = self.resolve(self.case["input"])
        required = {("company", self.company)}
        if self.case["service"] == "create_warehouse":
            required.add(("account", inp["account"]))
            required.update(("stock_warehouse", row["id"]) for row in before["stock_warehouse"].values()
                            if row["company_id"] == self.company)
        else:
            required.add(("item", inp["item"]))
            if inp.get("default_warehouse"):
                required.add(("stock_warehouse", inp["default_warehouse"]))
        return required

    def assert_effects(self, before: dict, after: dict) -> None:
        """Exact owned rows and allowed columns, never ignored bookkeeping tables."""
        if self.case["service"] not in WRITES or "refusal" in self.case:
            bookdiff.assert_changes(before, after, {})
            return
        proposal = self.proposal
        pending = self.proposed_snapshot["approval"][(proposal.approval_id,)]
        principal = next(row for row in before["principal"].values() if row["name"] == "owner")
        assert re.fullmatch(r"[0-9a-f]{32}", pending["nonce"])
        assert str(UUID(proposal.operation_key)) == proposal.operation_key
        expiry = datetime.fromisoformat(pending["expires_at"].replace("Z", "+00:00"))
        assert self.started + approvals.VALID_FOR <= expiry <= self.finished + approvals.VALID_FOR
        inp = self.resolve(self.case["input"])
        assert {(table, key) for table, key, _ in self.read_digests} == self.expected_refs(before)
        assert json.loads(pending["read_set_json"]) == self.read_digests
        expected_pending = {
            **self._generated(pending, "created_at"), "principal_id": principal["id"],
            "company_id": self.company, "conversation_id": None, "contract": SETUP_WRITE.name,
            "contract_version": "1.0.0", "policy_version": principal["grants_version"],
            "impl_version": f"{version('erpclaw-core')}+{'0' * 16}",
            "args_json": approvals.canonical({"service": self.case["service"], "input": inp}),
            "read_set_json": pending["read_set_json"], "card_json": approvals.canonical(proposal.card),
            "nonce": pending["nonce"], "digest": proposal.digest, "status": "pending",
            "operation_key": proposal.operation_key, "expires_at": pending["expires_at"],
            "decided_at": None, "approved_by": None, "approver_grants_version": None,
            "decision_json": None, "request_card_token": None,
        }
        assert pending == expected_pending
        bookdiff.assert_changes(before, self.proposed_snapshot, {"approval": {"added": [expected_pending]}})
        consumed = after["approval"][(proposal.approval_id,)]
        self._generated(consumed, "decided_at")
        expected_consumed = {**expected_pending, "status": "consumed", "approved_by": principal["id"],
                             "approver_grants_version": principal["grants_version"],
                             "decided_at": consumed["decided_at"]}
        assert consumed == expected_consumed
        output = self.resolve(self.case["output"])
        audits = [row for row in after["audit_log"].values() if row["approval_id"] == proposal.approval_id]
        assert len(audits) == 1
        audit = {**self._generated(audits[0], "at"), "company_id": self.company, "actor": "owner",
                 "action": f"commit:{SETUP_WRITE.name}", "document_type": None, "document_id": None,
                 "approval_id": proposal.approval_id,
                 "detail": json.dumps({"operation_key": proposal.operation_key, "out": output}, sort_keys=True)}
        assert audits[0] == audit
        outcome = after["operation_outcome"][(proposal.operation_key,)]
        expected_outcome = {**self._generated(outcome, "committed_at", identifier="key"),
                            "company_id": self.company, "approval_id": proposal.approval_id,
                            "card_digest": proposal.digest, "status": "committed",
                            "result_json": approvals.canonical(
                                {"text": "Done.", "data": {"out": output}, "figures": [], "amounts": []})}
        assert outcome == expected_outcome
        expected = {"approval": {"added": [expected_consumed]}, "audit_log": {"added": [audit]},
                    "operation_outcome": {"added": [expected_outcome]}}
        if self.case["service"] == "create_warehouse":
            row = after["stock_warehouse"][(output["warehouse"],)]
            position = 1 + max((r["position"] for r in before["stock_warehouse"].values()
                                if r["company_id"] == self.company), default=0)
            want = {**self._generated(row, "created_at"), "company_id": self.company,
                    "name": output["name"], "account_id": output["account"],
                    "is_default": output["company_default"], "position": position}
            assert row == want
            expected["stock_warehouse"] = {"added": [want]}
        else:
            rows = [row for row in after["stock_item"].values()
                    if row["company_id"] == self.company and row["item_id"] == output["item"]]
            assert len(rows) == 1
            want = {**self._generated(rows[0], "created_at"), "company_id": self.company,
                    "item_id": output["item"], "method": output["method"],
                    "default_warehouse_id": output.get("default_warehouse")}
            assert rows[0] == want
            expected["stock_item"] = {"added": [want]}
            assert before["item"][(output["item"],)]["kind"] == "service"
            expected["item"] = {"changed": [({"id": output["item"]}, {"kind": "stock"})]}
        bookdiff.assert_changes(before, after, expected)

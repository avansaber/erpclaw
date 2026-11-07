"""Explicit in-memory Core approval boundary for pure producer units.

This records an already approved invocation, not database approval qualification.
No guard or committing query is patched. Pending/completed fixture cards are never
issued a scope; business reads and writes still use each test's recording boundary.
"""
from functools import wraps
from types import SimpleNamespace

from erpclaw_core import approval_scope
from erpclaw_core.plugins import REGISTRY


class RecordedApproval:
    def __init__(self, ctx, card, outcome):
        self.ctx, self.card, self.outcome = ctx, card, outcome
        self.enabled = True

    def wrap(self, monkeypatch, module, name):
        original = getattr(module, name)

        @wraps(original)
        def invoke(ctx, *args, **kwargs):
            card = self.card()
            if isinstance(card, dict):
                card = SimpleNamespace(**card)
            if (not self.enabled or ctx is not self.ctx or card is None
                    or card.status != "consumed" or self.outcome() is not None
                    or card.company_id != ctx.company_id):
                return original(ctx, *args, **kwargs)
            owner = REGISTRY.contract_plugin.get(card.contract)
            key = args[1] if len(args) > 1 and isinstance(args[1], str) else "recorded-operation"
            conn = ctx.conn
            root = SimpleNamespace(connection=conn, is_active=True)
            effect = SimpleNamespace(connection=conn, is_active=True, _previous_nested=None)
            # Only transaction identity is recorded; no SQL/authority operation is mocked.
            with monkeypatch.context() as recording:
                for attr, value in {"closed": False, "invalidated": False,
                                    "get_transaction": lambda: root,
                                    "get_nested_transaction": lambda: effect}.items():
                    recording.setattr(conn, attr, value, raising=False)
                def commit(actual, unused, operation_key):
                    return original(actual, *args, **kwargs)
                entry = approval_scope._wrap_contract(commit, owner, card.contract, "commit")
                with approval_scope._issue(ctx, "approval", card.contract, key, owner, effect):
                    return entry(ctx, None, key)
        monkeypatch.setattr(module, name, invoke)

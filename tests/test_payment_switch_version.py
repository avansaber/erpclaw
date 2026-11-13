"""The immutable approval boundary is never above the release manifest."""
from packaging.version import Version
from sqlalchemy import select, update

from erpclaw_core import approvals, models as m
from erpclaw_core.approvals import PAYMENT_SWITCH_VERSION
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import MANIFEST, tables as t
from bookdiff import changes, snapshot


def test_switch_version_matches_manifest():
    # dispatcher 1's ruling, 12 Nov 2026: the switch boundary is immutable.
    assert PAYMENT_SWITCH_VERSION == '0.0.3'
    assert Version(MANIFEST.version) >= Version(PAYMENT_SWITCH_VERSION)


def _payment_card_at_version(engine, version):
    card = approvals.propose(engine, REGISTRY.contracts['record_payment'],
                             {'customer': 'Hooli', 'amount': '100'})
    with engine.begin() as conn:
        conn.execute(update(m.approval).where(m.approval.c.id == card.approval_id)
                     .values(contract_version=version))
    return card


def test_payment_at_switch_boundary_commits_once(engine):
    card = _payment_card_at_version(engine, '0.0.3')
    assert approvals.approve(engine, card.approval_id, card.digest).status == 'committed'
    with engine.connect() as conn:
        assert len(conn.execute(select(t.payment).where(t.payment.c.operation_key == card.operation_key)).all()) == 1
    approvals.approve(engine, card.approval_id, card.digest)
    with engine.connect() as conn:
        assert len(conn.execute(select(t.payment).where(t.payment.c.operation_key == card.operation_key)).all()) == 1


def test_payment_before_switch_stays_planned(engine):
    card = _payment_card_at_version(engine, '0.0.2')
    before = snapshot(engine)
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == 'superseded' and result.data['code'] == 'planned'
    assert set(changes(before, snapshot(engine))) == {'approval'}

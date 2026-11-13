"""The email plugin's skeleton: its manifest, its two message slots
and no operation offered to chat; it loads beside the folder connector; new books keep the folder connector, and the
SMTP plugin is the owner's choice."""
from __future__ import annotations

import pytest
from sqlalchemy import select

import erpclaw_email
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from erpclaw_core.outbox import OutboundMessage, SendOutcome
from erpclaw_core.plugins import REGISTRY, install_plugin, uninstall_plugin
from erpclaw_core.setup import create_company


MSG = OutboundMessage(id="6d1f3c2e-0000-4000-8000-000000000001", company_id="c0ffee00-0000-4000-8000-000000000001",
                      message_id="<6d1f3c2e-0000-4000-8000-000000000001@stark.test>", recipient="accounts@acme.test",
                      from_address="books@stark.test", from_name="Stark Books", reply_to=None,
                      subject="Invoice INV-2026-00007", body="Please find the invoice attached.\n")


def test_the_manifest():
    mf = erpclaw_email.MANIFEST
    assert (mf.name, mf.distribution, mf.licence) == ("email", "erpclaw-email", "GPL-3.0-only")
    assert set(mf.slots) == {"send_message", "message_sender"}
    assert all(d.cardinality == "one" for d in mf.slots.values())
    assert mf.contracts == {"update_email_sender": "write"} and mf.owned_tables == ["email_sender"] and mf.metrics == []
    assert mf.after_core_revision == "0009"
    assert erpclaw_email.PLUGIN.kind == "connector" and [(c.name, c.planned) for c in erpclaw_email.PLUGIN.contracts] == [("update_email_sender", None)]


def test_it_loads_beside_the_folder_connector():
    r = REGISTRY  # registered: the engine's own loaded set holds it, through its entry point
    assert r.plugins["email"] is erpclaw_email.PLUGIN or r.plugins["email"].name == "email"
    assert {"email", "email_folder"} <= set(r.plugins)
    assert set(r.slots["send_message"]) == {"email"} and set(r.slots["message_sender"]) == {"email"}


def test_the_sender_slot_refuses_until_a_sender_is_set_up(engine):
    from erpclaw_core.common import company_ctx

    with engine.begin() as conn:
        uninstall_plugin(conn, "email_folder")
    install_plugin(engine, "email")
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        assert conn.execute(select(erpclaw_email.tables.email_sender)).first() is None
        with pytest.raises(Refusal) as e:
            erpclaw_email.PLUGIN.slots["message_sender"](ctx)
    assert str(e.value) == "No email account is set up for this company." and e.value.code == "no_email_sender"


def test_the_send_slot_sends_nothing_yet():
    out = erpclaw_email.PLUGIN.slots["send_message"](None, MSG, lambda: pytest.fail("the skeleton asked for a mark"))
    assert isinstance(out, SendOutcome) and (out.stage, out.kind) == ("before_data", "permanent")


# --- books with the SMTP plugin loaded: new books keep the folder connector ----------------------------------------

@pytest.fixture
def email_loaded(engine):
    """The demo books seeded as today, then the SMTP plugin loaded beside the folder connector."""
    old = [REGISTRY.plugins[n] for n in REGISTRY.order]
    REGISTRY.fill([q for q in old if q.name != "email"] + [erpclaw_email.PLUGIN])  # registered now: loaded once
    try:
        yield engine
    finally:
        REGISTRY.fill(old)


def test_new_books_keep_the_folder_connector(email_loaded):
    create_company(email_loaded, "Wayne Books")  # installs every loaded plugin these books lack
    with email_loaded.connect() as conn:
        installed = set(conn.execute(select(m.plugin_install.c.name)).scalars())
    assert "email_folder" in installed and "email" not in installed


def test_the_smtp_plugin_is_the_owners_choice_and_refused_beside_the_folder(email_loaded):
    create_company(email_loaded, "Wayne Books")
    with pytest.raises(Refusal) as e:
        install_plugin(email_loaded, "email")
    assert e.value.code == "message_connector_installed"
    assert str(e.value) == "These books already send messages through email_folder; disable it first."


def test_another_company_keeps_the_owners_chosen_connector(email_loaded):
    """With the folder connector uninstalled and the SMTP plugin installed by the owner, creating another company does
    not try to bring the folder connector back (one message connector per set of books)."""
    with email_loaded.begin() as conn:
        uninstall_plugin(conn, "email_folder")
    install_plugin(email_loaded, "email")
    create_company(email_loaded, "Wayne Books")
    with email_loaded.connect() as conn:
        installed = set(conn.execute(select(m.plugin_install.c.name)).scalars())
    assert "email" in installed and "email_folder" not in installed

"""Email plugin: sends the outbox's messages through the company's own mail server over SMTP with verified TLS.
It fills core's send_message and message_sender slots and offers the owner a sender-settings
approval card. New books keep the folder connector; this plugin is the owner's choice, installed after the
folder connector is disabled (one message connector per set of books)."""
from __future__ import annotations

from typing import Callable

from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, SlotDecl, Source
from erpclaw_core.outbox import OutboundMessage, ReadCtx, SendOutcome
from erpclaw_core.plugins import Plugin

from .smtp import send_message as _smtp_send_message
from .settings import UPDATE_EMAIL_SENDER, UpdateEmailSender, message_sender
from . import tables


def send_message(settings: ReadCtx, msg: OutboundMessage, mark_data: Callable[[], bool]) -> SendOutcome:
    """Core's three-argument slot, with the company's secret resolved at send time."""
    return _smtp_send_message(settings, msg, mark_data)


MANIFEST = Manifest(
    name="email", version="0.0.1", publisher=PUBLISHER, distribution="erpclaw-email", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.1", after_core_revision="0009",
    depends={}, conflicts=[], owned_tables=[tables.email_sender.name],
    contracts={UPDATE_EMAIL_SENDER.name: UPDATE_EMAIL_SENDER.effect}, calls=[], events=Events(emits=[], consumes=[]),
    slots={"send_message": SlotDecl(cardinality="one", selection="the company's own mail server, over SMTP"),
           "message_sender": SlotDecl(cardinality="one", selection="the company's email sender settings")},
    voucher_types=[], capabilities=Capabilities(network=["smtp"], files=[]), metrics=[],
)

PLUGIN = Plugin(manifest=MANIFEST, kind="connector",
                contracts=[UPDATE_EMAIL_SENDER],
                slots={"send_message": send_message, "message_sender": message_sender})

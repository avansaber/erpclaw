"""Email plugin: sends the outbox's messages through the company's own mail server over SMTP with verified TLS.
It fills core's send_message and message_sender slots (plugin contract v2) and offers no
operation to chat. New books keep the folder connector; this plugin is the owner's choice, installed after the
folder connector is disabled (one message connector per set of books)."""
from __future__ import annotations

from typing import Callable

from erpclaw_core.common import Refusal
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, SlotDecl, Source
from erpclaw_core.outbox import OutboundMessage, ReadCtx, SendOutcome, SenderSnapshot
from erpclaw_core.plugins import Plugin


def send_message(settings: ReadCtx, msg: OutboundMessage, mark_data: Callable[[], bool]) -> SendOutcome:
    """Not connected yet: nothing is sent and no data mark is asked for."""
    return SendOutcome(stage="before_data", kind="permanent", code=None, text="")


def message_sender(ctx) -> SenderSnapshot:
    raise Refusal("No email account is set up for this company.", "no_email_sender")


MANIFEST = Manifest(
    name="email", version="0.0.1", publisher=PUBLISHER, distribution="erpclaw-email", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.1", after_core_revision="0009",
    depends={}, conflicts=[], owned_tables=[], contracts={}, calls=[], events=Events(emits=[], consumes=[]),
    slots={"send_message": SlotDecl(cardinality="one", selection="the company's own mail server, over SMTP"),
           "message_sender": SlotDecl(cardinality="one", selection="the company's email sender settings")},
    voucher_types=[], capabilities=Capabilities(network=["smtp"], files=[]), metrics=[],
)

PLUGIN = Plugin(manifest=MANIFEST, kind="connector",
                slots={"send_message": send_message, "message_sender": message_sender})

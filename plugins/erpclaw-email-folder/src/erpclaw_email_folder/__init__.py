"""Folder email connector: fills the core `deliver_message` slot by
writing a standards-shaped .eml file to var/mail/ instead of sending it.
A real SMTP connector fills the same slot with the same signature."""
from __future__ import annotations

import os
from email.message import EmailMessage
from pathlib import Path

from erpclaw_core.db import data_root
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, SlotDecl, Source
from erpclaw_core.plugins import Plugin


def mail_dir() -> Path:
    return Path(os.environ.get("ERPCLAW_MAIL_DIR") or data_root() / "mail")


def deliver_message(row: dict) -> str:
    msg = EmailMessage()
    msg["To"] = row["recipient"]
    msg["From"] = "books@stark.example"
    msg["Subject"] = row["subject"]
    msg.set_content(row["body"])
    d = mail_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{row['id']}.eml"
    path.write_bytes(bytes(msg))
    return str(path)


MANIFEST = Manifest(
    name="email_folder", version="0.0.1", publisher=PUBLISHER, distribution="erpclaw-email-folder",
    licence="GPL-3.0-only", source=Source(repository="workspace", commit="workspace"), engine=">=0.0.1",
    after_core_revision="0001", depends={}, conflicts=[], owned_tables=[], contracts={}, calls=[],
    events=Events(emits=[], consumes=[]),
    slots={"deliver_message": SlotDecl(cardinality="one", selection="the only message connector in the books")},
    voucher_types=[], capabilities=Capabilities(network=[], files=["mail"]), metrics=[],
)

PLUGIN = Plugin(manifest=MANIFEST, kind="connector", slots={"deliver_message": deliver_message})

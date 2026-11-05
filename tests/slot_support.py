"""A test's own slot filler, in two forms. For registration tests, the loaded plugins with their mail slots taken off
(every plugin and its tables kept, so table ownership stays whole). For tests that run a slot, the registered filler's
callable replaced for the test, after checking the replacement takes the same parameters, so a connector that would
not register in production cannot stand in for one in a test."""
from __future__ import annotations

import dataclasses
import inspect

from erpclaw_core import plugins
from erpclaw_core.plugins import Plugin

MAIL_SLOTS = ("send_message", "message_sender")


def _without_mail_slots(p: Plugin) -> Plugin:
    if not set(MAIL_SLOTS) & set(p.slots):
        return p
    manifest = p.manifest.model_copy(update={"slots": {k: v for k, v in p.manifest.slots.items()
                                                       if k not in MAIL_SLOTS}})
    return dataclasses.replace(p, manifest=manifest, slots={k: v for k, v in p.slots.items() if k not in MAIL_SLOTS})


def loaded_without_mailers() -> list[Plugin]:
    """The loaded plugins, each kept, with the send_message and message_sender slots taken off, so a test's own mail
    connector is the only one; the folder connector keeps deliver_message."""
    return [_without_mail_slots(plugins.REGISTRY.plugins[n]) for n in plugins.REGISTRY.order]


def _parameters(fn) -> list[tuple[str, object]]:
    return [(p.name, p.kind) for p in inspect.signature(fn).parameters.values()]


def replace_filler(monkeypatch, slot: str, fn) -> str:
    """Replace the registered filler of `slot` with `fn` for this test (undone at its end); returns that plugin's
    name. `fn` must take the registered filler's parameters, by name and kind."""
    registry = plugins.REGISTRY  # read now: a test may have put a registry of its own in place
    [(plugin, real)] = registry.slots[slot].items()
    if _parameters(fn) != _parameters(real):
        raise TypeError(f"a {slot} filler takes {inspect.signature(real)}; this one takes {inspect.signature(fn)}")
    monkeypatch.setitem(registry.slots[slot], plugin, fn)
    return plugin

"""Book-diff harness: what an operation changed in every table, row by row,
checked against expectations the test writes itself, independently of the
engine's code (only the canonical row form is shared, from the backup format)."""
from __future__ import annotations

import json

from erpclaw_core import backup as bk

# A turn's control state in the books, not a business table: every turn fences its conversation there
CONTROL = {"conversation_fence"}


def snapshot(engine) -> dict[str, dict[tuple, dict]]:
    """Every table of the books but CONTROL, as canonical rows keyed by primary key."""
    out: dict[str, dict[tuple, dict]] = {}
    with engine.connect() as conn:
        for t in bk.books_tables(conn):
            if t.name in CONTROL:
                continue
            pk = [c.name for c in t.primary_key.columns]
            out[t.name] = {tuple(r[c] for c in pk): r for r in (json.loads(x) for x in bk._dump(conn, t))}
    return out


def changes(before: dict, after: dict) -> dict[str, dict]:
    """Per table: rows added, removed, and changed (old and new), only where something changed."""
    out: dict[str, dict] = {}
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name, {}), after.get(name, {})
        d = {"added": [a[k] for k in sorted(a.keys() - b.keys(), key=str)],
             "removed": [b[k] for k in sorted(b.keys() - a.keys(), key=str)],
             "changed": [(b[k], a[k]) for k in sorted(a.keys() & b.keys(), key=str) if a[k] != b[k]]}
        if any(d.values()):
            out[name] = d
    return out


def _matches(row: dict, partial: dict) -> bool:
    return all(row.get(k) == v for k, v in partial.items())


def assert_changes(before: dict, after: dict, expected: dict[str, dict]) -> None:
    """Every table not named in `expected` is unchanged; in a named table the
    added and removed rows match the given partial rows one to one, and each
    changed row matches a (key partial, {column: new value}) pair, its other
    columns unchanged."""
    got = changes(before, after)
    extra = sorted(set(got) - set(expected))
    assert not extra, f"tables changed that should not have: {extra}: {[got[t] for t in extra]}"
    for name, want in expected.items():
        have = got.get(name, {"added": [], "removed": [], "changed": []})
        for kind in ("added", "removed"):
            rows, partials = list(have[kind]), list(want.get(kind, []))
            assert len(rows) == len(partials), f"{name}: {len(rows)} rows {kind}, expected {len(partials)}: {rows}"
            for p in partials:
                hit = next((r for r in rows if _matches(r, p)), None)
                assert hit is not None, f"{name}: no {kind} row matches {p}; {kind}: {rows}"
                rows.remove(hit)
        pairs, wants = list(have["changed"]), list(want.get("changed", []))
        assert len(pairs) == len(wants), f"{name}: {len(pairs)} rows changed, expected {len(wants)}: {pairs}"
        for key, new in wants:
            hit = next(((o, n) for o, n in pairs if _matches(o, key)), None)
            assert hit is not None, f"{name}: no changed row matches {key}"
            old, cur = hit
            diff = {k: cur[k] for k in cur if cur[k] != old.get(k)}
            assert diff == new, f"{name}: row {key} changed {diff}, expected {new}"
            pairs.remove(hit)

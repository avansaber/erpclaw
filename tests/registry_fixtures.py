"""Real registry validation for synthetic core contracts on isolated core tables."""

from sqlalchemy import MetaData

from erpclaw_core import db, models, plugins


def core_registry(monkeypatch, manifest, contract):
    """Keep imported plugin tables out of a core-only fixture's registry check."""
    metadata = MetaData()
    for name in sorted(models.CORE_TABLES):
        db.metadata.tables[name].to_metadata(metadata)

    registry = plugins.Registry()
    # _check_tables imports db.metadata at call time. Restore it immediately
    # after fill so other fixtures retain the suite's complete table inventory.
    with monkeypatch.context() as patch:
        patch.setattr(db, "metadata", metadata)
        registry.fill([plugins.Plugin(manifest, "core", contracts=[contract])])
    return registry

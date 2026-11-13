"""Authentication changes made when restored books are published."""
from sqlalchemy import func

# The writes are called only by restore on its owner connection. Comparison is read-only.
OWNER_ONLY = True

# None as a replacement means the restore time; other values are literal.
AUTH_INVALIDATIONS = {
    'principal_session': ('ended_at', {'ended_at': None, 'ended_why': 'restored'}),
    'principal_sign_in': ('used_at', {'used_at': None}),
}


def invalidate_authentication(conn):
    from . import models

    def replacement(name, column):
        value = AUTH_INVALIDATIONS[name][1][column]
        return func.now() if value is None else value

    conn.execute(models.principal_session.update().where(models.principal_session.c.ended_at.is_(None)).values(
        ended_at=replacement('principal_session', 'ended_at'),
        ended_why=replacement('principal_session', 'ended_why')))
    conn.execute(models.principal_sign_in.update().where(models.principal_sign_in.c.used_at.is_(None)).values(
        used_at=replacement('principal_sign_in', 'used_at')))


def matches_restored_authentication(name, old, new):
    """Compare a row with its restore result, including all unchanged columns."""
    open_column, replacements = AUTH_INVALIDATIONS[name]
    if old[open_column] is not None:
        return old == new
    for column in old:
        if column not in replacements:
            if old[column] != new[column]:
                return False
        elif replacements[column] is None:
            if new[column] is None:
                return False
        elif new[column] != replacements[column]:
            return False
    return True

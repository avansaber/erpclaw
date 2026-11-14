from erpclaw_assistant import store
from erpclaw_core import principals


def owner_check(engine):
    with engine.connect() as conn:
        owner_id = principals.owner(conn)["id"]
    return lambda conversation_id: store.conversation_owner(conversation_id) == owner_id

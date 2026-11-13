from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall


def assistant(engine, operation, **args):
    return Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', operation, args)])] * 8))


def invoice(engine, customer, lines):
    return assistant(engine, 'create_invoice', customer=customer, items=lines)


def card(events):
    assert events[-1]['type'] == 'card', events
    return events[-1]

"""Ephemeral protocol fields; ordinary JSON projections never include them."""

import json

from metric_pilot.data.common import DataError


class State:
    def __init__(self, value):
        self.value = value
        self.active = True

    def __deepcopy__(self, memo):
        return self


class ProtocolMessage(dict):
    pass


def bind(message, original):
    result = ProtocolMessage(message)
    result.protocol = getattr(original, "protocol", None)
    return result


def extra_bytes(message):
    state = getattr(message, "protocol", None)
    if state and state.active and state.value is not None:
        return len(json.dumps({"reasoning_content": state.value}, ensure_ascii=False).encode())
    return 0


def hydrate(messages):
    result = []
    for message in messages:
        item = dict(message)
        state = getattr(message, "protocol", None)
        if state is not None:
            if not state.active:
                raise DataError("MODEL_PROTOCOL_STATE_MISSING")
            if state.value is not None:
                item["reasoning_content"] = state.value
        elif message.get("tool_calls"):
            raise DataError("MODEL_PROTOCOL_STATE_MISSING")
        result.append(item)
    return result

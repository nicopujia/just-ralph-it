import json
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

type Round = Iterable[SimpleNamespace]


class _Item:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        return self.value


class _Stream:
    def __init__(self, events: Round) -> None:
        self.events = events

    def __enter__(self) -> Round:
        return self.events

    def __exit__(self, *_: object) -> None:
        return None


class _Responses:
    def __init__(self, rounds: Iterable[Round]) -> None:
        self.rounds = iter(rounds)
        self.inputs: list[object] = []

    def create(self, **options: object) -> _Stream:
        self.inputs.append(options["input"])
        return _Stream(next(self.rounds))


class FakeClient:
    def __init__(self, rounds: Iterable[Round]) -> None:
        self.responses = _Responses(rounds)


def response(*outputs: dict[str, Any]) -> Round:
    events = [
        SimpleNamespace(type="response.output_item.done", output_index=index, item=_Item(output))
        for index, output in enumerate(outputs)
    ]
    events.append(SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=None)))
    return events


def failure(message: str) -> Round:
    return [SimpleNamespace(type="error", message=message)]


def call(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def reply(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}

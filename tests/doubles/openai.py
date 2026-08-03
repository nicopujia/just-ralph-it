import json
from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any, Self, cast

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


class _ParsedStream:
    def __init__(self, parsed: object) -> None:
        self.events = cast("Round", parsed) if isinstance(parsed, list) else ()
        self.response = SimpleNamespace(output_parsed=None if self.events else parsed, output_text="")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[SimpleNamespace]:
        return iter(self.events)

    def get_final_response(self) -> SimpleNamespace:
        return self.response


class _Responses:
    def __init__(self, rounds: Iterable[Round], parsed: Iterable[object]) -> None:
        self.rounds = iter(rounds)
        self.parsed = iter(parsed)
        self.inputs: list[object] = []

    def create(self, **options: object) -> _Stream:
        self.inputs.append(options["input"])
        return _Stream(next(self.rounds))

    def stream(self, **options: object) -> _ParsedStream:
        self.inputs.append(options["input"])
        return _ParsedStream(next(self.parsed))


class FakeClient:
    def __init__(self, rounds: Iterable[Round], *, parsed: Iterable[object] = ()) -> None:
        self.responses = _Responses(rounds, parsed)


def response(*outputs: dict[str, Any]) -> Round:
    events = [
        SimpleNamespace(type="response.output_item.done", output_index=index, item=_Item(output))
        for index, output in enumerate(outputs)
    ]
    events.append(SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=None)))
    return events


def failure(message: str) -> Round:
    return [SimpleNamespace(type="error", message=message)]


def incomplete_response(reason: str | None) -> Round:
    details = SimpleNamespace(reason=reason) if reason else None
    return [SimpleNamespace(type="response.incomplete", response=SimpleNamespace(incomplete_details=details))]


def failed_response(error: str) -> Round:
    return [SimpleNamespace(type="response.failed", response=SimpleNamespace(error=error))]


def call(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def reply(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def streamed_reply(text: str) -> Round:
    return [SimpleNamespace(type="response.output_text.delta", delta=text), *response(reply(text))]

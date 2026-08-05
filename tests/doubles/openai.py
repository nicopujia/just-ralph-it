import json
from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any, Self, cast

import httpx
from openai import APIConnectionError, BadRequestError, OpenAIError, RateLimitError

type Round = Iterable[SimpleNamespace]

REQUEST = httpx.Request("POST", "https://provider.test/responses")


def response(*outputs: dict[str, Any]) -> Round:
    events = [
        SimpleNamespace(type="response.output_item.done", output_index=index, item=_Item(output))
        for index, output in enumerate(outputs)
    ]
    events.append(SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=None)))
    return events


def streamed_reply(text: str) -> Round:
    return [_delta(text), *response(reply(text))]


def partial_reply(text: str) -> Round:
    return [_delta(text)]


def reasoning(text: str, event_type: str) -> Round:
    return [SimpleNamespace(type=event_type, delta=text), *response()]


def interrupted_reply(text: str) -> Round:
    yield _delta(text)
    raise rate_limited()


def rate_limited(hint: str | None = None, code: str | None = None) -> RateLimitError:
    headers = {} if hint is None else {"retry-after-ms": hint}
    response = httpx.Response(429, headers=headers, request=REQUEST)
    return RateLimitError("Rate limit reached on tokens per min (TPM).", response=response, body={"code": code})


def disconnection() -> APIConnectionError:
    return APIConnectionError(request=REQUEST)


def rejection() -> BadRequestError:
    response = httpx.Response(400, request=REQUEST)
    return BadRequestError("Unknown model.", response=response, body=None)


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


class FakeClient:
    def __init__(self, rounds: Iterable[Round | OpenAIError], *, parsed: Iterable[object] = ()) -> None:
        self.responses = _Responses(rounds, parsed)


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", output_index=0, delta=text)


class _Responses:
    def __init__(self, rounds: Iterable[Round | OpenAIError], parsed: Iterable[object]) -> None:
        self.rounds = iter(rounds)
        self.parsed = iter(parsed)
        self.inputs: list[object] = []
        self.options: list[dict[str, object]] = []

    def create(self, **options: object) -> "_Stream":
        return _Stream(cast("Round", self._serve(self.rounds, options)))

    def stream(self, **options: object) -> "_ParsedStream":
        return _ParsedStream(self._serve(self.parsed, options))

    def _serve(self, source: Iterator[object], options: dict[str, object]) -> object:
        self.inputs.append(options["input"])
        self.options.append(options)
        served = next(source)
        if isinstance(served, OpenAIError):
            raise served
        return served


class _ParsedStream:
    def __init__(self, parsed: object) -> None:
        self.events = cast("Round", parsed) if isinstance(parsed, list) else ()
        # Like the SDK, aggregate the text of completed message items.
        output_text = "".join(
            content["text"]
            for event in self.events
            if event.type == "response.output_item.done" and event.item.to_dict()["type"] == "message"
            for content in event.item.to_dict()["content"]
        )
        self.response = SimpleNamespace(output_parsed=None if self.events else parsed, output_text=output_text)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[SimpleNamespace]:
        return iter(self.events)

    def get_final_response(self) -> SimpleNamespace:
        return self.response


class _Stream:
    def __init__(self, events: Round) -> None:
        self.events = events

    def __enter__(self) -> Round:
        return self.events

    def __exit__(self, *_: object) -> None:
        return None


class _Item:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        return self.value

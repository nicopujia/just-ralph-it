import json
from collections.abc import Iterable, Iterator
from threading import Event
from types import SimpleNamespace
from typing import Any, Self, cast

import httpx
from openai import APIConnectionError, BadRequestError, InternalServerError, OpenAIError, RateLimitError

type Round = Iterable[SimpleNamespace]

BASE_URL = "https://provider.test/v1"
RATE_LIMIT_MESSAGE = "Rate limit reached on tokens per min (TPM)."
REQUEST = httpx.Request("POST", f"{BASE_URL}/responses")


def response(*outputs: dict[str, Any], input_tokens: int | None = None) -> Round:
    events = [
        SimpleNamespace(type="response.output_item.done", output_index=index, item=_Item(output))
        for index, output in enumerate(outputs)
    ]
    # A provider states what a call spent once, as it completes, and
    # some state nothing at all.
    usage = None if input_tokens is None else SimpleNamespace(input_tokens=input_tokens)
    events.append(SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=usage)))
    return events


def streamed_reply(text: str) -> Round:
    return [_delta(text), *response(reply(text))]


def partial_reply(text: str) -> Round:
    return [_delta(text)]


def thought(text: str, event_type: str = "response.reasoning_summary_text.delta") -> SimpleNamespace:
    return SimpleNamespace(type=event_type, delta=text)


def reasoning(text: str, event_type: str) -> Round:
    return [thought(text, event_type), *response()]


def interrupted_reply(text: str) -> Round:
    yield _delta(text)
    raise rate_limited()


# The provider dropping the call after the model had begun thinking
# out loud, so whatever it said is already on the reader's screen.
def interrupted_thinking(text: str) -> Round:
    yield thought(text)
    raise rate_limited()


# A stop pressed while a response is still streaming. Nothing follows
# the event the stop is read on, so a run that pulls another event is
# one still waiting for a response it was told to abandon.
def stopped_stream(cancelled: Event) -> Round:
    yield _delta("{")
    cancelled.set()
    yield _delta('"outcome":')
    raise AssertionError("A stopped stream must be closed rather than read to its end.")


# A stop pressed while the model is still thinking out loud, which is
# where the minutes of a structured call are spent.
def stopped_thinking(cancelled: Event) -> Round:
    yield thought("Weighing ")
    cancelled.set()
    yield thought("the options.")
    raise AssertionError("A stopped stream must be closed rather than read to its end.")


def rate_limited(hint: str | None = None, code: str | None = None) -> RateLimitError:
    headers = {} if hint is None else {"retry-after-ms": hint}
    response = httpx.Response(429, headers=headers, request=REQUEST)
    body = {"message": RATE_LIMIT_MESSAGE, "code": code}
    return RateLimitError(f"Error code: 429 - {{'error': {body!r}}}", response=response, body=body)


# What the provider library raises for anything the transport did,
# with the transport's own account of it chained behind, exactly as
# the library chains it.
def disconnection(cause: Exception | None = None) -> APIConnectionError:
    error = APIConnectionError(request=REQUEST)
    error.__cause__ = cause
    return error


def rejection(message: str = "Unknown model.") -> BadRequestError:
    response = httpx.Response(400, request=REQUEST)
    # The library hands the exception the object under `error`, which
    # is where a provider spells out what it refused and why.
    return BadRequestError(
        f"Error code: 400 - {{'error': {{'message': {message!r}}}}}",
        response=response,
        body={"message": message, "type": "invalid_request_error"},
    )


# A gateway standing between JRI and the provider, answering for
# itself in a body of no shape the provider library knows.
def bad_gateway(body: str) -> InternalServerError:
    response = httpx.Response(502, request=REQUEST)
    return InternalServerError(body, response=response, body=body)


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
        # A real client normalizes what the settings named into the
        # address it sends to, and a failure names that address.
        self.base_url = httpx.URL(f"{BASE_URL}/")


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
        self.parsed = parsed
        # A stream the run abandons has no end to read, so the events
        # arrive one at a time rather than as a list read up front.
        self.events = cast("Round", parsed) if isinstance(parsed, list | Iterator) else ()
        self.delivered: list[SimpleNamespace] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for event in self.events:
            self.delivered.append(event)
            yield event

    def get_final_response(self) -> SimpleNamespace:
        # Like the SDK, aggregate the text of completed message items.
        output_text = "".join(
            content["text"]
            for event in self.delivered
            if event.type == "response.output_item.done" and event.item.to_dict()["type"] == "message"
            for content in event.item.to_dict()["content"]
        )
        return SimpleNamespace(output_parsed=None if self.events else self.parsed, output_text=output_text)


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

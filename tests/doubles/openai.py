import json
from collections.abc import Iterable, Iterator
from threading import Event
from types import SimpleNamespace
from typing import Any, Self, cast

import httpx
from openai import APIConnectionError, BadRequestError, InternalServerError, OpenAIError, RateLimitError
from openai.types.responses import ResponseError
from pydantic import BaseModel

type Round = Iterable[SimpleNamespace]

BASE_URL = "https://provider.test/v1"
RATE_LIMIT_MESSAGE = "Rate limit reached on tokens per min (TPM)."
REQUEST = httpx.Request("POST", f"{BASE_URL}/responses")


def response(*outputs: dict[str, Any], input_tokens: int | None = None, cached_tokens: int = 0) -> Round:
    events = [
        SimpleNamespace(type="response.output_item.done", output_index=index, item=_Item(output))
        for index, output in enumerate(outputs)
    ]
    # A provider reports what a call spent one time, on the event that completes it. Some providers report nothing.
    # The part of the input that came from the cache stands in the details of the input.
    usage = (
        None
        if input_tokens is None
        else SimpleNamespace(
            input_tokens=input_tokens, input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens)
        )
    )
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


# The provider drops the call after the model starts to think out loud. The reader already sees that text.
def interrupted_thinking(text: str) -> Round:
    yield thought(text)
    raise rate_limited()


# The user presses stop while a response still streams. Nothing follows the event that the run reads the stop on.
# A run that pulls one more event is a run that still waits for a response that it must abandon.
def stopped_stream(cancelled: Event) -> Round:
    yield _delta("{")
    cancelled.set()
    yield _delta('"outcome":')
    raise AssertionError("A stopped stream must be closed rather than read to its end.")


# The user presses stop while the model still thinks out loud. A structured call spends its minutes in that stage.
def stopped_thinking(cancelled: Event) -> Round:
    yield thought("Weighing ")
    cancelled.set()
    yield thought("the options.")
    raise AssertionError("A stopped stream must be closed rather than read to its end.")


# A provider asks for a delay in one of two headers. `retry-after-ms` counts milliseconds, `retry-after` counts
# seconds. A provider sends the one it prefers, and JRI reads both.
def rate_limited(
    *, milliseconds: str | None = None, seconds: str | None = None, code: str | None = None
) -> RateLimitError:
    hints = {"retry-after-ms": milliseconds, "retry-after": seconds}
    headers = {header: value for header, value in hints.items() if value is not None}
    response = httpx.Response(429, headers=headers, request=REQUEST)
    body = {"message": RATE_LIMIT_MESSAGE, "code": code}
    return RateLimitError(f"Error code: 429 - {{'error': {body!r}}}", response=response, body=body)


# The provider library raises this error for every transport failure. It chains the account of the transport behind
# the error. This double chains the cause in the same way, because JRI reads `__cause__` for the actual reason.
def disconnection(cause: Exception | None = None) -> APIConnectionError:
    error = APIConnectionError(request=REQUEST)
    error.__cause__ = cause
    return error


def rejection(message: str = "Unknown model.") -> BadRequestError:
    response = httpx.Response(400, request=REQUEST)
    # The library gives the exception the object under `error`, and not the full body. A provider writes there what
    # it refused and why.
    return BadRequestError(
        f"Error code: 400 - {{'error': {{'message': {message!r}}}}}",
        response=response,
        body={"message": message, "type": "invalid_request_error"},
    )


# A gateway stands between JRI and the provider, and answers for itself. Its body has no shape that the provider
# library knows.
def bad_gateway(body: str) -> InternalServerError:
    response = httpx.Response(502, request=REQUEST)
    return InternalServerError(body, response=response, body=body)


def failure(message: str) -> Round:
    return [SimpleNamespace(type="error", message=message)]


def incomplete_response(reason: str | None) -> Round:
    details = SimpleNamespace(reason=reason) if reason else None
    return [SimpleNamespace(type="response.incomplete", response=SimpleNamespace(incomplete_details=details))]


# The library gives a failed response an error object, and not a message. JRI takes what it shows the user from it.
# The object also holds a code for the class of the failure. A provider can report a failure with no object at all.
def failed_response(message: str | None) -> Round:
    error = ResponseError(code="server_error", message=message) if message else None
    return [SimpleNamespace(type="response.failed", response=SimpleNamespace(error=error))]


# The provider library reads the structured answer during the stream. If the model wrote text that the schema
# does not accept, the library raises an error. It raises during the loop over the events, and no event follows.
# This round gives text that `output_type` cannot read.
def unreadable_answer(output_type: type[BaseModel], text: str) -> Round:
    yield _delta(text)
    output_type.model_validate_json(text)


def call(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


# What a run said back to the model with the answer of each tool call it made.
def read_tool_outputs(client: "FakeClient") -> list[str]:
    answered: list[str] = []
    for context in client.responses.inputs:
        for message in cast("list[dict[str, object]]", context):
            if message.get("type") != "function_call_output":
                continue
            output = message["output"]
            if isinstance(output, str):
                answered.append(output)
            else:
                answered += [str(item.get("text", "")) for item in cast("list[dict[str, object]]", output)]
    return answered


def reply(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


class FakeClient:
    def __init__(self, rounds: Iterable[Round | OpenAIError], *, parsed: Iterable[object] = ()) -> None:
        self.responses = _Responses(rounds, parsed)
        # A real client makes the address in the settings into the address that it sends to. A failure names that
        # address.
        self.base_url = httpx.URL(f"{BASE_URL}/")


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", output_index=0, delta=text)


class _Responses:
    def __init__(self, rounds: Iterable[Round | OpenAIError], parsed: Iterable[object]) -> None:
        self.rounds = iter(rounds)
        self.parsed = iter(parsed)
        self.inputs: list[object] = []
        # The tools that each request gave to the model. These tools tell the model what it can do, and the
        # prompt in `inputs` tells it what to do. A test reads them.
        self.tools: list[list[str]] = []
        self.options: list[dict[str, object]] = []

    def create(self, **options: object) -> "_Stream":
        return _Stream(cast("Round", self._serve(self.rounds, options)))

    def stream(self, **options: object) -> "_ParsedStream":
        return _ParsedStream(self._serve(self.parsed, options))

    def _serve(self, source: Iterator[object], options: dict[str, object]) -> object:
        self.inputs.append(options["input"])
        self.tools.append([item["name"] for item in cast("list[dict[str, str]]", options["tools"])])
        self.options.append(options)
        served = next(source)
        if isinstance(served, OpenAIError):
            raise served
        return served


class _ParsedStream:
    def __init__(self, parsed: object) -> None:
        self.parsed = parsed
        # A stream that the run abandons has no end to read. Its events arrive one at a time, and not as a list
        # that the double reads in full first.
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
        # The SDK collects the text of the completed message items. This double does the same.
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

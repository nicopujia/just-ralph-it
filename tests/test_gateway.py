import json

import httpx

from jri.lib.providers import gateway

MODEL = "anthropic/claude-opus-5"


# The gateway reads fields of its own off a request, and it answers nothing about them. The body that left the
# process is where such a field either stands or does not, so a double stands at the transport and keeps it.
def build_client(requests: list[httpx.Request]) -> gateway.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(httpx.codes.OK, json={})

    transport = httpx.MockTransport(handle)
    return gateway.Client(base_url=gateway.BASE_URL, api_key="test-key", http_client=httpx.Client(transport=transport))


def test_asks_the_gateway_to_mark_the_start_of_a_request_for_its_cache() -> None:
    requests: list[httpx.Request] = []
    client = build_client(requests)

    client.responses.with_raw_response.create(model=MODEL, input="How often does it deploy?")

    assert json.loads(requests[0].content) == {"model": MODEL, "input": "How often does it deploy?", "caching": "auto"}


def test_sends_a_request_to_the_vercel_ai_gateway() -> None:
    requests: list[httpx.Request] = []
    client = build_client(requests)

    client.responses.with_raw_response.create(model=MODEL, input="How often does it deploy?")

    assert str(requests[0].url) == "https://ai-gateway.vercel.sh/v1/responses"

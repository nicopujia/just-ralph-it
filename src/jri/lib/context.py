import json

__all__ = ["estimate_tokens", "measure_item", "measure_request"]

# The serializer writes no whitespace, so a payload weighs what its parts weigh. One item costs its own bytes and
# the comma before it. A caller measures a long context once and then adds or removes one item at a time.
SEPARATOR_SIZE = 1


def measure_request(context: object, tools: object) -> int:
    return len(_serialize({"input": context, "tools": tools}).encode())


# This is what one more item costs the request that already holds the items before it.
def measure_item(item: object) -> int:
    return len(_serialize(item).encode()) + SEPARATOR_SIZE


def estimate_tokens(size: int) -> int:
    return (size + 2) // 3


def _serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

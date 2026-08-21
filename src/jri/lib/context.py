import json

__all__ = ["estimate_tokens", "measure_item", "measure_request"]

# The serializer writes no whitespace. The size of a payload is thus the sum of the sizes of its parts. One item
# adds its own bytes and the comma before it. A caller measures a long context one time. Then it adds or removes
# one item at a time.
SEPARATOR_SIZE = 1


def measure_request(context: object, tools: object) -> int:
    return len(_serialize({"input": context, "tools": tools}).encode())


# Return the size that one more item adds to a request that already holds the items before it.
def measure_item(item: object) -> int:
    return len(_serialize(item).encode()) + SEPARATOR_SIZE


def estimate_tokens(size: int) -> int:
    return (size + 2) // 3


def _serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

from jri.lib.context import estimate_tokens, measure_item, measure_request


def test_estimates_tokens_from_the_byte_size_of_the_payload() -> None:
    # JRI counts the UTF-8 bytes of the text, and not its characters.
    # Accented text and other non-ASCII text must cost more tokens.
    # A count of characters would make the budget for such text too small, and no one would see it.
    assert estimate_tokens(measure_request("é" * 300, None)) == estimate_tokens(measure_request("a" * 300, None)) + 100


def test_measures_the_tools_alongside_the_context() -> None:
    assert measure_request("prompt", [{"name": "search"}]) > measure_request("prompt", None)


# A caller weighs a context one time, and then removes one item at a time.
# Each item must cost the same alone as it costs in the whole payload.
# If it does not, the caller trims the context against a budget that no request weighed.
def test_measures_an_item_as_the_payload_holding_it_measures_it() -> None:
    head = [{"role": "system", "content": "Interview the user."}, {"role": "system", "content": "Notes: n1, n2."}]
    added = [{"role": "user", "content": "Añadir soporte para «ralphing»"}, {"type": "reasoning", "summary": []}]
    tools = [{"name": "capture_notes"}]

    weighed = measure_request(head, tools) + sum(measure_item(item) for item in added)

    assert weighed == measure_request([*head, *added], tools)

from collections.abc import Iterator

import pytest

from jri.core.exceptions import Error


def descend(error_type: type[Error]) -> Iterator[type[Error]]:
    for subclass in error_type.__subclasses__():
        yield subclass
        yield from descend(subclass)


@pytest.mark.parametrize("error_type", [Error, *descend(Error)], ids=lambda value: value.__name__)
def test_catches_every_jri_error_as_a_runtime_error(error_type: type[Error]) -> None:
    with pytest.raises(RuntimeError, match=r"Something failed\."):
        raise error_type("Something failed.")

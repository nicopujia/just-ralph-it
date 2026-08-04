import pytest

from jri.core.exceptions import Error


@pytest.mark.parametrize("error_type", [Error, *Error.__subclasses__()], ids=lambda value: value.__name__)
def test_catches_every_jri_error_as_a_runtime_error(error_type: type[Error]) -> None:
    with pytest.raises(RuntimeError, match=r"Something failed\."):
        raise error_type("Something failed.")

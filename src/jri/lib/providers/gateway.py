from typing import Any, ClassVar, cast, override

from openai import OpenAI
from openai._models import FinalRequestOptions

__all__ = ["BASE_URL", "Client"]

BASE_URL = "https://ai-gateway.vercel.sh/v1"


class Client(OpenAI):
    # Some providers cache the start of a request by themselves, but Anthropic caches nothing until the request
    # marks what to keep. This field makes the gateway put those marks in. Only a gateway in front of a model
    # reads the field: an endpoint that serves the model itself refuses a request that carries it.
    PROMPT_CACHING: ClassVar[dict[str, str]] = {"caching": "auto"}

    @override
    def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        if isinstance(options.json_data, dict):
            options.json_data = cast("dict[str, Any]", options.json_data) | self.PROMPT_CACHING
        return super()._prepare_options(options)

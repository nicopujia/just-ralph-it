from typing import Any, ClassVar, cast, override

from openai import OpenAI
from openai._models import FinalRequestOptions

__all__ = ["BASE_URL", "Client"]

BASE_URL = "https://ai-gateway.vercel.sh/v1"


class Client(OpenAI):
    # Some providers cache the start of a request by themselves. Anthropic caches nothing until the request marks
    # the part to keep. This field tells the gateway to add those marks. Only a gateway that sends the request to
    # a model reads this field. An endpoint that serves the model refuses a request that holds this field.
    PROMPT_CACHING: ClassVar[dict[str, str]] = {"caching": "auto"}

    @override
    def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        if isinstance(options.json_data, dict):
            options.json_data = cast("dict[str, Any]", options.json_data) | self.PROMPT_CACHING
        return super()._prepare_options(options)

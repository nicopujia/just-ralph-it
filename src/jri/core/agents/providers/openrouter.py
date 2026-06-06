"""OpenRouter model provider adapter."""

from jri.core.agents.models import AgentModelPreset, ProviderModelRegistry

OPENROUTER_PRESETS = {
    "cheap": AgentModelPreset(
        interviewer="deepseek/deepseek-chat",
        explorer="qwen/qwen2.5-vl-32b-instruct",
    ),
}


def format_openrouter_model(model_id: str) -> str:
    """Return the Pydantic AI model string for a model ID."""
    if model_id.startswith("openrouter:"):
        return model_id
    return f"openrouter:{model_id}"


OPENROUTER_REGISTRY = ProviderModelRegistry(
    provider="openrouter",
    api_key_env_var="OPENROUTER_API_KEY",
    presets=OPENROUTER_PRESETS,
    format_model_id=format_openrouter_model,
)

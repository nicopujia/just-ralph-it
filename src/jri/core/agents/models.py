"""Shared model configuration for JRI agents."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentModelConfig:
    """Model IDs for the JRI agents."""

    interviewer: str
    explorer: str


@dataclass(frozen=True)
class AgentModelPreset:
    """Provider-native model IDs for the JRI agents."""

    interviewer: str
    explorer: str


@dataclass(frozen=True)
class ProviderModelRegistry:
    """Named provider presets and model ID resolution."""

    provider: str
    api_key_env_var: str
    presets: Mapping[str, AgentModelPreset]
    format_model_id: Callable[[str], str]

    def load_model_config(
        self,
        *,
        preset_name: str,
        env: Mapping[str, str],
    ) -> AgentModelConfig:
        """Resolve model IDs from a preset and role overrides."""
        try:
            preset = self.presets[preset_name]
        except KeyError as exc:
            names = ", ".join(sorted(self.presets))
            msg = (
                "Unsupported JRI_MODEL_PRESET "
                f"{preset_name!r} for {self.provider}. "
                f"Supported presets: {names}."
            )
            raise ValueError(msg) from exc

        return AgentModelConfig(
            interviewer=self.format_model_id(
                env.get("JRI_INTERVIEWER_MODEL_ID", preset.interviewer)
            ),
            explorer=self.format_model_id(
                env.get("JRI_EXPLORER_MODEL_ID", preset.explorer)
            ),
        )

    def validate_credentials(self, env: Mapping[str, str]) -> None:
        """Require provider credentials before live model calls."""
        if env.get(self.api_key_env_var):
            return
        msg = (
            f"{self.api_key_env_var} is required to run JRI with the "
            f"{self.provider} model provider."
        )
        raise ValueError(msg)

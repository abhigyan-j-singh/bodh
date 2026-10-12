"""The model providers: one per service, behind the `Provider` interface in `base.py`.
`make_provider` builds one from its name and the secrets; `llm.LLM` holds them."""

from __future__ import annotations

from typing import Any

from .base import PROVIDERS, Provider


def make_provider(name: str, secrets: Any, flags: Any) -> Provider:
    """A provider by name, with its key (or base URL) from `.env`."""
    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(secrets.value("anthropic_api_key"), flags)
    if name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(secrets.value("openai_api_key"))
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(secrets.value("gemini_api_key"))
    if name == "openrouter":
        from .openai import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            secrets.value("openrouter_api_key"), "https://openrouter.ai/api/v1", name="openrouter"
        )
    if name == "openai-compatible":
        from .openai import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            secrets.value("openai_compatible_api_key"),
            secrets.value("openai_compatible_base_url"),
            name="openai-compatible",
        )
    raise ValueError(f"unknown provider {name!r}; one of {', '.join(PROVIDERS)}")

"""Model adapters: native function calling behind one provider-neutral interface.

The provider SDKs are optional extras (pip install "repo-engineer[openai]",
"[anthropic]", "[gemini]", or "[all]") and are imported only when an adapter
is created. create_adapter reads the API key from an environment variable at
construction; the key is never logged, traced or written anywhere.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (
    RETRYABLE_STATUS,
    AssistantMessage,
    ConfigurationError,
    Message,
    ModelAdapter,
    ModelReply,
    ModelToolCall,
    ProviderError,
    ToolResultMessage,
    Usage,
    UserMessage,
    call_with_retries,
)

PROVIDERS = ("gemini", "openai", "anthropic", "local")
DEFAULT_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "local": "",
}
DEFAULT_TIMEOUT_S = 90.0


def create_adapter(
    provider: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key_env: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ModelAdapter:
    """An adapter for a provider, with the SDK client built and its retries off."""
    if provider not in PROVIDERS:
        raise ConfigurationError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
    key_env = api_key_env if api_key_env is not None else DEFAULT_KEY_ENV[provider]
    key = os.environ.get(key_env, "").strip() if key_env else ""
    if key_env and not key:
        raise ConfigurationError(
            f"environment variable {key_env} is not set; the {provider} adapter has no "
            "credentials. Set it, or use the scripted planner."
        )
    if provider == "local" and not base_url:
        raise ConfigurationError(
            "the local provider needs --base-url (for example http://localhost:11434/v1)"
        )
    try:
        return _build(provider, model, key, base_url, timeout_s)
    except ImportError as exc:
        raise ConfigurationError(
            f"the {provider} SDK is not installed: pip install 'repo-engineer[{_extra(provider)}]'"
        ) from exc


def _extra(provider: str) -> str:
    return "openai" if provider == "local" else provider


def _build(
    provider: str, model: str, key: str, base_url: str | None, timeout_s: float
) -> ModelAdapter:
    client: Any
    if provider in ("openai", "local"):
        import openai

        from .openai_adapter import OpenAIAdapter

        client = openai.OpenAI(
            # Local servers ignore the key, but the SDK needs a non-empty one.
            api_key=key or "not-needed",
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,
        )
        if provider == "local":
            return OpenAIAdapter(model, client=client, provider="local", temperature=0.0)
        return OpenAIAdapter(model, client=client)
    if provider == "anthropic":
        import anthropic

        from .anthropic_adapter import AnthropicAdapter

        client = anthropic.Anthropic(
            api_key=key, base_url=base_url, timeout=timeout_s, max_retries=0
        )
        return AnthropicAdapter(model, client=client)
    from google import genai
    from google.genai import types

    from .gemini_adapter import GeminiAdapter

    # No retry_options: the SDK then makes one attempt, and ours apply.
    options = types.HttpOptions(timeout=int(timeout_s * 1000), base_url=base_url)
    client = genai.Client(api_key=key, http_options=options)
    return GeminiAdapter(model, client=client)


__all__ = [
    "PROVIDERS",
    "RETRYABLE_STATUS",
    "AssistantMessage",
    "ConfigurationError",
    "Message",
    "ModelAdapter",
    "ModelReply",
    "ModelToolCall",
    "ProviderError",
    "ToolResultMessage",
    "Usage",
    "UserMessage",
    "call_with_retries",
    "create_adapter",
]

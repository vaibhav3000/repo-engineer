"""Minimal OpenAI-compatible chat client (stdlib only).

It started as a copy of aire's src/aire/llm/provider.py: both projects stay
standalone repos, so the small client is duplicated rather than extracted into
a shared package, and fixes are ported between them by hand.

One provider class covers Gemini's OpenAI-compatible endpoint and any other
OpenAI-style API. The API key is read from an environment variable at call
time; it is never written to disk, logs or results by this module.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    finish_reason: str


class ProviderError(RuntimeError):
    pass


# Status codes worth retrying: rate limits and transient server errors. Anything
# else (400, 401, 403, 404, ...) fails the same way every time.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class OpenAICompatProvider:
    """Chat-completions client for OpenAI-compatible endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        temperature: float = 0.0,
        timeout_s: float = 90.0,
        max_tokens: int = 2048,
        backoff_s: float = 4.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.backoff_s = backoff_s

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError(
                f"environment variable {self.api_key_env} is not set; "
                "no API key available for the LLM provider"
            )
        return key

    def chat(self, system: str, user: str, retries: int = 2) -> LLMResponse:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        ).encode("utf-8")
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._key()}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read())
                choice = data["choices"][0]
                message = choice.get("message", {})
                usage = data.get("usage", {})
                return LLMResponse(
                    content=message.get("content") or "",
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    model=data.get("model", self.model),
                    finish_reason=choice.get("finish_reason", ""),
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                error = ProviderError(f"HTTP {exc.code}: {detail}")
                if exc.code not in RETRYABLE_STATUS or attempt == retries:
                    raise error from exc
                time.sleep(self.backoff_s * (attempt + 1))  # rate-limit backoff
            except (urllib.error.URLError, TimeoutError) as exc:
                error = ProviderError(f"{type(exc).__name__}: {exc}")
                if attempt == retries:
                    raise error from exc
                time.sleep(self.backoff_s / 2)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                # A malformed success body will not improve on retry.
                raise ProviderError(
                    f"malformed provider reply: {type(exc).__name__}: {exc}"
                ) from exc
        raise ProviderError("request failed")

    def chat_json(self, system: str, user: str) -> tuple[dict[str, Any], LLMResponse]:
        """Chat and return the first JSON object in the reply."""
        resp = self.chat(system, user)
        data = first_json_object(resp.content)
        if data is None:
            raise ProviderError(f"reply contains no JSON object: {resp.content[:200]!r}")
        return data, resp


def first_json_object(text: str) -> dict[str, Any] | None:
    """The first complete JSON object in text, skipping prose and code fences.

    Each '{' is tried in turn with JSONDecoder.raw_decode, so braces in the
    surrounding prose do not break parsing and trailing text is ignored.
    """
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    return None

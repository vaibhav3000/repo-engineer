"""Minimal OpenAI-compatible chat client (stdlib only).

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


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    finish_reason: str


class ProviderError(RuntimeError):
    pass


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError(
                f"environment variable {self.api_key_env} is not set; "
                "no API key available for the LLM provider"
            )
        return key

    def chat(self, system: str, user: str, retries: int = 2) -> LLMResponse:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        last_error: Exception | None = None
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
                last_error = ProviderError(f"HTTP {exc.code}: {detail}")
                if exc.code in (429, 500, 502, 503) and attempt < retries:
                    time.sleep(4.0 * (attempt + 1))  # rate-limit backoff
                    continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
                last_error = ProviderError(f"{type(exc).__name__}: {exc}")
                if attempt < retries:
                    time.sleep(2.0)
                    continue
        raise last_error if last_error else ProviderError("request failed")

    def chat_json(self, system: str, user: str) -> tuple[dict, LLMResponse]:
        """Chat and parse the reply as a JSON object (tolerates code fences)."""
        resp = self.chat(system, user)
        text = resp.content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ProviderError(f"reply is not JSON: {resp.content[:200]!r}")
        try:
            return json.loads(text[start:end + 1]), resp
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON in reply: {exc}: {resp.content[:200]!r}") from exc

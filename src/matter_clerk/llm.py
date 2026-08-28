from __future__ import annotations

import os

from openai import OpenAI


class LLMClient:
    """Single interface to the configured LLM provider.

    Per CLAUDE.md, provider swap is a one-file change: OpenRouter is OpenAI-
    protocol-compatible, so we use the OpenAI SDK with OpenRouter's base URL.
    To swap to Anthropic / OpenAI / Google directly, replace this class only.
    """

    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
    ) -> None:
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self.api_key = key
        self.model = model or os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")
        self._client = OpenAI(api_key=self.api_key, base_url=base_url)

    def complete(self, messages: list[dict]) -> str:
        # Deliberately no max_tokens. The default provider (MiMo Pro) is a
        # reasoning model whose hidden reasoning tokens are charged against
        # max_tokens, so a limit that looks generous starves the visible
        # output instead of bounding it: Phase-2a measured a 2,500-token cap
        # truncating a completion to 872 characters, where the same prompt
        # with no cap returned a complete 2,783-character block. Callers that
        # need structurally complete output handle truncation on their side.
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.1,
        )
        return response.choices[0].message.content or ""

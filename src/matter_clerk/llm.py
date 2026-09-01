from __future__ import annotations

import os

from openai import OpenAI


class MissingAPIKey(RuntimeError):
    """No OpenRouter credential is configured.

    A distinct type, not a bare RuntimeError, so web.py can turn it into a
    readable page. Before Phase 3 Session 5 this escaped the request handler
    and Flask rendered a bare 500: the app started fine without a key and only
    failed at the moment a lawyer actually ran a task, with the one useful
    sentence visible in a console window behind the browser.
    """


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
            raise MissingAPIKey(
                "No OpenRouter API key is configured, so drafting and analysis "
                "tasks cannot run. Close Matter Clerk and start it again with "
                "the --first-run option to enter your key, or add "
                "OPENROUTER_API_KEY to your .env file."
            )
        self.api_key = key
        self.model = model or os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")
        self._client = OpenAI(api_key=self.api_key, base_url=base_url)

    def complete_with_usage(self, messages: list[dict]) -> tuple[str, dict]:
        """Like complete(), but also returns the provider's token accounting.

        Added in Session 8 for exhaustive mode, which shows a running cost while
        it works. The usage dict is whatever the provider reported -- never
        estimated here, because a made-up number on a cost display is worse than
        no number.
        """
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.1,
        )
        usage = {}
        u = getattr(response, "usage", None)
        if u is not None:
            usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }
        return (response.choices[0].message.content or ""), usage

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

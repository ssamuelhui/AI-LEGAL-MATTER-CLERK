from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from openai import OpenAI

log = logging.getLogger("matter_clerk.llm")


class MissingAPIKey(RuntimeError):
    """No OpenRouter credential is configured.

    A distinct type, not a bare RuntimeError, so web.py can turn it into a
    readable page. Before Phase 3 Session 5 this escaped the request handler
    and Flask rendered a bare 500: the app started fine without a key and only
    failed at the moment a lawyer actually ran a task, with the one useful
    sentence visible in a console window behind the browser.
    """


# --------------------------------------------------------------------------
# Per-run cost accounting (Session 11)
#
# The accumulator lives HERE, inside the client, rather than at the call sites.
# That choice is the whole safety property: a new call site is counted
# automatically, whereas a per-call-site hook has to be remembered, and the
# failure mode of forgetting is a billing figure that is silently too low.
#
# It is not theoretical. `discovery` makes TWO model calls per run -- concept
# extraction and case notes -- so Suggest Relevant Cases would have been
# under-counted by half, indefinitely, with no symptom.
#
# Multiple calls inside one run SUM. They are not double-counted, because the
# accumulator is opened once per run by the caller, not once per call.
# --------------------------------------------------------------------------
@dataclass
class CostAccumulator:
    task: str = ""
    matter_id: int | None = None
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # True once any call came back without a cost figure. The run is then
    # recorded with cost NULL rather than with a total that is quietly short.
    cost_unavailable: bool = False
    models_used: list[str] = field(default_factory=list)

    def add(self, usage: dict) -> None:
        self.calls += 1
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            self.cost_unavailable = True
        else:
            self.cost_usd += float(cost)
        model = usage.get("model")
        if model and model not in self.models_used:
            self.models_used.append(model)


_RUN = threading.local()


def current_run() -> CostAccumulator | None:
    return getattr(_RUN, "acc", None)


def start_cost_run(task: str = "", matter_id: int | None = None,
                   model: str = "") -> CostAccumulator:
    """Open a cost scope without a `with` block.

    The context-manager form is nicer, but the web handlers wrap a long
    try/except chain that cannot be re-indented safely, so they open the scope
    explicitly and close it in a `finally`.
    """
    acc = CostAccumulator(task=task, matter_id=matter_id, model=model)
    _RUN.acc = acc
    return acc


def end_cost_run() -> None:
    _RUN.acc = None


@contextmanager
def cost_run(task: str = "", matter_id: int | None = None, model: str = ""):
    """Open a cost-accounting scope for one task run.

    Yields the accumulator. The caller writes the row in its own `finally`, so
    a run that fails before any model call still leaves a $0.00 record -- a
    lawyer looking for "why did that disappear" must find the attempt rather
    than silence.
    """
    previous = getattr(_RUN, "acc", None)
    acc = CostAccumulator(task=task, matter_id=matter_id, model=model)
    _RUN.acc = acc
    try:
        yield acc
    finally:
        _RUN.acc = previous


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

    def _call(self, messages: list[dict]):
        # Deliberately no max_tokens. The default provider (MiMo Pro) is a
        # reasoning model whose hidden reasoning tokens are charged against
        # max_tokens, so a limit that looks generous starves the visible
        # output instead of bounding it: Phase-2a measured a 2,500-token cap
        # truncating a completion to 872 characters, where the same prompt
        # with no cap returned a complete 2,783-character block. Callers that
        # need structurally complete output handle truncation on their side.
        #
        # `usage.include` asks OpenRouter to report what it ACTUALLY charged.
        # Session 11 records that figure rather than computing one from a price
        # table: a computed number cannot see prompt caching, provider routing,
        # or a price that changed since the catalogue was cached.
        return self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.1,
            extra_body={"usage": {"include": True}},
        )

    def _usage_dict(self, response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            data = usage.model_dump()
        else:
            data = dict(usage)
        data["model"] = getattr(response, "model", None) or self.model
        return data

    def _record(self, usage: dict) -> None:
        acc = current_run()
        if acc is not None:
            acc.add(usage)

    def complete(self, messages: list[dict]) -> str:
        response = self._call(messages)
        self._record(self._usage_dict(response))
        return response.choices[0].message.content or ""

    def complete_with_usage(self, messages: list[dict]) -> tuple[str, dict]:
        """Like complete(), but also hands the caller the token accounting.

        Used by exhaustive mode, which shows a running tally while it works.
        The usage dict is whatever the provider reported -- never estimated
        here, because a made-up number on a cost display is worse than none.
        """
        response = self._call(messages)
        usage = self._usage_dict(response)
        self._record(usage)
        return (response.choices[0].message.content or ""), usage

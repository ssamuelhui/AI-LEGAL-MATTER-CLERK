r"""Exhaustive mode (Session 8).

The lawyer's complaint was that Timeline and Summarize "aren't detailed enough
-- we don't want the software to decide what to include and exclude."

Measurement located the cause precisely, and it was not the prompt. The
Timeline prompt has said "Capture EVERY dated event... Do not summarize or omit
events" since Day 4c-a. The problem is upstream: `search_across_collections`
retrieves top-k from each file and then merges to a GLOBAL top-k, so a Timeline
sends 14 chunks for the whole matter regardless of how many files it holds.

    measured on the 9-file dev matter (67 chunks, 31,806 tokens)
        Timeline Concise    14 chunks   20.9% of the matter
        Timeline Detailed   28 chunks   41.8%
        Summarize Standard  12 chunks   17.9%
        Find Entities       16 chunks   23.9%

At 28 files the cap is still 14, so coverage falls to roughly 7%. The model was
told to be exhaustive; it was never shown the documents.

Exhaustive mode sends every chunk of every selected file. That is affordable
because the chunks are small and the context window is not: the whole dev
matter is 31,806 tokens against a 1,000,000-token window.

MEASURED, 2026-09-01, one real run on the 9-file dev matter:
    67 chunks -> 54,542 prompt tokens (the [SOURCE:] headers and system prompt
    add ~23k on top of 31,806 tokens of chunk text), 16,989 output tokens,
    169.5 seconds wall clock, $0.6974 on anthropic/claude-opus-4.7, 65 timeline
    rows. That latency is why exhaustive runs execute as background tasks with
    polling rather than inside the request.

BATCHING is an overflow path, not the architecture. Single pass is the normal
case and is what makes deduplication a non-problem -- one call sees the whole
email chain, quoted replies included, and merges them itself.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import tiktoken

from .llm import LLMClient

log = logging.getLogger("matter_clerk.exhaustive")

_ENC = tiktoken.get_encoding("cl100k_base")

# --------------------------------------------------------------------------
# Model coercion
#
# Exhaustive runs ignore the configured MODEL and use a pinned model. Recorded
# in the run metadata and named in the UI -- a lawyer must always be able to see
# which model produced a document they may rely on.
#
# NOTE FOR THE RECORD: this departs from CLAUDE.md and the SoW, which specify
# MiMo Pro as the default provider. It applies to exhaustive runs ONLY; every
# other task still honours MODEL from .env.
#
# The id uses a DOT, not a hyphen. `anthropic/claude-opus-4-7` does not exist on
# OpenRouter and 404s on every call; verified against the live model list.
# --------------------------------------------------------------------------
EXHAUSTIVE_MODEL = "anthropic/claude-opus-4.7"

# USD per million tokens. Fetched from OpenRouter's /models endpoint on
# 2026-09-01; re-check when the run summary's costs start looking wrong.
MODEL_PRICING = {
    "anthropic/claude-opus-4.7": (5.00, 25.00),
    "xiaomi/mimo-v2.5-pro": (0.43, 0.87),
    "xiaomi/mimo-v2.5": (0.14, 0.28),
}
DEFAULT_PRICING = (5.00, 25.00)

# Conservative against a 1,000,000-token window. A reasoning model's quality
# degrades well before its context limit does, so batch early rather than serve
# a degraded single pass. At the measured 475-token mean chunk this is ~840
# chunks: ~113 files at this matter's density, ~17 at a pathological 50/file.
INPUT_BUDGET_TOKENS = 400_000

# Room for the system prompt, the task body and the request section.
PROMPT_OVERHEAD_TOKENS = 8_000

# tiktoken's cl100k_base is OpenAI's tokenizer. Anthropic models use a
# different one, and on this codebase's content (OCR'd legal correspondence,
# heavy on headers, dates and punctuation) it is materially denser: a single
# measured run counted 34,653 by cl100k and was billed 54,542 -- a ratio of
# 1.57. Applied to cost and batching estimates for Anthropic models so the
# figures shown to a lawyer err high rather than low.
#
# ONE data point, so it is rounded up and kept as a named constant rather than
# buried in a formula. Re-measure if the run summary's actual cost drifts from
# the estimate again.
CLAUDE_TOKEN_INFLATION = 1.65


def _billing_tokens(counted: int, model: str) -> int:
    """cl100k count adjusted toward what the provider will actually bill."""
    if model.startswith("anthropic/"):
        return int(counted * CLAUDE_TOKEN_INFLATION)
    return counted


@dataclass
class BatchOutcome:
    index: int
    files: list[str]
    chunks: int
    ok: bool
    text: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ExhaustiveRun:
    """Everything the result page and the audit log need to be honest."""

    model: str
    batches: list[BatchOutcome] = field(default_factory=list)
    total_chunks: int = 0
    files_covered: list[str] = field(default_factory=list)
    collapsed_duplicates: int = 0
    seconds: float = 0.0
    cancelled: bool = False

    @property
    def failed_batches(self) -> list[BatchOutcome]:
        return [b for b in self.batches if not b.ok]

    @property
    def prompt_tokens(self) -> int:
        return sum(b.prompt_tokens for b in self.batches)

    @property
    def completion_tokens(self) -> int:
        return sum(b.completion_tokens for b in self.batches)

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.prompt_tokens, self.completion_tokens)

    @property
    def complete(self) -> bool:
        return not self.failed_batches and not self.cancelled


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text or ""))


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (prompt_tokens / 1e6) * p_in + (completion_tokens / 1e6) * p_out


# --------------------------------------------------------------------------
# Gathering every chunk
# --------------------------------------------------------------------------
def gather_all_chunks(client, files) -> tuple[dict[str, list[str]], list[str]]:
    """Every chunk of every selected file, keyed by filename.

    Uses `all_chunks_for`, which carries the Session 6a guard: a collection that
    cannot be read is skipped and named rather than failing the run. An
    exhaustive run over a matter with one damaged file must still cover the rest
    -- and must say which one it could not read, because "exhaustive" is a claim
    about coverage and an unread file falsifies it.
    """
    from .vectorstore import all_chunks_for

    by_coll, report = all_chunks_for(client, [f.collection for f in files])
    name_by_coll = {f.collection: f.filename for f in files}

    texts: dict[str, list[str]] = {}
    for f in files:
        rows = by_coll.get(f.collection) or []
        if rows:
            texts[f.filename] = rows

    unreadable = [name_by_coll.get(c, c) for c in report.skipped]
    return texts, unreadable


def plan_batches(texts: dict[str, list[str]],
                 budget: int = INPUT_BUDGET_TOKENS) -> list[list[str]]:
    """Group files into batches that fit the input budget.

    Files are kept whole wherever possible: splitting one document across two
    model calls is what creates the cross-batch duplicate problem in the first
    place, so it is done only when a single file exceeds the budget alone.
    Returns a list of batches, each a list of filenames.
    """
    usable = budget - PROMPT_OVERHEAD_TOKENS
    sizes = {name: sum(count_tokens(t) for t in rows) for name, rows in texts.items()}

    batches: list[list[str]] = []
    current: list[str] = []
    running = 0
    for name in texts:                       # insertion order = the caller's sort
        size = sizes[name]
        if current and running + size > usable:
            batches.append(current)
            current, running = [], 0
        current.append(name)
        running += size
    if current:
        batches.append(current)
    return batches


# --------------------------------------------------------------------------
# Per-file duplicate collapse
#
# Cross-file deduplication is deliberately OFF. Two files describing the same
# date are usually two pieces of evidence about it, and collapsing them loses
# corroboration a lawyer needs to see. Within a single file, an exact repeat is
# an artefact of chunk overlap (CHUNK_OVERLAP_TOKENS = 100) rather than a second
# occurrence, so it is safe to collapse.
#
# Matching is exact on normalised text -- never fuzzy. "Notice served on tenant"
# and "Notice served on landlord" score ~90% similar by any string metric and
# are opposite facts; a threshold loose enough to merge genuine restatements is
# loose enough to merge distinct events, and a silently merged event is a
# MISSING event, which is precisely the complaint this feature exists to answer.
# --------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower().rstrip(".;,")


def collapse_within_file(rows: list[dict], key_fields: tuple[str, ...]) -> tuple[list[dict], int]:
    """Collapse exact repeats inside one file's extraction. Returns (rows, n)."""
    seen: dict[tuple, dict] = {}
    order: list[tuple] = []
    collapsed = 0
    for row in rows:
        key = tuple(_normalize(str(row.get(f, ""))) for f in key_fields)
        if key in seen:
            collapsed += 1
            existing = seen[key]
            src = row.get("source")
            if src and src not in (existing.get("source") or ""):
                existing["source"] = f"{existing.get('source', '')}; {src}".strip("; ")
        else:
            seen[key] = dict(row)
            order.append(key)
    return [seen[k] for k in order], collapsed


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def build_context_block(texts: dict[str, list[str]], names: list[str]) -> str:
    """The CONTEXT section for one batch, with per-chunk source headers.

    The header format matches the standard retrieval path exactly, so the
    model's citation behaviour is unchanged -- exhaustive mode alters WHAT the
    model sees, never how it is told to cite it.
    """
    parts: list[str] = []
    for name in names:
        for i, text in enumerate(texts.get(name) or [], start=1):
            parts.append(f"[SOURCE: {name} chunk {i}]\n{text}")
    return "\n\n".join(parts)


def run_exhaustive(
    texts: dict[str, list[str]],
    system_prompt: str,
    build_user,
    model: str = EXHAUSTIVE_MODEL,
    should_cancel=None,
    on_progress=None,
) -> tuple[str, ExhaustiveRun]:
    """Send every chunk to the model, in as few calls as will fit.

    `should_cancel` is polled at batch boundaries only: aborting mid-call would
    abandon tokens already paid for without gaining anything.
    A failed batch is recorded and the run continues -- one bad call must not
    discard the analysis of every other file.
    """
    import time

    run = ExhaustiveRun(model=model)
    run.total_chunks = sum(len(v) for v in texts.values())
    run.files_covered = list(texts.keys())

    batches = plan_batches(texts)
    started = time.monotonic()
    outputs: list[str] = []

    llm = LLMClient(model=model)

    for i, names in enumerate(batches, start=1):
        if should_cancel is not None and should_cancel():
            run.cancelled = True
            log.warning(f"exhaustive run cancelled before batch {i}/{len(batches)}")
            break

        run.seconds = time.monotonic() - started
        if on_progress is not None:
            on_progress(i, len(batches), names, run)

        chunk_count = sum(len(texts.get(n) or []) for n in names)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user(names)},
        ]

        try:
            text, usage = llm.complete_with_usage(messages)
            outputs.append(text)
            run.batches.append(BatchOutcome(
                index=i, files=list(names), chunks=chunk_count, ok=True, text=text,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ))
        except Exception as e:                                    # noqa: BLE001
            log.warning(f"exhaustive batch {i}/{len(batches)} failed: "
                        f"{type(e).__name__}: {e}")
            run.batches.append(BatchOutcome(
                index=i, files=list(names), chunks=chunk_count, ok=False,
                error=f"{type(e).__name__}: {e}",
            ))

        # Report again AFTER the call. Firing only before it meant a run with a
        # single batch -- the normal case -- never published any token or cost
        # figures at all, so the live tally sat at zero for its whole duration
        # and then jumped straight to the finished page.
        run.seconds = time.monotonic() - started
        if on_progress is not None:
            on_progress(i, len(batches), names, run)

    run.seconds = time.monotonic() - started
    return "\n\n".join(outputs), run


def estimate_run(texts: dict[str, list[str]], system_prompt: str = "",
                 model: str = EXHAUSTIVE_MODEL) -> dict:
    """Pre-run figures for the confirmation dialog.

    Input tokens are measured on the ACTUAL assembled context, not on the raw
    chunk text. The first version of this counted only the chunks and came in
    17% under a real run: the [SOURCE: ...] header on every chunk, plus the
    system prompt, added 22,736 tokens to a 31,806-token matter. An estimate
    that is quietly low is worse than no estimate on a cost dialog, so it is now
    computed from the same builder the run itself uses.

    Only the output side is estimated, and it is bounded by a measured rate:
    one real 67-chunk exhaustive Timeline produced 16,989 output tokens, i.e.
    ~250 per chunk, which is what the high band uses.
    """
    chunks = sum(len(v) for v in texts.values())
    batches = plan_batches(texts)

    # Assemble what will actually be sent, per batch, and count that.
    counted = 0
    for names in batches:
        counted += count_tokens(build_context_block(texts, names))
        counted += count_tokens(system_prompt)
    in_tokens = _billing_tokens(counted, model)

    out_low = max(2_000, chunks * 80)
    out_high = max(8_000, chunks * 300)
    p_in, p_out = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return {
        "files": len(texts),
        "chunks": chunks,
        "input_tokens": in_tokens,
        "batches": len(batches),
        "model": model,
        "cost_low": (in_tokens / 1e6) * p_in + (out_low / 1e6) * p_out,
        "cost_high": (in_tokens / 1e6) * p_in + (out_high / 1e6) * p_out,
        # Measured: 169.5 s for a single 67-chunk / 54.5k-token batch.
        "seconds_low": int(len(batches) * 90),
        "seconds_high": int(len(batches) * 240),
    }

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel

# --------------------------------------------------------------------------
# The non-removable safety / citation-discipline clause (SoW Section 1.4).
#
# This text is owned by the CODE, not by the per-task YAML templates, and is
# prepended to every task's system prompt at runtime by `build_system_prompt`.
# Keeping it here is what makes it "non-removable" in the literal sense the SoW
# requires: a template author -- or the Phase 3 prompt curator proposing a diff
# -- cannot weaken or delete it, because it does not live in any template file.
# --------------------------------------------------------------------------
SAFETY_PREAMBLE = """\
You perform document-grounded legal work on a single legal-matter document, \
using only the retrieved passages provided in the CONTEXT section below. You \
are operating in MATTER-ONLY MODE: no external legal authority has been \
retrieved for this run. Follow these rules without exception, regardless of \
the task:

1. Every factual claim you make carries an inline citation copied exactly as \
written in the passage's "[SOURCE: ...]" header -- for example [FILENAME p.N] \
for a page of a PDF, or [FILENAME from Sender, DATE] for an email. Reproduce \
the source label verbatim. Do not invent filenames, page numbers, senders, or \
dates, and do not reformat the label.

2. You may cite only passages that appear in the CONTEXT section of this \
prompt. You must not cite any case, statute, regulation, rule, or other legal \
authority from memory or general knowledge -- even if you are confident the \
authority exists and even if the request seems to call for it.

3. You must not state the elements of a cause of action, the test for a \
remedy, the requirements of a statute, or the contents of a procedural rule \
from memory. These are legal authority and are not available in matter-only \
mode.

4. If the retrieved passages do not contain enough information to complete the \
request, say so explicitly and state what is missing. Do not guess and do not \
fall back on general knowledge.

5. Quote verbatim only when the exact wording matters, and quoted text must \
appear in the cited passage.

The task-specific instructions follow. They refine HOW to present the output, \
but they never override the five rules above."""


# --------------------------------------------------------------------------
# Matter-aware (cross-document) prompt switch (Day 4b).
#
# When a query runs across a whole matter, the prompts must stop assuming a
# single document. We do this WITHOUT editing the YAML templates so the
# single-file path stays character-identical (preserving the gold-set baseline
# and all Day 3/3.5 safety testing). Two code-owned, reversible transforms,
# applied only when cross_document=True:
#
#   1. _matterize_preamble: swap the SAFETY_PREAMBLE opening clause.
#   2. _MATTER_PHRASES: a literal phrase map applied to the task body (and any
#      pleading variant). Ordered longest/most-specific first so the "this
#      single ..." forms are consumed before the bare catch-all.
#
# The newline variant exists because timeline.yaml wraps the phrase as
# "this single\nlegal-matter document"; a space-only key would silently miss it.
# --------------------------------------------------------------------------
_PREAMBLE_SINGLE_CLAUSE = "on a single legal-matter document"
_PREAMBLE_MATTER_CLAUSE = "on the documents in this matter (the case file)"

MATTER_CONTEXT_NOTE = (
    "The CONTEXT may contain passages from multiple documents in this matter. "
    "Each passage's [SOURCE: ...] header identifies which document it came from; "
    "cite accordingly."
)

# --------------------------------------------------------------------------
# Detailed-timeline instruction (Day 4c-a).
#
# Code-owned, like SAFETY_PREAMBLE and MATTER_CONTEXT_NOTE, so a template author
# cannot weaken it and so it can be threaded into ANY assembly path (single-file
# or matter) without editing the YAML. Appended after the task body ONLY when the
# Timeline task's `detail_level` control input is "Detailed"; when "Concise" (or
# absent) it is never appended and the prompt is byte-identical to pre-4c-a.
# --------------------------------------------------------------------------
DETAILED_TIMELINE_INSTRUCTION = (
    "Capture EVERY dated event and material action mentioned in the retrieved "
    "passages. Do not summarize or omit events. Err on the side of "
    "over-inclusion. If in doubt about whether an event is material, include it. "
    "The purpose of a detailed timeline is exhaustiveness, not editorial "
    "selection."
)

_MATTER_PHRASES = {
    "this single\nlegal-matter document": "these matter documents",  # timeline (wrapped)
    "this single legal-matter document": "these matter documents",
    "single legal-matter document": "matter documents (the case file)",  # catch-all
}


def _matterize(text: str) -> str:
    """Apply the literal matter-mode phrase map to a task body / variant."""
    for old, new in _MATTER_PHRASES.items():
        text = text.replace(old, new)
    return text


def _matterize_preamble(preamble: str) -> str:
    """Swap the preamble's single-document opening clause. Fails loud if the
    clause has drifted, so a preamble edit can never silently leave matter mode
    still claiming 'a single legal-matter document'."""
    if _PREAMBLE_SINGLE_CLAUSE not in preamble:
        raise ValueError(
            "SAFETY_PREAMBLE opening clause "
            f"{_PREAMBLE_SINGLE_CLAUSE!r} not found; the matter-mode swap cannot "
            "be applied. Update _PREAMBLE_SINGLE_CLAUSE to match the preamble."
        )
    return preamble.replace(_PREAMBLE_SINGLE_CLAUSE, _PREAMBLE_MATTER_CLAUSE)


# --------------------------------------------------------------------------
# Template model
# --------------------------------------------------------------------------
class ShowWhen(BaseModel):
    """Conditional-visibility rule: show this input only when another input's
    value is one of `values`. Drives the web form's nested toggling (e.g. show
    claim_particulars only for plaintiff pleading types)."""

    input: str
    values: list[str]


class InputField(BaseModel):
    """One user-supplied input for a task, declared in the task's YAML.

    Drives both the web form (how to render the control) and prompt assembly
    (how the value is folded into the request)."""

    name: str
    type: Literal["text", "textarea", "multiselect", "select", "checkbox"]
    label: str
    required: bool = False
    placeholder: Optional[str] = None
    options: Optional[list[str]] = None  # select / multiselect
    default: Optional[list[str]] = None  # multiselect only
    show_when: Optional[ShowWhen] = None
    # A "control" input steers HOW the task runs (retrieval depth, prompt mode)
    # rather than supplying content. It still renders in the form and shows on
    # the result page, but it is deliberately excluded from build_retrieval_query
    # (it must not perturb the embed query) and from build_user_message's REQUEST
    # section (it must not perturb the model's context). Day-4c-a's `detail_level`
    # is the first such field; keeping "Concise" out of both builders is what
    # makes Concise byte-identical to pre-4c-a behaviour.
    control: bool = False


class TaskTemplate(BaseModel):
    """A single named task, loaded and validated from prompts/templates/<id>.yaml.

    `system_prompt` holds ONLY the task-specific body; the non-removable
    SAFETY_PREAMBLE is prepended at runtime by `build_system_prompt`.

    `variants` (used by draft_pleading) maps an input value to extra,
    type-specific prompt text appended after the shared body — one task id,
    several output structures."""

    id: str
    label: str
    version: int
    system_prompt: str
    retrieval_query: str = ""
    top_k: int = 8
    inputs: list[InputField] = []
    variants: Optional[dict[str, str]] = None


# Display / dropdown order. Any template not listed is appended alphabetically.
TASK_ORDER = [
    "summarize",
    "timeline",
    "find_facts",
    "find_entities",
    "draft_memo",
    "draft_correspondence",
    "draft_pleading",
]

# The task selected by default (preserves the pre-Day-3 free-form Q&A behaviour).
DEFAULT_TASK = "find_facts"


def templates_dir() -> Path:
    """Directory holding the task-template YAML files.

    Repo layout puts these at <repo>/prompts/templates/. Overridable via
    MATTER_CLERK_PROMPTS_DIR for non-standard deployments."""
    override = os.environ.get("MATTER_CLERK_PROMPTS_DIR")
    if override:
        return Path(override)
    # this file: <repo>/src/matter_clerk/prompts.py  ->  parents[2] == <repo>
    return Path(__file__).resolve().parents[2] / "prompts" / "templates"


@lru_cache(maxsize=1)
def load_templates() -> dict[str, TaskTemplate]:
    """Load, validate, and cache every task template. Raises on startup if the
    directory is missing, empty, malformed, or has a duplicate/mismatched id."""
    d = templates_dir()
    if not d.is_dir():
        raise FileNotFoundError(f"Task-template directory not found: {d}")

    out: dict[str, TaskTemplate] = {}
    for path in sorted(d.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = TaskTemplate(**data)
        if template.id != path.stem:
            raise ValueError(
                f"Template id '{template.id}' does not match filename '{path.name}'."
            )
        if template.id in out:
            raise ValueError(f"Duplicate task template id: {template.id}")
        out[template.id] = template

    if not out:
        raise FileNotFoundError(f"No task templates (*.yaml) found in {d}")

    # Fail loudly if the pleading template drifts from the code-owned canon.
    if "draft_pleading" in out:
        from . import pleadings

        pleadings.check_template(out["draft_pleading"])
    return out


def ordered_templates() -> list[TaskTemplate]:
    """Templates in display order for the UI dropdown."""
    templates = load_templates()
    ranked = sorted(
        templates.values(),
        key=lambda t: (
            TASK_ORDER.index(t.id) if t.id in TASK_ORDER else len(TASK_ORDER),
            t.label,
        ),
    )
    return ranked


def get_template(task_id: str) -> TaskTemplate:
    templates = load_templates()
    if task_id not in templates:
        raise KeyError(task_id)
    return templates[task_id]


def missing_required_inputs(
    template: TaskTemplate, structured_inputs: dict
) -> list[str]:
    """Labels of required inputs that are absent/empty in `structured_inputs`."""
    missing: list[str] = []
    for field in template.inputs:
        if not field.required:
            continue
        val = structured_inputs.get(field.name)
        if val is None or (isinstance(val, (str, list)) and len(val) == 0):
            missing.append(field.label)
    return missing


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def build_system_prompt(
    template: TaskTemplate,
    structured_inputs: dict | None = None,
    cross_document: bool = False,
) -> str:
    """Assemble the system prompt: code-owned SAFETY_PREAMBLE + task body (+ any
    pleading variant). When cross_document is True (a matter scatter-gather
    query), the preamble clause is swapped, a MATTER CONTEXT note is inserted,
    and the matter phrase map is applied to the body/variant. When False the
    output is character-identical to the single-document behaviour."""
    if cross_document:
        parts = [
            _matterize_preamble(SAFETY_PREAMBLE),
            MATTER_CONTEXT_NOTE,
            _matterize(template.system_prompt.strip()),
        ]
    else:
        parts = [SAFETY_PREAMBLE, template.system_prompt.strip()]
    if template.variants:
        si = structured_inputs or {}
        key = si.get("pleading_type")
        variant = template.variants.get(key) if key else None
        if not variant:
            raise ValueError(
                f"Task '{template.id}' requires a valid pleading_type "
                f"(one of {list(template.variants)}); got {key!r}"
            )
        v = variant.strip()
        parts.append(_matterize(v) if cross_document else v)
    # Day 4c-a: the Timeline "Detailed" control appends the code-owned
    # exhaustiveness instruction after the task body, in BOTH single-file and
    # matter modes. Read from structured_inputs (like `pleading_type` above), so
    # no new parameter is needed. Absent / "Concise" -> nothing appended ->
    # byte-identical to pre-4c-a.
    si = structured_inputs or {}
    if (si.get("detail_level") or "Concise") == "Detailed":
        parts.append(DETAILED_TIMELINE_INSTRUCTION)
    return "\n\n".join(parts)


def build_retrieval_query(template: TaskTemplate, structured_inputs: dict) -> str:
    """Embed-query text for retrieval.

    Combines the template's seed `retrieval_query` (used for query-less tasks
    like Summarize) with whatever text the user supplied. Never returns empty."""
    parts: list[str] = []
    if template.retrieval_query:
        parts.append(template.retrieval_query)
    for field in template.inputs:
        if field.control:          # control inputs never enter the embed query
            continue
        val = structured_inputs.get(field.name)
        if not val:
            continue
        parts.append(" ".join(val) if isinstance(val, list) else str(val))
    query = " ".join(parts).strip()
    return query or template.label


def build_user_message(
    template: TaskTemplate, structured_inputs: dict, retrieved_chunks: list[dict]
) -> str:
    lines: list[str] = ["CONTEXT:", ""]
    for c in retrieved_chunks:
        lines.append(f"[SOURCE: {c['source']} {c['locator']}]")
        lines.append(c["text"])
        lines.append("")
    lines.append("REQUEST:")
    lines.append(f"Task: {template.label}")
    for field in template.inputs:
        if field.control:          # control inputs never enter the REQUEST section
            continue
        val = structured_inputs.get(field.name)
        if not val:
            continue
        rendered = ", ".join(val) if isinstance(val, list) else str(val)
        lines.append(f"{field.label}: {rendered}")
    return "\n".join(lines)

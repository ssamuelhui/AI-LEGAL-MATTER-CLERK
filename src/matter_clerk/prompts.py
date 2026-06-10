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

1. Every factual claim you make carries an inline citation in the form \
[FILENAME p.N], where FILENAME and N come from the passage's "[SOURCE: ...]" \
header. Do not invent filenames or page numbers.

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
# Template model
# --------------------------------------------------------------------------
class InputField(BaseModel):
    """One user-supplied input for a task, declared in the task's YAML.

    Drives both the web form (how to render the control) and prompt assembly
    (how the value is folded into the request)."""

    name: str
    type: Literal["text", "textarea", "multiselect"]
    label: str
    required: bool = False
    placeholder: Optional[str] = None
    options: Optional[list[str]] = None  # multiselect only
    default: Optional[list[str]] = None  # multiselect only


class TaskTemplate(BaseModel):
    """A single named task, loaded and validated from prompts/templates/<id>.yaml.

    `system_prompt` holds ONLY the task-specific body; the non-removable
    SAFETY_PREAMBLE is prepended at runtime by `build_system_prompt`."""

    id: str
    label: str
    version: int
    system_prompt: str
    retrieval_query: str = ""
    top_k: int = 8
    inputs: list[InputField] = []


# Display / dropdown order. Any template not listed is appended alphabetically.
TASK_ORDER = [
    "summarize",
    "timeline",
    "find_facts",
    "find_entities",
    "draft_memo",
    "draft_correspondence",
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
def build_system_prompt(template: TaskTemplate) -> str:
    return f"{SAFETY_PREAMBLE}\n\n{template.system_prompt.strip()}"


def build_retrieval_query(template: TaskTemplate, structured_inputs: dict) -> str:
    """Embed-query text for retrieval.

    Combines the template's seed `retrieval_query` (used for query-less tasks
    like Summarize) with whatever text the user supplied. Never returns empty."""
    parts: list[str] = []
    if template.retrieval_query:
        parts.append(template.retrieval_query)
    for field in template.inputs:
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
        lines.append(f"[SOURCE: {c['source']} p.{c['page']}]")
        lines.append(c["text"])
        lines.append("")
    lines.append("REQUEST:")
    lines.append(f"Task: {template.label}")
    for field in template.inputs:
        val = structured_inputs.get(field.name)
        if not val:
            continue
        rendered = ", ".join(val) if isinstance(val, list) else str(val)
        lines.append(f"{field.label}: {rendered}")
    return "\n".join(lines)

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

# --------------------------------------------------------------------------
# Case discovery (Phase 2a) -- the first task that is NOT matter-only.
#
# SAFETY_PREAMBLE cannot be reused here. Its opening sentence states that no
# external legal authority has been retrieved for this run, which is false for
# this task: CanLII case METADATA has been retrieved. Reusing it would either
# make the prompt lie about its own context or force a template author to weaken
# the clause that exists precisely so it cannot be weakened.
#
# So this task gets its own code-owned, non-removable preamble with a different
# and narrower danger to guard against. In matter-only mode the risk is the
# model inventing authority. Here the risk is subtler and worse: real cases are
# in the prompt, with real citations and real catchwords, and the model has
# every cue it needs to describe what they HELD -- which it cannot know, because
# CanLII's API does not return case text and never will. A confident sentence
# about the holding of a real, correctly-cited case is the most believable
# hallucination this system could produce.
# --------------------------------------------------------------------------
CASE_DISCOVERY_PREAMBLE = """\
You are helping a Canadian lawyer DISCOVER cases worth reading. You are not \
performing legal research and you are not analysing any case.

You have been given, for each candidate case: its title, citation, court, \
decision date, and CanLII's editorial catchwords and topic classification. \
You have NOT been given the text of any case, the reasons for judgment, the \
facts, the disposition, or the holding. You cannot obtain them. Write as \
someone who has read a library catalogue entry, not the book.

These rules are absolute:

1. Never state what a case held, decided, found, ruled, ordered, or \
established. Never describe its outcome or its reasoning.

2. Never say that a case "supports", "confirms", "establishes", "is authority \
for", "stands for", "applies to", "governs", or "is on point". You cannot \
assess any of that without reading the case, and you have not read it.

3. Never state whether a case is good law, or whether it has been followed, \
overruled, distinguished, reversed, appealed, or considered.

4. Never state the elements of a cause of action, the test for a remedy, the \
requirements of a statute, or the content of a procedural rule -- neither from \
these metadata nor from memory.

5. Do not rank the cases, recommend any of them, say which is strongest or \
most useful, or suggest which to read first. The ordering is not yours to make.

6. Describe only the METADATA CONNECTION: what in this case's catchwords, \
topic, court, or party description overlaps with the matter's legal concepts. \
Name the specific overlapping term you relied on.

7. Do not invent metadata. If a field says "(none published)", you may not \
describe what the case is about from the case name or from memory.

The lawyer will read each case on CanLII and form their own view. Your only \
job is to say why the case surfaced."""

# Ontario-specific vocabulary for query construction (approved design B).
#
# The tool is built for Ontario practice, and Ontario statutory names and forum
# vocabulary measurably sharpen CanLII's relevance ranking -- "Condominium Act,
# 1998" retrieves what "condominium legislation" does not. We do NOT have
# equivalent domain knowledge for other provinces, so a non-Ontario run is
# instructed to use general Canadian terminology rather than guess at
# province-specific statute names it might get wrong.
ONTARIO_VOCABULARY_NOTE = """\
JURISDICTION-SPECIFIC VOCABULARY -- Ontario.

This matter is an Ontario matter. Use Ontario statutory names, Ontario forum \
names, and Ontario practice vocabulary in your queries wherever the matter \
supports them, because CanLII's ranking responds strongly to exact statutory \
titles. Where relevant, that vocabulary includes: the Condominium Act, 1998; \
the Residential Tenancies Act, 2006; the Rules of Civil Procedure; the \
Limitations Act, 2002; the Occupiers' Liability Act; the Construction Act; the \
Employment Standards Act, 2000; the Human Rights Code; the Courts of Justice \
Act; and the Business Corporations Act (Ontario). Ontario forums include the \
Superior Court of Justice, the Divisional Court, the Court of Appeal for \
Ontario, the Condominium Authority Tribunal, the Landlord and Tenant Board, \
and the Human Rights Tribunal of Ontario.

Name a statute ONLY if the matter documents or the research direction actually \
engage it. Do not attach a statute to a query because it is on this list."""

GENERAL_VOCABULARY_NOTE = """\
JURISDICTION-SPECIFIC VOCABULARY -- not Ontario.

This run is not scoped to Ontario, and you do not have reliable knowledge of \
every province's statutory naming. Use GENERAL Canadian legal terminology -- \
doctrines, causes of action, and common-law concepts -- rather than \
province-specific statute titles, unless a statute is named explicitly in the \
matter documents or in the research direction. A wrong statutory title \
produces a confidently irrelevant search."""

# The analytical angles a good discovery search covers. Code-owned so coverage
# is structural rather than left to the model's judgment on the day. The model
# is told to SKIP angles the matter does not support rather than pad to a quota:
# a padded query returns confidently irrelevant cases, which costs the lawyer
# more time than a missing angle does.
QUERY_ANGLES = """\
  doctrinal_core      The cause of action, duty, or legal test named in the
                      research direction.
  statutory_hook      A statute and section the matter documents actually
                      engage. Emit up to two of these where the matter supports
                      more than one provision.
  factual_analogue    The fact pattern restated in the vocabulary a court would
                      use, so that factually similar cases surface.
  remedy              The relief or remedy in issue.
  defence             The opposing party's likely answer or defence, so the
                      lawyer sees the cases against them as well as for them.
  forum_specific      The vocabulary of the specialised tribunal or court that
                      hears this kind of dispute, where one exists."""

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
    # "file_multiselect" (Day 4c) is the one type whose choices are RUNTIME data
    # rather than YAML `options`: the form renders one checkbox per ingested file
    # in the current matter. It is therefore matter-mode only, and its submitted
    # values are file ids that the handler must authorize against the matter
    # exactly as it does `file_id`.
    type: Literal[
        "text", "textarea", "multiselect", "select", "checkbox",
        "file_multiselect", "number",
    ]
    label: str
    required: bool = False
    placeholder: Optional[str] = None
    options: Optional[list[str]] = None  # select / multiselect
    default: Optional[list[str]] = None  # multiselect only
    # "number" only. The form enforces these client-side; the task's own code
    # re-clamps server-side, because a min/max attribute is a hint and a POST
    # can carry anything.
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    initial: Optional[int] = None
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
    "compare_clauses",
    "suggest_cases",
    "draft_memo",
    "draft_correspondence",
    "draft_pleading",
]

# The task selected by default (preserves the pre-Day-3 free-form Q&A behaviour).
DEFAULT_TASK = "find_facts"

# --------------------------------------------------------------------------
# Task availability (Day 4c).
#
# Some tasks are meaningful only inside a matter, and only above a minimum file
# count: comparing clauses ACROSS documents needs at least two documents. This
# map is code-owned (not a YAML field) for the same reason the safety preamble
# is: availability is a correctness rule, not a presentation preference a
# template author should be able to relax.
#
# Enforced in three places from this one source of truth — the ad-hoc form, the
# matter form, and the CLI's --task choices — plus a server-side re-check on
# POST, so a hand-crafted request is a clean refusal rather than a nonsensical
# run.
# --------------------------------------------------------------------------
MATTER_ONLY_TASKS: dict[str, int] = {
    "compare_clauses": 2,  # task id -> minimum ingested files in the matter
    # Case discovery searches CanLII using concepts extracted from the matter's
    # own documents. With no documents there is nothing to extract, and the task
    # would degrade into a bare keyword search dressed up as matter-aware
    # research. One file is the honest minimum. Listing it here also keeps it
    # out of the CLI for free: cli.py offers available_tasks(None).
    "suggest_cases": 1,
}


def available_tasks(matter_file_count: int | None = None) -> list[TaskTemplate]:
    """Templates in display order, filtered to those runnable in this context.

    `matter_file_count` is the number of successfully-ingested files in the
    matter, or None for the ad-hoc single-file path (where every matter-only
    task is excluded)."""
    out = []
    for t in ordered_templates():
        minimum = MATTER_ONLY_TASKS.get(t.id)
        if minimum is not None:
            if matter_file_count is None or matter_file_count < minimum:
                continue
        out.append(t)
    return out


def task_unavailable_reason(task_id: str, matter_file_count: int | None) -> str | None:
    """A user-facing refusal message if `task_id` cannot run in this context, else
    None. Server-side counterpart to `available_tasks` — the form hides the task,
    this catches a POST that arrives anyway."""
    minimum = MATTER_ONLY_TASKS.get(task_id)
    if minimum is None:
        return None
    try:
        label = get_template(task_id).label
    except KeyError:
        label = task_id
    if matter_file_count is None:
        return f"{label} runs across the documents in a matter and is not available for a single uploaded file."
    if matter_file_count < minimum:
        return f"{label} needs at least {minimum} ingested files in this matter; this matter has {matter_file_count}."
    return None


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
    # Control inputs never enter the REQUEST section (see _request_lines).
    lines.extend(_request_lines(template, structured_inputs))
    return "\n".join(lines)


def _request_lines(template: TaskTemplate, structured_inputs: dict) -> list[str]:
    """The REQUEST section body, shared by both user-message builders so the two
    cannot drift on which inputs reach the model. Control inputs are excluded."""
    lines = [f"Task: {template.label}"]
    for field in template.inputs:
        if field.control:
            continue
        val = structured_inputs.get(field.name)
        if not val:
            continue
        rendered = ", ".join(val) if isinstance(val, list) else str(val)
        lines.append(f"{field.label}: {rendered}")
    return lines


def build_concept_extraction_prompt(jurisdiction: str) -> str:
    """System prompt for LLM call 1 of case discovery: matter passages plus a
    research direction -> de-identified legal concepts and CanLII queries.

    The CONFIDENTIALITY rules here are the load-bearing part. This is the only
    call in the task that sees matter text, and its output is what travels to a
    third party (CanLII). Everything downstream is scrubbed in code as well
    (`discovery.scrub_query`), because a prompt is an instruction and not a
    guarantee -- but the instruction comes first and is explicit about why.

    The QUERY SYNTAX rules encode what the live API actually does, which is not
    what its parameter names suggest: terms are OR-ed, AND/OR/NOT are treated as
    ordinary search words rather than operators, and quoted phrases measurably
    sharpen the ranking. See matter_clerk.canlii for the verification log."""
    vocabulary = (
        ONTARIO_VOCABULARY_NOTE
        if jurisdiction == "Ontario"
        else GENERAL_VOCABULARY_NOTE
    )
    return f"""\
You build case-law search queries for a Canadian lawyer. You are given passages
from the lawyer's own matter documents and their research direction. Your output
is a set of search queries for CanLII, plus a de-identified summary of the
matter's legal shape.

CONFIDENTIALITY -- the queries you produce are transmitted to CanLII, a third
party. The matter documents are privileged. Therefore:

  * Never put a party name, person's name, company name, address, unit or suite
    number, file or docket number, dollar amount, or specific date into a query
    or into any concept field.
  * Queries and concepts contain LEGAL VOCABULARY ONLY: doctrines, causes of
    action, statutory titles and section numbers, remedies, and the fact pattern
    described in the abstract ("water escape from a common element pipe", not
    "the leak in unit 315").
  * Statutory years are legal vocabulary and must be kept: "Condominium Act,
    1998" is the statute's name.

QUERY SYNTAX -- CanLII's search behaves as follows, which is not always what its
documentation implies:

  * Terms are OR-ed and results are relevance-ranked. A longer query is a
    broader query, not a narrower one.
  * AND, OR and NOT are NOT operators. They are matched as ordinary words and
    make results worse. Never use them.
  * "Quoted phrases" DO sharpen the ranking substantially. Build each query
    around one or two quoted phrases plus three to six bare discriminating
    terms.
  * Aim for six to twelve words per query.

{vocabulary}

ANGLES -- produce at most one query per angle, in this order:

{QUERY_ANGLES}

Emit between {MIN_QUERY_HINT} and {MAX_QUERY_HINT} queries. SKIP any angle the
matter and research direction do not genuinely support. Do not pad to a quota:
an invented angle returns confidently irrelevant cases and costs the lawyer more
time than a missing angle does.

OUTPUT -- return exactly one fenced JSON block and nothing else:

```json
{{
  "legal_issues": ["short noun phrases naming the legal issues in play"],
  "statutes": ["statutes or rules the DOCUMENTS engage, with section numbers"],
  "forum": "the court or tribunal that would hear this, if evident",
  "party_types": ["generic roles, e.g. condominium corporation, unit owner"],
  "fact_pattern": "one sentence, abstract legal vocabulary, no identifiers",
  "queries": [
    {{"angle": "doctrinal_core",
      "query": "\\"duty to repair\\" \\"common elements\\" condominium corporation liability",
      "rationale": "one short sentence on what this query is looking for"}}
  ]
}}
```"""


def build_case_note_prompt() -> str:
    """System prompt for LLM call 2: the preliminary 'why this surfaced' notes.

    Prepends the code-owned CASE_DISCOVERY_PREAMBLE, which is what makes the
    no-holding-claims rule non-removable in the same literal sense as
    SAFETY_PREAMBLE: it does not live in any template file, so no template edit
    can weaken it."""
    return f"""{CASE_DISCOVERY_PREAMBLE}

TASK: For each candidate case, write ONE sentence explaining why its metadata
connects to this matter.

Each sentence must:
  * name the specific catchword, topic, statute, forum, or party type that
    overlaps with the matter's concepts;
  * be phrased as an observation about the METADATA, not about the case. Write
    "its catchwords cover X", not "it deals with X"; write "CanLII files it
    under Y", not "it decided Y";
  * stay under 45 words.

If a case's metadata gives no honest basis for a connection, write exactly:

    Metadata gives no clear connection - surfaced by the "<angle>" search.

substituting the angle named for that case.

Never state or imply what any case held. If you find yourself about to write
what a case decided, you have exceeded what the metadata supports: describe the
catchword overlap instead.

OUTPUT -- return exactly one fenced JSON block and nothing else. Include one
entry for EVERY candidate case, with the citation copied character-exactly from
the input so it can be matched back:

```json
{{"notes": [
  {{"citation": "2020 ONCA 471 (CanLII)",
    "note": "Its catchwords cover exclusive-use common elements and maintenance and repair obligations, the same subject matter as this matter's declaration dispute."}}
]}}
```"""


# Kept in sync with discovery.MIN_QUERIES / MAX_QUERIES. Duplicated as plain
# ints rather than imported because prompts.py must not import discovery.py
# (discovery imports prompts); the pair is asserted in the discovery tests.
MIN_QUERY_HINT = 5
MAX_QUERY_HINT = 10


def build_comparison_user_message(
    template: TaskTemplate,
    structured_inputs: dict,
    groups: list[tuple[str, list[dict]]],
) -> str:
    """User message for Compare Clauses: a FILE MANIFEST plus per-document
    passage groups (Day 4c).

    `build_user_message`'s flat CONTEXT list cannot express the fact this task is
    built on — that a document was searched and yielded nothing. A file missing
    from a flat list is indistinguishable from a file that was never looked at,
    which is precisely the ambiguity the "Not present in this document" cell
    exists to remove. So the manifest names every document up front (with its
    passage count, including zero), and each document's passages sit under their
    own header.

    `groups` is ordered [(source_filename, [chunk dicts]), ...]; that order
    becomes the comparison's column order. Chunk dicts carry the same
    source/locator/text keys as `build_user_message`, so the per-passage
    [SOURCE: ...] headers — and therefore the citation discipline — are
    byte-identical to every other task."""
    lines: list[str] = ["FILE MANIFEST", ""]
    lines.append(
        "Every document listed below was searched for the requested clause. The "
        "comparison table must contain one column for each of them, in this order:"
    )
    for i, (source, chunks) in enumerate(groups, 1):
        note = (
            f"{len(chunks)} passage(s) retrieved"
            if chunks
            else "NO passages retrieved"
        )
        lines.append(f"{i}. {source} - {note}")
    lines.append("")

    lines.append("CONTEXT:")
    lines.append("")
    for source, chunks in groups:
        lines.append(f"=== DOCUMENT: {source} ===")
        lines.append("")
        if not chunks:
            lines.append(
                "(no passages relevant to the requested clause were retrieved "
                "from this document)"
            )
            lines.append("")
            continue
        for c in chunks:
            lines.append(f"[SOURCE: {c['source']} {c['locator']}]")
            lines.append(c["text"])
            lines.append("")

    lines.append("REQUEST:")
    lines.extend(_request_lines(template, structured_inputs))
    return "\n".join(lines)

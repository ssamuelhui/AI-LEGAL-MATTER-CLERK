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
# Authority mode (Phase 2b) -- Draft Memo and Draft Pleading only.
#
# Matter-only mode forbids the model from invoking any external legal authority,
# which is safe but leaves a memo full of gap markers. Authority mode lifts that
# prohibition for these two tasks and instead catches fabrication AFTER
# generation: every citation the model produces is checked against CanLII, and
# one that does not resolve is stripped from the document.
#
# Two things about this text are load-bearing.
#
# First, it REPLACES the matter-only prohibition rather than being appended
# alongside it. An appended permission would leave the prompt holding two
# contradictory instructions ("you must not cite any case" and "you may cite
# cases"), and a model free to follow either is a model whose citation
# behaviour is unpredictable — the opposite of what this feature is for.
#
# Second, rule 4's escape hatch does real work. Fabrication happens when a model
# has no permitted way to say "authority is needed here and I do not have it".
# Giving it an explicit, blessed way to say exactly that reduces invention far
# more than any amount of prohibition does.
#
# Third -- and this is what the first cut of this text got wrong -- the rules
# have to be CALIBRATED, not merely strict. The original rule 3 demanded
# certainty ("if you are not certain a case is real, DO NOT CITE IT"), and since
# no rule anywhere requires the model to cite anything, "cite nothing" satisfied
# every rule in the block perfectly. Both MiMo and Claude found that equilibrium
# and sat in it: zero citations on every authority-mode run, which made the mode
# functionally identical to matter-only. The fix is to state the positive
# expectation explicitly and to move the threshold to reasonable confidence,
# because the CanLII check downstream is what catches an existence error -- the
# model is not the last line of defence and should not behave as though it is.
# Rules 1 and 6 are NOT softened: verification checks that a case exists, and
# nothing anywhere checks that it held what the sentence claims it held.
# --------------------------------------------------------------------------
AUTHORITY_MODE_INSTRUCTION = """\
AUTHORITY MODE — you may cite real Canadian legal authority.

Support the legal analysis with real Canadian cases where they bear on the
issue. A document produced in this mode is EXPECTED to carry authority: citing
nothing at all is not the cautious answer, it is an incomplete one. Work within
the rules below.

1. Never invent a citation. Do not fabricate a case name, approximate a year or
   a number, reconstruct a half-remembered citation, or assemble a
   plausible-looking one out of fragments. A citation you made up is the single
   worst thing this system can produce.

2. Give every case its NEUTRAL CITATION in standard form — year, court,
   number: "2020 ONCA 471", "2021 SCC 7", "2019 ONSC 4484". A citation given in
   any other form cannot be checked and will be marked unverified in your
   output.

3. Cite when you have reasonable confidence a case exists. You do not need
   certainty. Verification will catch factually incorrect citations, so the
   threshold is reasonable confidence, not proof. Only decline to cite when you
   have no reasonable basis — in which case, either use the rule 4 marker or
   state the legal principle without attribution.

4. Where a proposition needs authority and you have no reasonable basis for a
   citation, write exactly, inline:
   "[AUTHORITY REQUIRED — lawyer to confirm: <state the proposition that needs
   authority>]"
   This marker is always available and using it is never a failure. It is the
   right move when you genuinely have nothing — not a substitute for a citation
   you could reasonably give under rule 3.

5. Prefer Supreme Court of Canada and Ontario authority. Label any
   out-of-province decision as persuasive only.

6. Never state that a case HELD something you are not certain it held. Rule 3
   does NOT relax this one. Reasonable confidence that a case exists is enough
   to cite it; describing its holding, facts, reasoning, or disposition requires
   real confidence in that description, and nothing downstream checks it. Cite a
   case for the proposition it supports and keep the characterisation minimal.

7. Every citation you give WILL be checked against CanLII after you finish. One
   that does not resolve is removed from your output and recorded in an audit
   log, and the lawyer is shown exactly what was removed. That safety net is
   precisely why rule 3 asks for reasonable confidence instead of certainty: an
   existence error is caught downstream, so you do not have to suppress a
   citation to be safe. It is not licence to guess — a citation you have no
   basis for belongs under rule 4 instead.

Authority mode changes NOTHING about facts. Every factual claim still carries
its inline [FILENAME p.N] citation to the matter documents exactly as required
above. This mode relaxes the prohibition on legal authority only."""


# The clauses of SAFETY_PREAMBLE that forbid external authority. In authority
# mode these two are replaced (not deleted -- the replacement re-imposes the
# fabrication rules in the form appropriate to the mode).
#
# Held as exact anchors and checked at template-load time so that an edit to
# SAFETY_PREAMBLE which orphans them fails loudly at startup rather than
# silently leaving matter-only prohibitions in an authority-mode prompt.
_PREAMBLE_AUTHORITY_ANCHOR = """\
2. You may cite only passages that appear in the CONTEXT section of this \
prompt. You must not cite any case, statute, regulation, rule, or other legal \
authority from memory or general knowledge -- even if you are confident the \
authority exists and even if the request seems to call for it.

3. You must not state the elements of a cause of action, the test for a \
remedy, the requirements of a statute, or the contents of a procedural rule \
from memory. These are legal authority and are not available in matter-only \
mode."""

_PREAMBLE_AUTHORITY_REPLACEMENT = """\
2. For FACTS, you may rely only on passages that appear in the CONTEXT section \
of this prompt. Every factual claim carries its [SOURCE: ...] citation as \
described in rule 1.

3. For LEGAL AUTHORITY, you are in AUTHORITY MODE (see the authority-mode \
instructions below). You may cite real Canadian cases subject to those rules. \
You must still not invent a citation, and you must not state that a case held \
something you are not certain it held."""

_PREAMBLE_MATTER_ONLY_LINE = (
    "You are operating in MATTER-ONLY MODE: no external legal authority has "
    "been retrieved for this run."
)
_PREAMBLE_AUTHORITY_LINE = (
    "You are operating in AUTHORITY MODE: you may cite real Canadian case law, "
    "and every citation you give will be verified against CanLII after you "
    "finish."
)

# Per-task matter-only text in the YAML bodies, removed in authority mode.
# Keyed by task id; each value is (exact text to replace, replacement).
#
# Kept in CODE rather than expressed as an alternative body in the YAML for the
# same reason SAFETY_PREAMBLE is code-owned: which prohibitions apply in which
# mode is a correctness rule, not drafting guidance a template author should be
# able to relax. Every anchor is asserted at load time by `check_authority_anchors`.
_TASK_AUTHORITY_SWAPS: dict[str, list[tuple[str, str]]] = {
    "draft_memo": [
        (
            "- Analysis: reason from the matter facts. You are in MATTER-ONLY "
            "MODE: no\n  external legal authority (cases, statutes, "
            "regulations, rules) has been\n  retrieved for this run.",
            "- Analysis: reason from the matter facts, and support the legal "
            "propositions\n  with real Canadian authority under the "
            "authority-mode rules below.",
        ),
        (
            'MATTER-ONLY MODE — mandatory and non-negotiable:\nYou have been '
            "provided NO external legal authority. You must not cite, name,\n"
            "paraphrase, or assert any case, statute, regulation, rule, legal "
            "test, or the\nelements of any cause of action from memory or "
            "general knowledge — not even if\nyou are confident it exists and "
            'not even framed as "generally" or "typically."\n\nWherever the '
            "analysis would require external legal authority to be complete,\n"
            "do not fill the gap. Instead insert, inline at that point, the "
            'exact sentence:\n\n"External legal authority is required to '
            "complete this point and is not\navailable in matter-only mode — "
            "[state specifically what authority is needed,\ne.g. 'the test for "
            "relief from forfeiture'].\"\n\nA memo that openly flags these gaps "
            "is correct. A memo that quietly supplies\nlegal authority from "
            "training knowledge is a critical failure.",
            "A memo that openly flags the points where authority is needed but "
            "unavailable\nis correct. A memo that quietly supplies a plausible "
            "but unverified case is a\ncritical failure.",
        ),
    ],
    "draft_pleading": [
        (
            "- You may NAME a cause of action or a defence, but you must NOT "
            "state its\n  legal elements, the governing statutory test, or the "
            "supporting authority\n  from memory. Where the elements or legal "
            "test are needed to complete a\n  paragraph, do NOT supply them — "
            'insert exactly, inline:\n  "[ELEMENTS REQUIRED — lawyer to '
            "complete: <name the cause of action / defence\n  and what must be "
            "established>; external legal authority is required and is\n  not "
            'available in matter-only mode.]"',
            "- You may name a cause of action or a defence and state its legal "
            "elements\n  where you can support them with real Canadian "
            "authority under the\n  authority-mode rules below. Where you "
            "cannot, do NOT supply the elements\n  from memory — insert "
            'exactly, inline:\n  "[ELEMENTS REQUIRED — lawyer to complete: '
            "<name the cause of action / defence\n  and what must be "
            'established>; authority for these elements was not\n  available.]"',
        ),
    ],
}


def check_authority_anchors(templates: dict) -> None:
    """Fail at startup if any authority-mode anchor no longer matches its
    template, if the SAFETY_PREAMBLE clauses have drifted, or if a template's
    `authority_mode` options have drifted from AUTHORITY_MODE_OPTIONS.

    Without this, a reworded template silently leaves matter-only prohibitions
    in an authority-mode prompt: the model would be told both that it may cite
    cases and that it must not, and the result would be neither mode.

    The options check guards a second, quieter failure of the same class. The
    radio's option string IS the value the form submits, and
    `authority_mode_enabled` gates on equality with AUTHORITY_MODE_ON — so a
    template whose options drift by one character does not error anywhere. The
    radio still renders, the lawyer still selects it, and the run silently
    falls back to matter-only with no case law and no explanation."""
    if _PREAMBLE_AUTHORITY_ANCHOR not in SAFETY_PREAMBLE:
        raise ValueError(
            "SAFETY_PREAMBLE rules 2-3 have drifted from "
            "_PREAMBLE_AUTHORITY_ANCHOR; authority mode cannot lift the "
            "matter-only prohibition. Update the anchor to match."
        )
    if _PREAMBLE_MATTER_ONLY_LINE not in SAFETY_PREAMBLE:
        raise ValueError(
            "SAFETY_PREAMBLE's matter-only mode sentence has drifted from "
            "_PREAMBLE_MATTER_ONLY_LINE."
        )
    for task_id, swaps in _TASK_AUTHORITY_SWAPS.items():
        template = templates.get(task_id)
        if template is None:
            continue
        for anchor, _replacement in swaps:
            if anchor not in template.system_prompt:
                raise ValueError(
                    f"{task_id}.yaml no longer contains the matter-only text "
                    f"that authority mode replaces. The anchor starting "
                    f"{anchor[:60]!r} was not found; update "
                    f"_TASK_AUTHORITY_SWAPS to match the template."
                )
    for task_id in sorted(AUTHORITY_MODE_TASKS):
        template = templates.get(task_id)
        if template is None:
            continue
        field = next(
            (f for f in template.inputs if f.name == "authority_mode"), None
        )
        if field is None:
            raise ValueError(
                f"{task_id}.yaml is in AUTHORITY_MODE_TASKS but declares no "
                f"`authority_mode` input, so authority mode cannot be reached "
                f"from the form. Add the input or remove the task from "
                f"AUTHORITY_MODE_TASKS."
            )
        if tuple(field.options or ()) != AUTHORITY_MODE_OPTIONS:
            raise ValueError(
                f"{task_id}.yaml `authority_mode` options "
                f"{tuple(field.options or ())} do not match "
                f"prompts.AUTHORITY_MODE_OPTIONS {AUTHORITY_MODE_OPTIONS}. The "
                f"option string is the value the form submits and is compared "
                f"against AUTHORITY_MODE_ON, so any difference silently "
                f"disables authority mode for this task."
            )


AUTHORITY_MODE_OPTIONS = ("Matter-only", "Matter + CanLII case authority")
AUTHORITY_MODE_ON = AUTHORITY_MODE_OPTIONS[1]

# Tasks permitted to run in authority mode (SoW Phase 2b scope). Code-owned:
# every other task's matter-only or case-discovery discipline is unchanged, and
# a template author must not be able to opt a ninth task in by editing YAML.
AUTHORITY_MODE_TASKS = frozenset({"draft_memo", "draft_pleading"})


def authority_mode_enabled(task: str, structured_inputs: dict | None) -> bool:
    """Whether this run is in authority mode. False for any task outside
    AUTHORITY_MODE_TASKS regardless of what the form submitted."""
    if task not in AUTHORITY_MODE_TASKS:
        return False
    si = structured_inputs or {}
    return si.get("authority_mode") == AUTHORITY_MODE_ON


def _authorize_preamble(preamble: str) -> str:
    """Lift the matter-only prohibition from SAFETY_PREAMBLE."""
    out = preamble.replace(_PREAMBLE_MATTER_ONLY_LINE, _PREAMBLE_AUTHORITY_LINE)
    return out.replace(
        _PREAMBLE_AUTHORITY_ANCHOR, _PREAMBLE_AUTHORITY_REPLACEMENT
    )


def _authorize_body(task: str, body: str) -> str:
    """Apply the per-task matter-only -> authority-mode text swaps."""
    for anchor, replacement in _TASK_AUTHORITY_SWAPS.get(task, []):
        body = body.replace(anchor, replacement)
    return body


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
        "file_multiselect", "number", "radio",
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
    # Phase 2b: fail loudly if a template edit orphaned an authority-mode
    # anchor, which would leave matter-only prohibitions inside an
    # authority-mode prompt.
    check_authority_anchors(out)
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
    output is character-identical to the single-document behaviour.

    Phase 2b: when `structured_inputs` selects authority mode on a task that
    permits it, the matter-only prohibition is REPLACED (never merely
    supplemented) and AUTHORITY_MODE_INSTRUCTION is appended. With authority
    mode off — the default — every byte of this function's output is unchanged
    from Phase 1."""
    authority = authority_mode_enabled(template.id, structured_inputs)

    preamble = SAFETY_PREAMBLE
    if cross_document:
        preamble = _matterize_preamble(preamble)
    if authority:
        preamble = _authorize_preamble(preamble)

    body = template.system_prompt.strip()
    if cross_document:
        body = _matterize(body)
    if authority:
        body = _authorize_body(template.id, body)

    parts = [preamble]
    if cross_document:
        parts.append(MATTER_CONTEXT_NOTE)
    parts.append(body)

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
        if cross_document:
            v = _matterize(v)
        if authority:
            v = _authorize_body(template.id, v)
        parts.append(v)

    # Appended AFTER the task body and any variant so it is the last word on
    # citation behaviour the model reads.
    if authority:
        parts.append(AUTHORITY_MODE_INSTRUCTION)
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

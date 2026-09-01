# Matter Clerk v1.0.3 — Exhaustive mode

**For the GitHub Release body.** The first paragraph is the in-app update
notification, so it is written to stand alone.

---

Timeline, Summarize and Find Entities can now run in exhaustive mode, which
reads every page of every file you select instead of the passages it judges
most relevant. Nothing is filtered out for you. It takes a few minutes and
costs more per run, so it is opt-in and tells you the expected cost before it
starts. All existing modes are unchanged.

## Why this exists

You told us the timeline and summary "aren't detailed enough — we don't want
the software to decide what to include and exclude." That was a fair
description of a real defect, and it was not the wording of the prompts. It was
how much of your matter reached the model at all.

Measured on a nine-file test matter:

| Mode | Passages read | Share of the matter |
|---|---|---|
| Timeline — Concise | 14 | 21% |
| Timeline — Detailed | 28 | 42% |
| Summarize — Standard | 12 | 18% |
| Find Entities — Standard | 16 | 24% |
| **Exhaustive** | **all 67** | **100%** |

That cap is a total for the whole matter, not per file — so on a 28-file matter
the same 14 passages are shared across every document, and coverage falls to
roughly 7%. The software was reading a small fraction of your matter and
summarising that.

Exhaustive mode reads all of it. On the test matter it produced **63 dated
events** where the standard mode was working from 14 passages.

## Using it

- **Timeline** — the *Detail level* menu gains **Exhaustive** alongside Concise
  and Detailed.
- **Summarize** and **Find Entities** — a new *Analysis mode* menu with
  Standard and **Exhaustive (preview)**.

Combine it with the file selector from v1.0.2 to run exhaustively over the
files that matter rather than the whole matter.

Before it starts, you are shown the number of files and passages, the expected
time, and the estimated cost, and asked to confirm. While it runs you see a
progress view with a live tally of what has actually been spent. **You can
close the tab and come back** — the analysis keeps running, and the page picks
it up again. There is a Cancel button; anything already finished is kept.

## What it costs, and which model

Exhaustive runs use `anthropic/claude-opus-4.7` regardless of the model
configured in your settings, and the model is named on the result. Every other
task continues to use your configured model.

Measured on the nine-file test matter: **170 seconds and $0.70** for a full
exhaustive Timeline. A two-file subset took 23 seconds and $0.08. A 28-file
matter is likely to be six to nine minutes.

## Preview status

Exhaustive mode for **Summarize** and **Find Entities** is labelled *preview*.
That refers to the **mode**, not the task — Summarize and Find Entities
themselves are unchanged and are not preview features. It means we have
validated exhaustive extraction most thoroughly on Timeline, and you should
check the output of the other two more carefully than usual.

Timeline exhaustive is not a preview.

## Honesty about what it does not do

- Passages repeated **within one file** (chunk overlap) are collapsed, and the
  run tells you how many were — including when the answer is none.
- Passages repeated **across files** are deliberately *not* merged. Two
  documents describing the same date are usually two pieces of evidence about
  it, and collapsing them would hide corroboration. Expect some repetition
  between files; that repetition is information.
- If a file cannot be read from the search index, the result says so by name.
  "Exhaustive" is a claim about coverage, and an unread file falsifies it.

## Unchanged

Concise, Detailed and Standard modes produce exactly what they produced in
v1.0.2 — the prompts and the retrieval they use are byte-for-byte identical.

---

## Update notification text

> **Update available: v1.0.3**
> Timeline, Summarize and Find Entities can now run in exhaustive mode, which
> reads every page of every file you select instead of the passages it judges
> most relevant. Nothing is filtered out for you. It takes a few minutes and
> costs more per run, so it is opt-in and tells you the expected cost before it
> starts. All existing modes are unchanged.
> [Install now] [Later]

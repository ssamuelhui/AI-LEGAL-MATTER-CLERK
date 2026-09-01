# Matter Clerk v1.0.2 — Choose your files, and see them in order

**For the GitHub Release body.** The first paragraph is what appears in the
in-app update notification, so it is written to stand alone.

---

Every task can now be pointed at particular files instead of the whole matter —
all files, a selection, or a single one — and your file list is sorted by the
date in the filename rather than upload order. Both are optional: if you do not
touch the new controls, tasks behave exactly as they did before.

## New: choose which files a task runs against

Compare Clauses already let you pick files. Now every task does — Timeline,
Summarize, Find Facts, Find Entities, Draft Memo, Draft Correspondence, Draft
Pleading and Suggest Relevant Cases.

Above each task's options you will find:

- **All files in this matter** — the default, and what happens today
- **Selected files** — tick the ones you want
- **A single file** — pick one from a list

Useful when a matter has grown past the point where "everything" is the right
answer: a chronology of one email chain, a summary of just the expert reports,
facts drawn only from the pleadings.

The result page now says what the run was scoped to — *"Ran against 5 of 28
files: …"* — so a narrowed run is never mistaken later for a complete one.

For **Suggest Relevant Cases**, the selection is labelled *"Files whose
concepts guide CanLII search"*: those files shape what is searched for, rather
than being searched themselves.

Files that cannot be searched still appear in the list, greyed out with the
reason, rather than quietly vanishing. Files marked "Poor scan quality" can be
selected normally.

## New: files sorted by date

Files are now ordered by a date at the start of the filename, oldest first, so
a matter reads chronologically. These all work:

```
2026-03-27 - Technician Report Form.pdf
2026-01-21 - 2026-03-26 - Condo management email.pdf
2024-04-01 to 2026-04-30 - email exchange.pdf
2024-03-15_letter.pdf
24-03-15_letter.pdf
```

Date ranges sort by their start date. Files without a leading date sort
alphabetically after the dated ones.

A **Sort** control at the top of the file list switches between *Oldest first*,
*Newest first* and *Upload order*. Your choice is remembered per matter.

Ambiguous names are deliberately left alone: `3-15-24_letter.pdf` could be
March 15th or the 24th of some month, so it sorts alphabetically rather than
being guessed at. A wrong chronology is worse than none.

## Fixed

- Files marked **"Poor scan quality"** could not be selected individually, even
  though searching the whole matter included them. They are now selectable
  everywhere.

## Also

- The support report has moved to a quieter **Generate support report** link at
  the bottom of the matters page, and now writes a plain-English README beside
  it explaining what to send and what the file does and does not contain.

## On upgrading

Nothing to do. No changes to your matters, documents, settings or search index,
and nothing to re-process. The file list will simply be in a different order
the first time you open a matter.

---

## Update notification text

> **Update available: v1.0.2**
> Every task can now be pointed at particular files instead of the whole
> matter — all files, a selection, or a single one — and your file list is
> sorted by the date in the filename rather than upload order. Both are
> optional: if you do not touch the new controls, tasks behave exactly as they
> did before.
> [Install now] [Later]

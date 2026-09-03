# Matter Clerk v1.0.6 — What each task costs

**For the GitHub Release body.** The first paragraph is the in-app update
notification, so it is written to stand alone.

---

Every task run now shows what it cost, right at the top of the result, ready to
write into your billing system. A new cost log lists every run with its cost,
filterable by matter and date, exportable to CSV for firm records. The figures
are what OpenRouter actually charged, not an estimate.

## The cost of a run, where you need it

When a task finishes, the result opens with the amount:

> **$0.47**
> This run · `anthropic/claude-opus-4.7` · 4,232 in / 1,847 out tokens · 8.3s · copy amount

The dollar figure is the largest thing on the block because it is the one thing
being copied out. **Copy amount** puts just the number on the clipboard — a
billing field wants `0.47`, not a sentence.

Exhaustive runs show the same block, with the number of model calls it took.

## The cost log

**Task costs**, linked from the foot of the matters page and from Settings.
One row per run: date, matter, task, model, cost. Sort by any column, filter by
matter or by date range, and see the total for whatever you are looking at:

> Filtered total: **$1.20** across 4 tasks

**Export to CSV** downloads exactly the rows you are looking at, with the token
counts and durations included, in a format that opens straight into Excel.

Runs that failed or were cancelled appear too, with what they cost before they
stopped — *cancelled at batch 8 of 12*, *failed after 3s*. Tokens spent are
spent, and a run you are looking for should be findable rather than absent.

## These are measured figures, not estimates

OpenRouter reports what it actually billed for each call, and that is the
number recorded. It is not calculated from a price list, which means it stays
correct when prices change, when a request is served from cache at a lower
rate, and when your request is routed to a different provider.

Two consequences worth knowing:

- **Costs are recorded from v1.0.6 onward.** Earlier runs were not measured.
  Exhaustive runs from v1.0.3–v1.0.5 are recovered from the audit log on first
  launch, since those did record a cost.
- **CanLII searches are not included.** That is a separate account with separate
  billing, and mixing the two would misstate both.

## Also fixed

The estimated cost shown before an exhaustive run was wrong for most models.
It was calculated from a table of three, so anything else was quoted at Claude
Opus rates — far too high for a cheap model, and far too low for an expensive
one. Estimates now use the full model catalogue.

## Unchanged

Matters, files, search indexes, settings and model choices are untouched. The
database gains one table. Nothing about how tasks run, retrieve or cite has
changed.

---

## Update notification text

> **Update available: v1.0.6**
> Every task run now shows what it cost, right at the top of the result, ready
> to write into your billing system. A new cost log lists every run with its
> cost, filterable by matter and date, exportable to CSV for firm records. The
> figures are what OpenRouter actually charged, not an estimate.
> [Install now] [Later]

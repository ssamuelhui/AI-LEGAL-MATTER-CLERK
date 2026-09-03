# Matter Clerk v1.0.5 — Delete and restore, choose your model, change your keys

**For the GitHub Release body.** The first paragraph is the in-app update
notification, so it is written to stand alone.

---

You can now delete files and matters, with 30 days to change your mind; choose
which AI model each task uses from the full OpenRouter catalogue; and change
your OpenRouter and CanLII keys from a new Settings page instead of editing a
file. Nothing you have already set up changes.

## Deleting, and undoing it

**Files** have a Delete button in the file list. One confirmation, and the file
disappears from the matter.

**Matters** have a Delete section at the foot of the matter page. You type the
matter's name to confirm — deliberately more friction than a single click,
because deleting a matter removes a great deal at once.

Neither is permanent. Everything goes to **Deleted items**, linked from the
foot of the matters page, showing what was deleted, when, and how many days
remain. Restore is one click. After 30 days items are removed for good,
including their search index.

Your original documents on disk are never touched by any of this.

Two details worth knowing:

- **Re-uploading a file you deleted restores it**, rather than reporting a
  duplicate. If you delete something and change your mind, dragging it back in
  does what you meant.
- **A deleted matter keeps its name reserved** until it is restored or expires.
  Creating a new matter with that name points you at Deleted items instead.

## Choosing a model per task

Each task can now use a different AI model, remembered across matters. Set them
on the Settings page, or on the task form itself.

Three models are starred and listed first — these are the ones the tasks have
actually been tested against:

| | Model | Rough cost |
|---|---|---|
| ★ | `xiaomi/mimo-v2.5-pro` (default) | `$` |
| ★ | `anthropic/claude-opus-4.7` | `$$$` |
| ★ | `anthropic/claude-sonnet-5` | `$$` |

Below them, the rest of OpenRouter's catalogue — 425 models — searchable by
name or provider. Type `opus` or `anthropic` or `gpt` and the list narrows.

Cost indicators are a guide, not a quote: `$` is the cheaper half of the
catalogue, `$$$` the most expensive tenth. Choosing a model outside the starred
three shows a note that it has not been tested with these tasks.

**Exhaustive mode still always uses Claude Opus 4.7**, whatever you select.
Weaker models silently omit events, which defeats the point of reading every
page (see the v1.0.3 release notes). When you pick a different model and switch
on exhaustive, the form tells you plainly, and the result records both the
model you asked for and the model used.

## Changing your API keys

The new Settings page has an **API keys** section for both OpenRouter and
CanLII. Keys are shown masked, with a Show toggle, and each new key is
**tested against its service before it is saved** — a mistyped key is refused
there rather than failing later in the middle of a task.

Changing a key does not interrupt anything running. A task in progress
finishes on the key it started with; the next one uses the new key.

Keys are never written to the logs, not even partially — the length of a key is
itself information, so the masked form hides that too.

## Unchanged

Existing matters, files, settings and search indexes are untouched. The
database gains one column and no data is rewritten. PDF, Word, Excel and email
ingestion, the file selector, exhaustive mode and citation behaviour are all
exactly as they were in v1.0.4.

---

## Update notification text

> **Update available: v1.0.5**
> You can now delete files and matters, with 30 days to change your mind;
> choose which AI model each task uses from the full OpenRouter catalogue; and
> change your OpenRouter and CanLII keys from a new Settings page instead of
> editing a file. Nothing you have already set up changes.
> [Install now] [Later]

# Matter Clerk v1.0.1 — Reliability improvements

**For the GitHub Release body.** The first paragraph is what appears in the
in-app update notification, so it is written to stand alone.

---

Reliability improvements for matters containing files that could not be fully
read. Matter Clerk no longer fails an entire task when one file's search index
is damaged, tells you which files are affected, and lets you re-process them.
Adds automatic update checking, and a diagnostic report you can send us.

**This release contains a crash class rather than curing it.** The underlying
cause is still under investigation. A diagnostic tool is included so that
anyone hitting problems can send us the state information we need to identify
the root cause — it reports structure only, and contains no document text, no
matter names, no file names and no API keys.

## Fixed

- **A single unreadable file no longer breaks a whole task.** Previously, if
  one file's search index was damaged, running Find Facts (or any
  cross-document task) over the matter failed with "Internal Server Error".
  Now that file is skipped, the task completes on the rest, and the result page
  says plainly which files did not contribute.
- **Results tell you when they are incomplete.** A banner above the answer
  names the files that could not be read, so you know before you rely on it,
  not after. The pleading limitation review does the same, because an
  incomplete limitation scan must never read like a clean one.
- **No more raw "Internal Server Error" pages.** Unexpected failures now show
  an explanatory page with next steps, and the full details are written to your
  audit log.
- **Files that produce no readable text are no longer recorded as ready.** A
  scan that yields nothing searchable is marked "No readable text" and excluded
  from tasks, instead of silently contributing nothing.
- **Missing API keys are explained rather than crashing.** Running a task with
  no OpenRouter key configured now says so, and points at the fix.

## New

- **Scan quality warnings.** Files whose OCR output looks too poor to answer
  from are marked "Poor scan quality". They remain searchable — you decide
  whether a clearer copy is worth chasing.
- **Re-process and clean up.** Every file that is not fully ready has a
  *Re-process* button, and matters with several unreadable files offer a bulk
  removal. Your original documents on disk are never deleted.
- **Automatic update checking.** Matter Clerk checks for new versions at
  startup and offers them on the matters page. Nothing is ever downloaded or
  installed without your confirmation, and if you are offline or behind a
  proxy the check fails silently.
- **Diagnostic report.** A button on the matters page writes a file describing
  your installation's structure — how many matters and files, and which parts
  of the search index are healthy. It contains no client content.

## On upgrading

When you first start v1.0.1, it checks each file in your matters against the
search index. Any file it cannot read is marked as needing re-upload, and you
will see a one-time message saying how many were affected.

**Your documents, matters and settings are not modified.** Re-marking a file
changes only its status in Matter Clerk's own list; the file on disk is
untouched, and re-processing or re-uploading it restores it.

## Known limitations

- The root cause of the original crash is not yet identified. If you hit a
  problem, please use the diagnostic report — it is what will let us find it.
- Poor OCR on faint or skewed scans remains the main reason a matter yields
  thin results. v1.0.1 tells you when this is happening; it does not improve
  the OCR itself.
- The installer is unsigned, so Windows SmartScreen will warn on first run.

---

## Update notification text

The in-app bar shows the version and the first paragraph above. Kept short
deliberately — it appears on the matters page while someone is trying to work.

> **Update available: v1.0.1**
> Reliability improvements for matters containing files that could not be
> fully read. Matter Clerk no longer fails an entire task when one file's
> search index is damaged, tells you which files are affected, and lets you
> re-process them. Adds automatic update checking, and a diagnostic report you
> can send us.
> [Install now] [Later]

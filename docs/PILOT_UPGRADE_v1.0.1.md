# Message for the pilot lawyer — upgrading to v1.0.1

Two pieces below: an email you can send more or less as written, and a
separate short note on the diagnostic tool that you can forward to anyone who
hits a problem later.

---

## 1. Email

**Subject: Matter Clerk update (v1.0.1) — fixes the error you hit**

Thanks for the report — the "Internal Server Error" you got from Find Facts was
a real bug, and this update addresses it.

**What was going wrong.** Your matter had files whose search index could not be
read. Matter Clerk was treating those files as ready, and when a task tried to
search across all 28 of them, the one bad file brought down the whole request.
That is also why Timeline gave you so little: several files were contributing
nothing, and nothing said so.

**What v1.0.1 does.** A file that cannot be read is now skipped rather than
fatal, so the task completes on the rest — and the result page tells you which
files did not contribute, so you always know when an answer is based on part of
the matter rather than all of it. Files that produce no readable text are
flagged in the file list, with a **Re-process** button, and files whose scan
quality is too poor to answer from are marked as such.

**One thing I want to be straight with you about.** We have contained this
failure rather than found its cause. I have not been able to reproduce the
exact corruption on a test machine, so I cannot promise it will not recur —
what I can promise is that if it does, it will no longer cost you the whole
task, and you will be told. The update also adds a **Create diagnostic report**
button on the matters page. If anything looks wrong, press it and email me the
file it creates. It contains no document text, no client names and no file
names — only structure — so it is safe to send.

### How to install

1. Close Matter Clerk if it is running.
2. Download **MatterClerk-Setup.exe** (attached / linked below).
3. Double-click it. Windows will warn you that the publisher is unrecognised —
   click **More info**, then **Run anyway**. That warning is expected; the
   installer is not code-signed.
4. Install over the top of your existing copy. Keep the default location.
5. Start Matter Clerk from the Start Menu as usual.

You will **not** be asked for your API keys again — your existing settings are
kept.

### What you will see the first time

A message at the top of the matters page saying that some files have been
marked as needing re-upload, with a count.

**This does not mean anything has been deleted.** Your documents, matters and
settings are all untouched. Matter Clerk has checked each file against its
search index and flagged the ones it cannot read, so that it stops quietly
pretending they are contributing.

### What to do next

Open the matter with the 28 files. In the file list you will see a status
against each one:

| Status | Meaning |
|---|---|
| **Ready** | Fine, searchable |
| **Poor scan quality** | Searchable, but the scan was hard to read — answers from it may be thin |
| **No readable text** | Not searchable, excluded from all tasks |

For anything not "Ready", press **Re-process**. That rebuilds the index from
the copy Matter Clerk already holds and takes a few seconds per file.

If a file still will not read after that, the source scan itself is the
problem. Re-upload a clearer copy if you have one — a higher-resolution scan,
or a text-based PDF rather than a photocopy. Given that Timeline found only two
events across 28 files, I suspect a good number of those scans are simply too
faint for the text recognition, and v1.0.1 will now tell you which.

Once the files show as Ready, run Find Facts again.

### After this one

This is the last update you will have to install by hand. From now on Matter
Clerk checks for new versions itself and offers them on the matters page — you
choose whether and when to install, and nothing downloads without you saying so.

---

## 2. Note on the diagnostic tool (forward to anyone reporting a problem)

**If Matter Clerk is behaving oddly:**

1. Open Matter Clerk.
2. On the matters page (the first screen), scroll to **Having trouble?**
3. Click **Create diagnostic report**.
4. A message appears giving the file's location — something like
   `C:\Users\<you>\AppData\Local\MatterClerk\matter-clerk-diagnostic-20260831-142530.json`
5. Email that file to your Matter Clerk contact.

The same button is on the error page, so it is available at the moment a
problem occurs.

**What the report contains:** the version you are running, how many matters and
files you have, each file's status and whether its search index is healthy, and
whether the underlying database opens.

**What it does not contain:** any document text, any matter name, any file
name, any client name, and no API keys. File names are reduced to their type
(`.pdf`) and length. It is designed so that you do not have to read it before
sending it.

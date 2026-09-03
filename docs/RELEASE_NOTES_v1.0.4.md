# Matter Clerk v1.0.4 — Word and Excel files

**For the GitHub Release body.** The first paragraph is the in-app update
notification, so it is written to stand alone.

---

You can now add Word documents (.docx) and Excel spreadsheets (.xlsx) to a
matter, alongside PDFs and emails. They are searched, cited and used by every
task exactly like any other file. Citations name the section or the sheet and
rows, so you can open the source and find the passage.

## Adding Word and Excel files

Drop them into a matter the same way you add a PDF. They appear in the file
list, in the file selector, and in exhaustive analysis, with no separate step.

**Word documents** are read section by section, following the document's own
headings. Citations name the section:

```
[Master Services Agreement.docx §2.3 Termination]
[Master Services Agreement.docx Table 3 "Fee Schedule", rows 4-9]
```

Tables are kept intact — never split part-way through a row — because half a
row of a schedule reads as a different fact. Where a document has no headings,
citations fall back to paragraph numbers.

**Excel spreadsheets** are read sheet by sheet, in blocks of rows, with the
column headings repeated so a citation tells you what you are looking at:

```
[Student list.xlsx sheet 'Invoice on May 20, 2026', rows 15-24
 (cols: Student ID | Last Name | First Name | DOB | Gender ...)]
```

Row numbers are the real Excel row numbers, so you can open the file and go
straight there. Dates are read as dates rather than as timestamps, so a
Timeline picks them up properly.

## Amended contracts are read correctly

Word documents with tracked changes are read **as if all changes were
accepted** — insertions included, deletions excluded.

This was worth doing carefully. The standard library for reading Word files
drops tracked insertions silently, which would have meant an amended contract
being searched with its amendments missing and nothing saying so. Matter Clerk
reads the underlying document directly to avoid that.

## What is deliberately not read

- **Comments** in Word, and **cell comments** in Excel. Review notes are often
  privileged and should not become searchable.
- **Deleted text** in tracked changes.
- **Images**, in either format. A Word file containing only scanned images is
  marked "No readable text" rather than appearing to have been read.
- **Formulas** as text. Excel cells contribute their calculated result — what
  the cell says, not how it was worked out.

## Files Matter Clerk cannot open

- **Password-protected** files are now identified as such, rather than
  reported as broken. Remove the password in Word or Excel, save a copy, and
  upload that.
- Files saved in the older **.doc** or **.xls** formats need re-saving as
  .docx or .xlsx.
- A spreadsheet written by another program and never opened in Excel may have
  formulas with no saved results. Matter Clerk says so and asks you to open
  and save it, which is a different fix from a damaged file.

## Unchanged

PDF and email ingestion is byte-for-byte identical to v1.0.3 — verified by
hashing the chunks produced for every file in the test matter before and after
this change.

---

## Update notification text

> **Update available: v1.0.4**
> You can now add Word documents (.docx) and Excel spreadsheets (.xlsx) to a
> matter, alongside PDFs and emails. They are searched, cited and used by every
> task exactly like any other file. Citations name the section or the sheet and
> rows, so you can open the source and find the passage.
> [Install now] [Later]

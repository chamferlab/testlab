# TestLab roadmap

Where the project stands and what comes next. Written 2026-10-12, last updated 2026-10-23; update it at the end of
each working session.

## Where we are

- `main` on GitHub holds everything up to M2F-B2 (Result revisions), the
  test workflow and the rough Final Report (840 tests pass with it).
- Built: Project, Test Program (minimal), TCN, Test Execution (manual log,
  DCS data, running intervals, Excel log import), Test Result (components,
  pictures, plots, seal-face measurements, acceptance), Test Run Report
  (Customer and Engineering profiles, issued documents, Result revisions).
- The **Test Summary** step is covered by the Test Run Report: it has the
  content of the old Word "Dynamic Test Run Summary" (objective, seal
  configuration, conditions and results, leakage, logged data,
  measurements, pictures, conclusion). Confirmed 2026-10-13: the Test Run
  Report replaces the Test Summary; there is no separate summary document.
- The **Final Report** (project page → Final Report): texts, included runs,
  generated summary table, one section per run from the frozen records,
  draft PDF, and since 2026-10-14 issuing: `<project>R-R0`, `-R1`, … stored
  once, the earlier one Superseded, a reason from R1, refused while an
  included run has a Result revision in progress. The texts stay editable
  after issuing; the page says what changed since the issue. Appendices
  with the tester drawing and system P&ID since 2026-10-23. Missing: the
  Test Program texts it could start from (the Test Program holds
  acceptance requirements only).
- Validated on Windows: up to M2D and the Chromium report (2026-10-05).
  M2E, running intervals, log import, M2F-a, B1 and B2 are not.
  The automated tests pass on Windows and Linux on GitHub for 3fe4635
  (2026-10-13); trying those parts by hand on the workstation is still open.
- `data/` is **test data and can be thrown away**, but it is made from
  real material: real projects (for example 2026RSD06TL TCN001, from start
  to finish), real DCS exports, measurement files and photos. A change
  counts as working when it works on that data, not only in the automated
  tests.
- `data/` is on this machine only and kept out of git because of its size
  (about 60 MB for one full run, mostly original photos and DCS data;
  screens and reports already use a reduced copy of each photo).

## How we work

- Requests come directly from the engineer; no intermediate prompt writer.
- Commit only when told. Suggested, not yet decided: one commit per
  accepted milestone.
- Larger changes: proposal first, code after approval.

## Agreed plan, in order

1. **Tests on Windows at every push.** `.github/workflows/tests.yml` runs
   the test suite on a fresh checkout on Windows and Linux. Added
   2026-10-12.
2. **Rough Final Report.** Done 2026-10-12, to be tried on real projects.
   What it showed so far:
   - The run sections reuse the Customer Test Run Report as it is; their
     status line ("Preview", "Customer report") was replaced by a Source
     line (the Customer document, else "Final Result", and the Result
     revision) on 2026-10-14.
   - The summary table reads speed, medium and pressure from the
     conditions table by row name; a record field for them would be
     sturdier.
   - Only one run of the real data is final, so the report over several
     runs is proven by the automated tests only.
3. **Evidence corrections.** Decided and done 2026-10-14, the simple way:
   the manual log, DCS data, running intervals and log imports are locked
   while the Result is Final and open again, like the rest of the Result,
   while a revision is in progress. The revision reason says why; each final
   revision keeps its own frozen copy of the evidence, so earlier revisions
   and their documents never change. No per-correction entries with their
   own reason ("not vaccine manufacturing: change is not harmful").

Then, in no fixed order:

| Change | Size | Touches |
|---|---|---|
| Requirements flow one way, Program → TCN → Result, instead of being synced from the Result side | Medium | `results/requirements.py`, Test Program, TCN requirement changes |
| One change log for the whole app (who changed what, when) | Medium | New table, hooked into every save |
| Locks in one place instead of per-table checks — **done 2026-10-23**: `testlab/locks.py` holds every edit lock (what locks what, the page messages and the backstops); the Result and the run's evidence share one message | Small | `results/models.py`, `tcn/models.py` |
| Backup of `data/` to another disk, needed once the app holds records that must be kept | Small | Run script or a scheduled job |
| "On record" from an explicit list of what each revision froze, instead of by time added | Small | `results/models.py`, `results/records.py` |
| Tester drawing and system P&ID in the Project Defaults (below) — **built 2026-10-17; in the Final Report appendices 2026-10-23, under review** | Small | `projects/` model and Project Defaults screen |
| PowerPoint slides of a project's tests, filled from the frozen records (below) — **built 2026-10-17; measurements slide 2026-10-23, under review** | Medium | New export next to `reports/`; template path in the settings, not in git |

### PowerPoint slides (built 2026-10-17, under review)

Built (`reports/slides.py`): *Slides (.pptx)* on the Final Report page, all
slides below; the measurements are on a slide of their own after the
run's results (2026-10-23): waviness = ROUNDPAK instrument flatness FLTt,
taper and Ra from Mahr, Pre and Post, as in the run's measurement summary
(so only when the Customer contents show that summary). The run's results slide shows running
time, leakage and acceptance with the leakage chart. Template from
`data/slides-template.pptx` or `TESTLAB_SLIDES_TEMPLATE` (layouts picked by name and placeholders), else
plain widescreen slides. New libraries: `python-pptx`, `pypdfium2` (a PDF
tester drawing as a picture). The cumulative leakage chart is on a run's
slide only when it is selected for the Customer report, as in the Final
Report.

Proposal as written before building:

Example: `reference/ACWA Seal Testing.pptx` (Flowserve 2025 template,
13.33 × 7.5 in), filled by hand today. Proposed: an *Export slides* button
on the Final Report page that writes a `.pptx` from the same frozen records
and Customer contents as the Final Report. It is an editable presentation
copy, never a controlled document: not issued, not stored, not numbered.
The engineer adjusts it in PowerPoint where needed, so the generator places
the content well but does not have to solve every fit.

Slides:

| Slide | Content | From |
|---|---|---|
| Project 1 | Title, date, tester drawing | Project, Final Report, tester drawing (needs the item below) |
| Project 2 | Test matrix: run, faces, fluid, speed, temperature, pressure | Final Report summary table |
| Project 3 | Parts: position, part number, material, New or Reused | Result components of the included runs |
| Per run 1 | Divider: run title, part, `<project> Test N<run>` | Run, TCN |
| Per run 2 | Test conditions, conclusion, seal cross section | TCN conditions, Result conclusion, cross-section picture |
| Per run, one per DCS plot | The saved plot with its caption | Saved Result plots (PNG) |
| Per run 3 | Pre/post radial taper and Ra (Mahr), waviness (ROUNDPAK); run time, total leakage and rate per seal; cumulative leakage chart | Seal-face analyses, running hours, leakage, leakage chart |
| Per run, as many as needed | Every photo of the run's Customer Test Run Report, with its caption, a fixed number per slide | Result pictures selected for the Customer report |

Template: kept **outside the repository**. The company template changes
every year, so the export takes a `.pptx` template from a path in the
settings and uses its layouts (title, section, title-only). Without one
it writes barebone slides (plain title and content on a blank widescreen
deck), which the engineer can move onto the current template.

Notes:
- Slides have no page flow: tables and photos are split over extra slides
  at a fixed number of rows or photos; long texts are left for the
  engineer to shorten.
- Charts and figures are SVG; `python-pptx` cannot insert SVG, so they are
  turned into PNG with Chromium first. The DCS plots are PNG already.

Answered 2026-10-17:
1. Photos: all the photos of the run's Test Run Report.
2. Measurements: radial taper and Ra from the Mahr profile, waviness from
   ROUNDPAK.
3. Parts: a summary table of part details, part number and condition (New,
   or Reused from a previous run).
4. One slide per DCS plot; the deck is editable, the engineer adjusts.
5. The tester drawing is missing from TestLab as a whole: add it to
   TestLab first (item below), then to the front page of the deck.
6. The template stays out of git; barebone slides are acceptable.

Answered 2026-10-17 (second round):
- Tester drawing and system P&ID go in the **Project Defaults**; the
  tester does not change from one run to another.
- New/Reused: a small summary table with New = Y or N per part, taken
  from the existing *Face condition* of the Result components (already
  frozen with the record); no new field. Damage during the test shows in
  the acceptance and the pictures, not here.
- Measurements (taper, Ra, waviness): **on hold** until the engineer has
  compared the values; the measurements slide waits for that, the other
  slides do not.

### Tester drawing and system P&ID (needed by the slides)

Built 2026-10-17 (`projects/drawings.py`, migration projects 0008), under
review: on the Project defaults page, a drawing no., revision and file for
each; tester drawing PDF, TIFF, JPEG or PNG, P&ID PDF only; stored
unchanged with its SHA-256, pictures with a display copy. A new upload
becomes the current drawing, the earlier ones are kept as its history
(as the first requirements asked: "current P&ID + possible revisions");
only the current one can be deleted, for a wrong upload.

Used by the deck's front page (tester drawing) and, since 2026-10-23, the
Final Report appendices (A tester drawing, B system P&ID; every page of a
PDF, landscape pages turned). The issued PDF keeps the drawing it was made
with; a drawing in an issued Final Report can no longer be deleted.

## Left alone

Projects, Test Program and TCN screens and documents; DCS import, signal
mapping and running intervals; Excel log import; seal-face measurement
analysis; report profiles, frozen records, issued documents and revisions.

## Confirmed 2026-10-13

Choices made during implementation, answered by the engineer.

M2F-B2 (Result revisions):
1. A revision in progress cannot be discarded; finalizing it unchanged
   gives a revision with the same content. Agreed.
2. While revising, acceptance requirements are brought in step with the
   Test Program and TCN again, as in any Draft. Agreed.
3. "Cannot be deleted" goes by when an item was added, so it also covers
   unreviewed measurements that existed at finalization. Agreed.
4. The revision reason appears in the Engineering document history only,
   not in the Customer report. Agreed.

M2E (final report):
5. A manual-log interval counts as running only when the entries at both
   ends agree. Agreed.
6. Pre and Post inspection times default to the whole run. Agreed.
7. The TCN does not have to be issued to finalize a Result; it is shown as
   a notice only. Agreed for now.

Running intervals:
8. Samples inside a bridged dip count as running time but are left out of
   the min–max, because they are below the threshold. Agreed.
9. A manual/DCS difference close to a DCS start or stop is not listed as a
   discrepancy: changed from 5 to 10 minutes on 2026-10-13.
10. Each confirm, override or re-detect adds a record that is never
    changed. Agreed.

Other:
11. Excel log import: tested with the real workbook; the rows above the log
    table got in the way. Since 2026-10-13 only the log table is read: the
    comparison of the top rows with the run and its acknowledgement are
    gone.
12. DOCX was a mistake: documents are HTML rendered to PDF by Chromium.
    Removed from `CLAUDE.md`, `README.md`, the code and the tests on
    2026-10-13; `docs/requirements/` (the first design, built on Word
    templates) was deleted the same day.

## Open points

1. Code comments still cite section numbers of the M1 specification
   ("M1 spec §9.4", "AT-16"); the specification was deleted with
   `docs/requirements/` on 2026-10-13 and is in the git history only.

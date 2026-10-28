# TestLab

Internal Test Lifecycle Management application for the wet-seal test
laboratory. Milestone M1 replaces the Excel/VBA TCN generator.

## Status

What comes next and what is still open: see [ROADMAP.md](ROADMAP.md).

M1 was implemented commit by commit following the M1 specification (the
requirement documents of September 2026, removed on 2026-10-13 and kept in
the git history; section numbers in code comments refer to it).

| #      | Commit                                                                                                                                                                                                                                        | State                                 |
|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------|
| 1      | Project skeleton (settings, login, base layout, pytest)                                                                                                                                                                                       | done                                  |
| 2      | Project model & screens (list/search, create, edit)                                                                                                                                                                                           | done                                  |
| 3      | Run setup model & arrangement rules                                                                                                                                                                                                           | done                                  |
| 4      | Project defaults form (shared run setup form, arrangement switching)                                                                                                                                                                          | done                                  |
| 5      | TestRun / TCN revision models (constraints, immutability guard; no screens yet)                                                                                                                                                               | done                                  |
| 6      | Create & copy TCN (project page / register, new TCN, draft edit, optimistic locking)                                                                                                                                                          | done                                  |
| 7      | Differences vs source (“changed — was: …” markers, difference list)                                                                                                                                                                           | done                                  |
| 8      | TCN document template & preview (PDF since 10a)                                                                                                                                                                                               | done                                  |
| 9      | Issue & lock (issue confirmation, stored document (PDF since 10a) + hashes + snapshot, read-only view, download, project-number lock)                                                                                                         | done                                  |
| 10     | Revisions, cancel, delete draft (new revision from S9, cancel with reason S10, delete draft with confirmation)                                                                                                                                | done                                  |
| 10a    | Stabilization (spec §17): controlled TCN document is a PDF; registration with corporate email domain and administrator approval                                                                                                               | done                                  |
| 11–13  | Test Data Sheet, PostgreSQL, acceptance                                                                                                                                                                                                       | not started                           |
| M2A    | Test execution evidence: manual log and LabVIEW/DCS datasets per Test Run (under review)                                                                                                                                                      | in progress                           |
| M2B    | Test Result: inspection measurements and pictures per seal component, acceptance, conclusion (under review)                                                                                                                                   | in progress                           |
| M2C    | Test Run Report: Draft preview PDF generated from the TCN, manual log, DCS plots and Result (Chromium)                                                                                                                                        | in progress                           |
| M2D    | Seal-face measurements: Mahr / ROUNDPAK instrument files and dimensional readings in the Result                                                                                                                                               | done (validated on Linux and Windows) |
| M2E    | Chromium-only PDFs; optional components; running hours, dynamic leakage, wear; Test Program requirements and TCN overrides; DCS signal mapping; DCS running intervals and simplified Test Conditions & Results; Draft → Final Test Run Report | under review                          |
| M2F    | Manual Log Import: an Excel (.xlsx) manual log into a run's log — header mapping with engineer review, preview (new / possible duplicate / conflict / error), atomic import, audited undo                                                     | under review                          |
| M2F-a  | Seal Face Flatness v5: engines mahr-2 / roundpak-2 (skip first points, Flat/Convex/Concave at ± 0.25 HLB, ROUNDPAK radial taper and UPR harmonics, light-band gauge, lenient Mahr reading, drag and drop)                                     | under review                          |
| M2F-B1 | Report profiles (Customer / Engineering), per-profile content selection, frozen record (revision 0) and controlled report documents (Issued / Superseded / Withdrawn)                                                                         | under review                          |
| M2F-B2 | Result revisions: a final Result is corrected by opening revision N+1 with a reason; execution evidence can be corrected; documents of earlier revisions become Superseded                                                                    | under review                          |
| FR-0   | Final Report: texts, included runs, generated summary table and one section per run from the frozen records; draft PDF; issued as R0, R1, … stored once                                                                                       | under review                          |

What works today:

- **Projects** — list with search, create, edit. Project numbers are stored
  upper-case and must be unique; the usual `YYYYRSDnnTL` format is a
  convention (warning only).
- **Project defaults** — the run setup new TCNs will start from:
  - seal arrangement under test: Single, Dual Pressurized or Dual Unpressurized;
  - **Plug Seal** (test equipment that establishes the process conditions, not part of the arrangement) with its own seal data — required for a single seal, optional for dual;
  - conditions per circuit: common (test unit, duration, speed) · process (the Plug Seal operating conditions; only with a Plug Seal) · seal support (Barrier / Buffer / Flush — a single seal may have a flush circuit);
  - parameters to log (process pressure, temperature and flow are logged with the Plug Seal; Plug Seal face temperature and leakage);
  - switching arrangement or Plug Seal asks for confirmation before any non-empty data is discarded; seal-support values are never cleared.
- **Drawings** — on the Project defaults page: the **tester drawing** (PDF,
  TIFF, JPEG or PNG) and the **system P&ID** (PDF), each with drawing no.
  and revision. For the whole project, never copied into a TCN. The file is
  stored unchanged with its SHA-256 (the format is read from the content);
  a picture also gets a display copy, since browsers do not show TIFF. A new
  upload becomes the current drawing and the earlier ones are kept as its
  history; only the current one can be deleted (a wrong upload). The
  project page links the current drawings.
- **Project page and TCN register** — one row per Test Run (its current
  revision), with Open and Copy to new TCN.
- **New TCN** — from the project defaults or as a copy of a TCN of the same
  project; the run number (= TCN number) is proposed and may be changed.
  The TCN gets its own copy of the run setup: later changes to the project
  defaults or to the source TCN never affect it.
- **Draft TCN editing** — project description, prepared by, and the shared
  run setup form. If someone else saved the draft in the meantime, the save
  is refused with who/when (reload to continue).
- **Differences vs source** — on a copied TCN every field that differs from
  the source TCN is marked "changed — was: …", with a list of all
  differences at the top (saved values; TCNs made from the project defaults
  have no comparison).
- **Preview PDF** — a draft TCN opens as a PDF in the layout of the Excel
  TCN, marked "DRAFT — not issued". It shows the saved draft and is not stored.
- **Issue** — "Issue…" on a draft checks that the TCN is complete, shows the
  differences from its source, and on confirmation stores the **PDF** (without
  the draft marking), its SHA-256, the document template's SHA-256 and the
  printed data. The user who confirms is recorded as issued by. The issued
  TCN is read-only for good; downloads always serve the stored PDF. If the
  PDF cannot be generated nothing is issued. Once a TCN of a project is
  issued, the project number can no longer be changed.
- **Controlled document** — TCNs are only ever shown and downloaded as PDF.
  Layout comes from the HTML document template `app/doc_templates/tcn/`
  (HTML + print CSS, Chromium header/footer templates, bundled Carlito
  font), printed to PDF by headless Chromium. It is separate from the TCN
  pages of the application.
- **New revision** — "New revision" on the current issued TCN creates the
  next revision as a draft (a copy of it; prepared by the current user).
  Only one draft at a time, and none for a cancelled run. It needs a reason
  to be issued; issuing it supersedes the previous revision, whose record
  and document stay unchanged and downloadable.
- **Cancel TCN** — the current issued revision can be cancelled with a
  reason (not while a draft revision of the run is pending — delete that
  first). It is kept with its document, cannot be revised any more, and
  can still be copied to a new TCN.
- **Delete draft** — after confirmation a draft is removed without trace;
  deleting a draft rev 0 also removes the run (the number becomes free). A
  draft another TCN was copied from cannot be deleted.
- **Accounts** — "Log in | Register". Registration asks first name, last
  name, corporate email address and password; only the domains in
  `TESTLAB_ALLOWED_EMAIL_DOMAINS` may register. New accounts wait for an
  administrator to approve them (below). Users log in with their email address.
  "Change password" in the top bar asks the current password and the new one
  twice; the user stays logged in. A forgotten password is reset by an
  administrator (Admin → Users → the user → Password).
- **Manual log (M2A)** — "Log" on a register row opens the run's test
  execution pages. A reading has a date/time, the measurements of the run's
  configuration (speed, torque, Process with the Plug Seal, Barrier/Buffer/
  Flush, face temperature and collected leakage per seal; for dual seals the
  diff. seal pressure — pumping device head, bar — only when the TCN ticks it
  in "Parameters to log") and a note; the
  person is the logged-in user (shown by initials). Elapsed hours (clock
  time since t = 0, the start of the run's LabVIEW recording; provisional
  from the first reading until that is imported) and leakage rates
  (collected volume as logged / clock time since the previous reading, or
  an entered collection period) are calculated, never typed.
- **Manual Log Import (M2F)** — *Import Excel…* on the Manual log: Upload →
  Mapping → Preview → Confirm. Only `.xlsx`: open a legacy `.xls` in Excel
  and save it as `.xlsx` first.
  - The workbook is stored byte for byte with its SHA-256; the import record keeps the column headings, every data row's cells (values, numbers as stored in the file, formulas) and what TestLab made of them.
  - **Mapping**: columns are matched to the run's manual-log fields by their headings (two-row/grouped headings too), units and the run's configuration, never by column letters. What a heading does not say for sure is marked **Review required** and must be confirmed — e.g. whether a "Job" or "Plug" flow/temperature circuit is the Flush/Barrier/Buffer or the Process circuit, or what "Seal Press" is. A confirmed mapping is saved per layout (fingerprint of the headings in column order) and configuration and proposed again for the same layout, still reviewable; each import keeps its own copy. Running hours and leakage rates of the workbook are never imported: TestLab calculates its own.
  - **Values** are imported exactly as recorded (to Excel's 15 significant digits): 0 is a reading, an empty cell is not measured, nothing is corrected. A mapped cell that cannot be read (text, an Excel error, more decimals than stored) makes the whole row an **Error**: skip it, or correct the workbook and import again. "Over [hours]": a formula is the workbook's default (not imported), a typed period is imported; when the leakages of one row have different typed periods the engineer chooses.
  - **Preview**: each row is **New** (imported), **Possible duplicate** (same minute and same values as an existing reading; skipped) or **Conflict** (another reading in the same minute; the engineer decides: skip, or import as an additional entry). Existing readings are never changed. Only the log table is imported: what is written above its headings (project, run, test unit, speed, seal type, torques…) is not read.
  - **Events** are suggestions only: a category from the note's words ("Start RPM" → Start; "Start/Stop recording" is not a shaft start/stop), a separate event at a time named in the note ("9:36 Changed …"), or a separate Stop after the reading for "after … stop" (time entered by the engineer).
  - **Confirm** creates all entries in one transaction (or none; if the log changed since the preview the rows are classified again). Each entry links to its workbook row; "By" in the log then shows the operator as written in the workbook, the importer is the TestLab user.
  - **Undo** (reason required; permission `can_undo_log_import`, the rule of finalizing a Result) removes all entries of an import; the record and the file stay. A final Result closes imports like all other run evidence.
- **DCS data (M2A)** — a LabVIEW/DCS text export (any file name, no
  extension needed) is stored unchanged with its SHA-256; signals, units,
  time range and recording start are read from its content. The plot
  (Plotly, served locally) shows selectable signals on left/right axes
  with zoom, range slider and unified hover.
- **Events (M2B)** — a manual log entry may carry an event type (start /
  restart, stop, adjustment, leakage collection, abnormal observation,
  inspection, other) besides its note: the log is the run's event timeline.
- **Test Program (M2E)** — "Test Program" on the project page: document no.,
  revision, title and objective (quoted in the Test Run Report) and the
  baseline **acceptance requirements** (numbered P1, P2, …): criterion,
  what it is compared with (average dynamic leakage, face wear rate,
  shaft-running hours, elapsed clock time, or assessed by the engineer),
  the seal/face it applies to (blank: every seal of the arrangement under
  test) and a limit with unit or a wording. Operating set points on the TCN
  are instructions, never acceptance limits. Program sections, planned
  tests and program revisions are M3. Edits apply to TCNs issued from then
  on: the page names the issued TCNs that still hold the earlier
  requirements (a new TCN revision brings that run in step).
- **TCN acceptance requirement changes (M2E)** — on a draft TCN: replace a
  Program requirement for this run, make it not applicable, or add one,
  each with a reason. They are printed on the TCN, frozen at issue, carried
  into a new revision, and in force for the run only while that revision is
  the run's issued one; a draft's changes are shown as pending. Issuing a
  TCN revision also freezes the full list in force with it (Test Program
  with its changes applied; not printed): requirements flow one way,
  Program → TCN → Result.
- **DCS signal mapping (M2E)** — on a DCS dataset: which TestLab/TCN
  parameter each signal is. Nothing is converted; when the units differ the
  engineer confirms that the values are in the parameter's unit. A reviewed
  mapping is remembered per export format (signal names and units) and
  applied to later imports of exactly that format; any difference needs a
  new review. Only reviewed mappings are used: the speed signal for the
  running intervals, the others for the actual min–max.
- **DCS running intervals** — on a DCS dataset with a reviewed speed signal:
  the **recording** (the whole file, kept unchanged, t = 0 and time axis as
  exported) and the **running intervals** (shaft genuinely rotating) are
  separate. Detection runs when the speed mapping is reviewed (or reused on
  import): speed ≥ 50 rpm (a fixed threshold, not a share of the instructed
  speed); dips below it of at most 60 s between running stretches are
  bridged (running time; their samples are not running readings); runs
  shorter than 2 min are ignored as spikes; a start or stop lies half-way
  between two samples, so start-up and rundown samples above the threshold
  are inside; a genuine stop gives separate intervals; a sampling gap is
  missing data — unknown, never stopped; an interval at the first or last
  sample is flagged (start/stop not recorded). Every setting is stored with
  the determination (with the speed signal, method and version, detected
  and effective intervals, status, who and when) and editable per dataset:
  **Confirm**, **Edit running intervals** (reason required; the detected
  intervals stay beside the entered ones) and **Redetect** with other
  settings. Each action adds a record; earlier ones stay in the history. A
  confirmation goes stale when the mapping is reviewed again. Until
  confirmed or overridden the intervals are used provisionally (marked on
  the Result and in the draft report) and the Result cannot be finalized.
  The plot shades the intervals and offers *Full recording | Running
  intervals* as a zoom only; saved Result plots keep their own time window.
- **Result (M2B)** — the run's Result tab (also *Result* beside *Log* on the
  project page): one page with Summary, Plots, Measurements, Pictures,
  Acceptance, Conclusion and Report. The Summary is derived, never typed
  (M2E rules):
  - **elapsed clock time** (t = 0 to the last evidence) and **shaft-running hours** separately. Running hours come only from evidence: the running intervals of a DCS recording (see *DCS running intervals*; inside the recording's coverage, outside an interval the shaft is stopped, a data gap is unknown), else the manual log between two entries whose ends agree (speed reading ≥ `TESTLAB_RUNNING_SPEED_RPM` or Start/Stop event). Anything else is *unknown*, shown, and never counted as running. Manual entries that contradict the DCS (a Stop event or a reading below the threshold inside a running interval, a running reading where the DCS shows the shaft stopped; more than 10 min from a DCS start/stop) are listed for review, never resolved automatically;
  - **leakage per seal**: each collection (since the previous reading, or the entered period) is dynamic (running throughout), static (stopped throughout), mixed (both: its own mixed-period collection rate, never called dynamic) or not used (running state unknown, first reading without period, blank = not measured; 0 is a measured zero). **Average dynamic leakage = Σ dynamic volumes / Σ their running hours** — the same intervals above and below the line, never a mean of interval rates and never partial collections over the whole runtime. Running hours not covered by a dynamic collection are shown;
  - **face wear**: Δ = Post − Pre (signed, as measured) and wear = Pre − Post face length (material removed); wear rate = wear / shaft-running hours between the Pre and Post inspections, unavailable while part of that period is unknown. Taper, roughness and flatness changes are not wear;
  - **actual min–max** of each logged parameter over the shaft-running readings (transients included, not a steady-state range), with its source (reviewed DCS signal, else the manual log); stopped readings are a separate static summary. Every DCS statistic states its scope: `running_intervals` (samples inside the effective intervals — the operating values) or `full_recording` (shown for reference); a steady-state scope is not implemented. In the Test Run Report the table is *Parameter | Unit | TCN (Instructed) | Actual | Source*, the actual at report precision (speed whole rpm; pressure, temperature, flow, torque, leakage rate 1 decimal; hours 2), with a compact source such as “DCS · X22 Process Syst Press”; acceptance is assessed in the Leakage, Face wear and Acceptance sections, not in this table.

  Measurements are Pre and Post per component (IB/OB/Test/Plug stator and
  rotor; sleeves and backing rings as optional components) and quantity. Plots come from the DCS viewer (*Add plot to Result*): the
  Result keeps the plot definition — source dataset, signals and their axis,
  time window, axis ranges, caption — and a report image drawn from it;
  *Open in DCS viewer* redraws it from the dataset to adjust and update it.
  Pictures keep the original file unchanged, show pre-test and
  post-test of a component side by side, and have a large compare view.
  Acceptance items come from the requirements frozen with the run's
  issued TCN (with their source) or are entered on the Result. The Result
  takes them when it is created, when a new TCN revision is issued while
  it is a Draft, and when a revision is opened, never on opening a page;
  before the TCN is issued the list is provisional (Test Program with the
  draft's changes, marked "TCN not issued yet"). Each item holds
  requirement, actual, assessment (Pass, Fail, Not
  evaluated, Not applicable) and remark. Pass/Fail is calculated only when
  limit, unit (equivalent units such as cc/h or mm/h are converted; g/h is
  not) and evidence give an unambiguous comparison; otherwise the item is
  *not assessed* — missing, which is not the same as an explicit Not
  evaluated. Observations, conclusion and recommendation are written by the
  engineer.
- **Optional components (M2E)** — sleeves (per seal, or the shared cartridge
  sleeve) and backing rings (per seal) are added only when inspected; not
  added means neither missing nor reported. Their dimensional readings
  name the measured feature (e.g. "Thickness", in or mm) and are optional;
  Mahr/ROUNDPAK are not applicable unless the engineer sets them. The
  applicability of every kind on every component is Required (missing until
  measured), Optional (reported when measured, never missing) or Not
  applicable. Each component has inspection observations; pictures attach
  as for the faces. The report lists only what was measured.
- **Seal Face Flatness v5 (M2F-a)** — the calculations follow
  `reference/measurement-tools/Seal_Face_Flatness_Claude_v5.html` (engines
  `mahr-2`, `roundpak-2`; parity in `golden_v5.json`, regenerated with
  `make_golden_v5.py`). Mahr: the first 5 points of each scan are skipped,
  default view common tilt removed, taper classified Flat / Convex (ID
  high, negative) / Concave (OD high, positive) at ± 0.25 HLB; a point
  count differing from CNT is read with a warning. ROUNDPAK: radial taper
  = mean slope of the two radial traces × (OD − ID) / 2, same sign and
  limit, with the setup tilt and the waviness harmonics (1–30 UPR). The
  summary shows both tapers (Mahr first, the more accurate). Stored
  `mahr-1` / `roundpak-1` results keep their values and class names.
  *Add measurement* accepts drag and drop: both Mahr TXT files at once
  (placed left/right by the L or R at the end of the name, as in v5) or
  one per field, and the ROUNDPAK CSV (`static/testlab/file_drop.js`).
- **Report profiles and controlled documents (M2F-B1)** — the report is
  presented per profile: *Customer* (clean: explanations and provenance
  off) or *Engineering* (complete technical detail), from the same record
  and the same numbers (`documents/report_profiles.present`, which only
  selects). *Report contents* (Result → Test Run Report → Contents) chooses
  per profile the notes and optional parts, per measurement slot (component
  × type, Pre and Post together) the summary, detail, figures and remark,
  and each picture and plot; only differences from the profile defaults are
  stored (`ReportSelection`), and choosing never touches evidence.
  Identification, conditions, the leakage table, every acceptance row, the
  narrative and the mandatory qualifications (leakage collections covering
  less than `TESTLAB_LEAKAGE_COVERAGE_WARNING_PCT` of shaft running, unknown
  shaft state, provisional data) cannot be hidden. Finalizing freezes the
  record as revision 0 (`ResultRevision`: record JSON and a copy of every
  picture and plot, each with its SHA-256) and issues the Engineering
  document `<project>-Run<nnn>-TRR-R0-E`; other profiles are issued later
  from the frozen record (`…-R0-C`), never from live data. Issuing again
  needs a reason and supersedes the earlier issue; withdrawing needs a
  reason. Issued PDFs are never overwritten and are served byte for byte
  against their SHA-256. Migration 0008 turns an M2E final into revision 0
  + R0-E with the same PDF file and SHA-256.
- **Final Report (FR-0)** — project page → *Final Report*:
  the customer report over the runs of a project, numbered `<project>R`.
  The engineer ticks the Results to include (those with a final revision),
  orders them and writes the introduction, executive summary, field
  conditions, test conditions and conclusion. The summary table (one row
  per run) and each run's section come from the frozen record of the run's
  latest final revision, presented with its Customer report contents — the
  same data as its Customer Test Run Report, never live data; its
  identification names that source instead of a report status. *Draft PDF*
  renders it on request and stores nothing. *Issue* (permission
  `can_issue_report`) stores the PDF once as `<project>R-R<n>` under
  `projects/<project>/final_report/`, with its SHA-256, the texts and the
  runs with their Result revisions; from R1 a reason is required and the
  earlier issue becomes Superseded. Issuing is refused while an included
  run has a Result revision in progress. The texts and runs stay editable
  after issuing; the page lists what changed since the issued document.
  The PDF carries a document history (document, revision, date, status);
  reasons and names stay on the page, as for Customer Test Run Reports.
  Appendices after the conclusion: the current tester drawing and system
  P&ID of the Project defaults, one report page per drawing page
  (landscape pages turned to fill the portrait page). A drawing in an
  issued Final Report can no longer be deleted.
- **Slides (.pptx)** — Final Report page → *Slides (.pptx)*: the included
  runs as PowerPoint slides, an editable copy made on request and never
  stored. Title (with the current tester drawing), test matrix, parts
  (Face condition and New = Y/N: Y for "New", N for any other condition),
  then per run a divider, test conditions with the conclusion and the
  seal cross section, one slide per DCS plot, leakage and acceptance with
  the cumulative leakage chart, the seal-face measurements (waviness from
  ROUNDPAK, taper and Ra from Mahr, Pre and Post, from the run's summary),
  and every picture of the Customer Test Run Report, four per slide. Same
  data and selection as the Final Report. The company template
  is a `.pptx` kept out of git: `data/slides-template.pptx` (or the file
  `TESTLAB_SLIDES_TEMPLATE` names); its slides are removed and its layouts used (the first for the title, a
  "Section…" layout for the runs, a title-only layout for the content).
  Without it the slides are plain widescreen slides.
- **Result revisions (M2F-B2)** — a final Result is never edited in place.
  *Open revision N+1* (Result → Test Run Report, reason required,
  permission `can_revise_result`) makes the Result a Draft again:
  measurements, pictures, plots, assessments, conclusion and report details
  can be corrected, and *Finalize* freezes the complete record again as
  revision N+1 (`records/r<N+1>/`) and issues `…-R<N+1>-E`. The manual log,
  DCS data, running intervals and log imports can be corrected too while a
  revision is in progress; earlier revisions keep their own frozen copy. What was on record in a final revision is never
  deleted: a measurement is superseded by a new reviewed one (it cannot be
  deleted or reopened), a picture or plot is left out under *Report
  contents*. While a revision is in progress the documents of the earlier
  revision stay Issued and no new document is issued; finalizing supersedes
  them (a withdrawn one stays withdrawn) and the first document of each
  profile for the new revision carries the revision's reason in the
  Engineering document history. Earlier records and PDFs never change.
  Migration 0009 drops the M2E `final_*` fields of the Result after
  checking that revision 0 and a stored document hold them.
- **Test Run Report: Draft → Final (M2E)** — *Preview Report* renders the
  Draft (marked DRAFT, never stored). *Finalize Report* lists what blocks
  it, each linked to its section: required measurements not reported,
  acceptance items not assessed, no conclusion, no responsible engineer.
  Optional measurements may stay unavailable; a failed test can be final —
  Final means complete and controlled, not passed. Finalizing freezes the
  report context (requirements with their sources, evidence, assessments,
  the SHA-256 of each picture and plot) as the snapshot, renders the PDF
  once without the DRAFT marking, stores it with its SHA-256 and the
  template's, and records finalized by/at. If rendering fails nothing
  changes. *Download Final Report* always serves the stored PDF. Afterwards
  the Result, the run's manual log and DCS data cannot be changed (pages
  and models refuse it). Who may finalize: `tcn/permissions.py`
  (`can_finalize_result`, M1 rule: every active user). The report quotes
  title and objective from the Test Program (plus a run-specific addition),
  "Prepared by" from the Result's responsible engineer, and places events
  and observations in the body, the manual log in Annex A, and a cumulative
  collected-volume chart (markers at the collection times only).
- **Lab plan** — *Lab plan* in the top bar: which test unit (A–D, managed
  in the admin) is booked by which project in which ISO weeks, one row per
  booking under its unit, as on the lab's yearly slide. A booking holds
  project no., description and PE(s) as free text (the project need not
  exist in TestLab yet; the plan links to it once it does), unit, first and
  last week, and status (Tentative, Confirmed, Done, Cancelled). Units can
  also be blocked (maintenance, rebuild, calibration, reserved). A unit
  holds one booking or block at a time (refused, naming the other one;
  Cancelled bookings excepted); one booking per project. Rough planning:
  never tied to a TCN, printed in a document or frozen. From 2026 on.

Out of M1: legacy Excel import, Test Programs, Results, Summaries, Final
Reports, DCS/LabVIEW data.

## Development setup

For a developer or test machine. Production deployment is separate (see
*Configuration* below); these scripts are development conveniences only.

**Requirements:** Python 3.12 or newer (on Windows from python.org; "Install
for me only" needs no administrator rights) and internet access for the
Python packages and the Chromium download. Nothing else: no Office,
LibreOffice, Node or Docker.

### First run

Linux:

```bash
./scripts/setup.sh
.venv/bin/python app/manage.py createsuperuser     # first administrator, once
./scripts/run_app.sh
```

Windows (Command Prompt, or double-click the `.bat` files):

```bat
scripts\setup.bat
.venv\Scripts\python app\manage.py createsuperuser
scripts\run_app.bat
```

Open http://127.0.0.1:8000/ and log in with the administrator account.

`setup` is safe to run again at any time (after a `git pull`, for example).
It:

1. checks the Python version (3.12+; on Windows it prefers `py -3.12`; set `TESTLAB_PYTHON` to the interpreter to use another one);
2. creates `.venv` in the repository if it does not exist (an existing one is kept; if it was made with another Python version, setup stops and says so);
3. installs or updates TestLab's dependencies from `pyproject.toml` (`pip install -e ".[dev]"`, including the test tools);
4. installs Playwright's Chromium, the browser that prints every PDF (TCN, Test Run Report; about 150 MB, once per user profile, no administrator rights). If the download is blocked, setup finishes with a warning: everything except the PDFs works, and running setup again retries;
5. creates the local data directories (`data/`, `data/files/`, or the locations configured by `TESTLAB_DATA_DIR` / `TESTLAB_DATABASE_URL` / `TESTLAB_FILE_ROOT`);
6. applies the database migrations (the database is never deleted or reset);
7. runs Django's system checks.

It does not create users, install system packages, or change certificates
or proxy settings. No Office, LibreOffice, Pango/MSYS2 or Word is needed:
Chromium is the only PDF renderer.

The shared logic is in `scripts/devenv.py` (Python standard library only);
`setup.sh`/`setup.bat` and `run_app.sh`/`run_app.bat` only locate Python
and call it, so both platforms follow the same procedure.

### Starting TestLab

```bash
./scripts/run_app.sh                  # http://127.0.0.1:8000/, this machine only
./scripts/run_app.sh --port 8001      # another port (or TESTLAB_PORT)
./scripts/run_app.sh --lan            # also reachable from other machines
```

(`scripts\run_app.bat` takes the same options.) The scripts work from any
directory. They use `.venv` (and stop with a pointer to setup if it is
missing or incomplete), apply pending migrations, and start the Django
development server; stop it with Ctrl+C.

- By default the server listens on 127.0.0.1 only. `--lan` listens on all interfaces and prints the addresses colleagues can use; it adds this machine's names to `TESTLAB_ALLOWED_HOSTS` unless that variable is set. Anyone on the network then reaches the login page — use it for testing only.
- All `TESTLAB_*` environment variables are passed through unchanged.

### Updating a machine where TestLab runs as an NSSM service

From an administrator Command Prompt:

```bat
scripts\update.bat MyServiceName
```

Without a name it uses `TESTLAB_SERVICE`, or else `TestLab`.

It checks first (administrator, `nssm` on the PATH, the service exists, no
files edited on that machine) and changes nothing if a check fails. Then it
stops the service, copies the SQLite database to
`data\backups\testlab-<date>-<time>.sqlite3`, runs `git pull --ff-only`,
runs setup (packages, Chromium, migrations) and starts the service again. If
the pull fails, the service is started again on the old version; if setup
fails, it stays stopped and the script prints how to go back (`git reset
--hard <previous commit>`, copy the backup back). Updating by hand with `git
pull` also needs a service restart: the development server reloads changed
code by itself but does not apply migrations. Variables set only in the
NSSM service (Environment tab) are not seen by the script, so it would back
up the default database path.

### Troubleshooting

- **Chromium download fails** (setup warning, or `testlab.W003` from `manage.py check`): behind HTTPS inspection the download needs the company root certificate. Ask IT for it as a PEM file and run `set NODE_EXTRA_CA_CERTS=C:\path\company-root.pem` (Linux: `export …`) before setup.
- **Linux: Chromium is installed but cannot start** (missing system libraries): `sudo .venv/bin/python -m playwright install-deps chromium` on Debian/Ubuntu. This needs administrator rights, so setup only reports it.
- **".venv was made with Python X"**: delete the `.venv` folder and run setup again with the Python you want.
- **Port already in use**: TestLab (or something else) is already running; open it, or start with `--port`.

### Users: registration and approval

1. Set the corporate domain(s), e.g. `TESTLAB_ALLOWED_EMAIL_DOMAINS=example.com` (comma-separated for several). Without it registration is closed.
2. A user chooses **Register** on the login page. The account is created inactive: "awaiting administrator approval".
3. An administrator opens Django admin → **Users**, filter **Approval: Awaiting approval**, selects the account(s) and runs **Approve selected accounts (activate)** — or ticks **Active** on the user page. Deactivating an account (untick Active) blocks its login again.
4. The user logs in with the email address.

Every active user may issue and cancel TCNs in M1; the check lives in
`app/testlab/tcn/permissions.py` so it can be restricted later. Corporate
SSO (Entra ID/LDAP) is future work and would be added as another Django
authentication backend without changing the TCN data.

By default the development database is a SQLite file at `data/testlab.sqlite3`
(git-ignored).

## PDF documents (Chromium)

Every PDF — TCN preview and issue, Test Run Report preview and
finalization — is printed by headless **Chromium** through Playwright from
an HTML template bundle in `app/doc_templates/` (M1 spec §19; validated on
Linux and on the corporate Windows workstation). The browser is the build
Playwright downloads into the user profile, not an installed desktop
browser; setup installs it (Linux and Windows alike):

```bash
.venv/bin/python -m playwright install chromium        # Windows: .venv\Scripts\python -m playwright install chromium
.venv/bin/python app/manage.py check                   # warns with testlab.W003 when Chromium is missing
```

On Linux, if Chromium cannot start for missing libraries:
`.venv/bin/python -m playwright install-deps chromium` (Debian/Ubuntu).
Each PDF is printed by a separate worker process ended after
`TESTLAB_PDF_TIMEOUT` seconds. Documents issued or finalized earlier are
stored PDFs with their SHA-256 and the template hash of the day: they are
never rendered again (TCNs issued before the Chromium-only change keep the
hash of the template they were printed with).

Tests print real PDFs only when marked `chromium` (skipped when Chromium is
missing); the others use a fast stand-in that keeps the rendered text.

**Check on a workstation:** `.venv\Scripts\pytest -m chromium` passes;
with `set TESTLAB_PROOF_DIR=%CD%\proof` it also writes sample TCN and Test
Run Report PDFs (draft and final look) and `timings.json` to `proof\`.

## Using it

1. Log in and choose **New project**: project no., description (printed on the TCN as "Project Description"), customer.
2. Saving continues to **Project defaults**. Choose the arrangement and whether a Plug Seal is installed — always for Single (the form redraws; the **Update** button does the same without JavaScript), fill in seals, conditions and parameters to log, then **Save**.
3. Typed units that TestLab prints anyway are removed (`70 barg` → `70`); a message shows each change.
4. On the project page choose **New TCN**: TCN001 starts from the project defaults; later TCNs are proposed as a copy of the latest TCN (or use **Copy to new TCN** on any register row). Edit what differs and **Save**.

## Code layout

```
app/
├── manage.py
├── conftest.py                 shared pytest fixtures
├── config/                     settings (env-driven), urls, wsgi
├── doc_templates/              document templates (Chromium HTML bundles): tcn/, run_report/, final_report/ + reference (README.md)
├── tests/                      cross-cutting tests (login required); fixtures/ (AT-2 expected text)
└── testlab/
    ├── templates/              base layout, login
    ├── static/testlab/         CSS, dcs_plot.js; static/vendor/plotly/ (local Plotly, no CDN)
    ├── locks.py                every edit lock in one place: what locks what, page messages and backstops
    ├── accounts/               registration, email login backend, user admin with approval
    ├── projects/
    │   ├── models.py           Project, RunSetup, SealPosition; TestProgram, ProgramRequirement (M2E); ProjectDrawing
    │   ├── drawings.py         tester drawing and system P&ID: stored unchanged, display copy, current + history
    │   ├── arrangements.py     arrangement / Plug Seal rules, labels, monitored parameters
    │   ├── conditions.py       condition field groups per circuit, unit clean-up
    │   ├── forms.py            project form
    │   ├── setup_forms.py      shared run setup form + RunSetupEditor (switching, discard)
    │   ├── templatetags/       `was` filter: change markers in the shared form
    │   ├── views.py, urls.py, templates/projects/
    │   ├── migrations/
    │   └── tests/
    ├── tcn/
    │   ├── models.py           TestRun, TcnRevision, TcnRequirementChange, TcnRequirement; status rules and immutability guards
    │   ├── requirements.py     acceptance requirements in force: Test Program + the TCN's changes, frozen at issue
    │   ├── services.py         create / copy TCN, save draft (optimistic locking), issue, revise, cancel, delete
    │   ├── permissions.py      who may issue / cancel a TCN, finalize a Result, undo a log import (M1: every active user)
    │   ├── diff.py             differences vs the source TCN (markers, change list)
    │   ├── forms.py, views.py, urls.py, templates/tcn/
    │   ├── migrations/
    │   └── tests/
    ├── execution/              M2A test execution evidence of a TestRun
    │   ├── models.py           ManualLogEntry/ManualMeasurement, DcsDataset (raw file + parsed metadata), DcsSignalMap,
    │   │                       ManualLogImport/ManualLogImportRow/ManualLogLayoutMap (M2F)
    │   ├── signal_mapping.py   DCS signal → TestLab/TCN parameter: review, reuse per format (M2E)
    │   ├── measurements.py     what can be read out per configuration (codes shared with "Parameters to log")
    │   ├── manual_log.py       elapsed hours, leakage rates, saving entries
    │   ├── labview.py          LabVIEW/DCS export parser (content-based)
    │   ├── datasets.py         import/storage, column cache, min/max decimation for plots
    │   ├── log_import/         Excel manual-log import (M2F): xlsx → SourceSheet, mapping, interpret,
    │   │                       matching, events, service (upload/stage/commit/undo)
    │   └── views.py, import_views.py, urls.py, templates/execution/, tests/
    ├── results/                M2B Test Result of a TestRun
    │   ├── models.py           TestResult, ResultComponent, InspectionMeasurement, ResultImage, ResultPlot, AcceptanceItem
    │   ├── inspection.py       component identities and measurement quantities; Δ and wear
    │   ├── runtime.py          running timeline: running / stopped / unknown from DCS speed and the manual log (M2E)
    │   ├── evidence.py         dynamic/static/mixed leakage, logged period, wear rate, actual ranges, events
    │   ├── acceptance.py       actuals and Pass/Fail where unambiguous; unit conversion
    │   ├── requirements.py     requirements in force (frozen with the issued TCN) → acceptance items with their source
    │   ├── finalization.py     blocking issues; Draft → Final: snapshot, stored PDF, hashes (M2E)
    │   ├── leakage_chart.py    cumulative collected volume, SVG for the report (M2E)
    │   ├── images.py           picture storage: original unchanged + display copy
    │   ├── plots.py            DCS plots: definition checked against the dataset + report image
    │   └── services.py, forms.py, views.py, urls.py, templates/results/, tests/
    ├── reports/                Final Report of a project
    │   ├── models.py           FinalReport (texts), FinalReportRun (included Results, in order)
    │   ├── assembly.py         summary table and run sections from the frozen records; draft PDF
    │   ├── slides.py           the included runs as PowerPoint slides (python-pptx), never stored
    │   └── forms.py, views.py, urls.py, templates/reports/, tests/
    ├── planning/               Lab plan: test units, bookings and unit blocks by ISO week
    │   ├── weeks.py            ISO week ↔ date, week choices
    │   ├── models.py           TestUnit, Booking, UnitBlock; one booking or block per unit at a time
    │   ├── plan.py             a year's bars per unit
    │   └── forms.py, views.py, urls.py, templates/planning/, tests/
    └── documents/
        ├── tcn_context.py      TCN render context: labels, units, NA rule, column sets
        ├── tcn_context.py / run_report_context.py   render contexts (plain data; the report one is the final snapshot)
        ├── render.py           ChromiumTemplate: HTML template bundle → PDF; tcn_template(), run_report_template()
        ├── chromium.py, chromium_print.py   Chromium worker process (timeout, only bundle files load)
        ├── pdf.py              PdfConversionError, PDF content type
        ├── checks.py           system check: Chromium for Playwright installed (testlab.W003)
        └── tests/
scripts/                        devenv.py (shared setup/start logic) + setup.sh/.bat, run_app.sh/.bat
reference/                      historical source documents (read-only)
```

## Tests

```bash
.venv/bin/pytest            # Windows: .venv\Scripts\pytest
```

## Configuration (environment variables)

| Variable                                | Default                                 | Purpose                                                                                                                                                            |
|-----------------------------------------|-----------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `TESTLAB_DEBUG`                         | `1`                                     | Set `0` in production                                                                                                                                              |
| `TESTLAB_SECRET_KEY`                    | dev key (only when debug)               | Required when debug is off                                                                                                                                         |
| `TESTLAB_ALLOWED_HOSTS`                 | `localhost,127.0.0.1`                   | Comma-separated host names                                                                                                                                         |
| `TESTLAB_DATABASE_URL`                  | `sqlite:///<repo>/data/testlab.sqlite3` | e.g. `postgres://user:pass@host/testlab`                                                                                                                           |
| `TESTLAB_DATA_DIR`                      | `<repo>/data`                           | Location of the default SQLite file                                                                                                                                |
| `TESTLAB_PROJECT_NO_PATTERN`            | `^\d{4}RSD\d{2}TL$`                     | Usual project-number format; a mismatch only gives a warning                                                                                                       |
| `TESTLAB_TIME_ZONE`                     | `Europe/Amsterdam`                      |                                                                                                                                                                    |
| `TESTLAB_FILE_ROOT`                     | `<repo>/data/files`                     | Server-side file store (issued TCN PDFs, final reports, evidence files)                                                                                            |
| `TESTLAB_TEMPLATE_DIR`                  | `<repo>/app/doc_templates`              | Document templates (Chromium HTML bundles)                                                                                                                         |
| `TESTLAB_PDF_TIMEOUT`                   | `120`                                   | Seconds Chromium may take to print one PDF                                                                                                                         |
| `TESTLAB_RUNNING_SPEED_RPM`             | `50`                                    | Speed at or above which the shaft counts as running: manual log readings, and the default threshold of a new DCS running-interval detection (stored per detection) |
| `TESTLAB_RUNNING_MAX_DROPOUT_S`         | `60`                                    | Default for a new detection: longest dip below the threshold bridged between running stretches                                                                     |
| `TESTLAB_RUNNING_MIN_RUN_S`             | `120`                                   | Default for a new detection: shorter runs above the threshold are ignored as spikes                                                                                |
| `TESTLAB_RUNNING_DISCREPANCY_MIN`       | `10`                                    | Manual entries this close to a DCS start/stop are not listed as disagreeing with the DCS                                                                           |
| `TESTLAB_ALLOWED_EMAIL_DOMAINS`         | *(empty: registration closed)*          | Comma-separated email domains that may register, e.g. `example.com`                                                                                                |
| `TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN` | `0`                                     | `1`: manual log readings, DCS imports and Result changes only once the run's TCN is issued (production, see *Temporary policy* below); `0` also allows drafts      |
| `TESTLAB_DCS_MAX_UPLOAD_MB`             | `200`                                   | Largest LabVIEW/DCS export accepted for import                                                                                                                     |
| `TESTLAB_LOG_IMPORT_MAX_MB`             | `20`                                    | Largest Excel manual log accepted for import (M2F)                                                                                                                 |
| `TESTLAB_LEAKAGE_COVERAGE_WARNING_PCT`  | `95`                                    | Below this leakage-collection coverage [%] of shaft running, a report hiding the leakage details states it (M2F-B)                                                 |
| `TESTLAB_RESULT_IMAGE_MAX_MB`           | `40`                                    | Largest Result picture accepted                                                                                                                                    |
| `TESTLAB_STATIC_ROOT`                   | `<repo>/staticfiles`                    | Target of `collectstatic`                                                                                                                                          |
| `TESTLAB_HOST`, `TESTLAB_PORT`          | `127.0.0.1`, `8000`                     | Run scripts only: address and port of the development server (`--lan` = all interfaces)                                                                            |
| `TESTLAB_PYTHON`                        | `py -3.12` / `python3.12` …             | Setup scripts only: Python interpreter that creates `.venv`                                                                                                        |

**Temporary policy — execution data for Draft TCNs.** During the current
development and validation phase, manual log readings, DCS/LabVIEW imports
and the Test Result are intentionally allowed while the run's TCN is still a Draft; this is not
missing validation. TODO for controlled production use: set
`TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN=1` (Issued TCN → execution permitted).
The rule is a lifecycle/authorization check in
`app/testlab/locks.py` (`result_refusal`); the ManualLogEntry,
DcsDataset, TestResult and TestRun models need no change.

Production runs one central Django application with **PostgreSQL**
(`pip install -e ".[postgres]"`), set up separately from the development
scripts above (debug off, own secret key, a real web server). SQLite is for development and tests only
and must not be placed on a network share or synchronised folder.
"""Render context of the Test Run Report (M2C).

The report is a renderer: everything comes from what TestLab already holds,
nothing is entered for it.

    Project / Test Program         title, objective, baseline acceptance requirements
    TCN revision                   identification, instructed conditions, seal information, approved requirement changes
    Manual log (execution)         actual readings, events, leakage collections
    DCS datasets (execution)       t = 0 of the run, end of the logged period, running hours, mapped actuals, plots
    Test Result (results)          components as fitted, Pre/Post, pictures, plots, acceptance, narrative

Calculations are not made here: running hours, leakage, ranges, wear and
acceptance come from results.runtime, results.evidence and
results.acceptance — the same code as the Result page — so the report and
the page cannot disagree. This module only selects and formats.

build_run_report_context() returns plain data (str, int, float, bool,
None, list, dict) and nothing else: the template only renders, and the
context is frozen as the record of a finalized Result (JSON).
Pictures and plots are named in context["assets"] with the SHA-256 of
their stored source file; report_assets() reads their bytes for the
renderer.

Record schema 2 (M2F-B): the context is the complete record, not one
presentation. Every selectable item carries a stable id (picture, plot,
measurement slot "<component id>:<kind>") and every reported Mahr /
ROUNDPAK measurement brings all its figures. A report profile
(report_profiles.present) selects from it; it never calculates.
"""

SCHEMA = 2

from datetime import timedelta
from itertools import zip_longest

from django.db.models import Prefetch
from django.utils import timezone

from testlab.execution import running_intervals, signal_mapping
from testlab.execution.measurements import COMMON_GROUP, LEAKAGE
from testlab.projects import conditions
from testlab.projects.arrangements import Arrangement, seal_support_label
from testlab.results import acceptance, evidence, images, leakage_chart, measurement_summary, plots, requirements
from testlab.results import runtime, seal_faces
from testlab.results.acceptance import format_number
from testlab.results.facemeasure import figures, mahr
from testlab.results.formatting import (
    SIGNED_QUANTITIES, fixed, quantity_delta, quantity_value, report_number, report_range, signed_fixed,
)
from testlab.results.inspection import Applicability, MeasurementKind, Stage
from testlab.results.models import AcceptanceItem, FaceMeasurement, ResultImage, ResultPlot, TestResult

from .tcn_context import format_date, run_description_lines, seal_columns

DASH = "—"
# Value columns of one manual-log table on portrait A4 (besides date/time and elapsed hours).
LOG_COLUMNS_PER_TABLE = 16

Basis = AcceptanceItem.Basis


def build_run_report_context(test_run, generated_at=None, final=None):
    """The report context; `final` = {"by": user, "at": datetime} when it is rendered for finalization."""
    tcn = test_run.current_revision()
    setup = tcn.setup
    project = test_run.project
    result = _result(test_run)
    found = evidence.collect(test_run, result)
    items = list(result.acceptance_items.all()) if result else []
    evaluations = acceptance.evaluate_all(
        items, acceptance.Evidence(found["leakage"], found["wear"], found["period"], found["timeline"])
    )
    events = _events(found["rows"])
    context = {
        "schema": SCHEMA,
        "is_draft": final is None,
        "generated": _datetime(generated_at or timezone.now()),
        "report": _report(test_run, tcn, project, result, final),
        "identification": _identification(test_run, tcn, setup, project, found),
        "running": _running(found),
        "description": {
            "project": tcn.project_description,
            "run_lines": run_description_lines(setup.run_description),
        },
        "configuration": _configuration(setup, found["components"]),
        "conditions": _conditions(setup, found),
        "leakage": _leakage(found["leakage"], evaluations),
        "leakage_chart": leakage_chart.svg(found["leakage"], found["origin"].at or found["period"].start),
        "plots": [],
        "events": events,
        "measurements": _measurements(found["components"]),
        "measurement_details": _measurement_details(found["components"]) if result else [],
        "wear": _wear(found["wear"], evaluations),
        "pictures": {"cross_sections": [], "comparisons": [], "single_stage": [], "other": []},
        "acceptance": [_acceptance_row(e) for e in evaluations],
        "narrative": _narrative(result),
        "log": _log(found, events),
        "has_result": result is not None,
        "assets": [],
    }
    context["component_observations"] = [c for c in context["configuration"]["components"] if c["observations"]]
    if result is not None:
        _pictures(context, result, found["components"])
        _plots(context, result)
    context["sections"] = _section_numbers(context)
    return context


def report_assets(context):
    """{asset path: bytes} of the pictures and plots the context names."""
    wanted = {(a["kind"], a["id"]): a["path"] for a in context["assets"]}
    files = {}
    picture_ids = [pk for kind, pk in wanted if kind == "picture"]
    for picture in ResultImage.objects.filter(pk__in=picture_ids):
        with picture.file.storage.open(images.preview_name(picture), "rb") as stored:
            files[wanted[("picture", picture.pk)]] = stored.read()
    for plot in ResultPlot.objects.filter(pk__in=[pk for kind, pk in wanted if kind == "plot"]):
        with plot.image.open("rb") as stored:
            files[wanted[("plot", plot.pk)]] = stored.read()
    return files


def run_report_file_name(test_run, draft=True):
    """e.g. 2026RSD03TL-Run002-TestRunReport-DRAFT.pdf."""
    suffix = "-DRAFT" if draft else ""
    return f"{test_run.project.project_no}-Run{test_run.display_no}-TestRunReport{suffix}.pdf"


# ---- helpers ------------------------------------------------------------------------------


def _result(test_run):
    return (
        TestResult.objects.filter(test_run=test_run)
        .prefetch_related(
            Prefetch("components__face_measurements", queryset=FaceMeasurement.objects.select_related(
                "mahr", "roundpak", "dimensional").prefetch_related("files")),
            "images__component", "acceptance_items__component",
        )
        .first()
    )


def _datetime(value):
    return format_date(timezone.localtime(value)) + timezone.localtime(value).strftime(" %H:%M")


def _hours(value, digits=2):
    return DASH if value is None else f"{value:.{digits}f}"


def _reading(value):
    return DASH if value is None else format_number(value)


def _with_unit(text, unit):
    return f"{text} {unit}" if text and text != DASH and unit else text


def _person(user):
    return (user.get_full_name() or user.username) if user else ""


def _report(test_run, tcn, project, result, final):
    """Title, objective (Test Program, + the run's addition), Prepared by and finalization."""
    program = requirements.program_of(project)
    title = program.title if program and program.title else project.description
    objective = _paragraphs(program.objective) if program else []
    addition = _paragraphs(result.objective_addition) if result else []
    finalized = {"by": _person(final["by"]), "at": _datetime(final["at"])} if final else None
    return {
        "title": title,
        "title_source": program.reference if program and program.title else "project description",
        "objective": objective,
        "objective_source": program.reference if objective else "",
        "objective_addition": addition,
        "prepared_by": _person(result.responsible_engineer) if result else "",
        "finalized": finalized,
        "program_reference": program.reference if program else "",
    }


def _identification(test_run, tcn, setup, project, found):
    period = found["period"]
    timeline = found["timeline"]
    seals = [
        {"heading": column["heading"], "seal_type_size": column["seal_type_size"] or DASH}
        for column in seal_columns(setup)
    ]
    dates = ""
    if period.start and period.end:
        first, last = format_date(timezone.localtime(period.start)), format_date(timezone.localtime(period.end))
        dates = first if first == last else f"{first} – {last}"
    spans = timeline.running_spans() if timeline.known else []
    return {
        "project_no": project.project_no,
        "customer": project.customer,
        "run_no": test_run.display_no,
        "tcn_no": f"TCN{test_run.display_no}",
        "tcn_revision": tcn.revision,
        "tcn_status": tcn.get_status_display(),
        "tcn_issued": format_date(timezone.localtime(tcn.issued_at)) if tcn.issued_at else "",
        "test_unit": setup.test_unit,
        "arrangement": setup.configuration_display,
        "reference_drawing": setup.reference_drawing,
        "seals": seals,
        "dates": dates,
        "recordings": [_recording(d) for d in found["datasets"]],
        "start": _datetime(period.start) if period.start else "",
        "start_source": period.start_source,
        "end": _datetime(period.end) if period.end else "",
        "end_source": period.end_source,
        "logged_hours": _hours(period.hours) if period.hours is not None else "",
        "running_hours": _hours(timeline.running_h) if timeline.known else "",
        "running_count": len(spans),
        "running_spans": [{"start": _datetime(_nearest_minute(a)), "end": _datetime(_nearest_minute(b)),
                           "hours": _hours(_span_hours(a, b))} for a, b in spans],  # fmt: skip
        "running_provisional": bool(timeline.provisional),
        "stopped_hours": _hours(timeline.stopped_h) if timeline.known and timeline.stopped_h else "",
        "unknown_hours": _hours(timeline.unknown_h) if timeline.known and round(timeline.unknown_h, 2) else "",
        "running_sources": timeline.sources,
        "running_notes": timeline.notes + _edge_notes(timeline),
        "manual_threshold_rpm": f"{runtime.threshold_rpm():g}",
        "provisional": period.provisional,
        "datasets": [d.original_name for d in found["datasets"]],
    }


def _nearest_minute(moment):
    """A detected start or stop lies between two samples (e.g. 09:59:55): shown to the nearest minute."""
    return (moment + timedelta(seconds=30)).replace(second=0, microsecond=0)


def _span_hours(start, end):
    return (end - start).total_seconds() / 3600


def _recording(dataset):
    """The complete recording of a DCS dataset: start → end and its duration (not shaft running)."""
    start = dataset.recording_started_at
    return {
        "name": dataset.original_name,
        "start": _datetime(start + timedelta(hours=dataset.time_start_h)) if start else "",
        "end": _datetime(start + timedelta(hours=dataset.time_end_h)) if start else "",
        "hours": _hours(dataset.duration_h),
    }


def _edge_notes(timeline):
    """A recording that starts or ends with the shaft running leaves that start or stop unrecorded, unless
    another recording of the run covers the moment (e.g. an export imported mid-test beside the complete one)."""
    states = list(timeline.dcs.values())
    notes = []
    for found in states:
        others = [s for s in states if s is not found]
        for interval in found.intervals:
            if interval.get("start_at") == running_intervals.RECORDING_START and not _covered_by(
                others, running_intervals.moment(found.dataset, interval["start_h"]), before=True
            ):
                notes.append(f"{found.dataset.original_name}: the shaft was already running when the recording "
                             "started; the start is not recorded")  # fmt: skip
            if interval.get("end_at") == running_intervals.RECORDING_END and not _covered_by(
                others, running_intervals.moment(found.dataset, interval["end_h"]), before=False
            ):
                notes.append(f"{found.dataset.original_name}: the shaft was still running when the recording "
                             "ended; the stop is not recorded")  # fmt: skip
    return notes


def _covered_by(states, at, before):
    """Whether another recording has samples on both sides of `at` (absolute time; None: unknown, not covered),
    i.e. it records what happened just before a start or just after a stop."""
    if at is None:
        return False
    for state in states:
        for a, b in state.coverage:
            start, end = (running_intervals.moment(state.dataset, h) for h in (a, b))
            if start is not None and end is not None and (start < at <= end if before else start <= at < end):
                return True
    return False


def _running(found):
    """How the shaft-running time was determined, per DCS recording (frozen in the report snapshot)."""
    determinations = []
    for state in found["timeline"].dcs.values():
        d = state.determination
        determinations.append({
            "dataset": state.dataset.original_name,
            "dataset_sha256": d.dataset_sha256,
            "speed_signal": {"column": d.speed_column, "name": d.speed_name, "unit": d.speed_unit},
            "method": d.method, "method_version": d.method_version,
            "threshold_rpm": d.threshold_rpm, "max_dropout_s": d.max_dropout_s, "min_run_s": d.min_run_s,
            "gap_limit_s": d.gap_limit_s,
            "status": d.status, "stale": state.stale, "provisional": state.provisional,
            "recorded_by": _person(d.recorded_by) if d.recorded_by_id else "",
            "recorded_at": _datetime(d.recorded_at) if d.recorded_at else "",
            "reason": d.reason,
            "effective_intervals": [[i["start_h"], i["end_h"]] for i in d.effective_intervals],
            "detected_intervals": [[i["start_h"], i["end_h"]] for i in d.detected.get("intervals", [])],
            "coverage": d.detected.get("coverage", []),
            "running_hours": round(state.running_h, 6),
        })  # fmt: skip
    return {
        "determinations": determinations,
        "manual_threshold_rpm": found["timeline"].manual_threshold_rpm,
        "discrepancies": [d.message for d in found.get("discrepancies", [])],
    }


def _configuration(setup, components):
    seals = [
        {"heading": c["heading"], "seal_type_size": c["seal_type_size"], "springs": c["springs"]}
        for c in seal_columns(setup)
    ]
    show_seals = any(s["seal_type_size"] or s["springs"] for s in seals)
    return {
        "reference_drawing": setup.reference_drawing,
        "seals": seals if show_seals else [],
        "components": [
            {
                "label": c.label,
                "description": c.description,
                "part_no": c.part_no or DASH,
                "material": c.material or DASH,
                "condition": c.condition or DASH,
                "observations": _paragraphs(c.observations),
            }
            for c in components
        ],
    }


def _criteria(evaluations, basis, position=None, component_id=None):
    """Requirement / assessment of the acceptance criteria that refer to one row."""
    found = []
    for e in evaluations:
        if e.item.basis != basis:
            continue
        if position is not None and e.item.position != position:
            continue
        if component_id is not None and e.item.component_id != component_id:
            continue
        found.append({"requirement": e.requirement or DASH, "assessment": str(e.effective or "missing"),
                      "label": str(e.effective_label)})  # fmt: skip
    return found


def _actual_range(found, code, unit, static=False):
    """Shaft-running readings by default; with `static` the stopped readings (static test)."""
    found_range = next((r for r in found["ranges"] if r.measurement.code == code), None)
    stats = None if found_range is None else (found_range.stopped if static else found_range.running)
    if stats is None:
        return None
    return {"text": report_range(stats.minimum, stats.maximum, unit), "count": stats.count,
            "source": found_range.source_label, "provisional": found_range.provisional and not static}  # fmt: skip


def _running_source(timeline):
    """Compact source of the shaft-running hours: "DCS · Motor Speed", "Manual log", or both."""
    labels = []
    used = {s.source for s in timeline.segments if s.state != runtime.UNKNOWN}
    for found in timeline.dcs.values():
        if f"DCS {found.dataset.original_name}" in used:
            label = f"DCS · {signal_mapping.short_name(found.dataset, 'speed')}"
            if label not in labels:
                labels.append(label)
    if "manual log" in used:
        labels.append("manual log" if labels else "Manual log")
    return " + ".join(labels)


def _period_source(period):
    labels = []
    if period.start_source == "start of the LabVIEW recording" or period.end_source.startswith("end of"):
        labels.append("DCS recording")
    if "manual log" in period.start_source or "manual log" in period.end_source:
        labels.append("manual log" if labels else "Manual log")
    return " + ".join(labels)


def _conditions(setup, found):
    """Test conditions: TCN (instructed) beside the actual values and their source.

    Rows follow the manual-log catalogue of the run (the same codes as the
    TCN's "Parameters to log"), plus the TCN fields that have no reading
    (test unit, duration, media). A row is shown when it has an instructed
    or an actual value. Actual = min – max of the shaft-running readings
    (a reviewed DCS signal of the parameter over its running intervals,
    else the manual readings), transients included: not a steady-state
    range, at report precision (formatting.report_number). A static test
    (no running readings) reports its stopped readings, labelled static.
    The TCN values are instructions, never acceptance limits: acceptance
    is assessed in the Leakage, Face wear and Acceptance sections, not here.
    """
    groups = []
    static = not any(r.running for r in found["ranges"]) and any(r.stopped for r in found["ranges"])

    def group(heading):
        groups.append({"heading": heading, "rows": []})
        return groups[-1]["rows"]

    def add(rows, parameter, unit, instructed, actual=None):
        actual = actual or {"text": DASH, "count": 0, "source": "", "provisional": False}
        if not instructed and actual["text"] == DASH:
            return
        rows.append({
            "parameter": parameter, "unit": unit, "instructed": instructed or DASH,
            "actual": actual["text"], "count": actual["count"],
            "source": actual["source"] if actual["text"] != DASH else "",
            "provisional": actual["provisional"] and actual["text"] != DASH,
        })  # fmt: skip

    def actual(column):
        return _actual_range(found, column.code, column.unit, static)

    period = found["period"]
    timeline = found["timeline"]
    by_group = {}
    for column in found["columns"]:
        if column.kind != LEAKAGE:
            by_group.setdefault(column.group, []).append(column)

    rows = group("Test run")
    add(rows, "Test unit", "", setup.test_unit)
    duration = None
    if timeline.known:
        duration = {"text": report_number(timeline.running_h, "h"), "count": 0,
                    "source": _running_source(timeline), "provisional": bool(timeline.provisional)}  # fmt: skip
    add(rows, "Duration, shaft running", "h", setup.duration, duration)
    if period.hours:
        add(rows, "Elapsed clock time", "h", "", {"text": report_number(period.hours, "h"), "count": 0,
                                                  "source": _period_source(period), "provisional": False})  # fmt: skip
    for column in by_group.pop(COMMON_GROUP, []):
        add(rows, column.label, column.unit, evidence.instructed_value(setup, column.code), actual(column))

    circuits = []
    if setup.plug_seal_present:
        circuits.append(("Process", "process_medium"))
    if setup.arrangement != Arrangement.SINGLE or any(getattr(setup, f) for f in conditions.SEAL_SUPPORT_FIELDS):
        circuits.append((seal_support_label(setup.arrangement), "seal_support_medium"))
    for heading, medium_field in circuits:
        rows = group(f"{heading} fluid" if heading == "Process" else heading)
        add(rows, "Medium", "", getattr(setup, medium_field))
        for column in by_group.pop(heading, []):
            add(rows, column.label, column.unit, evidence.instructed_value(setup, column.code), actual(column))

    for seal in found["leakage"]:  # per seal position: its readings, then its average leakage
        by_group.setdefault(seal.label, [])
    for heading, columns in by_group.items():
        rows = group(heading)
        for column in columns:
            add(rows, column.label, column.unit, evidence.instructed_value(setup, column.code), actual(column))
        seal = next((s for s in found["leakage"] if s.label == heading), None)
        if seal is not None and seal.average is not None:
            add(rows, "Leakage, average dynamic", "ml/h", "", {
                "text": report_number(seal.average, "ml/h"), "count": 0, "source": "Manual log",
                "provisional": bool(timeline.provisional),
            })  # fmt: skip
    groups = [g for g in groups if g["rows"]]
    provisional = any(row["provisional"] for g in groups for row in g["rows"])
    return {"groups": groups, "static": static, "provisional": provisional}


def _collection(c):
    period = f"{_short(c.start)} – {_short(c.end)}" if c.start else f"until {_short(c.end)}"
    return {
        "period": period,
        "hours": _hours(c.hours) if c.hours else DASH,
        "volume": _reading(c.volume),
        "kind": c.label,
        "reason": c.reason,
        "rate": _hours(c.rate, 1) if c.rate is not None else "",
        "split": runtime.describe_split(c.split) if c.split is not None and c.kind in (evidence.MIXED, evidence.UNCLASSIFIED) else "",
    }


def _short(moment):
    local = timezone.localtime(moment)
    return local.strftime("%d-%b %H:%M")


def _leakage(leakage, evaluations):
    found = []
    for seal in leakage:
        if not seal.collections:
            continue
        uncovered = seal.uncovered_running_h
        covered = seal.dynamic_hours if seal.dynamic else 0.0
        running = covered + uncovered if uncovered is not None else None
        found.append({
            "seal": seal.label,
            "total": _reading(seal.total_volume),
            "dynamic_volume": _reading(seal.dynamic_volume) if seal.dynamic else DASH,
            "dynamic_hours": _hours(seal.dynamic_hours) if seal.dynamic else DASH,
            "dynamic_count": len(seal.dynamic),
            "average": _hours(seal.average, 1),
            "reason": seal.reason,
            "uncovered": _hours(uncovered) if uncovered is not None and uncovered > 0.005 else "",
            "running_hours": running,  # shaft running of the period with collections [h]; None: unknown
            "coverage_pct": 100 * covered / running if running else None,
            "static_average": _hours(seal.static_average, 1) if seal.static_average is not None else "",
            "static_volume": _reading(seal.static_volume) if seal.static else "",
            "static_hours": _hours(seal.static_hours) if seal.static else "",
            "mixed": [_collection(c) for c in seal.mixed],
            "excluded": [_collection(c) for c in seal.excluded if c.kind not in (evidence.STATIC, evidence.MIXED)],
            "criteria": _criteria(evaluations, Basis.LEAKAGE_RATE, position=seal.position),
        })  # fmt: skip
    return found


def _events(rows):
    found = []
    for number, row in enumerate(evidence.events(rows), start=1):
        found.append({
            "ref": f"E{number}",
            "entry_id": row.entry.pk,
            "at": _datetime(row.entry.observed_at),
            "elapsed": _hours(row.elapsed_h),
            "category": row.entry.get_category_display() if row.entry.category else "",
            "note": row.entry.note.strip(),
        })  # fmt: skip
    return found


def _summary_cell(value, code):
    """One Pre or Post cell of the summary: the value with its HLB/classification, or why there is none."""
    if value.value is not None:
        extra = []
        if value.hlb is not None:
            extra.append(f"{signed_fixed(value.hlb, 3) if code in SIGNED_QUANTITIES else fixed(value.hlb, 3)} HLB")
        if value.classification:
            extra.append(value.classification)
        if value.note:
            extra.append(value.note)
        return {"value": quantity_value(value.value, code), "extra": " · ".join(extra), "status": value.status}
    if value.status == measurement_summary.COMPLETED:
        return {"value": DASH, "extra": value.note, "status": value.status}
    return {"value": DASH, "extra": measurement_summary.STATUS_LABELS[value.status], "status": value.status}


def _measurements(components):
    """Pre/Post summary from the reported measurements; not-applicable quantities have no row."""
    found = []
    for group in measurement_summary.summary(components):
        rows = []
        for row in group.rows:
            code = row.quantity.code
            rows.append({
                "kind": str(row.quantity.kind),
                "quantity": row.label,
                "unit": row.unit,
                "source": row.source,
                "pre": _summary_cell(row.pre, code),
                "post": _summary_cell(row.post, code),
                "delta": quantity_delta(row.delta, code) or DASH,
                "wear": (format_number(row.wear) if row.wear is not None else DASH) if row.quantity.wear else "",
                "wear_quantity": row.quantity.wear,
            })  # fmt: skip
        found.append({"component": group.component.label, "component_id": group.component.pk, "rows": rows,
                      "optional": not group.component.is_face})  # fmt: skip
    return found


REPORT_PANEL = {"width": 380, "height": 210}


def _files(measurement):
    return [{"name": f.original_name, "sha256": f.sha256} for f in measurement.files.all()]


def _pre_post(rows, label, unit, pre, post, digits=None, signed=False, delta=True):
    """A row of a detail table: Pre, Post and Δ of a numeric attribute (None where not available)."""
    def text(v):
        if v is None:
            return DASH
        return signed_fixed(v, digits) if signed else fixed(v, digits)

    d = post - pre if delta and pre is not None and post is not None else None
    rows.append({"label": label, "unit": unit, "pre": text(pre), "post": text(post),
                 "delta": signed_fixed(d, digits) if d is not None else (DASH if delta else "")})  # fmt: skip


def _text_row(rows, label, pre, post, unit=""):
    rows.append({"label": label, "unit": unit, "pre": pre or DASH, "post": post or DASH, "delta": ""})


def _attr(analysis, name):
    return getattr(analysis, name) if analysis is not None else None


def _mahr_detail(component, pre, post, codes):
    analyses = {"pre": pre.mahr if pre else None, "post": post.mahr if post else None}
    a, b = analyses["pre"], analyses["post"]
    rows = []
    _pre_post(rows, "Radial taper, signed", "µm", _attr(a, "taper_um"), _attr(b, "taper_um"), 4, signed=True)
    _pre_post(rows, "Radial taper, light bands", "HLB", _attr(a, "taper_hlb"), _attr(b, "taper_hlb"), 3, signed=True)
    _text_row(rows, "Taper classification", _attr(a, "classification"), _attr(b, "classification"))
    _text_row(rows, "Flat limit", *(f"± {x.flat_limit_hlb:g} HLB" if x else "" for x in (a, b)))
    _pre_post(rows, "Roughness Ra, mean of scans*", "µm", _attr(a, "mean_ra_um"), _attr(b, "mean_ra_um"), 3)
    for side, label in (("left", "left"), ("right", "right")):
        _pre_post(rows, f"Roughness Ra, {label} scan*", "µm", _attr(a, f"{side}_ra_um"), _attr(b, f"{side}_ra_um"), 3)
        _pre_post(rows, f"Roughness Rz, {label} scan*", "µm", _attr(a, f"{side}_rz_um"), _attr(b, f"{side}_rz_um"), 3)
    for side in ("left", "right"):
        _pre_post(rows, f"Profile P–V, {side} scan", "µm", _attr(a, f"{side}_pv_um"), _attr(b, f"{side}_pv_um"), 4)
    _text_row(rows, "Profile view (P–V)", *(mahr.VIEWS[x.pv_view] if x else "" for x in (a, b)))
    _text_row(rows, "Evaluated interval L / R", *(
        f"{x.left_start_mm:.2f}–{x.left_end_mm:.2f} / {x.right_start_mm:.2f}–{x.right_end_mm:.2f} mm" if x else ""
        for x in (a, b)))  # fmt: skip
    _text_row(rows, "First points skipped", *(str(x.skip_points) if x else "" for x in (a, b)))
    _text_row(rows, "Face OD / ID", *(f"{x.od:g} / {x.id:g} {x.diameter_unit}" if x else "" for x in (a, b)))
    figures_found = []
    if "mahr_profiles" in codes:
        results = {stage: seal_faces.calculate(m) for stage, m in (("pre", pre), ("post", post)) if m}
        x_range = figures.mahr_x_range(results.values())
        views = {r.settings.pv_view for r in results.values()}
        shared = figures.mahr_y_range(results.values(), views.pop()) if len(views) == 1 else None
        grid = []
        for side in ("left", "right"):
            cells = {}
            for stage, r in results.items():
                view = r.settings.pv_view
                y_range = shared or figures.mahr_y_range([r], view)
                fit = r.left if side == "left" else r.right
                cells[stage] = figures.mahr_panel(
                    r, side, view, x_range, y_range, **REPORT_PANEL,
                    title=f"{Stage(stage).label} · {side} · {fit.profile.name}",
                )  # fmt: skip
            grid.append(cells)
        figures_found.append({"code": "mahr_profiles", "title": "Left and right profiles", "grid": grid,
                              "note": "Shared axes; profile view as listed below." if shared else
                              "Different profile views Pre and Post: each column has its own height axis."})  # fmt: skip
    if "taper_gauge" in codes:
        figures_found.append(_gauges(a, b))
    return {"rows": rows, "figures": figures_found,
            "notes": ["*Ra and Rz as exported by the instrument (its evaluation, not the selected interval).",
                      "Profile P–V is a profile range over the evaluated interval, not whole-face flatness.",
                      "Taper: opposing-scan method of the Excel template, −(mL − mR) × (OD − ID) / 4; negative = "
                      "ID high (Convex), positive = OD high (Concave), Flat within the flat limit."]}  # fmt: skip


def _gauges(a, b):
    """Taper in light bands, Pre and Post (analyses with a taper)."""
    row = {}
    for stage, x in (("pre", a), ("post", b)):
        if x is not None and x.taper_um is not None and x.flat_limit_hlb is not None and x.classification:
            row[stage] = figures.taper_gauge(x.taper_hlb, x.flat_limit_hlb, x.taper_um, x.classification,
                                             title=f"{Stage(stage).label} · taper", width=REPORT_PANEL["width"])
    return {"code": "taper_gauge", "title": "Taper in helium light bands", "grid": [row] if row else [],
            "note": "Convex (ID high) left, Concave (OD high) right; green: the flat band."}


def _roundpak_detail(component, pre, post, codes):
    a, b = (pre.roundpak if pre else None), (post.roundpak if post else None)
    rows = []
    _pre_post(rows, "Instrument flatness FLTt", "µm", _attr(a, "flatness_um"), _attr(b, "flatness_um"), 3)
    _pre_post(rows, "Instrument flatness FLTt, light bands", "HLB", _attr(a, "flatness_hlb"), _attr(b, "flatness_hlb"), 3)
    _text_row(rows, "Instrument check result", _attr(a, "check_result"), _attr(b, "check_result"))
    _text_row(rows, "Instrument tolerance (upper / lower)",
              *(f"{x.tolerance_upper or DASH} / {x.tolerance_lower or DASH}" if x else "" for x in (a, b)))  # fmt: skip
    _text_row(rows, "Measurement radius", _attr(a, "circular_radius"), _attr(b, "circular_radius"))
    _pre_post(rows, "Circular LFD P–V (calculated)", "µm", _attr(a, "lfd_pv_um"), _attr(b, "lfd_pv_um"), 4)
    _pre_post(rows, "Mean outward radial slope (calculated)", "µm/mm", _attr(a, "mean_slope"), _attr(b, "mean_slope"),
              5, signed=True)  # fmt: skip
    _pre_post(rows, "Setup tilt between the radial traces (calculated)", "µm/mm", _attr(a, "tilt_slope"),
              _attr(b, "tilt_slope"), 5, signed=True, delta=False)  # fmt: skip
    if any(x is not None and x.taper_um is not None for x in (a, b)):
        _pre_post(rows, "Radial taper, signed (calculated)", "µm", _attr(a, "taper_um"), _attr(b, "taper_um"), 4,
                  signed=True)  # fmt: skip
        _pre_post(rows, "Radial taper, light bands", "HLB", _attr(a, "taper_hlb"), _attr(b, "taper_hlb"), 3, signed=True)
        _text_row(rows, "Taper classification", *((x.classification or "not classified") if x else ""
                                                  for x in (a, b)))  # fmt: skip
    _text_row(rows, "Strongest waviness components (UPR)", *(
        ", ".join(f"{h['k']} UPR {h['amplitude_um']:.3f} µm" for h in x.strongest_harmonics) if x else ""
        for x in (a, b)))  # fmt: skip
    results = {}
    if codes:
        results = {stage: seal_faces.calculate(m) for stage, m in (("pre", pre), ("post", post)) if m}
    figures_found = []
    if "roundpak_circular" in codes:
        circulars = {stage: r.circular for stage, r in results.items() if r.circular}
        if circulars:
            y_range = figures.padded(min(c.lo for c in circulars.values()), max(c.hi for c in circulars.values()))
            row = {stage: figures.roundpak_circular(c, y_range, title=f"{Stage(stage).label} · circular trace "
                                                    f"{c.scan.line.get('X Position', '')}".strip(),
                                                    guides=figures.radial_guides(results[stage]), **REPORT_PANEL)
                   for stage, c in circulars.items()}  # fmt: skip
            figures_found.append({"code": "roundpak_circular", "title": "Circular trace", "grid": [row],
                                  "note": "Shared height axis. Angular datum of Pre and Post as exported."})  # fmt: skip
    if "roundpak_polar" in codes:
        row = {stage: figures.roundpak_polar(r.circular, size=240, title=f"{Stage(stage).label} · polar",
                                             guides=figures.radial_guides(r))
               for stage, r in results.items() if r.circular}  # fmt: skip
        if row:
            figures_found.append({"code": "roundpak_polar", "title": "Polar profile", "grid": [row], "polar": True,
                                  "note": "Height exaggerated, not physical radial distortion."})  # fmt: skip
    if "roundpak_radial" in codes:
        radial = {stage: r for stage, r in results.items() if r.left and r.right}
        if radial:
            evaluations = [e for r in radial.values() for e in (r.left, r.right)]
            x_range = (min(e.start for e in evaluations), max(e.end for e in evaluations))
            y_range = figures.padded(min(e.lo for e in evaluations), max(e.hi for e in evaluations))
            grid = [{stage: figures.roundpak_radial(r, side, x_range, y_range, **REPORT_PANEL)
                     for stage, r in radial.items()} for side in ("left", "right")]  # fmt: skip
            figures_found.append({"code": "roundpak_radial", "title": "Radial profiles", "grid": grid,
                                  "note": "Shared axes; dashed: least-squares fit over the evaluation interval."})
    if "roundpak_harmonics" in codes:
        row = {stage: figures.roundpak_harmonics(r.harmonics, title=f"{Stage(stage).label} · waviness by UPR",
                                                 width=REPORT_PANEL["width"], height=170)
               for stage, r in results.items() if r.harmonics}  # fmt: skip
        if row:
            figures_found.append({"code": "roundpak_harmonics", "title": "Waviness by undulations per revolution",
                                  "grid": [row], "note": "Amplitude = half the peak-to-valley of each sine component "
                                  "of the circular trace; the three strongest highlighted."})  # fmt: skip
    if "roundpak_radial_taper" in codes:
        radial = {stage: r for stage, r in results.items() if r.left and r.right}
        if radial:
            evaluations = [e for r in radial.values() for e in (r.left, r.right)]
            x_range = (min(e.start for e in evaluations), max(e.end for e in evaluations))
            y_range = figures.radial_taper_y_range(radial.values())
            row = {stage: figures.roundpak_radial_taper(r, x_range, y_range, title=f"{Stage(stage).label} · radial "
                                                        "traces, setup tilt removed", **REPORT_PANEL)
                   for stage, r in radial.items()}  # fmt: skip
            figures_found.append({"code": "roundpak_radial_taper", "title": "Radial traces, setup tilt removed",
                                  "grid": [row], "note": "Shared axes; each trace zeroed at its inner end; dashed: "
                                  "the taper (mean slope)."})  # fmt: skip
    if "taper_gauge" in codes:
        found = _gauges(a, b)
        if found["grid"]:
            figures_found.append(found)
    return {"rows": rows, "figures": figures_found,
            "notes": ["The instrument's flatness, tolerance and check result are reported as exported; the values "
                      "marked calculated are TestLab's from the exported traces and never replace them.",
                      "Radial taper: mean slope of the two radial traces 180° apart × (OD − ID) / 2; negative = ID high "
                      "(Convex), positive = OD high (Concave), Flat within the flat limit. The setup tilt cancels.",
                      "One circular trace at the measurement radius, not a map of the whole sealing face."]}  # fmt: skip


def _measurement_details(components):
    """Mahr and ROUNDPAK detail blocks: per component and kind (the measurement slot), the reported Pre → Post
    measurements with every figure available. Which of them a report shows is a profile selection."""
    found = []
    builders = {MeasurementKind.MAHR: _mahr_detail, MeasurementKind.ROUNDPAK: _roundpak_detail}
    for component in components:
        measurements = measurement_summary.measurements_of(component)
        for kind, build in builders.items():
            if component.applicability(kind) == Applicability.NOT_APPLICABLE:
                continue
            pre = measurement_summary.reported(measurements, Stage.PRE, kind)
            post = measurement_summary.reported(measurements, Stage.POST, kind)
            if not (pre or post):
                continue
            block = build(component, pre, post, set(seal_faces.REPORT_FIGURES[kind]))
            found.append({
                "slot": f"{component.pk}:{kind}",
                "component_id": component.pk,
                "title": f"{component.label} — {MeasurementKind(kind).label}",
                "kind": str(kind),
                "sources": {stage: ({"files": _files(m), "engine": m.engine, "measured": m.measured_on_text,
                                     "remark": m.remark} if m else None)
                            for stage, m in (("pre", pre), ("post", post))},
                **block,
            })  # fmt: skip
    return found


def _signed(value):
    text = format_number(abs(value))
    return f"+{text}" if value > 0 else f"−{text}" if value < 0 else text


def _signed_reading(value):
    return DASH if value is None else _signed(value)


def _wear(wear, evaluations):
    return [
        {
            "component": w.component.label,
            "component_id": w.component.pk,
            "quantity": "Face length",
            "unit": w.unit,
            "pre": _reading(w.pre.value if w.pre else None),
            "post": _reading(w.post.value if w.post else None),
            "delta": _signed_reading(w.delta),
            "wear": _reading(w.loss),
            "hours": _hours(w.hours),
            "hours_source": w.hours_source,
            "split": runtime.describe_split(w.split) if w.split is not None else "",
            "direction": w.direction,
            "rate": DASH if w.rate is None else format_number(w.rate),
            "reason": w.reason if w.rate is None else "",
            "criteria": _criteria(evaluations, Basis.WEAR_RATE, component_id=w.component.pk),
        }
        for w in wear
    ]


def _acceptance_row(e):
    item = e.item
    basis = ""
    if item.basis != Basis.MANUAL:
        basis = item.get_basis_display()
        if item.position:
            basis += f" · {item.get_position_display()}"
        if item.component_id:
            basis += f" · {item.component.label}"
    return {
        "criterion": item.criterion,
        "basis": basis,
        "requirement": e.requirement or DASH,
        "requirement_source": item.source or "entered on the Test Result",
        "actual": e.actual_text or DASH,
        "source": e.source if item.basis != Basis.MANUAL else "",
        "assessment": str(e.effective or "missing"),
        "label": str(e.effective_label),
        "by": {"engineer": "engineer", "calculated": "calculated"}.get(e.by, "not assessed"),
        "note": e.note if not e.by else "",
        "remark": item.remark,
    }


def _paragraphs(text):
    """Blank-line separated paragraphs, each a list of lines."""
    blocks = [block.strip() for block in text.replace("\r\n", "\n").split("\n\n")]
    return [[line.strip() for line in block.splitlines()] for block in blocks if block]


def _narrative(result):
    if result is None:
        return {"observations": [], "conclusion": [], "recommendation": []}
    return {
        "observations": _paragraphs(result.observations),
        "conclusion": _paragraphs(result.conclusion),
        "recommendation": _paragraphs(result.recommendation),
    }


def _picture(context, picture):
    path = f"assets/picture-{picture.pk}.jpg"
    context["assets"].append({"path": path, "kind": "picture", "id": picture.pk, "sha256": picture.sha256})
    width, height = picture.width, picture.height
    return {
        "id": picture.pk,
        "src": path,
        "caption": picture.caption,
        "name": picture.original_name,
        "orientation": "portrait" if height > width else "landscape",
    }


def _pictures(context, result, components):
    groups = images.picture_groups(result, components)
    pictures = context["pictures"]
    pictures["cross_sections"] = [_picture(context, p) for p in groups["cross_sections"]]
    single = {"Pre-test": [], "Post-test": []}
    for comparison in groups["comparisons"]:
        component = comparison["component"]
        label = component.label if component else "Whole seal"
        if comparison["pre"] and comparison["post"]:  # side by side, Pre ↔ Post
            pairs = [
                {"pre": _picture(context, pre) if pre else None, "post": _picture(context, post) if post else None}
                for pre, post in zip_longest(comparison["pre"], comparison["post"])
            ]
            pictures["comparisons"].append({"label": label, "pairs": pairs})
        else:  # only one stage photographed: no empty half-page placeholders
            stage = "Pre-test" if comparison["pre"] else "Post-test"
            for picture in comparison["pre"] or comparison["post"]:
                single[stage].append({"label": label, "picture": _picture(context, picture)})
    for stage, items in single.items():
        if items:
            rows = [items[i : i + 2] for i in range(0, len(items), 2)]
            pictures["single_stage"].append({"stage": stage, "rows": rows})
    pictures["other"] = [_picture(context, p) for p in groups["other"]]


def _plots(context, result):
    for number, plot in enumerate(result.plots.select_related("dataset").order_by("order", "pk"), start=1):
        path = f"assets/plot-{plot.pk}.png"
        context["assets"].append({"path": path, "kind": "plot", "id": plot.pk, "sha256": plot.image_sha256})
        about = plots.describe(plot)
        rendering = plot.rendering or {}
        if rendering.get("decimated"):
            data = (f"{rendering['samples_in_window']} samples per signal, drawn as min/max of time buckets "
                    f"({rendering['points_per_signal']} points)")  # fmt: skip
        elif rendering:
            data = f"all {rendering.get('samples_in_window', 0)} samples per signal"
        else:
            data = ""
        context["plots"].append({
            "id": plot.pk,
            "number": number,
            "src": path,
            "caption": plot.caption,
            "dataset": plot.dataset.original_name,
            "dataset_sha256": plot.dataset.sha256,
            "signals": about["signals"],
            "window": about["window"],
            "ranges": about["ranges"],
            "data": data,
            "landscape": plot.image_width >= plot.image_height,
        })  # fmt: skip


def _log(found, events):
    """The manual log as report tables: only the measurements that hold a reading.

    Leakage: one collection-period column (it is per entry, the same for
    every seal), then per seal the collected volume and the calculated rate.
    More columns than fit portrait A4 are split into consecutive tables, at
    column-group boundaries, each repeating date/time and elapsed hours.
    """
    rows = found["rows"]
    if not rows:
        return {"tables": [], "count": 0, "provisional": found["origin"].provisional}
    event_refs = {e["entry_id"]: e["ref"] for e in events}
    columns = []
    used = lambda code: any(code in row.values for row in rows)  # noqa: E731
    value_columns = [c for c in found["columns"] if c.kind != LEAKAGE and used(c.code)]
    leakage_columns = [c for c in found["columns"] if c.kind == LEAKAGE and (used(c.code) or any(c.code in r.leakage for r in rows))]
    for c in value_columns:
        columns.append({"group": c.group if c.group != COMMON_GROUP else "", "label": c.label, "unit": c.unit,
                        "kind": "value", "code": c.code})  # fmt: skip
    if leakage_columns:
        columns.append({"group": "Leakage", "label": "Period", "unit": "h", "kind": "period", "code": ""})
        for c in leakage_columns:
            seal = c.group.replace(" Seal", "")
            columns.append({"group": "Leakage", "label": seal, "unit": "ml", "kind": "volume", "code": c.code})
            columns.append({"group": "Leakage", "label": f"{seal} rate", "unit": "ml/h", "kind": "rate", "code": c.code})

    def cell(row, column):
        if column["kind"] == "value":
            value = row.values.get(column["code"])
            return "" if value is None else format_number(value.value)
        leak = row.leakage
        if column["kind"] == "period":
            hours = next((l.hours for l in leak.values() if l.hours is not None), None)  # noqa: E741
            return "" if hours is None else f"{hours:.2f}"
        found_leak = leak.get(column["code"])
        if found_leak is None:
            return ""
        if column["kind"] == "volume":
            return "" if found_leak.volume is None else format_number(found_leak.volume)
        return "" if found_leak.rate is None else f"{found_leak.rate:.1f}"

    tables = []
    for chunk in _chunks(columns, LOG_COLUMNS_PER_TABLE):
        groups = []
        for column in chunk:
            if groups and groups[-1]["heading"] == column["group"]:
                groups[-1]["span"] += 1
            else:
                groups.append({"heading": column["group"], "span": 1})
        tables.append({
            "groups": groups,
            "columns": [{k: c[k] for k in ("label", "unit", "kind")} for c in chunk],
            "rows": [
                {
                    "date": format_date(timezone.localtime(row.entry.observed_at)),
                    "time": timezone.localtime(row.entry.observed_at).strftime("%H:%M"),
                    "elapsed": _hours(row.elapsed_h),
                    "cells": [cell(row, c) for c in chunk],
                    "event": event_refs.get(row.entry.pk, ""),
                }
                for row in rows
            ],
        })  # fmt: skip
    return {"tables": tables, "count": len(rows), "provisional": found["origin"].provisional}


def _chunks(columns, size):
    """Split columns into tables of at most `size`, preferring a split between groups."""
    chunks, current = [], []
    for column in columns:
        if len(current) >= size:
            cut = len(current)
            for i in range(len(current) - 1, 0, -1):
                if current[i]["group"] != current[i - 1]["group"]:
                    cut = i
                    break
            chunks.append(current[:cut])
            current = current[cut:]
        current.append(column)
    if current:
        chunks.append(current)
    return chunks


def _section_numbers(context):
    """Numbers of the sections that have content, in report order (empty sections are omitted)."""
    present = [
        ("objective", bool(context["report"]["objective"] or context["report"]["objective_addition"])),
        ("description", bool(context["description"]["project"] or context["description"]["run_lines"])),
        ("configuration", bool(context["configuration"]["components"] or context["configuration"]["seals"]
                               or context["configuration"]["reference_drawing"] or context["pictures"]["cross_sections"])),
        ("conditions", bool(context["conditions"]["groups"])),
        ("leakage", bool(context["leakage"])),
        ("plots", bool(context["plots"])),
        ("events", bool(context["events"] or context["narrative"]["observations"])),
        ("measurements", bool(context["measurements"] or context["wear"] or context["component_observations"])),
        ("measurement_details", bool(context["measurement_details"])),
        ("pictures", bool(context["pictures"]["comparisons"] or context["pictures"]["single_stage"]
                          or context["pictures"]["other"])),
        ("acceptance", bool(context["acceptance"])),
        ("conclusion", bool(context["narrative"]["conclusion"] or context["narrative"]["recommendation"])),
    ]  # fmt: skip
    numbers, n = {}, 0
    for name, shown in present:
        if shown:
            n += 1
            numbers[name] = n
    return numbers

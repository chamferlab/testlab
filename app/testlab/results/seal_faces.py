"""Seal-face measurements of a Result (M2D): store, calculate, review, report.

    instrument file(s) ──► MeasurementFile (byte for byte, SHA-256)
            │ parse + calculate with the stored settings (facemeasure/, engine version)
            ▼
    MahrAnalysis / RoundpakAnalysis (settings + results)   DimensionalReading (value as measured)
            │ engineer review
            ▼
    reviewed ──► figures generated and kept (MeasurementFigure) ──► reported in the summary and report

Storage (server file store, next to the Result's pictures):

    projects/<project_no>/runs/<run_no>/result/measurements/<upload time>-<file name>      original, unchanged
    projects/<project_no>/runs/<run_no>/result/measurements/figures/<id>-<code>.svg        generated at review

A Draft measurement is recalculated whenever its settings change; a
reviewed one keeps its stored results. Reviewing a measurement makes it the
reported one of its slot (component, stage, kind); earlier measurements of
the slot stay on record and can be chosen again. Everything here refuses
changes once the Result is no longer a Draft (testlab/locks.py).
"""

import hashlib
import re
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from testlab import locks

from . import facemeasure
from .facemeasure import CalculationError, InstrumentFileError, figures, mahr, roundpak
from .inspection import Applicability, MeasurementKind, Stage
from .models import (
    DimensionalReading,
    FaceMeasurement,
    MahrAnalysis,
    MeasurementFigure,
    MeasurementFile,
    RoundpakAnalysis,
    TestResult,
)

Role = MeasurementFile.Role

# Figures of the Engineering report by default (documents/report_profiles.slot_default; Customer: none).
DEFAULT_FIGURES = {
    MeasurementKind.MAHR: ["mahr_profiles", "taper_gauge"],
    MeasurementKind.ROUNDPAK: ["roundpak_circular", "taper_gauge"],
    MeasurementKind.DIMENSIONAL: [],
}
# Figure choices of a report's measurement detail (per report profile): code → label.
REPORT_FIGURES = {
    MeasurementKind.MAHR: {"mahr_profiles": "Left and right profiles", "taper_gauge": "Taper in light bands"},
    MeasurementKind.ROUNDPAK: {
        "roundpak_circular": "Circular trace",
        "roundpak_polar": "Polar profile",
        "roundpak_harmonics": "Waviness by undulations per revolution (UPR)",
        "roundpak_radial_taper": "Radial traces, setup tilt removed",
        "roundpak_radial": "Radial profiles (each with its own fit)",
        "taper_gauge": "Taper in light bands",
    },
    MeasurementKind.DIMENSIONAL: {},
}


class MeasurementError(Exception):
    """A measurement change the rules or the files do not allow; the message says why."""


# ---- rules ----------------------------------------------------------------------------------


def ensure_editable(result):
    if result.status != TestResult.Status.DRAFT:
        raise MeasurementError(locks.final_result_message(result.test_run))


def ensure_applicable(component, kind):
    if component.applicability(kind) == Applicability.NOT_APPLICABLE:
        raise MeasurementError(
            f"{component.kind_label(kind)} is not applicable to {component}. Change the measurement plan first."
        )


def ensure_not_on_record(measurement, action):
    """A measurement held by a final revision stays as it is while a later revision is in progress."""
    if lock := locks.on_record_lock(measurement, action):
        raise MeasurementError(f"{lock} Add the corrected measurement as a new one and review it: it becomes the "
                               "reported measurement of its slot.")  # fmt: skip


def ensure_draft(measurement):
    ensure_editable(measurement.component.result)
    if measurement.is_reviewed:
        raise MeasurementError("A reviewed measurement cannot change; reopen it first.")


# ---- files ----------------------------------------------------------------------------------


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "measurement")[:120]


def measurement_dir(test_run):
    return f"projects/{test_run.project.project_no}/runs/{test_run.display_no}/result/measurements"


def _storage():
    return MeasurementFile._meta.get_field("file").storage


def read_upload(uploaded):
    """(name, bytes) of an uploaded instrument file within the size limit."""
    limit = settings.TESTLAB_MEASUREMENT_MAX_MB
    if uploaded.size > limit * 1024 * 1024:
        raise MeasurementError(f"{uploaded.name} is larger than {limit} MB.")
    return uploaded.name, uploaded.read()


def file_content(measurement_file):
    with measurement_file.file.open("rb") as stored:
        return stored.read()


def duplicate_warnings(measurement):
    """Notices for files identical (SHA-256) to other files of the Result or of this measurement."""
    files = list(measurement.files.all())
    warnings = []
    if len(files) == 2 and files[0].sha256 == files[1].sha256:
        warnings.append(f"The left and right files are identical ({files[0].original_name}).")
    for stored in files:
        others = MeasurementFile.objects.filter(
            sha256=stored.sha256, measurement__component__result=measurement.component.result
        ).exclude(measurement=measurement).select_related("measurement__component")
        for found in others:
            warnings.append(f"{stored.original_name} is identical to {found.original_name} of {found.measurement}.")
    return warnings


def _store_files(measurement, files, user, written):
    storage = _storage()
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    base = measurement_dir(measurement.component.result.test_run)
    for role, (name, content, metadata) in files.items():
        stored = storage.save(f"{base}/{stamp}-{_safe_name(name)}", ContentFile(content))
        written.append(stored)
        with storage.open(stored, "rb") as check:
            if check.read() != content:
                raise MeasurementError(f"{name} was not stored correctly; nothing was saved.")
        MeasurementFile.objects.create(
            measurement=measurement, role=role, file=stored, original_name=name[:255], size=len(content),
            sha256=hashlib.sha256(content).hexdigest(), metadata=metadata, uploaded_by=user,
        )  # fmt: skip


def _aware(value):
    return timezone.make_aware(value) if value is not None else None


def _parse_datetime(text, formats):
    for pattern in formats:
        try:
            return datetime.strptime(text.strip(), pattern)
        except ValueError:
            continue
    return None


def _new_measurement(component, stage, kind, user, **fields):
    return FaceMeasurement.objects.create(
        component=component, stage=stage, kind=kind, created_by=user, updated_by=user, **fields,
    )  # fmt: skip


def _create(component, stage, kind, user, files, build):
    """Create a Draft measurement with its stored files; build(measurement) adds the analysis row."""
    ensure_editable(component.result)
    ensure_applicable(component, kind)
    if stage not in Stage.values:
        raise MeasurementError("Choose Pre-test or Post-test.")
    written = []
    try:
        with transaction.atomic():
            measurement = build(files, written)
            return measurement
    except Exception:
        storage = _storage()
        for name in written:
            storage.delete(name)
        raise


def _touch(result, user):
    result.updated_by = user
    result.save(update_fields=["updated_by", "updated_at"])


# ---- Mahr -----------------------------------------------------------------------------------


def parse_mahr(name, content):
    try:
        return mahr.parse(facemeasure.decode(content), name)
    except InstrumentFileError as error:
        raise MeasurementError(f"{name} could not be read: {error}") from error


def face_dimensions(component, od=None, id_=None, units=None):
    """Face OD/ID as entered for the measurement, else as entered on the component (both or neither)."""
    if od is None and id_ is None:
        od, id_ = component.face_od, component.face_id
        units = units or component.diameter_unit
    if od is None or id_ is None:
        return None, None, units or component.diameter_unit
    return float(od), float(id_), units or component.diameter_unit


def default_mahr_settings(component, left, right, od=None, id_=None, units=None):
    od, id_, units = face_dimensions(component, od, id_, units)
    return mahr.Settings(od=od, id=id_, units=units, left_end=max(left.length, left.travel),
                         right_end=max(right.length, right.travel))  # fmt: skip


def mahr_settings(analysis):
    return mahr.Settings(
        od=analysis.od, id=analysis.id, units=analysis.diameter_unit, light_band_um=analysis.light_band_um,
        left_direction=analysis.left_direction, right_direction=analysis.right_direction,
        left_start=analysis.left_start_mm, left_end=analysis.left_end_mm,
        right_start=analysis.right_start_mm, right_end=analysis.right_end_mm, pv_view=analysis.pv_view,
        skip_points=analysis.skip_points, flat_limit_hlb=analysis.flat_limit_hlb,
    )  # fmt: skip


def mahr_profiles(measurement):
    files = {f.role: f for f in measurement.files.all()}
    return tuple(parse_mahr(files[role].original_name, file_content(files[role]))
                 for role in (Role.MAHR_LEFT, Role.MAHR_RIGHT))  # fmt: skip


def _apply_mahr(analysis, result):
    s = result.settings
    analysis.od, analysis.id, analysis.diameter_unit, analysis.light_band_um = s.od, s.id, s.units, s.light_band_um
    analysis.left_direction, analysis.right_direction = s.left_direction, s.right_direction
    analysis.left_start_mm, analysis.left_end_mm = result.left.start, result.left.end
    analysis.right_start_mm, analysis.right_end_mm = result.right.start, result.right.end
    analysis.pv_view, analysis.skip_points, analysis.flat_limit_hlb = s.pv_view, s.skip_points, s.flat_limit_hlb
    analysis.face_width_mm = result.face_width_mm
    analysis.left_slope, analysis.right_slope = result.left.m, result.right.m
    analysis.common_slope = result.common_slope
    analysis.taper_um, analysis.taper_hlb, analysis.classification = (
        result.taper_um, result.taper_hlb, result.classification)  # fmt: skip
    analysis.left_pv_um = result.pv[s.pv_view]["left"]
    analysis.right_pv_um = result.pv[s.pv_view]["right"]
    analysis.left_ra_um = result.left.profile.reported.get("Ra")
    analysis.left_rz_um = result.left.profile.reported.get("Rz")
    analysis.right_ra_um = result.right.profile.reported.get("Ra")
    analysis.right_rz_um = result.right.profile.reported.get("Rz")
    analysis.detail = {
        "pv": result.pv,
        "points": {side: {"evaluated": fit.count, "total": fit.profile.count, "first": fit.first, "last": fit.last}
                   for side, fit in (("left", result.left), ("right", result.right))},  # fmt: skip
        "scan_length_mm": {"left": result.left.profile.length, "right": result.right.profile.length},
        "scan_longer_than_face": result.scan_longer_than_face,
        "warnings": result.warnings,
    }


def mahr_metadata(profile):
    return {"headers": profile.headers, "reported": profile.reported, "count": profile.count,
            "warnings": list(profile.warnings)}  # fmt: skip


def create_mahr(component, stage, left_upload, right_upload, user, od=None, id_=None, units=None):
    """A Draft Mahr measurement from the left and right TXT exports, calculated with the default settings."""
    ensure_editable(component.result)
    ensure_applicable(component, MeasurementKind.MAHR)
    (lname, lcontent), (rname, rcontent) = read_upload(left_upload), read_upload(right_upload)
    left, right = parse_mahr(lname, lcontent), parse_mahr(rname, rcontent)
    chosen = default_mahr_settings(component, left, right, od, id_, units)
    try:
        result = mahr.calculate(left, right, chosen)
    except CalculationError as error:
        raise MeasurementError(str(error)) from error

    def build(files, written):
        when = f"{left.headers.get('DATE', '')} {left.headers.get('TIME', '')}".strip()
        measurement = _new_measurement(
            component, stage, MeasurementKind.MAHR, user, engine=facemeasure.MAHR_ENGINE,
            measured_on_text=when, measured_at=_aware(_parse_datetime(when, ["%d.%m.%Y %H:%M", "%d.%m.%Y"])),
        )  # fmt: skip
        _store_files(measurement, files, user, written)
        analysis = MahrAnalysis(measurement=measurement)
        _apply_mahr(analysis, result)
        analysis.save()
        _touch(component.result, user)
        return measurement

    files = {Role.MAHR_LEFT: (lname, lcontent, mahr_metadata(left)),
             Role.MAHR_RIGHT: (rname, rcontent, mahr_metadata(right))}  # fmt: skip
    return _create(component, stage, MeasurementKind.MAHR, user, files, build)


def calculate_mahr(measurement, chosen=None):
    left, right = mahr_profiles(measurement)
    try:
        return mahr.calculate(left, right, chosen or mahr_settings(measurement.mahr))
    except CalculationError as error:
        raise MeasurementError(str(error)) from error


def update_mahr(measurement, chosen, user):
    ensure_draft(measurement)
    result = calculate_mahr(measurement, chosen)
    analysis = measurement.mahr
    with transaction.atomic():
        _apply_mahr(analysis, result)
        analysis.save()
        measurement.engine = facemeasure.MAHR_ENGINE
        measurement.updated_by = user
        measurement.save(update_fields=["engine", "updated_by", "updated_at"])
        _touch(measurement.component.result, user)
    return result


# ---- ROUNDPAK -------------------------------------------------------------------------------


def parse_roundpak(name, content):
    try:
        return roundpak.parse(facemeasure.decode(content), name)
    except InstrumentFileError as error:
        raise MeasurementError(f"{name} could not be read: {error}") from error


def default_roundpak_settings(component, od=None, id_=None, units=None):
    """Face dimensions when known (the CSV does not hold them); otherwise none: no radial taper."""
    od, id_, units = face_dimensions(component, od, id_, units)
    return roundpak.Settings(od=od, id=id_, units=units)


def roundpak_settings(analysis):
    return roundpak.Settings(
        od=analysis.od, id=analysis.id, units=analysis.diameter_unit, light_band_um=analysis.light_band_um,
        circle_scan=analysis.circle_scan or None, circle_source=analysis.circle_source,
        left_scan=analysis.left_scan or None, right_scan=analysis.right_scan or None,
        radial_source=analysis.radial_source,
        left_start=analysis.left_start_mm, left_end=analysis.left_end_mm,
        right_start=analysis.right_start_mm, right_end=analysis.right_end_mm,
        **({} if analysis.flat_limit_hlb is None else {"flat_limit_hlb": analysis.flat_limit_hlb}),
    )  # fmt: skip


def roundpak_file(measurement):
    stored = measurement.files.get(role=Role.ROUNDPAK)
    return parse_roundpak(stored.original_name, file_content(stored))


def _scan_summary(scan):
    return {"id": scan.id, "kind": scan.kind, "name": scan.name, "points": len(scan.points), "deleted": scan.flagged,
            "min": scan.min, "max": scan.max, "complete": scan.complete, "line": scan.line, "params": scan.params}


def _apply_roundpak(analysis, result, file):
    s = result.settings
    analysis.od, analysis.id, analysis.diameter_unit, analysis.light_band_um = s.od, s.id, s.units, s.light_band_um
    analysis.circle_scan, analysis.circle_source = s.circle_scan or "", s.circle_source
    analysis.left_scan, analysis.right_scan, analysis.radial_source = s.left_scan or "", s.right_scan or "", s.radial_source
    analysis.left_start_mm, analysis.left_end_mm = s.left_start, s.left_end
    analysis.right_start_mm, analysis.right_end_mm = s.right_start, s.right_end
    analysis.flat_limit_hlb = s.flat_limit_hlb
    meta = file.meta
    analysis.part_code = meta.get("title", "")[:200]
    analysis.description = meta.get("subtitle", "")[:300]
    analysis.operator = meta.get("operator", "")[:100]
    params = result.instrument
    analysis.flatness_um = result.reported_flatness_um
    analysis.tolerance_upper = params.get("Upper Tolerance Limit", "")[:30]
    analysis.tolerance_lower = params.get("Lower Tolerance Limit", "")[:30]
    analysis.check_result = params.get("Check Result", "")[:30]
    analysis.instrument = params
    analysis.lfd_pv_um = result.lfd.pv if result.lfd else None
    analysis.circular_radius = (result.circular.scan.line.get("X Position", "") if result.circular else "")[:30]
    analysis.full_revolution = bool(result.circular and result.circular.scan.complete)
    analysis.left_slope = result.left.m if result.left else None
    analysis.right_slope = result.right.m if result.right else None
    analysis.mean_slope, analysis.tilt_slope, analysis.separation_deg = (
        result.mean_slope, result.tilt_slope, result.separation_deg)  # fmt: skip
    analysis.taper_um, analysis.classification = result.taper_um, result.classification
    analysis.detail = {
        "scans": [_scan_summary(scan) for scan in file.scans],
        "lfd_points": {"used": len(result.lfd.used), "total": len(result.lfd.scan.points)} if result.lfd else None,
        "circular_source_pv": result.circular.pv if result.circular else None,
        "radial_pv": {"left": result.left.pv, "right": result.right.pv} if result.left else None,
        "harmonics": [{"k": k, "amplitude_um": amp, "phase_deg": phase} for k, amp, phase in result.harmonics],
    }


def create_roundpak(component, stage, upload, user, od=None, id_=None, units=None):
    """A Draft ROUNDPAK measurement from the CSV export, calculated with the reference tool's defaults."""
    ensure_editable(component.result)
    ensure_applicable(component, MeasurementKind.ROUNDPAK)
    name, content = read_upload(upload)
    file = parse_roundpak(name, content)
    try:
        result = roundpak.calculate(file, default_roundpak_settings(component, od, id_, units))
    except CalculationError as error:
        raise MeasurementError(str(error)) from error

    def build(files, written):
        when = file.meta.get("date", "")
        measurement = _new_measurement(
            component, stage, MeasurementKind.ROUNDPAK, user, engine=facemeasure.ROUNDPAK_ENGINE,
            measured_on_text=when[:60],
            measured_at=_aware(_parse_datetime(when, ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"])),
        )  # fmt: skip
        _store_files(measurement, files, user, written)
        analysis = RoundpakAnalysis(measurement=measurement)
        _apply_roundpak(analysis, result, file)
        analysis.save()
        _touch(component.result, user)
        return measurement

    metadata = {"meta": file.meta, "scans": len(file.scans)}
    return _create(component, stage, MeasurementKind.ROUNDPAK, user, {Role.ROUNDPAK: (name, content, metadata)}, build)


def calculate_roundpak(measurement, chosen=None, file=None):
    file = file or roundpak_file(measurement)
    try:
        return roundpak.calculate(file, chosen or roundpak_settings(measurement.roundpak))
    except CalculationError as error:
        raise MeasurementError(str(error)) from error


def update_roundpak(measurement, chosen, user):
    ensure_draft(measurement)
    file = roundpak_file(measurement)
    result = calculate_roundpak(measurement, chosen, file)
    analysis = measurement.roundpak
    with transaction.atomic():
        _apply_roundpak(analysis, result, file)
        analysis.save()
        measurement.engine = facemeasure.ROUNDPAK_ENGINE
        measurement.updated_by = user
        measurement.save(update_fields=["engine", "updated_by", "updated_at"])
        _touch(measurement.component.result, user)
    return result


# ---- dimensional ----------------------------------------------------------------------------


def create_dimensional(component, stage, value, user, instrument="", remark="", unit="in", feature=""):
    """A dimension as measured: the face length of a seal face, or a named feature of another part.
    Entered by the engineer, so reviewed and reported at once."""
    ensure_editable(component.result)
    ensure_applicable(component, MeasurementKind.DIMENSIONAL)
    if value is None:
        raise MeasurementError("Enter the measured value.")
    feature = feature.strip()
    if not component.is_face and not feature:
        raise MeasurementError(f"Name the measured feature of the {component.get_component_display().lower()}.")
    quantity = "face_length" if component.is_face else "dimension"
    with transaction.atomic():
        measurement = _new_measurement(component, stage, MeasurementKind.DIMENSIONAL, user, remark=remark.strip())
        DimensionalReading.objects.create(measurement=measurement, value=value, unit=unit, instrument=instrument.strip(),
                                          quantity=quantity, feature=feature if not component.is_face else "")  # fmt: skip
        _review(measurement, user)
        _touch(component.result, user)
    return measurement


# ---- review and reporting -------------------------------------------------------------------


def calculate(measurement):
    """The live calculation of a Mahr or ROUNDPAK measurement from its files and stored settings."""
    if measurement.kind == MeasurementKind.MAHR:
        return calculate_mahr(measurement)
    if measurement.kind == MeasurementKind.ROUNDPAK:
        return calculate_roundpak(measurement)
    return None


def figure_svgs(measurement, result=None):
    """{code: SVG} drawn from the files and stored settings."""
    result = result or calculate(measurement)
    if measurement.kind == MeasurementKind.MAHR:
        return figures.mahr_figures(result)
    if measurement.kind == MeasurementKind.ROUNDPAK:
        return figures.roundpak_figures(result)
    return {}


# Display order of the figures (as drawn for a Draft); unknown codes last.
FIGURE_ORDER = ["mahr_left", "mahr_right", "mahr_gauge", "roundpak_circular", "roundpak_polar", "roundpak_harmonics",
                "roundpak_radial_taper", "roundpak_radial_left", "roundpak_radial_right", "roundpak_gauge"]  # fmt: skip


def _figure_rank(figure):
    return FIGURE_ORDER.index(figure.code) if figure.code in FIGURE_ORDER else len(FIGURE_ORDER)


def stored_figures(measurement):
    """{code: SVG} of the figures kept at review (empty for a Draft), in display order."""
    found = {}
    for figure in sorted(measurement.figures.all(), key=_figure_rank):
        with figure.file.open("rb") as stored:
            found[figure.code] = stored.read().decode("utf-8")
    return found


def _store_figures(measurement, written):
    storage = _storage()
    engine = measurement.engine
    base = f"{measurement_dir(measurement.component.result.test_run)}/figures"
    for code, svg in figure_svgs(measurement).items():
        content = svg.encode("utf-8")
        name = storage.save(f"{base}/{measurement.pk}-{code}.svg", ContentFile(content))
        written.append(name)
        MeasurementFigure.objects.create(measurement=measurement, code=code, file=name,
                                         sha256=hashlib.sha256(content).hexdigest(), engine=engine)  # fmt: skip


def _slot(measurement):
    return FaceMeasurement.objects.filter(component=measurement.component, stage=measurement.stage,
                                          kind=measurement.kind).exclude(pk=measurement.pk)  # fmt: skip


def _review(measurement, user):
    _slot(measurement).filter(reported=True).update(reported=False)
    measurement.status = FaceMeasurement.Status.REVIEWED
    measurement.reviewed_by, measurement.reviewed_at = user, timezone.now()
    measurement.reported = True
    measurement.updated_by = user
    measurement.save()


def review(measurement, user):
    """Accept the calculated result: figures are generated and kept; it becomes the slot's reported measurement."""
    ensure_draft(measurement)
    written = []
    try:
        with transaction.atomic():
            measurement.figures.all().delete()
            _store_figures(measurement, written)
            _review(measurement, user)
            _touch(measurement.component.result, user)
    except Exception:
        for name in written:
            _storage().delete(name)
        raise


def reopen(measurement, user):
    """Back to Draft for corrections: not reported any more, generated figures discarded."""
    ensure_editable(measurement.component.result)
    if not measurement.is_reviewed:
        return
    if measurement.kind == MeasurementKind.DIMENSIONAL:
        raise MeasurementError("A face length is not reopened: add the corrected value as a new measurement.")
    ensure_not_on_record(measurement, "reopened")
    names = [f.file.name for f in measurement.figures.all()]
    with transaction.atomic():
        measurement.figures.all().delete()
        measurement.status = FaceMeasurement.Status.DRAFT
        measurement.reviewed_by = measurement.reviewed_at = None
        measurement.reported = False
        measurement.updated_by = user
        measurement.save()
        _touch(measurement.component.result, user)
        storage = _storage()
        transaction.on_commit(lambda: [storage.delete(n) for n in names if storage.exists(n)])


def set_reported(measurement, user):
    """Use this reviewed measurement for its slot in the summary and report."""
    ensure_editable(measurement.component.result)
    if not measurement.is_reviewed:
        raise MeasurementError("Only a reviewed measurement can be reported.")
    with transaction.atomic():
        _slot(measurement).filter(reported=True).update(reported=False)
        measurement.reported = True
        measurement.updated_by = user
        measurement.save(update_fields=["reported", "updated_by", "updated_at"])
        _touch(measurement.component.result, user)


def set_remark(measurement, remark, user):
    """The engineer's remark on a measurement. Whether a report shows it is a report selection (M2F-B)."""
    ensure_editable(measurement.component.result)
    measurement.remark = remark.strip()[:300]
    measurement.updated_by = user
    measurement.save(update_fields=["remark", "updated_by", "updated_at"])
    _touch(measurement.component.result, user)


def delete_measurement(measurement, user):
    """Remove a measurement of a Draft Result with its files (evidence of a Draft may still be withdrawn)."""
    ensure_editable(measurement.component.result)
    ensure_not_on_record(measurement, "deleted")
    names = [f.file.name for f in measurement.files.all()] + [f.file.name for f in measurement.figures.all()]
    result = measurement.component.result
    with transaction.atomic():
        measurement.files.all().delete()
        measurement.delete()
        _touch(result, user)
        storage = _storage()
        transaction.on_commit(lambda: [storage.delete(n) for n in names if storage.exists(n)])

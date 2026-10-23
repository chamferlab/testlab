"""Test data of the Test Run Report (M2C): complete dual and single + Plug Seal runs."""

import io
import json
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from testlab.execution import datasets
from testlab.execution.tests.test_labview import NAME, export
from testlab.projects.models import ProgramRequirement, TestProgram
from testlab.results import images, plots, requirements, seal_faces, services
from testlab.results.facemeasure import mahr
from testlab.results.models import AcceptanceItem, ReportSelection, ResultImage
from testlab.results.tests.facemeasure_variants import (
    MAHR_LEFT, MAHR_RIGHT, ROUNDPAK_CSV, fixture_text, flagged_roundpak,
)
from testlab.results.tests.helpers import START, log, make_run


def picture(name, size=(1200, 800), color="steelblue", fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return SimpleUploadedFile(name, out.getvalue())


def png(size=(1200, 600)):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


def component(result, label):
    return next(c for c in result.components.all() if c.label == label)


def upload(name, text=None):
    return SimpleUploadedFile(name, (text if text is not None else fixture_text(name)).encode("utf-8"))


def not_applicable(item, *kinds):
    for kind in kinds or ("dimensional", "mahr", "roundpak"):
        setattr(item, f"{kind}_applicability", "not_applicable")
    item.save()


def seal_face_measurements(result, user):
    """IB Stator: every kind Pre and Post (instrument fixtures); OB Stator: Mahr Pre only (Post missing);
    the rotors (SiC) are not measured: not applicable."""
    ib_stator, ob_stator = component(result, "IB Stator"), component(result, "OB Stator")
    for label in ("IB Rotor", "OB Rotor"):
        not_applicable(component(result, label))
    not_applicable(ob_stator, "dimensional", "roundpak")
    ib_stator.face_od, ib_stator.face_id = Decimal("5.034"), Decimal("4.634")
    ib_stator.save()
    seal_faces.create_dimensional(ib_stator, "pre", Decimal("1.25037"), user)
    seal_faces.create_dimensional(ib_stator, "post", Decimal("1.25025"), user)
    pre = seal_faces.create_mahr(ib_stator, "pre", upload(MAHR_LEFT), upload(MAHR_RIGHT), user)
    seal_faces.review(pre, user)
    post = seal_faces.create_mahr(ib_stator, "post", upload(MAHR_LEFT), upload(MAHR_RIGHT), user)
    seal_faces.update_mahr(post, mahr.Settings(od=5.034, id=4.634, left_start=1.0, left_end=8.0, right_start=1.0,
                                               right_end=8.0), user)  # fmt: skip
    seal_faces.review(post, user)
    pre = seal_faces.create_roundpak(ib_stator, "pre", upload("pre.csv", flagged_roundpak(fixture_text(ROUNDPAK_CSV))),
                                     user)  # fmt: skip
    seal_faces.review(pre, user)
    post = seal_faces.create_roundpak(ib_stator, "post", upload(ROUNDPAK_CSV), user)
    # Engineering report: the figures chosen for this slot (Pre and Post together).
    ReportSelection.objects.create(result=result, profile="engineering", updated_by=user, data={"slots": {
        f"{ib_stator.pk}:roundpak": {"figures": ["roundpak_circular", "roundpak_polar", "taper_gauge"]}}})
    seal_faces.review(post, user)
    ob_stator.face_od, ob_stator.face_id = Decimal("5.034"), Decimal("4.634")
    ob_stator.save()
    seal_faces.review(seal_faces.create_mahr(ob_stator, "pre", upload(MAHR_LEFT), upload(MAHR_RIGHT), user), user)


def dual_run(user, readings=6):
    """Dual pressurized run 001 of 2026RSD03TL with every kind of evidence."""
    run = make_run(user, "dual_pressurized", inboard=("C100603033 (SIC30)", "C100603035 (SL Wavy)"),
                   outboard=("C100603033 (SIC30)", "C100603038 (SL Wavy)"))  # fmt: skip
    project = run.project
    project.customer = "ADNOC"
    project.save()
    setup = run.current_revision().setup
    setup.test_unit, setup.duration, setup.speed = "B", "72", "2980"
    setup.seal_support_medium, setup.seal_support_pressure, setup.seal_support_inlet_temperature = "Demi water", "70", "60"
    setup.reference_drawing = "UHTW 4.750 cartridge"
    setup.run_description = "Run on water at site conditions.\n    Leakage collected per seal."
    setup.save()
    setup.positions.update(seal_type_size="UHTW 4.750")
    # Readings at 0, 1, 4, 9, 16, 25 h (unequal intervals): leakage volumes (ml) since the previous reading.
    log(run, user, 0, speed=0, seal_support_pressure="70.05", inboard_leakage=5, note="Start recording", category="start")
    for n in range(1, readings):
        log(run, user, n * n, speed=2980 + n, seal_support_pressure=f"{69 + n * 0.5}", inboard_face_temp=60 + n,
            inboard_leakage=10 * n, outboard_leakage=4 * n)  # fmt: skip
    # Speed read with the adjustment: the shaft ran on; no leakage was read (not measured, not 0).
    log(run, user, readings * readings, speed=2985, note="Cooling-water flow reduced", category="adjustment")
    program = TestProgram.objects.create(
        project=project, document_no="TP-2026-03", revision="B", title="Wavy-face qualification on water",
        objective="Demonstrate the leakage and wear performance of the wavy seal faces.", updated_by=user,
    )  # fmt: skip
    ProgramRequirement.objects.create(program=program, number=1, criterion="Leakage", basis="leakage_rate",
                                      comparison="<=", limit=Decimal("15"), unit="ml/h", updated_by=user)  # fmt: skip
    ProgramRequirement.objects.create(program=program, number=2, criterion="Test duration", basis="running_hours",
                                      comparison=">=", limit=Decimal("72"), unit="h", updated_by=user)  # fmt: skip
    result = services.create_result(run, user)
    result.objective_addition = "Run 001: first 24 h on demineralised water."
    result.save()
    ib_stator, ob_stator = component(result, "IB Stator"), component(result, "OB Stator")
    ib_stator.part_no, ib_stator.condition = "C100603033", "New"
    ib_stator.save()
    seal_face_measurements(result, user)
    images.add_image(result, picture("section.png", fmt="PNG", size=(1600, 700)), user,
                     ResultImage.Category.CROSS_SECTION, None, "Cross section of the new parts")  # fmt: skip
    images.add_image(result, picture("ib-rotor-pre.jpg"), user, ResultImage.Category.PRE_TEST,
                     component(result, "IB Rotor"), "IB rotor before")  # fmt: skip
    images.add_image(result, picture("ib-rotor-post.jpg", color="gray"), user, ResultImage.Category.POST_TEST,
                     component(result, "IB Rotor"), "IB rotor after")  # fmt: skip
    images.add_image(result, picture("ob-stator-post.jpg", size=(600, 900)), user, ResultImage.Category.POST_TEST,
                     ob_stator, "OB stator after")  # fmt: skip
    # Optional components: an inspected IB backing ring (thickness Pre/Post, a picture, an observation) and an
    # OB sleeve added but not measured (optional: no empty rows).
    ring = services.add_component(result, "inboard", "backing_ring", user)
    ring.part_no, ring.observations = "BR-4750", "Light fretting marks at the anti-rotation pin."
    ring.save()
    seal_faces.create_dimensional(ring, "pre", Decimal("3.502"), user, unit="mm", feature="Thickness")
    seal_faces.create_dimensional(ring, "post", Decimal("3.498"), user, unit="mm", feature="Thickness")
    images.add_image(result, picture("ib-ring-post.jpg"), user, ResultImage.Category.POST_TEST, ring,
                     "IB backing ring after")  # fmt: skip
    services.add_component(result, "outboard", "sleeve", user)
    dataset = datasets.import_dataset(run, SimpleUploadedFile(NAME, export()), user)
    dataset.recording_started_at = START  # t = 0 of the run: the first reading
    dataset.save()
    definition = json.dumps({"signals": [{"column": 1, "axis": "y"}], "time_range": None, "caption": "Barrier inlet temperature"})
    plots.add_plot(result, dataset, definition, png(), user)
    definition = json.dumps({"signals": [{"column": 3, "axis": "y"}], "time_range": None, "caption": "Motor speed"})
    plots.add_plot(result, dataset, definition, png(), user)
    requirements.sync_requirements(result, user)  # Program P1 (each seal) and P2, provisional: the TCN is a draft
    for order, (criterion, basis, fields) in enumerate([
        ("IB stator wear", "wear_rate", {"component": ib_stator, "comparison": "<=", "limit": Decimal("0.0000025"), "unit": "in/h"}),
        ("Face condition", "manual", {"requirement_text": "No damage", "actual_text": "No damage seen", "assessment": "pass",
                                      "remark": "Visual inspection."}),
    ], start=10):  # fmt: skip
        AcceptanceItem.objects.create(result=result, order=order, criterion=criterion, basis=basis, updated_by=user, **fields)
    result.observations = "Stable for the first 24 h.\n\nLeakage steady."
    result.conclusion = "The wavy faces performed acceptably."
    result.recommendation = ""
    result.save()
    return run


def single_plug_run(user, project_no="2025RSD02TL", readings=4):
    """Single seal with Plug Seal, every parameter logged: the manual-log table needs more than one table."""
    run = make_run(user, "single", plug_seal=True, project_no=project_no,
                   test=("SiC", "Carbon"), plug=("SiC", "SiC"))  # fmt: skip
    setup = run.current_revision().setup
    setup.process_medium, setup.process_pressure, setup.speed = "Crude oil", "40", "3600"
    setup.save()
    codes = ["speed", "torque", "process_pressure", "process_inlet_temp", "process_outlet_temp", "process_flow",
             "seal_support_pressure", "seal_support_inlet_temp", "seal_support_outlet_temp", "seal_support_flow",
             "plug_face_temp", "test_face_temp"]  # fmt: skip
    for n in range(readings):
        values = {code: 10 + n for code in codes}
        log(run, user, n, plug_leakage=3 + n, test_leakage=1 + n, **values)
    result = services.create_result(run, user)
    services.add_component(result, "plug", "rotor", user)
    result.mahr_applicability = result.roundpak_applicability = "not_applicable"
    result.save()
    not_applicable(component(result, "Test Stator"), "dimensional")
    test_rotor = component(result, "Test Rotor")  # carbon: face length measured
    seal_faces.create_dimensional(test_rotor, "pre", Decimal("0.5"), user)
    seal_faces.create_dimensional(test_rotor, "post", Decimal("0.4999"), user)
    images.add_image(result, picture("plug-rotor-post.jpg"), user, ResultImage.Category.POST_TEST,
                     component(result, "Plug Rotor"))  # fmt: skip
    return run


DCS_HEADER = "10/01/26\t09:00:00 AM\tPROJECT - Customer\tPROJECT - TCN001\t\t\t\n"
DCS_NAMES = "Time\tMotor Speed\tMotor Torque\t(X12) PT Barrier\t(T04) LBI\n"
DCS_UNITS = "Hrs\tRPM\tNm\tBarG\tT [C]\n"
DCS_NAME = "PROJECT - TCN001 Thu, Oct 01, 2026 09 00 AM"


def dcs_running_export(step_s=10):
    """01 Oct 09:00 → 04 Oct 16:00 (79 h): start-up 09:15, a 20 s dip, stop 02 Oct 10:00, restart 11:00,
    stop 04 Oct 15:00; barrier pressure and temperature also recorded before and after (not operating values)."""
    lines = []
    start_up, stop, restart, end_run, end = 0.25 * 3600, 25 * 3600, 26 * 3600, 78 * 3600, 79 * 3600
    ramp = [45, 1500, 2950]
    for i in range(int(end / step_s) + 1):
        t = i * step_s
        running_since = next((t - s for s, e in ((start_up, stop), (restart, end_run)) if s <= t < e), None)
        if running_since is None:
            speed = [0.1, -4.8, 2.9, -0.2][i % 4]  # noise while stopped
        elif running_since < 3 * step_s:
            speed = ramp[int(running_since // step_s)]
        else:
            speed = 2985 + (i % 7) * 4 - 12
        if 30 * 3600 <= t < 30 * 3600 + 20:
            speed = 0.0  # brief dropout of the signal, shaft running on
        rotating = speed >= 50 or 30 * 3600 <= t < 30 * 3600 + 20
        pressure = 29.832 + (i % 50) * 0.0236 if rotating else 0.412
        temperature = (21.033 + min(running_since or 0, 6 * 3600) / (6 * 3600) * 35.377) if rotating else 18.5
        torque = 47.682 + (i % 9) * 0.31 if rotating else 0.007
        lines.append(f"{t / 3600:.6f}\t{speed:.3f}\t{torque:.3f}\t{pressure:.3f}\t{temperature:.3f}\n")
    return (DCS_HEADER + DCS_NAMES + DCS_UNITS + "".join(lines)).encode("ascii")


def dcs_running_run(user, confirm=False, project_no="2026RSD07TL"):
    """A dual run with a 79 h LabVIEW recording mapped and its running intervals detected (or confirmed)."""
    from datetime import datetime

    from django.utils import timezone

    from testlab.execution import manual_log, running_intervals, signal_mapping

    run = make_run(user, "dual_pressurized", project_no=project_no)
    setup = run.current_revision().setup
    setup.test_unit, setup.duration, setup.speed = "A", "72", "3000"
    setup.seal_support_medium, setup.seal_support_pressure = "Demi water", "30"
    setup.seal_support_inlet_temperature, setup.seal_support_flow = "55", "2"
    setup.save()
    setup.positions.update(seal_type_size="UHTW 4.750")
    dataset = datasets.import_dataset(run, SimpleUploadedFile(DCS_NAME, dcs_running_export()), user)
    c = {s["name"]: s["column"] for s in dataset.value_signals()}
    signal_mapping.review_mapping(dataset, {c["Motor Speed"]: "speed", c["Motor Torque"]: "torque",
                                            c["(X12) PT Barrier"]: "seal_support_pressure",
                                            c["(T04) LBI"]: "seal_support_inlet_temp"}, {c["(T04) LBI"]}, user)  # fmt: skip
    running_intervals.ensure_current(dataset, user)
    dataset.refresh_from_db()
    if confirm:
        running_intervals.confirm(dataset, user, running_intervals.current(dataset).pk)
    origin = timezone.make_aware(datetime(2026, 10, 1, 9, 0))
    columns = manual_log.log_columns(run)
    for hours, values, category, note in [
        (0.5, {"seal_support_flow": "15.0", "inboard_leakage": "0", "outboard_leakage": "0"}, "", ""),
        (12, {"seal_support_flow": "15.2", "inboard_leakage": "14", "outboard_leakage": "6"}, "", ""),
        (24.5, {"seal_support_flow": "14.9", "inboard_leakage": "16", "outboard_leakage": "7"}, "", ""),
        (25, {}, "stop", "Stopped for a coupling check"),
        (26, {}, "start", "Restarted"),
        (50, {"seal_support_flow": "15.1", "inboard_leakage": "33", "outboard_leakage": "15"}, "", ""),
        (77.5, {"seal_support_flow": "15.0", "inboard_leakage": "38", "outboard_leakage": "16"}, "", ""),
    ]:  # fmt: skip
        cleaned = {"observed_at": origin + timedelta(hours=hours), "note": note, "category": category,
                   "leakage_hours": None, **{k: Decimal(v) for k, v in values.items()}}  # fmt: skip
        manual_log.save_entry(run, cleaned, columns, user)
    result = services.create_result(run, user)
    result.conclusion = "Example of DCS running intervals."
    result.save()
    return run

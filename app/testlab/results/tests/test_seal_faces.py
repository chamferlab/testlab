"""Seal-face measurements of a Result (M2D): evidence, review, reporting, summary, completeness."""

import hashlib
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from testlab.results import measurement_summary, seal_faces, services
from testlab.results.facemeasure import mahr, roundpak
from testlab.results.models import FaceMeasurement, MeasurementFile

from .facemeasure_variants import MAHR_LEFT, MAHR_RIGHT, ROUNDPAK_CSV, bad_mahr, fixture_text, flagged_roundpak
from .helpers import make_run

pytestmark = pytest.mark.django_db


def upload(name, text=None):
    return SimpleUploadedFile(name, (fixture_text(name) if text is None else text).encode("utf-8"))


@pytest.fixture
def result(user):
    return services.create_result(make_run(user, "dual_pressurized"), user)


def component(result, label):
    found = next(c for c in result.components.all() if c.label == label)
    found.face_od, found.face_id = Decimal("5.034"), Decimal("4.634")
    found.save()
    return found


def add_mahr(component, user, stage="pre"):
    return seal_faces.create_mahr(component, stage, upload(MAHR_LEFT), upload(MAHR_RIGHT), user)


def add_roundpak(component, user, stage="pre", text=None):
    return seal_faces.create_roundpak(component, stage, upload(ROUNDPAK_CSV, text), user)


def stored_bytes(measurement_file):
    with measurement_file.file.open("rb") as f:
        return f.read()


# --- evidence ----------------------------------------------------------------------------------


def test_mahr_files_are_kept_byte_for_byte_and_results_are_the_engine_s(result, user):
    stator = component(result, "IB Stator")
    m = add_mahr(stator, user)
    files = {f.role: f for f in m.files.all()}
    for role, name in (("mahr_left", MAHR_LEFT), ("mahr_right", MAHR_RIGHT)):
        content = fixture_text(name).encode("utf-8")
        assert stored_bytes(files[role]) == content and files[role].original_name == name
        assert files[role].sha256 == hashlib.sha256(content).hexdigest() and files[role].size == len(content)
    assert files["mahr_left"].metadata["headers"]["PICKUP"] == "BFW-250"
    expected = mahr.calculate(mahr.parse(fixture_text(MAHR_LEFT)), mahr.parse(fixture_text(MAHR_RIGHT)),
                              mahr.Settings(od=5.034, id=4.634, left_end=9.0, right_end=9.0))  # fmt: skip
    a = m.mahr
    assert (a.taper_um, a.taper_hlb, a.classification) == (expected.taper_um, expected.taper_hlb, expected.classification)
    assert (a.left_ra_um, a.right_ra_um, a.left_rz_um) == (0.035, 0.033, 0.55)  # as exported
    assert a.mean_ra_um == pytest.approx(0.034) and a.left_pv_um == expected.pv["common"]["left"]
    assert (a.skip_points, a.flat_limit_hlb, a.pv_view, a.classification) == (5, 0.25, "common", "Flat")  # v5 defaults
    assert a.detail["points"]["left"]["first"] == 5 and a.detail["warnings"] == []
    assert m.engine == "mahr-2" and m.measured_on_text == "24.09.2025 07:15"
    assert m.status == "draft" and not m.reported and not m.figures.exists()


def test_roundpak_keeps_the_instrument_result_apart_from_the_calculated_values(result, user):
    m = add_roundpak(component(result, "IB Stator"), user, text=flagged_roundpak(fixture_text(ROUNDPAK_CSV)))
    a = m.roundpak
    assert (a.flatness_um, a.check_result, a.tolerance_upper, a.tolerance_lower) == (0.296, "Pass", "0.580µm", "-0.580µm")
    assert a.instrument["Peak(FLTp)"] == "0.137µm" and a.part_code == "C000000000 - SIC30"
    assert a.lfd_pv_um == pytest.approx(0.2955) and a.lfd_pv_um != a.flatness_um  # deleted points excluded
    assert a.full_revolution and a.circular_radius == "62.00 mm"
    assert a.flatness_hlb == pytest.approx(0.296 / 0.29)
    # radial taper (v5): mean slope × radial width, classified against ± 0.25 HLB; tilt cancels
    assert a.taper_um == pytest.approx(a.mean_slope * (5.034 - 4.634) / 2 * 25.4)
    assert a.taper_hlb == pytest.approx(a.taper_um / 0.29) and a.classification == "Concave" and a.flat_limit_hlb == 0.25
    assert a.tilt_slope == pytest.approx((a.right_slope - a.left_slope) / 2) and m.engine == "roundpak-2"
    assert [h["k"] for h in a.strongest_harmonics] == [3, 2, 4] and len(a.detail["harmonics"]) == 30


def test_unreadable_file_is_refused_with_its_name_and_nothing_is_kept(result, user, file_storage):
    stator = component(result, "IB Stator")
    bad = next(b["text"] for b in bad_mahr(fixture_text(MAHR_LEFT)) if b["label"] == "zero_res")
    with pytest.raises(seal_faces.MeasurementError, match=r"^bad\.TXT could not be read: Header is missing the resolution"):
        seal_faces.create_mahr(stator, "pre", upload("bad.TXT", bad), upload(MAHR_RIGHT), user)
    assert not FaceMeasurement.objects.exists() and not list(file_storage.rglob("*.TXT"))


def test_a_file_v5_reads_with_a_warning_is_kept_with_the_warning(result, user):
    """A point count that differs from CNT is read as v5 reads it; the warning stays with the measurement."""
    stator = component(result, "IB Stator")
    short = next(b["text"] for b in bad_mahr(fixture_text(MAHR_LEFT)) if b["label"] == "count_mismatch")
    m = seal_faces.create_mahr(stator, "pre", upload("short.TXT", short), upload(MAHR_RIGHT), user)
    assert m.mahr.detail["warnings"] == ["short.TXT: Header says 18000 points, file holds 17999."]
    assert m.files.get(role="mahr_left").metadata["warnings"] == ["Header says 18000 points, file holds 17999."]


def test_settings_that_allow_no_result_are_refused(result, user):
    rotor = next(c for c in result.components.all() if c.label == "IB Rotor")  # no face dimensions entered
    with pytest.raises(seal_faces.MeasurementError, match="Enter OD > ID"):
        add_mahr(rotor, user)
    m = add_roundpak(rotor, user)  # ROUNDPAK: face dimensions optional, no rise estimate
    assert m.roundpak.taper_um is None and m.roundpak.classification == "" and m.roundpak.od is None


# --- draft → reviewed → reported ---------------------------------------------------------------


def test_draft_is_recalculated_and_review_keeps_figures_and_reports_it(result, user):
    stator = component(result, "IB Stator")
    m = add_mahr(stator, user)
    assert measurement_summary.slot_status(stator, "mahr", "pre") == measurement_summary.IN_REVIEW
    seal_faces.update_mahr(m, mahr.Settings(od=5.034, id=4.634, left_start=1, left_end=8, right_start=1, right_end=8,
                                            pv_view="residual", skip_points=0, flat_limit_hlb=0.1), user)  # fmt: skip
    m.refresh_from_db()
    assert (m.mahr.left_start_mm, m.mahr.pv_view, m.mahr.skip_points, m.mahr.flat_limit_hlb) == (1, "residual", 0, 0.1)
    assert m.mahr.left_pv_um == m.mahr.detail["pv"]["residual"]["left"]
    seal_faces.review(m, user)
    m.refresh_from_db()
    assert m.status == "reviewed" and m.reported and m.reviewed_by == user
    figures = {f.code: f for f in m.figures.all()}
    assert set(figures) == {"mahr_left", "mahr_right", "mahr_gauge"}
    svg = stored_bytes(figures["mahr_left"])
    assert svg.startswith(b"<svg") and figures["mahr_left"].sha256 == hashlib.sha256(svg).hexdigest()
    assert seal_faces.figure_svgs(m)["mahr_left"].encode() == svg  # regenerated identically from the evidence
    assert measurement_summary.slot_status(stator, "mahr", "pre") == measurement_summary.COMPLETED
    with pytest.raises(seal_faces.MeasurementError, match="reopen it first"):
        seal_faces.update_mahr(m, seal_faces.mahr_settings(m.mahr), user)


def test_roundpak_v5_figures_are_kept_at_review_and_offered_to_the_report(result, user):
    from testlab.documents.run_report_context import _roundpak_detail

    stator = component(result, "IB Stator")
    m = add_roundpak(stator, user)
    seal_faces.review(m, user)
    figures = {f.code: stored_bytes(f).decode() for f in m.figures.all()}
    assert set(figures) == {"roundpak_circular", "roundpak_polar", "roundpak_harmonics", "roundpak_radial_taper",
                            "roundpak_radial_left", "roundpak_radial_right", "roundpak_gauge"}  # fmt: skip
    assert "L radial 269.9°" in figures["roundpak_circular"]  # where the radial traces were taken
    assert "Concave" in figures["roundpak_gauge"] and "Flat band ± 0.25 HLB" in figures["roundpak_gauge"]
    assert "3 UPR: 0.0834 µm" in figures["roundpak_harmonics"]
    block = _roundpak_detail(stator, None, m, {"taper_gauge", "roundpak_radial_taper", "roundpak_harmonics"})
    assert [f["code"] for f in block["figures"]] == ["roundpak_harmonics", "roundpak_radial_taper", "taper_gauge"]
    assert all(set(f["grid"][0]) == {"post"} for f in block["figures"])


def test_a_roundpak_1_result_keeps_its_unclassified_rise(result, user):
    """Stored results are never reclassified: an earlier result shows its taper as not classified."""
    stator = component(result, "IB Stator")
    m = add_roundpak(stator, user)
    seal_faces.review(m, user)
    FaceMeasurement.objects.filter(pk=m.pk).update(engine="roundpak-1")
    type(m.roundpak).objects.filter(pk=m.pk).update(classification="", flat_limit_hlb=None, detail={})
    (group,) = measurement_summary.summary([stator])
    value = next(r for r in group.rows if r.quantity.code == "face_taper_roundpak").pre
    assert value.value is not None and value.classification == "" and value.note == "not classified (roundpak-1)"


def test_newest_review_supersedes_and_the_engineer_can_choose_again(result, user):
    stator = component(result, "IB Stator")
    first, second = add_roundpak(stator, user), add_roundpak(stator, user)
    seal_faces.review(first, user)
    seal_faces.review(second, user)
    first.refresh_from_db()
    assert not first.reported and FaceMeasurement.objects.get(reported=True) == second  # first stays on record
    seal_faces.set_reported(first, user)
    second.refresh_from_db()
    assert not second.reported and FaceMeasurement.objects.get(reported=True) == first
    with pytest.raises(IntegrityError), transaction.atomic():
        FaceMeasurement.objects.filter(pk=second.pk).update(reported=True)  # one reported per slot


def test_reopen_takes_it_out_of_the_summary_and_discards_generated_figures(
    result, user, file_storage, django_capture_on_commit_callbacks
):
    stator = component(result, "IB Stator")
    m = add_mahr(stator, user)
    seal_faces.review(m, user)
    with django_capture_on_commit_callbacks(execute=True):
        seal_faces.reopen(m, user)
    m.refresh_from_db()
    assert m.status == "draft" and not m.reported and m.reviewed_at is None and not m.figures.exists()
    assert not list(file_storage.rglob("*.svg"))
    assert measurement_summary.slot_status(stator, "mahr", "pre") == measurement_summary.IN_REVIEW
    with pytest.raises(seal_faces.MeasurementError, match="Only a reviewed"):
        seal_faces.set_reported(m, user)


def test_a_draft_cannot_be_reported_or_unstamped(result, user):
    m = add_roundpak(component(result, "IB Stator"), user)
    with pytest.raises(IntegrityError), transaction.atomic():
        FaceMeasurement.objects.filter(pk=m.pk).update(reported=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        FaceMeasurement.objects.filter(pk=m.pk).update(status="reviewed")  # without who and when


def test_face_length_is_reviewed_at_entry_and_corrected_by_a_new_reading(result, user):
    stator = component(result, "OB Stator")
    first = seal_faces.create_dimensional(stator, "pre", Decimal("1.25037"), user)
    corrected = seal_faces.create_dimensional(stator, "pre", Decimal("1.25036"), user, remark="re-measured")
    first.refresh_from_db()
    assert corrected.reported and not first.reported and first.is_reviewed
    with pytest.raises(seal_faces.MeasurementError, match="not reopened"):
        seal_faces.reopen(corrected, user)


def test_delete_removes_the_record_and_its_files(result, user, file_storage, django_capture_on_commit_callbacks):
    m = add_mahr(component(result, "IB Stator"), user)
    seal_faces.review(m, user)
    with django_capture_on_commit_callbacks(execute=True):
        seal_faces.delete_measurement(m, user)
    assert not FaceMeasurement.objects.exists() and not MeasurementFile.objects.exists()
    assert not [p for p in file_storage.rglob("*") if p.is_file()]


def test_identical_files_are_noticed_not_refused(result, user):
    stator = component(result, "IB Stator")
    add_mahr(stator, user)
    post = add_mahr(stator, user, stage="post")
    warnings = seal_faces.duplicate_warnings(post)
    assert len(warnings) == 2 and "identical to JRO-003L.TXT of IB Stator Mahr profile / roughness Pre-test" in warnings[0]
    same = seal_faces.create_mahr(stator, "post", upload(MAHR_LEFT), upload(MAHR_LEFT), user)
    assert "The left and right files are identical" in seal_faces.duplicate_warnings(same)[0]


def test_nothing_changes_once_the_result_is_no_longer_a_draft(result, user):
    m = add_roundpak(component(result, "IB Stator"), user)
    result.status = "final"  # finalization is not built; the rule is already in place
    m.component.result = result
    for action in (lambda: seal_faces.review(m, user), lambda: seal_faces.delete_measurement(m, user),
                   lambda: seal_faces.update_roundpak(m, roundpak.Settings(), user)):  # fmt: skip
        with pytest.raises(seal_faces.MeasurementError, match="is final"):
            action()


# --- applicability, summary, completeness ----------------------------------------------------


def test_not_applicable_is_not_missing_and_is_refused_for_new_measurements(result, user):
    rotor = component(result, "IB Rotor")
    rotor.dimensional_applicability = "not_applicable"  # SiC face: no wear, no length
    rotor.save()
    with pytest.raises(seal_faces.MeasurementError, match="not applicable to IB Rotor"):
        seal_faces.create_dimensional(rotor, "pre", Decimal("1"), user)
    assert measurement_summary.slot_status(rotor, "dimensional", "pre") == measurement_summary.NOT_APPLICABLE
    assert measurement_summary.slot_status(rotor, "mahr", "pre") == measurement_summary.MISSING
    (group,) = [g for g in measurement_summary.summary(services.sorted_components(result)) if g.component == rotor]
    assert [row.quantity.code for row in group.rows] == ["face_flatness", "face_taper", "face_taper_roundpak",
                                                         "roughness_ra"]  # fmt: skip


def test_sleeve_and_plug_faces_only_when_the_engineer_says_so(user):
    result = services.create_result(make_run(user, "dual_unpressurized", plug_seal=True), user)
    sleeve = services.add_component(result, "", "sleeve", user)
    plug = services.add_component(result, "plug", "rotor", user)
    assert sleeve.applicable_kinds() == ["dimensional"] and plug.applicable_kinds() == ["dimensional", "mahr", "roundpak"]
    assert sleeve.applicability("dimensional") == "optional"  # never missing
    plug.mahr_applicability = plug.roundpak_applicability = plug.dimensional_applicability = "not_applicable"
    plug.save()
    sleeve.dimensional_applicability = "required"
    sleeve.mahr_applicability = "optional"
    sleeve.save()
    assert plug.applicable_kinds() == [] and sleeve.applicable_kinds() == ["dimensional", "mahr"]


def test_summary_values_come_from_the_reported_measurements(result, user):
    stator = component(result, "IB Stator")
    for stage in ("pre", "post"):
        seal_faces.review(add_roundpak(stator, user, stage), user)
        seal_faces.review(add_mahr(stator, user, stage), user)
    seal_faces.create_dimensional(stator, "pre", Decimal("1.25037"), user)
    seal_faces.create_dimensional(stator, "post", Decimal("1.25025"), user)
    (group,) = [g for g in measurement_summary.summary([stator])]
    rows = {row.quantity.code: row for row in group.rows}
    assert rows["face_flatness"].pre.value == 0.296 and rows["face_flatness"].source == "ROUNDPAK"
    assert rows["face_taper"].pre.classification == "Flat" and rows["face_taper"].source == "Mahr"
    taper = rows["face_taper_roundpak"]  # both tapers are shown; ROUNDPAK from the radial traces
    assert taper.source == "ROUNDPAK" and taper.pre.classification == "Concave" and taper.pre.value > 0
    assert rows["roughness_ra"].post.value == pytest.approx(0.034)
    assert rows["face_length"].delta == Decimal("-0.00012") and rows["face_length"].wear == Decimal("0.00012")
    assert all(row.complete for row in group.rows)
    assert rows["face_flatness"].delta == 0 and rows["face_taper"].wear is None


def test_completeness_distinguishes_completed_in_review_missing_and_not_applicable(result, user):
    result.roundpak_applicability = "not_applicable"
    result.save()
    stator = component(result, "IB Stator")
    seal_faces.review(add_mahr(stator, user), user)
    add_mahr(stator, user, "post")
    found = measurement_summary.completeness(services.sorted_components(result))
    # 4 faces × (face length + Mahr) × Pre/Post; ROUNDPAK not applicable: no slots
    assert len(found.slots) == 16
    assert found.counts == {"completed": 1, "in_review": 1, "missing": 14, "optional_measured": 0}
    assert not found.complete and all(kind != "roundpak" for _, kind, _, _ in found.slots)

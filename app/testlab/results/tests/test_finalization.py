"""Draft → Final Test Result and Test Run Report (M2E)."""

import hashlib
from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse

from testlab.documents import pdf
from testlab.documents.render import ChromiumTemplate
from testlab.documents.tests.pdf_text import pdf_lines
from testlab.execution.models import ManualLogEntry
from testlab.projects.models import ProgramRequirement, TestProgram
from testlab.results import finalization, publication, records, requirements, seal_faces, services
from testlab.results.models import AcceptanceItem, Assessment, FinalResultError, ReportDocument, TestResult

from .helpers import face_length, log, make_run

pytestmark = pytest.mark.django_db


def complete_result(user, assessment=Assessment.PASS):
    """A run whose Result has everything finalization needs: a dual seal, face length only."""
    run = make_run(user, "dual_pressurized")
    log(run, user, 0, speed=2980, inboard_leakage=0, outboard_leakage=0)
    log(run, user, 24, speed=2980, inboard_leakage=12, outboard_leakage=6)
    result = services.create_result(run, user)
    result.mahr_applicability = result.roundpak_applicability = "not_applicable"
    result.conclusion = "The faces performed as expected."
    result.save()
    for label in ("IB Stator", "IB Rotor", "OB Stator", "OB Rotor"):
        face_length(result, user, label, "1.25037", "1.25025")
    AcceptanceItem.objects.create(result=result, criterion="Leakage", basis="leakage_rate", position="inboard",
                                  comparison="<=", limit=Decimal("10"), unit="ml/h", updated_by=user,
                                  assessment=assessment)  # fmt: skip
    return run, TestResult.objects.get(pk=result.pk)


def issues_of(result, user):
    issues, _, _ = finalization.check(result, user)
    return issues


# --- what blocks finalization ---------------------------------------------------------------


def test_complete_result_has_no_blocking_issue(user):
    _, result = complete_result(user)
    assert issues_of(result, user) == []


def test_missing_required_measurement_assessment_and_conclusion_block_with_links(user):
    run = make_run(user, "dual_pressurized")
    result = services.create_result(run, user)
    result.mahr_applicability = result.roundpak_applicability = "not_applicable"
    result.save()
    AcceptanceItem.objects.create(result=result, criterion="Face condition", requirement_text="No damage",
                                  updated_by=user)  # fmt: skip
    issues = issues_of(result, user)
    anchors = {issue.anchor for issue in issues}
    stator = next(c for c in result.components.all() if c.label == "IB Stator")
    assert f"component-{stator.pk}" in anchors and "acceptance" in anchors and "conclusion" in anchors
    assert any("IB Stator: Face length (dimensional) Pre-test is required and missing" in i.message for i in issues)
    assert any("“Face condition” is not assessed" in i.message for i in issues)


def test_a_draft_measurement_is_not_yet_reported(user):
    run, result = complete_result(user)
    stator = next(c for c in result.components.all() if c.label == "IB Stator")
    stator.mahr_applicability = "required"
    stator.save()
    issues = issues_of(result, user)
    assert len([i for i in issues if "Mahr" in i.message]) == 2  # Pre and Post missing


def test_optional_measurements_and_components_never_block(user):
    run, result = complete_result(user)
    sleeve = services.add_component(result, "inboard", "sleeve", user)  # optional dimensional, not measured
    stator = next(c for c in result.components.all() if c.label == "IB Stator")
    stator.mahr_applicability = "optional"
    stator.save()
    assert sleeve.applicability("dimensional") == "optional"
    assert issues_of(result, user) == []


@pytest.mark.parametrize("assessment", [Assessment.NOT_EVALUATED, Assessment.NOT_APPLICABLE, Assessment.FAIL])
def test_explicit_assessments_and_a_failed_test_can_be_finalized(user, assessment):
    _, result = complete_result(user, assessment=assessment)
    assert issues_of(result, user) == []
    final = finalization.finalize(result, user)
    assert final.is_final


def test_not_assessed_requirement_from_the_program_blocks(user):
    run, result = complete_result(user)
    program = TestProgram.objects.create(project=run.project, updated_by=user)
    ProgramRequirement.objects.create(program=program, number=1, criterion="Test duration", basis="running_hours",
                                      comparison=">=", limit=Decimal("100"), unit="h", updated_by=user)  # fmt: skip
    assert not result.acceptance_items.filter(criterion="Test duration").exists()  # taken at set moments only
    requirements.sync_requirements(result, user)  # as when the Result is created, provisional: the TCN is a draft
    # 24 running h of 24 elapsed, no unknown period: the shortfall is calculated, so assessed (Fail)
    assert issues_of(result, user) == []
    duration = result.acceptance_items.get(criterion="Test duration")
    assert duration.source == "Test Program P1 (TCN not issued yet)"
    ProgramRequirement.objects.create(program=program, number=2, criterion="Noise", basis="manual",
                                      requirement_text="No abnormal noise", updated_by=user)  # fmt: skip
    requirements.sync_requirements(result, user)
    assert [i.anchor for i in issues_of(result, user)] == ["acceptance"]


# --- finalizing --------------------------------------------------------------------------------


def engineering(result):
    return ReportDocument.objects.get(revision__result=result, profile="engineering", status="issued")


def stored(result):
    with engineering(result).file.open("rb") as f:
        return f.read()


def test_finalize_freezes_snapshot_stores_the_pdf_with_its_hash_and_who(user, file_storage):
    run, result = complete_result(user)
    final = finalization.finalize(result, user)
    content = stored(final)
    assert final.status == "final" and final.finalized_by == user and final.finalized_at is not None
    # M2F-B: the Engineering document R0-E of the frozen revision 0
    (revision,) = final.revisions.all()
    (document,) = revision.documents.all()
    assert document.file.name == "projects/2026RSD03TL/runs/001/result/reports/2026RSD03TL-Run001-TRR-R0-E.pdf"
    assert (revision.number, revision.record_schema, document.profile, document.status) == (0, 2, "engineering", "issued")
    assert (revision.reason, revision.opened_at, revision.opened_by) == ("", None, None)
    assert document.sha256 == hashlib.sha256(content).hexdigest()
    assert document.template_sha256 == ChromiumTemplate.load("run_report", document="report.html").sha256
    snapshot = records.read_record(revision)
    assert snapshot["is_draft"] is False and snapshot["report"]["finalized"]["by"] == "Maria Geerts"
    assert snapshot["acceptance"][0]["requirement_source"] == "entered on the Test Result"
    text = " ".join(pdf_lines(content))
    assert "DRAFT" not in text and "PREVIEW" not in text and "Issued" in text and "finalized by Maria Geerts" in text
    assert "2026RSD03TL-Run001-TRR-R0-E" in text


def test_download_serves_the_stored_pdf_never_a_new_one(client, user, monkeypatch):
    run, result = complete_result(user)
    final = finalization.finalize(result, user)
    original = stored(final)

    def no_rendering(*args, **kwargs):
        raise AssertionError("the final report must not be rendered again")

    monkeypatch.setattr(ChromiumTemplate, "render_pdf", no_rendering)
    TestProgram.objects.create(project=run.project, title="Changed later", updated_by=user)  # live data changes
    client.force_login(user)
    response = client.get(reverse("results:report_final", args=[run.pk]))
    assert response.status_code == 200 and response.content == original
    assert response["Content-Disposition"] == 'attachment; filename="2026RSD03TL-Run001-TRR-R0-E.pdf"'
    monkeypatch.undo()
    # A preview once final is rendered from the frozen record, not from the changed live data.
    preview = client.get(reverse("results:report_profile_preview", args=[run.pk, "customer"]))
    text = " ".join(pdf_lines(preview.content))
    assert preview.status_code == 200 and "PREVIEW" in text and "Changed later" not in text


def test_rendering_failure_leaves_the_result_a_draft_and_no_file(user, file_storage, monkeypatch):
    _, result = complete_result(user)

    def failing(self, context, assets=None):
        raise pdf.PdfConversionError("Chromium could not print the PDF: font missing")

    monkeypatch.setattr(ChromiumTemplate, "render_pdf", failing)
    with pytest.raises(finalization.FinalizationError, match="font missing"):
        finalization.finalize(result, user)
    result.refresh_from_db()
    assert result.status == "draft" and not result.revisions.exists() and not ReportDocument.objects.exists()
    assert not list(file_storage.rglob("*TestRunReport*.pdf"))


def test_blocking_issues_refuse_finalization_and_change_nothing(user):
    _, result = complete_result(user)
    result.conclusion = ""
    result.save()
    with pytest.raises(finalization.FinalizationError) as refused:
        finalization.finalize(result, user)
    assert [i.anchor for i in refused.value.issues] == ["conclusion"]
    assert TestResult.objects.get(pk=result.pk).status == "draft"


def test_duplicate_finalization_is_refused(user):
    _, result = complete_result(user)
    stale = TestResult.objects.get(pk=result.pk)
    finalization.finalize(result, user)
    with pytest.raises(finalization.FinalizationError, match="already final"):
        finalization.finalize(stale, user)
    assert TestResult.objects.get(pk=result.pk).revisions.count() == 1


def test_authorization_is_enforced_on_the_server(client, user, monkeypatch):
    run, result = complete_result(user)
    monkeypatch.setattr("testlab.results.finalization.can_finalize_result", lambda u: False)
    monkeypatch.setattr("testlab.results.views.can_finalize_result", lambda u: False)
    client.force_login(user)
    response = client.post(reverse("results:finalize", args=[run.pk]))
    assert response.status_code == 302 and TestResult.objects.get(pk=result.pk).status == "draft"
    with pytest.raises(finalization.FinalizationError, match="not allowed"):
        finalization.finalize(result, user)


def test_finalize_through_the_page(client, user):
    run, result = complete_result(user)
    client.force_login(user)
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Finalize Report" in page and "Everything required is in place." in page
    response = client.post(reverse("results:finalize", args=[run.pk]))
    assert response.status_code == 302 and response["Location"].endswith("#report")
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Download Engineering Report" in page and "is final" in page


def test_customer_report_is_downloaded_once_issued(client, user):
    run, result = complete_result(user)
    final = finalization.finalize(result, user)
    client.force_login(user)
    url = reverse("results:report_final_profile", args=[run.pk, "customer"])
    assert client.get(url).status_code == 404
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Issue Customer Report" in page and url not in page
    document = publication.issue(final.revisions.get(), "customer", user)
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Download Customer Report" in page and url in page
    response = client.get(url)
    assert response.status_code == 200 and response.content == publication.document_bytes(document)
    assert response["Content-Disposition"] == 'attachment; filename="2026RSD03TL-Run001-TRR-R0-C.pdf"'
    assert client.get(reverse("results:report_final_profile", args=[run.pk, "other"])).status_code == 404


# --- a final Result cannot change ---------------------------------------------------------------


@pytest.fixture
def final(user):
    run, result = complete_result(user)
    return run, finalization.finalize(result, user)


def test_pages_refuse_changes_of_a_final_result_and_its_evidence(client, user, final):
    run, result = final
    client.force_login(user)
    narrative = client.post(reverse("results:narrative", args=[run.pk]),
                            {"observations": "", "conclusion": "Changed", "recommendation": ""})  # fmt: skip
    assert narrative.status_code == 302
    assert TestResult.objects.get(pk=result.pk).conclusion == "The faces performed as expected."
    entry = ManualLogEntry.objects.filter(test_run=run).first()
    client.post(reverse("execution:entry_delete", args=[entry.pk]))
    client.post(reverse("execution:log", args=[run.pk]), {"observed_at": "2026-06-30 10:00", "speed": "1"})
    assert ManualLogEntry.objects.filter(test_run=run).count() == 2
    item = result.acceptance_items.get()
    client.post(reverse("results:acceptance_edit", args=[item.pk]), {"delete": "1"})
    assert AcceptanceItem.objects.filter(pk=item.pk).exists()


def test_models_refuse_changes_of_a_final_result(user, final):
    run, result = final
    for change in (
        lambda: TestResult.objects.get(pk=result.pk).save(),
        lambda: result.acceptance_items.get().delete(),
        lambda: AcceptanceItem.objects.create(result=result, criterion="x", updated_by=user),
        lambda: seal_faces.create_dimensional(result.components.first(), "pre", Decimal(1), user),
        lambda: ManualLogEntry.objects.filter(test_run=run).first().delete(),
    ):
        with pytest.raises((FinalResultError, seal_faces.MeasurementError)), transaction.atomic():
            change()


def test_requirements_are_not_synced_into_a_final_result(user, final):
    run, result = final
    program = TestProgram.objects.create(project=run.project, updated_by=user)
    ProgramRequirement.objects.create(program=program, number=1, criterion="New", requirement_text="x",
                                      updated_by=user)  # fmt: skip
    assert requirements.sync_requirements(TestResult.objects.get(pk=result.pk), user) == []
    assert result.acceptance_items.count() == 1

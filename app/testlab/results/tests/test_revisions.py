"""Revisions of a final Result (M2F-B2).

A final Result is corrected by opening the next revision with a reason: a Draft again, finalized as
revision N+1 with its own frozen record and Engineering document. Earlier revisions and their PDFs never
change; the run's execution evidence can be corrected with the rest of the Result, each revision keeping
its own copy; what was on record in a final revision is never deleted, only superseded (a new
measurement) or left out of a report.
"""

from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse

from testlab.documents.tests.pdf_text import pdf_lines
from testlab.documents.tests.report_data import dual_run, picture
from testlab.execution.models import ManualLogEntry
from testlab.execution.views import recording_refusal, result_refusal
from testlab.results import finalization, images, plots, publication, records, revisions, seal_faces
from testlab.results.models import FaceMeasurement, FinalResultError, ReportDocument, ResultImage, TestResult

from .test_finalization import complete_result
from .test_report_publication import make_finalizable, read

pytestmark = pytest.mark.django_db
REASON = "IB Rotor post-test face length was read from the wrong gauge"


@pytest.fixture
def final(user):
    run, result = complete_result(user)
    return run, finalization.finalize(result, user)


@pytest.fixture
def revising(user, final):
    run, result = final
    return run, revisions.open_revision(result, user, REASON)


def text_of(document):
    return " ".join(" ".join(pdf_lines(read(document.file.name))).split())


# ---- opening a revision ------------------------------------------------------------------------------


def test_open_revision_needs_a_final_result_a_reason_and_permission(user, final, monkeypatch):
    run, result = final
    with pytest.raises(revisions.RevisionError, match="Give the reason"):
        revisions.open_revision(result, user, "  ")
    monkeypatch.setattr("testlab.results.revisions.can_revise_result", lambda u: False)
    with pytest.raises(revisions.RevisionError, match="not allowed"):
        revisions.open_revision(result, user, REASON)
    monkeypatch.undo()
    assert TestResult.objects.get(pk=result.pk).is_final
    opened = revisions.open_revision(result, user, REASON)
    assert (opened.status, opened.is_revising, opened.revision_reason) == ("draft", True, REASON)
    assert opened.revision_opened_by == user and opened.finalized_at is None
    assert revisions.working_number(opened) == 1
    with pytest.raises(revisions.RevisionError, match="revision in progress"):
        revisions.open_revision(opened, user, "again")


def test_a_draft_that_was_never_final_is_not_revised(user):
    _, result = complete_result(user)
    with pytest.raises(revisions.RevisionError, match="Only a final Result"):
        revisions.open_revision(result, user, REASON)
    assert revisions.working_number(result) == 0 and not result.is_revising


def test_opening_changes_no_record_and_no_document(user, final):
    run, result = final
    revision = result.revisions.get()
    document = revision.documents.get()
    record, pdf = records.read_record(revision), read(document.file.name)
    revisions.open_revision(result, user, REASON)
    revision.refresh_from_db()
    document.refresh_from_db()
    assert records.read_record(revision) == record and read(document.file.name) == pdf
    assert document.status == "issued"  # still the controlled publication until revision 1 is final


# ---- a revision in progress ---------------------------------------------------------------------------


def test_the_result_and_the_execution_evidence_can_change(client, user, revising):
    run, result = revising
    assert result_refusal(run) == "" and recording_refusal(run) == ""
    client.force_login(user)
    client.post(reverse("results:narrative", args=[run.pk]),
                {"observations": "", "conclusion": "Corrected conclusion.", "recommendation": ""})  # fmt: skip
    assert TestResult.objects.get(pk=result.pk).conclusion == "Corrected conclusion."
    count = ManualLogEntry.objects.filter(test_run=run).count()
    entry = ManualLogEntry.objects.filter(test_run=run).first()
    client.post(reverse("execution:entry_delete", args=[entry.pk]))
    client.post(reverse("execution:log", args=[run.pk]), {"observed_at": "2026-06-30 10:00", "speed": "1"})
    assert not ManualLogEntry.objects.filter(pk=entry.pk).exists()
    assert ManualLogEntry.objects.filter(test_run=run).count() == count


def test_corrected_evidence_is_in_the_next_revision_only_and_locked_again(user, revising):
    run, result = revising
    before = records.read_record(result.revisions.get(number=0))
    ManualLogEntry.objects.create(test_run=run, observed_at=run.manual_log_entries.last().observed_at,
                                  note="Gauge re-read after the run", entered_by=user, updated_by=user)  # fmt: skip
    again = finalization.finalize(TestResult.objects.get(pk=result.pk), user)
    assert records.read_record(again.revisions.get(number=0)) == before
    assert "Gauge re-read" not in str(before)
    assert "Gauge re-read" in str(records.read_record(again.revisions.get(number=1)))
    assert "is final" in recording_refusal(run)
    with pytest.raises(FinalResultError, match="manual log and DCS data cannot change"), transaction.atomic():
        ManualLogEntry.objects.filter(test_run=run).first().delete()


def test_a_measurement_on_record_is_superseded_not_deleted_or_reopened(user, revising):
    run, result = revising
    rotor = next(c for c in result.components.all() if c.label == "IB Rotor")
    old = rotor.face_measurements.get(stage="post", reported=True)
    with pytest.raises(seal_faces.MeasurementError, match="on record in final revision 0 and cannot be deleted"):
        seal_faces.delete_measurement(old, user)
    with pytest.raises(FinalResultError, match="on record in final revision 0"), transaction.atomic():
        old.delete()
    new = seal_faces.create_dimensional(rotor, "post", Decimal("1.25011"), user)
    old.refresh_from_db()
    assert FaceMeasurement.objects.get(pk=new.pk).reported and not old.reported  # superseded, still on record
    wrong = seal_faces.create_dimensional(rotor, "post", Decimal("9"), user)  # added in this revision: may go
    seal_faces.delete_measurement(wrong, user)
    seal_faces.set_reported(FaceMeasurement.objects.get(pk=new.pk), user)
    assert rotor.face_measurements.filter(stage="post").count() == 2


def test_issuing_waits_for_the_revision_to_be_final(client, user, revising):
    run, result = revising
    revision = result.revisions.get()
    with pytest.raises(publication.PublicationError, match="revision of the Result is in progress"):
        publication.issue(revision, "customer", user)
    client.force_login(user)
    client.post(reverse("results:report_issue", args=[run.pk, "customer"]))
    assert ReportDocument.objects.count() == 1
    preview = client.get(reverse("results:report_profile_preview", args=[run.pk, "engineering"]))
    text = " ".join(" ".join(pdf_lines(preview.content)).split())
    assert "DRAFT" in text and "Result revision 1 in progress" in text and "2026RSD03TL-Run001-TRR-R0-E" in text
    assert client.get(reverse("results:report_final", args=[run.pk])).status_code == 404
    contents = client.get(reverse("results:report_contents", args=[run.pk, "customer"])).content.decode()
    assert "revision 1 in progress" in contents and "Issue Customer report" not in contents


# ---- finalizing a revision ------------------------------------------------------------------------------


def test_finalizing_freezes_revision_1_and_supersedes_the_documents_of_revision_0(user, final):
    run, result = final
    first = result.revisions.get()
    r0_e = first.documents.get()
    r0_c = publication.issue(first, "customer", user)
    withdrawn = publication.issue(first, "customer", user, reason="Typo in the contents")
    publication.withdraw(withdrawn, user, "Sent to the wrong address")
    kept = {d.pk: read(d.file.name) for d in (r0_e, r0_c, withdrawn)}
    record_0 = records.read_record(first)

    opened = revisions.open_revision(result, user, REASON)
    opened.conclusion = "Corrected: the IB Rotor wore 0.26 mm."
    opened.save()
    again = finalization.finalize(opened, user)

    assert again.is_final and not again.is_revising and again.revision_reason == ""
    second = again.revisions.get(number=1)
    assert (second.reason, second.opened_by, second.finalized_by) == (REASON, user, user)
    assert second.record_file.name == "projects/2026RSD03TL/runs/001/result/records/r1/record.json.gz"
    assert records.read_record(second)["narrative"]["conclusion"] == [["Corrected: the IB Rotor wore 0.26 mm."]]
    assert records.read_record(first) == record_0  # revision 0 as it was frozen
    r1_e = second.documents.get()
    assert (r1_e.identity, r1_e.status, r1_e.origin) == ("2026RSD03TL-Run001-TRR-R1-E", "issued", "finalized")
    assert r1_e.reason == f"Result revision 1: {REASON}"
    for document in (r0_e, r0_c, withdrawn):
        document.refresh_from_db()
        assert read(document.file.name) == kept[document.pk]  # no PDF is overwritten
    assert (r0_e.status, r0_e.superseded_by) == ("superseded", r1_e)
    assert r0_e.status_reason == f"Replaced by Result revision 1: {REASON}"
    assert r0_c.status == "superseded"  # by its second issue, before the revision
    assert withdrawn.status == "withdrawn" and withdrawn.status_reason == "Sent to the wrong address"
    text = text_of(r1_e)
    assert "2026RSD03TL-Run001-TRR-R1-E" in text and "Result revision 1" in text and "DRAFT" not in text
    assert "2026RSD03TL-Run001-TRR-R0-E" in text and "Superseded" in text and REASON in text  # document history


def test_the_customer_document_of_the_new_revision_replaces_the_superseded_one(user, final):
    run, result = final
    first = result.revisions.get()
    r0_c = publication.issue(first, "customer", user)
    again = finalization.finalize(revisions.open_revision(result, user, REASON), user)
    r0_c.refresh_from_db()
    assert r0_c.status == "superseded" and r0_c.superseded_by is None  # no Customer document of revision 1 yet
    with pytest.raises(publication.PublicationError, match="not the latest final revision"):
        publication.issue(first, "customer", user, reason="late")
    r1_c = publication.issue(again.revisions.get(number=1), "customer", user)
    r0_c.refresh_from_db()
    assert r1_c.identity == "2026RSD03TL-Run001-TRR-R1-C" and r0_c.superseded_by == r1_c
    text = text_of(r1_c)
    assert "2026RSD03TL-Run001-TRR-R0-C" in text and REASON not in text  # Customer history: no reasons


def test_a_second_revision_follows_the_first(user, final):
    run, result = final
    for number in (1, 2):
        result = finalization.finalize(revisions.open_revision(result, user, f"Correction {number}"), user)
        assert revisions.working_number(result) == number
    assert [r.number for r in revisions.history(result)] == [0, 1, 2]
    issued = ReportDocument.objects.filter(status="issued")
    assert [d.identity for d in issued] == ["2026RSD03TL-Run001-TRR-R2-E"]


def test_a_failed_finalization_keeps_the_revision_open_and_the_documents_issued(user, revising, monkeypatch):
    run, result = revising
    monkeypatch.setattr("testlab.results.publication.render", lambda *a, **k: (_ for _ in ()).throw(
        finalization.PdfConversionError("printer on fire")))  # fmt: skip
    with pytest.raises(finalization.FinalizationError, match="printer on fire"):
        finalization.finalize(result, user)
    result.refresh_from_db()
    assert result.is_revising and result.revisions.count() == 1
    assert ReportDocument.objects.get().status == "issued"
    assert not records.storage().exists(f"{records.revision_dir(run, 1)}/record.json.gz")


# ---- pictures and plots on record --------------------------------------------------------------------------


def test_pictures_and_plots_on_record_stay_and_new_ones_may_go(client, user):
    run = dual_run(user)
    result = finalization.finalize(make_finalizable(run.result, user), user)
    result = revisions.open_revision(result, user, REASON)
    client.force_login(user)
    old_picture, old_plot = result.images.first(), result.plots.first()
    response = client.post(reverse("results:image_delete", args=[old_picture.pk]), follow=True)
    assert "on record in final revision 0 and cannot be deleted" in response.content.decode()
    response = client.post(reverse("results:plot_delete", args=[old_plot.pk]), follow=True)
    assert "Leave it out of the report instead" in response.content.decode()
    with pytest.raises(FinalResultError), transaction.atomic():
        plots.delete_plot(old_plot)
    assert result.images.filter(pk=old_picture.pk).exists() and result.plots.filter(pk=old_plot.pk).exists()
    added = images.add_image(result, picture("wrong.jpg"), user, ResultImage.Category.OTHER, None, "Wrong upload")
    client.post(reverse("results:image_delete", args=[added.pk]))
    assert not ResultImage.objects.filter(pk=added.pk).exists()
    again = finalization.finalize(result, user)  # revision 1 freezes its own copy of every asset
    second = again.revisions.get(number=1)
    assert second.assets and all(a["file"].startswith(f"{records.revision_dir(run, 1)}/assets/") for a in second.assets)


# ---- the pages ----------------------------------------------------------------------------------------------


def test_revise_and_finalize_through_the_pages(client, user, final):
    run, result = final
    client.force_login(user)
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Open revision 1" in page and "Result revisions" in page
    refused = client.post(reverse("results:revise", args=[run.pk]), {"reason": ""}, follow=True)
    assert "Give the reason" in refused.content.decode() and TestResult.objects.get(pk=result.pk).is_final
    response = client.post(reverse("results:revise", args=[run.pk]), {"reason": REASON})
    assert response.status_code == 302 and response["Location"].endswith("#report")
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Revision 1 in progress" in page and REASON in page and "Finalize revision 1" in page
    assert "Open revision" not in page and "Download Engineering Report" not in page
    client.post(reverse("results:finalize", args=[run.pk]))
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Final · revision 1" in page and "Open revision 2" in page and "2026RSD03TL-Run001-TRR-R1-E" in page
    final_pdf = client.get(reverse("results:report_final", args=[run.pk]))
    assert final_pdf["Content-Disposition"] == 'attachment; filename="2026RSD03TL-Run001-TRR-R1-E.pdf"'


def test_revising_needs_permission_on_the_page(client, user, final, monkeypatch):
    run, result = final
    monkeypatch.setattr("testlab.results.revisions.can_revise_result", lambda u: False)
    monkeypatch.setattr("testlab.results.views.can_revise_result", lambda u: False)
    client.force_login(user)
    assert "Open revision 1" not in client.get(reverse("results:result", args=[run.pk])).content.decode()
    client.post(reverse("results:revise", args=[run.pk]), {"reason": REASON})
    assert TestResult.objects.get(pk=result.pk).is_final

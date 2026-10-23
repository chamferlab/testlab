"""Final Report of a project (rough version): texts, included runs and a draft PDF from the frozen records."""

from decimal import Decimal

import pytest
from django.urls import reverse

from testlab.documents.tests.pdf_text import pdf_lines
from testlab.reports import assembly
from testlab.reports.models import FinalReport, FinalReportRun
from testlab.results import finalization, publication, revisions, services
from testlab.results.models import AcceptanceItem, ReportDocument, TestResult
from testlab.results.tests.helpers import configure, face_length, log
from testlab.results.tests.test_finalization import complete_result
from testlab.tcn.services import create_tcn

pytestmark = pytest.mark.django_db


def second_run(project, user, conclusion="Run two: no visible wear."):
    """Run 002 of the project, with a Result finalization accepts."""
    run = create_tcn(project, 2, user).test_run
    configure(run, "dual_pressurized")
    log(run, user, 0, speed=1500, inboard_leakage=0, outboard_leakage=0)
    log(run, user, 10, speed=1500, inboard_leakage=30, outboard_leakage=5)
    result = services.create_result(run, user)
    result.mahr_applicability = result.roundpak_applicability = "not_applicable"
    result.conclusion = conclusion
    result.save()
    for label in ("IB Stator", "IB Rotor", "OB Stator", "OB Rotor"):
        face_length(result, user, label, "1.25037", "1.25025")
    AcceptanceItem.objects.create(result=result, criterion="Leakage", basis="leakage_rate", position="inboard",
                                  comparison="<=", limit=Decimal("10"), unit="ml/h", updated_by=user)  # fmt: skip
    return run, TestResult.objects.get(pk=result.pk)


@pytest.fixture
def two_finals(user):
    run1, result1 = complete_result(user)
    result1 = finalization.finalize(result1, user)
    run2, result2 = second_run(run1.project, user)
    result2 = finalization.finalize(result2, user)
    return run1.project, result1, result2


def include(project, user, *results, **texts):
    report = FinalReport.objects.create(project=project, updated_by=user, **texts)
    for position, result in enumerate(results, start=1):
        FinalReportRun.objects.create(report=report, result=result, position=position)
    return report


def text_of(project):
    return " ".join(" ".join(pdf_lines(assembly.render_pdf(project))).split())


def test_only_results_with_a_final_revision_can_be_included(user):
    run, draft = complete_result(user)
    assert assembly.candidates(run.project) == []
    final = finalization.finalize(draft, user)
    assert [r.pk for r in assembly.candidates(run.project)] == [final.pk]
    revisions.open_revision(final, user, "Wrong gauge")  # a Draft again: its final revision 0 still counts
    (found,) = assembly.candidates(run.project)
    assert found.revision.number == 0


def test_page_saves_texts_and_runs_in_the_order_entered(client, user, two_finals):
    project, result1, result2 = two_finals
    client.force_login(user)
    url = reverse("reports:final_report", args=[project.pk])
    assert "Run 001" in client.get(url).content.decode()
    response = client.post(url, {
        "introduction": "Qualification of the wavy faces.", "conclusion": "Both runs passed.",
        f"include_{result1.pk}": "on", f"order_{result1.pk}": "2",
        f"include_{result2.pk}": "on", f"order_{result2.pk}": "1",
    })  # fmt: skip
    assert response.status_code == 302
    report = FinalReport.objects.get(project=project)
    assert (report.report_no, report.introduction, report.updated_by) == (f"{project.project_no}R",
                                                                         "Qualification of the wavy faces.", user)  # fmt: skip
    assert [r.pk for r in assembly.included(report)] == [result2.pk, result1.pk]
    client.post(url, {f"include_{result1.pk}": "on"})  # unticked: left out, nothing else lost
    assert [r.pk for r in assembly.included(FinalReport.objects.get(project=project))] == [result1.pk]
    assert TestResult.objects.filter(pk=result2.pk).exists()


def test_draft_pdf_has_the_texts_the_summary_table_and_a_section_per_run(user, two_finals):
    project, result1, result2 = two_finals
    include(project, user, result1, result2, introduction="Qualification of the wavy faces.\n\nSecond paragraph.",
            conclusion="Both runs are reported.")  # fmt: skip
    text = text_of(project)
    assert f"Report No.: {project.project_no}R" in text and "DRAFT" in text
    assert "1. Introduction Qualification of the wavy faces. Second paragraph." in text
    assert "3. Field Conditions Not written yet." in text
    assert "5.1 Run 001" in text and "5.2 Run 002" in text
    assert "5.2.3. Test Conditions & Results" in text  # the run's own numbering, under its section
    assert "The faces performed as expected." in text and "Run two: no visible wear." in text
    assert text.index("5.2 Run 002") < text.index("6. Conclusion Both runs are reported.")
    rows = [run["summary"] for run in assembly.build(project)[0]["runs"]]
    assert [(r["section"], r["run_no"], r["speed"], r["running_hours"]) for r in rows] == [
        ("5.1", "001", "2980", "24.00"), ("5.2", "002", "1500", "10.00")]  # fmt: skip
    assert rows[0]["leakage"] == ["Inboard Seal: 0.5", "Outboard Seal: 0.2"]  # 6 ml in 24 h, as the run report prints it
    assert rows[0]["acceptance"] == ["1 Pass"]


def test_report_follows_the_order_and_leaves_out_what_is_not_included(user, two_finals):
    project, result1, result2 = two_finals
    include(project, user, result2)
    text = text_of(project)
    assert "5.1 Run 002" in text and "Run 001" not in text


def test_run_section_comes_from_the_frozen_record_not_from_live_data(user, two_finals):
    project, result1, result2 = two_finals
    include(project, user, result1)
    revising = revisions.open_revision(result1, user, "Conclusion reworded")
    revising.conclusion = "Reworded, not final yet."
    revising.save()
    text = text_of(project)
    assert "The faces performed as expected." in text and "Reworded" not in text
    finalization.finalize(TestResult.objects.get(pk=result1.pk), user)
    assert "Reworded, not final yet." in text_of(project)  # the latest final revision


def test_run_section_names_the_issued_customer_document(user, two_finals):
    project, result1, _ = two_finals
    include(project, user, result1)
    assert "not an issued document" in text_of(project)
    document = publication.issue(result1.revisions.get(), "customer", user)
    text = text_of(project)
    assert document.identity in text
    assert ReportDocument.objects.count() == 3  # two Engineering documents and this one: the draft stores nothing


def test_preview_without_runs_explains_what_to_do(client, user, two_finals):
    project, _, _ = two_finals
    client.force_login(user)
    response = client.get(reverse("reports:preview", args=[project.pk]), follow=True)
    assert "No run is included yet" in response.content.decode()
    include(project, user, two_finals[1])
    response = client.get(reverse("reports:preview", args=[project.pk]))
    assert response["Content-Type"] == "application/pdf"
    assert "FinalReport-DRAFT.pdf" in response["Content-Disposition"]


def test_project_page_links_to_the_final_report(client, user, two_finals):
    project, _, _ = two_finals
    client.force_login(user)
    assert reverse("reports:final_report", args=[project.pk]) in client.get(
        reverse("projects:detail", args=[project.pk])).content.decode()  # fmt: skip


@pytest.mark.chromium
def test_real_pdf_starts_each_run_on_a_new_page(user, two_finals):
    import io

    import pypdf

    from testlab.documents.tests.pdf_text import is_real_pdf

    project, result1, result2 = two_finals
    include(project, user, result1, result2, conclusion="Both runs are reported.")
    content = assembly.render_pdf(project)
    assert is_real_pdf(content)
    pages = [" ".join(page.extract_text().split()) for page in pypdf.PdfReader(io.BytesIO(content)).pages]
    assert "1. Introduction" in pages[0] and "5.1 Run 001 (29-" not in pages[0]
    # The run headings carry the dates; the summary table and the list of sections name the runs too.
    starts = [next(n for n, page in enumerate(pages) if heading in page) for heading in ("5.1 Run 001 (29-", "5.2 Run 002 (29-")]
    assert 0 < starts[0] < starts[1]
    assert "6. Conclusion Both runs are reported." in pages[-1] and "5.2 Run 002 (29-" not in pages[-1]
    assert all(f"Report No.: {project.project_no}R" in page for page in pages)  # header on every page


def drawing_upload(name, size, kind="PNG", pages=1):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    out = io.BytesIO()
    images = [Image.new("RGB", size, "gray") for _ in range(pages)]
    images[0].save(out, kind, save_all=pages > 1, append_images=images[1:])
    return SimpleUploadedFile(name, out.getvalue())


def test_appendices_show_the_current_tester_drawing_and_pid(user, two_finals):
    import io

    from PIL import Image

    from testlab.projects import drawings

    project, result1, _ = two_finals
    include(project, user, result1)
    assert assembly.build(project)[0]["appendices"] == []
    drawings.add_drawing(project, "tester", drawing_upload("old.png", (100, 200)), user, "F001842744", "1")
    drawings.add_drawing(project, "tester", drawing_upload("tester.png", (300, 200)), user, "F001842744", "2")
    drawings.add_drawing(project, "pid", drawing_upload("pid.pdf", (300, 200), "PDF", pages=2), user, "P-100")
    context, assets, _ = assembly.build(project)
    tester, pid = context["appendices"]
    assert (tester["letter"], tester["title"], tester["drawing"]) == ("A", "Tester drawing", "F001842744 rev 2")
    assert (pid["letter"], pid["title"], pid["drawing"], len(pid["pages"])) == ("B", "System P&ID", "P-100", 2)
    for path in tester["pages"] + pid["pages"]:
        with Image.open(io.BytesIO(assets[path])) as page:
            assert page.height > page.width  # landscape pages are turned to fill the portrait page
    html = assembly.final_report_template().render_parts(context)[0]
    assert "Appendix A – Tester drawing F001842744 rev 2" in html
    assert "Appendix B – System P&amp;ID P-100 (page 2 of 2)" in html


@pytest.mark.chromium
def test_real_pdf_has_each_drawing_page_on_its_own_page(user, two_finals):
    import io

    import pypdf

    from testlab.projects import drawings

    project, result1, _ = two_finals
    include(project, user, result1, conclusion="Reported.")
    drawings.add_drawing(project, "tester", drawing_upload("tester.png", (300, 200)), user, "F001842744", "2")
    drawings.add_drawing(project, "pid", drawing_upload("pid.pdf", (300, 200), "PDF"), user, "P-100")
    pages = [" ".join(page.extract_text().split())
             for page in pypdf.PdfReader(io.BytesIO(assembly.render_pdf(project))).pages]  # fmt: skip
    assert "Appendix A – Tester drawing F001842744 rev 2" in pages[-2]
    assert "Appendix B – System P&ID P-100" in pages[-1]
    assert "Appendices" in pages[-3] and "6. Conclusion Reported." in pages[-3]


def test_unreadable_drawing_is_reported(user, two_finals):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from testlab.projects import drawings

    project, result1, _ = two_finals
    include(project, user, result1)
    drawings.add_drawing(project, "pid", SimpleUploadedFile("pid.pdf", b"%PDF-1.7 broken"), user, "P-100")
    with pytest.raises(assembly.AssemblyError, match="system p&id P-100 cannot be shown"):
        assembly.build(project)

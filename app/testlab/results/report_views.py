"""Report profiles, previews and issued documents of a Result (M2F-B).

    Report contents (per profile)   what the Customer / Engineering report shows; presentation only
    Preview                         a profile rendered now, never stored (DRAFT for a Draft, PREVIEW once final)
    Issue / withdraw                controlled documents of a final revision (publication.py)
    Download                        an issued document, byte for byte, checked against its SHA-256
"""

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from testlab.documents.pdf import PDF_CONTENT_TYPE, PdfConversionError
from testlab.documents.report_profiles import FLAGS, LOCKED, PROFILES
from testlab.execution.views import get_run, run_context
from testlab.tcn.permissions import can_issue_report

from . import evidence, publication, report_contents, revisions
from .models import RecordIntegrityError, ReportDocument, TestResult


def get_result(test_run):
    return get_object_or_404(TestResult.objects.select_related("test_run__project"), test_run=test_run)


def check_profile(profile):
    if profile not in PROFILES:
        raise Http404


def contents_url(test_run, profile, anchor=""):
    return reverse("results:report_contents", args=[test_run.pk, profile]) + (f"#{anchor}" if anchor else "")


class ReportContentsView(View):
    template_name = "results/report_contents.html"

    def get(self, request, run_pk, profile):
        check_profile(profile)
        test_run = get_run(run_pk)
        result = get_result(test_run)
        components = evidence.collect(test_run, result)["components"]
        found_slots, pictures, plots = report_contents.items(result, components)
        selection, stored = report_contents.current(result, profile, components)
        revision = publication.current_revision(result)
        documents = list(ReportDocument.objects.filter(revision__result=result).select_related(
            "revision", "issued_by", "status_changed_by").order_by("profile", "revision__number", "issue"))  # fmt: skip
        context = {
            **run_context(test_run, "result"), "result": result, "profile": profile, "profile_label": PROFILES[profile],
            "profiles": PROFILES, "flags": [(f, selection["flags"][f.key], f.default(profile)) for f in FLAGS],
            "locked": LOCKED, "slots": [(s, selection["slots"][s.key]) for s in found_slots],
            "pictures": [(p, selection["pictures"][str(p.pk)], report_contents.picture_label(p)) for p in pictures],
            "plots": [(p, selection["plots"][str(p.pk)]) for p in plots], "customized": bool(stored),
            "revision": revision, "legacy_record": revision is not None and revision.record_schema < 2,
            "revising": revisions.working_number(result) if result.is_revising else None,
            "documents": [d for d in documents if d.profile == profile],
            "issued_before": any(d.profile == profile and d.revision_id == getattr(revision, "pk", None)
                                 for d in documents),  # fmt: skip
            "can_issue": can_issue_report(request.user),
        }  # fmt: skip
        return render(request, self.template_name, context)

    def post(self, request, run_pk, profile):
        check_profile(profile)
        test_run = get_run(run_pk)
        result = get_result(test_run)
        components = evidence.collect(test_run, result)["components"]
        if request.POST.get("action") == "reset":
            report_contents.reset(result, profile)
            messages.success(request, f"{PROFILES[profile]} report: back to the profile defaults.")
        else:
            report_contents.save(result, profile, request.POST, components, request.user)
            messages.success(request, f"{PROFILES[profile]} report contents saved. Evidence is unchanged; "
                                      "issued documents are unchanged.")  # fmt: skip
        return redirect(contents_url(test_run, profile))


class ReportPreviewView(View):
    """A profile's report PDF, not issued: DRAFT from the live data of a Draft Result, PREVIEW from the frozen
    record once final. Never stored."""

    def get(self, request, run_pk, profile=None):
        profile = profile or request.GET.get("profile", "engineering")
        check_profile(profile)
        test_run = get_run(run_pk)
        try:
            content = publication.preview(test_run, profile, request.user)
        except (PdfConversionError, RecordIntegrityError, publication.PublicationError) as error:
            messages.error(request, f"The report preview could not be generated. {error}")
            return redirect("results:result", test_run.pk)
        final = TestResult.objects.filter(test_run=test_run, status=TestResult.Status.FINAL).exists()
        marking = "PREVIEW" if final else "DRAFT"
        name = f"{test_run.project.project_no}-Run{test_run.display_no}-TestRunReport-{PROFILES[profile]}-{marking}.pdf"
        response = HttpResponse(content, content_type=PDF_CONTENT_TYPE)
        response["Content-Disposition"] = f'inline; filename="{name}"'
        response["Cache-Control"] = "no-store"
        return response


class IssueDocumentView(View):
    def post(self, request, run_pk, profile):
        check_profile(profile)
        test_run = get_run(run_pk)
        result = get_result(test_run)
        revision = publication.current_revision(result)
        if revision is None:
            messages.error(request, "Documents are issued from a final Result: finalize it first.")
            return redirect(contents_url(test_run, profile))
        try:
            document = publication.issue(revision, profile, request.user, reason=request.POST.get("reason", ""))
        except (PdfConversionError, RecordIntegrityError, publication.PublicationError) as error:
            messages.error(request, f"Nothing was issued. {error}")
        else:
            messages.success(request, f"{document.identity} issued (SHA-256 {document.sha256[:12]}…). "
                                      "The evidence of the Result is unchanged.")  # fmt: skip
        return redirect(contents_url(test_run, profile, "documents"))


class WithdrawDocumentView(View):
    def post(self, request, pk):
        document = get_object_or_404(ReportDocument.objects.select_related("revision__result__test_run"), pk=pk)
        test_run = document.revision.result.test_run
        try:
            publication.withdraw(document, request.user, request.POST.get("reason", ""))
        except publication.PublicationError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"{document.identity} withdrawn. It stays stored and retrievable.")
        return redirect(contents_url(test_run, document.profile, "documents"))


def document_response(document):
    try:
        content = publication.document_bytes(document)
    except RecordIntegrityError as error:
        return HttpResponse(str(error), status=500, content_type="text/plain; charset=utf-8")
    response = HttpResponse(content, content_type=PDF_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{document.identity}.pdf"'
    return response


class DocumentDownloadView(View):
    """An issued (or superseded, withdrawn) document, byte for byte; never rendered again."""

    def get(self, request, pk):
        return document_response(get_object_or_404(ReportDocument, pk=pk))


class FinalReportView(View):
    """The issued document of a profile — Engineering by default — of the latest final revision
    (the Download Engineering / Customer Report links)."""

    def get(self, request, run_pk, profile="engineering"):
        check_profile(profile)
        test_run = get_run(run_pk)
        result = get_object_or_404(TestResult, test_run=test_run, status=TestResult.Status.FINAL)
        document = ReportDocument.objects.filter(revision=publication.latest_revision(result), profile=profile,
                                                 status=ReportDocument.Status.ISSUED).first()  # fmt: skip
        if document is None:
            raise Http404(f"No issued {PROFILES[profile]} document.")
        return document_response(document)


"""Controlled publication of Test Run Report documents (M2F-B).

Two kinds of controlled things, never confused:

    Result revision (data)       Draft → Final        the record: evidence, assessments, conclusion (records.py)
    Report document (publication) Preview → Issued    one rendering of one final revision with one profile

A Draft Preview is not a document (DRAFT or PREVIEW marking, never stored). Issuing renders the profile
from the frozen record of a final revision and its frozen assets — never from live data — and stores the
PDF once with its SHA-256 under the document identity

    <project>-Run<nnn>-TRR-R<revision>-<C|E>[-<issue>]          e.g. 2026RSD06TL-Run001-TRR-R0-C

Finalizing a revision issues its Engineering document (finalization.py). Issuing a profile again
(a changed selection) needs a reason; the earlier issue becomes Superseded. Withdrawn: explicitly
withdrawn from use, with a reason. No PDF is ever overwritten or deleted; issuing changes no evidence.

Revisions (M2F-B2, revisions.py): documents are issued from the latest final revision only, and not
while a later revision is in progress. Finalizing a revision supersedes the issued documents of the
earlier ones; the first document of a profile for the new revision carries the revision's reason and
becomes the successor of the superseded one.
"""

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from testlab.documents.render import run_report_template
from testlab.documents.report_profiles import PROFILE_CODES, PROFILES, present
from testlab.documents.run_report_context import _datetime, build_run_report_context, report_assets
from testlab.documents.tcn_context import format_date
from testlab.tcn.permissions import can_issue_report

from . import records
from .models import RecordIntegrityError, ReportDocument, ReportSelection, TestResult


class PublicationError(Exception):
    """A publication the rules do not allow; the message says why."""


def identity(test_run, revision_number, profile, issue):
    base = f"{test_run.project.project_no}-Run{test_run.display_no}-TRR-R{revision_number}-{PROFILE_CODES[profile]}"
    return base if issue == 1 else f"{base}-{issue}"


def document_path(test_run, ident):
    return f"{records.result_dir(test_run)}/reports/{ident}.pdf"


def stored_selection(result, profile):
    found = ReportSelection.objects.filter(result=result, profile=profile).first()
    return found.data if found else {}


def latest_revision(result):
    return result.revisions.order_by("-number").first() if result is not None else None


def current_revision(result):
    """The revision documents are issued and previewed from: the latest one of a final Result, else None."""
    return latest_revision(result) if result is not None and result.is_final else None


def _person(user):
    return (user.get_full_name() or user.username) if user else ""


def _history_row(doc, status=None):
    return {"identity": doc.identity, "revision": doc.revision.number, "issued_on": format_date(
        timezone.localtime(doc.issued_at)), "status": status or doc.get_status_display(),
        "issued_by": _person(doc.issued_by), "reason": doc.reason}  # fmt: skip


def history(result, profile, upcoming=None, superseding=None):
    """The issued documents of a profile, oldest first; `upcoming` (a row) is the one being issued now,
    which supersedes `superseding`."""
    rows = []
    for doc in ReportDocument.objects.filter(revision__result=result, profile=profile).select_related(
            "revision", "issued_by").order_by("revision__number", "issue"):  # fmt: skip
        rows.append(_history_row(doc, "Superseded" if superseding is not None and doc.pk == superseding.pk else None))
    if upcoming:
        rows.append(upcoming)
    return rows


def render(context, assets):
    template = run_report_template()  # read once: the PDF is rendered from what its hash describes
    return template.render_pdf(context, assets), template


def issue(revision, profile, user, reason="", origin="issued", written=None):
    """Issue the profile's document of a final revision; returns the ReportDocument.

    Called inside finalization's transaction for the Engineering document (origin "finalized");
    on its own otherwise. Files written are listed in `written` (removed by the caller on failure).
    """
    if profile not in PROFILES:
        raise PublicationError("Choose the Customer or Engineering report.")
    if not can_issue_report(user):
        raise PublicationError("You are not allowed to issue report documents.")
    reason = (reason or "").strip()
    own = written is None
    written = [] if own else written
    try:
        with transaction.atomic():
            result = TestResult.objects.select_for_update().get(pk=revision.result_id)
            test_run = result.test_run
            if revision.pk != latest_revision(result).pk:
                raise PublicationError(f"Revision {revision.number} is not the latest final revision of the Result: "
                                       "documents are issued from the latest one.")  # fmt: skip
            if origin != "finalized" and not result.is_final:
                raise PublicationError("A revision of the Result is in progress: documents are issued once it is "
                                       "final.")  # fmt: skip
            documents = ReportDocument.objects.select_for_update().filter(revision=revision, profile=profile)
            previous = documents.filter(status=ReportDocument.Status.ISSUED).first()
            number = (documents.aggregate(n=Max("issue"))["n"] or 0) + 1
            if number > 1 and not reason:
                raise PublicationError(f"The {PROFILES[profile]} report of revision {revision.number} was issued "
                                       "before: give the reason for issuing it again.")  # fmt: skip
            # The profile's document of an earlier revision this one replaces (superseded at finalization).
            replaced = None
            if number == 1 and revision.number > 0:
                reason = reason or f"Result revision {revision.number}: {revision.reason}"
                replaced = ReportDocument.objects.select_for_update().filter(
                    revision__result=result, revision__number__lt=revision.number, profile=profile,
                    status=ReportDocument.Status.SUPERSEDED, superseded_by__isnull=True,
                ).order_by("-revision__number", "-issue").first()  # fmt: skip
            record = records.read_record(revision)
            now = timezone.now()
            ident = identity(test_run, revision.number, profile, number)
            upcoming = {"identity": ident, "revision": revision.number, "issued_on": format_date(timezone.localtime(now)),
                        "status": "Issued", "issued_by": _person(user), "reason": reason}  # fmt: skip
            document = {
                "identity": ident, "revision": revision.number, "issue": number, "status": "Issued",
                "issued_on": format_date(timezone.localtime(now)), "issued_at": _datetime(now),
                "history": history(result, profile, upcoming, previous),
            }  # fmt: skip
            context = present(record, profile, stored_selection(result, profile), document=document)
            assets = records.read_assets(revision, {a["path"] for a in context["assets"]})
            content, template = render(context, assets)
            name = records._save_new(document_path(test_run, ident), content, written)
            if previous is not None:  # one issued document per revision and profile
                previous.status = ReportDocument.Status.SUPERSEDED
                previous.status_changed_at, previous.status_changed_by = now, user
                previous.status_reason = f"Replaced by {ident}: {reason}"
                previous.save()
            doc = ReportDocument.objects.create(
                revision=revision, profile=profile, issue=number, identity=ident, reason=reason,
                selection=context["selection"], presentation_sha256=records.sha256(records.canonical(context)),
                file=name, sha256=records.sha256(content), size=len(content), template_sha256=template.sha256,
                origin=origin, issued_at=now, issued_by=user,
            )  # fmt: skip
            for earlier in (previous, replaced):
                if earlier is not None:
                    earlier.superseded_by = doc
                    earlier.save()
            return doc
    except BaseException:
        if own:
            for name in written:
                records.storage().delete(name)
        raise


def withdraw(document, user, reason):
    """Withdraw an issued document from use; it stays stored and retrievable, marked Withdrawn."""
    if not can_issue_report(user):
        raise PublicationError("You are not allowed to withdraw report documents.")
    reason = (reason or "").strip()
    if not reason:
        raise PublicationError("Give the reason for withdrawing the document.")
    with transaction.atomic():
        document = ReportDocument.objects.select_for_update().get(pk=document.pk)
        if document.status != ReportDocument.Status.ISSUED:
            raise PublicationError(f"{document.identity} is {document.get_status_display().lower()}, not issued.")
        document.status = ReportDocument.Status.WITHDRAWN
        document.status_changed_at, document.status_changed_by, document.status_reason = timezone.now(), user, reason
        document.save()
    return document


def document_bytes(document):
    """The stored PDF of a document, byte for byte; refused when it does not match its SHA-256."""
    with document.file.open("rb") as stored:
        content = stored.read()
    if records.sha256(content) != document.sha256:
        raise RecordIntegrityError(f"{document.identity} does not match its SHA-256; it is not served.")
    return content


def preview(test_run, profile, user):
    """A profile's report, not issued: from the live data of a Draft — also of a revision in progress —
    (DRAFT), or from the frozen record of the latest final revision (PREVIEW). Never stored."""
    if profile not in PROFILES:
        raise PublicationError("Choose the Customer or Engineering report.")
    result = TestResult.objects.filter(test_run=test_run).first()
    revision = current_revision(result)
    if revision is not None:
        record = records.read_record(revision)
        context = present(record, profile, stored_selection(result, profile),
                          document={"revision": revision.number, "history": history(result, profile)})
        assets = records.read_assets(revision, {a["path"] for a in context["assets"]})
    else:
        record = build_run_report_context(test_run)
        document = None
        if result is not None and result.is_revising:  # the Draft of revision N: named so, with what is issued
            document = {"revision": latest_revision(result).number + 1, "history": history(result, profile)}
        context = present(record, profile, stored_selection(result, profile) if result else {}, document=document)
        assets = {path: content for path, content in report_assets(record).items()
                  if path in {a["path"] for a in context["assets"]}}  # fmt: skip
    content, _ = render(context, assets)
    return content

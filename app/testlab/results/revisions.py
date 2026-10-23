"""Revisions of a final Test Result (M2F-B2).

    Final, revision N  ──open_revision(reason)──►  Draft, revision N+1 in progress  ──finalize──►  Final, revision N+1

A final Result is never edited in place. A correction opens the next revision with a reason: the
Result is a Draft again and its measurements, pictures, plots, assessments, conclusion and report
details can change, and so can the run's execution evidence (manual log, DCS data, running
intervals, log imports); finalizing (finalization.py) freezes the complete record again as revision N+1
and issues its Engineering report.

What does not change, ever:

* earlier revisions: their frozen records, frozen assets and stored PDFs (models guards), which hold
  the execution evidence as it was when they were finalized;
* what was on record in a final revision: a picture, plot or measurement (with its instrument files)
  cannot be deleted and a reviewed measurement cannot be reopened. A measurement is superseded by a
  new reviewed one; a picture or plot is left out of a report (report contents).

Documents: those of the earlier revision stay Issued while the next revision is in progress — they
are still the controlled publication — and no new document is issued until it is final. Finalizing
revision N+1 supersedes every issued document of the earlier revisions (supersede_earlier); a
withdrawn document stays withdrawn. A revision in progress cannot be discarded: finalizing it
unchanged gives a revision whose record equals the earlier one.
"""

from django.db import transaction
from django.utils import timezone

from testlab.tcn.permissions import can_revise_result

from . import requirements
from .models import ReportDocument, ResultRevision, TestResult


class RevisionError(Exception):
    """A revision the rules do not allow; the message says why."""


def latest(result):
    """The latest final revision of the Result, or None."""
    return result.revisions.order_by("-number").first() if result is not None else None


def working_number(result):
    """The number of the revision the Result is (a Draft: will be finalized as; Final: is)."""
    found = latest(result)
    if found is None:
        return 0
    return found.number if result.is_final else found.number + 1


def open_revision(result, user, reason):
    """Open the next revision of a final Result; returns the Result, a Draft again."""
    if not can_revise_result(user):
        raise RevisionError("You are not allowed to revise Test Results.")
    reason = (reason or "").strip()
    if not reason:
        raise RevisionError("Give the reason for the revision: it is recorded with the revision and shown in the "
                            "document history of the Engineering report.")  # fmt: skip
    with transaction.atomic():
        result = TestResult.objects.select_for_update().get(pk=result.pk)
        if not result.is_final:
            raise RevisionError("Only a final Result is revised: this one is a Draft"
                                + (" with a revision in progress." if result.is_revising else "."))  # fmt: skip
        if latest(result) is None:
            raise RevisionError("The final Result has no frozen record; it cannot be revised.")
        now = timezone.now()
        # Compare-and-set, like finalizing: only a final Result is opened, once.
        opened = TestResult.objects.filter(pk=result.pk, status=TestResult.Status.FINAL).update(
            status=TestResult.Status.DRAFT, finalized_at=None, finalized_by=None, revision_reason=reason,
            revision_opened_at=now, revision_opened_by=user, updated_by=user, updated_at=now,
        )  # fmt: skip
        if opened != 1:
            raise RevisionError("The Result was changed by another request.")
        result = TestResult.objects.get(pk=result.pk)
        requirements.sync_requirements(result, user)  # a TCN issued since the last revision now applies
    return result


def supersede_earlier(revision, user, now):
    """The issued documents of the revisions before `revision` become Superseded (inside finalization)."""
    earlier = ReportDocument.objects.select_for_update().filter(
        revision__result_id=revision.result_id, revision__number__lt=revision.number,
        status=ReportDocument.Status.ISSUED)  # fmt: skip
    for document in earlier:
        document.status = ReportDocument.Status.SUPERSEDED
        document.status_changed_at, document.status_changed_by = now, user
        document.status_reason = f"Replaced by Result revision {revision.number}: {revision.reason}"
        document.save()


def history(result):
    """The final revisions of the Result, oldest first."""
    return list(ResultRevision.objects.filter(result=result).select_related("finalized_by", "opened_by")
                .order_by("number"))  # fmt: skip

"""Draft → Final Test Result and Test Run Report (M2E).

Final means complete and controlled, not passed: a failed test has a final
report too. A final Result stays final; a correction is a new revision
(revisions.py), finalized here in the same way as revision N.

Finalization checks (blocking_issues), each with a link to its section:

* every required measurement of every component is reported (optional
  ones may stay unavailable; not applicable ones are not expected);
* every acceptance item is assessed — by the engineer or calculated. An
  explicit Not evaluated or Not applicable counts; a missing one does not;
* the running intervals of every DCS recording that contributes to the
  running hours are confirmed or overridden by an engineer (not detected
  only, not stale); a Draft may use them provisionally;
* the engineer has written the conclusion;
* the report has a responsible engineer (Prepared by).

Then, in one transaction (finalize): the complete record context is built with the final marking
and frozen as Result revision N — 0 the first time, the next number for a
revision in progress (records.py: the record file and a copy of every
picture and plot it shows, each with its SHA-256); the issued documents of
the earlier revisions become Superseded; the Engineering report is issued
from that frozen record (publication.py: document R<N>-E, its PDF stored
once with its SHA-256 and the template's); and the Result becomes Final
with who and when. If anything fails — rendering
included — nothing is kept: the Result stays a Draft and every file
already written is removed. Documents are never rendered again from live
data; other profiles are issued later from the same frozen record.
"""

from dataclasses import dataclass

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from testlab.documents.pdf import PdfConversionError
from testlab.documents.report_profiles import ENGINEERING
from testlab.documents.run_report_context import build_run_report_context, report_assets
from testlab.tcn.permissions import can_finalize_result

from . import acceptance, evidence, measurement_summary, publication, records, requirements, revisions
from .inspection import Stage
from .models import RecordIntegrityError, ResultRevision, TestResult


class FinalizationError(Exception):
    def __init__(self, issues):
        self.issues = issues
        super().__init__(" ".join(issue.message for issue in issues))


@dataclass
class Issue:
    message: str
    anchor: str  # section of the Result page
    url: str = ""  # elsewhere (e.g. the DCS dataset), instead of the anchor


def blocking_issues(result, found, evaluations):
    issues = []
    if result.is_final:
        return [Issue("The Result is already final.", "summary")]
    for component, kind, stage, status in measurement_summary.completeness(found["components"]).slots:
        if status in (measurement_summary.MISSING, measurement_summary.IN_REVIEW):
            state = "not reviewed yet" if status == measurement_summary.IN_REVIEW else "missing"
            issues.append(Issue(
                f"{component.label}: {component.kind_label(kind)} {Stage(stage).label} is required and {state}.",
                f"component-{component.pk}",
            ))  # fmt: skip
    for e in evaluations:
        if e.missing:
            hint = f" ({e.note})" if e.note else ""
            issues.append(Issue(f"Acceptance “{e.item.criterion}” is not assessed{hint}: choose Pass, Fail, "
                                "Not evaluated or Not applicable.", "acceptance"))  # fmt: skip
    for found_state in found["timeline"].dcs.values():
        if found_state.provisional:
            dataset = found_state.dataset
            why = f"stale: {found_state.stale}" if found_state.stale else "detected only"
            issues.append(Issue(f"Running intervals of {dataset.original_name} are not confirmed ({why}): "
                                "confirm or edit them on the DCS dataset page.", "summary",
                                reverse("execution:dcs_detail", args=[dataset.pk]) + "#running"))  # fmt: skip
    if not result.conclusion.strip():
        issues.append(Issue("The conclusion has not been written.", "conclusion"))
    if result.responsible_engineer_id is None:
        issues.append(Issue("Choose the responsible engineer (Prepared by).", "report"))
    return issues


def notices(result, found, items):
    """Not blocking, shown before finalizing."""
    found_notices = []
    tcn = result.test_run.current_revision()
    if tcn and tcn.status != tcn.Status.ISSUED:
        found_notices.append(f"{tcn} is {tcn.get_status_display().lower()}: the report will name it so.")
    pending = requirements.pending_changes(result.test_run)
    if pending:
        found_notices.append(f"{len(pending)} acceptance requirement change(s) of a draft TCN revision are not in "
                             "force until that revision is issued.")  # fmt: skip
    for item in requirements.withdrawn(items):
        found_notices.append(f"“{item.criterion}”: {item.source}")
    open_imports = [b for b in result.test_run.log_imports.all() if b.is_open]
    if open_imports:
        names = ", ".join(b.original_name for b in open_imports)
        found_notices.append(f"Manual-log import not completed ({names}): it is not in the log and can no longer "
                             "be imported once the Result is final.")  # fmt: skip
    for discrepancy in found.get("discrepancies", []):
        found_notices.append(f"{discrepancy.message} Review the manual log and the DCS data.")
    return found_notices


def check(result, user):
    """(issues, notices, evaluations) for the Result as it is now."""
    found = evidence.collect(result.test_run, result)
    items = list(result.acceptance_items.select_related("component"))
    evaluations = acceptance.evaluate_all(
        items, acceptance.Evidence(found["leakage"], found["wear"], found["period"], found["timeline"])
    )
    return blocking_issues(result, found, evaluations), notices(result, found, items), evaluations


def finalize(result, user):
    """Finalize; returns the final Result. Raises FinalizationError (nothing changed) when it cannot."""
    if not can_finalize_result(user):
        raise FinalizationError([Issue("You are not allowed to finalize Test Results.", "report")])
    test_run = result.test_run
    written = []
    try:
        with transaction.atomic():
            result = TestResult.objects.select_for_update().select_related("test_run__project").get(pk=result.pk)
            if result.is_final:
                raise FinalizationError([Issue("The Result is already final.", "summary")])
            issues, _, _ = check(result, user)
            if issues:
                raise FinalizationError(issues)

            now = timezone.now()
            number = revisions.working_number(result)
            record = build_run_report_context(test_run, generated_at=now, final={"by": user, "at": now})
            try:
                record_name, record_sha = records.write_record(test_run, number, record, written)
                manifest = records.freeze_assets(test_run, number, record, report_assets(record), written)
                revision = ResultRevision.objects.create(
                    result=result, number=number, finalized_at=now, finalized_by=user,
                    tcn_revision=test_run.current_revision(), record_file=record_name, record_sha256=record_sha,
                    record_schema=record.get("schema", 1), assets=manifest, reason=result.revision_reason,
                    opened_at=result.revision_opened_at, opened_by=result.revision_opened_by,
                )  # fmt: skip
                revisions.supersede_earlier(revision, user, now)
                publication.issue(revision, ENGINEERING, user, origin="finalized", written=written)
            except PdfConversionError as error:
                raise FinalizationError([Issue(f"The final PDF could not be generated; nothing was finalized. {error}",
                                               "report")]) from error  # fmt: skip
            except (RecordIntegrityError, publication.PublicationError) as error:
                raise FinalizationError([Issue(f"{error} Nothing was finalized.", "report")]) from error

            # Compare-and-set: only a Draft becomes Final, once, whatever another request did meanwhile.
            claimed = TestResult.objects.filter(pk=result.pk, status=TestResult.Status.DRAFT).update(
                status=TestResult.Status.FINAL, finalized_at=now, finalized_by=user, revision_reason="",
                revision_opened_at=None, revision_opened_by=None, updated_by=user, updated_at=now,
            )  # fmt: skip
            if claimed != 1:
                raise FinalizationError([Issue("The Result was finalized by another request.", "summary")])
    except BaseException:
        storage = records.storage()
        for name in written:
            storage.delete(name)
        raise
    return TestResult.objects.get(pk=result.pk)

"""Edit locks: what locks what, in one place.

    Rule                       Locked when                                  What it locks
    tcn_lock(tcn)              the TCN revision is issued, superseded or    the revision, its run setup and seal
                               cancelled                                    positions, its acceptance requirement
                                                                            changes
    project_no_lock(project)   a TCN of the project has been issued         the project number
    result_lock(test_run)      the run's Result is Final                    the Result and everything under it
                                                                            (components, pictures, plots,
                                                                            assessments, measurements and their
                                                                            analyses); the run's manual log, DCS
                                                                            data, running intervals and log imports
    on_record_lock(item)       a final revision holds the item              deleting a picture, plot, measurement
                                                                            or instrument file; reopening a
                                                                            reviewed measurement
    drawing_lock(drawing)      it is not the current drawing, or a Final    deleting a tester drawing or P&ID
                               Report was issued with it

Each rule returns why the change is refused, or "". Pages show that text as
their message; the backstops (register(), connected when the apps are ready)
raise it as an error for every other write path, so the two cannot drift
apart. QuerySet.update bypasses the backstops and is used only for Draft data.

A revision in progress (results/revisions.py) makes the Result a Draft again:
result_lock opens with it, while on_record_lock keeps what earlier final
revisions hold.

Not locks, and so not here: the status machines (TCN transitions in
tcn/models.py, finalize and revise in results/), the immutability of frozen
records and issued documents (ResultRevision, ReportDocument,
FinalReportDocument), and the temporary policy that execution needs an
issued TCN (result_refusal below, TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN).

Models are imported inside the functions: the apps' models import this module.
"""

from django.conf import settings


class Locked(Exception):
    """A change a lock refuses; the message says why. FinalResultError and ImmutableTcnError are Locked."""


# ---- TCN and project --------------------------------------------------------------------------


def tcn_lock(tcn):
    from testlab.tcn.models import TcnRevision

    if tcn.status == TcnRevision.Status.DRAFT:
        return ""
    return f"{tcn} is {tcn.get_status_display().lower()} and cannot be edited."


def _issued_tcn_of_setups(setup_ids):
    from testlab.tcn.models import TcnRevision

    return TcnRevision.objects.filter(setup_id__in=setup_ids).exclude(status=TcnRevision.Status.DRAFT).first()


def project_no_lock(project):
    """Once any TCN of the project has been issued, whatever its later status (V2): issued TCNs carry the number."""
    if project.pk is not None and project.test_runs.filter(revisions__issued_at__isnull=False).exists():
        return "The project number cannot be changed: a TCN of this project has been issued."
    return ""


# ---- Result and execution evidence ------------------------------------------------------------


def result_lock(test_run):
    from testlab.results.models import TestResult

    if TestResult.objects.filter(test_run=test_run, status=TestResult.Status.FINAL).exists():
        return final_result_message(test_run)
    return ""


def final_result_message(test_run):
    return (f"The Result of Run {test_run.display_no} is final: it and the run's manual log and DCS data cannot "
            "change. Open a revision to correct it.")  # fmt: skip


def result_refusal(test_run):
    """What the run pages check before any change of the Result or the execution evidence: the lock, then
    the temporary policy. Intentional during development and validation: evidence may be recorded for a
    Draft TCN (TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN off, the default). TODO(production): switch the setting
    on so that only an issued TCN permits execution. The models stay as they are."""
    if lock := result_lock(test_run):
        return lock
    if settings.TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN and not test_run.has_issued_revision():
        return f"TCN{test_run.display_no} has not been issued yet: readings and DCS data are recorded once it is issued."
    return ""


def on_record(item):
    """The latest final revision that holds the picture, plot, measurement or measurement file, or None.
    An item is on record in every revision finalized after it was added."""
    from testlab.results.models import FaceMeasurement, MeasurementFile, ResultComponent, ResultImage, ResultPlot

    if isinstance(item, ResultImage):
        return _finalized_since(item.result_id, item.uploaded_at)
    if isinstance(item, ResultPlot):
        return _finalized_since(item.result_id, item.created_at)
    if isinstance(item, MeasurementFile):
        item = FaceMeasurement.objects.filter(pk=item.measurement_id).first()
    if item is None:
        return None
    result_id = ResultComponent.objects.filter(pk=item.component_id).values_list("result_id", flat=True).first()
    return _finalized_since(result_id, item.created_at)


def _finalized_since(result_id, since):
    from testlab.results.models import ResultRevision

    if result_id is None or since is None:
        return None
    return ResultRevision.objects.filter(result_id=result_id, finalized_at__gte=since).order_by("-number").first()


def on_record_lock(item, action="deleted"):
    if revision := on_record(item):
        return f"{item} is on record in final revision {revision.number} and cannot be {action}."
    return ""


# ---- Project drawings --------------------------------------------------------------------------


def drawing_lock(drawing):
    """Deleting a drawing: only the current one (a wrong upload), and only while no Final Report was issued
    with it (the appendices show the current drawings)."""
    from testlab.projects import drawings
    from testlab.reports.models import FinalReportDocument

    if drawings.current(drawing.project, drawing.kind) != drawing:
        return "Only the current drawing can be deleted; earlier drawings are kept as its history."
    issued = FinalReportDocument.objects.filter(report__project=drawing.project, issued_at__gte=drawing.uploaded_at)
    if document := issued.order_by("-issued_at").first():
        return (f"This drawing is in the issued Final Report {document.identity}: upload a new drawing instead; "
                "this one is kept as its history.")  # fmt: skip
    return ""


# ---- Backstops ---------------------------------------------------------------------------------


def _tcn_part_guard(sender, instance, **kwargs):
    """Run setup and seal positions of an issued TCN (also the setup a position is moved away from)."""
    from testlab.projects.models import RunSetup, SealPosition
    from testlab.tcn.models import ImmutableTcnError

    if sender is RunSetup:
        setup_ids = {instance.pk} if instance.pk is not None else set()
    else:
        setup_ids = {instance.setup_id}
        if instance.pk is not None:
            setup_ids.update(SealPosition.objects.filter(pk=instance.pk).values_list("setup_id", flat=True))
    if tcn := _issued_tcn_of_setups(setup_ids):
        noun = "run setup" if sender is RunSetup else "seal position"
        raise ImmutableTcnError(f"This {noun} belongs to {tcn}, which has been issued, and cannot be changed.")


def _test_runs(sender, instance):
    """The Test Runs whose Result holds `instance`, or whose execution evidence it is."""
    from testlab.execution.models import (DcsDataset, DcsRunningDetermination, ManualLogImport, ManualLogImportRow,
                                          ManualMeasurement, ManualLogEntry)  # fmt: skip
    from testlab.results.models import FaceMeasurement, ResultComponent, TestResult
    from testlab.tcn.models import TestRun

    if sender is ManualMeasurement:
        run_ids = ManualLogEntry.objects.filter(pk=instance.entry_id).values_list("test_run_id", flat=True)
    elif sender is ManualLogImportRow:
        run_ids = ManualLogImport.objects.filter(pk=instance.batch_id).values_list("test_run_id", flat=True)
    elif sender is DcsRunningDetermination:
        run_ids = DcsDataset.objects.filter(pk=instance.dataset_id).values_list("test_run_id", flat=True)
    elif hasattr(instance, "test_run_id"):  # manual log entries, DCS datasets, log imports
        run_ids = [instance.test_run_id]
    else:
        if hasattr(instance, "result_id"):  # components, pictures, plots, acceptance items
            result_ids = [instance.result_id]
        elif sender is FaceMeasurement:
            result_ids = ResultComponent.objects.filter(pk=instance.component_id).values_list("result_id", flat=True)
        else:  # files, figures and analysis rows of a measurement
            result_ids = FaceMeasurement.objects.filter(pk=instance.measurement_id).values_list(
                "component__result_id", flat=True)  # fmt: skip
        run_ids = TestResult.objects.filter(pk__in=list(result_ids)).values_list("test_run_id", flat=True)
    return TestRun.objects.filter(pk__in=list(run_ids))


def _result_guard(sender, instance, update_fields=None, **kwargs):
    from testlab.execution.models import DcsDataset
    from testlab.results.models import FinalResultError

    if sender is DcsDataset and update_fields is not None and set(update_fields) <= {"columns_file"}:
        return  # the derived column cache may be rebuilt from the unchanged raw file
    for test_run in _test_runs(sender, instance):
        if lock := result_lock(test_run):
            raise FinalResultError(lock)


def _on_record_delete_guard(sender, instance, **kwargs):
    from testlab.results.models import FinalResultError

    if lock := on_record_lock(instance):
        raise FinalResultError(lock)


def _on_record_reopen_guard(sender, instance, **kwargs):
    from testlab.results.models import FaceMeasurement, FinalResultError

    if instance.pk is None or instance.status == FaceMeasurement.Status.REVIEWED:
        return
    if FaceMeasurement.objects.filter(pk=instance.pk, status=FaceMeasurement.Status.REVIEWED).exists():
        if lock := on_record_lock(instance, "reopened"):
            raise FinalResultError(lock)


def register():
    """Connect the backstops (once; dispatch_uid). Called by the Results app when the apps are ready."""
    from django.db.models.signals import pre_delete, pre_save

    from testlab.execution.models import (DcsDataset, DcsRunningDetermination, ManualLogEntry, ManualLogImport,
                                          ManualLogImportRow, ManualMeasurement)  # fmt: skip
    from testlab.projects.models import RunSetup, SealPosition
    from testlab.results.models import (AcceptanceItem, DimensionalReading, FaceMeasurement, MahrAnalysis,
                                        MeasurementFigure, MeasurementFile, ResultComponent, ResultImage, ResultPlot,
                                        RoundpakAnalysis)  # fmt: skip

    signals = {"save": pre_save, "delete": pre_delete}

    def guard(rule, models, on=("save", "delete")):
        for model in models:
            for name in on:
                signals[name].connect(rule, sender=model, dispatch_uid=f"lock{rule.__name__}_{name}_{model.__name__}")

    guard(_tcn_part_guard, [RunSetup, SealPosition])
    guard(_result_guard, [ResultComponent, ResultImage, ResultPlot, AcceptanceItem, FaceMeasurement, MeasurementFile,
                          MeasurementFigure, DimensionalReading, MahrAnalysis, RoundpakAnalysis,
                          ManualLogEntry, ManualMeasurement, DcsDataset, DcsRunningDetermination, ManualLogImport,
                          ManualLogImportRow])  # fmt: skip
    guard(_on_record_delete_guard, [ResultImage, ResultPlot, FaceMeasurement, MeasurementFile], ["delete"])
    guard(_on_record_reopen_guard, [FaceMeasurement], ["save"])

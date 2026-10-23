"""Test helpers. Status changes are made directly on the model here, bypassing
the services, to test the model-level rules on their own."""

from django.utils import timezone

from testlab.projects.tests.test_models import make_project
from testlab.tcn import requirements
from testlab.tcn.models import TcnRevision, TestRun
from testlab.tcn.signals import tcn_issued

__all__ = ["make_project", "make_run", "new_revision", "issue", "supersede", "cancel"]


def make_run(project, user, run_no=1):
    """TestRun + draft rev 0 holding a copy of the project defaults."""
    run = TestRun.objects.create(project=project, run_no=run_no, created_by=user)
    TcnRevision.objects.create(
        test_run=run,
        setup=project.default_setup.clone(),
        project_description=project.description,
        prepared_by="MG",
        source_kind=TcnRevision.SourceKind.PROJECT_DEFAULTS,
        created_by=user,
        updated_by=user,
    )
    return run


def new_revision(source, user):
    return TcnRevision.objects.create(
        test_run=source.test_run,
        revision=source.test_run.next_revision_no(),
        setup=source.setup.clone(),
        project_description=source.project_description,
        prepared_by=source.prepared_by,
        source_kind=TcnRevision.SourceKind.NEW_REVISION,
        source_tcn=source,
        created_by=user,
        updated_by=user,
    )


def issue(tcn, user):
    """Issue without the PDF; as services.issue_tcn, the requirements in force are frozen and the run's Draft
    Result is told (an earlier issue is not superseded: see supersede)."""
    issued_at = timezone.now()
    requirements.freeze(tcn, issued_at)
    tcn.status = TcnRevision.Status.ISSUED
    tcn.issued_at = issued_at
    tcn.issued_by = user
    tcn.save()
    tcn_issued.send(sender=TcnRevision, tcn=tcn, user=user)
    return tcn


def supersede(tcn):
    tcn.status = TcnRevision.Status.SUPERSEDED
    tcn.save()
    return tcn


def cancel(tcn, user, reason="Test cancelled by customer"):
    tcn.status = TcnRevision.Status.CANCELLED
    tcn.cancelled_at = timezone.now()
    tcn.cancelled_by = user
    tcn.cancel_reason = reason
    tcn.save()
    return tcn

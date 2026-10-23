"""Test Runs and their TCN revisions (M1 spec §1.4, §1.5, §3, §4).

TestRun is the individual test (run 001, 002, …; Run No. = TCN No.).
TcnRevision is the issued instruction for that run (Instructed). A run has
one or more revisions — rev 0, rev 1, … — and never becomes several runs.
Results (Actual, M2) will attach to the TestRun, not to a revision.

Enforced here, independent of any form or view:

* every revision owns its own RunSetup, a copy (RunSetup.clone()) that is
  never the project defaults and never shared with another revision;
* status changes only draft → issued → superseded | cancelled;
* only drafts can be edited or deleted. Issued, superseded and cancelled
  revisions are immutable: save, delete, QuerySet.update and
  QuerySet.delete are refused. Their RunSetup and seal positions are
  guarded by signals (save and delete, testlab/locks.py); QuerySet.update on those two
  tables bypasses signals and is not used for TCN setups.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Max, Q
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from testlab.locks import Locked, tcn_lock
from testlab.projects.models import AcceptanceRequirementFields, Project, ProgramRequirement, RunSetup


class TcnRuleError(Exception):
    """A change the TCN domain rules do not allow."""


class ImmutableTcnError(TcnRuleError, Locked):
    """Attempt to change or delete an issued, superseded or cancelled TCN revision."""


class InvalidTransitionError(TcnRuleError):
    """A status change the TCN status machine does not allow."""


class TestRun(models.Model):
    """One individual test of a project; its instruction is the TCN with the same number."""

    __test__ = False  # not a pytest test class

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="test_runs")
    run_no = models.PositiveSmallIntegerField(
        "Run no.", validators=[MinValueValidator(1), MaxValueValidator(999)]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["project", "run_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "run_no"],
                name="unique_run_no_per_project",
                violation_error_message="Run number already used!",
            ),
            models.CheckConstraint(condition=Q(run_no__gte=1, run_no__lte=999), name="run_no_1_to_999"),
        ]

    def __str__(self):
        return f"{self.project.project_no} run {self.display_no}"

    @property
    def display_no(self):
        return f"{self.run_no:03d}"

    @classmethod
    def next_run_no(cls, project):
        """Proposed number for a new run: highest in the project + 1 (gaps allowed)."""
        highest = cls.objects.filter(project=project).aggregate(highest=Max("run_no"))["highest"]
        return (highest or 0) + 1

    def next_revision_no(self):
        highest = self.revisions.aggregate(highest=Max("revision"))["highest"]
        return 0 if highest is None else highest + 1

    def current_revision(self):
        """The issued revision; otherwise the latest (a draft before first issue, or cancelled)."""
        revisions = list(self.revisions.all())  # uses prefetched revisions when present
        issued = [r for r in revisions if r.status == TcnRevision.Status.ISSUED]
        return issued[0] if issued else (revisions[-1] if revisions else None)

    def has_issued_revision(self):
        """True once any revision has been issued (whatever its later status)."""
        return self.revisions.exclude(status=TcnRevision.Status.DRAFT).exists()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = TestRun.objects.filter(pk=self.pk).values_list("project_id", "run_no").first()
            if stored and stored != (self.project_id, self.run_no) and self.has_issued_revision():
                raise ImmutableTcnError(
                    f"TCN{stored[1]:03d} has been issued; its run number and project cannot change."
                )
        super().save(*args, **kwargs)


class TcnRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # Bulk updates bypass save(); allow them only for draft content.
        fixed = (TcnRevision.FIXED_FIELDS | {"status"}) & set(kwargs)
        if fixed:
            raise TcnRuleError(f"{', '.join(sorted(fixed))} cannot be changed by a bulk update.")
        if self.exclude(status=TcnRevision.Status.DRAFT).exists():
            raise ImmutableTcnError("Issued, superseded and cancelled TCNs cannot be changed.")
        return super().update(**kwargs)

    def delete(self):
        # Checked before Django's delete transaction starts, so a refusal
        # leaves the caller's transaction usable.
        _check_deletable(self)
        return super().delete()


class TcnRevision(models.Model):
    """One revision of a Test Change Notice: the instruction for a TestRun."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        SUPERSEDED = "superseded", "Superseded"
        CANCELLED = "cancelled", "Cancelled"

    class SourceKind(models.TextChoices):
        PROJECT_DEFAULTS = "project_defaults", "Project defaults"
        TCN_COPY = "tcn_copy", "Copy of TCN"
        NEW_REVISION = "new_revision", "New revision"

    test_run = models.ForeignKey(TestRun, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveSmallIntegerField("Revision", default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    # Own copy of the technical block; never the project defaults, never shared.
    setup = models.OneToOneField(RunSetup, on_delete=models.PROTECT, related_name="tcn_revision")
    project_description = models.CharField("Project description", max_length=200, blank=True)
    prepared_by = models.CharField("Prepared by", max_length=10, blank=True)
    created_date = models.DateField("Created", default=timezone.localdate)
    revision_reason = models.TextField("Reason for revision", blank=True)
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    source_tcn = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="derived_revisions"
    )

    # Set when issued (commit 9); the stored document and snapshot are the record.
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    issued_document = models.FileField(max_length=255, blank=True)
    issued_document_sha256 = models.CharField(max_length=64, blank=True)
    issued_snapshot = models.JSONField(null=True, blank=True)
    template_sha256 = models.CharField(max_length=64, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.TextField(blank=True)

    row_version = models.PositiveIntegerField(default=1)  # optimistic locking of draft edits
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    objects = TcnRevisionQuerySet.as_manager()

    TRANSITIONS = {
        Status.DRAFT: {Status.DRAFT, Status.ISSUED},
        Status.ISSUED: {Status.SUPERSEDED, Status.CANCELLED},
        Status.SUPERSEDED: set(),
        Status.CANCELLED: set(),
    }
    # Fields a transition of an issued revision may change; everything else is frozen.
    TRANSITION_FIELDS = {
        (Status.ISSUED, Status.SUPERSEDED): {"status", "updated_at", "updated_by"},
        (Status.ISSUED, Status.CANCELLED): {
            "status", "cancelled_at", "cancelled_by", "cancel_reason", "updated_at", "updated_by",
        },
    }  # fmt: skip
    # Identity and provenance: fixed at creation, even while draft.
    FIXED_FIELDS = {
        "test_run", "revision", "setup", "source_kind", "source_tcn", "created_date", "created_at", "created_by",
    }  # fmt: skip

    class Meta:
        ordering = ["test_run", "revision"]
        constraints = [
            models.UniqueConstraint(fields=["test_run", "revision"], name="unique_revision_per_run"),
            models.UniqueConstraint(
                fields=["test_run"], condition=Q(status="draft"), name="one_draft_revision_per_run"
            ),
            models.UniqueConstraint(
                fields=["test_run"], condition=Q(status="issued"), name="one_issued_revision_per_run"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "issued", "superseded", "cancelled"]), name="tcn_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(source_kind__in=["project_defaults", "tcn_copy", "new_revision"]),
                name="tcn_source_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(source_kind="project_defaults", source_tcn__isnull=True)
                | Q(source_kind__in=["tcn_copy", "new_revision"], source_tcn__isnull=False),
                name="tcn_source_tcn_matches_source_kind",
            ),
            models.CheckConstraint(
                condition=(Q(revision=0) & ~Q(source_kind="new_revision"))
                | Q(revision__gt=0, source_kind="new_revision"),
                name="tcn_revision_matches_source_kind",
            ),
            models.CheckConstraint(
                condition=Q(status="draft", issued_at__isnull=True, issued_by__isnull=True)
                | (~Q(status="draft") & Q(issued_at__isnull=False, issued_by__isnull=False)),
                name="tcn_issue_data_iff_issued",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="cancelled", cancelled_at__isnull=False, cancelled_by__isnull=False)
                    & ~Q(cancel_reason="")
                )
                | (~Q(status="cancelled") & Q(cancelled_at__isnull=True, cancelled_by__isnull=True, cancel_reason="")),
                name="tcn_cancel_data_iff_cancelled",
            ),
        ]

    def __str__(self):
        return f"TCN{self.tcn_no}"

    @property
    def tcn_no(self):
        """Number as printed in the TCN header: "001", or "001 rev 1" from revision 1."""
        return self.test_run.display_no + (f" rev {self.revision}" if self.revision else "")

    @property
    def is_locked(self):
        return self.status != self.Status.DRAFT

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self._state.adding:
                self._check_new()
            else:
                self._check_change()
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _check_deletable(TcnRevision.objects.filter(pk=self.pk))
        return super().delete(*args, **kwargs)

    def _check_new(self):
        if self.status != self.Status.DRAFT:
            raise InvalidTransitionError("A TCN revision is always created as a draft.")
        expected = self.test_run.next_revision_no()
        if self.revision != expected:
            raise TcnRuleError(
                f"The next revision of TCN{self.test_run.display_no} is {expected}, not {self.revision}."
            )
        source = self.source_tcn
        if self.source_kind == self.SourceKind.NEW_REVISION and source is not None:
            if source.test_run_id != self.test_run_id:
                raise TcnRuleError("A new revision must be made from a revision of the same Test Run.")
        # M1: TCN_COPY stays within the project. Reuse across projects would be
        # a separate template/project-copy feature, not a TCN copy.
        if self.source_kind == self.SourceKind.TCN_COPY and source is not None:
            if source.test_run.project_id != self.test_run.project_id:
                raise TcnRuleError("A TCN can only be copied from a TCN of the same project.")
        if Project.objects.filter(default_setup_id=self.setup_id).exists():
            raise TcnRuleError("A TCN must own a copy of the project defaults (RunSetup.clone()), not the defaults.")

    def _check_change(self):
        stored = TcnRevision.objects.select_for_update().get(pk=self.pk)
        old, new = stored.status, self.status
        if new not in self.TRANSITIONS[old]:
            if old != self.Status.DRAFT and new in (old, self.Status.DRAFT):  # incl. a stale draft object
                raise ImmutableTcnError(f"{stored} is {stored.get_status_display().lower()} and cannot be changed.")
            raise InvalidTransitionError(f"{stored}: status cannot change from {old} to {new}.")
        if old == self.Status.DRAFT:
            frozen = [f for f in self._meta.concrete_fields if f.name in self.FIXED_FIELDS]
        else:
            allowed = self.TRANSITION_FIELDS[(old, new)]
            frozen = [f for f in self._meta.concrete_fields if f.name not in allowed]
        changed = [f.name for f in frozen if f.value_from_object(self) != f.value_from_object(stored)]
        if not changed:
            return
        if old == self.Status.DRAFT:
            raise TcnRuleError(f"{', '.join(changed)} cannot change after the TCN revision is created.")
        raise ImmutableTcnError(
            f"{stored} is {stored.get_status_display().lower()}; {', '.join(changed)} cannot change."
        )


class TcnRequirementChange(AcceptanceRequirementFields):
    """An acceptance requirement changed for this run by the TCN (TCN = instructed).

    Replace: a Test Program requirement is replaced for this run by the
    requirement given here. Not applicable: a Test Program requirement does
    not apply to this run. Add: a requirement the Program does not have.

    A change is in force for the run only when its TCN revision is issued
    (an issued TCN is the approved instruction); a draft's changes are
    pending. It is part of the TCN: printed on it, frozen with it at issue
    (guarded below like the run setup) and carried into a new revision.
    """

    class Action(models.TextChoices):
        REPLACE = "replace", "Replaces Program requirement"
        NOT_APPLICABLE = "not_applicable", "Program requirement not applicable to this run"
        ADD = "add", "Additional requirement"

    tcn = models.ForeignKey(TcnRevision, on_delete=models.CASCADE, related_name="requirement_changes")
    action = models.CharField(max_length=20, choices=Action.choices)
    program_requirement = models.ForeignKey(
        ProgramRequirement, on_delete=models.PROTECT, null=True, blank=True, related_name="tcn_changes"
    )
    reason = models.TextField("Reason for the change")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["tcn", "order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(action__in=["replace", "not_applicable", "add"]), name="requirement_change_action_valid"
            ),
            models.CheckConstraint(
                condition=Q(action="add", program_requirement__isnull=True)
                | Q(action__in=["replace", "not_applicable"], program_requirement__isnull=False),
                name="requirement_change_target_matches_action",
            ),
            models.UniqueConstraint(
                fields=["tcn", "program_requirement"], condition=Q(program_requirement__isnull=False),
                name="one_change_per_program_requirement",
            ),
        ]  # fmt: skip

    def __str__(self):
        return f"{self.tcn}: {self.get_action_display()} {self.program_requirement or self.criterion}"

    def _check_editable(self):
        # Checked before Django's save/delete transaction starts, so a refusal leaves the caller's
        # transaction usable; the signal guard below is the backstop.
        tcn_ids = {self.tcn_id}
        if self.pk is not None:
            tcn_ids.update(TcnRequirementChange.objects.filter(pk=self.pk).values_list("tcn_id", flat=True))
        for tcn in TcnRevision.objects.filter(pk__in=tcn_ids):
            if lock := tcn_lock(tcn):
                raise ImmutableTcnError(f"This acceptance requirement change belongs to the TCN: {lock}")

    def save(self, *args, **kwargs):
        self._check_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_editable()
        return super().delete(*args, **kwargs)

    def clone_to(self, tcn):
        values = self.requirement_values()
        return TcnRequirementChange.objects.create(
            tcn=tcn, action=self.action, program_requirement=self.program_requirement, reason=self.reason,
            order=self.order, **values,
        )  # fmt: skip


def _check_deletable(revisions):
    # Checked against the database: an instance may be stale.
    issued = revisions.exclude(status=TcnRevision.Status.DRAFT)
    if issued.exists():
        raise ImmutableTcnError(f"{issued.first()} has been issued and cannot be deleted (cancel it instead).")


# The signal guards below are backstops for deletes that do not go through
# TcnRevision.delete() or its QuerySet (e.g. a cascade). The run setup and seal
# positions of an issued TCN are guarded in testlab/locks.py. Raised inside
# Django's delete transaction, they also abort the surrounding transaction.


@receiver(pre_delete, sender=TcnRevision)
def _only_drafts_can_be_deleted(sender, instance, **kwargs):
    _check_deletable(TcnRevision.objects.filter(pk=instance.pk))


@receiver(post_delete, sender=TcnRevision)
def _delete_owned_records(sender, instance, **kwargs):
    # The setup belongs to this revision alone, and a run never exists
    # without a TCN: deleting the draft rev 0 removes the run too.
    RunSetup.objects.filter(pk=instance.setup_id).delete()
    TestRun.objects.filter(pk=instance.test_run_id, revisions__isnull=True).delete()


@receiver(pre_delete, sender=TcnRequirementChange)
def _guard_issued_requirement_change(sender, instance, **kwargs):
    instance._check_editable()  # deletes that bypass TcnRequirementChange.delete() (e.g. a cascade)

"""Lab planning: test units, project bookings and unit blocks.

Rough planning by week, edited freely: never controlled, never frozen, and
not tied to the TCN (a test is often planned months before its TCN exists).
Nothing here is read by a TCN, Result or report.

Rules (PLANNING_BRIEF.md): a unit holds one project or block at a time,
Cancelled bookings excepted; a project has one booking, so it stays on one
unit.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q

from testlab.projects.models import normalize_project_no

from . import weeks


class TestUnit(models.Model):
    """A test unit of the lab (A, B, C, D); managed in the admin."""

    __test__ = False  # not a pytest test class

    code = models.CharField("Code", max_length=10, unique=True)
    name = models.CharField("Name", max_length=100, blank=True)
    active = models.BooleanField("Active", default=True, help_text="Inactive units take no new bookings.")
    sort_order = models.PositiveSmallIntegerField("Sort order", default=0)
    note = models.TextField("Note", blank=True)

    class Meta:
        ordering = ["sort_order", "code"]

    def __str__(self):
        return f"Unit {self.code}"


class PlannedPeriod(models.Model):
    """Weeks on a unit: start is the Monday of the first week, end the Sunday of the last."""

    unit = models.ForeignKey(TestUnit, on_delete=models.PROTECT, related_name="+")
    start = models.DateField("Start")
    end = models.DateField("End")
    note = models.TextField("Note", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True

    @property
    def weeks_text(self):
        return weeks.span_text(self.start, self.end)

    def occupies_unit(self):
        return True

    def _snap_to_weeks(self):
        if self.start:
            self.start = weeks.monday(*weeks.week_of(self.start))
        if self.end:
            self.end = weeks.sunday(*weeks.week_of(self.end))

    def clean(self):
        super().clean()
        self._snap_to_weeks()
        if self.start and self.end and self.end < self.start:
            raise ValidationError("The last week lies before the first week.")
        if self.unit_id and self.start and self.end and (conflict := self.conflict()):
            raise ValidationError(conflict)

    def conflict(self):
        """Why the unit is not free in these weeks, or None."""
        if not self.occupies_unit():
            return None
        return unit_conflict(self.unit_id, self.start, self.end,
                             booking=self if isinstance(self, Booking) else None,
                             block=self if isinstance(self, UnitBlock) else None)  # fmt: skip

    def save(self, *args, **kwargs):
        self._snap_to_weeks()
        with transaction.atomic():
            # Enforced here too, not only in the forms: a unit never holds two projects at once.
            if conflict := self.conflict():
                raise ValidationError(conflict)
            super().save(*args, **kwargs)


class Booking(PlannedPeriod):
    """A project's booking of a test unit. The project is free text: it may not exist in TestLab yet."""

    class Status(models.TextChoices):
        TENTATIVE = "tentative", "Tentative"
        CONFIRMED = "confirmed", "Confirmed"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    project_no = models.CharField(
        "Project no.",
        max_length=20,
        unique=True,
        error_messages={"unique": "This project already has a booking: a project stays on one unit."},
    )
    description = models.CharField("Description", max_length=200)
    engineers = models.CharField("PE(s)", max_length=200, blank=True)
    status = models.CharField("Status", max_length=10, choices=Status.choices, default=Status.TENTATIVE)

    class Meta:
        ordering = ["unit", "start"]
        constraints = [models.CheckConstraint(condition=Q(end__gte=F("start")), name="booking_end_after_start")]

    def __str__(self):
        return self.project_no

    def occupies_unit(self):
        return self.status != self.Status.CANCELLED

    def _normalize(self):
        self.project_no = normalize_project_no(self.project_no)
        self.description = self.description.strip()
        self.engineers = self.engineers.strip()

    def clean(self):
        self._normalize()  # before the unique check of the project no.
        super().clean()

    def save(self, *args, **kwargs):
        self._normalize()
        super().save(*args, **kwargs)


class UnitBlock(PlannedPeriod):
    """Weeks a unit is not available for projects: maintenance, rebuild, calibration or reserved."""

    class Kind(models.TextChoices):
        MAINTENANCE = "maintenance", "Maintenance"
        REBUILD = "rebuild", "Rebuild"
        CALIBRATION = "calibration", "Calibration"
        RESERVED = "reserved", "Reserved"

    kind = models.CharField("Kind", max_length=12, choices=Kind.choices)

    class Meta:
        ordering = ["unit", "start"]
        constraints = [models.CheckConstraint(condition=Q(end__gte=F("start")), name="unit_block_end_after_start")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.unit.code} {self.weeks_text}"


def unit_conflict(unit_id, start, end, booking=None, block=None):
    """The message naming what already holds the unit in [start, end], or None.

    Periods are whole weeks, so a period ending in week N and one starting in
    week N+1 do not overlap; sharing a week does.
    """
    overlapping = Q(unit_id=unit_id, start__lte=end, end__gte=start)
    bookings = Booking.objects.filter(overlapping).exclude(status=Booking.Status.CANCELLED)
    if booking is not None and booking.pk:
        bookings = bookings.exclude(pk=booking.pk)
    blocks = UnitBlock.objects.filter(overlapping)
    if block is not None and block.pk:
        blocks = blocks.exclude(pk=block.pk)
    unit = TestUnit.objects.get(pk=unit_id)
    if other := bookings.order_by("start").first():
        return (f"{unit} is booked by {other.project_no} ({other.get_status_display().lower()}) in "
                f"{other.weeks_text}: a unit holds one project at a time.")  # fmt: skip
    if other := blocks.order_by("start").first():
        return f"{unit} is blocked for {other.get_kind_display().lower()} in {other.weeks_text}."
    return None

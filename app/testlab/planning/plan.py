"""The lab plan of one year: per unit its bookings and blocks as bars over the ISO weeks."""

import calendar
import datetime
from dataclasses import dataclass, field

from django.db.models import Q

from testlab.projects.models import Project

from . import weeks
from .models import Booking, TestUnit, UnitBlock


@dataclass
class Bar:
    item: object  # Booking or UnitBlock
    first: int  # first and last week shown in this year (clipped at the year's edges)
    last: int
    clipped_before: bool
    clipped_after: bool
    project: Project | None = None  # the TestLab project with the booking's number, once it exists

    @property
    def is_booking(self):
        return isinstance(self.item, Booking)

    @property
    def weeks(self):
        return self.last - self.first + 1


@dataclass
class UnitRows:
    unit: TestUnit
    bars: list = field(default_factory=list)

    @property
    def booked_weeks(self):
        return sum(bar.weeks for bar in self.bars if bar.is_booking and bar.item.occupies_unit())

    @property
    def blocked_weeks(self):
        return sum(bar.weeks for bar in self.bars if not bar.is_booking)


def months(year):
    """(month abbreviation, number of weeks) across the year; a week belongs to the month of its Thursday."""
    spans = []
    for week in range(1, weeks.weeks_in_year(year) + 1):
        name = calendar.month_abbr[datetime.date.fromisocalendar(year, week, 4).month]
        if spans and spans[-1][0] == name:
            spans[-1][1] += 1
        else:
            spans.append([name, 1])
    return [tuple(span) for span in spans]


def build(year, show_cancelled=False):
    """Units in plan order, each with the bars of its bookings and blocks that touch the year."""
    first_day, last_day = weeks.monday(year, 1), weeks.sunday(year, weeks.weeks_in_year(year))
    in_year = Q(start__lte=last_day, end__gte=first_day)
    bookings = Booking.objects.filter(in_year).select_related("unit")
    if not show_cancelled:
        bookings = bookings.exclude(status=Booking.Status.CANCELLED)
    bookings = list(bookings)
    blocks = list(UnitBlock.objects.filter(in_year).select_related("unit"))
    projects = {p.project_no: p for p in Project.objects.filter(project_no__in=[b.project_no for b in bookings])}

    rows = {unit.pk: UnitRows(unit) for unit in TestUnit.objects.all()}
    for item in [*bookings, *blocks]:
        start, end = max(item.start, first_day), min(item.end, last_day)
        rows[item.unit_id].bars.append(Bar(
            item=item, first=weeks.week_of(start)[1], last=weeks.week_of(end)[1],
            clipped_before=item.start < first_day, clipped_after=item.end > last_day,
            project=projects.get(getattr(item, "project_no", None)),
        ))  # fmt: skip
    for unit_rows in rows.values():
        unit_rows.bars.sort(key=lambda bar: (bar.item.start, bar.is_booking))
    # Inactive units only when they still hold something this year.
    return [r for r in rows.values() if r.unit.active or r.bars]

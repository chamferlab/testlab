import pytest
from django.core.exceptions import ValidationError

from testlab.planning import weeks
from testlab.planning.models import Booking, TestUnit, UnitBlock


def unit(code):
    return TestUnit.objects.get(code=code)  # A–D come with the migration


def book(user, project_no, code, first, last, year=2026, **fields):
    booking = Booking(project_no=project_no, description=fields.pop("description", "Test project"),
                      unit=unit(code), start=weeks.monday(year, first), end=weeks.sunday(year, last),
                      created_by=user, updated_by=user, **fields)  # fmt: skip
    booking.full_clean()
    booking.save()
    return booking


def block(user, code, first, last, kind=UnitBlock.Kind.MAINTENANCE, year=2026):
    item = UnitBlock(unit=unit(code), kind=kind, start=weeks.monday(year, first), end=weeks.sunday(year, last),
                     created_by=user, updated_by=user)  # fmt: skip
    item.full_clean()
    item.save()
    return item


def test_units_a_to_d_exist(db):
    assert list(TestUnit.objects.values_list("code", flat=True)) == ["A", "B", "C", "D"]


def test_overlap_on_the_same_unit_is_refused_naming_the_project(user):
    book(user, "2026RSD03TL", "B", 10, 20, status=Booking.Status.CONFIRMED)
    with pytest.raises(ValidationError, match="Unit B is booked by 2026RSD03TL \\(confirmed\\) in w10 – w20 2026"):
        book(user, "2026RSD07TL", "B", 18, 25)


def test_save_refuses_an_overlap_without_full_clean(user):
    book(user, "2026RSD03TL", "B", 10, 20)
    clash = Booking(project_no="2026RSD07TL", description="x", unit=unit("B"), start=weeks.monday(2026, 20),
                    end=weeks.sunday(2026, 22), created_by=user, updated_by=user)  # fmt: skip
    with pytest.raises(ValidationError, match="2026RSD03TL"):
        clash.save()
    assert Booking.objects.count() == 1


def test_same_weeks_on_another_unit_are_allowed(user):
    book(user, "2026RSD03TL", "B", 10, 20)
    book(user, "2026RSD07TL", "C", 10, 20)


def test_touching_weeks_allowed_same_week_refused(user):
    book(user, "2026RSD03TL", "A", 10, 20)
    book(user, "2026RSD07TL", "A", 21, 30)
    with pytest.raises(ValidationError):
        book(user, "2026RSD08TL", "A", 30, 32)


def test_tentative_holds_the_unit_cancelled_frees_it(user):
    tentative = book(user, "2026RSD03TL", "A", 10, 20)
    with pytest.raises(ValidationError, match="tentative"):
        book(user, "2026RSD07TL", "A", 15, 16)
    tentative.status = Booking.Status.CANCELLED
    tentative.save()
    book(user, "2026RSD07TL", "A", 15, 16)
    # Reviving the cancelled booking is refused while its weeks are taken.
    tentative.status = Booking.Status.CONFIRMED
    with pytest.raises(ValidationError, match="2026RSD07TL"):
        tentative.full_clean()


def test_editing_a_booking_does_not_clash_with_itself(user):
    booking = book(user, "2026RSD03TL", "A", 10, 20)
    booking.end = weeks.sunday(2026, 24)
    booking.full_clean()
    booking.save()


def test_blocks_and_bookings_exclude_each_other(user):
    block(user, "D", 30, 31)
    with pytest.raises(ValidationError, match="Unit D is blocked for maintenance in w30 – w31 2026"):
        book(user, "2026RSD03TL", "D", 31, 40)
    book(user, "2026RSD03TL", "D", 32, 40)
    with pytest.raises(ValidationError, match="booked by 2026RSD03TL"):
        block(user, "D", 40, 41, kind=UnitBlock.Kind.CALIBRATION)
    with pytest.raises(ValidationError, match="blocked"):
        block(user, "D", 31, 31)


def test_one_booking_per_project(user):
    book(user, "2026RSD03TL", "A", 10, 20)
    with pytest.raises(ValidationError, match="already has a booking"):
        book(user, "2026rsd03tl ", "B", 10, 20)


def test_booking_may_cross_the_year_boundary(user):
    booking = Booking(project_no="2026RSD03TL", description="x", unit=unit("A"), start=weeks.monday(2026, 50),
                      end=weeks.sunday(2027, 3), created_by=user, updated_by=user)  # fmt: skip
    booking.full_clean()
    booking.save()
    with pytest.raises(ValidationError, match="w50 2026 – w3 2027"):
        book(user, "2027RSD01TL", "A", 2, 5, year=2027)


def test_dates_snap_to_whole_weeks(user):
    booking = Booking(project_no="2026RSD03TL", description="x", unit=unit("A"),
                      start=weeks.monday(2026, 10).replace(day=4), end=weeks.monday(2026, 12),
                      created_by=user, updated_by=user)  # fmt: skip
    booking.save()
    assert (booking.start, booking.end) == (weeks.monday(2026, 10), weeks.sunday(2026, 12))


def test_last_week_before_first_week_refused(user):
    with pytest.raises(ValidationError, match="before the first week"):
        book(user, "2026RSD03TL", "A", 20, 10)

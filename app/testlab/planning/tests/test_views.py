import re

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from testlab.planning import weeks
from testlab.planning.models import Booking, TestUnit, UnitBlock
from testlab.projects.tests.test_models import make_project

from .test_models import block, book


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def post_booking(client, url, **data):
    values = {"project_no": "2026RSD09TL", "description": "Jafurah SQT", "engineers": "Martijn, Naresh",
              "unit": TestUnit.objects.get(code="B").pk, "start_week": "2026-W10", "end_week": "2026-W20",
              "status": "confirmed", "note": ""}  # fmt: skip
    values.update(data)
    return client.post(url, values)


def test_create_booking_by_week(logged_in, user):
    response = post_booking(logged_in, reverse("planning:booking_create"), project_no=" 2026rsd09tl ")
    assert response.status_code == 302
    assert response["Location"] == reverse("planning:plan") + "?year=2026"
    booking = Booking.objects.get()
    assert booking.project_no == "2026RSD09TL" and booking.engineers == "Martijn, Naresh"
    assert (booking.start, booking.end) == (weeks.monday(2026, 10), weeks.sunday(2026, 20))
    assert booking.created_by == user
    assert message_texts(response) == ["Booking of 2026RSD09TL (Unit B, w10 – w20 2026) saved."]


def test_overlap_shown_on_the_form(logged_in, user):
    book(user, "2026RSD03TL", "B", 18, 30)
    response = post_booking(logged_in, reverse("planning:booking_create"))
    assert response.status_code == 200
    assert "Unit B is booked by 2026RSD03TL" in response.content.decode()
    assert Booking.objects.count() == 1


def test_last_week_before_first_shown_on_the_field(logged_in):
    response = post_booking(logged_in, reverse("planning:booking_create"), start_week="2026-W20", end_week="2026-W10")
    assert response.status_code == 200
    assert response.context["form"].errors["end_week"] == ["The last week lies before the first week."]


def test_edit_form_shows_the_stored_weeks_and_moves_the_booking(logged_in, user):
    booking = book(user, "2026RSD03TL", "A", 10, 20)
    url = reverse("planning:booking_edit", args=[booking.pk])
    form = logged_in.get(url).context["form"]
    assert (form["start_week"].value(), form["end_week"].value()) == ("2026-W10", "2026-W20")
    post_booking(logged_in, url, project_no="2026RSD03TL", unit=TestUnit.objects.get(code="C").pk,
                 start_week="2026-W12", end_week="2027-W02")  # fmt: skip
    booking.refresh_from_db()
    assert (booking.unit.code, booking.weeks_text) == ("C", "w12 2026 – w2 2027")


def test_new_booking_from_a_unit_row_preselects_unit_and_week(logged_in):
    unit = TestUnit.objects.get(code="C")
    form = logged_in.get(reverse("planning:booking_create"), {"unit": unit.pk, "year": "2027"}).context["form"]
    assert form["unit"].value() == str(unit.pk)
    assert form["start_week"].value() == "2027-W01"


def test_inactive_unit_takes_no_new_bookings(logged_in):
    TestUnit.objects.filter(code="D").update(active=False)
    form = logged_in.get(reverse("planning:booking_create")).context["form"]
    assert [u.code for u in form.fields["unit"].queryset] == ["A", "B", "C"]


def test_delete_booking(logged_in, user):
    booking = book(user, "2026RSD03TL", "A", 10, 20)
    response = logged_in.post(reverse("planning:booking_delete", args=[booking.pk]))
    assert response.status_code == 302 and not Booking.objects.exists()


def test_block_create_and_delete(logged_in, user):
    response = logged_in.post(reverse("planning:block_create"), {
        "unit": TestUnit.objects.get(code="D").pk, "kind": "calibration",
        "start_week": "2026-W30", "end_week": "2026-W31", "note": "Torque meter"})  # fmt: skip
    assert response.status_code == 302
    item = UnitBlock.objects.get()
    assert message_texts(response) == ["Calibration of Unit D (w30 – w31 2026) saved."]
    logged_in.post(reverse("planning:block_delete", args=[item.pk]))
    assert not UnitBlock.objects.exists()


def bars(html):
    """(class, grid-column) of every bar on the plan."""
    return re.findall(r'class="bar ([^"]*)" style="grid-column: (\d+ / \d+)"', html)


def test_lab_plan_draws_the_bars_of_the_year(logged_in, user):
    make_project(user, project_no="2026RSD03TL")
    book(user, "2026RSD03TL", "B", 10, 20, status=Booking.Status.CONFIRMED, engineers="Ali, Montadhar")
    Booking(project_no="2026RSD07TL", description="Crosses the year", unit=TestUnit.objects.get(code="B"),
            start=weeks.monday(2026, 48), end=weeks.sunday(2027, 4), created_by=user, updated_by=user).save()
    block(user, "D", 30, 31)
    book(user, "2026RSD08TL", "C", 5, 6, status=Booking.Status.CANCELLED)

    html = logged_in.get(reverse("planning:plan"), {"year": 2026}).content.decode()
    # Weeks start in grid column 4 (after Project, Description, PE(s)); a bar ends after its last week.
    assert bars(html) == [("confirmed", "13 / 24"), ("tentative clip-after", "51 / 57"), ("block", "33 / 35")]
    assert "Ali, Montadhar" in html
    assert 'href="/projects/' in html  # 2026RSD03TL exists in TestLab: linked
    assert "17 of 53 weeks booked" in html  # unit B: w10–w20 and w48–w53 of 2026
    html = logged_in.get(reverse("planning:plan"), {"year": 2027}).content.decode()
    assert bars(html) == [("tentative clip-before", "4 / 8")]
    html = logged_in.get(reverse("planning:plan"), {"year": 2026, "cancelled": "1"}).content.decode()
    assert ("cancelled", "8 / 10") in bars(html)


def test_lab_plan_marks_the_current_week(logged_in, monkeypatch):
    import datetime

    from django.utils import timezone

    monkeypatch.setattr(timezone, "localdate", lambda: datetime.date(2026, 10, 24))
    html = logged_in.get(reverse("planning:plan")).content.decode()
    assert "Lab plan 2026" in html and "This week (w43)" in html
    assert 'class="week-no now">w43<' in html


def test_years_before_2026_are_not_shown(logged_in):
    assert logged_in.get(reverse("planning:plan"), {"year": 2023}).context["year"] == 2026

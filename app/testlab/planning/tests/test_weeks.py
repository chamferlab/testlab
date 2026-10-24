import datetime

import pytest

from testlab.planning import weeks


def test_monday_and_sunday_of_a_week():
    assert weeks.monday(2026, 45) == datetime.date(2026, 11, 2)
    assert weeks.sunday(2026, 45) == datetime.date(2026, 11, 8)


def test_week_one_can_start_in_the_previous_year():
    assert weeks.monday(2026, 1) == datetime.date(2025, 12, 29)
    assert weeks.week_of(datetime.date(2025, 12, 31)) == (2026, 1)


def test_53_week_years():
    assert weeks.weeks_in_year(2026) == 53
    assert weeks.weeks_in_year(2027) == 52
    assert weeks.sunday(2026, 53) == datetime.date(2027, 1, 3)
    assert weeks.week_of(datetime.date(2027, 1, 3)) == (2026, 53)


@pytest.mark.parametrize("value, expected", [("2026-W45", (2026, 45)), (" 2026-w05 ", (2026, 5)), ("2026-W53", (2026, 53))])
def test_parse(value, expected):
    assert weeks.parse(value) == expected


@pytest.mark.parametrize("value", ["2027-W53", "2026-W0", "2026", "w45", ""])
def test_parse_refuses_weeks_that_do_not_exist(value):
    with pytest.raises(ValueError):
        weeks.parse(value)


def test_span_text():
    assert weeks.span_text(weeks.monday(2026, 10), weeks.sunday(2026, 20)) == "w10 – w20 2026"
    assert weeks.span_text(weeks.monday(2026, 7), weeks.sunday(2026, 7)) == "w7 2026"
    assert weeks.span_text(weeks.monday(2026, 50), weeks.sunday(2027, 3)) == "w50 2026 – w3 2027"


def test_week_choices_are_grouped_by_year_with_dates():
    groups = weeks.week_choices(2026, 2027)
    assert [year for year, _ in groups] == ["2026", "2027"]
    assert len(groups[0][1]) == 53 and len(groups[1][1]) == 52
    assert groups[0][1][0] == ("2026-W01", "w1 · 29 Dec – 4 Jan 2026")

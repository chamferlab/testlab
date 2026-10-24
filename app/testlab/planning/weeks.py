"""ISO weeks: planning is entered and shown by week (w1–w52/53), stored as dates.

A planned period starts on the Monday of its first week and ends on the
Sunday of its last week.
"""

import datetime

FIRST_YEAR = 2026  # planning starts with 2026; no earlier years are entered


def monday(year, week):
    return datetime.date.fromisocalendar(year, week, 1)


def sunday(year, week):
    return datetime.date.fromisocalendar(year, week, 7)


def weeks_in_year(year):
    # 28 December always lies in the last ISO week of its year.
    return datetime.date(year, 12, 28).isocalendar().week


def week_of(day):
    """(ISO year, week) of a date."""
    iso = day.isocalendar()
    return iso.year, iso.week


def label(year, week):
    """`2026-W45`: the value of a week field."""
    return f"{year}-W{week:02d}"


def parse(value):
    """(year, week) from `2026-W45`; ValueError when it is not an existing week."""
    year, _, week = (value or "").strip().upper().partition("-W")
    year, week = int(year), int(week)
    if not 1 <= week <= weeks_in_year(year):
        raise ValueError(f"{value} is not a week of {year}.")
    return year, week


def span_text(start, end):
    """`w45 2026 – w3 2027` (`w45 – w50 2026` within one year) for a stored period."""
    (y1, w1), (y2, w2) = week_of(start), week_of(end)
    if y1 == y2:
        return f"w{w1} – w{w2} {y1}" if w1 != w2 else f"w{w1} {y1}"
    return f"w{w1} {y1} – w{w2} {y2}"


def week_choices(first_year, last_year):
    """Grouped choices for a week select: one group per year, each week with its dates."""
    groups = []
    for year in range(first_year, last_year + 1):
        weeks = []
        for week in range(1, weeks_in_year(year) + 1):
            start, end = monday(year, week), sunday(year, week)
            weeks.append((label(year, week), f"w{week} · {_day(start)} – {_day(end)} {end.year}"))
        groups.append((str(year), weeks))
    return groups


def _day(day):
    return f"{day.day} {day:%b}"

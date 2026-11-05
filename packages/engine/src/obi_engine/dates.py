"""Date filter values: presets and explicit ranges, resolved against an `as_of` date."""

from __future__ import annotations

from datetime import date, timedelta

PRESETS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month",
           "this_quarter", "last_quarter", "this_year", "last_year", "year_to_date",
           "last_7_days", "last_30_days", "last_90_days", "last_12_months")

DateRange = tuple[date, date]  # inclusive


def _month_start(d: date, back: int = 0) -> date:
    m = d.year * 12 + d.month - 1 - back
    return date(m // 12, m % 12 + 1, 1)


def _month_end(d: date) -> date:
    return _month_start(d, -1) - timedelta(days=1)


def resolve(value: str, as_of: date) -> DateRange:
    """`this_year`, `2026-01-31`, or `2026-01-01..2026-03-31` -> inclusive range."""
    if ".." in value:
        a, b = value.split("..", 1)
        return date.fromisoformat(a), date.fromisoformat(b)
    if value not in PRESETS:
        d = date.fromisoformat(value)
        return d, d
    t = as_of
    q_start = date(t.year, 3 * ((t.month - 1) // 3) + 1, 1)
    return {
        "today": (t, t),
        "yesterday": (t - timedelta(1), t - timedelta(1)),
        "this_week": (t - timedelta(t.weekday()), t - timedelta(t.weekday()) + timedelta(6)),
        "last_week": (t - timedelta(t.weekday() + 7), t - timedelta(t.weekday() + 1)),
        "this_month": (_month_start(t), _month_end(t)),
        "last_month": (_month_start(t, 1), _month_start(t) - timedelta(1)),
        "this_quarter": (q_start, _month_end(_month_start(q_start, -2))),
        "last_quarter": (_month_start(q_start, 3), q_start - timedelta(1)),
        "this_year": (date(t.year, 1, 1), date(t.year, 12, 31)),
        "last_year": (date(t.year - 1, 1, 1), date(t.year - 1, 12, 31)),
        "year_to_date": (date(t.year, 1, 1), t),
        "last_7_days": (t - timedelta(6), t),
        "last_30_days": (t - timedelta(29), t),
        "last_90_days": (t - timedelta(89), t),
        # Twelve whole months including the current one, so monthly charts have no partial first bar.
        "last_12_months": (_month_start(t, 11), _month_end(t)),
    }[value]


def shift_year(r: DateRange, years: int = -1) -> DateRange:
    def s(d: date) -> date:
        try:
            return d.replace(year=d.year + years)
        except ValueError:  # 29 February
            return d.replace(year=d.year + years, day=28)
    return s(r[0]), s(r[1])


def shift_period(r: DateRange) -> DateRange:
    """The range of equal length that ends the day before `r` starts."""
    days = (r[1] - r[0]).days + 1
    return r[0] - timedelta(days), r[0] - timedelta(1)


def label(value: str, r: DateRange) -> str:
    if value in PRESETS:
        return value.replace("_", " ").capitalize()
    return r[0].isoformat() if r[0] == r[1] else f"{r[0].isoformat()} – {r[1].isoformat()}"

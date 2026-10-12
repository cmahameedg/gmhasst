"""Return periods and due dates.

Periods follow the Indian financial year (April-March). A generation month
(e.g. October 2026) produces a filing for every return whose period *ends*
in that month: monthly returns every month, quarterly returns in June,
September, December and March, half-yearly in September and March, annual
in March. "Months 1 and 2 of the quarter" returns (IFF, PMT-06) are produced
in the first two months of each quarter only.

Due dates are never hard-coded: each return type carries a rule (day of
month + months after the period end, or days after an event such as the
AGM) with optional per-period overrides and one-off extensions, all stored
in the Return Master.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

MONTHLY = "monthly"
MONTHLY_Q12 = "monthly_q12"
QUARTERLY = "quarterly"
HALF_YEARLY = "half_yearly"
ANNUAL = "annual"

FREQUENCIES = {
    MONTHLY: "Monthly",
    MONTHLY_Q12: "Monthly (months 1 and 2 of quarter)",
    QUARTERLY: "Quarterly",
    HALF_YEARLY: "Half-yearly",
    ANNUAL: "Annual",
}

MONTH_ABBR = [None, "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str
    code: str  # key used by due-date overrides: M01-M12, Q1-Q4, H1-H2, Y


def month_key(year, month):
    return f"{year:04d}-{month:02d}"


def parse_month_key(key):
    year, month = key.split("-")
    year, month = int(year), int(month)
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        raise ValueError(key)
    return year, month


def month_label(key):
    year, month = parse_month_key(key)
    return f"{calendar.month_name[month]} {year}"


def add_months(year, month, n):
    index = year * 12 + (month - 1) + n
    return index // 12, index % 12 + 1


def last_day(year, month):
    return calendar.monthrange(year, month)[1]


def fy_start_year(year, month):
    return year if month >= 4 else year - 1


def fy_label(year, month):
    start = fy_start_year(year, month)
    return f"FY {start}-{str(start + 1)[2:]}"


def fy_quarter(month):
    """1 for Apr-Jun ... 4 for Jan-Mar."""
    return ((month - 4) % 12) // 3 + 1


def _span(year, month, months):
    """Period of ``months`` length ending in year/month."""
    sy, sm = add_months(year, month, -(months - 1))
    return date(sy, sm, 1), date(year, month, last_day(year, month))


def period_for(frequency, year, month):
    """The period of this frequency that ends in year/month, or None if none does."""
    position_in_quarter = (month - 4) % 3 + 1  # 1, 2 or 3
    if frequency == MONTHLY or (frequency == MONTHLY_Q12 and position_in_quarter in (1, 2)):
        start, end = _span(year, month, 1)
        return Period(start, end, f"{MONTH_ABBR[month]} {year}", f"M{month:02d}")
    if frequency == QUARTERLY and position_in_quarter == 3:
        start, end = _span(year, month, 3)
        q = fy_quarter(month)
        label = f"Q{q} {fy_label(year, month)} ({MONTH_ABBR[start.month]}–{MONTH_ABBR[month]} {year})"
        return Period(start, end, label, f"Q{q}")
    if frequency == HALF_YEARLY and month in (9, 3):
        start, end = _span(year, month, 6)
        h = 1 if month == 9 else 2
        return Period(start, end, f"H{h} {fy_label(year, month)} ({MONTH_ABBR[start.month]}–{MONTH_ABBR[month]} {year})", f"H{h}")
    if frequency == ANNUAL and month == 3:
        start, end = _span(year, month, 12)
        return Period(start, end, fy_label(year, month), "Y")
    return None


def override_codes(frequency):
    """Period codes a due-date override can target, with labels, for the master form."""
    if frequency in (MONTHLY, MONTHLY_Q12):
        return [(f"M{m:02d}", f"{calendar.month_name[m]} period") for m in (4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3)]
    if frequency == QUARTERLY:
        return [("Q1", "Q1 (Apr–Jun)"), ("Q2", "Q2 (Jul–Sep)"), ("Q3", "Q3 (Oct–Dec)"), ("Q4", "Q4 (Jan–Mar)")]
    if frequency == HALF_YEARLY:
        return [("H1", "H1 (Apr–Sep)"), ("H2", "H2 (Oct–Mar)")]
    return [("Y", "Year")]


def day_in_month(year, month, day):
    """``day`` of the month, clamped to its last day (31 in a 30-day month -> 30)."""
    return date(year, month, min(day, last_day(year, month)))


def due_on(period_end, day, month_offset):
    year, month = add_months(period_end.year, period_end.month, month_offset)
    return day_in_month(year, month, day)


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_rule(day, month_offset):
    when = {0: "of the period's last month", 1: "of the next month"}.get(
        month_offset, f"of the {ordinal(month_offset)} month after the period"
    )
    return f"{'Last day' if day >= 31 else ordinal(day)} {when}"


def add_days(d, days):
    return d + timedelta(days=days)

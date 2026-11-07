"""Date helpers: parsing common invoice date formats and resolving relative periods."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MONTHS = {
    name.lower(): i
    for i, name in enumerate(calendar.month_name)
    if name
} | {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name} | {"sept": 9}

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_DMY_TEXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY_TEXT = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, day_first: bool = True) -> list[date]:
    """Return dates in order of appearance."""
    found: list[tuple[int, date]] = []
    for m in _ISO.finditer(text):
        if d := _safe(int(m[1]), int(m[2]), int(m[3])):
            found.append((m.start(), d))
    for m in _NUMERIC.finditer(text):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        day, month = (a, b) if day_first else (b, a)
        if month > 12:  # unambiguous swap
            day, month = month, day
        if d := _safe(y, month, day):
            found.append((m.start(), d))
    for m in _DMY_TEXT.finditer(text):
        mon = MONTHS.get(m[2].lower())
        if mon and (d := _safe(int(m[3]), mon, int(m[1]))):
            found.append((m.start(), d))
    for m in _MDY_TEXT.finditer(text):
        mon = MONTHS.get(m[1].lower())
        if mon and (d := _safe(int(m[3]), mon, int(m[2]))):
            found.append((m.start(), d))
    found.sort(key=lambda t: t[0])
    seen: list[date] = []
    for _, d in found:
        if d not in seen:
            seen.append(d)
    return seen


def month_range(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def quarter_range(year: int, q: int) -> tuple[date, date]:
    start_month = 3 * (q - 1) + 1
    return date(year, start_month, 1), month_range(year, start_month + 2)[1]


def resolve_period(text: str, today: date) -> tuple[date | None, date | None]:
    """Best-effort relative/absolute period resolution for the offline planner."""
    t = text.lower()
    if "today" in t:
        return today, today
    if "yesterday" in t:
        yday = today - timedelta(days=1)
        return yday, yday
    if "last month" in t or "previous month" in t:
        first = today.replace(day=1) - timedelta(days=1)
        return month_range(first.year, first.month)
    if "this month" in t:
        return month_range(today.year, today.month)
    current_q = (today.month - 1) // 3 + 1
    if "last quarter" in t:
        q, y = (current_q - 1, today.year) if current_q > 1 else (4, today.year - 1)
        return quarter_range(y, q)
    if "this quarter" in t:
        return quarter_range(today.year, current_q)
    if "last year" in t:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if "this year" in t:
        return date(today.year, 1, 1), today
    if m := re.search(r"last (\d{1,3}) days", t):
        return today - timedelta(days=int(m[1])), today

    year_match = re.search(r"\b(20\d{2})\b", t)
    for word in re.findall(r"[a-z]+", t):
        if word in MONTHS and len(word) >= 3 and word != "may":
            year = int(year_match[1]) if year_match else today.year
            month = MONTHS[word]
            if not year_match and month > today.month:
                year -= 1  # "in November" asked in March means last November
            return month_range(year, month)
    # "may" is also a verb, so only treat it as a month in an explicit date context.
    if re.search(r"\b(?:in|of|during|for)\s+may\b|\bmay\s+20\d{2}\b", t):
        year = int(year_match[1]) if year_match else (today.year if today.month >= 5 else today.year - 1)
        return month_range(year, 5)
    if year_match:
        yr = int(year_match[1])
        return date(yr, 1, 1), date(yr, 12, 31)
    return None, None

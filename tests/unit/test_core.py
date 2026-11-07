from datetime import date

from app.core.dates import find_dates, resolve_period
from app.core.logging import redact
from app.core.security import SlidingWindowRateLimiter, compute_signature, verify_whatsapp_signature


def test_signature_roundtrip():
    body = b'{"hello":"world"}'
    sig = compute_signature(body, "s3cret")
    assert verify_whatsapp_signature(body, sig, "s3cret")
    assert not verify_whatsapp_signature(body, sig, "other")
    assert not verify_whatsapp_signature(body + b" ", sig, "s3cret")
    assert not verify_whatsapp_signature(body, None, "s3cret")
    assert not verify_whatsapp_signature(body, "md5=abc", "s3cret")


def test_rate_limiter():
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=60)
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")


def test_redact_phone_numbers():
    assert redact("from +91 98765 43210 ok") == "from ***3210 ok"


def test_find_dates_formats():
    assert find_dates("Date: 2026-03-05") == [date(2026, 3, 5)]
    assert find_dates("Date: 12/03/2026") == [date(2026, 3, 12)]
    assert find_dates("Date: 12/03/2026", day_first=False) == [date(2026, 12, 3)]
    assert find_dates("Issued 5th March 2026, due April 4, 2026") == [date(2026, 3, 5), date(2026, 4, 4)]
    assert find_dates("13/25/2026") == []  # impossible either way


def test_resolve_period():
    today = date(2026, 11, 7)
    assert resolve_period("spend last month", today) == (date(2026, 10, 1), date(2026, 10, 31))
    assert resolve_period("this year", today) == (date(2026, 1, 1), today)
    assert resolve_period("in march 2025", today) == (date(2025, 3, 1), date(2025, 3, 31))
    assert resolve_period("in december", today) == (date(2025, 12, 1), date(2025, 12, 31))
    assert resolve_period("last quarter", today) == (date(2026, 7, 1), date(2026, 9, 30))
    assert resolve_period("total in may", today) == (date(2026, 5, 1), date(2026, 5, 31))
    assert resolve_period("may I see unpaid bills", today) == (None, None)
    assert resolve_period("anything", today) == (None, None)

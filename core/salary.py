"""Parse salary text as written by sources, keeping the original currency and period.

Examples seen in real listings: "$215K – $250K • Offers Equity", "CA$215K – CA$295K",
"₹2M - ₹3.5M INR", "₹8L - ₹15L", "$6K - $7.5K / monthly", "$40 – $50 per hour",
"SGD 335K – SGD 405K". Ambiguous text (e.g. "$220 - $285" with no period) parses to
None rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Longest prefixes first so "CA$" is not read as "$".
_PREFIX_CURRENCIES = [
    ("CA$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"), ("R$", "BRL"), ("MX$", "MXN"), ("S$", "SGD"), ("HK$", "HKD"),
    ("NZ$", "NZD"), ("US$", "USD"), ("$", "USD"), ("£", "GBP"), ("€", "EUR"), ("₹", "INR"), ("Rs.", "INR"),
    ("Rs", "INR"), ("₩", "KRW"), ("¥", "JPY"),
]
_CODE = re.compile(r"\b(USD|INR|EUR|GBP|CAD|AUD|SGD|AED|SAR|KRW|JPY|CHF|BRL|MXN|HKD|NZD)\b")
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|m|mn|l|lpa|lakhs?|cr|crores?)?\b", re.I)
_MULTIPLIERS = {"k": 1e3, "m": 1e6, "mn": 1e6, "l": 1e5, "lpa": 1e5, "lakh": 1e5, "lakhs": 1e5,
                "cr": 1e7, "crore": 1e7, "crores": 1e7}
_PERIODS = [
    ("hour", re.compile(r"hour|/\s*hr\b|hourly|\bph\b", re.I)),
    ("day", re.compile(r"\bday\b|daily", re.I)),
    ("week", re.compile(r"week", re.I)),
    ("month", re.compile(r"month|/\s*mo\b|\bpm\b", re.I)),
    ("year", re.compile(r"year|annum|annual|/\s*yr\b|\bpa\b|lpa", re.I)),
]
PERIODS_PER_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}


@dataclass(frozen=True)
class Salary:
    min: float
    max: float
    currency: str
    period: str  # hour | day | week | month | year


def _currency(text: str) -> str | None:
    if m := _CODE.search(text):
        return m.group(1)
    stripped = text.strip()
    for prefix, code in _PREFIX_CURRENCIES:
        if prefix in stripped:
            return code
    return None


def _period(text: str) -> str | None:
    for name, pattern in _PERIODS:
        if pattern.search(text):
            return name
    return None


def _number(raw: str, suffix: str | None) -> float:
    # "108,300" -> 108300; "3.5" -> 3.5; European "1.200.000" -> 1200000
    if raw.count(",") or raw.count(".") > 1:
        raw = raw.replace(",", "").replace(".", "") if raw.count(".") > 1 else raw.replace(",", "")
    value = float(raw)
    return value * _MULTIPLIERS.get((suffix or "").lower(), 1)


def parse_salary_text(text: str | None) -> Salary | None:
    if not text:
        return None
    main = text.split("•")[0]
    currency = _currency(main)
    amounts = [_number(n, s) for n, s in _AMOUNT.findall(main)]
    amounts = [a for a in amounts if a > 0]
    if not currency or not amounts:
        return None
    period = _period(main)
    if period is None:
        # No period stated: only trust amounts that are plainly annual.
        if min(amounts) < 1000:
            return None
        period = "year"
    low, high = min(amounts[:2]), max(amounts[:2])
    return Salary(low, high, currency, period)


def _lever_period(interval: str | None) -> str:
    for name, pattern in _PERIODS:
        if interval and pattern.search(interval):
            return name
    return "year"


def parse_salary(text: str | None, *, min_value=None, max_value=None, currency=None, period=None) -> Salary | None:
    """Prefer structured fields (Lever), else parse the text."""
    if currency and (min_value or max_value):
        low = float(min_value or max_value)
        high = float(max_value or min_value)
        return Salary(low, high, currency.upper(), _lever_period(period))
    return parse_salary_text(text)


def to_inr_per_year(salary: Salary, inr_per_unit: dict[str, float]) -> tuple[float, float] | None:
    """Approximate annual INR range, or None if the currency has no configured rate."""
    rate = inr_per_unit.get(salary.currency)
    if rate is None:
        return None
    factor = rate * PERIODS_PER_YEAR[salary.period]
    return salary.min * factor, salary.max * factor


def format_inr_lpa(amount: float) -> str:
    return f"₹{amount / 1e5:.1f} LPA"

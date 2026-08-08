"""British Columbia PST rules: the rate, the commission table, and the calendar.

Figures here come from the Province of B.C.'s PST return guide and the FIN 400
return itself. They are the only numbers in this project that are not derived
from the store's own data, so they are kept together and cited.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal("0.01")

#: B.C. general PST rate on retail sales of goods.
PST_RATE = Decimal("0.07")

#: Federal GST, present on the same invoices. Tracked only so it can be
#: separated out -- it is never part of a PST return.
GST_RATE = Decimal("0.05")

# Commission for filing and paying on time, per reporting period.
# https://www2.gov.bc.ca/gov/content/taxes/sales-taxes/pst/report-pay/pst-return-guide
COMMISSION_FULL_CREDIT_CEILING = Decimal("333.33")  # 6.6% of this is exactly $22.00
COMMISSION_FLAT = Decimal("22.00")
COMMISSION_RATE = Decimal("0.066")
COMMISSION_MAX = Decimal("198.00")


def round_cents(value: Decimal) -> Decimal:
    """Round to the nearest cent, halves upward, as tax reporting expects."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def pst_on(taxable_amount: Decimal) -> Decimal:
    """PST payable on a taxable sale amount."""
    return round_cents(taxable_amount * PST_RATE)


def commission(pst_collectable: Decimal) -> Decimal:
    """Commission earned for filing and remitting on time.

    The table is a three-step schedule: the smallest filers keep the whole
    amount, a flat $22.00 applies through the middle band, and above that it
    is 6.6% capped at $198.00. The bands meet cleanly -- 6.6% of $333.33 is
    $22.00, and the cap binds from $3,000.00 of PST upward.

    Late filers get nothing, so callers decide whether to apply this at all.
    """
    if pst_collectable <= Decimal("0.00"):
        return Decimal("0.00")
    if pst_collectable <= COMMISSION_FLAT:
        return round_cents(pst_collectable)
    if pst_collectable <= COMMISSION_FULL_CREDIT_CEILING:
        return COMMISSION_FLAT
    return min(round_cents(pst_collectable * COMMISSION_RATE), COMMISSION_MAX)


@dataclass(frozen=True)
class Quarter:
    """A calendar reporting quarter and the dates that hang off it."""

    year: int
    number: int  # 1-4

    @property
    def start(self) -> date:
        return date(self.year, (self.number - 1) * 3 + 1, 1)

    @property
    def end(self) -> date:
        month = self.number * 3
        return date(self.year, month, monthrange(self.year, month)[1])

    @property
    def statutory_due_date(self) -> date:
        """Last day of the month following the end of the reporting period."""
        month = self.end.month + 1
        year = self.end.year
        if month > 12:
            month, year = 1, year + 1
        return date(year, month, monthrange(year, month)[1])

    @property
    def due_date(self) -> date:
        """Due date after rolling off a weekend to the next business day.

        B.C. statutory holidays are not checked because none of them can fall
        on a quarterly due date: those dates are always Jan 31, Apr 30, Jul 31
        or Oct 31, and no B.C. stat holiday lands on any of them.
        """
        due = self.statutory_due_date
        while due.weekday() >= 5:  # Saturday or Sunday
            due += timedelta(days=1)
        return due

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    @property
    def label(self) -> str:
        return f"Q{self.number} {self.year}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.label


def quarter_of(day: date) -> Quarter:
    return Quarter(day.year, (day.month - 1) // 3 + 1)


def parse_quarter(text: str) -> Quarter:
    """Accept the forms a person would actually type: 2026Q3, Q3-2026, 2026-Q3."""
    cleaned = re.sub(r"[\s\-_/]", "", str(text).strip().upper())

    match = re.fullmatch(r"(\d{4})Q(\d)", cleaned)
    if match:
        year, number = int(match.group(1)), int(match.group(2))
    else:
        match = re.fullmatch(r"Q(\d)(\d{4})", cleaned)
        if not match:
            raise ValueError(f"could not read a quarter from {text!r}; try 2026Q3")
        number, year = int(match.group(1)), int(match.group(2))

    if not 1 <= number <= 4:
        raise ValueError(f"quarter must be 1-4, got {number}")
    return Quarter(year, number)

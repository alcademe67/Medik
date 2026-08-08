"""Tolerant parsing of the money and date strings that spreadsheet exports contain.

Invoice2go's CSV export is generated for humans, not machines: amounts arrive
with currency symbols and thousands separators, credit notes are sometimes
parenthesised instead of signed, and the date format follows whatever locale
the account was set up in. Everything here exists to turn that into Decimal
and date without silently guessing wrong.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = Decimal("0.01")

# Strip currency symbols, thousands separators, and stray whitespace, but keep
# the characters that carry meaning: digits, sign, decimal point, parentheses.
_MONEY_NOISE = re.compile(r"[^\d.\-()]")

_DATE_FORMATS_UNAMBIGUOUS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d-%B-%Y",
    "%B %d, %Y",
)

# Matches the numeric-with-separator forms whose field order we cannot know
# without looking at the whole column: 03/04/2026 is either 3 April or 4 March.
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")


class ParseError(ValueError):
    """Raised when a value cannot be interpreted at all."""


def parse_money(raw: object, *, field: str = "amount") -> Decimal:
    """Parse a spreadsheet money cell into a Decimal rounded to cents.

    Blank cells mean zero -- exports leave the PST column empty rather than
    writing 0.00 on an exempt sale. Parenthesised values are negative, which
    is how credit notes usually come across.
    """
    if raw is None:
        return Decimal("0.00")
    if isinstance(raw, Decimal):
        return raw.quantize(CENTS, rounding=ROUND_HALF_UP)

    text = str(raw).strip()
    if not text or text in {"-", "--", "N/A", "n/a"}:
        return Decimal("0.00")

    cleaned = _MONEY_NOISE.sub("", text)
    negative = "(" in cleaned and ")" in cleaned
    cleaned = cleaned.replace("(", "").replace(")", "")
    # A non-blank cell that holds no digits is not a zero, it is a column
    # that has been read wrongly. Say so rather than quietly totalling nothing.
    if not cleaned or cleaned in {"-", "."}:
        raise ParseError(f"could not read {field} from {raw!r}")

    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"could not read {field} from {raw!r}") from exc

    if negative:
        value = -value
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _try_unambiguous(text: str) -> date | None:
    for fmt in _DATE_FORMATS_UNAMBIGUOUS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def detect_date_order(samples: list[str]) -> str:
    """Work out whether a column of numeric dates is day-first or month-first.

    A single date like 03/04/2026 is ambiguous, but a column of them usually
    is not: as soon as one row has a first field above 12 the column must be
    day-first, and vice versa. Returns "dmy", "mdy", or "ambiguous" when the
    whole column happens to fit both readings.
    """
    saw_day_first = False
    saw_month_first = False

    for text in samples:
        match = _NUMERIC_DATE.match(str(text or ""))
        if not match:
            continue
        first, second, third = (int(part) for part in match.groups())
        if len(match.group(1)) == 4:
            # Already year-first, which is unambiguous; ignore for voting.
            continue
        if first > 12 and second <= 12:
            saw_day_first = True
        elif second > 12 and first <= 12:
            saw_month_first = True
        del third

    if saw_day_first and saw_month_first:
        # Genuinely inconsistent data -- neither reading works for every row.
        return "ambiguous"
    if saw_day_first:
        return "dmy"
    if saw_month_first:
        return "mdy"
    return "ambiguous"


def parse_date(raw: object, *, order: str = "dmy", field: str = "date") -> date:
    """Parse a date cell. `order` disambiguates purely numeric formats."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = str(raw or "").strip()
    if not text:
        raise ParseError(f"missing {field}")

    # Exports sometimes append a time; the date half is all we need.
    text = text.split("T")[0].strip()
    if " " in text and _NUMERIC_DATE.match(text.split(" ")[0]):
        text = text.split(" ")[0]

    found = _try_unambiguous(text)
    if found is not None:
        return found

    match = _NUMERIC_DATE.match(text)
    if not match:
        raise ParseError(f"could not read {field} from {raw!r}")

    first, second, third = (int(part) for part in match.groups())
    if len(match.group(1)) == 4:
        year, month, day = first, second, third
    else:
        year = third + 2000 if third < 100 else third
        day, month = (first, second) if order == "dmy" else (second, first)

    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"could not read {field} from {raw!r}: {exc}") from exc

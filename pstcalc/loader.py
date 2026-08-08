"""Read an Invoice2go CSV export into Invoice records."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .bcpst import pst_on, round_cents
from .columns import ColumnMap, build_column_map, classify_tax_label
from .parsing import ParseError, detect_date_order, parse_date, parse_money

# Statuses that mean the document never became a real sale. PST is not
# collectable on these and they must stay out of the return entirely.
VOID_STATUSES = {"void", "voided", "cancelled", "canceled", "draft", "deleted"}

# Statuses that mark a document as a quote rather than an invoice. Some
# exports mix both into one file.
QUOTE_STATUSES = {"quote", "estimate", "proposal", "pending"}


@dataclass
class Invoice:
    """One invoice from the export, normalised."""

    number: str
    invoice_date: date
    customer: str
    subtotal: Decimal
    pst: Decimal
    gst: Decimal
    total: Decimal
    status: str = ""
    currency: str = "CAD"
    exempt_reason: str = ""
    source_row: int = 0

    @property
    def is_void(self) -> bool:
        return self.status.strip().lower() in VOID_STATUSES

    @property
    def is_quote(self) -> bool:
        return self.status.strip().lower() in QUOTE_STATUSES

    @property
    def is_credit(self) -> bool:
        return self.total < 0 or self.subtotal < 0

    @property
    def charged_pst(self) -> bool:
        return self.pst != 0

    @property
    def expected_pst(self) -> Decimal:
        """PST at 7% of the subtotal, i.e. what a fully taxable sale would carry."""
        return pst_on(self.subtotal)

    @property
    def implied_rate(self) -> Decimal | None:
        if self.subtotal == 0:
            return None
        return (self.pst / self.subtotal).quantize(Decimal("0.0001"))


@dataclass
class LoadResult:
    invoices: list[Invoice] = field(default_factory=list)
    column_map: ColumnMap | None = None
    date_order: str = "dmy"
    warnings: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read the CSV, tolerating a UTF-8 BOM and preamble rows above the header.

    Some Invoice2go reports put a title and a date range above the real header
    row, so the first line is not always the headings.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))

    if not raw_rows:
        raise ValueError(f"{path} is empty")

    header_index = 0
    best_score = -1
    # The header is the earliest row with the most non-empty distinct cells,
    # looking only at the first few rows so a wide data row cannot win.
    for index, row in enumerate(raw_rows[:10]):
        cells = [cell.strip() for cell in row if cell.strip()]
        score = len(set(cells))
        if score > best_score:
            best_score, header_index = score, index

    headings = [cell.strip() for cell in raw_rows[header_index]]
    body = raw_rows[header_index + 1:]

    rows: list[dict[str, str]] = []
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        padded = list(row) + [""] * (len(headings) - len(row))
        rows.append({headings[i]: padded[i] for i in range(len(headings))})

    return headings, rows


def _split_named_taxes(
    row: dict[str, str], column_map: ColumnMap
) -> tuple[Decimal | None, Decimal | None]:
    """Pull PST and GST out of paired "Tax N Name"/"Tax N Amount" columns."""
    pst: Decimal | None = None
    gst: Decimal | None = None

    for slot in ("1", "2", "3"):
        name_col = column_map.tax_label_columns.get(f"tax{slot}name") or \
            column_map.tax_label_columns.get(f"tax{slot}label")
        if not name_col:
            continue
        kind = classify_tax_label(row.get(name_col, ""))
        if kind is None:
            continue
        amount_col = None
        for heading in row:
            key = heading.lower().replace(" ", "").replace("_", "")
            if key in (f"tax{slot}amount", f"tax{slot}total", f"tax{slot}"):
                if key != f"tax{slot}" or heading != name_col:
                    amount_col = heading
                    break
        if amount_col is None:
            continue
        amount = parse_money(row.get(amount_col), field=f"tax {slot}")
        if kind == "pst":
            pst = amount if pst is None else pst + amount
        else:
            gst = amount if gst is None else gst + amount

    return pst, gst


def load_invoices(
    path: Path,
    *,
    overrides: dict[str, str] | None = None,
    date_order: str | None = None,
) -> LoadResult:
    """Load and normalise every usable row of the export."""
    headings, rows = _read_rows(path)
    column_map = build_column_map(headings, overrides)
    result = LoadResult(column_map=column_map)

    for required in ("date",):
        if not column_map.has(required):
            raise KeyError(
                f"could not find a {required} column in {path.name}. "
                f"Headings found: {', '.join(headings)}. "
                f"Pin it with \"columns\": {{\"{required}\": \"<heading>\"}} in the config."
            )

    date_col = column_map.get("date")
    if date_order is None:
        samples = [row.get(date_col, "") for row in rows]
        detected = detect_date_order(samples)
        if detected == "ambiguous":
            result.date_order = "dmy"
            result.warnings.append(
                "Every date in this file reads the same whether the format is "
                "day/month or month/day, so day-first was assumed. If the export "
                "is US-formatted, re-run with --date-order mdy."
            )
        else:
            result.date_order = detected
    else:
        result.date_order = date_order

    has_pst_column = column_map.has("pst")
    if not has_pst_column and not column_map.tax_label_columns:
        result.warnings.append(
            "No PST column was found, so PST was derived from the tax total by "
            "separating out GST at 5%. Check a few invoices against the audit "
            "file before filing."
        )

    for offset, row in enumerate(rows, start=2):
        try:
            invoice = _build_invoice(row, column_map, result, offset, date_col)
        except ParseError as exc:
            # One malformed row must not lose the other 300. Skip it, record
            # why, and let the caller show the list.
            result.skipped.append((offset, str(exc)))
            continue
        result.invoices.append(invoice)

    return result


def _build_invoice(
    row: dict[str, str],
    column_map: ColumnMap,
    result: LoadResult,
    offset: int,
    date_col: str,
) -> Invoice:
    """Normalise one export row, raising ParseError if it cannot be read."""
    has_pst_column = column_map.has("pst")

    invoice_date = parse_date(
        row.get(date_col), order=result.date_order, field="invoice date"
    )

    subtotal = parse_money(row.get(column_map.get("subtotal")), field="subtotal")
    total = parse_money(row.get(column_map.get("total")), field="total")

    # A "Tax 1 Name"/"Tax 2 Name" pair states which tax is which, so it
    # outranks any column matched by position. Invoice2go does not guarantee
    # GST occupies slot 1, and trusting the slot silently swaps the two taxes
    # when it does not.
    pst, gst = _split_named_taxes(row, column_map)
    if pst is None and has_pst_column:
        pst = parse_money(row.get(column_map.get("pst")), field="PST")
    if gst is None and column_map.has("gst"):
        gst = parse_money(row.get(column_map.get("gst")), field="GST")

    if pst is None or gst is None:
        tax_total = parse_money(row.get(column_map.get("tax_total")), field="tax")
        if pst is None and gst is None:
            # Only the combined tax is available. On a BC invoice carrying both
            # taxes the split is 5/12 GST and 7/12 PST; on one carrying GST
            # alone the whole amount is GST. Compare against the subtotal to
            # tell those apart instead of assuming.
            both = round_cents(subtotal * Decimal("0.12"))
            gst_only = round_cents(subtotal * Decimal("0.05"))
            if abs(tax_total - both) <= Decimal("0.05"):
                gst = round_cents(subtotal * Decimal("0.05"))
                pst = tax_total - gst
            elif abs(tax_total - gst_only) <= Decimal("0.05"):
                gst, pst = tax_total, Decimal("0.00")
            else:
                gst, pst = Decimal("0.00"), Decimal("0.00")
                result.warnings.append(
                    f"Row {offset}: tax of {tax_total} does not match GST-only "
                    f"or GST+PST on a subtotal of {subtotal}; treated as no PST."
                )
        elif pst is None:
            pst = tax_total - gst
        else:
            gst = tax_total - pst

    if subtotal == 0 and total != 0:
        subtotal = total - pst - gst

    return Invoice(
        number=str(row.get(column_map.get("number"), "") or "").strip(),
        invoice_date=invoice_date,
        customer=str(row.get(column_map.get("customer"), "") or "").strip(),
        subtotal=subtotal,
        pst=pst,
        gst=gst,
        total=total if total != 0 else round_cents(subtotal + pst + gst),
        status=str(row.get(column_map.get("status"), "") or "").strip(),
        currency=(str(row.get(column_map.get("currency"), "") or "CAD").strip()
                  or "CAD").upper(),
        exempt_reason=str(row.get(column_map.get("exempt_reason"), "") or "").strip(),
        source_row=offset,
    )

"""Render the quarter's figures as a filing worksheet and an audit trail."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from .calc import QuarterResult

WIDTH = 74


def _money(value: Decimal) -> str:
    """Format for a worksheet column: thousands separated, negatives in brackets."""
    if value < 0:
        return f"({abs(value):,.2f})"
    return f"{value:,.2f}"


def _line(label: str, value: Decimal, *, indent: int = 2, rule: str = ".") -> str:
    amount = _money(value)
    pad = WIDTH - indent - len(label) - len(amount) - 2
    return f"{' ' * indent}{label} {rule * max(pad, 1)} {amount}"


def worksheet(result: QuarterResult, *, source: str = "", prepared: str = "") -> str:
    """The human-facing summary, laid out in the order the return asks for it."""
    quarter = result.quarter
    out: list[str] = []

    out.append("=" * WIDTH)
    out.append(f"  B.C. PST RETURN WORKSHEET - {quarter.label}".rstrip())
    out.append(
        f"  Reporting period: {quarter.start:%b %-d, %Y} to {quarter.end:%b %-d, %Y}"
    )
    if source:
        out.append(f"  Source: {source}")
    if prepared:
        out.append(f"  Prepared: {prepared}")
    out.append("=" * WIDTH)
    out.append("")

    out.append("  SALES")
    out.append(_line("Taxable sales", result.taxable_sales))
    out.append(_line("Exempt / non-taxable sales", result.exempt_sales))
    out.append(_line("Box A - total sales and leases in Canada", result.total_sales))
    out.append("")

    out.append("  PST")
    out.append(_line("PST collected per the invoices", result.pst_collectable))
    out.append(_line("Cross-check: 7% of taxable sales", result.expected_pst))
    if result.variance != 0:
        out.append(_line("Variance", result.variance))
    out.append("")

    out.append("  REMITTANCE")
    out.append(_line("PST collectable", result.pst_collectable))
    if result.on_time:
        out.append(_line("Less commission (filed and paid on time)", -result.commission))
    else:
        out.append("  Less commission ............ not available on a late return")
    out.append(_line("NET PST TO REMIT", result.net_remittance))
    out.append("")

    due = quarter.due_date
    statutory = quarter.statutory_due_date
    if due != statutory:
        out.append(
            f"  Due {due:%B %-d, %Y} "
            f"({statutory:%B %-d} falls on a {statutory:%A}, so it rolls forward)"
        )
    else:
        out.append(f"  Due {due:%B %-d, %Y}")
    out.append("")

    out.append("  FOR CONTEXT (not part of the PST return)")
    out.append(_line("GST collected in the same period", result.gst_collected))
    out.append(
        f"  Invoices counted: {result.invoice_count}"
        f"  |  credits: {result.credit_count}"
        f"  |  excluded: {result.excluded_count}"
    )
    out.append("")

    if result.findings:
        out.append("-" * WIDTH)
        out.append(f"  REVIEW BEFORE FILING - {len(result.errors)} error(s), "
                   f"{len(result.warnings)} warning(s)")
        out.append("-" * WIDTH)
        for finding in result.errors + result.warnings:
            marker = "!!" if finding.severity == "error" else " -"
            out.append(f"  {marker} {finding.label}: {finding.message}")
        out.append("")
    else:
        out.append("  No discrepancies found. Every taxable invoice carries 7%, and")
        out.append("  every zero-PST sale has a documented reason.")
        out.append("")

    if result.excluded:
        out.append(f"  Excluded from the return ({len(result.excluded)}):")
        for invoice, reason in result.excluded[:20]:
            label = invoice.number or f"row {invoice.source_row}"
            out.append(f"     {label}: {reason}")
        if len(result.excluded) > 20:
            out.append(f"     ... and {len(result.excluded) - 20} more")
        out.append("")

    return "\n".join(out)


AUDIT_HEADINGS = [
    "invoice_number",
    "invoice_date",
    "customer",
    "treatment",
    "subtotal",
    "pst_charged",
    "pst_expected_at_7pct",
    "pst_variance",
    "gst",
    "total",
    "currency",
    "status",
    "exempt_reason",
    "flag",
    "source_row",
]


def write_audit_csv(result: QuarterResult, path: Path) -> None:
    """Write every invoice behind the totals, so the numbers can be traced back."""
    flags = {id(f.invoice): f.message for f in result.findings}

    rows = []
    for invoice in result.taxable_invoices:
        rows.append((invoice, "taxable"))
    for invoice in result.exempt_invoices:
        rows.append((invoice, "exempt"))
    for invoice, reason in result.excluded:
        rows.append((invoice, f"excluded: {reason}"))

    rows.sort(key=lambda pair: (pair[0].invoice_date, pair[0].source_row))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(AUDIT_HEADINGS)
        for invoice, treatment in rows:
            expected = invoice.expected_pst if treatment == "taxable" else Decimal("0.00")
            variance = invoice.pst - expected if treatment == "taxable" else Decimal("0.00")
            writer.writerow([
                invoice.number,
                invoice.invoice_date.isoformat(),
                invoice.customer,
                treatment,
                f"{invoice.subtotal:.2f}",
                f"{invoice.pst:.2f}",
                f"{expected:.2f}",
                f"{variance:.2f}",
                f"{invoice.gst:.2f}",
                f"{invoice.total:.2f}",
                invoice.currency,
                invoice.status,
                invoice.exempt_reason,
                flags.get(id(invoice), ""),
                invoice.source_row,
            ])


def year_summary(results: list[QuarterResult]) -> str:
    """A one-line-per-quarter overview across everything in the file."""
    out = ["", "=" * WIDTH, "  ALL QUARTERS IN THIS FILE", "=" * WIDTH, ""]
    out.append(
        f"  {'Quarter':<10}{'Taxable':>14}{'Exempt':>13}"
        f"{'PST':>12}{'Comm.':>9}{'Net':>12}"
    )
    out.append("  " + "-" * (WIDTH - 4))
    for result in results:
        out.append(
            f"  {result.quarter.label:<10}"
            f"{_money(result.taxable_sales):>14}"
            f"{_money(result.exempt_sales):>13}"
            f"{_money(result.pst_collectable):>12}"
            f"{_money(result.commission):>9}"
            f"{_money(result.net_remittance):>12}"
        )
    out.append("")
    for result in results:
        if result.errors:
            out.append(
                f"  {result.quarter.label} has {len(result.errors)} error(s) "
                f"to review -- run that quarter on its own for detail."
            )
    out.append("")
    return "\n".join(out)

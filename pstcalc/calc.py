"""Turn a quarter's invoices into the figures a FIN 400 return needs.

The arithmetic itself is small. Most of this module is the reconciliation
pass: recomputing what PST *should* have been on every invoice and reporting
where the export disagrees, so mistakes surface before the return is filed
rather than during an audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .bcpst import PST_RATE, Quarter, commission, pst_on, round_cents
from .loader import Invoice

ZERO = Decimal("0.00")

# Per-invoice tolerance when comparing charged PST against 7% of the subtotal.
# Invoice2go rounds tax per line item, so a multi-line invoice can legitimately
# differ from a single rounding of the whole subtotal by a cent or two.
ROUNDING_TOLERANCE = Decimal("0.05")


@dataclass
class Finding:
    """Something on an invoice that a person should look at before filing."""

    severity: str  # "error" | "warning"
    invoice: Invoice
    message: str

    @property
    def label(self) -> str:
        return self.invoice.number or f"row {self.invoice.source_row}"


@dataclass
class QuarterResult:
    quarter: Quarter
    taxable_sales: Decimal = ZERO
    exempt_sales: Decimal = ZERO
    pst_collected: Decimal = ZERO
    gst_collected: Decimal = ZERO
    invoice_count: int = 0
    credit_count: int = 0
    excluded_count: int = 0
    on_time: bool = True
    taxable_invoices: list[Invoice] = field(default_factory=list)
    exempt_invoices: list[Invoice] = field(default_factory=list)
    excluded: list[tuple[Invoice, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def total_sales(self) -> Decimal:
        """Box A: all sales and leases in Canada, taxable and exempt alike."""
        return round_cents(self.taxable_sales + self.exempt_sales)

    @property
    def pst_collectable(self) -> Decimal:
        """What goes on the return: the PST actually charged in the period."""
        return round_cents(self.pst_collected)

    @property
    def expected_pst(self) -> Decimal:
        """7% of taxable sales -- the cross-check against pst_collectable."""
        return pst_on(self.taxable_sales)

    @property
    def variance(self) -> Decimal:
        return round_cents(self.pst_collectable - self.expected_pst)

    @property
    def commission(self) -> Decimal:
        if not self.on_time:
            return ZERO
        return commission(self.pst_collectable)

    @property
    def net_remittance(self) -> Decimal:
        return round_cents(self.pst_collectable - self.commission)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def _exclusion_reason(invoice: Invoice) -> str | None:
    """Why an invoice should not appear on the return at all, if so."""
    if invoice.is_void:
        return f"status {invoice.status!r}"
    if invoice.is_quote:
        return f"not an invoice (status {invoice.status!r})"
    return None


def analyse_quarter(
    invoices: list[Invoice],
    quarter: Quarter,
    *,
    on_time: bool = True,
) -> QuarterResult:
    """Compute the return figures for one quarter.

    Invoices are selected by invoice date, not payment date: B.C. PST is
    reported for the period in which the tax became payable, so an unpaid
    invoice issued in the quarter still belongs on this return.
    """
    result = QuarterResult(quarter=quarter, on_time=on_time)

    for invoice in invoices:
        if not quarter.contains(invoice.invoice_date):
            continue

        reason = _exclusion_reason(invoice)
        if reason is not None:
            result.excluded.append((invoice, reason))
            result.excluded_count += 1
            continue

        result.invoice_count += 1
        if invoice.is_credit:
            result.credit_count += 1

        if invoice.currency != "CAD":
            result.findings.append(
                Finding(
                    "warning",
                    invoice,
                    f"is in {invoice.currency}, not CAD; PST must be reported in "
                    f"Canadian dollars, so convert this one by hand.",
                )
            )

        if invoice.charged_pst:
            result.taxable_sales += invoice.subtotal
            result.pst_collected += invoice.pst
            result.taxable_invoices.append(invoice)
            _check_rate(invoice, result)
        else:
            result.exempt_sales += invoice.subtotal
            result.exempt_invoices.append(invoice)
            _check_exemption(invoice, result)

        result.gst_collected += invoice.gst

    result.taxable_sales = round_cents(result.taxable_sales)
    result.exempt_sales = round_cents(result.exempt_sales)
    result.pst_collected = round_cents(result.pst_collected)
    result.gst_collected = round_cents(result.gst_collected)
    return result


def _check_rate(invoice: Invoice, result: QuarterResult) -> None:
    """Flag PST that is not 7% of the invoice subtotal."""
    expected = invoice.expected_pst
    difference = invoice.pst - expected
    if abs(difference) <= ROUNDING_TOLERANCE:
        return

    rate = invoice.implied_rate
    rate_text = f"{rate * 100:.2f}%" if rate is not None else "n/a"
    severity = "error" if abs(difference) > Decimal("1.00") else "warning"
    result.findings.append(
        Finding(
            severity,
            invoice,
            f"charged {invoice.pst} PST on a subtotal of {invoice.subtotal} "
            f"({rate_text}); 7% would be {expected}, a difference of "
            f"{difference:+}.",
        )
    )


def _check_exemption(invoice: Invoice, result: QuarterResult) -> None:
    """Flag a zero-PST sale that has no documented reason.

    B.C. requires the seller to hold documentation for an exempt sale. A sale
    with no PST and no stated reason is the single most common way a retailer
    loses an assessment, so it is surfaced rather than quietly totalled.
    """
    if invoice.subtotal == 0:
        return
    if invoice.is_credit:
        return
    if invoice.exempt_reason:
        return
    result.findings.append(
        Finding(
            "error",
            invoice,
            f"has no PST on a sale of {invoice.subtotal} and no exemption reason "
            f"recorded. If it should have been taxed, PST of "
            f"{invoice.expected_pst} is missing.",
        )
    )


def analyse_all_quarters(invoices: list[Invoice]) -> list[QuarterResult]:
    """Run every quarter present in the data, oldest first."""
    from .bcpst import quarter_of

    quarters = sorted(
        {quarter_of(invoice.invoice_date) for invoice in invoices},
        key=lambda q: (q.year, q.number),
    )
    return [analyse_quarter(invoices, quarter) for quarter in quarters]


def rate_summary() -> str:
    return f"B.C. PST at {PST_RATE * 100:.0f}% on taxable retail sales"

"""End-to-end figures for the sample quarter, plus the reconciliation rules."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pstcalc.bcpst import Quarter
from pstcalc.calc import analyse_all_quarters, analyse_quarter
from pstcalc.loader import Invoice, load_invoices

SAMPLE = Path(__file__).parent.parent / "sample_data" / "invoice2go_export_sample.csv"


def D(value: str) -> Decimal:
    return Decimal(value)


def make_invoice(**kwargs) -> Invoice:
    defaults = dict(
        number="INV-1",
        invoice_date=date(2026, 8, 1),
        customer="Test Customer",
        subtotal=D("1000.00"),
        pst=D("70.00"),
        gst=D("50.00"),
        total=D("1120.00"),
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


@pytest.fixture(scope="module")
def sample_invoices():
    return load_invoices(SAMPLE).invoices


@pytest.fixture(scope="module")
def q3(sample_invoices):
    return analyse_quarter(sample_invoices, Quarter(2026, 3))


class TestSampleQuarter:
    """The sample file was built so every figure below is hand-checkable."""

    def test_only_q3_invoices_are_counted(self, q3):
        # The June invoice belongs to Q2 and must not leak in.
        assert q3.invoice_count == 11
        assert q3.excluded_count == 2

    def test_taxable_and_exempt_sales(self, q3):
        assert q3.taxable_sales == D("66150.00")
        assert q3.exempt_sales == D("17250.00")

    def test_box_a_is_the_sum_of_both(self, q3):
        assert q3.total_sales == D("83400.00")

    def test_pst_collected(self, q3):
        assert q3.pst_collectable == D("4518.50")

    def test_variance_equals_the_undercharged_invoice(self, q3):
        # INV-1047 was billed at 5% instead of 7% on 5,600.00, i.e. 112.00 short.
        assert q3.expected_pst == D("4630.50")
        assert q3.variance == D("-112.00")

    def test_commission_is_capped(self, q3):
        assert q3.commission == D("198.00")

    def test_net_remittance(self, q3):
        assert q3.net_remittance == D("4320.50")

    def test_gst_is_tracked_separately_and_excluded_from_pst(self, q3):
        assert q3.gst_collected == D("4170.00")

    def test_credit_note_reduces_the_totals(self, q3):
        assert q3.credit_count == 1

    def test_void_and_quote_are_excluded(self, q3):
        reasons = {invoice.number: reason for invoice, reason in q3.excluded}
        assert "INV-1050" in reasons
        assert "QTE-0088" in reasons

    def test_unpaid_invoice_still_counts(self, q3):
        # PST is reported when it becomes payable, not when the customer pays.
        numbers = {invoice.number for invoice in q3.taxable_invoices}
        assert "INV-1048" in numbers

    def test_findings_name_the_two_real_problems(self, q3):
        flagged = {finding.label for finding in q3.errors}
        assert "INV-1046" in flagged  # no PST, no exemption reason
        assert "INV-1047" in flagged  # charged 5% instead of 7%

    def test_foreign_currency_is_warned_about(self, q3):
        warned = {finding.label for finding in q3.warnings}
        assert "INV-1052" in warned

    def test_due_date_rolls_off_the_weekend(self, q3):
        assert q3.quarter.due_date == date(2026, 11, 2)


class TestQuarterSelection:
    def test_earlier_quarter_is_isolated(self, sample_invoices):
        q2 = analyse_quarter(sample_invoices, Quarter(2026, 2))
        assert q2.invoice_count == 1
        assert q2.taxable_sales == D("5000.00")
        assert q2.pst_collectable == D("350.00")

    def test_empty_quarter_reports_nothing(self, sample_invoices):
        q1 = analyse_quarter(sample_invoices, Quarter(2026, 1))
        assert q1.invoice_count == 0
        assert q1.pst_collectable == D("0.00")
        assert q1.net_remittance == D("0.00")

    def test_all_quarters_are_found_in_order(self, sample_invoices):
        results = analyse_all_quarters(sample_invoices)
        assert [r.quarter.label for r in results] == ["Q2 2026", "Q3 2026"]


class TestCommissionAndLateFiling:
    def test_late_filing_forfeits_the_commission(self, sample_invoices):
        late = analyse_quarter(sample_invoices, Quarter(2026, 3), on_time=False)
        assert late.commission == D("0.00")
        assert late.net_remittance == late.pst_collectable

    def test_small_quarter_gets_the_percentage_not_the_cap(self):
        invoices = [make_invoice(subtotal=D("10000.00"), pst=D("700.00"))]
        result = analyse_quarter(invoices, Quarter(2026, 3))
        assert result.pst_collectable == D("700.00")
        assert result.commission == D("46.20")  # 6.6% of 700.00
        assert result.net_remittance == D("653.80")


class TestReconciliation:
    def test_correct_invoice_raises_nothing(self):
        result = analyse_quarter([make_invoice()], Quarter(2026, 3))
        assert result.findings == []

    def test_per_line_rounding_is_tolerated(self):
        # A multi-line invoice can differ a cent or two from one rounding of
        # the whole subtotal; that is not worth flagging.
        invoices = [make_invoice(pst=D("70.02"))]
        assert analyse_quarter(invoices, Quarter(2026, 3)).findings == []

    def test_small_discrepancy_is_a_warning(self):
        invoices = [make_invoice(pst=D("70.60"))]
        result = analyse_quarter(invoices, Quarter(2026, 3))
        assert len(result.warnings) == 1
        assert result.errors == []

    def test_large_discrepancy_is_an_error(self):
        invoices = [make_invoice(pst=D("50.00"))]
        result = analyse_quarter(invoices, Quarter(2026, 3))
        assert len(result.errors) == 1
        assert "7% would be 70.00" in result.errors[0].message

    def test_undocumented_exemption_is_an_error(self):
        invoices = [make_invoice(pst=D("0.00"), exempt_reason="")]
        result = analyse_quarter(invoices, Quarter(2026, 3))
        assert len(result.errors) == 1
        assert "no exemption reason" in result.errors[0].message

    def test_documented_exemption_is_accepted(self):
        invoices = [make_invoice(pst=D("0.00"), exempt_reason="Resale PST-1234-5678")]
        result = analyse_quarter(invoices, Quarter(2026, 3))
        assert result.findings == []
        assert result.exempt_sales == D("1000.00")

    def test_zero_value_invoice_is_not_flagged(self):
        invoices = [make_invoice(subtotal=D("0.00"), pst=D("0.00"), total=D("0.00"))]
        assert analyse_quarter(invoices, Quarter(2026, 3)).findings == []

    def test_credit_note_without_a_reason_is_not_flagged(self):
        invoices = [make_invoice(subtotal=D("-500.00"), pst=D("0.00"),
                                 total=D("-500.00"))]
        assert analyse_quarter(invoices, Quarter(2026, 3)).findings == []

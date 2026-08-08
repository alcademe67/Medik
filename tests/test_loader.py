"""Reading exports: layouts, tax-column variants, and rows that must be skipped."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pstcalc.loader import load_invoices

SAMPLE = Path(__file__).parent.parent / "sample_data" / "invoice2go_export_sample.csv"


def D(value: str) -> Decimal:
    return Decimal(value)


def write_csv(tmp_path: Path, text: str, name: str = "export.csv") -> Path:
    path = tmp_path / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


class TestSampleFile:
    def test_every_row_loads(self):
        result = load_invoices(SAMPLE)
        assert len(result.invoices) == 14
        assert result.skipped == []

    def test_dates_are_read_day_first(self):
        result = load_invoices(SAMPLE)
        assert result.date_order == "dmy"
        first = result.invoices[0]
        assert first.invoice_date == date(2026, 6, 12)

    def test_thousands_separators_and_quotes(self):
        result = load_invoices(SAMPLE)
        by_number = {i.number: i for i in result.invoices}
        assert by_number["INV-1043"].subtotal == D("12400.00")
        assert by_number["INV-1048"].pst == D("1386.00")

    def test_parenthesised_credit_note_is_negative(self):
        by_number = {i.number: i for i in load_invoices(SAMPLE).invoices}
        credit = by_number["CN-0031"]
        assert credit.subtotal == D("-1200.00")
        assert credit.pst == D("-84.00")
        assert credit.is_credit

    def test_notes_column_supplies_the_exemption_reason(self):
        by_number = {i.number: i for i in load_invoices(SAMPLE).invoices}
        assert "resale" in by_number["INV-1045"].exempt_reason.lower()
        assert by_number["INV-1046"].exempt_reason == ""

    def test_status_flags(self):
        by_number = {i.number: i for i in load_invoices(SAMPLE).invoices}
        assert by_number["INV-1050"].is_void
        assert by_number["QTE-0088"].is_quote
        assert not by_number["INV-1048"].is_void  # unpaid is not void

    def test_currency_is_captured(self):
        by_number = {i.number: i for i in load_invoices(SAMPLE).invoices}
        assert by_number["INV-1052"].currency == "USD"
        assert by_number["INV-1042"].currency == "CAD"


class TestLayoutVariants:
    def test_named_tax_slot_columns(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,Subtotal,Tax 1 Name,Tax 1 Amount,Tax 2 Name,Tax 2 Amount,Total
INV-1,2026-07-05,Acme,1000.00,GST 5%,50.00,PST 7%,70.00,1120.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.pst == D("70.00")
        assert invoice.gst == D("50.00")

    def test_tax_slots_in_the_other_order(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,Subtotal,Tax 1 Name,Tax 1 Amount,Tax 2 Name,Tax 2 Amount,Total
INV-1,2026-07-05,Acme,1000.00,BC PST,70.00,GST,50.00,1120.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.pst == D("70.00")
        assert invoice.gst == D("50.00")

    def test_combined_tax_column_is_split_when_both_taxes_apply(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,Subtotal,Tax,Total
INV-1,2026-07-05,Acme,1000.00,120.00,1120.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.gst == D("50.00")
        assert invoice.pst == D("70.00")

    def test_combined_tax_column_recognises_a_gst_only_sale(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,Subtotal,Tax,Total
INV-1,2026-07-05,Acme,1000.00,50.00,1050.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.gst == D("50.00")
        assert invoice.pst == D("0.00")

    def test_subtotal_is_derived_when_absent(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,GST,PST,Total
INV-1,2026-07-05,Acme,50.00,70.00,1120.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.subtotal == D("1000.00")

    def test_total_is_derived_when_absent(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Client,Subtotal,GST,PST
INV-1,2026-07-05,Acme,1000.00,50.00,70.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.total == D("1120.00")

    def test_preamble_rows_above_the_header_are_skipped(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice2go Sales Report
1 Jul 2026 - 30 Sep 2026

Invoice No.,Date,Client,Subtotal,GST,PST,Total
INV-1,2026-07-05,Acme,1000.00,50.00,70.00,1120.00
""")
        result = load_invoices(path)
        assert len(result.invoices) == 1
        assert result.invoices[0].number == "INV-1"

    def test_utf8_bom_is_stripped(self, tmp_path):
        path = tmp_path / "bom.csv"
        path.write_text(
            "Invoice No.,Date,Subtotal,PST,Total\nINV-1,2026-07-05,100.00,7.00,107.00\n",
            encoding="utf-8-sig",
        )
        result = load_invoices(path)
        assert result.column_map.get("number") == "Invoice No."


class TestBadInput:
    def test_missing_date_column_is_a_clear_error(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Client,Subtotal,PST,Total
INV-1,Acme,1000.00,70.00,1120.00
""")
        with pytest.raises(KeyError, match="date column"):
            load_invoices(path)

    def test_unreadable_date_skips_the_row_and_says_so(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,PST,Total
INV-1,2026-07-05,1000.00,70.00,1120.00
INV-2,whenever,500.00,35.00,535.00
""")
        result = load_invoices(path)
        assert len(result.invoices) == 1
        assert len(result.skipped) == 1
        assert result.skipped[0][0] == 3

    def test_unreadable_amount_skips_only_that_row(self, tmp_path):
        # One malformed row must not cost us the rest of the quarter.
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,PST,Total
INV-1,2026-07-05,1000.00,70.00,1070.00
INV-2,2026-07-06,see attached,35.00,535.00
INV-3,2026-07-07,500.00,35.00,535.00
""")
        result = load_invoices(path)
        assert [i.number for i in result.invoices] == ["INV-1", "INV-3"]
        assert len(result.skipped) == 1
        assert result.skipped[0][0] == 3
        assert "subtotal" in result.skipped[0][1]

    def test_blank_rows_are_ignored(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,PST,Total
INV-1,2026-07-05,1000.00,70.00,1120.00
,,,,
INV-2,2026-07-06,500.00,35.00,535.00
""")
        assert len(load_invoices(path).invoices) == 2

    def test_empty_file_is_an_error(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            load_invoices(path)

    def test_ragged_row_is_padded_not_dropped(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,GST,PST,Total,Notes
INV-1,2026-07-05,1000.00,50.00,70.00,1120.00
""")
        invoice = load_invoices(path).invoices[0]
        assert invoice.pst == D("70.00")
        assert invoice.exempt_reason == ""

    def test_ambiguous_dates_default_to_day_first_with_a_warning(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,PST,Total
INV-1,03/07/2026,1000.00,70.00,1070.00
""")
        result = load_invoices(path)
        assert result.date_order == "dmy"
        assert result.invoices[0].invoice_date == date(2026, 7, 3)
        assert any("day-first" in w for w in result.warnings)

    def test_explicit_date_order_overrides_detection(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,PST,Total
INV-1,03/07/2026,1000.00,70.00,1070.00
""")
        result = load_invoices(path, date_order="mdy")
        assert result.invoices[0].invoice_date == date(2026, 3, 7)

    def test_column_override_is_applied(self, tmp_path):
        path = write_csv(tmp_path, """
Invoice,Date,Subtotal,Provincial Levy,Total
INV-1,2026-07-05,1000.00,70.00,1070.00
""")
        result = load_invoices(path, overrides={"pst": "Provincial Levy"})
        assert result.invoices[0].pst == D("70.00")

"""Heading detection across the export layouts Invoice2go produces."""

import pytest

from pstcalc.columns import build_column_map, classify_tax_label, normalise


def test_normalise_collapses_punctuation_and_case():
    assert normalise("Invoice No.") == "invoiceno"
    assert normalise("INVOICE_NUMBER") == "invoicenumber"
    assert normalise("  Client Name  ") == "clientname"


class TestBuildColumnMap:
    def test_plain_layout(self):
        headings = ["Invoice No.", "Date", "Client", "Subtotal", "GST", "PST", "Total"]
        column_map = build_column_map(headings)
        assert column_map.get("number") == "Invoice No."
        assert column_map.get("date") == "Date"
        assert column_map.get("customer") == "Client"
        assert column_map.get("pst") == "PST"
        assert column_map.get("gst") == "GST"

    def test_alternative_headings(self):
        headings = ["Document Number", "Issue Date", "Customer Name", "Net Amount",
                    "Total Amount"]
        column_map = build_column_map(headings)
        assert column_map.get("number") == "Document Number"
        assert column_map.get("date") == "Issue Date"
        assert column_map.get("customer") == "Customer Name"
        assert column_map.get("subtotal") == "Net Amount"
        assert column_map.get("total") == "Total Amount"

    def test_named_tax_slot_columns_are_recorded(self):
        headings = ["Invoice", "Date", "Tax 1 Name", "Tax 1 Amount",
                    "Tax 2 Name", "Tax 2 Amount"]
        column_map = build_column_map(headings)
        assert column_map.tax_label_columns["tax1name"] == "Tax 1 Name"
        assert column_map.tax_label_columns["tax2name"] == "Tax 2 Name"

    def test_a_heading_is_only_claimed_once(self):
        # "Total" could match both `total` and, via a loose reading, other
        # fields; whichever claims it first must release the rest.
        headings = ["Date", "Total"]
        column_map = build_column_map(headings)
        claimed = list(column_map.resolved.values())
        assert len(claimed) == len(set(claimed))

    def test_override_wins_over_detection(self):
        headings = ["Date", "PST", "Provincial Sales Tax"]
        column_map = build_column_map(headings, {"pst": "Provincial Sales Tax"})
        assert column_map.get("pst") == "Provincial Sales Tax"

    def test_override_naming_a_missing_column_is_an_error(self):
        with pytest.raises(KeyError, match="not in the file"):
            build_column_map(["Date", "Total"], {"pst": "Nope"})

    def test_unmatched_headings_are_reported(self):
        column_map = build_column_map(["Date", "Total", "Salesperson"])
        assert "Salesperson" in column_map.unmatched


class TestClassifyTaxLabel:
    @pytest.mark.parametrize("label", ["PST", "P.S.T.", "BC PST 7%", "Provincial Tax"])
    def test_pst_labels(self, label):
        assert classify_tax_label(label) == "pst"

    @pytest.mark.parametrize("label", ["GST", "G.S.T. 5%", "HST", "Federal Tax"])
    def test_gst_labels(self, label):
        assert classify_tax_label(label) == "gst"

    @pytest.mark.parametrize("label", ["", "Tax", "Levy", None])
    def test_unknown_labels_are_not_guessed_at(self, label):
        assert classify_tax_label(label) is None

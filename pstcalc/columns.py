"""Map the headings in an Invoice2go export onto the fields we need.

Invoice2go's column headings vary by account age, region and which report you
export, so nothing here hard-codes a single expected layout. Each field lists
the headings seen in the wild; anything unmatched can be pinned in the config
file rather than requiring a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered best-match-first. Matching is done on a normalised heading, so
# "Invoice #", "invoice_no" and "INVOICE NUMBER" all collapse together.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "number": (
        "invoicenumber", "invoiceno", "invoice", "documentnumber", "docno",
        "number", "no", "reference", "refno",
    ),
    "date": (
        "invoicedate", "issuedate", "dateissued", "date", "created", "createddate",
        "documentdate",
    ),
    "customer": (
        "client", "clientname", "customer", "customername", "billto", "company",
        "contact", "contactname",
    ),
    "subtotal": (
        "subtotal", "netamount", "amountbeforetax", "amountexcltax", "nettotal",
        "goodsamount",
    ),
    "total": (
        "total", "invoicetotal", "totalamount", "grandtotal", "amount",
        "totalincltax",
    ),
    "pst": (
        "pst", "pstamount", "provincialsalestax", "pstcollected", "bcpst",
        "tax2amount", "tax2",
    ),
    "gst": (
        "gst", "gstamount", "goodsandservicestax", "hst", "gsthst",
        "tax1amount", "tax1",
    ),
    "tax_total": (
        "tax", "taxamount", "totaltax", "taxes", "salestax",
    ),
    "status": (
        "status", "invoicestatus", "paymentstatus", "state",
    ),
    "currency": (
        "currency", "currencycode", "curr",
    ),
    "exempt_reason": (
        "exemptreason", "exemptionreason", "pstexemptreason", "taxexemptreason",
        "exemptioncertificate", "certificatenumber", "notes", "note", "memo",
        "description", "ponumber", "po",
    ),
}

# Headings for the name-of-tax columns that pair with "Tax 1 Amount" etc.
TAX_LABEL_COLUMNS = ("tax1name", "tax1label", "tax1", "tax2name", "tax2label", "tax2")

_PST_LABEL = re.compile(r"\bp\.?s\.?t\b|provincial", re.IGNORECASE)
_GST_LABEL = re.compile(r"\bg\.?s\.?t\b|\bh\.?s\.?t\b|federal", re.IGNORECASE)


def normalise(heading: str) -> str:
    """Reduce a heading to a comparable key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(heading or "").lower())


@dataclass
class ColumnMap:
    """Which source heading feeds each field we care about."""

    resolved: dict[str, str] = field(default_factory=dict)
    tax_label_columns: dict[str, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)

    def get(self, field_name: str) -> str | None:
        return self.resolved.get(field_name)

    def has(self, field_name: str) -> bool:
        return field_name in self.resolved

    def describe(self) -> list[str]:
        return [f"{name} <- {source!r}" for name, source in sorted(self.resolved.items())]


def build_column_map(
    headings: list[str],
    overrides: dict[str, str] | None = None,
) -> ColumnMap:
    """Resolve headings to fields, with config overrides taking precedence.

    An override names the exact source heading to use for a field, which is the
    escape hatch when a store has renamed columns or when two headings match
    the same synonym list.
    """
    overrides = overrides or {}
    by_key: dict[str, str] = {}
    for heading in headings:
        key = normalise(heading)
        # First heading wins so that duplicated blank columns cannot displace
        # a real one.
        by_key.setdefault(key, heading)

    column_map = ColumnMap()
    claimed: set[str] = set()

    for field_name, wanted in overrides.items():
        match = by_key.get(normalise(wanted))
        if match is None:
            raise KeyError(
                f"config maps {field_name!r} to column {wanted!r}, "
                f"which is not in the file. Available: {', '.join(headings)}"
            )
        column_map.resolved[field_name] = match
        claimed.add(match)

    for field_name, candidates in SYNONYMS.items():
        if field_name in column_map.resolved:
            continue
        for candidate in candidates:
            match = by_key.get(candidate)
            if match is not None and match not in claimed:
                column_map.resolved[field_name] = match
                claimed.add(match)
                break

    for key in TAX_LABEL_COLUMNS:
        match = by_key.get(key)
        if match is not None:
            column_map.tax_label_columns[key] = match

    column_map.unmatched = [h for h in headings if h not in claimed]
    return column_map


def classify_tax_label(label: str) -> str | None:
    """Say whether a tax label names PST, GST, or something we should not guess at."""
    text = str(label or "")
    if _PST_LABEL.search(text):
        return "pst"
    if _GST_LABEL.search(text):
        return "gst"
    return None

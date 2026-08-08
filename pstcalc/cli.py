"""Command line entry point: pst.py <export.csv> [--quarter 2026Q3]."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .bcpst import Quarter, parse_quarter, quarter_of
from .calc import analyse_all_quarters, analyse_quarter
from .loader import load_invoices
from .report import worksheet, write_audit_csv, year_summary

DEFAULT_CONFIG = Path("pst.config.json")


def _load_config(path: Path | None) -> dict:
    candidate = path or DEFAULT_CONFIG
    if not candidate.exists():
        if path is not None:
            raise SystemExit(f"config file not found: {path}")
        return {}
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{candidate} is not valid JSON: {exc}") from exc


def _previous_quarter(today: date) -> Quarter:
    """The quarter most likely to be the one being filed: the one just ended."""
    current = quarter_of(today)
    if current.number == 1:
        return Quarter(current.year - 1, 4)
    return Quarter(current.year, current.number - 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pst",
        description="Work out the quarterly B.C. PST return from an Invoice2go "
                    "CSV export.",
    )
    parser.add_argument("export", type=Path, help="CSV exported from Invoice2go")
    parser.add_argument(
        "--quarter", "-q",
        help="Quarter to file, e.g. 2026Q3. Defaults to the quarter just ended.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Summarise every quarter in the file instead of filing one.",
    )
    parser.add_argument(
        "--audit", type=Path, metavar="PATH",
        help="Write a per-invoice audit CSV backing up the totals.",
    )
    parser.add_argument(
        "--config", type=Path,
        help=f"JSON config with column overrides (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--date-order", choices=("dmy", "mdy"),
        help="Force the reading of numeric dates instead of detecting it.",
    )
    parser.add_argument(
        "--late", action="store_true",
        help="Return will be filed after the due date, so claim no commission.",
    )
    parser.add_argument(
        "--show-columns", action="store_true",
        help="Print how the export's headings were matched, then carry on.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.export.exists():
        print(f"error: {args.export} not found", file=sys.stderr)
        return 2

    config = _load_config(args.config)
    overrides = config.get("columns", {})

    try:
        loaded = load_invoices(
            args.export,
            overrides=overrides,
            date_order=args.date_order or config.get("date_order"),
        )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.show_columns and loaded.column_map:
        print("Column mapping:")
        for line in loaded.column_map.describe():
            print(f"  {line}")
        if loaded.column_map.unmatched:
            print(f"  (ignored: {', '.join(loaded.column_map.unmatched)})")
        print(f"  date order: {loaded.date_order}")
        print()

    if not loaded.invoices:
        print("error: no usable invoice rows were found in the export", file=sys.stderr)
        for row, reason in loaded.skipped[:10]:
            print(f"  row {row}: {reason}", file=sys.stderr)
        return 2

    for warning in loaded.warnings[:10]:
        print(f"note: {warning}\n")
    for row, reason in loaded.skipped[:10]:
        print(f"note: skipped row {row}: {reason}")
    if loaded.skipped:
        print()

    if args.all:
        print(year_summary(analyse_all_quarters(loaded.invoices)))
        return 0

    if args.quarter:
        try:
            quarter = parse_quarter(args.quarter)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        quarter = _previous_quarter(date.today())

    result = analyse_quarter(loaded.invoices, quarter, on_time=not args.late)

    if result.invoice_count == 0 and result.excluded_count == 0:
        covered = sorted({quarter_of(i.invoice_date).label for i in loaded.invoices})
        print(f"error: no invoices fall in {quarter.label}.", file=sys.stderr)
        print(f"       This file covers: {', '.join(covered)}", file=sys.stderr)
        return 2

    print(worksheet(
        result,
        source=args.export.name,
        prepared=date.today().strftime("%B %-d, %Y"),
    ))

    if args.audit:
        write_audit_csv(result, args.audit)
        print(f"  Per-invoice audit trail written to {args.audit}")
        print()

    # A non-zero exit on errors makes this usable as a pre-filing gate.
    return 1 if result.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

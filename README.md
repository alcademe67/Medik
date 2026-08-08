# Quarterly B.C. PST calculator

Works out the quarterly **Provincial Sales Tax** return for a B.C. retail
store from an Invoice to Go (Invoice2go) CSV export. It totals the quarter,
calculates the commission and the net amount to remit, and — the part that
matters most — checks every invoice for the mistakes that cause trouble later.

Built for a flooring retailer selling **goods at retail** in **British
Columbia** (PST 7%).

---

## Quick start

Three steps, every quarter.

**1. Export the quarter from Invoice to Go.**
In the app: *Reports → Invoices*, set the date range to the quarter, and
export to CSV. Include at least the invoice number, date, client, subtotal,
tax columns, total and status.

**2. Run it.**

```bash
python3 pst.py ~/Downloads/invoices.csv --quarter 2026Q3
```

**3. Read the worksheet, fix anything it flags, then file.**

Nothing to install — it uses only what comes with Python 3.

### Useful variations

```bash
# The quarter that just ended (the usual case) — no --quarter needed
python3 pst.py invoices.csv

# Every quarter in the file, one line each
python3 pst.py invoices.csv --all

# Also write a per-invoice audit trail for your accountant
python3 pst.py invoices.csv -q 2026Q3 --audit q3-2026-audit.csv

# Filing after the due date, so no commission is claimable
python3 pst.py invoices.csv -q 2026Q3 --late

# Show how it matched your column headings
python3 pst.py invoices.csv --show-columns
```

Try it on the included sample first, to see what the output looks like:

```bash
python3 pst.py sample_data/invoice2go_export_sample.csv -q 2026Q3
```

---

## What it produces

```
  SALES
  Taxable sales ................................................ 66,150.00
  Exempt / non-taxable sales ................................... 17,250.00
  Box A - total sales and leases in Canada ..................... 83,400.00

  PST
  PST collected per the invoices ................................ 4,518.50
  Cross-check: 7% of taxable sales .............................. 4,630.50
  Variance ...................................................... (112.00)

  REMITTANCE
  PST collectable ............................................... 4,518.50
  Less commission (filed and paid on time) ...................... (198.00)
  NET PST TO REMIT .............................................. 4,320.50

  Due November 2, 2026 (October 31 falls on a Saturday, so it rolls forward)

--------------------------------------------------------------------------
  REVIEW BEFORE FILING - 2 error(s), 1 warning(s)
--------------------------------------------------------------------------
  !! INV-1046: has no PST on a sale of 2300.00 and no exemption reason
     recorded. If it should have been taxed, PST of 161.00 is missing.
  !! INV-1047: charged 280.00 PST on a subtotal of 5600.00 (5.00%);
     7% would be 392.00, a difference of -112.00.
   - INV-1052: is in USD, not CAD; PST must be reported in Canadian
     dollars, so convert this one by hand.
```

That variance line is the point of the whole thing. It is the gap between the
PST you actually charged and 7% of your taxable sales, and it should normally
be zero. Here it is exactly the −112.00 from INV-1047, which was billed at 5%
instead of 7% — an error worth catching before you file rather than during an
audit.

**The checks it runs on every invoice:**

| Check | Why |
|---|---|
| PST is 7% of the subtotal | Catches a wrong tax rate saved against a client or item |
| Zero PST has a documented reason | B.C. requires you to hold documentation for an exempt sale |
| Void and quote documents are excluded | These never became sales and must not be on the return |
| Credit notes reduce the totals | A return of goods reduces the PST you owe |
| Everything is in Canadian dollars | The return is filed in CAD |

Small differences of a cent or two are ignored: Invoice2go rounds tax per line,
so a multi-line invoice can differ slightly from one rounding of the whole
subtotal. Anything above five cents is reported, and anything above a dollar is
an error rather than a warning.

The command exits with a non-zero status when there are errors, so you can use
it as a gate before filing.

---

## Things it gets right that are easy to get wrong

**Unpaid invoices still count.** B.C. PST is reported for the period in which
the tax *became payable* — the invoice date — not when the customer pays you.
An invoice issued on 27 September on net-60 terms belongs on the Q3 return even
though the money arrives in November. The tool selects by invoice date for this
reason. Filing on payment date is one of the most common ways a return comes
out wrong.

**The commission is a three-step schedule, not a flat percentage.** If you file
and pay on time you keep a commission: the whole amount if your PST is $22.00
or less, a flat $22.00 up to $333.33, and 6.6% above that, capped at $198.00
per reporting period. The steps join cleanly — 6.6% of $333.33 is exactly
$22.00 — and the cap starts biting at $3,000.00 of PST. File late and you get
nothing, which is what `--late` models.

**GST is kept out of it.** Your invoices carry both 5% GST and 7% PST. The tool
separates them and reports GST only as context, since it has no place on a PST
return.

**Due dates roll off weekends.** The return is due the last day of the month
after the quarter ends. When that lands on a Saturday or Sunday it moves to the
next business day, and the worksheet says so. (B.C. statutory holidays are not
checked because none can fall on a quarterly due date — those are always
31 January, 30 April, 31 July or 31 October.)

---

## If it cannot read your export

Invoice2go's column headings vary between accounts and reports, so the tool
matches headings by name rather than by position, and recognises the common
variants (`Invoice No.` / `Document Number` / `Reference`, `Client` /
`Customer Name`, and so on). It also handles `$`, thousands separators,
parenthesised credits like `(84.00)`, a UTF-8 byte-order mark, title rows above
the real header, and ragged rows.

Run `--show-columns` to see what it matched. If something is wrong, copy
`pst.config.example.json` to `pst.config.json` and pin the column by hand:

```json
{ "columns": { "pst": "Provincial Sales Tax", "exempt_reason": "Notes" } }
```

**Dates.** A date like `03/07/2026` is either 3 July or 7 March. The tool reads
the whole column and works it out — as soon as one row has a day above 12 the
format is settled. If every date in the file happens to fit both readings it
says so and assumes day-first; use `--date-order mdy` to override.

**Exemption reasons.** The tool looks for a reason in an exemption or notes
column (`Exempt Reason`, `Notes`, `Memo`, `PO Number`, and similar). Recording
the reason on the invoice — "purchased for resale, PST-1234-5678" — is what
stops it flagging a legitimate exempt sale, and is what you would need to show
if asked.

---

## Before you rely on the numbers

- **This is a calculator, not tax advice.** Have your accountant check the
  first quarter's output against your own figures before you file from it.
- **Box A is labelled from the B.C. guide**; confirm the remaining box
  placements against the current FIN 400 or the eTaxBC screen when you enter
  them, since the form's layout can change.
- **It assumes retail sales of goods in B.C. at 7%.** If the store starts doing
  supply-and-install work, the rules change materially — that is a real
  property contract, where you generally pay PST on your materials rather than
  charging it to the customer, and this tool does not model that. There are
  also goods with their own rates or exemptions.
- Anything the tool cannot read is reported rather than silently treated as
  zero, so read the `note:` lines at the top of the output.

Sources for the figures above:
[Reporting and paying PST](https://www2.gov.bc.ca/gov/content/taxes/sales-taxes/pst/report-pay) ·
[Guide to completing the PST return](https://www2.gov.bc.ca/gov/content/taxes/sales-taxes/pst/report-pay/pst-return-guide) ·
[FIN 400](https://www2.gov.bc.ca/assets/gov/taxes/sales-taxes/forms/fin-400-provincial-sales-tax-return.pdf).
B.C.'s PST enquiry line is 1-877-388-4440.

---

## Development

```bash
pip install pytest
python3 -m pytest tests/ -q
```

135 tests cover the commission bands and their boundaries, the reporting
calendar, money and date parsing, column matching across export layouts, and
the full sample quarter end to end. The sample file in `sample_data/` is built
so every total in `tests/test_calc.py` can be checked by hand.

Layout:

| File | Purpose |
|---|---|
| `pstcalc/bcpst.py` | The B.C. rules: rate, commission table, quarters, due dates |
| `pstcalc/parsing.py` | Money and date parsing from spreadsheet cells |
| `pstcalc/columns.py` | Matching export headings to the fields needed |
| `pstcalc/loader.py` | Reading an export into invoice records |
| `pstcalc/calc.py` | Quarter totals and the reconciliation checks |
| `pstcalc/report.py` | The worksheet and the audit CSV |
| `pstcalc/cli.py` | Command line handling |

---

## Repository history

This repository previously held a KuCoin spot-trading toolkit, removed in
`2fae2d7`. The code is still in the history — commit `95b04f3` is the last one
that contained it. No API credentials were ever committed.

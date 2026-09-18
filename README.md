# Month-End Close Assistant

[![Tests](https://github.com/Amanaggarwal54/month-end-close-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/Amanaggarwal54/month-end-close-assistant/actions/workflows/tests.yml)

Synthetic finance automation project for a three-entity month-end close.

## Current scope

Steps 0â€“7 are implemented and tested:

1. Deterministic synthetic purchase orders, invoices, payments, shared costs and FX data.
2. Planted source errors E1-E4 plus downstream intercompany errors E5-E6.
3. Synthetic PDF invoice generation.
4. PDF invoice extraction with `pdfplumber` and CSV-to-PDF comparison.
5. Three-way invoice matching and payment-status classification.
6. Intercompany shared-cost recharge, FX conversion and elimination checks.
7. Automated close controls, FX controls and close-decision logic.

The project covers three entities:

- `BE01` â€” Belgian parent, EUR
- `US01` â€” US subsidiary, USD
- `BE02` â€” Belgian company, EUR

The synthetic close period is Januaryâ€“March 2026.

## Data

Clean source data is stored under `data/raw/`.

The project includes deliberately planted errors under `data/error_data/` for testing:

- **E1** â€” invoice amount mismatch
- **E2** â€” duplicate invoice
- **E3** â€” payment without invoice
- **E4** â€” incorrect February EUR/USD FX rate
- **E5** â€” one side of an intercompany entry deleted
- **E6** â€” one intercompany local amount blanked

The clean and error datasets are deterministic and can be regenerated for repeatable testing.

## Generate clean data

```bash
python src/generate_data.py
```

## Generate planted-error data

```bash
python src/generate_data.py --with-errors
```

The command prints the planted changes and writes the planted-error manifest.

## Generate sample invoice PDFs

```bash
python src/generate_invoice_pdfs.py --limit 15
```

## Extract the PDFs and compare against the source CSV

```bash
python src/extract_invoices.py
```

## Three-way invoice matching

The matching engine in `src/match.py` compares purchase orders, invoices and payments.

It checks:

- invoice net amount against the related purchase order
- invoice payment status
- duplicate invoices
- invoices without purchase orders
- orphan payments
- underpayments and overpayments

Run the matching module with:

```bash
python src/match.py
```

## Intercompany recharge and FX

The intercompany engine in `src/intercompany.py` allocates shared costs between entities, applies the configured markup, converts amounts using month-end EUR/USD rates and checks whether intercompany balances eliminate.

The FX convention is:

```text
1 EUR = X USD
```

Therefore:

- EUR â†’ USD: multiply by the FX rate
- USD â†’ EUR: divide by the FX rate

## Close controls

The control engine in `src/controls.py` evaluates reconciliation, source-accuracy, completeness and FX controls.

Controls are divided into:

- `MAT` â€” invoice matching controls
- `ICO` â€” intercompany controls
- `SRC` â€” source-data controls
- `CVG` â€” close-period and entity coverage controls
- `FXC` â€” FX controls

Critical control failures block the close. Warnings remain visible but do not block the close.

The close decision contains:

- `PASS`
- `PASS_WITH_WARNINGS`
- `FAIL`

A failed close has:

```text
report_allowed: False
```

## FX controls

The FX control family contains six controls:

- **FXC-01** â€” expected close-period months are present
- **FXC-02** â€” duplicate FX months are detected
- **FXC-03** â€” supplied FX rates agree with the reference rates
- **FXC-04** â€” extra FX months are reported as warnings
- **FXC-05** â€” intercompany recharge amounts are re-performed using reference FX rates
- **FXC-06** â€” implausible or decimal-point FX errors are detected

FXC-03 and FXC-05 are critical controls.

FXC-05 deliberately uses the independent reference FX rates rather than trusting the FX rate already recorded on the intercompany entry. It checks both local-currency amounts and EUR-equivalent amounts. Entries with missing amounts are left to the intercompany completeness controls rather than being treated as an FX calculation failure.

## Run the close controls

For the clean dataset:

```bash
python src/controls.py --fx-rates data/raw/fx_rates.csv --fx-reference data/raw/fx_rates_expected.csv
```

Expected result:

```text
PASS    38

close_status: PASS
report_allowed: True
critical_failed: 0
errors: 0
warnings: 0
```

For the planted E4 FX error:

```bash
python src/controls.py --fx-rates data/error_data/fx_rates.csv --fx-reference data/raw/fx_rates_expected.csv --dataset-label E4
```

Expected result:

```text
PASS    36
FAIL     2
```

The blocking controls should be:

```text
FXC-03, FXC-05
```

and the close should return:

```text
close_status: FAIL
report_allowed: False
```

## Testing

The project uses `pytest`.

Run the complete test suite with:

```bash
python -m pytest tests -v
```

The current suite contains **86 tests**, covering:

- clean close behavior
- invoice matching
- planted source errors E1-E4
- intercompany controls
- planted downstream errors E5-E6
- FX controls FXC-01-FXC-06
- FX tolerance behavior
- FX-control independence
- close-decision behavior
- input/output safety

Current validation:

```text
86 passed
```

## Important design choice

The raw input files are deliberately kept deterministic and inspectable. Accounting calculations and control decisions remain ordinary Python code rather than being delegated to an LLM at runtime.

LLMs can assist with:

- code generation
- logic review
- documentation
- test design
- exception explanation

They should not be the runtime source of numeric accounting decisions.

## Local Git setup

```bash
git init
git add .
git commit -m "Initialize month-end close assistant"
```

Create the public GitHub repository separately and connect it with:

```bash
git remote add origin ...
```


# README section: one-command close

*(Paste into README.md, adapting the headings to your existing structure.)*

## Running the close

```powershell
python src\pipeline.py --data-dir data\raw --dataset-label clean
```

One command runs the whole sequence - matching, intercompany recharges, close
controls, the decision, and the report package:

```
source data -> match.py -> intercompany.py -> controls.py -> decide_close() -> report.py
```

`pipeline.py` is orchestration only. It contains no matching rule, no FX
conversion, no tolerance comparison and no formatting: every step is a call into
the module that owns that logic. It is also the project's only orchestration
implementation - `report.py`'s command line delegates to it - so the two entry
points cannot drift apart. A test proves it, by producing the same close twice
(once by calling the modules by hand, once through the pipeline) and comparing
the SHA-256 of every artefact.

## Scenarios

| Case | Command | Result |
|---|---|---|
| Clean | `python src\pipeline.py --data-dir data\raw --dataset-label clean` | PASS, close report |
| E1 / E2 / E3 | `python src\pipeline.py --data-dir data\error_data --fx-rates data\raw\fx_rates.csv --fx-reference data\raw\fx_rates_expected.csv --dataset-label E1-E3` | blocked by MAT-03, MAT-04, MAT-06 |
| E4 | `python src\pipeline.py --fx-rates data\error_data\fx_rates.csv --dataset-label E4` | blocked by FXC-03, FXC-05 |
| E5 | `python src\pipeline.py --ic-entries data\error_data\intercompany_entries_E5.csv --dataset-label E5` | blocked by ICO-01 |
| E6 | `python src\pipeline.py --ic-entries data\error_data\intercompany_entries_E6.csv --dataset-label E6` | blocked by ICO-03 |

The FX reference always defaults to `data/raw/fx_rates_expected.csv`, including
when `--data-dir` points at the error data: a reference that lived inside the
dataset under test would not be independent of it.

## Options

| Flag | Effect |
|---|---|
| `--out-dir` | where the decision package is written (default `out/`) |
| `--run-timestamp` | fixes the timestamp; the same inputs then produce byte-identical output |
| `--dry-run` | decide the close and print the result, write nothing |
| `--no-fail-on-blocked` | still produce the exception report, but exit 0 |
| `--quiet` | suppress the summary |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the close is approved and the report may be issued |
| 2 | the close is blocked by a critical control |
| 1 | the run could not complete (missing input, protected output path) |

2 and 1 are deliberately different: 2 means the controls did their job, 1 means
the run never got far enough to have an opinion.

### Audit trail and exceptions

Every close run also produces two machine-readable audit artifacts:

- `audit_trail.json` — close status, report eligibility, control counts, blocking controls, and the structured exceptions for the run.
- `exceptions.json` — the exception records as a flat list for downstream review or further automation.

Both are built by `src/audit.py`, which is a pure transformation layer over the deterministic control results. `src/report.py` only serializes the completed objects; it does not re-derive exceptions or close outcomes.

`package_manifest.json` records SHA-256 hashes for both files as part of the decision package.

## Example output

```
Dataset:        E4
Close status:   FAIL  (report_allowed=false)
Blocking:       FXC-03, FXC-05
  - FXC-03: 1 month(s) differ from the reference; largest gap +0.1000 in 2026-02 (reference 1.09, supplied 1.19).
  - FXC-05: 6 entr(y/ies) do not re-perform at reference rates; first: COST002-BE01-US01-P - local amount booked 13,993.78, reference 12,817.83.
Document:       out\e4\close_exception_report_e4_20260331T180000Z.pdf
Package:        out\e4
```

Nothing is ever written under `data/raw`: every output path passes the same
guard used by the error injector, and a test hashes every source file before and
after a full run.


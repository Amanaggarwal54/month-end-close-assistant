# Month-End Close Assistant

Synthetic finance automation project for a three-entity month-end close.

## Current scope

Steps 0–7 are implemented and tested:

1. Deterministic synthetic purchase orders, invoices, payments, shared costs and FX data.
2. Planted source errors E1–E4 plus downstream intercompany errors E5–E6.
3. Synthetic PDF invoice generation.
4. PDF invoice extraction with `pdfplumber` and CSV-to-PDF comparison.
5. Three-way invoice matching and payment-status classification.
6. Intercompany shared-cost recharge, FX conversion and elimination checks.
7. Automated close controls, FX controls and close-decision logic.

The project covers three entities:

- `BE01` — Belgian parent, EUR
- `US01` — US subsidiary, USD
- `BE02` — Belgian company, EUR

The synthetic close period is January–March 2026.

## Data

Clean source data is stored under `data/raw/`.

The project includes deliberately planted errors under `data/error_data/` for testing:

- **E1** — invoice amount mismatch
- **E2** — duplicate invoice
- **E3** — payment without invoice
- **E4** — incorrect February EUR/USD FX rate
- **E5** — one side of an intercompany entry deleted
- **E6** — one intercompany local amount blanked

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

- EUR → USD: multiply by the FX rate
- USD → EUR: divide by the FX rate

## Close controls

The control engine in `src/controls.py` evaluates reconciliation, source-accuracy, completeness and FX controls.

Controls are divided into:

- `MAT` — invoice matching controls
- `ICO` — intercompany controls
- `SRC` — source-data controls
- `CVG` — close-period and entity coverage controls
- `FXC` — FX controls

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

- **FXC-01** — expected close-period months are present
- **FXC-02** — duplicate FX months are detected
- **FXC-03** — supplied FX rates agree with the reference rates
- **FXC-04** — extra FX months are reported as warnings
- **FXC-05** — intercompany recharge amounts are re-performed using reference FX rates
- **FXC-06** — implausible or decimal-point FX errors are detected

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

The current suite contains **45 tests**, covering:

- clean close behavior
- invoice matching
- planted source errors E1–E4
- intercompany controls
- planted downstream errors E5–E6
- FX controls FXC-01–FXC-06
- FX tolerance behavior
- FX-control independence
- close-decision behavior
- input/output safety

Current validation:

```text
45 passed
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

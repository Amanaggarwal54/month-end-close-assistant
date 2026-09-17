# Month-End Close Assistant

Synthetic finance automation project for a three-entity month-end close.

## Current scope
Steps 0-2 are implemented:

1. Deterministic synthetic purchase orders, invoices, payments, shared costs and FX data.
2. Optional planted errors E1-E4 in `data/error_data`; E5-E6 are reserved for downstream intercompany-output tests.
3. Synthetic PDF invoice generation.
4. PDF invoice extraction with `pdfplumber` and CSV-to-PDF comparison.

## Setup

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

## Generate clean data

```bash
python src/generate_data.py
```

## Generate planted-error data

```bash
python src/generate_data.py --with-errors
```

The command prints the planted changes and writes `planted_errors.csv`.

## Generate sample invoice PDFs

```bash
python src/generate_invoice_pdfs.py --limit 15
```

## Extract the PDFs and compare against the source CSV

```bash
python src/extract_invoices.py
```

## Important design choice

The raw input files are deliberately kept deterministic and inspectable. The accounting calculations planned for later steps should remain ordinary code. LLMs can assist with code generation, logic review and documentation, but should not be the runtime source of numeric accounting decisions.

## Local Git setup

```bash
git init
git add .
git commit -m "Initialize month-end close assistant"
```

Create the public GitHub repository separately and connect it with `git remote add origin ...`.

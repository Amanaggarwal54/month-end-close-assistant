# Month-End Close Assistant

[![Tests](https://github.com/Amanaggarwal54/month-end-close-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/Amanaggarwal54/month-end-close-assistant/actions/workflows/tests.yml)

A synthetic finance automation project for a three-entity month-end close. The system combines deterministic accounting logic, independent close controls, audit-ready reporting, an immutable close package, a reviewer workflow, and an optional AI investigation layer.

> **Core principle:** the LLM can investigate an exception, but it never makes the accounting decision. Matching, FX calculations, controls, close status, report eligibility, and exception lifecycle remain deterministic and human-controlled.

## What this project does

The project covers a complete January-March 2026 close for three synthetic entities:

| Entity | Country | Currency |
|---|---|---|
| `BE01` | Belgium | EUR |
| `US01` | United States | USD |
| `BE02` | Belgium | EUR |

The close flow is:

```text
Synthetic source data
        |
        v
Invoice / payment matching ------+
                                  |
Shared-cost recharge + FX -------+----> Close controls
                                  |          |
                                  |          v
                                  |     Close decision
                                  |          |
                                  |          v
                                  |   Immutable close package
                                  |          |
                                  |          v
                                  |    Review workspace
                                  |          |
                                  |          v
                                  | Investigation packet
                                  |          |
                                  |      +---+---+
                                  |      |       |
                                  |    Gemini  Claude
                                  |      |       |
                                  |      +---+---+
                                  |          |
                                  |          v
                                  |    13A validation
                                  |          |
                                  +------> Human reviewer
```

## Main capabilities

### 1. Deterministic synthetic finance data

The repository contains reproducible purchase orders, invoices, payments, shared costs, and month-end FX rates.

The generated dataset contains deliberately planted errors for control testing:

| Error | Description |
|---|---|
| **E1** | Invoice amount differs from the related purchase order |
| **E2** | Duplicate invoice |
| **E3** | Payment exists without a matching invoice |
| **E4** | February EUR/USD FX rate is incorrect |
| **E5** | One side of an intercompany entry is deleted |
| **E6** | One intercompany local amount is blank |

Clean and error data are kept deterministic so the same inputs can be tested repeatedly.

### 2. Three-way invoice matching

`src/match.py` compares:

- purchase orders
- invoices
- payments

It identifies matched invoices, amount mismatches, missing POs, unpaid/partially-paid invoices, overpayments, duplicate invoices, and payments without invoices.

The matching engine is deliberately ordinary Python rather than an LLM.

### 3. Intercompany recharge and elimination

`src/intercompany.py` allocates shared costs between entities, applies the configured markup, converts amounts using month-end EUR/USD rates, creates payer/receiver entries, and checks intercompany elimination.

FX convention:

```text
1 EUR = X USD

EUR -> USD: multiply by X
USD -> EUR: divide by X
```

### 4. Independent FX controls

The FX control family (`FXC-01` to `FXC-06`) does more than check the FX table itself. `FXC-05` independently re-performs intercompany calculations using the reference FX rates, so a wrong FX rate cannot simply balance itself by being used on both sides of the same calculation.

Critical FX controls include:

- `FXC-03` — supplied FX rates agree with the reference rates
- `FXC-05` — intercompany recharge amounts re-perform correctly using reference FX rates

### 5. Close decision and report package

`src/pipeline.py` orchestrates the close without owning the accounting rules.

The close can end in:

```text
PASS
PASS_WITH_WARNINGS
FAIL
```

A critical control failure produces:

```text
report_allowed: false
```

and a blocked exception report rather than an approval report.

The generated decision package includes the close report, control results, close decision, audit artifacts, exception records, exception register, and a package manifest with SHA-256 hashes.

### 6. Audit and exception workflow

The project keeps the close evidence separate from post-close review activity:

```text
out/<dataset>/       immutable close package
review/<dataset>/    mutable reviewer workspace
```

The review workflow supports:

- opening and listing exceptions
- assigning owners
- moving exceptions through `OPEN -> INVESTIGATING -> RESOLVED`
- requiring a human resolution comment and evidence
- preserving a caller-supplied timestamp in the history

Investigating or resolving an exception does **not** rewrite the immutable close package or change the original close decision.

### 7. AI-assisted exception investigation

The AI layer is intentionally isolated behind the `InvestigationProvider` interface.

Gemini is the default live provider:

```text
GEMINI_API_KEY
GEMINI_MODEL=gemini-3.6-flash
```

Claude remains available as an optional provider:

```text
ANTHROPIC_API_KEY
ANTHROPIC_MODEL
```

The AI receives an investigation packet containing the exception and explicitly supplied evidence. The Step 13A validator then enforces the boundary:

- no close decision changes
- no `report_allowed` changes
- no control-result overrides
- no exception resolution by the model
- no invented evidence citations
- missing evidence must remain an uncertainty

The model's self-reported provenance is discarded and replaced by trusted provider metadata.

## Quick start

### 1. Create the environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. Generate or refresh the synthetic data

```powershell
python src\generate_data.py
python src\generate_data.py --with-errors
```

### 3. Generate sample invoice PDFs

```powershell
python src\generate_invoice_pdfs.py --limit 15
python src\extract_invoices.py
```

### 4. Run a clean close

```powershell
python -m src.pipeline --data-dir data\raw --dataset-label clean
```

A successful clean run produces an approved close package under `out\clean\`.

### 5. Run the planted E4 FX error

```powershell
python -m src.pipeline --data-dir data\raw --fx-rates data\error_data\fx_rates.csv --fx-reference data\raw\fx_rates_expected.csv --dataset-label E4
```

The expected blocking controls are:

```text
FXC-03
FXC-05
```

with:

```text
close_status: FAIL
report_allowed: false
```

### 6. Create a review workspace

```powershell
python -m src.review_cli create --package-dir out\E4 --review-dir review
```

List the exceptions:

```powershell
python -m src.review_cli list --dataset-label E4 --review-dir review
```

### 7. Investigate an exception with Gemini

Set the API key in your environment; never put it in source code or commit it to Git.

```powershell
$env:GEMINI_API_KEY = "<your-key>"
```

Then:

```powershell
python -m src.review_cli investigate --dataset-label E4 --review-dir review --exception-id EXC-FXC-03-E4 --occurred-at 2026-04-01T10:00:00Z --provider gemini --source-file data/raw/fx_rates_expected.csv
```

The result is stored in:

```text
review\E4\investigation_advice.json
```

The AI advice is advisory only. The close remains blocked until the deterministic controls and human review workflow say otherwise.

## Running the controls directly

Clean dataset:

```powershell
python src\controls.py --fx-rates data\raw\fx_rates.csv --fx-reference data\raw\fx_rates_expected.csv
```

E4 FX error:

```powershell
python src\controls.py --fx-rates data\error_data\fx_rates.csv --fx-reference data\raw\fx_rates_expected.csv --dataset-label E4
```

## Testing

Run the complete local suite:

```powershell
python -m pytest tests -v
```

At the current Gemini integration milestone:

```text
369 passed, 1 skipped
```

The skipped test is the optional live Claude integration test; normal CI does not require an Anthropic API key.

GitHub Actions runs the test suite automatically for pull requests targeting `main` and pushes to `main`.

## Important design boundaries

The accounting and control path is deterministic:

```text
match.py
intercompany.py
controls.py
pipeline.py
report.py
```

The investigation path is advisory:

```text
investigation.py
        |
        +--> Gemini provider
        +--> Claude provider
        |
        v
13A validation
        |
        v
human reviewer
```

This separation is intentional. An LLM is allowed to explain a control failure and suggest what a reviewer should check. It is not allowed to become the accounting system of record.

## Repository structure

```text
month-end-close-assistant/
|
+-- data/
|   +-- raw/                 clean synthetic source data
|   +-- error_data/          planted-error scenarios
|   +-- generated_pdfs/      synthetic invoice PDFs
|
+-- docs/                    control and architecture documentation
+-- src/
|   +-- generate_data.py
|   +-- generate_invoice_pdfs.py
|   +-- extract_invoices.py
|   +-- match.py
|   +-- intercompany.py
|   +-- controls.py
|   +-- pipeline.py
|   +-- report.py
|   +-- audit.py
|   +-- exception_workflow.py
|   +-- review_workspace.py
|   +-- review_cli.py
|   +-- investigation.py
|   +-- investigation_review.py
|   +-- gemini_provider.py
|   +-- claude_provider.py
|   +-- paths.py
|
+-- tests/                   unit and integration tests
+-- soda/                    data-quality configuration
+-- .github/workflows/       CI configuration
+-- out/                     generated close packages (ignored by Git)
+-- review/                  generated review workspaces (ignored by Git)
```

## Reproducibility and safety

- Source datasets are synthetic.
- Close outputs are generated outside `data/raw`.
- Generated `out/` and `review/` directories are ignored by Git.
- Fixed run timestamps can be supplied so decision packages are byte-identical for the same inputs.
- SHA-256 hashes are recorded in the close package manifest.
- The review workspace records the hashes of the package it came from.
- AI providers do not store API keys on the provider instance or place keys into prompts.
- AI results pass through the same Step 13A validation gate before being persisted.

## Documentation

Key design documents include:

- `docs/STEP9_PIPELINE_DESIGN.md` — orchestration, close package, audit, and review architecture
- `docs/STEP13_AI_INVESTIGATION_DESIGN.md` — AI investigation boundary and provider design
- `docs/STEP13C_REVIEW_INTEGRATION.md` — review-side investigation integration

## GitHub / CI

The repository uses GitHub Actions with Python 3.12 and runs:

```bash
python -m pytest tests -v
```

The current `main` branch contains the merged Gemini investigation integration.

## License / data note

This repository uses synthetic finance data for demonstration and portfolio purposes. It is not connected to a production accounting system and should not be used as a source of real financial records.

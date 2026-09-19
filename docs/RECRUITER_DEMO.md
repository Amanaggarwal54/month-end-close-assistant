# Recruiter Demo

The fastest way to see the Month-End Close Assistant working end to end is the demo runner.

## What the demo shows

The demo deliberately runs both a successful and a blocked month-end close, then creates the review workspace for the blocked case.

```text
Synthetic finance data
        |
        v
Deterministic finance processing
        |
        v
Close controls
        |
        +----------------------+
        |                      |
        v                      v
      PASS              FAIL / BLOCKED
        |                      |
        v                      v
Approved report        Exception report
                               |
                               v
                       Review workspace
```

## Deterministic demo

Run without any API key:

```powershell
python .\scripts\demo.py
```

The demo runs two scenarios.

### Clean close

Expected result:

```text
Close status:   PASS
report_allowed: true
Approved close report generated
```

The package is written to:

```text
out/demo-clean/
```

### E4 incorrect FX rate

The E4 scenario uses the planted incorrect February 2026 EUR/USD rate.

Expected blocking controls:

```text
FXC-03
FXC-05
```

Expected decision:

```text
Close status:   FAIL
report_allowed: false
```

The pipeline produces a blocked exception report instead of an approval report.

The package is written to:

```text
out/demo-e4/
```

## Review workspace

The deterministic demo also creates a review workspace from the immutable E4 package:

```text
review/demo-e4/exception_resolution.json
```

The workspace is separate from the close package. Reviewer actions do not rewrite the original close evidence or change the original close decision.

The E4 scenario contains two exceptions in the review workspace.

## AI-assisted demo

With `GEMINI_API_KEY` configured:

```powershell
python .\scripts\demo.py --with-ai
```

This adds one advisory investigation:

```text
Blocked E4 exception
        |
        v
Investigation packet
        |
        v
Gemini
        |
        v
13A validation
        |
        v
Advisory investigation record
```

The current verified Gemini configuration uses:

```text
provider: google-gemini
model: gemini-3.5-flash-lite
```

A successful investigation is stored at:

```text
review/demo-e4/investigation_advice.json
```

The advisory includes observations, possible causes, recommended checks, evidence references and uncertainties.

## AI control boundary

Gemini is deliberately not the accounting decision-maker.

The investigation layer cannot:

```text
change close_status
change report_allowed
override a control result
approve the close
resolve an exception
invent evidence
invent evidence references
```

The deterministic close remains the source of truth.

For the E4 demonstration, the stored investigation still records:

```text
close_status: FAIL
report_allowed: false
```

A human reviewer remains responsible for the exception lifecycle and any final resolution.

## Optional live AI failure behavior

The Gemini step is optional.

If `GEMINI_API_KEY` is missing, the demo skips the AI investigation and still completes the deterministic close demonstration.

If the Gemini service temporarily fails, the demo reports the AI failure but still completes the deterministic close demonstration.

This keeps the finance workflow independent from the availability of an external AI service.

## Generated outputs

After the deterministic demo, the main outputs are:

```text
out/demo-clean/
    approved close package

out/demo-e4/
    blocked exception package

review/demo-e4/
    reviewer workspace
```

With `--with-ai`, the review directory also contains:

```text
review/demo-e4/investigation_advice.json
```

## Reproduce the demo manually

Clean close:

```powershell
python -m src.pipeline --dataset-label demo-clean
```

E4 blocked close:

```powershell
python -m src.pipeline --dataset-label demo-E4 --fx-rates .\data\error_data\fx_rates.csv --fx-reference .\data\error_data\fx_rates_expected.csv --no-fail-on-blocked
```

Create the review workspace:

```powershell
python -m src.review_cli create --package-dir .\out\demo-e4 --review-dir .\review --force
```

Run the Gemini investigation:

```powershell
python -m src.review_cli investigate --dataset-label demo-E4 --review-dir review --exception-id EXC-FXC-03-E4 --occurred-at 2026-09-19T10:00:00Z --provider gemini --source-file data/raw/fx_rates_expected.csv
```

## Data note

The repository uses synthetic finance data for demonstration and portfolio purposes. It is not connected to a production accounting system and should not be used as a source of real financial records.

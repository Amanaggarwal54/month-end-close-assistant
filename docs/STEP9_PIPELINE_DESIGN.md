# Step 9 — End-to-end pipeline / one-command close: design

Specification only. No code until this is agreed.

---

## 1. The problem to solve first

Requirements 2 and 10 point at something already in the repository: **the
orchestration exists**. The `__main__` block of `src/report.py` already loads the
source files, runs matching, generates intercompany entries, checks elimination,
runs the controls, decides the close, builds the model and writes the package.
That is the pipeline.

So Step 9 has three possible shapes, and only one of them satisfies the
requirements:

| Option | What it means | Verdict |
|---|---|---|
| A. `pipeline.py` re-implements the sequence | two copies of the same orchestration | **Violates requirement 2.** The copies drift, and requirement 10 ("same package as running the modules directly") becomes untestable the moment one changes |
| B. `pipeline.py` owns the orchestration; `report.py`'s `__main__` delegates to it | one implementation, two entry points | **Recommended** |
| C. `pipeline.py` shells out to `report.py` as a subprocess | fragile, loses exceptions and typed results | Rejected |

**Recommendation: option B.** It requires one edit outside Step 9: the `__main__`
block of `report.py` is replaced by a short delegation to
`pipeline.run_close(...)`. No function in `report.py` changes — not
`build_report_model`, not `render_pdf`, not `write_decision_package` — and no
test of Steps 0–8 changes.

This is the "unless absolutely necessary" case in requirement 12: keeping both
CLIs implemented separately would guarantee the drift that requirement 10 exists
to prevent. If you would rather leave `report.py` untouched, the alternative is
to **delete** its `__main__` block and make `pipeline.py` the only entry point,
which is a larger change to Step 8, not a smaller one.

---

## 2. Architecture

```
                        ┌──────────────── src/pipeline.py ────────────────┐
data/raw or             │                                                 │
data/error_data ───────►│  load_artefacts()   I/O: read the CSVs          │
                        │        │                                        │
                        │        ▼                                        │
                        │  execute_close()    pure: no I/O, no mutation   │
                        │        │  match.three_way_match                 │
                        │        │  intercompany.generate_entries         │
                        │        │  intercompany.check_elimination        │
                        │        │  controls.run_controls                 │
                        │        │  controls.decide_close                 │
                        │        │  audit.build_exception_records         │
                        │        │  audit.build_audit_trail               │
                        │        │  report.build_report_model             │
                        │        ▼                                        │
                        │  write_outputs()    I/O: report.write_package   │
                        └───────────────────────┬─────────────────────────┘
                                                ▼
                                     out/<label>/ package + exit code
```

Same three-layer split already used by `controls.py` and `report.py`: one
function reads, one function computes and returns objects, one function writes.
Tests assert against the middle layer without touching the filesystem.

**`pipeline.py` calls existing functions and nothing else.** It contains no
matching rule, no FX conversion, no tolerance comparison, no severity logic and
no formatting. Its only original code is argument handling, sequencing, the
console summary and the exit code.

---

## 3. Public API

```python
@dataclass(frozen=True)
class CloseRunConfig:
    data_dir: Path = Path("data/raw")
    fx_rates: Path | None = None          # defaults to <data_dir>/fx_rates.csv
    fx_reference: Path | None = None      # defaults to data/raw/fx_rates_expected.csv
    ic_entries: Path | None = None        # pre-generated entries for E5 / E6
    dataset_label: str = "clean"
    out_dir: Path = Path("out")
    run_timestamp: str | None = None      # fixed value makes the run byte-identical
    max_exception_rows: int = 25
    control_config: ControlConfig | None = None
    write_package: bool = True            # False = compute only, write nothing

@dataclass(frozen=True)
class CloseRunResult:
    config: CloseRunConfig
    artefacts: CloseArtefacts      # match_results, ic_entries, ic_elimination, sources
    control_results: pd.DataFrame
    decision: dict
    exception_records: list[dict]  # built by audit.py, carried to the package
    audit_trail: dict              # built by audit.py, carried to the package
    model: ReportModel
    written: dict[str, Path]       # empty when write_package is False
    exit_code: int                 # 0 allowed, 2 blocked

def load_artefacts(config) -> SourceFrames        # I/O only
def execute_close(sources, config) -> CloseRunResult   # pure
def run_close(config) -> CloseRunResult           # load → execute → write
```

`run_close()` is what both CLIs call. Returning typed results rather than
printing means tests can assert on the decision without capturing stdout, and a
future scheduler or notebook can use the same call.

---

## 4. CLI

Identical flags to `report.py`, so no existing command line breaks:

```
python src\pipeline.py
    [--data-dir data\raw]
    [--fx-rates PATH]            [--fx-reference PATH]
    [--ic-entries PATH]
    [--dataset-label clean]
    [--out-dir out]
    [--run-timestamp 2026-03-31T18:00:00Z]
    [--max-exception-rows 25]
    [--fail-on-blocked | --no-fail-on-blocked]
    [--dry-run]                  # compute and print, write nothing
    [--quiet]
```

`--dry-run` is the one new flag: it answers "would this close pass?" without
producing an artefact, which is useful in a pre-commit or CI check.

### Scenario coverage (requirement 3)

| Case | Command |
|---|---|
| Clean | `--data-dir data\raw --dataset-label clean` |
| E1 / E2 | `--data-dir data\error_data --fx-reference data\raw\fx_rates_expected.csv --dataset-label E1-E2` |
| E3 | same as E1/E2 (the orphan payment lives in the same error directory) |
| E4 | `--data-dir data\raw --fx-rates data\error_data\fx_rates.csv --dataset-label E4` |
| E5 | `--data-dir data\raw --ic-entries data\error_data\intercompany_entries_E5.csv --dataset-label E5` |
| E6 | `--data-dir data\raw --ic-entries data\error_data\intercompany_entries_E6.csv --dataset-label E6` |

Note that `--data-dir data\error_data` needs `--fx-reference` pointing at
`data/raw/fx_rates_expected.csv`, because the error directory has no reference
file. **Open question 3** below asks whether the reference should always default
to `data/raw/fx_rates_expected.csv` regardless of `--data-dir`, which is the
current behaviour in `report.py` and which I recommend keeping.

---

## 5. Exit codes and error handling

| Code | Meaning | Condition |
|---|---|---|
| 0 | report allowed | `decision["report_allowed"] is True` |
| 2 | close blocked | `report_allowed is False` and `--fail-on-blocked` (default) |
| 1 | execution error | missing input file, unreadable CSV, `ProtectedPathError`, any unexpected exception |

The distinction matters operationally: 2 means the controls did their job, 1
means the job never got far enough to have an opinion. On code 1 the pipeline
prints the exception type and message, never a stack trace, and writes nothing.

With `--no-fail-on-blocked`, a blocked close still produces the exception report
and still prints `report_allowed: false`, but exits 0 — for the case where a
wrapper wants the artefact without the non-zero status.

---

## 6. Safety and determinism

- **No writes under `data/raw`.** Every output path passes through the existing
  `assert_writable()` guard, inherited by calling `report.write_decision_package`.
  The pipeline adds no new write path of its own.
- **Sources are opened read-only.** `execute_close()` copies nothing back, and a
  test hashes every file under `data/raw` before and after a full run.
- **`--run-timestamp` fixes the timestamp** that flows into `run_controls`,
  `decide_close` and the report, producing byte-identical packages.
- **One timestamp for the whole run.** The pipeline resolves it once and passes
  the same value everywhere; this is exactly the kind of thing that silently
  drifts when two CLIs each call `datetime.now()`.

---

## 7. Console output

Five lines by default, suppressed with `--quiet`:

```
Dataset:        E4
Close status:   FAIL  (report_allowed=false)
Blocking:       FXC-03, FXC-05
Document:       out\e4\close_exception_report_e4_20260331T180000Z.pdf
Package:        out\e4
```

Plus, when the close is blocked, one line per blocking control with its
explanation, so the terminal tells you what broke without opening the PDF. On a
`--dry-run` the document and package lines are replaced by
`Dry run: no files written`.

---

## 8. Test plan (`tests/test_pipeline.py`)

| # | Test | Assertion |
|---|---|---|
| 1 | Clean end-to-end | `close_status PASS`, `report_allowed True`, `exit_code 0`, package written |
| 2 | E4 blocked | `exit_code 2`, blocking controls exactly `["FXC-03", "FXC-05"]`, exception report filename |
| 3 | E5 blocked | `exit_code 2`, `ICO-01` among blocking controls |
| 4 | E6 blocked | `exit_code 2`, `ICO-03` among blocking controls |
| 5 | E1/E2/E3 blocked | `exit_code 2`, blocking set is `{MAT-03, MAT-04, MAT-06}` |
| 6 | Package existence | PDF, `control_results.csv`, `close_decision.json`, `audit_trail.json`, `exceptions.json`, `package_manifest.json` all present under `out/<label>/` |
| 7 | **Parity with direct module use** | run the modules by hand with the same fixed timestamp, then run the pipeline; assert the SHA-256 of the PDF, CSV and JSON match. This is requirement 10, tested rather than asserted in prose |
| 8 | Source protection | `out_dir=data/raw` raises `ProtectedPathError` and the CLI returns 1 |
| 9 | No mutation | SHA-256 of every file under `data/raw` unchanged after a full run; input DataFrames unchanged |
| 10 | Determinism | two runs with the same `--run-timestamp` give identical PDF bytes |
| 11 | Exit codes | 0 clean, 2 blocked, 1 on a missing input file, 0 blocked with `--no-fail-on-blocked` |
| 12 | Dry run | nothing written, decision still returned |
| 13 | No duplicated logic | `pipeline.py` source contains no arithmetic on amounts or rates: assert the module defines no function that matches, converts or compares amounts, e.g. by asserting the source has no `round(`, `* 1.0`, `/ rate` patterns and imports its work from the four modules |

Test 13 is a dependency and structure guard rather than a lexical one: it asks
where the code comes from, not which characters it contains. Test 7 remains the
primary behavioural guarantee.

Expected suite size after Step 9: about 80 tests.

---

## 9. Files created or changed

| Path | Change |
|---|---|
| `src/pipeline.py` | new — the only orchestration implementation |
| `tests/test_pipeline.py` | new, ~13 tests |
| `src/report.py` | **`__main__` block only**: replaced with a delegation to `pipeline.run_close()`. No function changes |
| `README.md` | "One-command close" section with the scenario table |
| `docs/STEP9_PIPELINE_DESIGN.md` | this document |

Unchanged: `match.py`, `intercompany.py`, `controls.py`, `generate_data.py`,
`plant_downstream_errors.py`, `verify_dataset.py`, every dataset, and every
existing test.

---

## 10. Open questions

1. **Option B** (§1): `report.py`'s `__main__` delegates to `pipeline.py`.
   Confirm, since it is the one edit inside Step 8.
2. **`--dry-run`**: keep or drop?
3. **FX reference default**: always `data/raw/fx_rates_expected.csv` even when
   `--data-dir` is `data/error_data`. I recommend keeping it, because the
   reference is by definition not part of the dataset under test.
4. **Test 13** (source-level no-logic guard): keep or drop?
5. **Out of scope unless you want it:** a `--scenario all` mode that runs clean
   plus E1–E6 in one command and prints a results table. It would make a
   compelling README demo and a single screenshot for reviewers, but it is a new
   feature rather than orchestration, so I have left it out of this design.

---

## 11. One further change to report.py, found while implementing

The parity test (test 7) failed on its first run, for a real reason. The manual
run named the FX reference with an absolute path and the pipeline used its
relative default, so the two provenance appendices printed different strings for
the same file and the PDFs differed by one line while every figure matched.

`report.describe_inputs` now renders each input path relative to the working
directory when the file sits under it, falling back to an absolute path
otherwise. Two callers that name the same file differently therefore produce
identical documents, and a committed example report no longer carries the
author's home directory - which matters for a repository a reviewer will open.

This is a second edit inside Step 8, beyond the approved `__main__` delegation.
It is one function, the hashes and roles are unchanged, and the alternative
(resolving every path to absolute inside the pipeline) would have put developer
directory names into the example PDFs.

---

## 12. Amendment (Step 11B): the audit artefacts in the package

The pipeline now calls `audit.build_exception_records()` and
`audit.build_audit_trail()` between `decide_close()` and `build_report_model()`,
and carries both objects into the report model. The package therefore contains
six artefacts rather than four:

```
out/<dataset_label>/
├── close_report_<label>_<stamp>.pdf   (or close_exception_report_… when blocked)
├── control_results.csv
├── close_decision.json
├── audit_trail.json        summary counts, the close verdict, and the exceptions
├── exceptions.json         the same exception records as a flat list
└── package_manifest.json
```

`audit_trail.json` embeds the exception list that `exceptions.json` holds flat.
The duplication is deliberate: the trail is self-contained for a reader who is
handed only that file, while the flat list is the convenient shape for tooling.
Both are written from one object in one call, so they cannot disagree, and a
test asserts they match.

**Where the boundary sits.** `audit.py` is the only module that decides what an
exception record contains or how the records are ordered. `report.py` receives
the finished objects on the report model and serialises them; it does not import
`audit`, does not call either builder, and does not inspect the control results
to work out what an exception is. A presentation layer that re-derived the
exception list could disagree with the audit file written beside it, which is
the one inconsistency this package must not be able to contain. Two tests hold
that line: one parses `report.py` and asserts neither builder is imported or
called, and one writes a package from a model carrying no audit data and asserts
the files come out empty rather than being reconstructed.

Serialisation is `indent=2`, `sort_keys=True`, UTF-8. Sorting fixes the key
order; list order is left alone, so the exception ordering `audit.py` already
determined is what reaches disk. `package_manifest.json` hashes both new files
and still excludes itself.

# Step 6 — Validation and control layer: design (rev. 2)

Revised after review. Changes in this revision: the control engine is a pure
function that writes nothing (§1, §9); `fx_rates_expected.csv` is described as a
synthetic reference benchmark rather than an external authority (§5, §11); the
FX plausibility bounds are configuration with documented defaults (§2, §11).
The E1–E6 test matrix, the two-layer FX design and the severity model are
unchanged.

Design only. No code is implemented here, and `match.py`, `intercompany.py`
and every dataset stay untouched.

---

## 1. Step 6 architecture

The control layer sits **above** Steps 4 and 5 and never re-implements their
logic. It consumes their outputs plus the original source files, and produces
one table of control results and one close decision.

```
data/raw (or data/error_data)
        │
        ├── match.three_way_match()            → match_results
        ├── intercompany.generate_entries()    → ic_entries
        │        └── check_elimination()       → ic_elimination
        │
        ▼
src/controls.py   run_controls(sources, match_results, ic_entries,
                               ic_elimination, fx_actual, fx_reference, config)
        │   pure function — returns objects, writes nothing
        ├── control_results   (DataFrame, one row per control)
        └── close_decision    (deterministic PASS / PASS_WITH_WARNINGS / FAIL)
        │
        ▼
src/pipeline.py (or the controls CLI)
        │   the only layer that touches the filesystem
        ├── writes control_results.csv
        ├── writes close_decision.json
        ▼
Step 7 report.py — produces the close report
ONLY when close_decision.report_allowed is True
```

### The central distinction: three control families

The E4 result proves why one family is not enough.

| Family | Question it answers | Detects | Blind to |
|---|---|---|---|
| **Reconciliation** (internal consistency) | Do the artefacts agree with each other? | E1, E2, E3, E5, E6 | An error applied consistently everywhere |
| **Source accuracy** (external reference) | Do the inputs agree with an authority outside the system? | E4 | Errors introduced after the source is read |
| **Completeness** (population coverage) | Is everything that should be here actually here? | dropped rows, missing months or entities | Wrong values in present rows |

E4 passes every reconciliation control because both sides of each recharge use
the same wrong rate. Receivable equals payable, so the books are internally
perfect and externally wrong. That is why FX accuracy must be tested against
`fx_rates_expected.csv`, never against the elimination result.

### Design rules for the layer

1. **Controls consume artefacts, they do not recompute business logic** — with
   one deliberate exception, `FX-05` (see §5), which independently re-performs
   the recharge arithmetic at reference rates. A control that reuses the code it
   is testing cannot fail when that code is wrong.
2. **A control that cannot be executed is a FAIL, not a skip.** Missing input,
   an exception during evaluation, or an empty artefact returns FAIL with the
   reason. Silence must never read as success.
3. **Every control returns the same row shape** (§9), so results can be filtered,
   totalled, exported and diffed between runs.
4. **The control engine writes nothing at all.** `run_controls()` and
   `decide_close()` are pure functions: inputs in, objects out, no file I/O and
   no mutation of the frames they are given. Persisting results is the job of
   the CLI or pipeline layer (§9). This keeps the engine reusable from a test,
   a notebook or a future scheduler without it leaving files behind, and it
   keeps "what the controls concluded" separate from "where we put the
   evidence".

---

## 2. Control catalogue

Severity column: **C** = critical (blocks the close), **W** = warning.

### Group MAT — three-way matching (consumes `match_results`)

| ID | Name | Test | Sev | Catches |
|---|---|---|---|---|
| MAT-01 | Invoice population complete | count of `record_type == INVOICE` rows equals row count of `invoices.csv`; the set of `invoice_id` values is identical | C | silent drops in a join |
| MAT-02 | Payment population complete | every `payment_id` in `payments.csv` appears exactly once across the `payment_ids` column of the result; count reconciles | C | silent drops, double counting |
| MAT-03 | No duplicate invoices | count of `DUPLICATE_INVOICE` is 0 | C | **E2** |
| MAT-04 | Invoice agrees with PO | count of `AMOUNT_MISMATCH` is 0 | C | **E1** |
| MAT-05 | Every invoice has a valid PO | count of `NO_PO` is 0 | C | unauthorised spend |
| MAT-06 | No payment without an invoice | count of `PAYMENT_WITHOUT_INVOICE` is 0 | C | **E3** |
| MAT-07 | No overpayments | count of `OVERPAID` is 0 | C | cash paid beyond the obligation |
| MAT-08 | Unpaid invoices reviewed | count of `UNPAID` reported, not blocking | W | normal open payables |
| MAT-09 | Partial payments reviewed | count of `PARTIALLY_PAID` reported, not blocking | W | instalments in progress |
| MAT-10 | Status domain valid | every row carries a status from the eight defined values; no nulls | C | logic gaps producing blank statuses |
| MAT-11 | Data-quality flags reviewed | count of non-empty `data_quality_flag` on rows whose status is `MATCHED` | W | a matched row with a missing VAT field |
| MAT-12 | Row-count reconciliation | `invoice rows + orphan payment groups == result rows` | C | fabricated or lost rows |

**Why MAT-07 is critical but MAT-08 and MAT-09 are warnings.** An overpayment
means money left the company that no invoice supports; the close cannot assert
correctness while it stands. Unpaid and partially paid invoices are the normal
state of a payables ledger at any month-end — blocking on them would make the
control layer cry wolf, and a control everyone learns to override is worse than
no control.

### Group ICO — intercompany structure (consumes `ic_entries`, `ic_elimination`)

| ID | Name | Test | Sev | Catches |
|---|---|---|---|---|
| ICO-01 | Every pair has two sides | count of `MISSING_RECEIVABLE` + `MISSING_PAYABLE` in the elimination result is 0 | C | **E5** |
| ICO-02 | No duplicate sides | count of `DUPLICATE_SIDE` is 0 | C | double postings |
| ICO-03 | Accounting amounts complete | no accounting entry has a null `local_amount`, `eur_equivalent`, `currency` or `pair_id` | C | **E6** (both variants) |
| ICO-04 | Elimination to zero | every pair's `difference_eur` ≤ 0.01; count of `AMOUNT_MISMATCH` is 0 | C | broken pairs |
| ICO-05 | Aggregate elimination | total receivable EUR minus total payable EUR is 0.00 | C | portfolio-level imbalance |
| ICO-06 | Entry status valid | every accounting entry has status `VALID`; count of `EXCEPTION` rows is 0 | C | a shared cost that produced no recharge |
| ICO-07 | Entities valid | `entity` and `counterparty_entity` are known entities; `entity != counterparty_entity` | C | self-dealing, typos |
| ICO-08 | Currency matches entity | each side's `currency` equals the entity's functional currency (BE01/BE02 EUR, US01 USD) | C | a USD amount booked as EUR |
| ICO-09 | FX consistent within pair | count of `FX_INCONSISTENT` is 0 | C | two sides at different rates |
| ICO-10 | Pair traceability | every `pair_id` maps to exactly one `cost_id`, one payer and one receiver | C | corrupted identifiers |

### Group SRC — shared-cost source quality (consumes `ic_entries` exception rows plus `shared_costs.csv`)

`intercompany.py` already validates each shared cost and emits `EXCEPTION`
rows. These controls **read those outcomes** instead of repeating the rules, and
add only what the module does not test.

| ID | Name | Test | Sev |
|---|---|---|---|
| SRC-01 | Shared-cost coverage | every `cost_id` in the source appears in `ic_entries` as either entries or an exception; no cost silently vanishes | C |
| SRC-02 | No invalid shared costs | count of exception rows with `INVALID_SHARE_TOTAL`, `MISSING_SHARE`, `MISSING_AMOUNT`, `MISSING_CURRENCY`, `INVALID_ENTITY`, `INVALID_MONTH` is 0 | C |
| SRC-03 | FX available where required | count of `MISSING_FX` exceptions is 0 | C |
| SRC-04 | Costs fully recharged or explained | `NO_RECHARGE` rows reported with their cost_id | W |
| SRC-05 | No negative amounts or shares | `amount` > 0 and every share in [0, 1] | C |
| SRC-06 | Month within scope | every shared-cost month falls in Jan–Mar 2026 | C |

SRC-05 is critical because the specification defines no credit-note treatment.
A negative shared cost would produce a negative recharge that still eliminates —
another internally consistent error. If credit notes are added later, this
becomes a supported case with its own rules, not an exception to ignore.

### Group FXC — independent FX validation (see §5 for detail)

| ID | Name | Test | Sev | Catches |
|---|---|---|---|---|
| FXC-01 | FX coverage | every month requiring conversion has exactly one rate in the actual file | C | missing month |
| FXC-02 | No duplicate FX months | no `month_end` appears twice in the actual file | C | ambiguous rate |
| FXC-03 | Rate matches reference | for every month, \|actual − reference\| ≤ 0.000001 | C | **E4** |
| FXC-04 | No unexpected FX months | months in the actual file that are absent from the reference are reported | W | stray rows |
| FXC-05 | Recharge re-performance at reference rates | independently recompute each entry's EUR equivalent from `source_amount × share × (1 + markup)` at the **reference** rate; difference ≤ 0.01 | C | **E4** at entry level, plus tampering after generation |
| FXC-06 | Rate plausibility | every rate is positive and within the configured band, default 0.5–2.0 | C | 10.9 instead of 1.09 |

FXC-06's bounds are **configuration, not a rule from the specification**. They
live in the control config as `fx_min` / `fx_max` with defaults of 0.5 and 2.0,
and are documented as a project assumption. The default band is wide enough that
no plausible EUR/USD rate trips it, while a decimal-point error such as 10.9 or
0.109 fails immediately. Passing them as config means changing the band is a
configuration decision with a visible default, not a code edit.

### Group CVG — period and entity completeness

| ID | Name | Test | Sev |
|---|---|---|---|
| CVG-01 | Months in scope | every month present in invoices and shared costs is within Jan–Mar 2026 | C |
| CVG-02 | Expected months present | each of the three months appears in the invoice population | W |
| CVG-03 | Entities known | every entity referenced anywhere is BE01, US01 or BE02 | C |
| CVG-04 | Intercompany output coverage | every month that has shared costs also has intercompany entries | C |

CVG-02 is a warning because "no activity in a month" is a legitimate business
state; the specification does not require activity in every month.

---

## 3. Severity policy

The rule to state in the README and apply consistently:

> **Critical** — if this control fails, a number in the close report is wrong,
> unverifiable, or missing. The report must not be produced.
>
> **Warning** — the condition is a real business fact that a reviewer should
> see, but the reported numbers remain correct and complete.

Applying that rule:

- **Critical:** anything affecting completeness (a row lost or invented), any
  broken or one-sided intercompany pair, any missing amount, any failed
  elimination, any deviation from reference FX, any invalid entity, currency or
  month, and any control that could not be executed.
- **Warning:** unpaid and partially paid invoices, a shared cost with no
  recharge, an unexpected extra FX month, a month with no activity, and
  data-quality flags on rows that are otherwise matched.

Deliberately **not** warnings: duplicate invoices, amount mismatches, orphan
payments and FX deviations. Each one means the reported figures are wrong.

---

## 4. Overall close decision

Deterministic, evaluated in this order:

```
if any control has status ERROR (could not be executed):     FAIL
elif any CRITICAL control has status FAIL:                   FAIL
elif any WARNING control has status FAIL:                    PASS_WITH_WARNINGS
else:                                                        PASS
```

The decision object carries:

| Field | Meaning |
|---|---|
| `close_status` | PASS / PASS_WITH_WARNINGS / FAIL |
| `config_used` | tolerances and bounds in force for this run |
| `report_allowed` | True only for PASS and PASS_WITH_WARNINGS |
| `critical_failed` | count |
| `warnings` | count |
| `blocking_controls` | list of failed critical check_ids |
| `run_timestamp`, `dataset_label`, `controls_version` | provenance for the audit trail |

`decide_close()` returns this object; it does not write it. `report.py` in Step 7
takes `report_allowed` as its only gate, so the blocking logic lives in exactly
one place.

---

## 5. Independent FX control design

### The comparison

Build two normalised tables — `actual` from the FX file that was used, and
`reference` from `fx_rates_expected.csv` — keyed on a normalised `YYYY-MM` month
(the same normalisation `intercompany.py` uses, so `2026-02-28` and `2026-02`
never miss each other). Then **outer join** on the month key.

| Condition | How it appears in the join | Control | Severity |
|---|---|---|---|
| Different rate | both present, values differ | FXC-03 | Critical |
| Missing FX month | reference present, actual null | FXC-01 | Critical |
| Unexpected FX month | actual present, reference null | FXC-04 | Warning |
| Duplicate FX month | actual month key count > 1 | FXC-02 | Critical |

FXC-02 must run **before** the comparison and on the raw file, not on a lookup
dictionary. A dict build silently keeps one of the duplicates, so by the time
the rate is in memory the ambiguity is invisible — this is a real weakness of
consuming `_build_fx_lookup` output instead of the file.

### What the reference file is

`fx_rates_expected.csv` is the **synthetic reference benchmark for this
project**, not an external authority. The control is designed so that in a
production implementation the reference would be replaced by an independently
sourced market rate (an ECB reference feed, a treasury system, a rate provider)
with no change to the control logic — only to where `fx_reference` is loaded
from. State exactly that in the README, because a reviewer will otherwise ask
why a file inside the repo counts as independent.

### Tolerance: use 0.000001, not 0.01

A 0.01 tolerance on a rate of 1.09 is a **0.9% band**. On the clean population
of EUR 182,096 that permits roughly EUR 1,600 of undetected error, and it would
not even catch E4 reliably if the planted error were 1.095 instead of 1.19.
Rates are quoted to four or five decimals and the reference file is
authoritative, so the correct test is equality within floating-point noise:
`1e-6`. The 0.01 tolerance belongs to **amounts**, where it represents a cent;
applying an amount tolerance to a rate confuses two different units.

Optionally add a **WARNING** control on month-on-month rate movement above, say,
10%, which would flag 1.08 → 1.19 even with no reference file available. Useful
in the real world where no "expected" file exists.

### Two layers, deliberately

- **FXC-03 validates the input file.** Fast, direct, and it fails E4 immediately.
- **FXC-05 validates the output.** It re-performs each recharge from
  `source_amount`, `share_pct` and `markup_pct` at the reference rate, then
  compares with the `eur_equivalent` actually booked.

FXC-05 exists because FXC-03 only proves the *file* was right. If the module
were pointed at a different file, or an entry were edited after generation, the
file check still passes. FXC-05 tests the artefact that the report is built
from, which is the thing that actually matters. It is the one place the control
layer recomputes business arithmetic, and it does so from an independent
reference rather than by calling `intercompany.py`.

---

## 6. E5 and E6 injection design

**Module:** `src/plant_downstream_errors.py`, run only as a CLI, never imported
by the pipeline.

**Flow:**

```
data/raw/intercompany_entries.csv            (clean, generated in Step 5)
        │  read-only
        ▼
src/plant_downstream_errors.py --error E5 --out data/error_data/intercompany_entries_E5.csv
                               --error E6 --out data/error_data/intercompany_entries_E6.csv
        │
        ▼
data/error_data/  corrupted copies + downstream_planted_errors.csv (manifest)
```

**Safety guard:** the script refuses to write to any path under `data/raw/` and
exits with an error. The clean artefact is only ever read.

**Deterministic target selection, without hard-coded ids.** Sort pairs by
`pair_id`, then select by rule:

- **E5:** the first pair whose payer and receiver have *different* currencies,
  and delete its `PAYABLE` row. A cross-currency pair is the harder case, since
  the deleted side is the one that cannot be inferred by eye.
- **E6:** the first cross-currency pair *other than* the E5 target, and blank one
  amount on it.

Recording the chosen `pair_id` in the manifest keeps the run reproducible while
the code stays free of literal ids.

**Which amount E6 should blank — a design recommendation.** Blanking
`eur_equivalent` is caught by the elimination check as `MISSING_AMOUNT`, which
you already have. Blanking **`local_amount`** is the stronger test: elimination
still passes, because it compares EUR equivalents, and only the completeness
control ICO-03 catches it. That proves ICO-03 is independent rather than
redundant. Recommendation: **E6 blanks `local_amount` on the payable side**, and
add an `E6b` variant blanking `eur_equivalent` if you want both paths covered.

**Manifest** (`data/error_data/downstream_planted_errors.csv`): `error_id`,
`source_file`, `output_file`, `pair_id`, `entry_id`, `field_changed`,
`original_value`, `new_value`, `expected_control`, `injected_at`. This is what
lets the test suite assert "the control that fired is the control we predicted",
rather than merely "something failed".

**Detection path:**

| Error | Artefact | Control that fires | Result |
|---|---|---|---|
| E5 | entries with one side deleted | ICO-01 (`MISSING_PAYABLE`) | FAIL, close blocked |
| E6 | entries with blanked `local_amount` | ICO-03 | FAIL, close blocked |
| E6b | entries with blanked `eur_equivalent` | ICO-03 and ICO-04 (`MISSING_AMOUNT`) | FAIL, close blocked |

---

## 7. Soda review

**`soda/checks.yml` is currently empty (0 bytes).** So nothing aligns, conflicts
or needs updating — the question is what belongs there at all.

**The division of labour to state explicitly in the README:**

- **Soda Core** — row and column level quality on each source dataset,
  independent of accounting logic: schema, nulls, duplicates, value domains,
  ranges, row counts. It runs on the *inputs*, before any processing.
- **`controls.py`** — everything requiring more than one file, or accounting
  meaning: three-way matching, elimination, FX re-performance, close decision.

Soda checks worth writing, per dataset:

- **invoices**: `missing_count(invoice_id) = 0`, `duplicate_count(invoice_id) = 0`
  (E2 at source), `missing_count(net_amount) = 0`, `missing_count(total_amount) = 0`,
  `invalid_count(currency)` against a valid-values list, `invalid_count(entity)`
  against the three entities, `min(net_amount) > 0`, row count > 0.
- **payments**: `duplicate_count(payment_id) = 0`, `missing_count(invoice_id) = 0`,
  `min(amount) > 0`.
- **purchase_orders**: `duplicate_count(po_id) = 0`, `missing_count(net_amount) = 0`.
- **shared_costs**: `missing_count` on amount, currency, paying_entity, month;
  `invalid_count(paying_entity)`; each share between 0 and 1. The shares-sum-to-1
  test crosses three columns, so keep it in Python unless you want a Soda
  user-defined check.
- **fx_rates**: `duplicate_count(month_end) = 0` (this is FXC-02 at source, and
  Soda does it well), `missing_count(eur_usd) = 0`, `min(eur_usd) > 0`.

**What Soda should not do here:** compare `fx_rates.csv` with
`fx_rates_expected.csv`, or test elimination. Cross-dataset accounting logic
belongs in `controls.py`, where the reasoning is visible and testable.

**One conflict to avoid:** do not add a Soda check asserting that
`invoices.csv` row count equals 60. The error dataset intentionally has 61, so
the check would fail for the wrong reason and train you to ignore it.

---

## 8. Test matrix

| Case | Input dataset | Control that detects it | Expected control status | Severity | Close status | Report produced |
|---|---|---|---|---|---|---|
| **Clean run** | `data/raw/*` + `fx_rates.csv` | all controls | all PASS (MAT-08/09 may warn) | — | PASS or PASS_WITH_WARNINGS | **Yes** |
| **E1** invoice 780 vs PO 800 | `error_data/invoices.csv` | MAT-04 | FAIL (1 `AMOUNT_MISMATCH`) | Critical | FAIL | No |
| **E2** duplicate invoice_id | `error_data/invoices.csv` | MAT-03, and MAT-01 on the id set; Soda `duplicate_count` at source | FAIL (2 rows) | Critical | FAIL | No |
| **E3** payment without invoice | `error_data/payments.csv` | MAT-06 | FAIL (1 orphan group) | Critical | FAIL | No |
| **E4** Feb rate 1.19 vs 1.09 | `error_data/fx_rates.csv` | FXC-03 (file) **and** FXC-05 (re-performance) | FAIL, Feb 2026, difference 0.10 | Critical | FAIL | No |
| **E4 control check** | same | ICO-04 elimination | **PASS** — by design | Critical | — | — |
| **E5** deleted side | `error_data/intercompany_entries_E5.csv` | ICO-01 | FAIL (`MISSING_PAYABLE`) | Critical | FAIL | No |
| **E6** blanked `local_amount` | `error_data/intercompany_entries_E6.csv` | ICO-03 | FAIL (1 null amount) | Critical | FAIL | No |
| **E6b** blanked `eur_equivalent` | `error_data/intercompany_entries_E6b.csv` | ICO-03, ICO-04 | FAIL (`MISSING_AMOUNT`) | Critical | FAIL | No |

The **E4 control check** row is the most valuable line in the matrix for an
interview. It documents a control that *correctly* passes while the data is
wrong, and names the control that covers the gap.

Each row becomes one pytest: run the pipeline on that dataset, assert the named
control failed, assert `close_status == FAIL`, and assert `report_allowed is
False`. Also assert that **no other control failed unexpectedly**, which catches
over-broad controls that fire on everything.

---

## 9. Output schema for the control-results table

One row per control:

| Column | Notes |
|---|---|
| `check_id` | e.g. `FXC-03` |
| `check_name` | short title |
| `check_group` | MAT / ICO / SRC / FXC / CVG |
| `control_family` | RECONCILIATION / SOURCE_ACCURACY / COMPLETENESS |
| `description` | what it proves, in one sentence |
| `severity` | CRITICAL / WARNING |
| `status` | PASS / FAIL / WARNING / ERROR |
| `expected_value` | e.g. `0`, `1.09` |
| `actual_value` | e.g. `1`, `1.19` |
| `difference` | numeric where meaningful, else null |
| `entity`, `month`, `pair_id`, `record_ref` | populated when the failure has a single locus |
| `failed_count` | number of offending rows |
| `sample_refs` | up to five identifiers, for investigation |
| `error_reference` | E1–E6 where the control targets a planted error |
| `explanation` | plain-language reason a reviewer can act on |
| `dataset_label`, `run_timestamp` | provenance |

**Where it is written, and by whom.** `run_controls()` returns this as a
DataFrame and `decide_close()` returns the decision object; neither touches
disk. The CLI or `pipeline.py` persists them to
`data/<dataset>/control_results.csv` and `data/<dataset>/close_decision.json`.
So the same engine can be called from a test with no side effects at all.

Keeping `control_family` on every row is what lets you show at a glance that the
E4 failure came from a source-accuracy control while every reconciliation
control passed.

---

## 10. Files to create or modify

**Create:**

| Path | Purpose |
|---|---|
| `src/controls.py` | control catalogue, `run_controls()`, `decide_close()` — pure, no file I/O |
| `src/controls_cli.py` (or the `__main__` block in `controls.py`) | argument parsing, loading inputs, writing `control_results.csv` and `close_decision.json` |
| `src/config.py` (or a `ControlConfig` dataclass in `controls.py`) | tolerances, FX bounds, entity map, period scope |
| `src/plant_downstream_errors.py` | E5/E6 injection CLI with the write guard |
| `tests/test_controls.py` | one test per test-matrix row |
| `tests/test_plant_downstream_errors.py` | injection determinism and the `data/raw` guard |
| `docs/CONTROLS.md` | the control catalogue as reviewer-facing documentation |
| `data/error_data/downstream_planted_errors.csv` | manifest, written by the injector |

**Populate:** `soda/checks.yml` (currently empty).

**Modify:** nothing. `match.py` and `intercompany.py` stay as they are, and no
dataset is touched.

**Step 7 will add:** `src/report.py`, gated on `close_decision.report_allowed`,
and `src/pipeline.py` if you want a single entry point.

---

## 11. Assumptions and open decisions

1. **`fx_rates_expected.csv` is a synthetic reference benchmark**, treated as the
   reference for this project only. In production the same control would compare
   against an independently sourced rate; only the loading of `fx_reference`
   changes, not the control. Say so in the README.
2. **FX tolerance of 1e-6 instead of 0.01** (§5). Confirm you accept the change.
3. **E6 blanks `local_amount`** rather than `eur_equivalent` (§6), so that the
   completeness control is proven independent of the elimination control.
4. **Unpaid and partially paid invoices are warnings.** If you would rather the
   close blocks on any open item, that is defensible for a synthetic project but
   would be unusual in practice — worth stating either way.
5. **Negative shared costs are invalid** because the specification defines no
   credit-note handling. If credit notes are in scope later, they need explicit
   rules rather than a relaxed control.
6. **Scope is Jan–Mar 2026 and three entities**, both taken from the spec. The
   controls read these from configuration, not from literals in the code.
6b. **Configurable parameters, all with documented defaults:** amount tolerance
   0.01, FX comparison tolerance 1e-6, FX plausibility band 0.5–2.0, markup
   0.05, entity-to-currency map, period scope. Only the amount tolerance and the
   markup come from the specification; the rest are project assumptions and are
   labelled as such in `docs/CONTROLS.md`.
7. **`data/raw/fx_rates.csv` now reads 1.09 for February**, which is correct. The
   earlier copy showed 1.19; if that file was ever used to generate output in
   `data/raw`, regenerate it before running the clean case.
8. **Control results are not yet versioned.** If you later change a threshold,
   an old result file will not say which version produced it — hence
   `controls_version` in the decision object.

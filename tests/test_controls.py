"""Tests for the Phase 1 control layer.

The planted-error tests run the real pipeline over the project datasets. Where a
dataset is absent the test skips rather than passing quietly, so a missing file
can never look like a green run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from controls import (  # noqa: E402
    CLOSE_FAIL,
    CRITICAL,
    ERROR,
    FAIL,
    PASS,
    WARNING,
    ControlConfig,
    decide_close,
    run_controls,
)
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return pd.read_csv(path, dtype=str)


def run_pipeline(
    invoices_path: Path,
    payments_path: Path,
    po_path: Path = RAW / "purchase_orders.csv",
    shared_costs_path: Path = RAW / "shared_costs.csv",
    fx_path: Path = RAW / "fx_rates.csv",
    fx_reference_path: Path = RAW / "fx_rates_expected.csv",
    ic_entries: pd.DataFrame | None = None,
    label: str = "test",
) -> tuple[pd.DataFrame, dict]:
    """Run Steps 4 and 5, then the controls, and return (results, decision)."""
    pos = _read(po_path)
    invoices = _read(invoices_path)
    payments = _read(payments_path)
    shared_costs = _read(shared_costs_path)
    fx_rates = _read(fx_path)
    fx_reference = _read(fx_reference_path)

    match_results = three_way_match(pos, invoices, payments)
    entries = (
        ic_entries
        if ic_entries is not None
        else generate_intercompany_entries(shared_costs, fx_rates)
    )
    elimination = check_elimination(entries)

    results = run_controls(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=shared_costs,
        fx_actual=fx_rates,
        fx_reference=fx_reference,
        purchase_orders=pos,
        dataset_label=label,
    )
    return results, decide_close(results, dataset_label=label)


def status_of(results: pd.DataFrame, check_id: str) -> str:
    row = results[results["check_id"] == check_id]
    assert len(row) == 1, f"expected exactly one result for {check_id}, got {len(row)}"
    return row.iloc[0]["status"]


def failed_critical_ids(results: pd.DataFrame) -> set[str]:
    mask = (results["severity"] == CRITICAL) & (results["status"].isin([FAIL, ERROR]))
    return set(results.loc[mask, "check_id"])


# ---------------------------------------------------------------------------
# clean run
# ---------------------------------------------------------------------------
def test_clean_run_passes_every_critical_control():
    results, decision = run_pipeline(
        RAW / "invoices.csv", RAW / "payments.csv", label="clean"
    )
    assert failed_critical_ids(results) == set()
    assert decision["close_status"] != CLOSE_FAIL
    assert decision["report_allowed"] is True
    assert decision["errors"] == 0


def test_every_control_returns_the_full_schema():
    results, _ = run_pipeline(RAW / "invoices.csv", RAW / "payments.csv", label="clean")
    from controls import RESULT_COLUMNS

    assert list(results.columns) == RESULT_COLUMNS
    assert results["status"].isin([PASS, FAIL, WARNING, ERROR]).all()
    assert results["severity"].isin([CRITICAL, "WARNING"]).all()
    assert not results["check_id"].duplicated().any()


def test_run_controls_does_not_mutate_its_inputs():
    pos = _read(RAW / "purchase_orders.csv")
    invoices = _read(RAW / "invoices.csv")
    payments = _read(RAW / "payments.csv")
    shared_costs = _read(RAW / "shared_costs.csv")
    fx_rates = _read(RAW / "fx_rates.csv")
    fx_reference = _read(RAW / "fx_rates_expected.csv")

    match_results = three_way_match(pos, invoices, payments)
    entries = generate_intercompany_entries(shared_costs, fx_rates)
    elimination = check_elimination(entries)
    before = {
        "invoices": invoices.copy(),
        "payments": payments.copy(),
        "match": match_results.copy(),
        "entries": entries.copy(),
        "elimination": elimination.copy(),
    }

    run_controls(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=shared_costs,
        fx_actual=fx_rates,
        fx_reference=fx_reference,
    )

    pd.testing.assert_frame_equal(invoices, before["invoices"])
    pd.testing.assert_frame_equal(payments, before["payments"])
    pd.testing.assert_frame_equal(match_results, before["match"])
    pd.testing.assert_frame_equal(entries, before["entries"])
    pd.testing.assert_frame_equal(elimination, before["elimination"])


# ---------------------------------------------------------------------------
# planted source errors E1, E2, E3
# ---------------------------------------------------------------------------
def test_e1_amount_mismatch_blocks_the_close():
    results, decision = run_pipeline(
        ERR / "invoices.csv", RAW / "payments.csv", label="E1"
    )
    assert status_of(results, "MAT-04") == FAIL
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False
    assert "MAT-04" in decision["blocking_controls"]


def test_e2_duplicate_invoice_blocks_the_close_without_a_false_payment_failure():
    results, decision = run_pipeline(
        ERR / "invoices.csv", RAW / "payments.csv", label="E2"
    )
    assert status_of(results, "MAT-03") == FAIL
    # The heart of the MAT-02 fix: duplicate invoice rows repeat the same
    # payment ids, which must not be read as a payment-population failure.
    assert status_of(results, "MAT-02") == PASS
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False


def test_e3_orphan_payment_blocks_the_close():
    results, decision = run_pipeline(
        RAW / "invoices.csv", ERR / "payments.csv", label="E3"
    )
    assert status_of(results, "MAT-06") == FAIL
    # The orphan payment is represented on the PAYMENT_ONLY row, so the
    # population control still passes: the payment was not lost, it is unmatched.
    assert status_of(results, "MAT-02") == PASS
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False


def test_planted_source_errors_do_not_trip_unrelated_controls():
    results, _ = run_pipeline(ERR / "invoices.csv", ERR / "payments.csv", label="E1-E3")
    failures = failed_critical_ids(results)
    assert failures <= {"MAT-03", "MAT-04", "MAT-06"}, (
        f"unexpected critical failures: {sorted(failures - {'MAT-03', 'MAT-04', 'MAT-06'})}"
    )


# ---------------------------------------------------------------------------
# in-memory fixtures for the controls that the project data cannot exercise
# ---------------------------------------------------------------------------
@pytest.fixture
def tiny_entries() -> pd.DataFrame:
    """One balanced cross-currency pair, in the schema intercompany.py produces."""
    base = {
        "cost_id": "C1",
        "month": "2026-01-31",
        "paying_entity": "BE01",
        "receiving_entity": "US01",
        "source_amount": 1000.0,
        "source_currency": "EUR",
        "share_pct": 0.5,
        "allocated_cost": 500.0,
        "markup_pct": 0.05,
        "fx_rate_eur_usd": 1.08,
        "fx_rate_required": True,
        "description": "Test cost",
        "status": "VALID",
        "status_reason": "",
        "data_quality_flag": "",
    }
    return pd.DataFrame(
        [
            {**base, "entry_id": "C1-BE01-US01-R", "pair_id": "C1-BE01-US01",
             "entity": "BE01", "counterparty_entity": "US01", "entry_type": "RECEIVABLE",
             "currency": "EUR", "local_amount": 525.0, "eur_equivalent": 525.0},
            {**base, "entry_id": "C1-BE01-US01-P", "pair_id": "C1-BE01-US01",
             "entity": "US01", "counterparty_entity": "BE01", "entry_type": "PAYABLE",
             "currency": "USD", "local_amount": 567.0, "eur_equivalent": 525.0},
        ]
    )


@pytest.fixture
def tiny_costs() -> pd.DataFrame:
    return pd.DataFrame(
        [{"cost_id": "C1", "paying_entity": "BE01", "description": "Test cost",
          "month": "2026-01-31", "currency": "EUR", "amount": "1000.0",
          "share_BE01": "0.5", "share_US01": "0.5", "share_BE02": "0.0"}]
    )


@pytest.fixture
def tiny_sources() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pos = pd.DataFrame([{"po_id": "PO1", "entity": "BE01", "supplier": "S",
                         "currency": "EUR", "net_amount": "100.00", "po_date": "2026-01-05"}])
    invoices = pd.DataFrame([{"invoice_id": "I1", "po_id": "PO1", "entity": "BE01",
                              "supplier": "S", "invoice_date": "2026-01-10",
                              "currency": "EUR", "net_amount": "100.00",
                              "vat_amount": "21.00", "total_amount": "121.00"}])
    payments = pd.DataFrame([{"payment_id": "P1", "invoice_id": "I1", "entity": "BE01",
                              "payment_date": "2026-01-20", "currency": "EUR",
                              "amount": "121.00"}])
    return pos, invoices, payments


TINY_FX = pd.DataFrame([{"month_end": "2026-01-31", "eur_usd": "1.08"}])


def _run_tiny(tiny_sources, entries, costs, config=None, fx=None, fx_reference=None):
    pos, invoices, payments = tiny_sources
    match_results = three_way_match(pos, invoices, payments)
    elimination = check_elimination(entries)
    config = config or ControlConfig(period_months=("2026-01",))
    results = run_controls(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=costs,
        fx_actual=fx if fx is not None else TINY_FX,
        fx_reference=fx_reference if fx_reference is not None else TINY_FX,
        config=config,
    )
    return results, decide_close(results, config=config)


def test_ico_controls_pass_on_a_balanced_pair(tiny_sources, tiny_entries, tiny_costs):
    results, decision = _run_tiny(tiny_sources, tiny_entries, tiny_costs)
    for check_id in ["ICO-01", "ICO-02", "ICO-03", "ICO-04", "ICO-05",
                     "ICO-06", "ICO-07", "ICO-08", "ICO-09", "ICO-10"]:
        assert status_of(results, check_id) == PASS, check_id
    assert decision["report_allowed"] is True


def test_ico_08_detects_a_currency_booked_against_the_wrong_entity(
    tiny_sources, tiny_entries, tiny_costs
):
    broken = tiny_entries.copy()
    broken.loc[broken["entity"] == "US01", "currency"] = "EUR"
    results, decision = _run_tiny(tiny_sources, broken, tiny_costs)
    assert status_of(results, "ICO-08") == FAIL
    assert decision["report_allowed"] is False


def test_ico_04_detects_a_pair_that_does_not_eliminate(
    tiny_sources, tiny_entries, tiny_costs
):
    broken = tiny_entries.copy()
    broken.loc[broken["entry_type"] == "PAYABLE", "eur_equivalent"] = 500.0
    results, decision = _run_tiny(tiny_sources, broken, tiny_costs)
    assert status_of(results, "ICO-04") == FAIL
    assert status_of(results, "ICO-05") == FAIL
    assert decision["close_status"] == CLOSE_FAIL


def test_src_02_detects_a_rejected_shared_cost(tiny_sources, tiny_costs):
    bad_costs = tiny_costs.copy()
    bad_costs.loc[0, "share_US01"] = "0.9"  # shares now sum to 1.4
    entries = generate_intercompany_entries(
        bad_costs, pd.DataFrame([{"month_end": "2026-01-31", "eur_usd": "1.08"}])
    )
    results, decision = _run_tiny(tiny_sources, entries, bad_costs)
    assert status_of(results, "SRC-02") == FAIL
    assert decision["report_allowed"] is False


def test_src_05_rejects_a_negative_shared_cost(tiny_sources, tiny_entries, tiny_costs):
    negative = tiny_costs.copy()
    negative.loc[0, "amount"] = "-1000.0"
    results, _ = _run_tiny(tiny_sources, tiny_entries, negative)
    assert status_of(results, "SRC-05") == FAIL


def test_cvg_01_detects_a_month_outside_the_close_period(
    tiny_sources, tiny_entries, tiny_costs
):
    pos, invoices, payments = tiny_sources
    out_of_scope = invoices.copy()
    out_of_scope.loc[0, "invoice_date"] = "2025-12-10"
    results, _ = _run_tiny((pos, out_of_scope, payments), tiny_entries, tiny_costs)
    assert status_of(results, "CVG-01") == FAIL


def test_cvg_03_detects_an_unknown_entity(tiny_sources, tiny_entries, tiny_costs):
    pos, invoices, payments = tiny_sources
    unknown = invoices.copy()
    unknown.loc[0, "entity"] = "ZZ99"
    results, _ = _run_tiny((pos, unknown, payments), tiny_entries, tiny_costs)
    assert status_of(results, "CVG-03") == FAIL


# ---------------------------------------------------------------------------
# close decision logic
# ---------------------------------------------------------------------------
def _decision_frame(rows: list[dict]) -> pd.DataFrame:
    from controls import RESULT_COLUMNS

    frame = pd.DataFrame(rows)
    for column in RESULT_COLUMNS:
        if column not in frame:
            frame[column] = None
    return frame[RESULT_COLUMNS]


def test_close_decision_precedence():
    all_pass = _decision_frame([
        {"check_id": "A", "severity": CRITICAL, "status": PASS},
        {"check_id": "B", "severity": "WARNING", "status": PASS},
    ])
    assert decide_close(all_pass)["close_status"] == "PASS"

    warned = _decision_frame([
        {"check_id": "A", "severity": CRITICAL, "status": PASS},
        {"check_id": "B", "severity": "WARNING", "status": WARNING},
    ])
    decision = decide_close(warned)
    assert decision["close_status"] == "PASS_WITH_WARNINGS"
    assert decision["report_allowed"] is True

    failed = _decision_frame([
        {"check_id": "A", "severity": CRITICAL, "status": FAIL},
        {"check_id": "B", "severity": "WARNING", "status": WARNING},
    ])
    decision = decide_close(failed)
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False
    assert decision["blocking_controls"] == ["A"]


def test_an_errored_control_blocks_the_close_even_when_nothing_else_fails():
    errored = _decision_frame([
        {"check_id": "A", "severity": CRITICAL, "status": PASS},
        {"check_id": "B", "severity": "WARNING", "status": ERROR},
    ])
    decision = decide_close(errored)
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False
    assert "B" in decision["blocking_controls"]


def test_a_control_group_that_raises_becomes_an_error_row(tiny_sources, tiny_costs):
    pos, invoices, payments = tiny_sources
    match_results = three_way_match(pos, invoices, payments)
    # An entries frame missing required columns makes the ICO group raise.
    broken_entries = pd.DataFrame([{"entry_type": "RECEIVABLE"}])
    results = run_controls(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=broken_entries,
        ic_elimination=pd.DataFrame(),
        shared_costs=tiny_costs,
        fx_actual=TINY_FX,
        fx_reference=TINY_FX,
        config=ControlConfig(period_months=("2026-01",)),
    )
    assert ERROR in set(results["status"])
    assert decide_close(results)["close_status"] == CLOSE_FAIL


def test_config_is_recorded_on_the_decision():
    config = ControlConfig(amount_tolerance=0.05)
    decision = decide_close(_decision_frame([]), config=config)
    assert decision["config_used"]["amount_tolerance"] == 0.05
    assert decision["controls_version"]

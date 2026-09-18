"""Tests for the FXC group: independent FX source accuracy and re-performance.

The centrepiece is ``test_e4_is_invisible_to_every_reconciliation_control``: it
asserts that under E4 every intercompany control still passes and only the FX
controls fail. That is the property the whole control architecture rests on.
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
    CLOSE_PASS,
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

FXC_IDS = ["FXC-01", "FXC-02", "FXC-03", "FXC-04", "FXC-05", "FXC-06"]
ICO_IDS = [f"ICO-{n:02d}" for n in range(1, 11)]


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return pd.read_csv(path, dtype=str)


def run_with_fx(
    fx_path: Path,
    reference_path: Path = RAW / "fx_rates_expected.csv",
    entries_override: pd.DataFrame | None = None,
    label: str = "test",
):
    """Full pipeline with an explicit FX file; data/raw is never written to."""
    pos = _read(RAW / "purchase_orders.csv")
    invoices = _read(RAW / "invoices.csv")
    payments = _read(RAW / "payments.csv")
    shared_costs = _read(RAW / "shared_costs.csv")
    fx_actual = _read(fx_path)
    fx_reference = _read(reference_path)

    entries = (
        entries_override
        if entries_override is not None
        else generate_intercompany_entries(shared_costs, fx_actual)
    )
    results = run_controls(
        match_results=three_way_match(pos, invoices, payments),
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=check_elimination(entries),
        shared_costs=shared_costs,
        fx_actual=fx_actual,
        fx_reference=fx_reference,
        purchase_orders=pos,
        dataset_label=label,
    )
    return results, decide_close(results, dataset_label=label), entries


def status_of(results: pd.DataFrame, check_id: str) -> str:
    row = results[results["check_id"] == check_id]
    assert len(row) == 1, f"expected one result for {check_id}, got {len(row)}"
    return row.iloc[0]["status"]


def row_of(results: pd.DataFrame, check_id: str) -> pd.Series:
    return results[results["check_id"] == check_id].iloc[0]


def failed_critical(results: pd.DataFrame) -> set[str]:
    mask = (results["severity"] == CRITICAL) & (results["status"].isin([FAIL, ERROR]))
    return set(results.loc[mask, "check_id"])


# ---------------------------------------------------------------------------
# clean data
# ---------------------------------------------------------------------------
def test_clean_data_passes_every_fx_control():
    results, decision, _ = run_with_fx(RAW / "fx_rates.csv", label="clean")
    for check_id in FXC_IDS:
        assert status_of(results, check_id) == PASS, check_id
    assert decision["close_status"] == CLOSE_PASS
    assert decision["report_allowed"] is True


def test_fx_controls_are_present_and_well_formed():
    results, _, _ = run_with_fx(RAW / "fx_rates.csv", label="clean")
    fxc = results[results["check_group"] == "FXC"]
    assert sorted(fxc["check_id"]) == FXC_IDS
    assert set(fxc.loc[fxc["check_id"].isin(["FXC-03", "FXC-05"]), "control_family"]) == {
        "SOURCE_ACCURACY"
    }
    assert row_of(results, "FXC-04")["severity"] == "WARNING"


# ---------------------------------------------------------------------------
# E4
# ---------------------------------------------------------------------------
def test_e4_fails_fxc_03_with_the_february_rate():
    results, decision, _ = run_with_fx(ERR / "fx_rates.csv", label="E4")
    row = row_of(results, "FXC-03")
    assert row["status"] == FAIL
    assert row["month"] == "2026-02"
    assert float(row["expected_value"]) == pytest.approx(1.09)
    assert float(row["actual_value"]) == pytest.approx(1.19)
    assert float(row["difference"]) == pytest.approx(0.10, abs=1e-6)
    assert row["error_reference"] == "E4"
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False


def test_e4_fails_fxc_05_on_six_entries():
    results, _, _ = run_with_fx(ERR / "fx_rates.csv", label="E4")
    row = row_of(results, "FXC-05")
    assert row["status"] == FAIL
    # Four entries fail on the EUR equivalent (both recharges of the USD-sourced
    # February cost, two sides each) and two on the local amount (the US01
    # payable of each EUR-sourced February cost).
    assert int(row["failed_count"]) == 6


def test_e4_is_invisible_to_every_reconciliation_control():
    """The property the architecture rests on.

    Under E4 both sides of each recharge use the same wrong rate, so every
    intercompany control passes. Only the source-accuracy controls can see it.
    """
    results, decision, entries = run_with_fx(ERR / "fx_rates.csv", label="E4")

    for check_id in ICO_IDS:
        assert status_of(results, check_id) == PASS, f"{check_id} should still pass under E4"

    assert failed_critical(results) == {"FXC-03", "FXC-05"}
    assert decision["blocking_controls"] == ["FXC-03", "FXC-05"]

    # and the elimination itself really does balance on the wrong rates
    elimination = check_elimination(entries)
    assert (elimination["check_status"] == "OK").all()


def test_e4_does_not_touch_the_clean_dataset():
    before = (RAW / "fx_rates.csv").read_bytes()
    run_with_fx(ERR / "fx_rates.csv", label="E4")
    assert (RAW / "fx_rates.csv").read_bytes() == before


# ---------------------------------------------------------------------------
# the tampering case: why FXC-05 exists alongside FXC-03
# ---------------------------------------------------------------------------
def test_an_edited_entry_passes_fxc_03_but_fails_fxc_05():
    """A clean FX file cannot prove the booked numbers are right.

    Here the rate file is correct, so FXC-03 passes, but one entry's EUR
    equivalent has been edited by hand. Only the re-performance control notices.
    """
    shared_costs = _read(RAW / "shared_costs.csv")
    fx_actual = _read(RAW / "fx_rates.csv")
    entries = generate_intercompany_entries(shared_costs, fx_actual)

    tampered = entries.copy()
    target = tampered.index[0]
    tampered.loc[target, "eur_equivalent"] = float(tampered.loc[target, "eur_equivalent"]) + 250.0

    results, decision, _ = run_with_fx(
        RAW / "fx_rates.csv", entries_override=tampered, label="tampered"
    )
    assert status_of(results, "FXC-03") == PASS
    assert status_of(results, "FXC-05") == FAIL
    assert int(row_of(results, "FXC-05")["failed_count"]) == 1
    assert decision["report_allowed"] is False


def test_fxc_05_ignores_the_rate_recorded_on_the_entry():
    """Rewriting fx_rate_eur_usd must not change the verdict.

    The control recomputes from source_amount, share and markup at the reference
    rate; the rate stamped on the entry is the thing under test, not evidence.
    """
    shared_costs = _read(RAW / "shared_costs.csv")
    entries = generate_intercompany_entries(shared_costs, _read(ERR / "fx_rates.csv"))
    disguised = entries.copy()
    disguised["fx_rate_eur_usd"] = 1.09  # make the wrong entries look right

    results, _, _ = run_with_fx(
        ERR / "fx_rates.csv", entries_override=disguised, label="E4-disguised"
    )
    assert status_of(results, "FXC-05") == FAIL
    assert int(row_of(results, "FXC-05")["failed_count"]) == 6


# ---------------------------------------------------------------------------
# in-memory fixtures for conditions the project data cannot produce
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_context():
    """(entries, reference) for a clean run, reused by the fixture-based tests."""
    shared_costs = _read(RAW / "shared_costs.csv")
    reference = _read(RAW / "fx_rates_expected.csv")
    entries = generate_intercompany_entries(shared_costs, reference)
    return entries, reference


def _fx_only(fx_actual: pd.DataFrame, clean_context, config: ControlConfig | None = None):
    """Run the whole engine but vary only the FX file under test."""
    entries, reference = clean_context
    pos = _read(RAW / "purchase_orders.csv")
    invoices = _read(RAW / "invoices.csv")
    payments = _read(RAW / "payments.csv")
    shared_costs = _read(RAW / "shared_costs.csv")
    results = run_controls(
        match_results=three_way_match(pos, invoices, payments),
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=check_elimination(entries),
        shared_costs=shared_costs,
        fx_actual=fx_actual,
        fx_reference=reference,
        config=config or ControlConfig(),
    )
    return results, decide_close(results)


def test_fxc_01_detects_a_missing_month(clean_context):
    _, reference = clean_context
    incomplete = reference[reference["month_end"] != "2026-02-28"]
    results, decision = _fx_only(incomplete, clean_context)
    assert status_of(results, "FXC-01") == FAIL
    assert decision["report_allowed"] is False


def test_fxc_02_detects_a_duplicate_month(clean_context):
    _, reference = clean_context
    duplicated = pd.concat([reference, reference.iloc[[1]]], ignore_index=True)
    results, decision = _fx_only(duplicated, clean_context)
    assert status_of(results, "FXC-02") == FAIL
    assert decision["close_status"] == CLOSE_FAIL


def test_fxc_04_warns_about_an_extra_month_without_blocking(clean_context):
    _, reference = clean_context
    extra = pd.concat(
        [reference, pd.DataFrame([{"month_end": "2026-04-30", "eur_usd": "1.06"}])],
        ignore_index=True,
    )
    results, decision = _fx_only(extra, clean_context)
    assert status_of(results, "FXC-04") == WARNING
    assert status_of(results, "FXC-03") == PASS
    assert decision["close_status"] == "PASS_WITH_WARNINGS"
    assert decision["report_allowed"] is True


def test_fxc_06_detects_a_decimal_point_error(clean_context):
    _, reference = clean_context
    implausible = reference.copy()
    implausible.loc[implausible["month_end"] == "2026-01-31", "eur_usd"] = "10.8"
    results, decision = _fx_only(implausible, clean_context)
    assert status_of(results, "FXC-06") == FAIL
    assert decision["report_allowed"] is False


# ---------------------------------------------------------------------------
# tolerance boundary: why the FX tolerance is not 0.01
# ---------------------------------------------------------------------------
def test_fx_tolerance_accepts_floating_point_noise(clean_context):
    _, reference = clean_context
    noisy = reference.copy()
    noisy["eur_usd"] = noisy["eur_usd"].astype(float) + 1e-9
    results, _ = _fx_only(noisy, clean_context)
    assert status_of(results, "FXC-03") == PASS


def test_fx_tolerance_rejects_a_small_but_real_rate_error(clean_context):
    """1e-5 on a rate would pass a 0.01 tolerance; here it must fail."""
    _, reference = clean_context
    drifted = reference.copy()
    drifted.loc[drifted["month_end"] == "2026-02-28", "eur_usd"] = "1.09001"
    results, decision = _fx_only(drifted, clean_context)
    assert status_of(results, "FXC-03") == FAIL
    assert decision["report_allowed"] is False


def test_fx_tolerance_is_configurable(clean_context):
    _, reference = clean_context
    drifted = reference.copy()
    drifted.loc[drifted["month_end"] == "2026-02-28", "eur_usd"] = "1.09001"
    relaxed = ControlConfig(fx_comparison_tolerance=0.001)
    results, _ = _fx_only(drifted, clean_context, config=relaxed)
    assert status_of(results, "FXC-03") == PASS

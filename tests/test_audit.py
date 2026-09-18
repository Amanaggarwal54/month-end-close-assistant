"""Tests for the audit-trail and exception-record module.

The module is pure, so every test asserts against returned objects. Where a
planted error is involved the real pipeline produces the control results, so the
assertions are about what the controls actually recorded rather than about a
fixture written to agree with them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit import (  # noqa: E402
    AUDIT_VERSION,
    RATE_COMPARISON_CONTROLS,
    build_audit_trail,
    build_exception_records,
)
from controls import decide_close, run_controls  # noqa: E402
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402
from plant_downstream_errors import inject  # noqa: E402

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return pd.read_csv(path, dtype=str)


def run_close_for(
    invoices_path: Path = RAW / "invoices.csv",
    payments_path: Path = RAW / "payments.csv",
    po_path: Path = RAW / "purchase_orders.csv",
    fx_path: Path = RAW / "fx_rates.csv",
    entries_override: pd.DataFrame | None = None,
    label: str = "test",
) -> tuple[pd.DataFrame, dict]:
    """Produce real control results and a real close decision, without file output."""
    pos = _read(po_path)
    invoices = _read(invoices_path)
    payments = _read(payments_path)
    shared_costs = _read(RAW / "shared_costs.csv")
    fx_actual = _read(fx_path)
    fx_reference = _read(RAW / "fx_rates_expected.csv")

    match_results = three_way_match(pos, invoices, payments)
    entries = (
        entries_override
        if entries_override is not None
        else generate_intercompany_entries(shared_costs, fx_actual)
    )
    elimination = check_elimination(entries)
    results = run_controls(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=shared_costs,
        fx_actual=fx_actual,
        fx_reference=fx_reference,
        purchase_orders=pos,
        dataset_label=label,
        run_timestamp=FIXED_TIMESTAMP,
    )
    decision = decide_close(results, dataset_label=label, run_timestamp=FIXED_TIMESTAMP)
    return results, decision


def ids_of(records: list[dict]) -> list[str]:
    return [record["check_id"] for record in records]


# ---------------------------------------------------------------------------
# clean data
# ---------------------------------------------------------------------------
def test_clean_controls_produce_no_exceptions():
    results, decision = run_close_for(label="clean")
    assert build_exception_records(results) == []

    trail = build_audit_trail(results, decision)
    assert trail["close_status"] == "PASS"
    assert trail["report_allowed"] is True
    assert trail["exception_count"] == 0
    assert trail["exceptions"] == []
    assert trail["blocking_controls"] == []


def test_an_empty_control_table_produces_an_empty_trail():
    """A caller with no results must get zeros, not a crash or a missing key."""
    empty = pd.DataFrame(columns=["check_id", "severity", "status"])
    assert build_exception_records(empty) == []

    trail = build_audit_trail(empty, {"close_status": "PASS", "report_allowed": True})
    assert trail["control_count"] == 0
    assert trail["status_counts"] == {}
    assert trail["critical_failed_count"] == 0
    assert trail["warning_count"] == 0
    assert trail["error_count"] == 0


# ---------------------------------------------------------------------------
# E1-E3: matching exceptions
# ---------------------------------------------------------------------------
def test_e1_to_e3_produce_the_expected_matching_exceptions():
    results, decision = run_close_for(
        invoices_path=ERR / "invoices.csv", payments_path=ERR / "payments.csv", label="E1-E3"
    )
    records = build_exception_records(results)

    assert ids_of(records) == ["MAT-03", "MAT-04", "MAT-06"]
    for record in records:
        assert record["check_group"] == "MAT"
        assert record["control_family"] == "RECONCILIATION"
        assert record["severity"] == "CRITICAL"
        assert record["status"] == "FAIL"
        assert record["message"]

    by_id = {record["check_id"]: record for record in records}
    assert by_id["MAT-03"]["error_reference"] == "E2"
    assert by_id["MAT-04"]["error_reference"] == "E1"
    assert by_id["MAT-06"]["error_reference"] == "E3"

    trail = build_audit_trail(results, decision)
    assert trail["close_status"] == "FAIL"
    assert trail["report_allowed"] is False
    assert trail["blocking_controls"] == ["MAT-03", "MAT-04", "MAT-06"]
    assert trail["exception_count"] == 3


def test_matching_exceptions_carry_the_identifiers_the_control_sampled():
    results, _ = run_close_for(
        invoices_path=ERR / "invoices.csv", payments_path=ERR / "payments.csv", label="E1-E3"
    )
    by_id = {record["check_id"]: record for record in build_exception_records(results)}

    # E1: the mismatching invoice. E2: the duplicated invoice, sampled twice.
    assert by_id["MAT-04"]["details"]["references"] == ["INV5001"]
    assert by_id["MAT-03"]["details"]["references"] == ["INV5002", "INV5002"]
    # E3: the orphan payment, which is a payment id rather than an invoice id.
    assert by_id["MAT-06"]["details"]["references"] == ["PAY9061"]


def test_matching_details_do_not_invent_amounts_or_label_the_identifier_kind():
    """MAT-04's references are invoice ids and MAT-06's are payment ids.

    A control result does not record which kind it sampled, so the records must
    not claim to know. Nor do the controls record ledger amounts, so no amount
    may appear on a record that was built only from control results.
    """
    results, _ = run_close_for(
        invoices_path=ERR / "invoices.csv", payments_path=ERR / "payments.csv", label="E1-E3"
    )
    for record in build_exception_records(results):
        details = record["details"]
        assert "invoice_id" not in details
        assert "payment_id" not in details
        assert "po_id" not in details
        assert "invoice_amount" not in details
        assert "po_amount" not in details


# ---------------------------------------------------------------------------
# E4: FX exceptions
# ---------------------------------------------------------------------------
def test_e4_produces_exactly_the_two_fx_exceptions():
    results, decision = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    records = build_exception_records(results)

    assert ids_of(records) == ["FXC-03", "FXC-05"]
    for record in records:
        assert record["check_group"] == "FXC"
        assert record["control_family"] == "SOURCE_ACCURACY"
        assert record["error_reference"] == "E4"

    trail = build_audit_trail(results, decision)
    assert trail["blocking_controls"] == ["FXC-03", "FXC-05"]
    assert trail["critical_failed_count"] == 2
    assert trail["report_allowed"] is False


def test_e4_rate_comparison_carries_the_month_and_both_rates():
    results, _ = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    details = {r["check_id"]: r["details"] for r in build_exception_records(results)}["FXC-03"]

    assert details["month"] == "2026-02"
    assert details["reference_rate"] == pytest.approx(1.09)
    assert details["supplied_rate"] == pytest.approx(1.19)
    assert details["rate_difference"] == pytest.approx(0.10, abs=1e-6)


def test_a_counting_fx_control_is_never_given_a_fabricated_rate():
    """FXC-05's expected/actual are counts of entries, not rates.

    Renaming them to reference_rate/supplied_rate would record a reference rate
    of 0.00 against the close, which is worse than recording nothing.
    """
    results, _ = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    details = {r["check_id"]: r["details"] for r in build_exception_records(results)}["FXC-05"]

    assert "FXC-05" not in RATE_COMPARISON_CONTROLS
    assert "reference_rate" not in details
    assert "supplied_rate" not in details
    assert details["failed_count"] == 6
    assert len(details["references"]) >= 1


# ---------------------------------------------------------------------------
# E5 / E6: intercompany exceptions
# ---------------------------------------------------------------------------
def _planted_entries(error_id: str, tmp_path: Path) -> pd.DataFrame:
    clean = generate_intercompany_entries(
        _read(RAW / "shared_costs.csv"), _read(RAW / "fx_rates.csv")
    )
    source = tmp_path / "intercompany_entries.csv"
    clean.to_csv(source, index=False)
    output = tmp_path / f"entries_{error_id}.csv"
    inject(error_id, source, output)
    return pd.read_csv(output)


def test_e5_exception_names_the_one_sided_pair(tmp_path: Path):
    results, decision = run_close_for(
        entries_override=_planted_entries("E5", tmp_path), label="E5"
    )
    records = build_exception_records(results)
    by_id = {record["check_id"]: record for record in records}

    assert "ICO-01" in by_id
    assert by_id["ICO-01"]["check_group"] == "ICO"
    assert by_id["ICO-01"]["details"]["references"], "the pair id should be carried through"
    assert build_audit_trail(results, decision)["report_allowed"] is False


def test_e6_exception_names_the_entry_with_the_blanked_amount(tmp_path: Path):
    results, decision = run_close_for(
        entries_override=_planted_entries("E6", tmp_path), label="E6"
    )
    by_id = {r["check_id"]: r for r in build_exception_records(results)}

    assert "ICO-03" in by_id
    assert by_id["ICO-03"]["control_family"] == "COMPLETENESS"
    assert by_id["ICO-03"]["details"]["references"]
    assert build_audit_trail(results, decision)["report_allowed"] is False


def test_an_elimination_difference_is_labelled_as_eur(tmp_path: Path):
    """ICO differences are EUR amounts, which the record says explicitly."""
    results, _ = run_close_for(entries_override=_planted_entries("E5", tmp_path), label="E5")
    by_id = {r["check_id"]: r for r in build_exception_records(results)}
    if "ICO-05" in by_id:
        details = by_id["ICO-05"]["details"]
        assert details["difference_eur"] == details["difference"]


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------
def _row(check_id: str, severity: str, status: str, **extra) -> dict:
    row = {
        "check_id": check_id,
        "check_name": f"name for {check_id}",
        "check_group": check_id.split("-")[0],
        "control_family": "RECONCILIATION",
        "description": "",
        "severity": severity,
        "status": status,
        "expected_value": None,
        "actual_value": None,
        "difference": None,
        "entity": None,
        "month": None,
        "pair_id": None,
        "record_ref": None,
        "failed_count": 0,
        "sample_refs": "",
        "error_reference": "",
        "explanation": f"finding for {check_id}",
        "dataset_label": "fixture",
        "run_timestamp": FIXED_TIMESTAMP,
    }
    row.update(extra)
    return row


def test_critical_exceptions_sort_before_warnings_then_by_check_id():
    frame = pd.DataFrame([
        _row("ZZZ-01", "WARNING", "WARNING"),
        _row("MAT-09", "CRITICAL", "FAIL"),
        _row("AAA-02", "WARNING", "FAIL"),
        _row("ICO-01", "CRITICAL", "ERROR"),
    ])
    records = build_exception_records(frame, statuses=("FAIL", "ERROR", "WARNING"))
    assert ids_of(records) == ["ICO-01", "MAT-09", "AAA-02", "ZZZ-01"]


def test_rows_of_the_same_control_are_ordered_by_a_stable_identifier():
    frame = pd.DataFrame([
        _row("ICO-01", "CRITICAL", "FAIL", pair_id="COST009-BE01-US01"),
        _row("ICO-01", "CRITICAL", "FAIL", pair_id="COST001-BE02-US01"),
        _row("ICO-01", "CRITICAL", "FAIL", pair_id="COST005-US01-BE01"),
    ])
    pairs = [r["details"]["pair_id"] for r in build_exception_records(frame)]
    assert pairs == sorted(pairs)


def test_ordering_does_not_depend_on_the_order_of_the_input_rows():
    rows = [
        _row("FXC-03", "CRITICAL", "FAIL", month="2026-02"),
        _row("MAT-03", "CRITICAL", "FAIL", sample_refs="INV5002"),
        _row("ICO-01", "CRITICAL", "FAIL", sample_refs="COST001-BE02-US01"),
    ]
    forward = build_exception_records(pd.DataFrame(rows))
    reversed_input = build_exception_records(pd.DataFrame(list(reversed(rows))))
    assert forward == reversed_input
    assert ids_of(forward) == ["FXC-03", "ICO-01", "MAT-03"]


def test_an_unknown_severity_is_reported_rather_than_dropped():
    frame = pd.DataFrame([
        _row("AAA-01", "INFORMATIONAL", "FAIL"),
        _row("BBB-01", "CRITICAL", "FAIL"),
    ])
    records = build_exception_records(frame)
    assert ids_of(records) == ["BBB-01", "AAA-01"]


# ---------------------------------------------------------------------------
# audit summary counts
# ---------------------------------------------------------------------------
def test_status_counts_are_read_from_the_control_results():
    frame = pd.DataFrame([
        _row("A-01", "CRITICAL", "PASS"),
        _row("A-02", "CRITICAL", "PASS"),
        _row("A-03", "CRITICAL", "FAIL"),
        _row("A-04", "WARNING", "WARNING"),
        _row("A-05", "WARNING", "PASS"),
        _row("A-06", "CRITICAL", "ERROR"),
    ])
    decision = {
        "close_status": "FAIL",
        "report_allowed": False,
        "blocking_controls": ["A-03", "A-06"],
        "dataset_label": "fixture",
        "run_timestamp": FIXED_TIMESTAMP,
        "controls_version": "2.0.0",
    }
    trail = build_audit_trail(frame, decision)

    assert trail["control_count"] == 6
    assert trail["status_counts"] == {"ERROR": 1, "FAIL": 1, "PASS": 3, "WARNING": 1}
    assert trail["critical_failed_count"] == 1
    assert trail["warning_count"] == 1
    assert trail["error_count"] == 1
    assert trail["exception_count"] == 2  # FAIL + ERROR
    assert trail["blocking_controls"] == ["A-03", "A-06"]
    assert trail["audit_version"] == AUDIT_VERSION
    assert trail["controls_version"] == "2.0.0"


def test_counts_agree_with_the_close_decision_on_real_results():
    """The audit counts and decide_close's own counts must not disagree."""
    for kwargs in (
        {"label": "clean"},
        {"invoices_path": ERR / "invoices.csv", "payments_path": ERR / "payments.csv",
         "label": "E1-E3"},
        {"fx_path": ERR / "fx_rates.csv", "label": "E4"},
    ):
        results, decision = run_close_for(**kwargs)
        trail = build_audit_trail(results, decision)
        assert trail["critical_failed_count"] == decision["critical_failed"]
        assert trail["warning_count"] == decision["warnings"]
        assert trail["error_count"] == decision["errors"]


def test_prebuilt_exception_records_are_reused_rather_than_rebuilt():
    results, decision = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    records = build_exception_records(results)
    trail = build_audit_trail(results, decision, exception_records=records)
    assert trail["exceptions"] == records
    assert trail["exception_count"] == len(records)


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------
def test_neither_function_mutates_its_inputs():
    results, decision = run_close_for(
        invoices_path=ERR / "invoices.csv", payments_path=ERR / "payments.csv", label="E1-E3"
    )
    results_before = results.copy()
    decision_before = dict(decision)

    records = build_exception_records(results)
    build_audit_trail(results, decision, exception_records=records)

    pd.testing.assert_frame_equal(results, results_before)
    assert decision == decision_before


def test_mutating_the_returned_records_does_not_affect_the_source():
    results, decision = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    records = build_exception_records(results)
    records[0]["details"]["month"] = "tampered"
    records.pop()

    fresh = build_exception_records(results)
    assert len(fresh) == 2
    assert fresh[0]["details"]["month"] == "2026-02"


def test_the_returned_trail_is_independent_of_the_records_passed_in():
    results, decision = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")
    records = build_exception_records(results)
    trail = build_audit_trail(results, decision, exception_records=records)
    records.clear()
    assert trail["exception_count"] == 2
    assert len(trail["exceptions"]) == 2


# ---------------------------------------------------------------------------
# serialisation and determinism
# ---------------------------------------------------------------------------
def test_the_audit_trail_is_json_serialisable_without_a_custom_encoder():
    for kwargs in (
        {"label": "clean"},
        {"invoices_path": ERR / "invoices.csv", "payments_path": ERR / "payments.csv",
         "label": "E1-E3"},
        {"fx_path": ERR / "fx_rates.csv", "label": "E4"},
    ):
        results, decision = run_close_for(**kwargs)
        trail = build_audit_trail(results, decision)
        encoded = json.dumps(trail)  # no default= fallback: numpy scalars would raise
        assert json.loads(encoded) == trail


def test_no_pandas_or_numpy_scalars_survive_into_the_records():
    results, _ = run_close_for(fx_path=ERR / "fx_rates.csv", label="E4")

    def check(value):
        if isinstance(value, dict):
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        else:
            assert value is None or isinstance(value, (bool, int, float, str)), (
                f"{value!r} is a {type(value).__name__}, which json cannot encode"
            )

    check(build_exception_records(results))


def test_two_runs_over_the_same_results_produce_identical_output():
    results, decision = run_close_for(
        invoices_path=ERR / "invoices.csv", payments_path=ERR / "payments.csv", label="E1-E3"
    )
    first = json.dumps(build_audit_trail(results, decision), sort_keys=True)
    second = json.dumps(build_audit_trail(results, decision), sort_keys=True)
    assert first == second


def test_absent_detail_is_omitted_rather_than_recorded_as_null():
    """A missing locus must not appear as a key with a null value.

    An audit reader distinguishes "this control recorded no month" from "this
    control recorded a month of nothing"; only the first is true here.
    """
    frame = pd.DataFrame([_row("MAT-05", "CRITICAL", "FAIL")])
    details = build_exception_records(frame)[0]["details"]
    for key in ("month", "entity", "pair_id", "record_ref", "references"):
        assert key not in details
    assert all(value is not None for value in details.values())

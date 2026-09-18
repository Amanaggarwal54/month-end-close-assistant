"""Tests for the close report and decision package.

Content is asserted against the pure ``ReportModel``; the PDF tests only check
that a real, multi-page document was written, so no test parses a PDF.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from controls import ControlConfig, decide_close, run_controls  # noqa: E402
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402
from plant_downstream_errors import ProtectedPathError  # noqa: E402
from report import (  # noqa: E402
    BAND_BLOCKED,
    BAND_PASS,
    BAND_WARN,
    STEM_EXCEPTION,
    STEM_REPORT,
    TONE_BLOCKED,
    TONE_PASS,
    TONE_WARN,
    build_report_model,
    describe_inputs,
    render_pdf,
    sha256_of,
    write_decision_package,
)

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return pd.read_csv(path, dtype=str)


def build_model(
    invoices_path: Path = RAW / "invoices.csv",
    payments_path: Path = RAW / "payments.csv",
    fx_path: Path = RAW / "fx_rates.csv",
    entries_override: pd.DataFrame | None = None,
    label: str = "test",
    config: ControlConfig | None = None,
    max_exception_rows: int = 25,
    with_inputs: bool = False,
):
    """Run the full pipeline and build the report model from its artefacts."""
    paths = {
        "purchase_orders": RAW / "purchase_orders.csv",
        "invoices": invoices_path,
        "payments": payments_path,
        "shared_costs": RAW / "shared_costs.csv",
        "fx_rates": fx_path,
        "fx_reference": RAW / "fx_rates_expected.csv",
    }
    pos = _read(paths["purchase_orders"])
    invoices = _read(invoices_path)
    payments = _read(payments_path)
    shared_costs = _read(paths["shared_costs"])
    fx_actual = _read(fx_path)
    fx_reference = _read(paths["fx_reference"])

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
        config=config,
        dataset_label=label,
        run_timestamp=FIXED_TIMESTAMP,
    )
    decision = decide_close(results, config=config, dataset_label=label,
                            run_timestamp=FIXED_TIMESTAMP)
    model = build_report_model(
        match_results=match_results,
        invoices=invoices,
        payments=payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=shared_costs,
        fx_actual=fx_actual,
        fx_reference=fx_reference,
        control_results=results,
        decision=decision,
        inputs=describe_inputs(paths) if with_inputs else None,
        max_exception_rows=max_exception_rows,
    )
    return model, results, decision


def control_status(model, check_id: str) -> str:
    for row in model.control_results_records:
        if row.get("check_id") == check_id:
            return row.get("status")
    raise AssertionError(f"{check_id} not present in the control results")


# ---------------------------------------------------------------------------
# 1. clean
# ---------------------------------------------------------------------------
def test_clean_data_produces_an_approved_report():
    model, results, decision = build_model(label="clean")
    assert decision["close_status"] == "PASS"
    assert model.report_allowed is True
    assert model.headline.band_text == BAND_PASS
    assert model.headline.tone == TONE_PASS
    assert model.failed_controls == []
    assert model.filename_stem.startswith(STEM_REPORT)


def test_matching_summary_reconciles_to_the_source_population():
    model, _, _ = build_model(label="clean")
    summary = dict(model.matching_summary)
    invoices = _read(RAW / "invoices.csv")
    payments = _read(RAW / "payments.csv")
    assert summary["Source invoice rows"] == f"{len(invoices):,}"
    assert summary["Source payment rows"] == f"{len(payments):,}"
    assert summary["Exceptions"] == "0"


def test_matched_value_is_reported_per_currency_and_never_blended():
    """Adding EUR and USD into one total would not be an economically real number."""
    model, _, _ = build_model(label="clean")
    summary = dict(model.matching_summary)

    currencies = {
        label.split("(")[-1].rstrip(")")
        for label in summary
        if label.startswith("Matched invoice value")
    }
    assert currencies == {"EUR", "USD"}

    invoices = _read(RAW / "invoices.csv")
    for currency in currencies:
        expected = pd.to_numeric(
            invoices.loc[invoices["currency"] == currency, "total_amount"], errors="coerce"
        ).sum()
        assert summary[f"Matched invoice value ({currency})"] == f"{expected:,.2f}"

    # no single blended figure anywhere in the summary
    assert not any(
        label.startswith("Value of matched invoices") for label in summary
    )


def test_intercompany_summary_nets_to_zero_on_clean_data():
    model, _, _ = build_model(label="clean")
    summary = dict(model.intercompany_summary)
    assert summary["Net position (EUR, expected 0.00)"] == "0.00"
    assert summary["Receivables (EUR)"] == summary["Payables (EUR)"]


# ---------------------------------------------------------------------------
# 2. warnings
# ---------------------------------------------------------------------------
def test_a_warning_still_produces_an_approved_report():
    """An extra FX month triggers FXC-04, a warning-severity control."""
    fx_reference = _read(RAW / "fx_rates_expected.csv")
    extra = pd.concat(
        [fx_reference, pd.DataFrame([{"month_end": "2026-04-30", "eur_usd": "1.06"}])],
        ignore_index=True,
    )
    extra_path = ROOT / "tests" / "_tmp_fx_with_extra_month.csv"
    extra.to_csv(extra_path, index=False)
    try:
        model, _, decision = build_model(fx_path=extra_path, label="warnings")
    finally:
        extra_path.unlink(missing_ok=True)

    assert decision["close_status"] == "PASS_WITH_WARNINGS"
    assert model.report_allowed is True
    assert model.headline.band_text == BAND_WARN
    assert model.headline.tone == TONE_WARN
    assert model.warnings, "the warning table should not be empty"
    assert model.filename_stem.startswith(STEM_REPORT)


# ---------------------------------------------------------------------------
# 3. critical failure
# ---------------------------------------------------------------------------
def test_a_critical_failure_blocks_the_report():
    model, _, decision = build_model(invoices_path=ERR / "invoices.csv", label="E1")
    assert decision["close_status"] == "FAIL"
    assert model.report_allowed is False
    assert model.headline.tone == TONE_BLOCKED
    assert model.filename_stem.startswith(STEM_EXCEPTION)
    blocking = {row["check_id"] for row in model.blocking_explanations}
    assert "MAT-04" in blocking
    assert all(row["explanation"] for row in model.blocking_explanations)


# ---------------------------------------------------------------------------
# 4. E4
# ---------------------------------------------------------------------------
def test_e4_shows_the_february_rate_and_blocks_on_the_fx_controls():
    model, _, decision = build_model(fx_path=ERR / "fx_rates.csv", label="E4")
    february = next(row for row in model.fx_summary if row["month"] == "2026-02")
    assert february["supplied"] == "1.1900"
    assert february["reference"] == "1.0900"
    assert february["difference"] == "+0.1000"
    assert february["verdict"] == "DIFFERS FROM REFERENCE"

    assert {row["check_id"] for row in model.failed_controls} == {"FXC-03", "FXC-05"}
    assert decision["blocking_controls"] == ["FXC-03", "FXC-05"]
    assert model.report_allowed is False


def test_e4_report_still_shows_every_intercompany_control_passing():
    """The independence story has to survive into the document a reader sees."""
    model, _, _ = build_model(fx_path=ERR / "fx_rates.csv", label="E4")
    for number in range(1, 11):
        assert control_status(model, f"ICO-{number:02d}") == "PASS"
    ico_row = next(row for row in model.control_summary if row["group"] == "ICO")
    assert ico_row["fail"] == 0 and ico_row["error"] == 0


# ---------------------------------------------------------------------------
# 5. E5 / E6
# ---------------------------------------------------------------------------
def _entries_with(error_id: str, tmp_path: Path) -> pd.DataFrame:
    from plant_downstream_errors import inject

    clean = generate_intercompany_entries(
        _read(RAW / "shared_costs.csv"), _read(RAW / "fx_rates.csv")
    )
    source = tmp_path / "intercompany_entries.csv"
    clean.to_csv(source, index=False)
    output = tmp_path / f"entries_{error_id}.csv"
    inject(error_id, source, output)
    return pd.read_csv(output)


def test_e5_blocks_the_report_and_names_ico_01(tmp_path: Path):
    model, _, decision = build_model(
        entries_override=_entries_with("E5", tmp_path), label="E5"
    )
    assert model.report_allowed is False
    assert "ICO-01" in {row["check_id"] for row in model.blocking_explanations}
    assert model.intercompany_exceptions.rows


def test_e6_blocks_the_report_and_names_ico_03(tmp_path: Path):
    model, _, decision = build_model(
        entries_override=_entries_with("E6", tmp_path), label="E6"
    )
    assert model.report_allowed is False
    assert "ICO-03" in {row["check_id"] for row in model.blocking_explanations}


# ---------------------------------------------------------------------------
# 6. approval language
# ---------------------------------------------------------------------------
def test_blocked_report_carries_no_approval_language_but_says_not_approved(tmp_path: Path):
    """Approval wording must be absent; 'NOT APPROVED' must be present."""
    model, _, _ = build_model(invoices_path=ERR / "invoices.csv", label="E1")

    assert BAND_PASS not in model.headline.band_text
    assert BAND_WARN not in model.headline.band_text
    assert "NOT APPROVED" in model.headline.band_text
    assert "approved" not in model.filename_stem.lower()

    # the cover sentence may say "not approved" but never approves anything
    summary = model.headline.summary.lower()
    assert "not approved" in summary
    for phrase in ("approved for close", "approved with warnings"):
        assert phrase not in summary

    path = render_pdf(model, tmp_path / f"{model.filename_stem}.pdf")
    assert path.exists()


def test_approved_report_carries_the_approval_band():
    model, _, _ = build_model(label="clean")
    assert model.headline.band_text == BAND_PASS
    assert "NOT APPROVED" not in model.headline.band_text


# ---------------------------------------------------------------------------
# 7-8. rendering and filenames
# ---------------------------------------------------------------------------
def test_pdf_is_written_and_is_a_real_multi_page_document(tmp_path: Path):
    model, _, _ = build_model(label="clean")
    path = render_pdf(model, tmp_path / "report.pdf")
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 8_000
    assert data.count(b"/Type /Page") >= 4 or data.count(b"/Type/Page") >= 4


def test_filename_stem_differs_between_approved_and_blocked():
    clean, _, _ = build_model(label="clean")
    blocked, _, _ = build_model(invoices_path=ERR / "invoices.csv", label="E1")
    assert clean.filename_stem == f"{STEM_REPORT}_clean"
    assert blocked.filename_stem == f"{STEM_EXCEPTION}_e1"


# ---------------------------------------------------------------------------
# 9. package completeness
# ---------------------------------------------------------------------------
def test_decision_package_contains_every_artefact_with_matching_hashes(tmp_path: Path):
    model, results, decision = build_model(label="clean", with_inputs=True)
    written = write_decision_package(model, tmp_path)

    assert set(written) == {"report_pdf", "control_results", "close_decision",
                            "audit_trail", "exceptions", "package_manifest"}
    for path in written.values():
        assert path.exists()

    manifest = json.loads(written["package_manifest"].read_text())
    assert manifest["close_status"] == decision["close_status"]
    assert "except" in manifest["hash_scope"]  # the manifest states its own hash scope
    assert not any(
        Path(item["path"]).name == written["package_manifest"].name
        for item in manifest["outputs"]
    )
    assert manifest["report_allowed"] is True

    assert manifest["inputs"], "the provenance appendix needs input hashes"
    for item in manifest["inputs"]:
        # paths are stored relative to the working directory for portability
        assert item["sha256"] == sha256_of(Path(item["path"]))

    for item in manifest["outputs"]:
        if Path(item["path"]).name != written["package_manifest"].name:
            assert item["sha256"] == sha256_of(item["path"])

    stored = pd.read_csv(written["control_results"])
    assert len(stored) == len(results)


# ---------------------------------------------------------------------------
# 10. determinism
# ---------------------------------------------------------------------------
def test_two_runs_with_the_same_timestamp_produce_identical_bytes(tmp_path: Path):
    model, _, _ = build_model(label="clean")
    first = render_pdf(model, tmp_path / "first.pdf").read_bytes()
    second = render_pdf(model, tmp_path / "second.pdf").read_bytes()
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


# ---------------------------------------------------------------------------
# 11. source safety
# ---------------------------------------------------------------------------
def test_writing_the_report_into_data_raw_is_refused():
    model, _, _ = build_model(label="clean")
    with pytest.raises(ProtectedPathError):
        render_pdf(model, RAW / "close_report.pdf")
    assert not (RAW / "close_report.pdf").exists()


def test_a_full_report_run_leaves_every_source_file_untouched(tmp_path: Path):
    before = {path.name: sha256_of(path) for path in sorted(RAW.glob("*.csv"))}
    model, _, _ = build_model(label="clean", with_inputs=True)
    write_decision_package(model, tmp_path)
    after = {path.name: sha256_of(path) for path in sorted(RAW.glob("*.csv"))}
    assert before == after


# ---------------------------------------------------------------------------
# 12. exception capping
# ---------------------------------------------------------------------------
def test_exception_tables_are_capped_with_an_accurate_more_note():
    model, _, _ = build_model(
        invoices_path=ERR / "invoices.csv",
        payments_path=ERR / "payments.csv",
        label="E1-E3",
        max_exception_rows=2,
    )
    table = model.matching_exceptions
    assert table.shown == 2
    assert table.total >= 4  # E1 + two duplicate rows + the orphan payment
    assert table.omitted == table.total - 2
    assert f"and {table.omitted} more" in table.more_note


def test_no_more_note_when_nothing_is_omitted():
    model, _, _ = build_model(
        invoices_path=ERR / "invoices.csv", label="E1", max_exception_rows=100
    )
    assert model.matching_exceptions.more_note == ""


# ---------------------------------------------------------------------------
# 13. the report reflects its inputs and never recomputes them
# ---------------------------------------------------------------------------
def test_reported_intercompany_total_is_the_sum_of_the_column_it_was_given():
    entries = generate_intercompany_entries(
        _read(RAW / "shared_costs.csv"), _read(RAW / "fx_rates.csv")
    )
    model, _, _ = build_model(entries_override=entries, label="clean")
    expected = pd.to_numeric(
        entries.loc[entries["entry_type"] == "RECEIVABLE", "eur_equivalent"], errors="coerce"
    ).sum()
    assert dict(model.intercompany_summary)["Receivables (EUR)"] == f"{expected:,.2f}"


def test_altering_the_entries_changes_the_reported_total():
    """Proof that the report presents its inputs rather than recalculating them."""
    entries = generate_intercompany_entries(
        _read(RAW / "shared_costs.csv"), _read(RAW / "fx_rates.csv")
    )
    baseline, _, _ = build_model(entries_override=entries, label="clean")

    altered = entries.copy()
    target = altered[altered["entry_type"] == "RECEIVABLE"].index[0]
    altered.loc[target, "eur_equivalent"] = float(altered.loc[target, "eur_equivalent"]) + 1000.0
    changed, _, _ = build_model(entries_override=altered, label="altered")

    assert (
        dict(changed.intercompany_summary)["Receivables (EUR)"]
        != dict(baseline.intercompany_summary)["Receivables (EUR)"]
    )
    # and the control layer catches the tampering rather than the report hiding it
    assert changed.report_allowed is False

"""Tests for the downstream error injector and the controls that catch E5 and E6."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from controls import CLOSE_FAIL, FAIL, PASS, ControlConfig, decide_close, run_controls  # noqa: E402
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402
from plant_downstream_errors import (  # noqa: E402
    MANIFEST_COLUMNS,
    ProtectedPathError,
    assert_writable,
    find_cross_currency_pairs,
    inject,
    plant_e5,
    plant_e6,
)

RAW = ROOT / "data" / "raw"


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return pd.read_csv(path, dtype=str)


@pytest.fixture
def clean_entries_file(tmp_path: Path) -> Path:
    """A clean intercompany output generated from the project data."""
    entries = generate_intercompany_entries(
        _read(RAW / "shared_costs.csv"), _read(RAW / "fx_rates.csv")
    )
    path = tmp_path / "intercompany_entries.csv"
    entries.to_csv(path, index=False)
    return path


def _controls_for(entries: pd.DataFrame, label: str):
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
        config=ControlConfig(),
        dataset_label=label,
    )
    return results, decide_close(results, dataset_label=label)


def _status(results: pd.DataFrame, check_id: str) -> str:
    return results.loc[results["check_id"] == check_id, "status"].iloc[0]


# ---------------------------------------------------------------------------
# write guard
# ---------------------------------------------------------------------------
def test_writing_into_data_raw_is_rejected():
    with pytest.raises(ProtectedPathError):
        assert_writable(RAW / "intercompany_entries_E5.csv")
    with pytest.raises(ProtectedPathError):
        assert_writable("data/raw/anything.csv")


def test_writing_outside_data_raw_is_allowed(tmp_path: Path):
    assert assert_writable(tmp_path / "entries_E5.csv").name == "entries_E5.csv"


def test_inject_refuses_to_write_into_data_raw(clean_entries_file: Path):
    with pytest.raises(ProtectedPathError):
        inject("E5", clean_entries_file, RAW / "corrupted.csv")
    assert not (RAW / "corrupted.csv").exists()


# ---------------------------------------------------------------------------
# determinism and target selection
# ---------------------------------------------------------------------------
def test_target_selection_is_deterministic_and_distinct(clean_entries_file: Path):
    entries = pd.read_csv(clean_entries_file)
    first = plant_e5(entries)[1]["pair_id"]
    second = plant_e5(entries)[1]["pair_id"]
    e6_target = plant_e6(entries)[1]["pair_id"]

    assert first == second, "E5 must select the same pair on every run"
    assert first != e6_target, "E5 and E6 must corrupt different pairs"
    candidates = find_cross_currency_pairs(entries)
    assert [first, e6_target] == candidates[:2]


def test_targets_are_cross_currency_pairs(clean_entries_file: Path):
    entries = pd.read_csv(clean_entries_file)
    for planter in (plant_e5, plant_e6):
        target = planter(entries)[1]["pair_id"]
        assert entries.loc[entries["pair_id"] == target, "currency"].nunique() == 2


# ---------------------------------------------------------------------------
# E5
# ---------------------------------------------------------------------------
def test_e5_removes_exactly_one_side_and_ico_01_fails(clean_entries_file: Path, tmp_path: Path):
    clean = pd.read_csv(clean_entries_file)
    output = tmp_path / "error_data" / "intercompany_entries_E5.csv"
    record = inject("E5", clean_entries_file, output, tmp_path / "manifest.csv")

    corrupted = pd.read_csv(output)
    assert len(corrupted) == len(clean) - 1
    removed = clean[~clean["entry_id"].isin(corrupted["entry_id"])]
    assert len(removed) == 1
    assert removed.iloc[0]["entry_type"] == "PAYABLE"
    assert removed.iloc[0]["pair_id"] == record["pair_id"]

    results, decision = _controls_for(corrupted, "E5")
    assert _status(results, "ICO-01") == FAIL
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False
    assert "ICO-01" in decision["blocking_controls"]


# ---------------------------------------------------------------------------
# E6
# ---------------------------------------------------------------------------
def test_e6_blanks_exactly_one_local_amount_and_ico_03_fails(
    clean_entries_file: Path, tmp_path: Path
):
    clean = pd.read_csv(clean_entries_file)
    output = tmp_path / "error_data" / "intercompany_entries_E6.csv"
    record = inject("E6", clean_entries_file, output, tmp_path / "manifest.csv")

    corrupted = pd.read_csv(output)
    assert len(corrupted) == len(clean)
    blanked = corrupted[corrupted["local_amount"].isna()]
    assert len(blanked) == 1
    assert blanked.iloc[0]["entry_id"] == record["entry_id"]
    # every other column on that row is untouched
    assert pd.notna(blanked.iloc[0]["eur_equivalent"])

    results, decision = _controls_for(corrupted, "E6")
    assert _status(results, "ICO-03") == FAIL
    assert decision["close_status"] == CLOSE_FAIL
    assert decision["report_allowed"] is False


def test_e6_is_invisible_to_the_elimination_control(clean_entries_file: Path, tmp_path: Path):
    """The point of blanking local_amount rather than eur_equivalent.

    Elimination compares EUR equivalents, so it still passes; only the
    completeness control catches the defect. This proves ICO-03 is independent
    of ICO-04 rather than redundant with it.
    """
    output = tmp_path / "error_data" / "intercompany_entries_E6.csv"
    inject("E6", clean_entries_file, output, tmp_path / "manifest.csv")
    corrupted = pd.read_csv(output)

    results, _ = _controls_for(corrupted, "E6")
    assert _status(results, "ICO-04") == PASS
    assert _status(results, "ICO-03") == FAIL


# ---------------------------------------------------------------------------
# clean artefact and manifest
# ---------------------------------------------------------------------------
def test_the_clean_output_is_never_modified(clean_entries_file: Path, tmp_path: Path):
    before = clean_entries_file.read_bytes()
    inject("E5", clean_entries_file, tmp_path / "e5.csv", tmp_path / "manifest.csv")
    inject("E6", clean_entries_file, tmp_path / "e6.csv", tmp_path / "manifest.csv")
    assert clean_entries_file.read_bytes() == before


def test_manifest_records_both_injections(clean_entries_file: Path, tmp_path: Path):
    manifest_path = tmp_path / "downstream_planted_errors.csv"
    inject("E5", clean_entries_file, tmp_path / "e5.csv", manifest_path)
    inject("E6", clean_entries_file, tmp_path / "e6.csv", manifest_path)

    manifest = pd.read_csv(manifest_path, dtype=str)
    assert list(manifest.columns) == MANIFEST_COLUMNS
    assert sorted(manifest["error_id"]) == ["E5", "E6"]
    assert set(manifest["expected_control"]) == {"ICO-01", "ICO-03"}
    assert manifest["pair_id"].nunique() == 2
    assert manifest["injected_at"].notna().all()


def test_reinjecting_replaces_the_manifest_row_rather_than_appending(
    clean_entries_file: Path, tmp_path: Path
):
    manifest_path = tmp_path / "manifest.csv"
    inject("E5", clean_entries_file, tmp_path / "e5.csv", manifest_path)
    inject("E5", clean_entries_file, tmp_path / "e5.csv", manifest_path)
    manifest = pd.read_csv(manifest_path, dtype=str)
    assert len(manifest) == 1


def test_unknown_error_id_is_rejected(clean_entries_file: Path, tmp_path: Path):
    with pytest.raises(ValueError):
        inject("E9", clean_entries_file, tmp_path / "e9.csv")

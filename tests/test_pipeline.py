"""Tests for the end-to-end pipeline.

The load test of the suite is ``test_pipeline_package_is_identical_to_running_the
_modules_directly``: it hashes the artefacts produced by hand against those
produced by the pipeline. Everything else is exit codes, safety and determinism.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline  # noqa: E402
from controls import decide_close, run_controls  # noqa: E402
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402
from pipeline import (  # noqa: E402
    EXIT_ALLOWED,
    EXIT_BLOCKED,
    EXIT_ERROR,
    CloseRunConfig,
    execute_close,
    load_artefacts,
    main,
    run_close,
)
from plant_downstream_errors import ProtectedPathError, inject  # noqa: E402
from report import build_report_model, describe_inputs, sha256_of, write_decision_package  # noqa: E402

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"

PACKAGE_FILES = ["control_results.csv", "close_decision.json", "package_manifest.json"]


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return path


def config_for(tmp_path: Path, **overrides) -> CloseRunConfig:
    base = {
        "data_dir": RAW,
        "out_dir": tmp_path / "out",
        "run_timestamp": FIXED_TIMESTAMP,
        "dataset_label": "clean",
    }
    base.update(overrides)
    return CloseRunConfig(**base)


@pytest.fixture
def entries_e5(tmp_path: Path) -> Path:
    return _planted("E5", tmp_path)


@pytest.fixture
def entries_e6(tmp_path: Path) -> Path:
    return _planted("E6", tmp_path)


def _planted(error_id: str, tmp_path: Path) -> Path:
    clean = generate_intercompany_entries(
        pd.read_csv(_require(RAW / "shared_costs.csv"), dtype=str),
        pd.read_csv(_require(RAW / "fx_rates.csv"), dtype=str),
    )
    source = tmp_path / "intercompany_entries.csv"
    clean.to_csv(source, index=False)
    output = tmp_path / f"entries_{error_id}.csv"
    inject(error_id, source, output)
    return output


# ---------------------------------------------------------------------------
# 1. clean end to end
# ---------------------------------------------------------------------------
def test_clean_run_passes_and_writes_the_package(tmp_path: Path):
    _require(RAW / "invoices.csv")
    result = run_close(config_for(tmp_path))

    assert result.close_status == "PASS"
    assert result.report_allowed is True
    assert result.exit_code == EXIT_ALLOWED
    assert result.blocking_controls == []
    assert result.written["report_pdf"].exists()


def test_package_contains_every_artefact(tmp_path: Path):
    _require(RAW / "invoices.csv")
    result = run_close(config_for(tmp_path))
    folder = result.written["report_pdf"].parent

    assert result.written["report_pdf"].name.startswith("close_report_clean")
    for name in PACKAGE_FILES:
        assert (folder / name).exists(), name


# ---------------------------------------------------------------------------
# 2-5. planted errors
# ---------------------------------------------------------------------------
def test_e4_is_blocked_by_the_fx_controls(tmp_path: Path):
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4")
    )
    assert result.close_status == "FAIL"
    assert result.exit_code == EXIT_BLOCKED
    assert result.blocking_controls == ["FXC-03", "FXC-05"]
    assert result.written["report_pdf"].name.startswith("close_exception_report_e4")


def test_e5_is_blocked_by_ico_01(tmp_path: Path, entries_e5: Path):
    result = run_close(config_for(tmp_path, ic_entries=entries_e5, dataset_label="E5"))
    assert result.exit_code == EXIT_BLOCKED
    assert "ICO-01" in result.blocking_controls


def test_e6_is_blocked_by_ico_03(tmp_path: Path, entries_e6: Path):
    result = run_close(config_for(tmp_path, ic_entries=entries_e6, dataset_label="E6"))
    assert result.exit_code == EXIT_BLOCKED
    assert "ICO-03" in result.blocking_controls


def test_source_errors_are_blocked_by_the_matching_controls(tmp_path: Path):
    _require(ERR / "invoices.csv")
    result = run_close(
        config_for(
            tmp_path,
            data_dir=ERR,
            fx_rates=RAW / "fx_rates.csv",
            dataset_label="E1-E3",
        )
    )
    assert result.exit_code == EXIT_BLOCKED
    assert set(result.blocking_controls) == {"MAT-03", "MAT-04", "MAT-06"}


# ---------------------------------------------------------------------------
# 7. parity with running the modules directly
# ---------------------------------------------------------------------------
def test_pipeline_package_is_identical_to_running_the_modules_directly(tmp_path: Path):
    """Requirement 10, tested rather than asserted.

    The same close is produced twice - once by calling the modules by hand, once
    through the pipeline - and every artefact is compared by hash.
    """
    paths = {
        "purchase_orders": _require(RAW / "purchase_orders.csv"),
        "invoices": RAW / "invoices.csv",
        "payments": RAW / "payments.csv",
        "shared_costs": RAW / "shared_costs.csv",
        "fx_rates": RAW / "fx_rates.csv",
        "fx_reference": RAW / "fx_rates_expected.csv",
    }
    read = lambda path: pd.read_csv(path, dtype=str)  # noqa: E731
    pos, invoices, payments = (read(paths[k]) for k in
                               ("purchase_orders", "invoices", "payments"))
    shared_costs, fx_actual, fx_reference = (read(paths[k]) for k in
                                             ("shared_costs", "fx_rates", "fx_reference"))

    match_results = three_way_match(pos, invoices, payments)
    entries = generate_intercompany_entries(shared_costs, fx_actual)
    elimination = check_elimination(entries)
    results = run_controls(
        match_results=match_results, invoices=invoices, payments=payments,
        ic_entries=entries, ic_elimination=elimination, shared_costs=shared_costs,
        fx_actual=fx_actual, fx_reference=fx_reference, purchase_orders=pos,
        dataset_label="clean", run_timestamp=FIXED_TIMESTAMP,
    )
    decision = decide_close(results, dataset_label="clean", run_timestamp=FIXED_TIMESTAMP)
    model = build_report_model(
        match_results=match_results, invoices=invoices, payments=payments,
        ic_entries=entries, ic_elimination=elimination, shared_costs=shared_costs,
        fx_actual=fx_actual, fx_reference=fx_reference, control_results=results,
        decision=decision, inputs=describe_inputs(paths), max_exception_rows=25,
    )
    manual = write_decision_package(model, tmp_path / "manual")
    piped = run_close(config_for(tmp_path, out_dir=tmp_path / "piped")).written

    for role in ("report_pdf", "control_results", "close_decision", "package_manifest"):
        assert manual[role].name == piped[role].name, role
        if role == "package_manifest":
            continue  # the manifest records its own directory, which differs
        assert sha256_of(manual[role]) == sha256_of(piped[role]), role


# ---------------------------------------------------------------------------
# 8-9. safety
# ---------------------------------------------------------------------------
def test_writing_the_package_into_data_raw_is_refused(tmp_path: Path):
    _require(RAW / "invoices.csv")
    with pytest.raises(ProtectedPathError):
        run_close(config_for(tmp_path, out_dir=RAW))


def test_cli_returns_one_when_the_output_directory_is_protected():
    code = main([
        "--data-dir", str(RAW), "--out-dir", str(RAW),
        "--run-timestamp", FIXED_TIMESTAMP, "--quiet",
    ])
    assert code == EXIT_ERROR


def test_a_full_run_leaves_every_source_file_untouched(tmp_path: Path):
    _require(RAW / "invoices.csv")
    before = {path.name: sha256_of(path) for path in sorted(RAW.glob("*.csv"))}
    run_close(config_for(tmp_path))
    after = {path.name: sha256_of(path) for path in sorted(RAW.glob("*.csv"))}
    assert before == after


def test_execute_close_does_not_mutate_the_frames_it_is_given(tmp_path: Path):
    config = config_for(tmp_path, write_package=False)
    sources = load_artefacts(config)
    snapshots = {
        name: getattr(sources, name).copy()
        for name in ("purchase_orders", "invoices", "payments", "shared_costs",
                     "fx_rates", "fx_reference")
    }
    execute_close(sources, config)
    for name, before in snapshots.items():
        pd.testing.assert_frame_equal(getattr(sources, name), before)


# ---------------------------------------------------------------------------
# 10. determinism
# ---------------------------------------------------------------------------
def test_two_runs_with_the_same_timestamp_produce_identical_bytes(tmp_path: Path):
    _require(RAW / "invoices.csv")
    first = run_close(config_for(tmp_path, out_dir=tmp_path / "a")).written["report_pdf"]
    second = run_close(config_for(tmp_path, out_dir=tmp_path / "b")).written["report_pdf"]
    assert sha256_of(first) == sha256_of(second)


def test_one_timestamp_is_used_everywhere_in_a_run(tmp_path: Path):
    result = run_close(config_for(tmp_path))
    assert result.run_timestamp == FIXED_TIMESTAMP
    assert result.decision["run_timestamp"] == FIXED_TIMESTAMP
    assert set(result.control_results["run_timestamp"]) == {FIXED_TIMESTAMP}
    assert result.model.run_timestamp == FIXED_TIMESTAMP


# ---------------------------------------------------------------------------
# 11. exit codes
# ---------------------------------------------------------------------------
def test_exit_codes_through_the_cli(tmp_path: Path):
    _require(RAW / "invoices.csv")
    clean = main(["--data-dir", str(RAW), "--out-dir", str(tmp_path / "clean"),
                  "--run-timestamp", FIXED_TIMESTAMP, "--quiet"])
    assert clean == EXIT_ALLOWED

    blocked = main(["--data-dir", str(RAW), "--fx-rates", str(_require(ERR / "fx_rates.csv")),
                    "--dataset-label", "E4", "--out-dir", str(tmp_path / "e4"),
                    "--run-timestamp", FIXED_TIMESTAMP, "--quiet"])
    assert blocked == EXIT_BLOCKED

    missing = main(["--data-dir", str(tmp_path / "nowhere"),
                    "--out-dir", str(tmp_path / "x"), "--quiet"])
    assert missing == EXIT_ERROR


def test_no_fail_on_blocked_still_writes_the_exception_report(tmp_path: Path):
    code = main(["--data-dir", str(RAW), "--fx-rates", str(_require(ERR / "fx_rates.csv")),
                 "--dataset-label", "E4", "--out-dir", str(tmp_path / "out"),
                 "--run-timestamp", FIXED_TIMESTAMP, "--no-fail-on-blocked", "--quiet"])
    assert code == EXIT_ALLOWED
    written = list((tmp_path / "out" / "e4").glob("close_exception_report_*.pdf"))
    assert len(written) == 1


# ---------------------------------------------------------------------------
# 12. dry run
# ---------------------------------------------------------------------------
def test_dry_run_decides_the_close_without_writing_anything(tmp_path: Path):
    out_dir = tmp_path / "out"
    code = main(["--data-dir", str(RAW), "--out-dir", str(out_dir),
                 "--run-timestamp", FIXED_TIMESTAMP, "--dry-run", "--quiet"])
    assert code == EXIT_ALLOWED
    assert not out_dir.exists()


def test_dry_run_still_returns_a_full_result(tmp_path: Path):
    result = run_close(config_for(tmp_path, write_package=False))
    assert result.written == {}
    assert result.close_status == "PASS"
    assert result.model.cover_figures


# ---------------------------------------------------------------------------
# 13. structure guard: the orchestration layer stays an orchestration layer
# ---------------------------------------------------------------------------
ALLOWED_PROJECT_IMPORTS = {
    "match", "intercompany", "controls", "report", "plant_downstream_errors",
}
ALLOWED_THIRD_PARTY = {"pandas"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_pipeline_imports_its_accounting_work_and_defines_none_of_it():
    """A dependency guard, not a lexical one.

    The pipeline may import from the four accounting modules and from pandas and
    the standard library. Anything else - a new numeric helper module, a private
    copy of a calculation - would mean orchestration had started to acquire
    accounting logic, which is what the parity test then could not protect.
    """
    source = ROOT / "src" / "pipeline.py"
    imported = _imported_modules(source)
    stdlib = set(getattr(sys, "stdlib_module_names", set()))

    unexpected = imported - ALLOWED_PROJECT_IMPORTS - ALLOWED_THIRD_PARTY - stdlib
    assert not unexpected, f"unexpected imports in pipeline.py: {sorted(unexpected)}"


def test_pipeline_delegates_every_accounting_step():
    """Each step of the close must be a call into an existing module."""
    for name in ("three_way_match", "generate_intercompany_entries", "check_elimination",
                 "run_controls", "decide_close", "build_report_model",
                 "write_decision_package"):
        attribute = getattr(pipeline, name, None)
        assert attribute is not None, f"pipeline does not use {name}"
        assert attribute.__module__ != "pipeline", (
            f"{name} is defined in pipeline.py; it must come from the module that owns it"
        )

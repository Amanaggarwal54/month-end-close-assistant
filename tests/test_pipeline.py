"""Tests for the end-to-end pipeline.

The load test of the suite is ``test_pipeline_package_is_identical_to_running_the
_modules_directly``: it hashes the artefacts produced by hand against those
produced by the pipeline. Everything else is exit codes, safety and determinism.
"""

from __future__ import annotations

import ast
import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline  # noqa: E402
from audit import build_audit_trail, build_exception_records  # noqa: E402
from exception_workflow import (  # noqa: E402
    INVESTIGATING,
    RESOLVED,
    assign_owner,
    build_exception_register,
    transition_exception,
)
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

PACKAGE_FILES = [
    "control_results.csv",
    "close_decision.json",
    "audit_trail.json",
    "exceptions.json",
    "exception_register.json",
    "package_manifest.json",
]
PACKAGE_ROLES = (
    "report_pdf", "control_results", "close_decision",
    "audit_trail", "exceptions", "exception_register", "package_manifest",
)


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
    exception_records = build_exception_records(results)
    audit_trail = build_audit_trail(results, decision, exception_records=exception_records)
    exception_register = build_exception_register(exception_records, "clean")
    model = build_report_model(
        match_results=match_results, invoices=invoices, payments=payments,
        ic_entries=entries, ic_elimination=elimination, shared_costs=shared_costs,
        fx_actual=fx_actual, fx_reference=fx_reference, control_results=results,
        decision=decision, inputs=describe_inputs(paths), max_exception_rows=25,
        audit_trail=audit_trail, exception_records=exception_records,
        exception_register=exception_register,
    )
    manual = write_decision_package(model, tmp_path / "manual")
    piped = run_close(config_for(tmp_path, out_dir=tmp_path / "piped")).written

    assert set(manual) == set(piped) == set(PACKAGE_ROLES)
    for role in PACKAGE_ROLES:
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
    "match", "intercompany", "controls", "report", "audit", "exception_workflow",
    "plant_downstream_errors",
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
                 "run_controls", "decide_close", "build_exception_records",
                 "build_audit_trail", "build_exception_register",
                 "build_report_model", "write_decision_package"):
        attribute = getattr(pipeline, name, None)
        assert attribute is not None, f"pipeline does not use {name}"
        assert attribute.__module__ != "pipeline", (
            f"{name} is defined in pipeline.py; it must come from the module that owns it"
        )


# ---------------------------------------------------------------------------
# 14. the audit artefacts in the decision package (Step 11B)
# ---------------------------------------------------------------------------
def _package(tmp_path: Path, name: str = "run", **overrides):
    """Run the close and return (result, package paths, loaded json)."""
    result = run_close(config_for(tmp_path, out_dir=tmp_path / name, **overrides))
    written = result.written
    loaded = {
        "audit": json.loads(written["audit_trail"].read_text(encoding="utf-8")),
        "exceptions": json.loads(written["exceptions"].read_text(encoding="utf-8")),
        "manifest": json.loads(written["package_manifest"].read_text(encoding="utf-8")),
    }
    return result, written, loaded


def test_clean_package_contains_the_audit_artefacts(tmp_path: Path):
    _require(RAW / "invoices.csv")
    _, written, _ = _package(tmp_path)
    assert written["audit_trail"].name == "audit_trail.json"
    assert written["exceptions"].name == "exceptions.json"
    assert written["audit_trail"].exists() and written["exceptions"].exists()


def test_clean_exceptions_file_is_an_empty_list(tmp_path: Path):
    _require(RAW / "invoices.csv")
    _, _, loaded = _package(tmp_path)
    assert loaded["exceptions"] == []


def test_clean_audit_trail_reports_pass_and_no_exceptions(tmp_path: Path):
    _require(RAW / "invoices.csv")
    _, _, loaded = _package(tmp_path)
    audit = loaded["audit"]
    assert audit["close_status"] == "PASS"
    assert audit["report_allowed"] is True
    assert audit["exception_count"] == 0
    assert audit["exceptions"] == []
    assert audit["blocking_controls"] == []
    assert audit["critical_failed_count"] == 0


def test_e4_package_carries_exactly_the_two_fx_exceptions(tmp_path: Path):
    _, _, loaded = _package(
        tmp_path, "e4", fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4"
    )
    assert [record["check_id"] for record in loaded["exceptions"]] == ["FXC-03", "FXC-05"]
    for record in loaded["exceptions"]:
        assert record["control_family"] == "SOURCE_ACCURACY"
        assert record["error_reference"] == "E4"


def test_e4_audit_trail_reports_fail_and_blocks_the_report(tmp_path: Path):
    _, _, loaded = _package(
        tmp_path, "e4", fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4"
    )
    audit = loaded["audit"]
    assert audit["close_status"] == "FAIL"
    assert audit["report_allowed"] is False
    assert audit["blocking_controls"] == ["FXC-03", "FXC-05"]
    assert audit["exception_count"] == 2
    assert audit["critical_failed_count"] == 2


def test_e1_to_e3_package_carries_the_matching_exceptions(tmp_path: Path):
    _require(ERR / "invoices.csv")
    _, _, loaded = _package(
        tmp_path, "e13", data_dir=ERR, fx_rates=RAW / "fx_rates.csv", dataset_label="E1-E3"
    )
    assert [record["check_id"] for record in loaded["exceptions"]] == [
        "MAT-03", "MAT-04", "MAT-06"
    ]
    assert loaded["audit"]["blocking_controls"] == ["MAT-03", "MAT-04", "MAT-06"]


def test_the_embedded_and_standalone_exception_lists_agree(tmp_path: Path):
    """audit_trail.json embeds the exceptions that exceptions.json holds flat.

    Both come from the same object in one call, so they cannot drift; this test
    is what keeps that true if either write path is changed later.
    """
    _, _, loaded = _package(
        tmp_path, "e4", fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4"
    )
    assert loaded["audit"]["exceptions"] == loaded["exceptions"]


def test_the_result_object_exposes_the_same_audit_data_that_was_written(tmp_path: Path):
    result, _, loaded = _package(
        tmp_path, "e4", fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4"
    )
    assert result.audit_trail == loaded["audit"]
    assert result.exception_records == loaded["exceptions"]
    assert result.model.audit_trail == loaded["audit"]


# ---------------------------------------------------------------------------
# 15. manifest coverage of the audit artefacts
# ---------------------------------------------------------------------------
def test_manifest_hashes_both_new_json_files(tmp_path: Path):
    _require(RAW / "invoices.csv")
    _, written, loaded = _package(tmp_path)
    by_role = {item["role"]: item for item in loaded["manifest"]["outputs"]}

    assert {"audit_trail", "exceptions"} <= set(by_role)
    for role in ("audit_trail", "exceptions"):
        assert by_role[role]["sha256"] == sha256_of(written[role]), role


def test_manifest_still_excludes_itself_and_keeps_its_hash_scope(tmp_path: Path):
    _require(RAW / "invoices.csv")
    _, written, loaded = _package(tmp_path)
    manifest = loaded["manifest"]

    names = {Path(item["path"]).name for item in manifest["outputs"]}
    assert written["package_manifest"].name not in names
    assert "except" in manifest["hash_scope"]
    for item in manifest["outputs"]:
        assert item["sha256"] == sha256_of(Path(item["path"]))


def test_changing_the_audit_output_changes_its_recorded_hash(tmp_path: Path):
    """The manifest hash must track the audit content, not just its presence."""
    _require(RAW / "invoices.csv")
    baseline = run_close(config_for(tmp_path, out_dir=tmp_path / "a", write_package=False))

    tampered_trail = dict(baseline.audit_trail)
    tampered_trail["exception_count"] = 99
    tampered_model = replace(baseline.model, audit_trail=tampered_trail)

    original = write_decision_package(baseline.model, tmp_path / "original")
    tampered = write_decision_package(tampered_model, tmp_path / "tampered")

    def recorded(paths, role):
        manifest = json.loads(paths["package_manifest"].read_text(encoding="utf-8"))
        return {item["role"]: item["sha256"] for item in manifest["outputs"]}[role]

    assert recorded(original, "audit_trail") != recorded(tampered, "audit_trail")
    assert recorded(original, "audit_trail") == sha256_of(original["audit_trail"])
    assert recorded(tampered, "audit_trail") == sha256_of(tampered["audit_trail"])
    # the rest of the package is untouched by an audit-only change
    assert recorded(original, "control_results") == recorded(tampered, "control_results")


# ---------------------------------------------------------------------------
# 16. determinism across every artefact
# ---------------------------------------------------------------------------
def test_every_package_artefact_is_byte_identical_across_two_runs(tmp_path: Path):
    _require(ERR / "fx_rates.csv")
    common = dict(fx_rates=ERR / "fx_rates.csv", dataset_label="E4")
    first = run_close(config_for(tmp_path, out_dir=tmp_path / "first", **common)).written
    second = run_close(config_for(tmp_path, out_dir=tmp_path / "second", **common)).written

    for role in PACKAGE_ROLES:
        if role == "package_manifest":
            continue  # records its own directory, which differs between runs
        assert sha256_of(first[role]) == sha256_of(second[role]), role


def test_the_audit_json_is_written_with_sorted_keys(tmp_path: Path):
    """Key order is what makes the file byte-stable, so assert it on disk."""
    _require(ERR / "fx_rates.csv")
    _, written, _ = _package(
        tmp_path, "e4", fx_rates=ERR / "fx_rates.csv", dataset_label="E4"
    )
    text = written["audit_trail"].read_text(encoding="utf-8")
    top_level = [
        line.split('"')[1]
        for line in text.splitlines()
        if line.startswith('  "')
    ]
    assert top_level == sorted(top_level)
    assert text.startswith("{\n  ")  # indent=2


def test_exception_order_on_disk_matches_the_order_audit_py_produced(tmp_path: Path):
    _require(ERR / "invoices.csv")
    result, _, loaded = _package(
        tmp_path, "e13", data_dir=ERR, fx_rates=RAW / "fx_rates.csv", dataset_label="E1-E3"
    )
    on_disk = [record["check_id"] for record in loaded["exceptions"]]
    in_memory = [record["check_id"] for record in build_exception_records(result.control_results)]
    assert on_disk == in_memory


# ---------------------------------------------------------------------------
# 17. the audit layer stays upstream of presentation
# ---------------------------------------------------------------------------
def test_report_does_not_build_audit_data_itself():
    """report.py may serialise audit data, never construct it.

    A presentation layer that re-derived the exception list could disagree with
    the audit file written beside it, which is the one inconsistency this
    package must not be able to contain.
    """
    import report

    source = ROOT / "src" / "report.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported = _imported_modules(source)
    assert "audit" not in imported, "report.py must not import the audit module"

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_audit_trail" not in called
    assert "build_exception_records" not in called
    assert not hasattr(report, "build_audit_trail")
    assert not hasattr(report, "build_exception_records")


def test_audit_construction_happens_in_the_pipeline_not_the_writer(tmp_path: Path):
    """A model with no audit data writes empty audit files rather than making some."""
    _require(RAW / "invoices.csv")
    baseline = run_close(config_for(tmp_path, write_package=False))
    bare = replace(baseline.model, audit_trail={}, exception_records=[])
    written = write_decision_package(bare, tmp_path / "bare")

    assert json.loads(written["audit_trail"].read_text(encoding="utf-8")) == {}
    assert json.loads(written["exceptions"].read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------
# 18. the exception register in the decision package (Step 12B)
# ---------------------------------------------------------------------------
def _register_of(written: dict) -> dict:
    return json.loads(written["exception_register"].read_text(encoding="utf-8"))


def test_clean_register_exists_and_is_empty_without_changing_the_close(tmp_path: Path):
    _require(RAW / "invoices.csv")
    result = run_close(config_for(tmp_path))
    register = _register_of(result.written)

    assert result.written["exception_register"].name == "exception_register.json"
    assert register["exception_count"] == 0
    assert register["exceptions"] == []
    assert register["dataset_label"] == "clean"
    assert result.close_status == "PASS"
    assert result.report_allowed is True


def test_e4_register_holds_exactly_the_two_fx_exceptions(tmp_path: Path):
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4")
    )
    register = _register_of(result.written)

    assert register["exception_count"] == 2
    assert [item["exception_id"] for item in register["exceptions"]] == [
        "EXC-FXC-03-E4",
        "EXC-FXC-05-E4",
    ]
    # opening an investigation record does not soften the verdict
    assert result.close_status == "FAIL"
    assert result.report_allowed is False
    assert result.exit_code == EXIT_BLOCKED


def test_e1_to_e3_register_holds_the_three_matching_exceptions(tmp_path: Path):
    _require(ERR / "invoices.csv")
    result = run_close(
        config_for(tmp_path, data_dir=ERR, fx_rates=RAW / "fx_rates.csv",
                   dataset_label="E1-E3")
    )
    register = _register_of(result.written)

    assert [item["exception_id"] for item in register["exceptions"]] == [
        "EXC-MAT-03-E2",
        "EXC-MAT-04-E1",
        "EXC-MAT-06-E3",
    ]
    assert result.close_status == "FAIL"
    assert result.report_allowed is False


def test_every_register_entry_opens_as_open_and_unowned(tmp_path: Path):
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4")
    )
    for entry in _register_of(result.written)["exceptions"]:
        assert entry["status"] == "OPEN"
        assert entry["owner"] is None
        assert entry["resolution"] is None


def test_the_register_is_built_from_the_same_records_audit_produced(tmp_path: Path):
    """One source of truth: the register opens records, it does not find them."""
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"),
                   dataset_label="E4", write_package=False)
    )
    registered = [entry["source_exception"] for entry in result.exception_register["exceptions"]]
    for source in registered:
        assert source in result.exception_records
    assert len(registered) == len(result.exception_records)


def test_the_result_object_exposes_the_register_that_was_written(tmp_path: Path):
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"), dataset_label="E4")
    )
    assert result.exception_register == _register_of(result.written)
    assert result.model.exception_register == result.exception_register


# ---------------------------------------------------------------------------
# 19. resolution isolation: the close decision stays authoritative
# ---------------------------------------------------------------------------
def test_resolving_every_exception_leaves_the_close_decision_untouched(tmp_path: Path):
    """The point of the whole layer.

    An investigator marks both E4 exceptions RESOLVED in memory. The close must
    still be FAIL, the report still blocked, and every control result identical:
    resolution records what a person did about a failure, never that it passed.
    """
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"),
                   dataset_label="E4", write_package=False)
    )
    decision_before = copy.deepcopy(result.decision)
    controls_before = result.control_results.copy()
    audit_before = copy.deepcopy(result.audit_trail)
    register_before = copy.deepcopy(result.exception_register)

    register = result.exception_register
    for entry in register_before["exceptions"]:
        register = assign_owner(register, entry["exception_id"], "finance.manager")
        register = transition_exception(
            register, entry["exception_id"], INVESTIGATING, comment="Reviewing."
        )
        register = transition_exception(
            register, entry["exception_id"], RESOLVED,
            comment="Investigated and explained.",
            evidence=[{"type": "control_result", "reference": entry["control_id"]}],
        )

    # every exception now says RESOLVED
    assert all(entry["status"] == RESOLVED for entry in register["exceptions"])

    # and none of that reached the close
    assert result.decision == decision_before
    assert result.decision["close_status"] == "FAIL"
    assert result.decision["report_allowed"] is False
    assert result.decision["blocking_controls"] == ["FXC-03", "FXC-05"]
    pd.testing.assert_frame_equal(result.control_results, controls_before)
    assert result.audit_trail == audit_before
    # the workflow is pure, so the register it was given is also unchanged
    assert result.exception_register == register_before


def test_a_resolved_register_cannot_change_a_written_package(tmp_path: Path):
    """Writing a package from a fully resolved register must not approve it."""
    result = run_close(
        config_for(tmp_path, fx_rates=_require(ERR / "fx_rates.csv"),
                   dataset_label="E4", write_package=False)
    )
    register = result.exception_register
    for entry in result.exception_register["exceptions"]:
        register = transition_exception(
            register, entry["exception_id"], INVESTIGATING, comment="Reviewing."
        )
        register = transition_exception(
            register, entry["exception_id"], RESOLVED, comment="Explained.",
            evidence=[{"type": "control_result", "reference": entry["control_id"]}],
        )

    resolved_model = replace(result.model, exception_register=register)
    written = write_decision_package(resolved_model, tmp_path / "resolved")

    decision = json.loads(written["close_decision"].read_text(encoding="utf-8"))
    assert decision["close_status"] == "FAIL"
    assert decision["report_allowed"] is False
    assert written["report_pdf"].name.startswith("close_exception_report_")
    assert json.loads(written["audit_trail"].read_text(encoding="utf-8"))[
        "report_allowed"
    ] is False


# ---------------------------------------------------------------------------
# 20. manifest and determinism for the register
# ---------------------------------------------------------------------------
def test_manifest_hashes_the_exception_register(tmp_path: Path):
    _require(RAW / "invoices.csv")
    result = run_close(config_for(tmp_path))
    manifest = json.loads(result.written["package_manifest"].read_text(encoding="utf-8"))
    by_role = {item["role"]: item for item in manifest["outputs"]}

    assert "exception_register" in by_role
    assert by_role["exception_register"]["sha256"] == sha256_of(
        result.written["exception_register"]
    )


def test_tampering_with_the_register_changes_its_recorded_hash(tmp_path: Path):
    _require(ERR / "fx_rates.csv")
    baseline = run_close(
        config_for(tmp_path, fx_rates=ERR / "fx_rates.csv", dataset_label="E4",
                   write_package=False)
    )
    tampered_register = copy.deepcopy(baseline.exception_register)
    tampered_register["exceptions"][0]["owner"] = "someone.else"
    tampered_model = replace(baseline.model, exception_register=tampered_register)

    original = write_decision_package(baseline.model, tmp_path / "original")
    tampered = write_decision_package(tampered_model, tmp_path / "tampered")

    def recorded(paths, role):
        manifest = json.loads(paths["package_manifest"].read_text(encoding="utf-8"))
        return {item["role"]: item["sha256"] for item in manifest["outputs"]}[role]

    assert recorded(original, "exception_register") != recorded(tampered, "exception_register")
    assert recorded(tampered, "exception_register") == sha256_of(tampered["exception_register"])
    # the rest of the package is untouched by a register-only change
    for role in ("control_results", "close_decision", "audit_trail", "exceptions"):
        assert recorded(original, role) == recorded(tampered, role), role


def test_the_register_is_byte_identical_across_two_runs(tmp_path: Path):
    _require(ERR / "fx_rates.csv")
    common = dict(fx_rates=ERR / "fx_rates.csv", dataset_label="E4")
    first = run_close(config_for(tmp_path, out_dir=tmp_path / "first", **common)).written
    second = run_close(config_for(tmp_path, out_dir=tmp_path / "second", **common)).written
    assert sha256_of(first["exception_register"]) == sha256_of(second["exception_register"])


def test_the_register_json_is_written_with_sorted_keys(tmp_path: Path):
    _require(ERR / "fx_rates.csv")
    result = run_close(
        config_for(tmp_path, fx_rates=ERR / "fx_rates.csv", dataset_label="E4")
    )
    text = result.written["exception_register"].read_text(encoding="utf-8")
    top_level = [line.split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert top_level == sorted(top_level)
    assert text.startswith("{\n  ")


def test_report_does_not_construct_the_register_itself():
    """The writer serialises the register; it must not build or reshape one."""
    import report

    source = ROOT / "src" / "report.py"
    assert "exception_workflow" not in _imported_modules(source)

    tree = ast.parse(source.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_exception_register" not in called
    assert not hasattr(report, "build_exception_register")


def test_a_model_without_a_register_writes_an_empty_one(tmp_path: Path):
    _require(RAW / "invoices.csv")
    baseline = run_close(config_for(tmp_path, write_package=False))
    bare = replace(baseline.model, exception_register={})
    written = write_decision_package(bare, tmp_path / "bare")
    assert json.loads(written["exception_register"].read_text(encoding="utf-8")) == {}

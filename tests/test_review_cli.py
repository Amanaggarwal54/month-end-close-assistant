"""Tests for the reviewer command line.

Every test drives ``review_cli.main`` the way a person would, and asserts on the
exit code and the captured output rather than on internals. The load-bearing
test is ``test_the_close_package_is_byte_identical_after_a_full_cli_investigation``:
if the CLI ever reaches into the immutable package, that is what notices.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_cli  # noqa: E402
from exception_workflow import INVESTIGATING, OPEN, RESOLVED  # noqa: E402
from pipeline import CloseRunConfig, run_close  # noqa: E402
from report import sha256_of  # noqa: E402
from review_cli import (  # noqa: E402
    EXIT_INTEGRITY,
    EXIT_LIFECYCLE,
    EXIT_OK,
    EXIT_PROTECTED,
    EXIT_USAGE,
    main,
)
from review_workspace import (  # noqa: E402
    MANIFEST_FILENAME,
    REGISTER_FILENAME,
    load_workspace,
    workspace_path_for,
)

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"

OWNED_AT = "2026-04-01T09:00:00Z"
STARTED_AT = "2026-04-01T09:30:00Z"
RESOLVED_AT = "2026-04-02T14:15:00Z"

FX_ID = "EXC-FXC-03-E4"
OTHER_ID = "EXC-FXC-05-E4"


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return path


def make_package(tmp_path: Path, label: str = "E4", **overrides) -> Path:
    """Run the real pipeline and return the immutable close package directory."""
    _require(RAW / "invoices.csv")
    result = run_close(
        CloseRunConfig(
            data_dir=RAW,
            out_dir=tmp_path / "out",
            run_timestamp=FIXED_TIMESTAMP,
            dataset_label=label,
            **overrides,
        )
    )
    return result.written["report_pdf"].parent


def e4_package(tmp_path: Path) -> Path:
    return make_package(tmp_path, label="E4", fx_rates=_require(ERR / "fx_rates.csv"))


def run_cli(*argv: str) -> tuple[int, str]:
    """Run one command, returning (exit_code, captured output)."""
    buffer = io.StringIO()
    code = main(list(argv), out=buffer)
    return code, buffer.getvalue()


def hash_package(package_dir: Path) -> dict[str, str]:
    return {path.name: sha256_of(path) for path in sorted(package_dir.iterdir())}


def create_workspace(package: Path, review: Path) -> tuple[int, str]:
    return run_cli("create", "--package-dir", str(package), "--review-dir", str(review))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def test_create_builds_a_workspace_and_reports_its_provenance(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"

    code, output = create_workspace(package, review)

    assert code == EXIT_OK
    written = workspace_path_for(review, "E4")
    assert written.exists()
    assert str(written) in output
    assert sha256_of(package / REGISTER_FILENAME) in output
    assert sha256_of(package / MANIFEST_FILENAME) in output
    assert "exceptions          2" in output


def test_create_from_a_clean_package_reports_no_exceptions(tmp_path: Path):
    package = make_package(tmp_path, label="clean")
    review = tmp_path / "review"

    code, output = create_workspace(package, review)

    assert code == EXIT_OK
    assert "exceptions          0" in output
    workspace = load_workspace(review, "clean")
    assert workspace["exceptions"] == []
    assert workspace["exception_count"] == 0


def test_create_refuses_to_silently_discard_an_existing_investigation(tmp_path: Path):
    """Re-running create would rebuild every entry from the package.

    Any owner assigned or status reached in the workspace would be lost, so the
    second run has to be asked for explicitly.
    """
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)

    code, output = create_workspace(package, review)

    assert code == EXIT_USAGE
    assert "--force" in output
    assert load_workspace(review, "E4")["exceptions"][0]["owner"] == "finance.manager"


def test_create_with_force_rebuilds_from_the_package(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)

    code, _ = run_cli("create", "--package-dir", str(package),
                      "--review-dir", str(review), "--force")

    assert code == EXIT_OK
    assert load_workspace(review, "E4")["exceptions"][0]["owner"] is None


def test_create_refuses_a_tampered_package(tmp_path: Path):
    package = e4_package(tmp_path)
    register = json.loads((package / REGISTER_FILENAME).read_text(encoding="utf-8"))
    register["exceptions"][0]["status"] = RESOLVED
    (package / REGISTER_FILENAME).write_text(
        json.dumps(register, indent=2, sort_keys=True), encoding="utf-8"
    )

    code, output = create_workspace(package, tmp_path / "review")

    assert code == EXIT_INTEGRITY
    assert "integrity" in output.lower()
    assert not workspace_path_for(tmp_path / "review", "E4").exists()


def test_create_refuses_a_package_directory_that_does_not_exist(tmp_path: Path):
    code, output = create_workspace(tmp_path / "nowhere", tmp_path / "review")
    assert code == EXIT_USAGE
    assert "not found" in output.lower()


def test_create_refuses_to_write_inside_the_close_package(tmp_path: Path):
    package = e4_package(tmp_path)
    code, output = run_cli("create", "--package-dir", str(package),
                           "--review-dir", str(package))

    assert code == EXIT_PROTECTED
    assert "immutable close package" in output
    assert not (package / "e4").exists()


# ---------------------------------------------------------------------------
# list and show
# ---------------------------------------------------------------------------
def test_list_shows_every_exception_with_its_state(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("list", "--dataset-label", "E4", "--review-dir", str(review))

    assert code == EXIT_OK
    assert FX_ID in output and OTHER_ID in output
    assert "FAIL" in output and "report_allowed=false" in output
    assert output.count("OPEN") == 2
    # deterministic order, matching the register
    assert output.index(FX_ID) < output.index(OTHER_ID)


def test_list_output_is_deterministic(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    first = run_cli("list", "--dataset-label", "E4", "--review-dir", str(review))
    second = run_cli("list", "--dataset-label", "E4", "--review-dir", str(review))
    assert first == second


def test_list_on_a_clean_workspace_says_so_plainly(tmp_path: Path):
    package = make_package(tmp_path, label="clean")
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("list", "--dataset-label", "clean",
                           "--review-dir", str(review))

    assert code == EXIT_OK
    assert "Exceptions:  0" in output
    assert "No exceptions were raised" in output


def test_list_without_a_workspace_fails_usefully(tmp_path: Path):
    code, output = run_cli("list", "--dataset-label", "E4",
                           "--review-dir", str(tmp_path / "review"))
    assert code == EXIT_USAGE
    assert "no review workspace" in output.lower()


def test_show_prints_the_complete_record(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("show", "--dataset-label", "E4", "--review-dir", str(review),
                           "--exception-id", FX_ID)

    assert code == EXIT_OK
    record = json.loads(output)
    assert record["exception_id"] == FX_ID
    assert record["status"] == OPEN
    for field in ("source_exception", "history", "details", "evidence",
                  "resolution", "control_id", "severity"):
        assert field in record, field
    assert record["history"][0]["occurred_at"] == FIXED_TIMESTAMP


def test_show_rejects_an_unknown_exception_id(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("show", "--dataset-label", "E4", "--review-dir", str(review),
                           "--exception-id", "EXC-NOT-REAL")

    assert code == EXIT_USAGE
    assert "EXC-NOT-REAL" in output


# ---------------------------------------------------------------------------
# assign
# ---------------------------------------------------------------------------
def test_assign_sets_the_owner_and_persists_it(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                           "--exception-id", FX_ID, "--owner", "finance.manager",
                           "--occurred-at", OWNED_AT)

    assert code == EXIT_OK
    assert "OWNER_CHANGED" in output

    entry = load_workspace(review, "E4")["exceptions"][0]
    assert entry["owner"] == "finance.manager"
    assert entry["status"] == OPEN, "assigning an owner must not move the status"
    assert entry["history"][-1]["occurred_at"] == OWNED_AT


def test_assign_can_clear_the_owner(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)

    code, _ = run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                      "--exception-id", FX_ID, "--clear-owner",
                      "--occurred-at", STARTED_AT)

    assert code == EXIT_OK
    assert load_workspace(review, "E4")["exceptions"][0]["owner"] is None


def test_assign_requires_a_caller_supplied_timestamp(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    with pytest.raises(SystemExit) as exit_info:
        run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                "--exception-id", FX_ID, "--owner", "finance.manager")

    assert exit_info.value.code != 0


def test_assign_requires_an_owner_or_an_explicit_clear(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    with pytest.raises(SystemExit):
        run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                "--exception-id", FX_ID, "--occurred-at", OWNED_AT)


# ---------------------------------------------------------------------------
# transition and resolve
# ---------------------------------------------------------------------------
def test_transition_moves_an_exception_and_records_when(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("transition", "--dataset-label", "E4",
                           "--review-dir", str(review), "--exception-id", FX_ID,
                           "--status", INVESTIGATING, "--comment", "Reviewing.",
                           "--occurred-at", STARTED_AT)

    assert code == EXIT_OK
    assert "STATUS_CHANGED" in output

    entry = load_workspace(review, "E4")["exceptions"][0]
    assert entry["status"] == INVESTIGATING
    assert entry["history"][-1]["occurred_at"] == STARTED_AT


def test_resolve_records_the_comment_and_every_evidence_reference(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)

    code, _ = run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
                      "--exception-id", FX_ID,
                      "--comment", "Confirmed the incorrect February rate.",
                      "--evidence", "source_file:data/raw/fx_rates_expected.csv",
                      "--evidence", "control_result:FXC-03",
                      "--occurred-at", RESOLVED_AT)

    assert code == EXIT_OK
    entry = load_workspace(review, "E4")["exceptions"][0]
    assert entry["status"] == RESOLVED
    assert entry["resolution"]["comment"] == "Confirmed the incorrect February rate."
    assert entry["resolution"]["evidence"] == [
        {"type": "source_file", "reference": "data/raw/fx_rates_expected.csv"},
        {"type": "control_result", "reference": "FXC-03"},
    ]


def test_an_evidence_reference_may_contain_colons(tmp_path: Path):
    """Only the first colon separates the type from the reference."""
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)

    code, _ = run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
                      "--exception-id", FX_ID, "--comment", "Explained.",
                      "--evidence", "note:seen at 2026-04-02T14:15:00Z",
                      "--occurred-at", RESOLVED_AT)

    assert code == EXIT_OK
    entry = load_workspace(review, "E4")["exceptions"][0]
    assert entry["resolution"]["evidence"] == [
        {"type": "note", "reference": "seen at 2026-04-02T14:15:00Z"}
    ]


def test_malformed_evidence_is_rejected_by_the_parser(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    with pytest.raises(SystemExit):
        run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
                "--exception-id", FX_ID, "--comment", "Explained.",
                "--evidence", "no-separator", "--occurred-at", RESOLVED_AT)


# ---------------------------------------------------------------------------
# the lifecycle rules still come from exception_workflow.py
# ---------------------------------------------------------------------------
def test_the_cli_cannot_skip_a_lifecycle_step(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    code, output = run_cli("resolve", "--dataset-label", "E4",
                           "--review-dir", str(review), "--exception-id", FX_ID,
                           "--comment", "Skipping ahead.",
                           "--evidence", "control_result:FXC-03",
                           "--occurred-at", RESOLVED_AT)

    assert code == EXIT_LIFECYCLE
    assert "Invalid transition" in output
    assert load_workspace(review, "E4")["exceptions"][0]["status"] == OPEN


def test_the_cli_cannot_resolve_without_evidence(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)

    code, output = run_cli("transition", "--dataset-label", "E4",
                           "--review-dir", str(review), "--exception-id", FX_ID,
                           "--status", RESOLVED, "--comment", "No evidence.",
                           "--occurred-at", RESOLVED_AT)

    assert code == EXIT_LIFECYCLE
    assert "evidence reference" in output
    assert load_workspace(review, "E4")["exceptions"][0]["status"] == INVESTIGATING


def test_the_cli_cannot_reopen_a_resolved_exception(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
    run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--comment", "Explained.",
            "--evidence", "control_result:FXC-03", "--occurred-at", RESOLVED_AT)

    code, output = run_cli("transition", "--dataset-label", "E4",
                           "--review-dir", str(review), "--exception-id", FX_ID,
                           "--status", OPEN, "--comment", "Reopening.",
                           "--occurred-at", "2026-04-03T08:00:00Z")

    assert code == EXIT_LIFECYCLE
    assert "cannot leave RESOLVED" in output
    assert load_workspace(review, "E4")["exceptions"][0]["status"] == RESOLVED


def test_the_cli_defines_no_lifecycle_rules_of_its_own():
    """Statuses and transitions must exist in exactly one module."""
    source = ROOT / "src" / "review_cli.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for forbidden in ("OPEN", "INVESTIGATING", "RESOLVED",
                      "VALID_STATUSES", "_ALLOWED_TRANSITIONS"):
        assert forbidden not in assigned, f"{forbidden} must not be redefined here"

    text = source.read_text(encoding="utf-8")
    for token in ("datetime", "time.time", "now(", "utcnow", "uuid", "random"):
        assert token not in text, f"{token!r} must not appear in the CLI"

    # no analytics dependencies in a reviewer tool
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "pandas" not in imported
    assert "numpy" not in imported


# ---------------------------------------------------------------------------
# the immutable package
# ---------------------------------------------------------------------------
def test_the_close_package_is_byte_identical_after_a_full_cli_investigation(
    tmp_path: Path,
):
    """Every command, run for real, then the whole package re-hashed."""
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    before = hash_package(package)
    assert len(before) == 7

    assert create_workspace(package, review)[0] == EXIT_OK

    for exception_id, control in ((FX_ID, "FXC-03"), (OTHER_ID, "FXC-05")):
        assert run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                       "--exception-id", exception_id, "--owner", "finance.manager",
                       "--occurred-at", OWNED_AT)[0] == EXIT_OK
        assert run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
                       "--exception-id", exception_id, "--status", INVESTIGATING,
                       "--comment", "Reviewing.", "--occurred-at", STARTED_AT)[0] == EXIT_OK
        assert run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
                       "--exception-id", exception_id, "--comment", "Explained.",
                       "--evidence", f"control_result:{control}",
                       "--occurred-at", RESOLVED_AT)[0] == EXIT_OK

    workspace = load_workspace(review, "E4")
    assert all(entry["status"] == RESOLVED for entry in workspace["exceptions"])

    assert hash_package(package) == before


def test_resolving_everything_does_not_change_the_close_decision(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
    run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--comment", "Explained.",
            "--evidence", "control_result:FXC-03", "--occurred-at", RESOLVED_AT)

    decision = json.loads((package / "close_decision.json").read_text(encoding="utf-8"))
    assert decision["close_status"] == "FAIL"
    assert decision["report_allowed"] is False

    register = json.loads((package / REGISTER_FILENAME).read_text(encoding="utf-8"))
    assert all(entry["status"] == OPEN for entry in register["exceptions"])


# ---------------------------------------------------------------------------
# persistence and determinism
# ---------------------------------------------------------------------------
def test_each_mutating_command_applies_exactly_one_event(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    lengths = [len(load_workspace(review, "E4")["exceptions"][0]["history"])]
    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)
    lengths.append(len(load_workspace(review, "E4")["exceptions"][0]["history"]))
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
    lengths.append(len(load_workspace(review, "E4")["exceptions"][0]["history"]))

    assert lengths == [1, 2, 3]


def test_the_workspace_file_stays_deterministic_across_commands(tmp_path: Path):
    package = e4_package(tmp_path)

    def investigate(review: Path) -> Path:
        create_workspace(package, review)
        run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
                "--exception-id", FX_ID, "--owner", "finance.manager",
                "--occurred-at", OWNED_AT)
        run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
                "--exception-id", FX_ID, "--status", INVESTIGATING,
                "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
        return workspace_path_for(review, "E4")

    first = investigate(tmp_path / "a")
    second = investigate(tmp_path / "b")
    assert sha256_of(first) == sha256_of(second)

    text = first.read_text(encoding="utf-8")
    assert text.startswith("{\n  ")
    top_level = [line.split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert top_level == sorted(top_level)


def test_provenance_survives_every_command(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)
    original = load_workspace(review, "E4")["source_package"]

    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
    run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--comment", "Explained.",
            "--evidence", "control_result:FXC-03", "--occurred-at", RESOLVED_AT)

    assert load_workspace(review, "E4")["source_package"] == original


def test_timestamps_are_stored_exactly_as_supplied(tmp_path: Path):
    """No parsing, no normalising: whatever the caller wrote is the record."""
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    odd = "02/04/2026 14:15  (Europe/Brussels)"
    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", odd)

    entry = load_workspace(review, "E4")["exceptions"][0]
    assert entry["history"][-1]["occurred_at"] == odd


def test_a_full_cli_history_reads_in_order(tmp_path: Path):
    package = e4_package(tmp_path)
    review = tmp_path / "review"
    create_workspace(package, review)

    run_cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--owner", "finance.manager",
            "--occurred-at", OWNED_AT)
    run_cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--status", INVESTIGATING,
            "--comment", "Reviewing.", "--occurred-at", STARTED_AT)
    run_cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
            "--exception-id", FX_ID, "--comment", "Explained.",
            "--evidence", "control_result:FXC-03", "--occurred-at", RESOLVED_AT)

    entry = load_workspace(review, "E4")["exceptions"][0]
    assert [
        (item["action"], item["occurred_at"]) for item in entry["history"]
    ] == [
        ("CREATED", FIXED_TIMESTAMP),
        ("OWNER_CHANGED", OWNED_AT),
        ("STATUS_CHANGED", STARTED_AT),
        ("STATUS_CHANGED", RESOLVED_AT),
    ]


def test_an_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        run_cli("demolish", "--dataset-label", "E4")


def test_every_documented_command_is_wired_up():
    parser = review_cli.build_parser()
    actions = [
        action for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    ]
    assert actions, "the CLI must expose subcommands"
    assert set(actions[0].choices) == {
        "create", "list", "show", "assign", "transition", "resolve"
    }

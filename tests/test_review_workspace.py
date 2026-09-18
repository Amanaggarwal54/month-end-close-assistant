"""Tests for the post-close review workspace.

The load-bearing test here is
``test_resolving_in_the_workspace_leaves_every_package_artefact_byte_identical``:
it hashes the whole close package, runs a full investigation to RESOLVED, and
hashes it again. If the separation between the immutable package and the mutable
review record ever breaks, that test is what notices.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_workspace  # noqa: E402
from exception_workflow import INVESTIGATING, OPEN, RESOLVED  # noqa: E402
from pipeline import CloseRunConfig, run_close  # noqa: E402
from report import sha256_of  # noqa: E402
from review_workspace import (  # noqa: E402
    MANIFEST_FILENAME,
    REGISTER_FILENAME,
    WORKSPACE_FILENAME,
    WORKSPACE_VERSION,
    PackageIntegrityError,
    WorkspaceWriteError,
    create_resolution_workspace,
    get_review_exception,
    load_workspace,
    review_assign_owner,
    review_transition_exception,
    validate_workspace,
    workspace_path_for,
    write_workspace,
)

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"

OWNED_AT = "2026-04-01T09:00:00Z"
STARTED_AT = "2026-04-01T09:30:00Z"
RESOLVED_AT = "2026-04-02T14:15:00Z"


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return path


def make_package(tmp_path: Path, label: str = "clean", **overrides) -> Path:
    """Run the real pipeline and return the close package directory."""
    _require(RAW / "invoices.csv")
    config = CloseRunConfig(
        data_dir=RAW,
        out_dir=tmp_path / "out",
        run_timestamp=FIXED_TIMESTAMP,
        dataset_label=label,
        **overrides,
    )
    result = run_close(config)
    return result.written["report_pdf"].parent


def e4_package(tmp_path: Path) -> Path:
    return make_package(tmp_path, label="E4", fx_rates=_require(ERR / "fx_rates.csv"))


def hash_package(package_dir: Path) -> dict[str, str]:
    return {path.name: sha256_of(path) for path in sorted(package_dir.iterdir())}


def resolve_everything(workspace: dict) -> dict:
    """Take every exception in a workspace all the way to RESOLVED."""
    for entry in list(workspace["exceptions"]):
        exception_id = entry["exception_id"]
        workspace = review_assign_owner(
            workspace, exception_id, "finance.manager", occurred_at=OWNED_AT
        )
        workspace = review_transition_exception(
            workspace, exception_id, INVESTIGATING,
            comment="Reviewing the finding.", occurred_at=STARTED_AT,
        )
        workspace = review_transition_exception(
            workspace, exception_id, RESOLVED,
            comment="Investigated and explained.",
            evidence=[{"type": "control_result", "reference": entry["control_id"]}],
            occurred_at=RESOLVED_AT,
        )
    return workspace


# ---------------------------------------------------------------------------
# the immutability test
# ---------------------------------------------------------------------------
def test_resolving_in_the_workspace_leaves_every_package_artefact_byte_identical(
    tmp_path: Path,
):
    """The whole point of Step 12D, in one test.

    A full investigation runs to RESOLVED and is written to disk. Every file in
    the close package must hash exactly as it did before.
    """
    package = e4_package(tmp_path)
    before = hash_package(package)
    assert len(before) == 7, "expected the full close package"

    workspace = create_resolution_workspace(package, tmp_path / "review")
    workspace = resolve_everything(workspace)
    written = write_workspace(workspace, tmp_path / "review")

    assert written.exists()
    assert all(
        entry["status"] == RESOLVED for entry in workspace["exceptions"]
    ), "the investigation must actually have concluded"

    after = hash_package(package)
    assert after == before
    for name in before:
        assert after[name] == before[name], name


def test_the_workspace_never_lands_inside_the_close_package(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    with pytest.raises(WorkspaceWriteError, match="immutable close package"):
        write_workspace(workspace, package)

    with pytest.raises(WorkspaceWriteError, match="immutable close package"):
        write_workspace(workspace, package.parent / package.name / "nested")

    assert not (package / WORKSPACE_FILENAME).exists()


def test_the_workspace_is_written_outside_the_package(tmp_path: Path):
    package = e4_package(tmp_path)
    create_resolution_workspace(package, tmp_path / "review")

    expected = workspace_path_for(tmp_path / "review", "E4")
    assert expected.exists()
    assert expected.name == WORKSPACE_FILENAME
    assert "out" not in expected.relative_to(tmp_path).parts


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
def test_clean_package_produces_a_workspace_with_no_exceptions(tmp_path: Path):
    package = make_package(tmp_path, label="clean")
    workspace = create_resolution_workspace(package, tmp_path / "review")

    assert workspace["workspace_version"] == WORKSPACE_VERSION
    assert workspace["dataset_label"] == "clean"
    assert workspace["exception_count"] == 0
    assert workspace["exceptions"] == []


def test_e4_package_produces_a_workspace_with_both_fx_exceptions(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review")

    assert [entry["exception_id"] for entry in workspace["exceptions"]] == [
        "EXC-FXC-03-E4",
        "EXC-FXC-05-E4",
    ]
    assert workspace["exception_count"] == 2
    for entry in workspace["exceptions"]:
        assert entry["status"] == OPEN
        assert entry["owner"] is None


def test_the_workspace_records_which_package_it_came_from(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review")
    source = workspace["source_package"]

    assert source["exception_register_sha256"] == sha256_of(package / REGISTER_FILENAME)
    assert source["package_manifest_sha256"] == sha256_of(package / MANIFEST_FILENAME)
    assert source["package_path"]
    assert Path(source["package_path"]).name == package.name


def test_workspace_entries_start_as_exact_copies_of_the_register(tmp_path: Path):
    package = e4_package(tmp_path)
    register = json.loads((package / REGISTER_FILENAME).read_text(encoding="utf-8"))
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    assert workspace["exceptions"] == register["exceptions"]


def test_the_creation_timestamp_from_the_close_survives_into_the_workspace(
    tmp_path: Path,
):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review")

    for entry in workspace["exceptions"]:
        created = entry["history"][0]
        assert created["action"] == "CREATED"
        assert created["occurred_at"] == FIXED_TIMESTAMP


# ---------------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------------
def test_a_tampered_exception_register_is_refused(tmp_path: Path):
    package = e4_package(tmp_path)
    register = json.loads((package / REGISTER_FILENAME).read_text(encoding="utf-8"))
    register["exceptions"][0]["status"] = RESOLVED
    (package / REGISTER_FILENAME).write_text(
        json.dumps(register, indent=2, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(PackageIntegrityError, match="exception_register"):
        create_resolution_workspace(package, tmp_path / "review")

    assert not workspace_path_for(tmp_path / "review", "E4").exists()


def test_a_tampered_manifest_hash_is_refused(tmp_path: Path):
    package = e4_package(tmp_path)
    manifest = json.loads((package / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    for entry in manifest["outputs"]:
        if entry["role"] == "exception_register":
            entry["sha256"] = "0" * 64
    (package / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(PackageIntegrityError, match="does not match its manifest"):
        create_resolution_workspace(package, tmp_path / "review")


def test_tampering_with_any_package_artefact_is_refused(tmp_path: Path):
    """Integrity covers the whole package, not only the register.

    A package whose control results were edited is not one a reviewer should
    build on, even though the register itself still matches.
    """
    package = e4_package(tmp_path)
    control_results = package / "control_results.csv"
    control_results.write_text(
        control_results.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )

    with pytest.raises(PackageIntegrityError, match="control_results"):
        create_resolution_workspace(package, tmp_path / "review")


def test_a_missing_package_artefact_is_refused(tmp_path: Path):
    package = e4_package(tmp_path)
    (package / "exceptions.json").unlink()

    with pytest.raises(PackageIntegrityError, match="exceptions"):
        create_resolution_workspace(package, tmp_path / "review")


def test_a_package_with_no_manifest_is_refused(tmp_path: Path):
    package = e4_package(tmp_path)
    (package / MANIFEST_FILENAME).unlink()

    with pytest.raises(FileNotFoundError, match=MANIFEST_FILENAME):
        create_resolution_workspace(package, tmp_path / "review")


def test_an_untouched_package_verifies_cleanly(tmp_path: Path):
    package = e4_package(tmp_path)
    manifest = review_workspace.verify_package_integrity(package)
    assert manifest["close_status"] == "FAIL"


# ---------------------------------------------------------------------------
# the review workspace cannot reach the close decision
# ---------------------------------------------------------------------------
def test_a_resolved_workspace_does_not_change_the_close_decision(tmp_path: Path):
    """FAIL stays FAIL. Investigation records what was done, not a new verdict."""
    package = e4_package(tmp_path)
    decision_before = (package / "close_decision.json").read_text(encoding="utf-8")
    audit_before = (package / "audit_trail.json").read_text(encoding="utf-8")
    controls_before = (package / "control_results.csv").read_text(encoding="utf-8")
    register_before = (package / REGISTER_FILENAME).read_text(encoding="utf-8")

    workspace = create_resolution_workspace(package, tmp_path / "review")
    workspace = resolve_everything(workspace)
    write_workspace(workspace, tmp_path / "review")

    assert (package / "close_decision.json").read_text(encoding="utf-8") == decision_before
    assert (package / "audit_trail.json").read_text(encoding="utf-8") == audit_before
    assert (package / "control_results.csv").read_text(encoding="utf-8") == controls_before
    assert (package / REGISTER_FILENAME).read_text(encoding="utf-8") == register_before

    decision = json.loads(decision_before)
    assert decision["close_status"] == "FAIL"
    assert decision["report_allowed"] is False


def test_the_immutable_register_still_shows_open_after_the_workspace_resolves(
    tmp_path: Path,
):
    """The snapshot keeps saying what was true at close time."""
    package = e4_package(tmp_path)
    workspace = resolve_everything(
        create_resolution_workspace(package, tmp_path / "review")
    )
    write_workspace(workspace, tmp_path / "review")

    register = json.loads((package / REGISTER_FILENAME).read_text(encoding="utf-8"))
    assert all(entry["status"] == OPEN for entry in register["exceptions"])
    assert all(entry["status"] == RESOLVED for entry in workspace["exceptions"])


def test_the_pipeline_never_reads_a_review_workspace():
    """Truth flows package -> workspace, never back."""
    source = ROOT / "src" / "pipeline.py"
    text = source.read_text(encoding="utf-8")
    assert "review_workspace" not in text
    assert WORKSPACE_FILENAME not in text

    report_text = (ROOT / "src" / "report.py").read_text(encoding="utf-8")
    assert "review_workspace" not in report_text


# ---------------------------------------------------------------------------
# operations are delegated, not reimplemented
# ---------------------------------------------------------------------------
def test_the_workspace_module_defines_no_lifecycle_rules_of_its_own():
    """Transitions belong to exception_workflow.py and must live in one place."""
    source = ROOT / "src" / "review_workspace.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for forbidden in ("VALID_STATUSES", "_ALLOWED_TRANSITIONS", "OPEN",
                      "INVESTIGATING", "RESOLVED"):
        assert forbidden not in assigned, f"{forbidden} must not be redefined here"

    imported_from_workflow = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "exception_workflow"
        for alias in node.names
    }
    assert {"assign_owner", "transition_exception"} <= imported_from_workflow


def test_workspace_transitions_enforce_the_workflow_rules(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    with pytest.raises(ValueError, match="Invalid transition"):
        review_transition_exception(
            workspace, "EXC-FXC-03-E4", RESOLVED, comment="Skipping ahead.",
            evidence=[{"type": "control_result", "reference": "FXC-03"}],
        )

    investigating = review_transition_exception(
        workspace, "EXC-FXC-03-E4", INVESTIGATING, comment="Started."
    )
    with pytest.raises(ValueError, match="resolution comment"):
        review_transition_exception(
            investigating, "EXC-FXC-03-E4", RESOLVED,
            evidence=[{"type": "control_result", "reference": "FXC-03"}],
        )
    with pytest.raises(ValueError, match="evidence reference"):
        review_transition_exception(
            investigating, "EXC-FXC-03-E4", RESOLVED, comment="No evidence."
        )


def test_a_resolved_exception_stays_terminal_in_the_workspace(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    workspace = review_transition_exception(
        workspace, "EXC-FXC-03-E4", INVESTIGATING, comment="Started.",
        occurred_at=STARTED_AT,
    )
    workspace = review_transition_exception(
        workspace, "EXC-FXC-03-E4", RESOLVED, comment="Explained.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at=RESOLVED_AT,
    )

    with pytest.raises(ValueError, match="cannot leave RESOLVED"):
        review_transition_exception(
            workspace, "EXC-FXC-03-E4", OPEN, comment="Reopening."
        )

    # and it is still terminal after a round trip through disk
    write_workspace(workspace, tmp_path / "review")
    reloaded = load_workspace(tmp_path / "review", "E4")
    with pytest.raises(ValueError, match="cannot leave RESOLVED"):
        review_transition_exception(
            reloaded, "EXC-FXC-03-E4", OPEN, comment="Reopening."
        )


def test_review_operations_do_not_mutate_the_workspace_they_are_given(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)
    original = json.dumps(workspace, sort_keys=True)

    review_assign_owner(workspace, "EXC-FXC-03-E4", "finance.manager")
    review_transition_exception(
        workspace, "EXC-FXC-03-E4", INVESTIGATING, comment="Started."
    )

    assert json.dumps(workspace, sort_keys=True) == original


# ---------------------------------------------------------------------------
# persistence, determinism and timestamps
# ---------------------------------------------------------------------------
def test_two_identical_writes_are_byte_identical(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = resolve_everything(
        create_resolution_workspace(package, tmp_path / "review", write=False)
    )

    first = write_workspace(workspace, tmp_path / "a")
    second = write_workspace(workspace, tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()
    assert sha256_of(first) == sha256_of(second)


def test_creating_the_same_workspace_twice_is_byte_identical(tmp_path: Path):
    package = e4_package(tmp_path)
    first = create_resolution_workspace(package, tmp_path / "a")
    second = create_resolution_workspace(package, tmp_path / "b")
    assert first == second
    assert sha256_of(workspace_path_for(tmp_path / "a", "E4")) == sha256_of(
        workspace_path_for(tmp_path / "b", "E4")
    )


def test_the_workspace_json_uses_the_project_convention(tmp_path: Path):
    package = e4_package(tmp_path)
    create_resolution_workspace(package, tmp_path / "review")
    text = workspace_path_for(tmp_path / "review", "E4").read_text(encoding="utf-8")

    assert text.startswith("{\n  ")  # indent=2
    top_level = [line.split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert top_level == sorted(top_level)  # sort_keys=True


def test_caller_timestamps_survive_the_workspace_round_trip(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    workspace = review_assign_owner(
        workspace, "EXC-FXC-03-E4", "finance.manager", occurred_at=OWNED_AT
    )
    workspace = review_transition_exception(
        workspace, "EXC-FXC-03-E4", INVESTIGATING, comment="Started.",
        occurred_at=STARTED_AT,
    )
    workspace = review_transition_exception(
        workspace, "EXC-FXC-03-E4", RESOLVED, comment="Explained.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at=RESOLVED_AT,
    )
    write_workspace(workspace, tmp_path / "review")

    reloaded = load_workspace(tmp_path / "review", "E4")
    exception = get_review_exception(reloaded, "EXC-FXC-03-E4")

    assert [
        (item["action"], item["occurred_at"]) for item in exception["history"]
    ] == [
        ("CREATED", FIXED_TIMESTAMP),
        ("OWNER_CHANGED", OWNED_AT),
        ("STATUS_CHANGED", STARTED_AT),
        ("STATUS_CHANGED", RESOLVED_AT),
    ]
    assert exception["status"] == RESOLVED
    assert exception["owner"] == "finance.manager"


def test_the_module_generates_no_timestamps_of_its_own():
    source = (ROOT / "src" / "review_workspace.py").read_text(encoding="utf-8")
    for token in ("datetime", "time.time", "now(", "utcnow", "uuid", "random"):
        assert token not in source, f"{token!r} must not appear in the workspace module"


def test_a_written_workspace_reloads_unchanged(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = resolve_everything(
        create_resolution_workspace(package, tmp_path / "review", write=False)
    )
    write_workspace(workspace, tmp_path / "review")
    assert load_workspace(tmp_path / "review", "E4") == workspace


def test_a_workspace_validates_against_the_workflow_engine(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)
    validate_workspace(workspace)

    resolved = resolve_everything(workspace)
    validate_workspace(resolved)

    broken = json.loads(json.dumps(resolved))
    broken["exceptions"][0]["resolution"] = None
    with pytest.raises(ValueError, match="resolution"):
        validate_workspace(broken)


def test_a_workspace_missing_its_provenance_is_rejected(tmp_path: Path):
    package = e4_package(tmp_path)
    workspace = create_resolution_workspace(package, tmp_path / "review", write=False)

    without_hash = json.loads(json.dumps(workspace))
    without_hash["source_package"].pop("exception_register_sha256")
    with pytest.raises(ValueError, match="exception_register_sha256"):
        validate_workspace(without_hash)

    without_source = json.loads(json.dumps(workspace))
    without_source.pop("source_package")
    with pytest.raises(ValueError, match="source_package"):
        validate_workspace(without_source)

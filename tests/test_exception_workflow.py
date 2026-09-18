import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ERR = ROOT / "data" / "error_data"

import pytest

from audit import build_exception_records

from exception_workflow import (
    INVESTIGATING,
    OPEN,
    RESOLVED,
    assign_owner,
    build_exception_register,
    get_exception,
    make_exception_id,
    transition_exception,
    validate_register,
)


def sample_exceptions():
    return [
        {
            "check_id": "FXC-05",
            "name": "EUR equivalent reconciliation",
            "group": "FX",
            "family": "RECONCILIATION",
            "severity": "CRITICAL",
            "status": "FAIL",
            "message": "EUR equivalents do not reconcile.",
            "error_reference": "E4",
            "details": {
                "actual": 6.0,
                "expected": 0.0,
                "failed_count": 6,
                "references": ["COST002-BE01-US01-P"],
            },
        },
        {
            "check_id": "FXC-03",
            "name": "FX rate accuracy",
            "group": "FX",
            "family": "SOURCE_ACCURACY",
            "severity": "CRITICAL",
            "status": "FAIL",
            "message": "February FX rate differs from reference.",
            "error_reference": "E4",
            "details": {
                "reference_rate": 1.09,
                "supplied_rate": 1.19,
                "difference": 0.10,
                "references": ["2026-02"],
            },
        },
    ]


def test_make_exception_id_is_deterministic():
    exception = sample_exceptions()[0]

    first = make_exception_id(exception)
    second = make_exception_id(copy.deepcopy(exception))

    assert first == second
    assert first == "EXC-FXC-05-E4"


def test_register_is_deterministic():
    source = sample_exceptions()

    first = build_exception_register(source, "E4")
    second = build_exception_register(source, "E4")

    assert first == second


def test_register_has_stable_sorted_exceptions():
    register = build_exception_register(sample_exceptions(), "E4")

    assert register["exception_count"] == 2
    assert [
        item["exception_id"] for item in register["exceptions"]
    ] == [
        "EXC-FXC-03-E4",
        "EXC-FXC-05-E4",
    ]


def test_initial_status_is_open():
    register = build_exception_register(sample_exceptions(), "E4")

    for exception in register["exceptions"]:
        assert exception["status"] == OPEN
        assert exception["owner"] is None
        assert exception["resolution"] is None
        assert exception["history"][0]["action"] == "CREATED"


def test_assign_owner():
    register = build_exception_register(sample_exceptions(), "E4")

    updated = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
        comment="Assigned for investigation.",
    )

    exception = get_exception(updated, "EXC-FXC-03-E4")

    assert exception["owner"] == "finance.manager"
    assert exception["status"] == OPEN

    history = exception["history"][-1]
    assert history["action"] == "OWNER_CHANGED"
    assert history["owner"] == "finance.manager"


def test_owner_can_be_cleared():
    register = build_exception_register(sample_exceptions(), "E4")

    assigned = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
    )

    cleared = assign_owner(
        assigned,
        "EXC-FXC-03-E4",
        None,
    )

    exception = get_exception(cleared, "EXC-FXC-03-E4")

    assert exception["owner"] is None


def test_open_to_investigating():
    register = build_exception_register(sample_exceptions(), "E4")

    updated = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigating FX source file.",
    )

    exception = get_exception(updated, "EXC-FXC-03-E4")

    assert exception["status"] == INVESTIGATING
    assert exception["history"][-1]["from_status"] == OPEN
    assert exception["history"][-1]["to_status"] == INVESTIGATING


def test_investigating_can_return_to_open():
    register = build_exception_register(sample_exceptions(), "E4")

    investigating = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    reopened = transition_exception(
        investigating,
        "EXC-FXC-03-E4",
        OPEN,
        comment="Waiting for supporting evidence.",
    )

    exception = get_exception(reopened, "EXC-FXC-03-E4")

    assert exception["status"] == OPEN
    assert exception["history"][-1]["from_status"] == INVESTIGATING
    assert exception["history"][-1]["to_status"] == OPEN


def test_resolved_requires_comment():
    register = build_exception_register(sample_exceptions(), "E4")

    investigating = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    with pytest.raises(ValueError, match="resolution comment"):
        transition_exception(
            investigating,
            "EXC-FXC-03-E4",
            RESOLVED,
            evidence=[
                {
                    "type": "control_result",
                    "reference": "FXC-03",
                }
            ],
        )


def test_resolved_requires_evidence():
    register = build_exception_register(sample_exceptions(), "E4")

    investigating = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    with pytest.raises(ValueError, match="evidence reference"):
        transition_exception(
            investigating,
            "EXC-FXC-03-E4",
            RESOLVED,
            comment="Corrected the source rate.",
        )


def test_investigating_to_resolved_requires_both():
    register = build_exception_register(sample_exceptions(), "E4")

    investigating = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    resolved = transition_exception(
        investigating,
        "EXC-FXC-03-E4",
        RESOLVED,
        comment="Confirmed the incorrect February FX source entry.",
        evidence=[
            {
                "type": "source_file",
                "reference": "data/raw/fx_rates_expected.csv",
            },
            {
                "type": "control_result",
                "reference": "FXC-03",
            },
        ],
    )

    exception = get_exception(resolved, "EXC-FXC-03-E4")

    assert exception["status"] == RESOLVED
    assert exception["resolution"]["comment"] == (
        "Confirmed the incorrect February FX source entry."
    )
    assert len(exception["resolution"]["evidence"]) == 2
    # No owner is assigned in this test, so the history is CREATED plus one
    # STATUS_CHANGED per transition (OPEN -> INVESTIGATING -> RESOLVED).
    assert len(exception["history"]) == 3
    assert [item["action"] for item in exception["history"]] == [
        "CREATED",
        "STATUS_CHANGED",
        "STATUS_CHANGED",
    ]


def test_resolved_is_terminal():
    register = build_exception_register(sample_exceptions(), "E4")

    investigating = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    resolved = transition_exception(
        investigating,
        "EXC-FXC-03-E4",
        RESOLVED,
        comment="Issue resolved.",
        evidence=[
            {
                "type": "control_result",
                "reference": "FXC-03",
            }
        ],
    )

    with pytest.raises(ValueError, match="cannot leave RESOLVED"):
        transition_exception(
            resolved,
            "EXC-FXC-03-E4",
            OPEN,
            comment="Reopening.",
        )


def test_open_cannot_jump_directly_to_resolved():
    register = build_exception_register(sample_exceptions(), "E4")

    with pytest.raises(ValueError, match="Invalid transition"):
        transition_exception(
            register,
            "EXC-FXC-03-E4",
            RESOLVED,
            comment="Resolved.",
            evidence=[
                {
                    "type": "control_result",
                    "reference": "FXC-03",
                }
            ],
        )


def test_invalid_status_is_rejected():
    register = build_exception_register(sample_exceptions(), "E4")

    with pytest.raises(ValueError, match="Invalid status"):
        transition_exception(
            register,
            "EXC-FXC-03-E4",
            "CLOSED",
        )


def test_unknown_exception_id_is_rejected():
    register = build_exception_register(sample_exceptions(), "E4")

    with pytest.raises(KeyError, match="Unknown exception_id"):
        get_exception(register, "EXC-NOT-REAL")


def test_source_exception_is_preserved():
    source = sample_exceptions()
    register = build_exception_register(source, "E4")

    source[0]["message"] = "changed outside the workflow"

    stored = get_exception(register, "EXC-FXC-05-E4")

    assert stored["source_exception"]["message"] == (
        "EUR equivalents do not reconcile."
    )


def test_workflow_operations_do_not_mutate_original_register():
    register = build_exception_register(sample_exceptions(), "E4")
    original = copy.deepcopy(register)

    updated = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
    )

    updated = transition_exception(
        updated,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
    )

    assert register == original
    assert updated != original


def test_evidence_is_accumulated():
    register = build_exception_register(sample_exceptions(), "E4")

    updated = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Started investigation.",
        evidence=[
            {
                "type": "control_result",
                "reference": "FXC-03",
            }
        ],
    )

    updated = transition_exception(
        updated,
        "EXC-FXC-03-E4",
        OPEN,
        comment="Need additional support.",
        evidence=[
            {
                "type": "source_file",
                "reference": "data/raw/fx_rates_expected.csv",
            }
        ],
    )

    exception = get_exception(updated, "EXC-FXC-03-E4")

    assert exception["evidence"] == [
        {
            "type": "control_result",
            "reference": "FXC-03",
        },
        {
            "type": "source_file",
            "reference": "data/raw/fx_rates_expected.csv",
        },
    ]


def test_validate_register_accepts_valid_register():
    register = build_exception_register(sample_exceptions(), "E4")

    validate_register(register)


def test_validate_register_rejects_duplicate_ids():
    register = build_exception_register(sample_exceptions(), "E4")

    register["exceptions"][1]["exception_id"] = (
        register["exceptions"][0]["exception_id"]
    )

    with pytest.raises(ValueError, match="unique"):
        validate_register(register)


def test_validate_register_rejects_invalid_resolved_record():
    register = build_exception_register(sample_exceptions(), "E4")

    register["exceptions"][0]["status"] = RESOLVED
    register["exceptions"][0]["resolution"] = None

    with pytest.raises(ValueError, match="resolution"):
        validate_register(register)

def test_real_e4_audit_exceptions_enter_resolution_register():
    """
    Verify that the actual E4 audit exceptions can be consumed by the
    resolution workflow without changing their source evidence.
    """
    from test_audit import run_close_for

    results, _ = run_close_for(
        fx_path=ERR / "fx_rates.csv",
        label="E4",
    )

    audit_exceptions = build_exception_records(results)

    register = build_exception_register(
        audit_exceptions,
        "E4",
    )

    assert register["workflow_version"] == "1.0.0"
    assert register["dataset_label"] == "E4"
    assert register["exception_count"] == 2

    assert [
        exception["exception_id"]
        for exception in register["exceptions"]
    ] == [
        "EXC-FXC-03-E4",
        "EXC-FXC-05-E4",
    ]

    # The source evidence from audit.py must be preserved exactly.
    for exception in register["exceptions"]:
        source = exception["source_exception"]

        assert source in audit_exceptions
        assert exception["control_id"] == source["check_id"]
        assert exception["severity"] == source["severity"]
        assert exception["source_status"] == source["status"]
        assert exception["details"] == source["details"]


def test_real_e4_exception_can_be_resolved_with_evidence():
    """
    A real FXC-03 exception can move through the workflow and be resolved
    using references to existing close evidence.
    """
    from test_audit import run_close_for

    results, _ = run_close_for(
        fx_path=ERR / "fx_rates.csv",
        label="E4",
    )

    audit_exceptions = build_exception_records(results)
    register = build_exception_register(audit_exceptions, "E4")

    register = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
        comment="Assigned to investigate the February FX discrepancy.",
    )

    register = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Reviewing the February FX source.",
        evidence=[
            {
                "type": "control_result",
                "reference": "FXC-03",
            },
            {
                "type": "period",
                "reference": "2026-02",
            },
        ],
    )

    register = transition_exception(
        register,
        "EXC-FXC-03-E4",
        RESOLVED,
        comment="Confirmed the February supplied FX rate differs from the reference.",
        evidence=[
            {
                "type": "source_file",
                "reference": "data/raw/fx_rates_expected.csv",
            }
        ],
    )

    exception = get_exception(register, "EXC-FXC-03-E4")

    assert exception["status"] == RESOLVED
    assert exception["owner"] == "finance.manager"
    assert exception["resolution"]["comment"].startswith(
        "Confirmed the February supplied FX rate"
    )

    assert len(exception["history"]) == 4
    assert [item["action"] for item in exception["history"]] == [
        "CREATED",
        "OWNER_CHANGED",
        "STATUS_CHANGED",
        "STATUS_CHANGED",
    ]


def test_real_e1_to_e3_exceptions_have_stable_workflow_ids():
    """
    The actual planted matching exceptions must map to deterministic workflow
    IDs, preserving the existing audit control IDs and error references.
    """
    from test_audit import run_close_for

    results, _ = run_close_for(
        invoices_path=ERR / "invoices.csv",
        payments_path=ERR / "payments.csv",
        label="E1-E3",
    )

    audit_exceptions = build_exception_records(results)

    register = build_exception_register(
        audit_exceptions,
        "E1-E3",
    )

    assert [
        exception["exception_id"]
        for exception in register["exceptions"]
    ] == [
        "EXC-MAT-03-E2",
        "EXC-MAT-04-E1",
        "EXC-MAT-06-E3",
    ]

    assert [
        exception["control_id"]
        for exception in register["exceptions"]
    ] == [
        "MAT-03",
        "MAT-04",
        "MAT-06",
    ]

    assert [
        exception["error_reference"]
        for exception in register["exceptions"]
    ] == [
        "E2",
        "E1",
        "E3",
    ]


def test_resolution_workflow_does_not_change_control_source_data():
    """
    Transitioning an exception must not modify the original audit exception.
    """
    from test_audit import run_close_for

    results, _ = run_close_for(
        fx_path=ERR / "fx_rates.csv",
        label="E4",
    )

    audit_exceptions = build_exception_records(results)

    original = copy.deepcopy(audit_exceptions)

    register = build_exception_register(
        audit_exceptions,
        "E4",
    )

    register = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
        evidence=[
            {
                "type": "control_result",
                "reference": "FXC-03",
            }
        ],
    )

    assert audit_exceptions == original

def test_real_audit_records_keep_their_control_name_and_family():
    """
    Audit records spell these fields check_name/check_group/control_family.

    Reading only the short spellings stored None for every real exception, so
    the register lost the control name and family it exists to preserve. The
    hand-written fixture above cannot catch that, because it uses the short
    spellings rather than the ones audit.py actually emits.
    """
    from test_audit import run_close_for

    results, _ = run_close_for(
        fx_path=ERR / "fx_rates.csv",
        label="E4",
    )

    audit_exceptions = build_exception_records(results)
    register = build_exception_register(audit_exceptions, "E4")

    for exception in register["exceptions"]:
        source = exception["source_exception"]

        assert exception["name"] == source["check_name"]
        assert exception["group"] == source["check_group"]
        assert exception["family"] == source["control_family"]

        assert exception["name"], "the control name must not be empty"
        assert exception["family"], "the control family must not be empty"


def test_short_field_spellings_are_still_accepted():
    """The hand-written fixture spelling must keep working."""
    register = build_exception_register(sample_exceptions(), "E4")
    exception = get_exception(register, "EXC-FXC-03-E4")

    assert exception["name"] == "FX rate accuracy"
    assert exception["group"] == "FX"
    assert exception["family"] == "SOURCE_ACCURACY"


# ---------------------------------------------------------------------------
# Step 12C: the caller-supplied time dimension
# ---------------------------------------------------------------------------
CREATED_AT = "2026-03-31T18:00:00Z"
OWNED_AT = "2026-03-31T18:05:00Z"
STARTED_AT = "2026-03-31T18:10:00Z"
RESOLVED_AT = "2026-03-31T18:30:00Z"


def test_created_event_has_no_timestamp_unless_one_is_supplied():
    register = build_exception_register(sample_exceptions(), "E4")

    for exception in register["exceptions"]:
        created = exception["history"][0]
        assert created["action"] == "CREATED"
        assert "occurred_at" in created, "the key must exist even when empty"
        assert created["occurred_at"] is None


def test_created_event_keeps_an_explicit_creation_timestamp():
    register = build_exception_register(
        sample_exceptions(), "E4", occurred_at=CREATED_AT
    )

    for exception in register["exceptions"]:
        assert exception["history"][0]["occurred_at"] == CREATED_AT


def test_owner_and_status_events_default_to_no_timestamp():
    register = build_exception_register(sample_exceptions(), "E4")

    register = assign_owner(register, "EXC-FXC-03-E4", "finance.manager")
    register = transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING, comment="Started."
    )

    exception = get_exception(register, "EXC-FXC-03-E4")
    assert [item["occurred_at"] for item in exception["history"]] == [None, None, None]


def test_assign_owner_stores_the_supplied_timestamp_verbatim():
    register = build_exception_register(sample_exceptions(), "E4")

    updated = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
        occurred_at=OWNED_AT,
    )

    event = get_exception(updated, "EXC-FXC-03-E4")["history"][-1]
    assert event["action"] == "OWNER_CHANGED"
    assert event["occurred_at"] == OWNED_AT


def test_transition_stores_the_supplied_timestamp_verbatim():
    register = build_exception_register(sample_exceptions(), "E4")

    updated = transition_exception(
        register,
        "EXC-FXC-03-E4",
        INVESTIGATING,
        comment="Investigation started.",
        occurred_at=STARTED_AT,
    )

    event = get_exception(updated, "EXC-FXC-03-E4")["history"][-1]
    assert event["action"] == "STATUS_CHANGED"
    assert event["occurred_at"] == STARTED_AT


def test_a_timestamp_is_never_reformatted():
    """Stored verbatim means verbatim: no parsing, padding or normalising."""
    odd_but_caller_chosen = "31/03/2026 18:05  (Europe/Brussels)"

    register = build_exception_register(sample_exceptions(), "E4")
    updated = assign_owner(
        register,
        "EXC-FXC-03-E4",
        "finance.manager",
        occurred_at=odd_but_caller_chosen,
    )

    event = get_exception(updated, "EXC-FXC-03-E4")["history"][-1]
    assert event["occurred_at"] == odd_but_caller_chosen


def test_a_full_lifecycle_records_a_timestamp_on_every_event():
    register = build_exception_register(sample_exceptions(), "E4")

    register = assign_owner(
        register, "EXC-FXC-03-E4", "finance.manager", occurred_at=OWNED_AT
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING,
        comment="Reviewing the February FX source.", occurred_at=STARTED_AT,
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", RESOLVED,
        comment="Confirmed the incorrect February rate.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at=RESOLVED_AT,
    )

    exception = get_exception(register, "EXC-FXC-03-E4")

    assert [
        (item["action"], item["occurred_at"]) for item in exception["history"]
    ] == [
        ("CREATED", None),
        ("OWNER_CHANGED", OWNED_AT),
        ("STATUS_CHANGED", STARTED_AT),
        ("STATUS_CHANGED", RESOLVED_AT),
    ]


def test_a_reopened_exception_records_when_it_was_reopened():
    register = build_exception_register(sample_exceptions(), "E4")

    register = transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING,
        comment="Started.", occurred_at=STARTED_AT,
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", OPEN,
        comment="Waiting for evidence.", occurred_at=RESOLVED_AT,
    )

    reopened = get_exception(register, "EXC-FXC-03-E4")["history"][-1]
    assert reopened["from_status"] == INVESTIGATING
    assert reopened["to_status"] == OPEN
    assert reopened["occurred_at"] == RESOLVED_AT


def test_resolution_does_not_duplicate_the_timestamp():
    """The event log owns 'when'; resolution owns 'what' and 'on what evidence'.

    Two copies of the same fact could disagree after an edit, and the history
    event is the one a reviewer reads in sequence.
    """
    register = build_exception_register(sample_exceptions(), "E4")

    register = transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING, comment="Started.",
        occurred_at=STARTED_AT,
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", RESOLVED, comment="Explained.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at=RESOLVED_AT,
    )

    exception = get_exception(register, "EXC-FXC-03-E4")
    assert set(exception["resolution"]) == {"comment", "evidence"}
    assert exception["history"][-1]["occurred_at"] == RESOLVED_AT


def test_an_invalid_timestamp_is_rejected_at_the_call_that_introduced_it():
    """A non-string would only fail later, when the register is serialised."""
    register = build_exception_register(sample_exceptions(), "E4")

    for bad in (20260331, "", "   ", ["2026-03-31"]):
        with pytest.raises(ValueError, match="occurred_at"):
            assign_owner(register, "EXC-FXC-03-E4", "finance.manager", occurred_at=bad)

        with pytest.raises(ValueError, match="occurred_at"):
            transition_exception(
                register, "EXC-FXC-03-E4", INVESTIGATING,
                comment="Started.", occurred_at=bad,
            )

    with pytest.raises(ValueError, match="occurred_at"):
        build_exception_register(sample_exceptions(), "E4", occurred_at=123)


def test_identical_timestamps_produce_identical_registers():
    def build():
        register = build_exception_register(
            sample_exceptions(), "E4", occurred_at=CREATED_AT
        )
        register = assign_owner(
            register, "EXC-FXC-03-E4", "finance.manager", occurred_at=OWNED_AT
        )
        register = transition_exception(
            register, "EXC-FXC-03-E4", INVESTIGATING,
            comment="Started.", occurred_at=STARTED_AT,
        )
        return transition_exception(
            register, "EXC-FXC-03-E4", RESOLVED, comment="Explained.",
            evidence=[{"type": "control_result", "reference": "FXC-03"}],
            occurred_at=RESOLVED_AT,
        )

    assert build() == build()


def test_different_timestamps_produce_different_registers():
    """Determinism must not mean the timestamp is ignored."""
    base = build_exception_register(sample_exceptions(), "E4")

    first = assign_owner(base, "EXC-FXC-03-E4", "a.person", occurred_at=OWNED_AT)
    second = assign_owner(base, "EXC-FXC-03-E4", "a.person", occurred_at=RESOLVED_AT)

    assert first != second


def test_timestamps_do_not_mutate_the_input_register():
    register = build_exception_register(sample_exceptions(), "E4")
    original = copy.deepcopy(register)

    assign_owner(register, "EXC-FXC-03-E4", "finance.manager", occurred_at=OWNED_AT)
    transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING, comment="Started.",
        occurred_at=STARTED_AT,
    )

    assert register == original


def test_the_module_never_reads_a_clock():
    """Grep the production source: no clock call may exist anywhere in it."""
    import ast

    source_path = ROOT / "src" / "exception_workflow.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "datetime" not in imported
    assert "time" not in imported
    assert "uuid" not in imported
    assert "random" not in imported

    for token in ("datetime", "time.time", "now(", "utcnow", "uuid", "random"):
        assert token not in source, f"{token!r} must not appear in the workflow"


def test_a_register_with_timestamps_survives_json_serialisation_unchanged():
    """The register ships as exception_register.json, so this is what matters."""
    import json

    register = build_exception_register(
        sample_exceptions(), "E4", occurred_at=CREATED_AT
    )
    register = assign_owner(
        register, "EXC-FXC-03-E4", "finance.manager", occurred_at=OWNED_AT
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", INVESTIGATING, comment="Started.",
        occurred_at=STARTED_AT,
    )
    register = transition_exception(
        register, "EXC-FXC-03-E4", RESOLVED, comment="Explained.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at=RESOLVED_AT,
    )

    encoded = json.dumps(register, indent=2, sort_keys=True)
    restored = json.loads(encoded)

    assert restored == register
    assert [
        item["occurred_at"]
        for item in restored["exceptions"][0]["history"]
    ] == [CREATED_AT, OWNED_AT, STARTED_AT, RESOLVED_AT]

    # and the serialisation itself is stable
    assert json.dumps(restored, indent=2, sort_keys=True) == encoded

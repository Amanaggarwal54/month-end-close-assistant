"""Step 13A: the AI investigation layer is advisory and cannot decide anything.

Most of these tests are about what an investigation must *not* be able to do.
The load-bearing pair are
``test_a_full_investigation_leaves_every_package_artefact_byte_identical`` and
``test_a_result_carrying_a_decision_field_is_rejected``: the first proves advice
cannot reach the immutable close, the second proves it cannot be dressed up as a
verdict.

Everything here runs offline against a deterministic stub. No network, no API
key, no model.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import investigation  # noqa: E402
from investigation import (  # noqa: E402
    FORBIDDEN_RESULT_FIELDS,
    PACKET_SCHEMA_VERSION,
    REQUIRED_RESULT_FIELDS,
    ExceptionNotFound,
    InvalidInvestigationResult,
    InvestigationProvider,
    StubInvestigationProvider,
    build_investigation_packet,
    investigate_exception,
    validate_investigation_result,
)
from paths import sha256_of  # noqa: E402
from pipeline import CloseRunConfig, run_close  # noqa: E402
from review_workspace import (  # noqa: E402
    REGISTER_FILENAME,
    create_resolution_workspace,
)

RAW = ROOT / "data" / "raw"
ERR = ROOT / "data" / "error_data"
FIXED_TIMESTAMP = "2026-03-31T18:00:00Z"
FX_ID = "EXC-FXC-03-E4"
OTHER_ID = "EXC-FXC-05-E4"


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"dataset not available: {path}")
    return path


def e4_package(tmp_path: Path) -> Path:
    _require(ERR / "fx_rates.csv")
    return run_close(
        CloseRunConfig(
            data_dir=RAW,
            fx_rates=ERR / "fx_rates.csv",
            out_dir=tmp_path / "out",
            run_timestamp=FIXED_TIMESTAMP,
            dataset_label="E4",
        )
    ).written["report_pdf"].parent


def e4_workspace(tmp_path: Path) -> dict:
    return create_resolution_workspace(e4_package(tmp_path), tmp_path / "review")


def hash_package(package_dir: Path) -> dict[str, str]:
    return {path.name: sha256_of(path) for path in sorted(package_dir.iterdir())}


def valid_result(packet: dict, **overrides) -> dict:
    """A minimal result that passes validation, for tests to break deliberately."""
    result = {
        "exception_id": packet["exception_id"],
        "summary": "A short explanation.",
        "observations": ["Something was observed."],
        "possible_causes": ["Something may have caused it."],
        "recommended_checks": ["Check something."],
        "evidence_references": [],
        "uncertainties": [],
        "resolution_suggestion": "A person should decide.",
    }
    result.update(overrides)
    return result


class EchoProvider:
    """Returns whatever it was constructed with, so a test can shape the answer."""

    def __init__(self, response):
        self.response = response
        self.packets: list[dict] = []

    def investigate(self, packet):
        self.packets.append(packet)
        return copy.deepcopy(self.response) if isinstance(self.response, dict) else self.response


# ---------------------------------------------------------------------------
# the packet
# ---------------------------------------------------------------------------
def test_the_packet_describes_the_selected_exception(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    assert packet["schema_version"] == PACKET_SCHEMA_VERSION
    assert packet["exception_id"] == FX_ID
    assert packet["control_id"] == "FXC-03"
    assert packet["control_family"] == "SOURCE_ACCURACY"
    assert packet["severity"] == "CRITICAL"
    assert packet["status"] == "OPEN"
    assert packet["dataset_label"] == "E4"
    assert packet["close_status"] == "FAIL"
    assert packet["report_allowed"] is False


def test_the_packet_carries_only_the_selected_exception(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    encoded = json.dumps(packet)
    assert OTHER_ID not in encoded, "a packet is about one exception"
    assert "FXC-05" not in encoded


def test_source_evidence_is_preserved_exactly(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    entry = workspace["exceptions"][0]
    packet = build_investigation_packet(workspace, FX_ID)

    assert packet["source_exception"] == entry["source_exception"]
    assert packet["details"] == entry["details"]
    assert packet["history"] == entry["history"]
    # the FX figures reach the packet unrounded and unrenamed
    assert packet["details"]["reference_rate"] == 1.09
    assert packet["details"]["supplied_rate"] == 1.19


def test_absent_evidence_is_named_rather_than_invented(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    assert packet["available_evidence"] == []
    assert set(packet["absent_evidence"]) == {
        "workspace_evidence", "source_files", "control_results"
    }
    assert "control_results" not in packet


def test_supplied_evidence_appears_and_stops_being_absent(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(
        workspace,
        FX_ID,
        source_files=["data/raw/fx_rates_expected.csv"],
        control_results=[
            {"check_id": "FXC-03", "status": "FAIL", "explanation": "rate differs"},
            {"check_id": "MAT-01", "status": "PASS", "explanation": "unrelated"},
        ],
    )

    assert {"type": "source_file", "reference": "data/raw/fx_rates_expected.csv"} in (
        packet["available_evidence"]
    )
    assert {"type": "control_result", "reference": "FXC-03"} in packet["available_evidence"]
    assert packet["absent_evidence"] == ["workspace_evidence"]

    # only the rows for this exception's control
    assert [row["check_id"] for row in packet["control_results"]] == ["FXC-03"]


def test_evidence_mappings_keep_their_own_type(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(
        workspace,
        FX_ID,
        source_files=[{"type": "reference_file", "reference": "fx_rates_expected.csv"}],
    )
    assert {"type": "reference_file", "reference": "fx_rates_expected.csv"} in (
        packet["available_evidence"]
    )


def test_the_packet_states_the_rules_a_provider_must_follow(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    instructions = build_investigation_packet(workspace, FX_ID)["instructions"]

    assert "never a decision" in instructions["advisory_only"]
    assert "invent" in instructions["evidence_rule"]
    assert "resolves nothing by itself" in instructions["resolution_rule"]
    assert sorted(REQUIRED_RESULT_FIELDS) == instructions["required_result_fields"]
    assert "close_status" in instructions["forbidden_result_fields"]


def test_building_a_packet_does_not_mutate_the_workspace(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    before = json.dumps(workspace, sort_keys=True)

    packet = build_investigation_packet(workspace, FX_ID)
    packet["details"]["reference_rate"] = 99.0
    packet["source_exception"]["message"] = "tampered"

    assert json.dumps(workspace, sort_keys=True) == before


def test_an_unknown_exception_id_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    with pytest.raises(ExceptionNotFound, match="EXC-NOT-REAL"):
        build_investigation_packet(workspace, "EXC-NOT-REAL")


def test_the_packet_is_json_serialisable_and_deterministic(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    first = build_investigation_packet(workspace, FX_ID)
    second = build_investigation_packet(workspace, FX_ID)

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# the stub provider
# ---------------------------------------------------------------------------
def test_the_stub_satisfies_the_provider_protocol():
    assert isinstance(StubInvestigationProvider(), InvestigationProvider)


def test_the_stub_produces_a_valid_advisory_result(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    result = investigate_exception(workspace, FX_ID, StubInvestigationProvider())

    assert result["exception_id"] == FX_ID
    for field in REQUIRED_RESULT_FIELDS:
        assert field in result
    assert "FXC-03" in result["summary"]
    assert any("1.19" in line for line in result["observations"])


def test_the_stub_turns_missing_evidence_into_stated_uncertainty(tmp_path: Path):
    """Requirement 9: missing evidence is reported, never filled in."""
    workspace = e4_workspace(tmp_path)
    result = investigate_exception(workspace, FX_ID, StubInvestigationProvider())

    assert result["uncertainties"], "absent evidence must be stated"
    joined = " ".join(result["uncertainties"])
    assert "source files" in joined
    assert "control results" in joined
    assert result["evidence_references"] == []


def test_the_stub_cites_evidence_only_when_it_was_given_some(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    result = investigate_exception(
        workspace, FX_ID, StubInvestigationProvider(),
        source_files=["data/raw/fx_rates_expected.csv"],
    )

    assert {"type": "source_file", "reference": "data/raw/fx_rates_expected.csv"} in (
        result["evidence_references"]
    )


def test_two_identical_investigations_produce_identical_output(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    provider = StubInvestigationProvider()

    first = investigate_exception(workspace, FX_ID, provider)
    second = investigate_exception(workspace, FX_ID, provider)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# validation: decisions are rejected
# ---------------------------------------------------------------------------
def test_a_result_carrying_a_decision_field_is_rejected(tmp_path: Path):
    """The core safety property: advice cannot be dressed up as a verdict."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    for field in ("close_status", "report_allowed", "approved", "approve_close",
                  "resolve_exception", "control_result_override",
                  "replacement_control_result"):
        bad = valid_result(packet, **{field: "PASS"})
        with pytest.raises(InvalidInvestigationResult, match="decision field"):
            validate_investigation_result(bad, packet)


def test_a_decision_field_hidden_inside_a_nested_object_is_rejected(tmp_path: Path):
    """Nesting is the obvious way round a top-level ban, so the check recurses."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    nested = valid_result(packet, extra={"deeply": {"approved": True}})
    with pytest.raises(InvalidInvestigationResult, match="decision field"):
        validate_investigation_result(nested, packet)

    in_a_list = valid_result(packet, notes=[{"close_status": "PASS"}])
    with pytest.raises(InvalidInvestigationResult, match="decision field"):
        validate_investigation_result(in_a_list, packet)


def test_a_provider_cannot_approve_a_close_through_the_orchestrator(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)
    rogue = EchoProvider(valid_result(packet, close_status="PASS", report_allowed=True))

    with pytest.raises(InvalidInvestigationResult):
        investigate_exception(workspace, FX_ID, rogue)


def test_the_forbidden_list_covers_every_field_the_brief_named():
    for field in ("close_status", "report_allowed", "approved", "approve_close",
                  "resolve_exception", "control_result_override",
                  "replacement_control_result"):
        assert field in FORBIDDEN_RESULT_FIELDS


# ---------------------------------------------------------------------------
# validation: shape and identity
# ---------------------------------------------------------------------------
def test_a_result_that_is_not_an_object_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    for junk in ("a string", ["a", "list"], 42, None):
        with pytest.raises(InvalidInvestigationResult, match="must be an object"):
            validate_investigation_result(junk, packet)


def test_a_result_missing_a_required_field_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    for field in REQUIRED_RESULT_FIELDS:
        incomplete = valid_result(packet)
        incomplete.pop(field)
        with pytest.raises(InvalidInvestigationResult, match="missing required"):
            validate_investigation_result(incomplete, packet)


def test_a_result_with_the_wrong_field_type_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    with pytest.raises(InvalidInvestigationResult, match="summary must be str"):
        validate_investigation_result(valid_result(packet, summary=["a", "list"]), packet)

    with pytest.raises(InvalidInvestigationResult, match="observations must be list"):
        validate_investigation_result(valid_result(packet, observations="text"), packet)

    with pytest.raises(InvalidInvestigationResult, match="must be a string"):
        validate_investigation_result(valid_result(packet, observations=[1, 2]), packet)


def test_an_empty_summary_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    with pytest.raises(InvalidInvestigationResult, match="summary must not be empty"):
        validate_investigation_result(valid_result(packet, summary="   "), packet)


def test_a_result_about_a_different_exception_is_rejected(tmp_path: Path):
    """An investigation must stay attached to the exception it was asked about."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    wrong = valid_result(packet, exception_id=OTHER_ID)
    with pytest.raises(InvalidInvestigationResult, match="but the packet asked"):
        validate_investigation_result(wrong, packet)


# ---------------------------------------------------------------------------
# validation: evidence cannot be invented
# ---------------------------------------------------------------------------
def test_a_citation_the_packet_never_offered_is_rejected(tmp_path: Path):
    """Requirement: the AI cannot invent evidence."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    fabricated = valid_result(packet, evidence_references=[
        {"type": "source_file", "reference": "data/raw/a_file_nobody_supplied.csv"}
    ])
    with pytest.raises(InvalidInvestigationResult, match="not in\n?.*the packet|invent"):
        validate_investigation_result(fabricated, packet)


def test_a_citation_the_packet_did_offer_is_accepted(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(
        workspace, FX_ID, source_files=["data/raw/fx_rates_expected.csv"]
    )

    cited = valid_result(packet, evidence_references=list(packet["available_evidence"]))
    validated = validate_investigation_result(cited, packet)
    assert validated["evidence_references"] == packet["available_evidence"]


def test_a_new_line_of_enquiry_belongs_in_recommended_checks_not_citations(tmp_path: Path):
    """Suggesting somewhere to look is fine; claiming to have looked is not."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    suggestion = valid_result(
        packet,
        recommended_checks=["Open the ECB reference feed for February 2026."],
        evidence_references=[],
    )
    validate_investigation_result(suggestion, packet)  # accepted

    citation = valid_result(packet, evidence_references=[
        {"type": "source_file", "reference": "ecb_reference_feed.csv"}
    ])
    with pytest.raises(InvalidInvestigationResult):
        validate_investigation_result(citation, packet)


def test_a_malformed_evidence_reference_is_rejected(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)

    with pytest.raises(InvalidInvestigationResult, match="must be an object"):
        validate_investigation_result(
            valid_result(packet, evidence_references=["just a string"]), packet
        )

    with pytest.raises(InvalidInvestigationResult, match="missing"):
        validate_investigation_result(
            valid_result(packet, evidence_references=[{"type": "source_file"}]), packet
        )

    packet_with = build_investigation_packet(
        workspace, FX_ID, source_files=["data/raw/fx_rates_expected.csv"]
    )
    with pytest.raises(InvalidInvestigationResult, match="unsupported field"):
        validate_investigation_result(
            valid_result(packet_with, evidence_references=[
                {"type": "source_file",
                 "reference": "data/raw/fx_rates_expected.csv",
                 "note": "looks fine"}
            ]),
            packet_with,
        )


def test_a_verdict_smuggled_into_a_citation_is_caught_as_a_decision(tmp_path: Path):
    """`verdict` is a decision word, so it fails the stricter check first."""
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(
        workspace, FX_ID, source_files=["data/raw/fx_rates_expected.csv"]
    )

    with pytest.raises(InvalidInvestigationResult, match="decision field"):
        validate_investigation_result(
            valid_result(packet, evidence_references=[
                {"type": "source_file",
                 "reference": "data/raw/fx_rates_expected.csv",
                 "verdict": "clean"}
            ]),
            packet,
        )


# ---------------------------------------------------------------------------
# the close package stays untouched
# ---------------------------------------------------------------------------
def test_a_full_investigation_leaves_every_package_artefact_byte_identical(
    tmp_path: Path,
):
    """Advice cannot reach the close, however enthusiastic it is."""
    package = e4_package(tmp_path)
    before = hash_package(package)
    assert len(before) == 7

    workspace = create_resolution_workspace(package, tmp_path / "review")
    for exception_id in (FX_ID, OTHER_ID):
        result = investigate_exception(workspace, exception_id, StubInvestigationProvider())
        assert result["exception_id"] == exception_id

    assert hash_package(package) == before


def test_the_immutable_register_and_decision_are_unchanged(tmp_path: Path):
    package = e4_package(tmp_path)
    register_before = (package / REGISTER_FILENAME).read_text(encoding="utf-8")
    decision_before = (package / "close_decision.json").read_text(encoding="utf-8")
    controls_before = (package / "control_results.csv").read_text(encoding="utf-8")

    workspace = create_resolution_workspace(package, tmp_path / "review")
    investigate_exception(workspace, FX_ID, StubInvestigationProvider())

    assert (package / REGISTER_FILENAME).read_text(encoding="utf-8") == register_before
    assert (package / "close_decision.json").read_text(encoding="utf-8") == decision_before
    assert (package / "control_results.csv").read_text(encoding="utf-8") == controls_before

    decision = json.loads(decision_before)
    assert decision["close_status"] == "FAIL"
    assert decision["report_allowed"] is False


def test_investigating_does_not_mutate_the_workspace(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    before = json.dumps(workspace, sort_keys=True)

    investigate_exception(workspace, FX_ID, StubInvestigationProvider())

    assert json.dumps(workspace, sort_keys=True) == before


def test_an_investigation_does_not_resolve_anything(tmp_path: Path):
    """Resolution stays a separate human operation through the workflow."""
    from exception_workflow import INVESTIGATING, OPEN, RESOLVED
    from review_workspace import review_transition_exception

    workspace = e4_workspace(tmp_path)
    investigate_exception(workspace, FX_ID, StubInvestigationProvider())

    assert workspace["exceptions"][0]["status"] == OPEN
    assert workspace["exceptions"][0]["resolution"] is None

    # a person still has to walk it through the lifecycle
    updated = review_transition_exception(
        workspace, FX_ID, INVESTIGATING, comment="Reviewing.",
        occurred_at="2026-04-01T09:30:00Z",
    )
    updated = review_transition_exception(
        updated, FX_ID, RESOLVED, comment="Explained.",
        evidence=[{"type": "control_result", "reference": "FXC-03"}],
        occurred_at="2026-04-02T14:15:00Z",
    )
    assert updated["exceptions"][0]["status"] == RESOLVED
    assert workspace["exceptions"][0]["status"] == OPEN


def test_a_provider_cannot_reach_back_into_the_callers_packet(tmp_path: Path):
    """The provider gets a copy, so mutating its argument changes nothing here."""
    workspace = e4_workspace(tmp_path)

    class VandalProvider:
        def investigate(self, packet):
            packet["details"]["supplied_rate"] = 0.0
            packet["close_status"] = "PASS"
            return StubInvestigationProvider().investigate(packet)

    investigate_exception(workspace, FX_ID, VandalProvider())

    fresh = build_investigation_packet(workspace, FX_ID)
    assert fresh["details"]["supplied_rate"] == 1.19
    assert fresh["close_status"] == "FAIL"


def test_the_validated_result_is_a_copy(tmp_path: Path):
    workspace = e4_workspace(tmp_path)
    packet = build_investigation_packet(workspace, FX_ID)
    provider = EchoProvider(valid_result(packet))

    result = investigate_exception(workspace, FX_ID, provider)
    result["summary"] = "changed by the caller"

    assert provider.response["summary"] == "A short explanation."


# ---------------------------------------------------------------------------
# dependencies and determinism
# ---------------------------------------------------------------------------
def test_investigation_imports_only_the_standard_library():
    tree = ast.parse((SRC / "investigation.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }

    for forbidden in ("pandas", "numpy", "reportlab", "pipeline", "controls",
                      "match", "intercompany", "report", "anthropic", "openai",
                      "requests", "httpx"):
        assert forbidden not in imported, f"investigation.py must not import {forbidden}"

    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    unexpected = imported - stdlib - {"__future__"}
    assert not unexpected, f"non-stdlib imports: {sorted(unexpected)}"


def test_importing_investigation_loads_nothing_heavy():
    """Measured in a fresh interpreter; this session has already imported pandas."""
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import investigation\n"
        "print(','.join(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert completed.returncode == 0, completed.stderr
    loaded = set(completed.stdout.strip().split(","))

    assert "investigation" in loaded
    for heavy in ("pandas", "numpy", "reportlab", "report", "pipeline"):
        assert heavy not in loaded


def _code_without_strings(source_path: Path) -> str:
    """Source with every string literal blanked out.

    A raw text scan would trip over the module's own docstring, which explains
    that it uses no clock and no uuid. The ban is on calling those things, not
    on naming them.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


def test_the_module_reads_no_clock_and_rolls_no_dice():
    source_path = SRC / "investigation.py"
    code = _code_without_strings(source_path)

    for token in ("datetime", "time.time", "now(", "utcnow", "uuid", "random",
                  "urlopen", "socket"):
        assert token not in code, f"{token!r} must not be used in investigation.py"

    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }
    for forbidden in ("datetime", "time", "uuid", "random", "socket", "urllib"):
        assert forbidden not in imported, f"investigation.py must not import {forbidden}"


def test_the_module_performs_no_file_or_network_io():
    tree = ast.parse((SRC / "investigation.py").read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called
    assert "input" not in called


def test_no_ai_sdk_is_present_in_this_step():
    """Step 13B adds a real provider; 13A must not depend on one."""
    code = _code_without_strings(SRC / "investigation.py")
    for sdk in ("anthropic", "openai", "api_key", "API_KEY"):
        assert sdk not in code, f"{sdk!r} belongs to Step 13B, not 13A"

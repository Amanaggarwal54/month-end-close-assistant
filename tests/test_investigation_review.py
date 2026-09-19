"""Step 13C tests: advisory investigations stay review-side and advisory-only."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from investigation import InvalidInvestigationResult  # noqa: E402
from investigation_review import (  # noqa: E402
    INVESTIGATION_FILENAME,
    InvestigationWriteError,
    get_investigation,
    investigation_path_for,
    load_investigation_store,
    run_claude_investigation,
    run_gemini_investigation,
    run_investigation,
)
from paths import sha256_of  # noqa: E402
from review_workspace import write_workspace  # noqa: E402


TIMESTAMP = "2026-04-03T10:00:00Z"
FX_ID = "EXC-FXC-03-E4"


class FakeAdvisoryProvider:
    """Offline provider that records the packet it receives."""

    def __init__(self, *, summary: str = "The February FX rate differs from the reference."):
        self.packet = None
        self.summary = summary

    def investigate(self, packet):
        self.packet = json.loads(json.dumps(packet))
        return {
            "exception_id": packet["exception_id"],
            "summary": self.summary,
            "observations": ["The supplied rate is 1.19 while the reference is 1.09."],
            "possible_causes": ["The February FX input may have been supplied incorrectly."],
            "recommended_checks": ["Reconcile the February rate to the approved FX source."],
            "evidence_references": [
                {"type": "control_result", "reference": "FXC-03"}
            ],
            "uncertainties": ["The authoritative source document was not supplied."],
            "resolution_suggestion": "Confirm the approved February FX source and replace the incorrect input if confirmed.",
            "provider": "fake",
            "model": "offline-test",
            "machine_generated": True,
        }


def _hashes(directory: Path) -> dict[str, str]:
    return {path.name: sha256_of(path) for path in sorted(directory.iterdir()) if path.is_file()}


def make_fixture(tmp_path: Path):
    package = tmp_path / "out" / "E4"
    package.mkdir(parents=True)

    register = {"dataset_label": "E4", "placeholder": "close-time evidence"}
    (package / "exception_register.json").write_text(
        json.dumps(register, indent=2, sort_keys=True), encoding="utf-8"
    )
    (package / "control_results.csv").write_text(
        "check_id,status,explanation\n"
        "FXC-03,FAIL,February supplied FX rate 1.19 differs from reference 1.09\n"
        "MAT-01,PASS,All clean\n",
        encoding="utf-8",
    )

    manifest = {
        "dataset_label": "E4",
        "close_status": "FAIL",
        "report_allowed": False,
        "outputs": [
            {
                "role": "exception_register",
                "path": "exception_register.json",
                "sha256": sha256_of(package / "exception_register.json"),
            },
            {
                "role": "control_results",
                "path": "control_results.csv",
                "sha256": sha256_of(package / "control_results.csv"),
            },
        ],
    }
    (package / "package_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    workspace = {
        "workspace_version": "1.0.0",
        "workflow_version": "1.0.0",
        "dataset_label": "E4",
        "exception_count": 1,
        "source_package": {
            "package_path": str(package),
            "exception_register_sha256": sha256_of(package / "exception_register.json"),
            "package_manifest_sha256": sha256_of(package / "package_manifest.json"),
            "close_status": "FAIL",
            "report_allowed": False,
            "run_timestamp": "2026-03-31T18:00:00Z",
        },
        "exceptions": [
            {
                "exception_id": FX_ID,
                "control_id": "FXC-03",
                "name": "FX supplied rate accuracy",
                "group": "FX",
                "family": "SOURCE_ACCURACY",
                "severity": "CRITICAL",
                "source_status": "FAIL",
                "status": "OPEN",
                "owner": None,
                "message": "Supplied February FX rate differs from reference.",
                "error_reference": "E4",
                "details": {"reference_rate": 1.09, "supplied_rate": 1.19},
                "evidence": [],
                "resolution": None,
                "source_exception": {
                    "check_id": "FXC-03",
                    "status": "FAIL",
                    "severity": "CRITICAL",
                    "message": "Supplied February FX rate differs from reference.",
                },
                "history": [
                    {
                        "action": "CREATED",
                        "from_status": None,
                        "to_status": "OPEN",
                        "comment": None,
                        "owner": None,
                        "evidence": [],
                        "occurred_at": "2026-03-31T18:00:00Z",
                    }
                ],
            }
        ],
    }

    review = tmp_path / "review"
    workspace_path = review / "e4" / "exception_resolution.json"
    write_workspace(workspace, review)
    assert workspace_path.exists()
    return package, review, workspace_path, workspace


def test_run_gemini_investigation_uses_gemini_provider_and_stores_advice(monkeypatch, tmp_path: Path):
    package, review, workspace_path, workspace = make_fixture(tmp_path)
    captured = {}

    class FakeGeminiProvider:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def investigate(self, packet):
            captured["packet"] = json.loads(json.dumps(packet))
            return {
                "exception_id": packet["exception_id"],
                "summary": "Gemini found the February FX rate differs from the reference.",
                "observations": ["The supplied rate is 1.19 while the reference is 1.09."],
                "possible_causes": ["An incorrect FX source may have been used."],
                "recommended_checks": ["Verify the approved February month-end FX source."],
                "evidence_references": [{"type": "control_result", "reference": "FXC-03"}],
                "uncertainties": ["The approved external FX feed was not supplied."],
                "resolution_suggestion": "A human reviewer should verify the source and document the outcome.",
                "provider": "google-gemini",
                "model": "gemini-3.6-flash",
                "machine_generated": True,
            }

    fake_module = types.SimpleNamespace(GeminiInvestigationProvider=FakeGeminiProvider)
    monkeypatch.setitem(sys.modules, "gemini_provider", fake_module)

    record, written = run_gemini_investigation(
        workspace, FX_ID,
        review_dir=review, workspace_path=workspace_path,
        occurred_at=TIMESTAMP, source_files=["data/raw/fx_rates_expected.csv"],
        model="gemini-3.6-flash", max_tokens=512,
    )

    assert captured["init"] == {"model": "gemini-3.6-flash", "max_tokens": 512}
    assert captured["packet"]["exception_id"] == FX_ID
    assert record["advisory"]["provider"] == "google-gemini"
    assert record["advisory"]["machine_generated"] is True
    assert written.exists()


def test_run_investigation_stores_validated_advice_and_keeps_package_unchanged(tmp_path: Path):
    package, review, workspace_path, workspace = make_fixture(tmp_path)
    before_package = _hashes(package)
    before_workspace = workspace_path.read_bytes()
    provider = FakeAdvisoryProvider()

    record, written = run_investigation(
        workspace,
        FX_ID,
        provider,
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
        source_files=["data/raw/fx_rates_expected.csv"],
    )

    assert written == investigation_path_for(review, "E4")
    assert written.exists()
    assert record["exception_id"] == FX_ID
    assert record["advisory"]["provider"] == "fake"
    assert record["advisory"]["machine_generated"] is True
    assert _hashes(package) == before_package
    assert workspace_path.read_bytes() == before_workspace
    assert workspace["exceptions"][0]["status"] == "OPEN"



def test_packet_contains_only_the_selected_control_result_and_explicit_source_reference(tmp_path: Path):
    _, review, workspace_path, workspace = make_fixture(tmp_path)
    provider = FakeAdvisoryProvider()

    run_investigation(
        workspace,
        FX_ID,
        provider,
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
        source_files=["data/raw/fx_rates_expected.csv"],
    )

    packet = provider.packet
    assert packet is not None
    assert {"type": "source_file", "reference": "data/raw/fx_rates_expected.csv"} in packet["available_evidence"]
    assert {"type": "control_result", "reference": "FXC-03"} in packet["available_evidence"]
    assert packet["control_results"] == [
        {
            "check_id": "FXC-03",
            "status": "FAIL",
            "explanation": "February supplied FX rate 1.19 differs from reference 1.09",
        }
    ]
    assert "MAT-01" not in json.dumps(packet)



def test_forbidden_advice_is_rejected_and_not_persisted(tmp_path: Path):
    _, review, workspace_path, workspace = make_fixture(tmp_path)

    class BadProvider:
        def investigate(self, packet):
            return {
                "exception_id": packet["exception_id"],
                "summary": "A bad answer.",
                "observations": [],
                "possible_causes": [],
                "recommended_checks": [],
                "evidence_references": [],
                "uncertainties": [],
                "resolution_suggestion": "approve this close",
                "decision": {"approved": True},
            }

    with pytest.raises(InvalidInvestigationResult):
        run_investigation(
            workspace,
            FX_ID,
            BadProvider(),
            review_dir=review,
            workspace_path=workspace_path,
            occurred_at=TIMESTAMP,
        )

    assert not investigation_path_for(review, "E4").exists()
    assert workspace["exceptions"][0]["status"] == "OPEN"



def test_invented_evidence_is_rejected(tmp_path: Path):
    _, review, workspace_path, workspace = make_fixture(tmp_path)

    class InventingProvider:
        def investigate(self, packet):
            return {
                "exception_id": packet["exception_id"],
                "summary": "There is an invented source.",
                "observations": [],
                "possible_causes": [],
                "recommended_checks": [],
                "evidence_references": [
                    {"type": "source_file", "reference": "secret/approved_fx.xlsx"}
                ],
                "uncertainties": [],
                "resolution_suggestion": "Check the real approved source.",
            }

    with pytest.raises(InvalidInvestigationResult):
        run_investigation(
            workspace,
            FX_ID,
            InventingProvider(),
            review_dir=review,
            workspace_path=workspace_path,
            occurred_at=TIMESTAMP,
        )

    assert not investigation_path_for(review, "E4").exists()



def test_same_inputs_replace_duplicate_record_and_remain_byte_deterministic(tmp_path: Path):
    _, review, workspace_path, workspace = make_fixture(tmp_path)
    provider = FakeAdvisoryProvider()

    first, path = run_investigation(
        workspace,
        FX_ID,
        provider,
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
    )
    first_bytes = path.read_bytes()
    second, path = run_investigation(
        workspace,
        FX_ID,
        provider,
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
    )
    second_bytes = path.read_bytes()

    assert first["investigation_id"] == second["investigation_id"]
    assert first_bytes == second_bytes
    store = load_investigation_store(review, "E4")
    assert len(store["records"]) == 1



def test_different_advice_or_timestamp_creates_a_second_record(tmp_path: Path):
    _, review, workspace_path, workspace = make_fixture(tmp_path)

    first, _ = run_investigation(
        workspace,
        FX_ID,
        FakeAdvisoryProvider(summary="First review."),
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
    )
    second, _ = run_investigation(
        workspace,
        FX_ID,
        FakeAdvisoryProvider(summary="Second review."),
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at="2026-04-04T10:00:00Z",
    )

    assert first["investigation_id"] != second["investigation_id"]
    store = load_investigation_store(review, "E4")
    assert [r["investigation_id"] for r in store["records"]] == sorted(
        r["investigation_id"] for r in store["records"]
    )
    assert len(store["records"]) == 2
    assert get_investigation(review, "E4", first["investigation_id"])["exception_id"] == FX_ID



def test_writing_inside_package_is_refused(tmp_path: Path):
    package, review, workspace_path, workspace = make_fixture(tmp_path)
    provider = FakeAdvisoryProvider()

    with pytest.raises(InvestigationWriteError):
        run_investigation(
            workspace,
            FX_ID,
            provider,
            review_dir=package,
            workspace_path=workspace_path,
            occurred_at=TIMESTAMP,
        )

    assert _hashes(package)["control_results.csv"] == sha256_of(package / "control_results.csv")



def test_package_tampering_is_caught_before_provider_runs(tmp_path: Path):
    package, review, workspace_path, workspace = make_fixture(tmp_path)
    provider = FakeAdvisoryProvider()

    control_results = package / "control_results.csv"
    control_results.write_text(
        control_results.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="does not match its manifest"):
        run_investigation(
            workspace,
            FX_ID,
            provider,
            review_dir=review,
            workspace_path=workspace_path,
            occurred_at=TIMESTAMP,
        )

    assert provider.packet is None
    assert not investigation_path_for(review, "E4").exists()



def test_claude_wiring_is_lazy_and_requires_trusted_provenance(tmp_path: Path, monkeypatch):
    package, review, workspace_path, workspace = make_fixture(tmp_path)

    fake_module = types.ModuleType("claude_provider")

    class FakeClaudeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def investigate(self, packet):
            return {
                "exception_id": packet["exception_id"],
                "summary": "Claude-style advisory.",
                "observations": ["The supplied rate is higher than the reference."],
                "possible_causes": ["Incorrect FX source input."],
                "recommended_checks": ["Check the approved FX reference file."],
                "evidence_references": [
                    {"type": "control_result", "reference": "FXC-03"}
                ],
                "uncertainties": [],
                "resolution_suggestion": "Validate the February source rate.",
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "machine_generated": True,
            }

    fake_module.ClaudeInvestigationProvider = FakeClaudeProvider
    monkeypatch.setitem(sys.modules, "claude_provider", fake_module)

    record, path = run_claude_investigation(
        workspace,
        FX_ID,
        review_dir=review,
        workspace_path=workspace_path,
        occurred_at=TIMESTAMP,
        model="claude-sonnet-5",
        max_tokens=512,
        structured_output_mode="auto",
    )

    assert path.exists()
    assert record["advisory"]["provider"] == "anthropic"
    assert record["advisory"]["machine_generated"] is True


from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_cli  # noqa: E402


def minimal_workspace(tmp_path: Path):
    review = tmp_path / "review"
    path = review / "e4" / "exception_resolution.json"
    path.parent.mkdir(parents=True)
    workspace = {
        "workspace_version": "1.0.0",
        "workflow_version": "1.0.0",
        "dataset_label": "E4",
        "exception_count": 0,
        "source_package": {
            "package_path": str(tmp_path / "package"),
            "exception_register_sha256": "a" * 64,
            "package_manifest_sha256": "b" * 64,
        },
        "exceptions": [],
    }
    path.write_text(json.dumps(workspace, indent=2, sort_keys=True), encoding="utf-8")
    return review


def test_parser_exposes_investigate():
    parser = review_cli.build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert "investigate" in action.choices


def test_investigate_command_calls_review_side_wiring(monkeypatch, tmp_path):
    review = minimal_workspace(tmp_path)
    observed = {}

    def fake_run(workspace, exception_id, **kwargs):
        observed["exception_id"] = exception_id
        observed["kwargs"] = kwargs
        record = {
            "investigation_id": "INV-TEST",
            "exception_id": exception_id,
            "occurred_at": kwargs["occurred_at"],
            "advisory": {
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "machine_generated": True,
            },
        }
        return record, review / "E4" / "investigation_advice.json"

    monkeypatch.setattr(review_cli, "run_claude_investigation", fake_run)

    stream = io.StringIO()
    code = review_cli.main([
        "investigate",
        "--dataset-label", "E4",
        "--review-dir", str(review),
        "--exception-id", "EXC-FXC-03-E4",
        "--occurred-at", "2026-04-03T10:00:00Z",
        "--model", "claude-sonnet-5",
        "--max-tokens", "512",
        "--structured-output-mode", "output_config",
        "--source-file", "data/raw/fx_rates_expected.csv",
    ], out=stream)

    assert code == review_cli.EXIT_OK
    assert observed["exception_id"] == "EXC-FXC-03-E4"
    assert observed["kwargs"]["model"] == "claude-sonnet-5"
    assert observed["kwargs"]["max_tokens"] == 512
    assert observed["kwargs"]["structured_output_mode"] == "output_config"
    assert observed["kwargs"]["source_files"] == ["data/raw/fx_rates_expected.csv"]
    assert "INV-TEST" in stream.getvalue()

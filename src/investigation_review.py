"""
Month-End Close Assistant - Step 13C: store advisory investigations beside review.

This module is the persistence/orchestration boundary between the immutable
review workspace and an InvestigationProvider. It does not decide anything and
it never writes into the close package.

Flow::

    immutable close package
            |
      review workspace
            |
      build investigation packet
            |
      InvestigationProvider (Claude in the real path)
            |
      13A validation gate
            |
      investigation_advice.json  <-- mutable review-side evidence

The provider remains advisory. A stored result cannot change a control result,
close decision, exception status, or the immutable exception register.

Timestamps are caller-supplied. No clock, random value or UUID is generated
here, so the same inputs produce the same stored record and the same file.
"""

from __future__ import annotations

import csv
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

from investigation import (
    InvestigationProvider,
    investigate_exception,
    validate_investigation_result,
)
from paths import display_path, sha256_of, slugify
from review_workspace import validate_workspace, verify_package_integrity

__all__ = [
    "INVESTIGATION_STORE_VERSION",
    "INVESTIGATION_FILENAME",
    "InvestigationStoreError",
    "InvestigationWriteError",
    "investigation_path_for",
    "load_investigation_store",
    "write_investigation_store",
    "build_advisory_record",
    "store_investigation",
    "run_investigation",
    "run_claude_investigation",
    "run_gemini_investigation",
    "get_investigation",
]

INVESTIGATION_STORE_VERSION = "1.0.0"
INVESTIGATION_FILENAME = "investigation_advice.json"
CONTROL_RESULTS_FILENAME = "control_results.csv"


class InvestigationStoreError(RuntimeError):
    """Raised when advisory investigation storage is structurally unusable."""


class InvestigationWriteError(RuntimeError):
    """Raised when an investigation result would be written into the package."""


def investigation_path_for(review_dir: str | Path, dataset_label: str) -> Path:
    """Return ``<review_dir>/<dataset_label>/investigation_advice.json``."""
    return Path(review_dir) / slugify(dataset_label) / INVESTIGATION_FILENAME


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _refuse_inside_package(path: str | Path, package_dir: str | Path) -> Path:
    candidate = _resolved(path)
    package = _resolved(package_dir)
    if _is_inside(candidate, package):
        raise InvestigationWriteError(
            f"refusing to write investigation advice to {candidate}: "
            f"it is inside the immutable close package at {package}"
        )
    return candidate


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"investigation file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InvestigationStoreError(f"investigation file must contain an object: {path}")
    return payload


def _validate_store(store: Mapping[str, Any], dataset_label: str | None = None) -> None:
    if not isinstance(store, Mapping):
        raise TypeError("investigation store must be a mapping")
    if store.get("investigation_store_version") != INVESTIGATION_STORE_VERSION:
        raise InvestigationStoreError("Unsupported investigation store version.")
    label = store.get("dataset_label")
    if not isinstance(label, str) or not label.strip():
        raise InvestigationStoreError("investigation store must carry a dataset_label.")
    if dataset_label is not None and label != dataset_label:
        raise InvestigationStoreError(
            f"investigation store belongs to {label!r}, not {dataset_label!r}."
        )
    source = store.get("source_workspace")
    if not isinstance(source, Mapping):
        raise InvestigationStoreError("investigation store must record source_workspace.")
    for field in ("path", "sha256"):
        if not source.get(field):
            raise InvestigationStoreError(f"source_workspace is missing {field}.")
    records = store.get("records")
    if not isinstance(records, list):
        raise InvestigationStoreError("investigation store must contain a records list.")
    ids: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise InvestigationStoreError("every investigation record must be an object.")
        record_id = record.get("investigation_id")
        exception_id = record.get("exception_id")
        if not isinstance(record_id, str) or not record_id:
            raise InvestigationStoreError("every investigation record needs investigation_id.")
        if not isinstance(exception_id, str) or not exception_id:
            raise InvestigationStoreError("every investigation record needs exception_id.")
        ids.append(record_id)
    if len(ids) != len(set(ids)):
        raise InvestigationStoreError("investigation_id values must be unique.")


def _empty_store(workspace: Mapping[str, Any], workspace_path: str | Path, workspace_sha256: str) -> dict[str, Any]:
    source = deepcopy(workspace.get("source_package") or {})
    return {
        "investigation_store_version": INVESTIGATION_STORE_VERSION,
        "dataset_label": workspace.get("dataset_label"),
        "source_workspace": {
            "path": display_path(workspace_path),
            "sha256": workspace_sha256,
            "source_package": source,
        },
        "records": [],
    }


def load_investigation_store(
    review_dir: str | Path,
    dataset_label: str,
    *,
    workspace: Mapping[str, Any] | None = None,
    workspace_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load a store, or create an in-memory empty one from the workspace."""
    path = investigation_path_for(review_dir, dataset_label)
    if not path.exists():
        if workspace is None or workspace_path is None:
            raise FileNotFoundError(f"no investigation advice at {path}")
        workspace_sha256 = sha256_of(workspace_path)
        store = _empty_store(workspace, workspace_path, workspace_sha256)
        _validate_store(store, dataset_label)
        return store

    store = _read_json(path)
    _validate_store(store, dataset_label)
    return deepcopy(store)


def write_investigation_store(
    store: Mapping[str, Any],
    review_dir: str | Path,
    *,
    package_dir: str | Path,
) -> Path:
    """Write deterministic review-side advice, never into the close package."""
    _validate_store(store)
    destination = _refuse_inside_package(
        investigation_path_for(review_dir, str(store["dataset_label"])),
        package_dir,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(store, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return destination


def _investigation_id(
    *,
    dataset_label: str,
    exception_id: str,
    occurred_at: str,
    workspace_sha256: str,
    advisory: Mapping[str, Any],
) -> str:
    material = {
        "dataset_label": dataset_label,
        "exception_id": exception_id,
        "occurred_at": occurred_at,
        "workspace_sha256": workspace_sha256,
        "advisory": advisory,
    }
    digest = hashlib.sha256(_stable_json(material).encode("utf-8")).hexdigest()[:12]
    return f"INV-{slugify(exception_id).upper()}-{digest}"


def build_advisory_record(
    workspace: Mapping[str, Any],
    exception_id: str,
    advisory: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
    occurred_at: str,
    workspace_path: str | Path,
) -> dict[str, Any]:
    """Validate and build one advisory record from a complete investigation packet."""
    validate_workspace(dict(workspace))
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise ValueError("occurred_at must be a non-empty caller-supplied string.")
    validated = validate_investigation_result(advisory, packet)
    return _record_from_validated(
        workspace,
        exception_id,
        validated,
        occurred_at=occurred_at,
        workspace_path=workspace_path,
    )

def _record_from_validated(
    workspace: Mapping[str, Any],
    exception_id: str,
    advisory: Mapping[str, Any],
    *,
    occurred_at: str,
    workspace_path: str | Path,
) -> dict[str, Any]:
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise ValueError("occurred_at must be a non-empty caller-supplied string.")
    workspace_sha256 = sha256_of(workspace_path)
    advisory_copy = deepcopy(dict(advisory))
    record_id = _investigation_id(
        dataset_label=str(workspace.get("dataset_label")),
        exception_id=exception_id,
        occurred_at=occurred_at,
        workspace_sha256=workspace_sha256,
        advisory=advisory_copy,
    )
    return {
        "investigation_id": record_id,
        "exception_id": exception_id,
        "occurred_at": occurred_at,
        "workspace_sha256": workspace_sha256,
        "source_package": deepcopy(workspace.get("source_package") or {}),
        "advisory": advisory_copy,
    }


def _upsert_record(store: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    updated = deepcopy(dict(store))
    records = [
        deepcopy(item)
        for item in updated.get("records", [])
        if item.get("investigation_id") != record.get("investigation_id")
    ]
    records.append(deepcopy(dict(record)))
    records.sort(key=lambda item: (str(item.get("exception_id", "")), str(item.get("investigation_id", ""))))
    updated["records"] = records
    return updated


def store_investigation(
    workspace: Mapping[str, Any],
    exception_id: str,
    advisory: Mapping[str, Any],
    *,
    occurred_at: str,
    review_dir: str | Path,
    workspace_path: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Persist an already validated advisory and return (record, path)."""
    validate_workspace(dict(workspace))
    record = _record_from_validated(
        workspace,
        exception_id,
        advisory,
        occurred_at=occurred_at,
        workspace_path=workspace_path,
    )

    package_dir = (workspace.get("source_package") or {}).get("package_path")
    if not package_dir:
        raise InvestigationStoreError("workspace does not identify its source package.")

    # Verify again at the write boundary: even a package changed after the
    # provider call must never be paired with newly stored advice.
    verify_package_integrity(package_dir)

    store = load_investigation_store(
        review_dir,
        str(workspace["dataset_label"]),
        workspace=workspace,
        workspace_path=workspace_path,
    )
    updated = _upsert_record(store, record)
    write_investigation_store(updated, review_dir, package_dir=package_dir)
    return record, investigation_path_for(review_dir, str(workspace["dataset_label"]))


def _read_control_results(package_dir: str | Path) -> list[dict[str, str]]:
    path = _resolved(package_dir) / CONTROL_RESULTS_FILENAME
    if not path.exists():
        raise InvestigationStoreError(
            f"the close package is missing {CONTROL_RESULTS_FILENAME}: {path}"
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _run_provider(
    workspace: Mapping[str, Any],
    exception_id: str,
    provider: InvestigationProvider,
    *,
    source_files: Iterable[Any] | None,
) -> dict[str, Any]:
    validate_workspace(dict(workspace))
    package_dir = (workspace.get("source_package") or {}).get("package_path")
    if not package_dir:
        raise InvestigationStoreError("workspace does not identify its source package.")

    # Re-check the immutable evidence immediately before the provider sees it.
    verify_package_integrity(package_dir)
    control_results = _read_control_results(package_dir)

    return investigate_exception(
        workspace,
        exception_id,
        provider,
        source_files=source_files,
        control_results=control_results,
    )


def run_investigation(
    workspace: Mapping[str, Any],
    exception_id: str,
    provider: InvestigationProvider,
    *,
    review_dir: str | Path,
    workspace_path: str | Path,
    occurred_at: str,
    source_files: Iterable[Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Run one provider investigation and persist the validated advisory."""
    advisory = _run_provider(
        workspace,
        exception_id,
        provider,
        source_files=source_files,
    )

    return store_investigation(
        workspace,
        exception_id,
        advisory,
        occurred_at=occurred_at,
        review_dir=review_dir,
        workspace_path=workspace_path,
    )


def run_claude_investigation(
    workspace: Mapping[str, Any],
    exception_id: str,
    *,
    review_dir: str | Path,
    workspace_path: str | Path,
    occurred_at: str,
    source_files: Iterable[Any] | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    structured_output_mode: str = "auto",
) -> tuple[dict[str, Any], Path]:
    """Construct the real Claude provider lazily, then run/store one advisory."""
    from claude_provider import ClaudeInvestigationProvider

    provider = ClaudeInvestigationProvider(
        model=model,
        max_tokens=max_tokens,
        structured_output_mode=structured_output_mode,
    )
    advisory = _run_provider(
        workspace,
        exception_id,
        provider,
        source_files=source_files,
    )

    if advisory.get("provider") != "anthropic" or advisory.get("machine_generated") is not True:
        raise InvestigationStoreError(
            "Claude investigation did not return trusted Anthropic machine-generated provenance."
        )

    return store_investigation(
        workspace,
        exception_id,
        advisory,
        occurred_at=occurred_at,
        review_dir=review_dir,
        workspace_path=workspace_path,
    )


def run_gemini_investigation(
    workspace: Mapping[str, Any],
    exception_id: str,
    *,
    review_dir: str | Path,
    workspace_path: str | Path,
    occurred_at: str,
    source_files: Iterable[Any] | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
) -> tuple[dict[str, Any], Path]:
    """Construct the Gemini provider lazily, then run/store one advisory."""
    from gemini_provider import GeminiInvestigationProvider

    provider = GeminiInvestigationProvider(
        model=model,
        max_tokens=max_tokens,
    )
    advisory = _run_provider(
        workspace,
        exception_id,
        provider,
        source_files=source_files,
    )

    if advisory.get("provider") != "google-gemini" or advisory.get("machine_generated") is not True:
        raise InvestigationStoreError(
            "Gemini investigation did not return trusted Google Gemini machine-generated provenance."
        )

    return store_investigation(
        workspace,
        exception_id,
        advisory,
        occurred_at=occurred_at,
        review_dir=review_dir,
        workspace_path=workspace_path,
    )


def get_investigation(
    review_dir: str | Path,
    dataset_label: str,
    investigation_id: str,
) -> dict[str, Any]:
    """Return a defensive copy of one stored advisory record."""
    store = load_investigation_store(review_dir, dataset_label)
    for record in store["records"]:
        if record.get("investigation_id") == investigation_id:
            return deepcopy(record)
    raise KeyError(f"Unknown investigation_id: {investigation_id}")

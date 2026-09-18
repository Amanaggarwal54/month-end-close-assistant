"""
Month-End Close Assistant - Step 12D: the post-close review workspace.

A close package under ``out/<dataset_label>/`` is evidence of what the close
looked like when it was run. It is finished the moment it is written, and
nothing here ever writes into it again.

Investigation happens somewhere else:

    out/<dataset_label>/exception_register.json    immutable close-time snapshot
    review/<dataset_label>/exception_resolution.json   mutable review record

The direction of truth only ever runs one way::

    control results -> close decision -> immutable package -> review workspace

A RESOLVED exception in a workspace records that somebody investigated a
failure. It does not unmake the failure, and nothing in this module can reach
back into a decision that was already taken.

Responsibilities
----------------
This module owns package provenance, package integrity, workspace creation and
workspace persistence - the file I/O that ``exception_workflow.py`` must never
acquire. It owns no lifecycle rules: statuses, allowed transitions, resolution
requirements and history all remain in ``exception_workflow.py``, which this
module calls rather than reimplements.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from exception_workflow import (
    assign_owner,
    build_exception_register,  # noqa: F401 - re-exported for callers building a workspace by hand
    get_exception,
    transition_exception,
    validate_register,
)
from report import display_path, sha256_of, slugify

__all__ = [
    "WORKSPACE_VERSION",
    "WORKSPACE_FILENAME",
    "PackageIntegrityError",
    "WorkspaceWriteError",
    "create_resolution_workspace",
    "load_workspace",
    "write_workspace",
    "workspace_path_for",
    "verify_package_integrity",
    "review_assign_owner",
    "review_transition_exception",
    "get_review_exception",
    "validate_workspace",
]

WORKSPACE_VERSION = "1.0.0"
WORKSPACE_FILENAME = "exception_resolution.json"

REGISTER_FILENAME = "exception_register.json"
MANIFEST_FILENAME = "package_manifest.json"

#: The manifest role naming the immutable register inside a close package.
REGISTER_ROLE = "exception_register"


class PackageIntegrityError(RuntimeError):
    """Raised when a close package does not agree with its own manifest."""


class WorkspaceWriteError(RuntimeError):
    """Raised when a write would land inside the immutable close package."""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_inside(candidate: Path, parent: Path) -> bool:
    """True when ``candidate`` is ``parent`` or sits underneath it."""
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _refuse_inside_package(target: str | Path, package_dir: str | Path) -> Path:
    """Refuse any workspace path that would write into the close package.

    The guard is a hard failure rather than a warning, for the same reason the
    error injector refuses ``data/raw``: the value of the package is that it has
    not been touched since it was written.
    """
    destination = _resolved(target)
    package = _resolved(package_dir)

    if _is_inside(destination, package):
        raise WorkspaceWriteError(
            f"refusing to write to {destination}: it is inside the immutable "
            f"close package at {package}"
        )

    return destination


def workspace_path_for(review_dir: str | Path, dataset_label: str) -> Path:
    """``<review_dir>/<dataset_label>/exception_resolution.json``.

    Mirrors how the close package lays out ``out/<dataset_label>/``, and slugs
    the label so it can never escape the review directory.
    """
    return Path(review_dir) / slugify(dataset_label) / WORKSPACE_FILENAME


# ---------------------------------------------------------------------------
# reading and verifying a close package
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"close package file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_package_integrity(package_dir: str | Path) -> dict[str, Any]:
    """Check a close package against its own manifest and return the manifest.

    Every output the manifest records is re-hashed and compared, not only the
    exception register: a package whose PDF or control results were edited is
    not a package a reviewer should be allowed to build on, even if the register
    itself still matches.

    The one thing this cannot attest to is the manifest's own non-hash content.
    A manifest cannot contain a stable hash of itself, so a close_status edited
    inside the manifest is undetectable from within the package. Detecting that
    needs a signature or an external record, which this project does not have.
    """
    package = _resolved(package_dir)
    manifest = _read_json(package / MANIFEST_FILENAME)

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise PackageIntegrityError(
            f"{package / MANIFEST_FILENAME} records no outputs to verify."
        )

    roles = {str(entry.get("role")) for entry in outputs}
    if REGISTER_ROLE not in roles:
        raise PackageIntegrityError(
            f"the manifest does not record {REGISTER_FILENAME}; this package "
            "predates the exception register and cannot seed a workspace."
        )

    mismatches: list[str] = []
    missing: list[str] = []

    for entry in outputs:
        role = str(entry.get("role"))
        recorded = entry.get("sha256")
        # The manifest stores the path as written; resolve it against the
        # package so a workspace can be created from a moved or copied package.
        name = Path(str(entry.get("path", ""))).name
        artefact = package / name

        if not artefact.exists():
            missing.append(f"{role} ({name})")
            continue

        actual = sha256_of(artefact)
        if actual != recorded:
            mismatches.append(
                f"{role} ({name}): manifest records {recorded}, file hashes {actual}"
            )

    if missing or mismatches:
        details = "; ".join(missing + mismatches)
        raise PackageIntegrityError(
            f"close package at {package} does not match its manifest: {details}"
        )

    return manifest


# ---------------------------------------------------------------------------
# building a workspace
# ---------------------------------------------------------------------------
def build_workspace(
    register: dict[str, Any],
    manifest: dict[str, Any],
    package_dir: str | Path,
    register_sha256: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Assemble the workspace document. Pure: no file I/O, no mutation.

    The exception entries start as exact copies of the immutable register
    entries, so the first thing a workspace says is what the close said. The
    recorded hashes are what prove which package it came from, and
    ``workflow_version`` and ``exception_count`` are carried through so
    ``exception_workflow.validate_register`` can validate the workspace itself.
    """
    exceptions = deepcopy(register.get("exceptions", []))

    return {
        "workspace_version": WORKSPACE_VERSION,
        "workflow_version": register.get("workflow_version"),
        "dataset_label": register.get("dataset_label")
        or manifest.get("dataset_label"),
        "exception_count": len(exceptions),
        "source_package": {
            "package_path": display_path(package_dir),
            "exception_register_sha256": register_sha256,
            "package_manifest_sha256": manifest_sha256,
            "close_status": manifest.get("close_status"),
            "report_allowed": manifest.get("report_allowed"),
            "run_timestamp": manifest.get("run_timestamp"),
        },
        "exceptions": exceptions,
    }


def create_resolution_workspace(
    package_dir: str | Path,
    review_dir: str | Path,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Initialise a reviewer workspace from an existing close package.

    Reads ``exception_register.json`` and ``package_manifest.json``, refuses a
    package that disagrees with its own manifest, and writes the workspace to
    ``<review_dir>/<dataset_label>/exception_resolution.json``.

    The close package is opened read-only and is never written to. Passing
    ``write=False`` returns the workspace without touching the filesystem at
    all, which is what the pure tests use.
    """
    package = _resolved(package_dir)
    manifest = verify_package_integrity(package)

    register_path = package / REGISTER_FILENAME
    register = _read_json(register_path)

    workspace = build_workspace(
        register=register,
        manifest=manifest,
        package_dir=package,
        register_sha256=sha256_of(register_path),
        manifest_sha256=sha256_of(package / MANIFEST_FILENAME),
    )

    if write:
        write_workspace(workspace, review_dir)

    return workspace


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def write_workspace(workspace: dict[str, Any], review_dir: str | Path) -> Path:
    """Write the workspace deterministically, never inside the close package.

    UTF-8, two-space indent, sorted keys - the same convention the close package
    uses - so writing the same workspace twice is byte-identical. No timestamp
    is generated here; every timestamp in the document came from a caller.
    """
    label = workspace.get("dataset_label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("workspace must carry a non-empty dataset_label.")

    destination = workspace_path_for(review_dir, label)

    package_path = (workspace.get("source_package") or {}).get("package_path")
    if package_path:
        destination = _refuse_inside_package(destination, package_path)
    else:
        destination = _resolved(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(workspace, indent=2, sort_keys=True), encoding="utf-8"
    )
    return destination


def load_workspace(review_dir: str | Path, dataset_label: str) -> dict[str, Any]:
    """Read a workspace back from the review directory."""
    path = workspace_path_for(review_dir, dataset_label)
    if not path.exists():
        raise FileNotFoundError(f"no review workspace at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# review operations - delegated, never reimplemented
# ---------------------------------------------------------------------------
def review_assign_owner(
    workspace: dict[str, Any],
    exception_id: str,
    owner: str | None,
    *,
    comment: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Assign an owner in the workspace, using the workflow engine.

    The workspace is register-shaped, so it is handed straight to
    ``exception_workflow.assign_owner``: ownership rules live there and are not
    restated here.
    """
    return assign_owner(
        workspace,
        exception_id,
        owner,
        comment=comment,
        occurred_at=occurred_at,
    )


def review_transition_exception(
    workspace: dict[str, Any],
    exception_id: str,
    new_status: str,
    *,
    comment: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Move an exception through its lifecycle, using the workflow engine.

    Allowed transitions, the terminal RESOLVED state, and the comment and
    evidence a resolution requires are all enforced by
    ``exception_workflow.transition_exception``. This module adds nothing to
    them, so a rule can never be enforced in one place and not the other.
    """
    return transition_exception(
        workspace,
        exception_id,
        new_status,
        comment=comment,
        evidence=evidence,
        occurred_at=occurred_at,
    )


def get_review_exception(
    workspace: dict[str, Any],
    exception_id: str,
) -> dict[str, Any]:
    """Return a defensive copy of one workspace exception."""
    return get_exception(workspace, exception_id)


def validate_workspace(workspace: dict[str, Any]) -> None:
    """Validate the workspace envelope, then its exceptions via the engine."""
    if not isinstance(workspace, dict):
        raise TypeError("workspace must be a dictionary.")

    if workspace.get("workspace_version") != WORKSPACE_VERSION:
        raise ValueError("Unsupported workspace version.")

    source = workspace.get("source_package")
    if not isinstance(source, dict):
        raise ValueError("workspace must record its source_package.")

    for field_name in ("package_path", "exception_register_sha256",
                       "package_manifest_sha256"):
        if not source.get(field_name):
            raise ValueError(f"workspace source_package is missing {field_name}.")

    # Lifecycle invariants are the workflow engine's to judge, not this module's.
    validate_register(workspace)

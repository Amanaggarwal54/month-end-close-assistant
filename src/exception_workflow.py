"""
Exception resolution workflow.

This module manages investigation and resolution metadata for exceptions
already produced by the deterministic close-control layer.

Design principles:
- Does not calculate accounting controls.
- Does not change control results.
- Does not make or override close decisions.
- Does not perform file I/O.
- Does not call an LLM.
- Uses deterministic state transitions and identifiers.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable

WORKFLOW_VERSION = "1.0.0"

OPEN = "OPEN"
INVESTIGATING = "INVESTIGATING"
RESOLVED = "RESOLVED"

VALID_STATUSES = {OPEN, INVESTIGATING, RESOLVED}

_ALLOWED_TRANSITIONS = {
    OPEN: {INVESTIGATING},
    INVESTIGATING: {OPEN, RESOLVED},
    RESOLVED: set(),
}


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a dictionary.")
    return value


def _stable_json(value: Any) -> str:
    """Serialize a value deterministically for hashing/comparison."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _slug(value: str) -> str:
    """Convert a string into a stable identifier-safe token."""
    token = re.sub(r"[^A-Za-z0-9]+", "-", str(value).strip())
    token = token.strip("-")
    return token or "UNKNOWN"


def _source_fingerprint(exception: dict[str, Any]) -> str:
    """
    Generate a deterministic fingerprint for a source exception.

    The fingerprint is only used to avoid collisions when two source
    exceptions share the same control/error reference.
    """
    payload = {
        "check_id": exception.get("check_id"),
        "error_reference": exception.get("error_reference"),
        "details": exception.get("details"),
        "message": exception.get("message"),
    }

    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:8]


def make_exception_id(exception: dict[str, Any]) -> str:
    """
    Generate a deterministic exception ID.

    Examples:
        EXC-FXC-03-E4
        EXC-MAT-03-E1-E3-xxxxxxxx

    The basic identifier uses the control ID and error reference.
    A short deterministic fingerprint is added only when needed to
    distinguish otherwise identical source exceptions.
    """
    _require_mapping(exception, "exception")

    check_id = _slug(exception.get("check_id", "UNKNOWN"))
    error_reference = _slug(
        exception.get("error_reference") or "SOURCE"
    )

    return f"EXC-{check_id}-{error_reference}"


def _unique_exception_ids(
    exceptions: list[dict[str, Any]],
) -> list[str]:
    """
    Generate deterministic, collision-safe IDs for a sorted exception list.
    """
    result: list[str] = []
    used: set[str] = set()

    for exception in exceptions:
        base_id = make_exception_id(exception)

        if base_id not in used:
            exception_id = base_id
        else:
            exception_id = f"{base_id}-{_source_fingerprint(exception)}"

            if exception_id in used:
                counter = 2
                candidate = f"{exception_id}-{counter}"

                while candidate in used:
                    counter += 1
                    candidate = f"{exception_id}-{counter}"

                exception_id = candidate

        used.add(exception_id)
        result.append(exception_id)

    return result


def _first_present(
    exception: dict[str, Any],
    *field_names: str,
) -> Any:
    """
    Return the first field that the source exception actually carries.

    Audit exception records name their descriptive fields ``check_name``,
    ``check_group`` and ``control_family``. Reading only the short spellings
    would silently store ``None`` for every real exception, so both spellings
    are accepted and the audit one is preferred.
    """
    for field_name in field_names:
        if field_name in exception:
            return exception[field_name]

    return None


def _exception_sort_key(exception: dict[str, Any]) -> tuple[str, ...]:
    """Stable ordering for register creation."""
    return (
        str(exception.get("severity", "")),
        str(exception.get("check_id", "")),
        str(exception.get("error_reference", "")),
        _stable_json(exception.get("details", {})),
        str(exception.get("message", "")),
    )


def _validate_source_exception(exception: dict[str, Any]) -> None:
    """Validate the minimum structure expected from audit exceptions."""
    required = {"check_id", "severity", "status", "message"}

    missing = sorted(field for field in required if field not in exception)

    if missing:
        raise ValueError(
            f"Source exception is missing required fields: {missing}"
        )

    status = exception["status"]

    if status not in {"FAIL", "ERROR"}:
        raise ValueError(
            f"Source exception status must be FAIL or ERROR, got {status!r}."
        )


def build_exception_register(
    exception_records: Iterable[dict[str, Any]],
    dataset_label: str,
) -> dict[str, Any]:
    """
    Build the initial exception register from audit exception records.

    The source exception is copied into each workflow record so the original
    control evidence remains available and cannot be silently changed.

    No timestamps are generated here because generated timestamps would make
    identical inputs produce different outputs.
    """
    if not isinstance(dataset_label, str) or not dataset_label.strip():
        raise ValueError("dataset_label must be a non-empty string.")

    source = [deepcopy(_require_mapping(item, "exception_record"))
              for item in exception_records]

    for exception in source:
        _validate_source_exception(exception)

    source.sort(key=_exception_sort_key)
    ids = _unique_exception_ids(source)

    exceptions: list[dict[str, Any]] = []

    for exception, exception_id in zip(source, ids):
        exceptions.append(
            {
                "exception_id": exception_id,
                "control_id": exception.get("check_id"),
                "name": _first_present(exception, "check_name", "name"),
                "group": _first_present(exception, "check_group", "group"),
                "family": _first_present(exception, "control_family", "family"),
                "severity": exception.get("severity"),
                "source_status": exception.get("status"),
                "status": OPEN,
                "owner": None,
                "message": exception.get("message"),
                "error_reference": exception.get(
                    "error_reference"
                ),
                "details": deepcopy(exception.get("details", {})),
                "evidence": [],
                "resolution": None,
                "source_exception": exception,
                "history": [
                    {
                        "action": "CREATED",
                        "from_status": None,
                        "to_status": OPEN,
                        "comment": None,
                        "owner": None,
                        "evidence": [],
                    }
                ],
            }
        )

    return {
        "workflow_version": WORKFLOW_VERSION,
        "dataset_label": dataset_label,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
    }


def _find_exception(
    register: dict[str, Any],
    exception_id: str,
) -> dict[str, Any]:
    """Return an exception from the register or raise a clear error."""
    _require_mapping(register, "register")

    exceptions = register.get("exceptions")

    if not isinstance(exceptions, list):
        raise ValueError("Register must contain an 'exceptions' list.")

    for exception in exceptions:
        if exception.get("exception_id") == exception_id:
            return exception

    raise KeyError(f"Unknown exception_id: {exception_id}")


def _copy_register(register: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(_require_mapping(register, "register"))


def assign_owner(
    register: dict[str, Any],
    exception_id: str,
    owner: str | None,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    """
    Assign or clear an owner.

    Owner changes are recorded in history but do not change exception status.
    """
    if owner is not None:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string or None.")
        owner = owner.strip()

    updated = _copy_register(register)
    exception = _find_exception(updated, exception_id)

    previous_owner = exception["owner"]
    exception["owner"] = owner

    exception["history"].append(
        {
            "action": "OWNER_CHANGED",
            "from_status": exception["status"],
            "to_status": exception["status"],
            "comment": comment,
            "owner": owner,
            "previous_owner": previous_owner,
            "evidence": [],
        }
    )

    return updated


def transition_exception(
    register: dict[str, Any],
    exception_id: str,
    new_status: str,
    *,
    comment: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Move an exception through the allowed lifecycle.

    Valid transitions:
        OPEN -> INVESTIGATING
        INVESTIGATING -> OPEN
        INVESTIGATING -> RESOLVED

    RESOLVED is terminal.

    RESOLVED requires:
        - a non-empty comment
        - at least one evidence reference
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {new_status!r}. "
            f"Expected one of {sorted(VALID_STATUSES)}."
        )

    if comment is not None:
        if not isinstance(comment, str) or not comment.strip():
            raise ValueError(
                "comment must be a non-empty string or None."
            )
        comment = comment.strip()

    if evidence is None:
        evidence = []

    if not isinstance(evidence, list):
        raise TypeError("evidence must be a list.")

    evidence_copy = deepcopy(evidence)

    for item in evidence_copy:
        if not isinstance(item, dict):
            raise TypeError(
                "Each evidence item must be a dictionary."
            )

    updated = _copy_register(register)
    exception = _find_exception(updated, exception_id)

    current_status = exception["status"]

    if new_status == current_status:
        raise ValueError(
            f"Exception {exception_id} is already {current_status}."
        )

    if current_status == RESOLVED:
        raise ValueError(
            f"Exception {exception_id} cannot leave RESOLVED status."
        )

    allowed = _ALLOWED_TRANSITIONS.get(current_status, set())

    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition for {exception_id}: "
            f"{current_status} -> {new_status}."
        )

    if new_status == RESOLVED:
        if not comment:
            raise ValueError(
                "A resolution comment is required before RESOLVED."
            )

        if not evidence_copy:
            raise ValueError(
                "At least one evidence reference is required "
                "before RESOLVED."
            )

        exception["resolution"] = {
            "comment": comment,
            "evidence": evidence_copy,
        }

    exception["status"] = new_status

    if evidence_copy:
        exception["evidence"].extend(evidence_copy)

    exception["history"].append(
        {
            "action": "STATUS_CHANGED",
            "from_status": current_status,
            "to_status": new_status,
            "comment": comment,
            "owner": exception["owner"],
            "evidence": evidence_copy,
        }
    )

    return updated


def get_exception(
    register: dict[str, Any],
    exception_id: str,
) -> dict[str, Any]:
    """Return a defensive copy of one exception."""
    return deepcopy(_find_exception(register, exception_id))


def validate_register(register: dict[str, Any]) -> None:
    """
    Validate structural and lifecycle invariants.

    Raises ValueError when the register is invalid.
    """
    _require_mapping(register, "register")

    if register.get("workflow_version") != WORKFLOW_VERSION:
        raise ValueError("Unsupported workflow version.")

    exceptions = register.get("exceptions")

    if not isinstance(exceptions, list):
        raise ValueError("Register must contain an 'exceptions' list.")

    if register.get("exception_count") != len(exceptions):
        raise ValueError(
            "exception_count does not match number of exceptions."
        )

    ids = [item.get("exception_id") for item in exceptions]

    if any(not isinstance(item, str) or not item for item in ids):
        raise ValueError("Every exception must have a valid exception_id.")

    if len(ids) != len(set(ids)):
        raise ValueError("Exception IDs must be unique.")

    for exception in exceptions:
        status = exception.get("status")

        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid exception status: {status!r}."
            )

        if not isinstance(exception.get("history"), list):
            raise ValueError(
                f"History missing for {exception['exception_id']}."
            )

        if status == RESOLVED:
            resolution = exception.get("resolution")

            if not isinstance(resolution, dict):
                raise ValueError(
                    f"Resolved exception {exception['exception_id']} "
                    "must contain resolution details."
                )

            if not resolution.get("comment"):
                raise ValueError(
                    f"Resolved exception {exception['exception_id']} "
                    "must contain a resolution comment."
                )

            if not resolution.get("evidence"):
                raise ValueError(
                    f"Resolved exception {exception['exception_id']} "
                    "must contain resolution evidence."
                )
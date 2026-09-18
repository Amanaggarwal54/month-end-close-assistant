"""
Month-End Close Assistant - Step 13A: AI-assisted exception investigation.

An investigation explains an exception. It does not decide anything.

The close decision was taken by deterministic controls before any of this runs,
and it stays taken::

    control results -> close decision -> immutable package -> review workspace
                                                                    |
                                                          investigation (advisory)
                                                                    |
                                                             human reviewer

What this layer may do is assemble the evidence for one exception, hand it to a
provider, and check that what comes back is advice rather than a verdict. What
it may never do is change a control result, change a close decision, touch the
immutable package, resolve an exception, or cite evidence it was not given.

Three rules are enforced in code rather than asked for politely:

* **No decision fields.** A result carrying ``close_status``, ``report_allowed``,
  ``approved`` or any of their relatives is rejected outright, at any depth. A
  model cannot approve a close by naming a key after one.
* **No invented evidence.** Every entry in ``evidence_references`` must already
  appear in the packet. A provider may *recommend* looking somewhere new - that
  is what ``recommended_checks`` is for, in prose - but it may not cite a
  document nobody gave it.
* **Missing means missing.** Evidence the caller did not supply is listed in
  ``absent_evidence`` and nothing is filled in on its behalf.

No provider is implemented here. Step 13B can add a Claude or OpenAI
``InvestigationProvider``; this module only defines the shape one must satisfy
and ships a deterministic stub so the boundary can be tested without a network,
an API key, or a bill.

Purity
------
Standard library only. No file reads, no network, no clock, no uuid, no random.
``build_investigation_packet`` is pure: workspace in, packet out, and the
workspace is never modified.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "PACKET_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "FORBIDDEN_RESULT_FIELDS",
    "REQUIRED_RESULT_FIELDS",
    "InvestigationError",
    "ExceptionNotFound",
    "InvalidInvestigationResult",
    "InvestigationProvider",
    "StubInvestigationProvider",
    "build_investigation_packet",
    "validate_investigation_result",
    "investigate_exception",
]

PACKET_SCHEMA_VERSION = "1.0.0"
RESULT_SCHEMA_VERSION = "1.0.0"

#: Keys a result may never carry, at any depth. Each one is a way of dressing an
#: accounting decision up as advice, so the name alone is disqualifying.
FORBIDDEN_RESULT_FIELDS: frozenset[str] = frozenset({
    "close_status",
    "report_allowed",
    "approved",
    "approve",
    "approve_close",
    "approval",
    "resolve_exception",
    "resolved",
    "resolution",
    "control_result_override",
    "replacement_control_result",
    "override",
    "new_status",
    "status_override",
    "decision",
    "verdict",
})

#: Advisory fields a result must carry, and the type each must have.
REQUIRED_RESULT_FIELDS: dict[str, type] = {
    "exception_id": str,
    "summary": str,
    "observations": list,
    "possible_causes": list,
    "recommended_checks": list,
    "evidence_references": list,
    "uncertainties": list,
    "resolution_suggestion": str,
}

#: Evidence sections a caller may supply. Anything absent is named as absent
#: rather than guessed at.
EVIDENCE_SECTIONS = ("workspace_evidence", "source_files", "control_results")


class InvestigationError(RuntimeError):
    """Base class for investigation failures."""


class ExceptionNotFound(InvestigationError):
    """Raised when the workspace holds no exception with the given id."""


class InvalidInvestigationResult(InvestigationError):
    """Raised when a provider returns something that is not advisory output."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _put(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a key only when the value is present, so absent detail stays absent."""
    if value is not None and value != "":
        target[key] = value


def _evidence_entry(value: Any, default_type: str) -> dict[str, str] | None:
    """Normalise one evidence reference to ``{type, reference}``.

    A mapping that already carries both fields is copied through unchanged, so a
    reference recorded by the workflow reaches the packet exactly as the workflow
    wrote it. A bare string is given the section's own type rather than a guessed
    one.
    """
    if isinstance(value, Mapping):
        reference = value.get("reference")
        kind = value.get("type")
        if reference is None:
            return None
        return {"type": str(kind) if kind is not None else default_type,
                "reference": str(reference)}

    if isinstance(value, str) and value.strip():
        return {"type": default_type, "reference": value}

    return None


def _evidence_list(values: Iterable[Any] | None, default_type: str) -> list[dict[str, str]]:
    if not values:
        return []
    entries = [_evidence_entry(item, default_type) for item in values]
    return [entry for entry in entries if entry is not None]


def _find_exception(workspace: Mapping[str, Any], exception_id: str) -> Mapping[str, Any]:
    exceptions = workspace.get("exceptions")
    if not isinstance(exceptions, list):
        raise InvestigationError("workspace must contain an 'exceptions' list.")

    for entry in exceptions:
        if isinstance(entry, Mapping) and entry.get("exception_id") == exception_id:
            return entry

    raise ExceptionNotFound(f"Unknown exception_id: {exception_id}")


def _relevant_control_results(
    control_results: Sequence[Mapping[str, Any]] | None,
    control_id: str | None,
) -> list[dict[str, Any]]:
    """Only the control rows belonging to this exception.

    A packet carries the evidence for one exception, not the whole close. Rows
    are accepted as plain dictionaries so this module needs no dataframe library;
    the caller converts.
    """
    if not control_results or control_id is None:
        return []

    return [
        deepcopy(dict(row))
        for row in control_results
        if isinstance(row, Mapping) and row.get("check_id") == control_id
    ]


# ---------------------------------------------------------------------------
# the packet
# ---------------------------------------------------------------------------
def build_investigation_packet(
    workspace: Mapping[str, Any],
    exception_id: str,
    *,
    source_files: Iterable[Any] | None = None,
    control_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble everything a provider may see about one exception.

    Pure: no file reads, no clock, no mutation of ``workspace``. Nothing is
    invented - a field the workspace does not carry is left out, and an evidence
    section the caller did not supply is named in ``absent_evidence``.

    Parameters
    ----------
    workspace:
        A review workspace, as ``review_workspace.load_workspace`` returns it.
    exception_id:
        Which exception to investigate.
    source_files:
        Optional references to source documents. Either ``{type, reference}``
        mappings or bare path strings; nothing is read from disk here.
    control_results:
        Optional control-result rows as plain dictionaries. Only the rows whose
        ``check_id`` matches this exception's control are included.

    Returns
    -------
    dict
        Deterministic and JSON-serialisable.
    """
    exception = _find_exception(workspace, exception_id)
    source = workspace.get("source_package") or {}

    workspace_evidence = _evidence_list(exception.get("evidence"), "workspace_evidence")
    supplied_files = _evidence_list(source_files, "source_file")
    matched_controls = _relevant_control_results(control_results, exception.get("control_id"))
    control_evidence = [
        {"type": "control_result", "reference": str(row.get("check_id"))}
        for row in matched_controls
        if row.get("check_id") is not None
    ]

    available = workspace_evidence + supplied_files + control_evidence
    present = {
        "workspace_evidence": bool(workspace_evidence),
        "source_files": bool(supplied_files),
        "control_results": bool(matched_controls),
    }

    packet: dict[str, Any] = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "exception_id": exception_id,
    }

    _put(packet, "dataset_label", workspace.get("dataset_label"))
    _put(packet, "control_id", exception.get("control_id"))
    _put(packet, "control_name", exception.get("name"))
    _put(packet, "control_family", exception.get("family"))
    _put(packet, "control_group", exception.get("group"))
    _put(packet, "severity", exception.get("severity"))
    _put(packet, "status", exception.get("status"))
    _put(packet, "owner", exception.get("owner"))
    _put(packet, "message", exception.get("message"))
    _put(packet, "error_reference", exception.get("error_reference"))
    _put(packet, "source_status", exception.get("source_status"))

    # The close's own verdict, carried for context only. A provider may read it
    # and may not return it: see FORBIDDEN_RESULT_FIELDS.
    _put(packet, "close_status", source.get("close_status"))
    if "report_allowed" in source:
        packet["report_allowed"] = source.get("report_allowed")

    packet["details"] = deepcopy(exception.get("details", {}))
    packet["source_exception"] = deepcopy(exception.get("source_exception", {}))
    packet["history"] = deepcopy(exception.get("history", []))
    packet["available_evidence"] = available
    packet["absent_evidence"] = [
        section for section in EVIDENCE_SECTIONS if not present[section]
    ]
    if matched_controls:
        packet["control_results"] = matched_controls

    packet["instructions"] = {
        "role": "Explain this exception to the finance reviewer who must decide "
                "what to do about it.",
        "advisory_only": (
            "Deterministic controls decided this close before you saw it. Your "
            "output is advice for a person, never a decision: it cannot change a "
            "control result, a close status, or whether a report may be issued."
        ),
        "evidence_rule": (
            "Cite only the references listed in available_evidence. If something "
            "you would want is not there, say so in uncertainties and describe "
            "the check you would run in recommended_checks; do not invent a "
            "document, a figure or a reference."
        ),
        "resolution_rule": (
            "A human reviewer resolves an exception through the workflow. "
            "resolution_suggestion is a proposal for that person to weigh, and "
            "resolves nothing by itself."
        ),
        "required_result_fields": sorted(REQUIRED_RESULT_FIELDS),
        "forbidden_result_fields": sorted(FORBIDDEN_RESULT_FIELDS),
    }

    return packet


# ---------------------------------------------------------------------------
# the provider boundary
# ---------------------------------------------------------------------------
@runtime_checkable
class InvestigationProvider(Protocol):
    """Anything that can turn an investigation packet into advisory output.

    Step 13B may implement this against Claude or another model. The contract is
    deliberately narrow: one call, a dictionary in, a dictionary out, and the
    result is validated before anyone sees it.
    """

    def investigate(self, packet: dict[str, Any]) -> dict[str, Any]:
        ...


class StubInvestigationProvider:
    """A deterministic stand-in that reads the packet and invents nothing.

    It is not a model and does not pretend to be one: it restates what the packet
    already contains, cites only evidence the packet listed, and turns every
    absent evidence section into an explicit uncertainty. That makes it useful
    for exercising the boundary - validation, immutability, determinism - without
    a network, an API key or a non-deterministic answer.
    """

    def investigate(self, packet: dict[str, Any]) -> dict[str, Any]:
        exception_id = packet.get("exception_id")
        control_id = packet.get("control_id", "an unnamed control")
        control_name = packet.get("control_name")
        message = packet.get("message")

        observations: list[str] = []
        if message:
            observations.append(f"The control reported: {message}")
        if packet.get("severity"):
            observations.append(
                f"{control_id} is a {packet['severity']} control, so the close "
                "treats it as blocking."
            )
        details = packet.get("details") or {}
        for key in sorted(details):
            observations.append(f"Recorded {key}: {details[key]}")

        uncertainties = [
            f"No {section.replace('_', ' ')} was supplied with this packet, so "
            "nothing could be checked against it."
            for section in packet.get("absent_evidence", [])
        ]
        if not packet.get("available_evidence"):
            uncertainties.append(
                "No evidence references were available, so every observation "
                "below restates the control result rather than corroborating it."
            )

        summary = (
            f"{control_id}"
            + (f" ({control_name})" if control_name else "")
            + " failed and is recorded as "
            + f"{packet.get('status', 'OPEN')} for review."
        )

        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "exception_id": exception_id,
            "summary": summary,
            "observations": observations,
            "possible_causes": [
                "The underlying source data differs from the reference the "
                "control compares against.",
                "The control is behaving as designed and the finding is real.",
            ],
            "recommended_checks": [
                f"Re-read the control result for {control_id} in the close package.",
                "Compare the figure the control reported against its source document.",
            ],
            "evidence_references": list(packet.get("available_evidence", [])),
            "uncertainties": uncertainties,
            "resolution_suggestion": (
                "Review the finding against its source, then record a resolution "
                "through the workflow if it is explained. This suggestion "
                "resolves nothing on its own."
            ),
        }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _forbidden_keys_in(value: Any, path: str = "") -> list[str]:
    """Every forbidden key anywhere in the structure, with its location.

    The check is recursive because nesting is the obvious way round a top-level
    ban: ``{"decision": {"approved": true}}`` is the same claim one level down.
    """
    found: list[str] = []

    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_RESULT_FIELDS:
                found.append(here)
            found.extend(_forbidden_keys_in(item, here))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_forbidden_keys_in(item, f"{path}[{index}]"))

    return found


def _validate_string_list(value: Any, field_name: str) -> None:
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise InvalidInvestigationResult(
                f"{field_name}[{index}] must be a string, got "
                f"{type(item).__name__}."
            )


def _validate_evidence_references(
    references: Sequence[Any],
    packet: Mapping[str, Any],
) -> None:
    """Every citation must already appear in the packet.

    This is the rule that stops a provider inventing a document. Somewhere new
    worth looking belongs in ``recommended_checks``, as prose; a citation claims
    the reviewer can go and read the thing.
    """
    available = {
        (entry.get("type"), entry.get("reference"))
        for entry in packet.get("available_evidence", [])
        if isinstance(entry, Mapping)
    }

    for index, entry in enumerate(references):
        if not isinstance(entry, Mapping):
            raise InvalidInvestigationResult(
                f"evidence_references[{index}] must be an object with 'type' and "
                f"'reference', got {type(entry).__name__}."
            )

        missing = [field for field in ("type", "reference") if field not in entry]
        if missing:
            raise InvalidInvestigationResult(
                f"evidence_references[{index}] is missing {', '.join(missing)}."
            )

        unexpected = set(entry) - {"type", "reference"}
        if unexpected:
            raise InvalidInvestigationResult(
                f"evidence_references[{index}] carries unsupported field(s): "
                f"{', '.join(sorted(unexpected))}."
            )

        if (entry.get("type"), entry.get("reference")) not in available:
            raise InvalidInvestigationResult(
                f"evidence_references[{index}] cites "
                f"{entry.get('type')}:{entry.get('reference')}, which was not in "
                "the packet. A provider may recommend a new check but may not "
                "invent a reference."
            )


def validate_investigation_result(
    result: Any,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Check that a provider returned advice, and return a copy of it.

    Raises :class:`InvalidInvestigationResult` on anything else. The returned
    dictionary is a deep copy, so a provider holding a reference to its own
    output cannot change what the reviewer is shown.
    """
    if not isinstance(result, Mapping):
        raise InvalidInvestigationResult(
            f"an investigation result must be an object, got {type(result).__name__}."
        )

    forbidden = _forbidden_keys_in(result)
    if forbidden:
        raise InvalidInvestigationResult(
            "an investigation is advisory and may not carry decision field(s): "
            f"{', '.join(sorted(forbidden))}."
        )

    missing = sorted(field for field in REQUIRED_RESULT_FIELDS if field not in result)
    if missing:
        raise InvalidInvestigationResult(
            f"the investigation result is missing required field(s): {', '.join(missing)}."
        )

    for field_name, expected_type in REQUIRED_RESULT_FIELDS.items():
        value = result[field_name]
        if not isinstance(value, expected_type) or isinstance(value, bool):
            raise InvalidInvestigationResult(
                f"{field_name} must be {expected_type.__name__}, got "
                f"{type(value).__name__}."
            )

    expected_id = packet.get("exception_id")
    if result["exception_id"] != expected_id:
        raise InvalidInvestigationResult(
            f"the result is for {result['exception_id']!r} but the packet asked "
            f"about {expected_id!r}."
        )

    if not result["summary"].strip():
        raise InvalidInvestigationResult("summary must not be empty.")

    for field_name in ("observations", "possible_causes", "recommended_checks",
                       "uncertainties"):
        _validate_string_list(result[field_name], field_name)

    _validate_evidence_references(result["evidence_references"], packet)

    validated = deepcopy(dict(result))
    validated.setdefault("schema_version", RESULT_SCHEMA_VERSION)

    # A last, cheap proof that nothing unserialisable slipped through: this
    # output is written beside a close package and read back by people.
    try:
        json.dumps(validated)
    except (TypeError, ValueError) as error:
        raise InvalidInvestigationResult(
            f"the investigation result is not JSON-serialisable: {error}"
        ) from error

    return validated


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def investigate_exception(
    workspace: Mapping[str, Any],
    exception_id: str,
    provider: InvestigationProvider,
    *,
    source_files: Iterable[Any] | None = None,
    control_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a packet, ask a provider, validate the answer, return it.

    The workspace is read and never written. Nothing here resolves an exception,
    assigns an owner or changes a status: those remain workflow operations a
    person performs, and the advice returned is an input to that decision rather
    than a substitute for it.
    """
    packet = build_investigation_packet(
        workspace,
        exception_id,
        source_files=source_files,
        control_results=control_results,
    )

    # The provider is handed a copy: a badly behaved one cannot reach back into
    # the packet the caller still holds.
    raw = provider.investigate(deepcopy(packet))
    return validate_investigation_result(raw, packet)

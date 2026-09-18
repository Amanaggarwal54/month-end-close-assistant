"""
Month-End Close Assistant - Step 11A: audit trail and exception records.

Turns results that already exist into structured, JSON-serialisable audit data:
a summary of what the close decided, and one record per control that failed or
errored.

Purity contract
---------------
This module reads no files, writes no files, calls no model, and makes no
accounting decision. It does not classify a control, evaluate a tolerance, or
decide whether the close may proceed - those verdicts arrive already made in the
control results and the close decision. What it adds is shape: counting what is
there, ordering it deterministically, and carrying forward the detail fields the
upstream controls already recorded.

It imports nothing from ``pipeline.py`` or ``report.py``, so either can consume
it without a circular import.

On not inventing detail
-----------------------
A control result carries whatever locus its author recorded: a month for an FX
rate comparison, sample references for a matching control, a difference for an
elimination control. Where a field is absent it is left out of the record.
Nothing is reconstructed, inferred or back-calculated, because an invented
identifier in an audit trail is worse than a missing one.

Two consequences worth knowing, both deliberate:

* ``sample_refs`` holds different kinds of identifier depending on the control -
  invoice ids for a duplicate-invoice finding, payment ids for an orphan
  payment, pair ids for a one-sided intercompany pair, entry ids for a blanked
  amount. This module surfaces them under the neutral name ``references``
  instead of labelling them, because labelling would mean guessing.
* ``expected_value`` and ``actual_value`` are FX rates on a rate-comparison
  control and plain counts on every other FX control. They are renamed to
  ``reference_rate`` and ``supplied_rate`` only for the controls listed in
  :data:`RATE_COMPARISON_CONTROLS`, and only when the row names a single month.
  Renaming them unconditionally would write a reference rate of 0.00 into the
  audit trail for a re-performance control whose expected value is a count.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = ["build_audit_trail", "build_exception_records"]

AUDIT_VERSION = "1.0.0"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARNING = "WARNING"

#: Statuses that make a control an exception.
EXCEPTION_STATUSES: tuple[str, ...] = (STATUS_FAIL, STATUS_ERROR)

#: Severity ordering for exception records; anything unrecognised sorts last so
#: an unknown severity is still reported rather than silently dropped.
SEVERITY_PRIORITY: dict[str, int] = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1}
UNKNOWN_SEVERITY_PRIORITY = 99

#: The control-result column carrying the human-readable finding. It is exposed
#: on the record as ``message``, which is the name the audit schema uses.
MESSAGE_COLUMN = "explanation"

#: Controls whose ``expected_value`` and ``actual_value`` are FX rates rather
#: than counts. This reflects the control catalogue in ``controls.py``: FXC-03
#: compares a supplied rate with a reference rate, while the other FX controls
#: count months or entries. A row is only treated as a rate comparison when it
#: also names a single month, so a control that changes shape later degrades to
#: the neutral expected/actual fields rather than reporting a false rate.
RATE_COMPARISON_CONTROLS: frozenset[str] = frozenset({"FXC-03"})


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------
def _is_missing(value: Any) -> bool:
    """True for None, NaN, and empty or whitespace-only strings."""
    if value is None:
        return True
    try:
        if bool(pd.isna(value)):
            return True
    except (TypeError, ValueError):  # arrays and other non-scalars
        pass
    return isinstance(value, str) and not value.strip()


def _plain(value: Any) -> Any:
    """Convert a pandas or numpy scalar to a JSON-serialisable Python value."""
    if _is_missing(value):
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return str(value)


def _text(value: Any) -> str | None:
    """A trimmed string, or None when the value is absent."""
    if _is_missing(value):
        return None
    return str(value).strip()


def _number(value: Any) -> float | int | None:
    """A numeric value, or None when absent or unparseable.

    Strings are parsed because control results round-trip through CSV, where a
    numeric field comes back as text.
    """
    if _is_missing(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return int(number) if number.is_integer() else number


def _references(value: Any) -> list[str]:
    """Split a '; '-joined ``sample_refs`` field into its parts."""
    text = _text(value)
    if text is None:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a key only when the value is present, so absent detail stays absent."""
    if value is not None and value != []:
        target[key] = value


# ---------------------------------------------------------------------------
# family-specific detail
# ---------------------------------------------------------------------------
def _common_details(row: Mapping[str, Any]) -> dict[str, Any]:
    """Detail every control family records in the same way."""
    details: dict[str, Any] = {}
    _put(details, "expected_value", _plain(row.get("expected_value")))
    _put(details, "actual_value", _plain(row.get("actual_value")))
    _put(details, "difference", _plain(row.get("difference")))
    _put(details, "failed_count", _number(row.get("failed_count")))
    _put(details, "entity", _text(row.get("entity")))
    _put(details, "month", _text(row.get("month")))
    _put(details, "record_ref", _text(row.get("record_ref")))
    _put(details, "pair_id", _text(row.get("pair_id")))
    _put(details, "references", _references(row.get("sample_refs")))
    return details


def _mat_details(row: Mapping[str, Any]) -> dict[str, Any]:
    """Matching controls: the invoice, payment or PO identifiers recorded.

    The identifiers stay under the neutral ``references`` key set by
    :func:`_common_details`. Which kind they are depends on the control - a
    duplicate-invoice finding samples invoice ids, an orphan-payment finding
    samples payment ids - and the control result does not say which, so naming
    them would be a guess.

    Invoice, PO and payment amounts are not part of a control result: the
    controls record counts and the identifiers of the offending rows, not the
    values on them. They are therefore absent here rather than looked up from
    the accounting data, which would make this module a second reader of the
    ledgers.
    """
    return _common_details(row)


def _ico_details(row: Mapping[str, Any]) -> dict[str, Any]:
    """Intercompany controls: pair and entry identifiers, and the EUR difference.

    ``difference`` on an elimination control is already a EUR amount, so it is
    surfaced under a name that says so alongside the neutral field.
    """
    details = _common_details(row)
    _put(details, "difference_eur", _number(row.get("difference")))
    return details


def _fxc_details(row: Mapping[str, Any]) -> dict[str, Any]:
    """FX controls: the month, and the rates when the control compares rates.

    ``expected_value``/``actual_value`` are renamed to ``reference_rate`` and
    ``supplied_rate`` only for a control in :data:`RATE_COMPARISON_CONTROLS`
    that also names a single month. On any other FX control those columns hold
    counts, and renaming them would put a fabricated rate into the audit trail.
    """
    details = _common_details(row)
    check_id = _text(row.get("check_id")) or ""
    if check_id in RATE_COMPARISON_CONTROLS and details.get("month"):
        _put(details, "reference_rate", _number(row.get("expected_value")))
        _put(details, "supplied_rate", _number(row.get("actual_value")))
        _put(details, "rate_difference", _number(row.get("difference")))
    return details


#: Detail builders by control group. A group without an entry falls back to the
#: common fields, so a control group added later still produces a usable record.
_DETAIL_BUILDERS = {
    "MAT": _mat_details,
    "ICO": _ico_details,
    "FXC": _fxc_details,
}


def _group_of(row: Mapping[str, Any]) -> str:
    """The control group, from the column or from the check_id prefix."""
    group = _text(row.get("check_group"))
    if group:
        return group.upper()
    check_id = _text(row.get("check_id")) or ""
    return check_id.split("-")[0].upper()


def _details_for(row: Mapping[str, Any]) -> dict[str, Any]:
    builder = _DETAIL_BUILDERS.get(_group_of(row), _common_details)
    return builder(row)


def _stable_identifier(details: Mapping[str, Any]) -> str:
    """A stable tie-breaker for ordering: the first locus the record carries.

    Two rows of the same control - the two halves of a duplicate invoice, say -
    are otherwise indistinguishable to the sort, so their order would depend on
    how pandas happened to return them.
    """
    for key in ("pair_id", "month", "record_ref", "entity"):
        value = details.get(key)
        if value:
            return str(value)
    references = details.get("references") or []
    return str(references[0]) if references else ""


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def build_exception_records(
    control_results: pd.DataFrame,
    statuses: Iterable[str] = EXCEPTION_STATUSES,
) -> list[dict[str, Any]]:
    """One record per failed or errored control, deterministically ordered.

    Parameters
    ----------
    control_results:
        The table produced by ``controls.run_controls``. Not modified.
    statuses:
        Which statuses count as exceptions. Defaults to FAIL and ERROR; pass
        ``("FAIL", "ERROR", "WARNING")`` to include warnings in the same list.

    Returns
    -------
    list[dict]
        Records carrying ``check_id``, ``check_name``, ``check_group``,
        ``control_family``, ``severity``, ``status`` and ``message``, plus a
        ``details`` block holding whatever locus the control recorded. Sorted by
        severity (critical first), then check_id, then a stable identifier.
        Every value is JSON-serialisable.
    """
    if control_results is None or len(control_results) == 0:
        return []

    wanted = {str(status) for status in statuses}
    selected = control_results.loc[control_results["status"].isin(wanted)]

    records: list[dict[str, Any]] = []
    for row in selected.to_dict(orient="records"):
        record: dict[str, Any] = {
            "check_id": _text(row.get("check_id")),
            "check_name": _text(row.get("check_name")),
            "check_group": _text(row.get("check_group")),
            "control_family": _text(row.get("control_family")),
            "severity": _text(row.get("severity")),
            "status": _text(row.get("status")),
            "message": _text(row.get(MESSAGE_COLUMN)),
        }
        _put(record, "error_reference", _text(row.get("error_reference")))
        record["details"] = _details_for(row)
        records.append(record)

    records.sort(
        key=lambda record: (
            SEVERITY_PRIORITY.get(record.get("severity") or "", UNKNOWN_SEVERITY_PRIORITY),
            record.get("check_id") or "",
            _stable_identifier(record.get("details") or {}),
        )
    )
    return records


def build_audit_trail(
    control_results: pd.DataFrame,
    decision: Mapping[str, Any],
    exception_records: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarise one close run as a JSON-serialisable audit record.

    Counts are derived from ``control_results`` by reading the ``status`` and
    ``severity`` columns the controls already populated. The verdict fields -
    close status, whether a report may be issued, which controls blocked it -
    are carried through from ``decision`` exactly as ``controls.decide_close``
    returned them. Nothing here re-evaluates a control or re-decides a close.

    Parameters
    ----------
    control_results:
        The table produced by ``controls.run_controls``. Not modified.
    decision:
        The mapping produced by ``controls.decide_close``. Not modified.
    exception_records:
        Optional pre-built records, so a caller that already has them does not
        pay to build them twice. Built from ``control_results`` when omitted.

    Returns
    -------
    dict
        Deterministic and JSON-serialisable: identical inputs give an identical
        dict, with no pandas or numpy scalars inside it.
    """
    results = control_results if control_results is not None else pd.DataFrame()
    exceptions = (
        list(exception_records)
        if exception_records is not None
        else build_exception_records(results)
    )

    if len(results):
        counts = results["status"].value_counts()
        # Sorted so the mapping serialises identically on every run.
        status_counts = {str(status): int(count) for status, count in counts.items()}
        status_counts = dict(sorted(status_counts.items()))

        severity = results["severity"]
        status = results["status"]
        critical_failed = int(((severity == SEVERITY_CRITICAL) & (status == STATUS_FAIL)).sum())
        warning_count = int(((severity == SEVERITY_WARNING) & (status == STATUS_WARNING)).sum())
        error_count = int((status == STATUS_ERROR).sum())
    else:
        status_counts, critical_failed, warning_count, error_count = {}, 0, 0, 0

    blocking = decision.get("blocking_controls") or []

    return {
        "audit_version": AUDIT_VERSION,
        "dataset_label": _text(decision.get("dataset_label")),
        "run_timestamp": _text(decision.get("run_timestamp")),
        "controls_version": _text(decision.get("controls_version")),
        "close_status": _text(decision.get("close_status")),
        "report_allowed": bool(decision.get("report_allowed")),
        "control_count": int(len(results)),
        "status_counts": status_counts,
        "critical_failed_count": critical_failed,
        "warning_count": warning_count,
        "error_count": error_count,
        "blocking_controls": [str(item) for item in blocking],
        "exception_count": len(exceptions),
        "exceptions": exceptions,
    }

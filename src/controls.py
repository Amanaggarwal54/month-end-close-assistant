"""
Month-End Close Assistant - Step 6 (Phase 1): validation and control layer.

This module answers one question: can the month-end close report safely be
produced? It consumes the artefacts produced by ``match.py`` and
``intercompany.py`` and never re-implements their logic.

Purity contract
---------------
``run_controls()`` and ``decide_close()`` are pure functions. They read no
files, write no files, and never mutate the objects passed to them. Persisting
``control_results.csv`` and ``close_decision.json`` is the job of the CLI at the
bottom of this module (or of a future ``pipeline.py``), not of the engine.

Control families
----------------
RECONCILIATION  do the artefacts agree with each other?
COMPLETENESS    is everything that should be present actually present?
SOURCE_ACCURACY do the inputs agree with a reference outside the system?

Phase 1 implements the MAT, ICO, SRC and CVG groups. The FXC group
(source accuracy for FX, including E4 detection) arrives in Phase 2, so no
control in this file can detect a rate that is internally consistent but wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

CONTROLS_VERSION = "phase1-1.0.0"

# result statuses
PASS = "PASS"
FAIL = "FAIL"
WARNING = "WARNING"
ERROR = "ERROR"

# severities
CRITICAL = "CRITICAL"
SEV_WARNING = "WARNING"

# control families
RECONCILIATION = "RECONCILIATION"
COMPLETENESS = "COMPLETENESS"
SOURCE_ACCURACY = "SOURCE_ACCURACY"

# close statuses
CLOSE_PASS = "PASS"
CLOSE_PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
CLOSE_FAIL = "FAIL"

RESULT_COLUMNS: list[str] = [
    "check_id",
    "check_name",
    "check_group",
    "control_family",
    "description",
    "severity",
    "status",
    "expected_value",
    "actual_value",
    "difference",
    "entity",
    "month",
    "pair_id",
    "record_ref",
    "failed_count",
    "sample_refs",
    "error_reference",
    "explanation",
    "dataset_label",
    "run_timestamp",
]

MATCH_STATUS_DOMAIN = {
    "MATCHED",
    "AMOUNT_MISMATCH",
    "NO_PO",
    "UNPAID",
    "PARTIALLY_PAID",
    "OVERPAID",
    "PAYMENT_WITHOUT_INVOICE",
    "DUPLICATE_INVOICE",
}

# Shared-cost exception statuses that mean the cost could not be processed.
INVALID_COST_STATUSES = {
    "INVALID_SHARE_TOTAL",
    "MISSING_SHARE",
    "MISSING_AMOUNT",
    "MISSING_CURRENCY",
    "INVALID_ENTITY",
    "INVALID_MONTH",
}


@dataclass(frozen=True)
class ControlConfig:
    """Thresholds and scope for a control run.

    Only ``amount_tolerance`` and ``markup_pct`` come from the project
    specification. Everything else is a documented project assumption, recorded
    on the close decision as ``config_used`` so a stored result says which
    thresholds produced it.
    """

    amount_tolerance: float = 0.01
    entity_currency: dict[str, str] = field(
        default_factory=lambda: {"BE01": "EUR", "US01": "USD", "BE02": "EUR"}
    )
    period_months: tuple[str, ...] = ("2026-01", "2026-02", "2026-03")
    max_sample_refs: int = 5

    # Phase 2 (FXC) parameters, carried here so the config object is stable.
    fx_comparison_tolerance: float = 1e-6
    fx_min: float = 0.5
    fx_max: float = 2.0

    @property
    def entities(self) -> set[str]:
        return set(self.entity_currency)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _month_key(value: Any) -> str | None:
    """Normalise any date-like value to 'YYYY-MM'; None when unparseable."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return pd.Period(pd.to_datetime(text), freq="M").strftime("%Y-%m")
    except Exception:  # noqa: BLE001 - any parse failure means "not a month"
        return None


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _samples(values: Iterable[Any], limit: int) -> str:
    out = [str(v) for v in values if v is not None and not pd.isna(v)]
    return "; ".join(out[:limit])


def _split_ids(value: Any) -> list[str]:
    """Split a '; '-joined identifier list into its parts."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _result(
    check_id: str,
    check_name: str,
    check_group: str,
    control_family: str,
    description: str,
    severity: str,
    status: str,
    *,
    expected_value: Any = None,
    actual_value: Any = None,
    difference: Any = None,
    entity: Any = None,
    month: Any = None,
    pair_id: Any = None,
    record_ref: Any = None,
    failed_count: int = 0,
    sample_refs: str = "",
    error_reference: str = "",
    explanation: str = "",
) -> dict:
    """Build one control-result row in the common schema."""
    return {
        "check_id": check_id,
        "check_name": check_name,
        "check_group": check_group,
        "control_family": control_family,
        "description": description,
        "severity": severity,
        "status": status,
        "expected_value": expected_value,
        "actual_value": actual_value,
        "difference": difference,
        "entity": entity,
        "month": month,
        "pair_id": pair_id,
        "record_ref": record_ref,
        "failed_count": int(failed_count),
        "sample_refs": sample_refs,
        "error_reference": error_reference,
        "explanation": explanation,
        "dataset_label": None,
        "run_timestamp": None,
    }


def _verdict(severity: str, failed: bool) -> str:
    """A critical control fails; a warning-severity control warns."""
    if not failed:
        return PASS
    return FAIL if severity == CRITICAL else WARNING


def _count_control(
    check_id: str,
    check_name: str,
    group: str,
    family: str,
    description: str,
    severity: str,
    offenders: pd.DataFrame | pd.Series | Sequence,
    ref_values: Iterable[Any],
    config: ControlConfig,
    explanation_ok: str,
    explanation_bad: str,
    error_reference: str = "",
) -> dict:
    """Standard 'this population must be empty' control."""
    count = len(offenders)
    status = _verdict(severity, count > 0)
    return _result(
        check_id,
        check_name,
        group,
        family,
        description,
        severity,
        status,
        expected_value=0,
        actual_value=count,
        difference=count,
        failed_count=count,
        sample_refs=_samples(ref_values, config.max_sample_refs),
        error_reference=error_reference,
        explanation=explanation_ok if count == 0 else explanation_bad.format(count=count),
    )


# ---------------------------------------------------------------------------
# MAT - three-way matching controls
# ---------------------------------------------------------------------------
def _mat_controls(
    match_results: pd.DataFrame,
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    config: ControlConfig,
) -> list[dict]:
    results: list[dict] = []
    invoice_rows = match_results[match_results["record_type"] == "INVOICE"]
    orphan_rows = match_results[match_results["record_type"] == "PAYMENT_ONLY"]

    # MAT-01 invoice population complete -------------------------------------
    source_ids = sorted(str(v).strip() for v in invoices["invoice_id"].dropna())
    result_ids = sorted(str(v).strip() for v in invoice_rows["invoice_id"].dropna())
    # Multiset comparison: duplicates in the source must appear as duplicates in
    # the output, which is how E2 survives Step 4 instead of being deduplicated.
    missing = sorted(set(source_ids) - set(result_ids))
    unexpected = sorted(set(result_ids) - set(source_ids))
    failed = source_ids != result_ids
    results.append(
        _result(
            "MAT-01",
            "Invoice population complete",
            "MAT",
            COMPLETENESS,
            "Every invoice row in the source appears exactly once in the match output.",
            CRITICAL,
            _verdict(CRITICAL, failed),
            expected_value=len(source_ids),
            actual_value=len(result_ids),
            difference=len(result_ids) - len(source_ids),
            failed_count=len(missing) + len(unexpected),
            sample_refs=_samples(missing + unexpected, config.max_sample_refs),
            explanation=(
                "All invoice rows are represented in the match output."
                if not failed
                else f"{len(missing)} invoice(s) missing from the output, "
                f"{len(unexpected)} unexpected invoice id(s) present."
            ),
        )
    )

    # MAT-02 payment population complete -------------------------------------
    # Duplicate invoice rows repeat the same aggregated payment_ids, so raw
    # occurrence counts would report a false failure on E2. The population is
    # therefore compared as a set of unique payment ids: every source payment
    # must be represented somewhere, and nothing may appear that the source does
    # not contain. Orphan payments (E3) carry their ids on the PAYMENT_ONLY row,
    # so they count as represented.
    source_payment_ids = [str(v).strip() for v in payments["payment_id"].dropna()]
    source_unique = set(source_payment_ids)
    represented: set[str] = set()
    for value in match_results["payment_ids"]:
        represented.update(_split_ids(value))

    missing_payments = sorted(source_unique - represented)
    unexpected_payments = sorted(represented - source_unique)
    duplicate_source_ids = sorted(
        {pid for pid in source_payment_ids if source_payment_ids.count(pid) > 1}
    )
    offenders = missing_payments + unexpected_payments + duplicate_source_ids
    results.append(
        _result(
            "MAT-02",
            "Payment population complete",
            "MAT",
            COMPLETENESS,
            "Every unique source payment_id is represented in the match output, and no "
            "payment id appears that the source does not contain.",
            CRITICAL,
            _verdict(CRITICAL, bool(offenders)),
            expected_value=len(source_unique),
            actual_value=len(represented),
            difference=len(represented) - len(source_unique),
            failed_count=len(offenders),
            sample_refs=_samples(offenders, config.max_sample_refs),
            explanation=(
                "All payments are represented; duplicate invoice rows repeat payment ids "
                "but do not affect the unique population."
                if not offenders
                else f"{len(missing_payments)} payment(s) missing, "
                f"{len(unexpected_payments)} unexpected, "
                f"{len(duplicate_source_ids)} duplicated in the source file."
            ),
        )
    )

    # status-count controls ---------------------------------------------------
    def _by_status(status: str) -> pd.DataFrame:
        return match_results[match_results["status"] == status]

    duplicates = _by_status("DUPLICATE_INVOICE")
    results.append(
        _count_control(
            "MAT-03", "No duplicate invoices", "MAT", RECONCILIATION,
            "No invoice_id appears more than once in the invoice ledger.",
            CRITICAL, duplicates, duplicates["invoice_id"], config,
            "No duplicate invoice ids.",
            "{count} invoice row(s) share a duplicated invoice_id; the same cost could be "
            "paid twice.",
            error_reference="E2",
        )
    )

    mismatches = _by_status("AMOUNT_MISMATCH")
    results.append(
        _count_control(
            "MAT-04", "Invoice agrees with purchase order", "MAT", RECONCILIATION,
            "Invoice net amount equals the PO net amount within tolerance, and every "
            "amount needed for verification is present.",
            CRITICAL, mismatches, mismatches["invoice_id"], config,
            "Every invoice agrees with its purchase order.",
            "{count} invoice(s) do not agree with the purchase order or cannot be verified.",
            error_reference="E1",
        )
    )

    no_po = _by_status("NO_PO")
    results.append(
        _count_control(
            "MAT-05", "Every invoice has a valid purchase order", "MAT", RECONCILIATION,
            "Every invoice references a purchase order that exists.",
            CRITICAL, no_po, no_po["invoice_id"], config,
            "Every invoice references an existing purchase order.",
            "{count} invoice(s) reference a missing or absent purchase order.",
        )
    )

    orphan_payments = _by_status("PAYMENT_WITHOUT_INVOICE")
    results.append(
        _count_control(
            "MAT-06", "No payment without an invoice", "MAT", RECONCILIATION,
            "Every payment maps to an invoice in the ledger.",
            CRITICAL, orphan_payments, orphan_payments["payment_ids"], config,
            "Every payment maps to an invoice.",
            "{count} payment group(s) reference an invoice that does not exist.",
            error_reference="E3",
        )
    )

    overpaid = _by_status("OVERPAID")
    results.append(
        _count_control(
            "MAT-07", "No overpayments", "MAT", RECONCILIATION,
            "No invoice was paid beyond its total amount.",
            CRITICAL, overpaid, overpaid["invoice_id"], config,
            "No invoice was overpaid.",
            "{count} invoice(s) were paid more than the invoiced total; cash left the "
            "business without an obligation behind it.",
        )
    )

    unpaid = _by_status("UNPAID")
    results.append(
        _count_control(
            "MAT-08", "Unpaid invoices reviewed", "MAT", RECONCILIATION,
            "Open payables at month-end are reported for review; they do not block the close.",
            SEV_WARNING, unpaid, unpaid["invoice_id"], config,
            "No unpaid invoices.",
            "{count} invoice(s) are unpaid at month-end; normal for a payables ledger, "
            "listed for review.",
        )
    )

    partial = _by_status("PARTIALLY_PAID")
    results.append(
        _count_control(
            "MAT-09", "Partial payments reviewed", "MAT", RECONCILIATION,
            "Partially settled invoices are reported for review; they do not block the close.",
            SEV_WARNING, partial, partial["invoice_id"], config,
            "No partially paid invoices.",
            "{count} invoice(s) are partially paid; listed for review.",
        )
    )

    # MAT-10 status domain ----------------------------------------------------
    bad_status = match_results[
        match_results["status"].isna() | ~match_results["status"].isin(MATCH_STATUS_DOMAIN)
    ]
    results.append(
        _count_control(
            "MAT-10", "Match status domain valid", "MAT", RECONCILIATION,
            "Every result row carries one of the eight defined match statuses.",
            CRITICAL, bad_status, bad_status.get("invoice_id", pd.Series(dtype=str)), config,
            "Every row carries a defined status.",
            "{count} row(s) carry a blank or unknown status, so their condition is unverified.",
        )
    )

    # MAT-11 data-quality flags on matched rows -------------------------------
    flags = pd.Series("", index=match_results.index)
    if "data_quality_flag" in match_results:
        flags = match_results["data_quality_flag"].fillna("").astype(str)
    flagged = match_results[(match_results["status"] == "MATCHED") & (flags.str.len() > 0)]
    results.append(
        _count_control(
            "MAT-11", "Data-quality flags on matched rows", "MAT", RECONCILIATION,
            "Rows that matched but still carry a data-quality flag are reported for review.",
            SEV_WARNING, flagged, flagged["invoice_id"], config,
            "No matched row carries a data-quality flag.",
            "{count} matched row(s) carry a data-quality flag such as a missing secondary "
            "amount.",
        )
    )

    # MAT-12 row-count reconciliation ----------------------------------------
    expected_rows = len(invoices) + len(orphan_rows)
    actual_rows = len(match_results)
    failed_rows = expected_rows != actual_rows
    results.append(
        _result(
            "MAT-12",
            "Result row count reconciles",
            "MAT",
            COMPLETENESS,
            "Result rows equal source invoice rows plus orphan payment groups.",
            CRITICAL,
            _verdict(CRITICAL, failed_rows),
            expected_value=expected_rows,
            actual_value=actual_rows,
            difference=actual_rows - expected_rows,
            failed_count=abs(actual_rows - expected_rows),
            explanation=(
                "Row counts reconcile to the source population."
                if not failed_rows
                else "Result rows do not reconcile: rows were lost or created in processing."
            ),
        )
    )
    return results


# ---------------------------------------------------------------------------
# ICO - intercompany structure controls
# ---------------------------------------------------------------------------
def _ico_controls(
    ic_entries: pd.DataFrame,
    ic_elimination: pd.DataFrame,
    config: ControlConfig,
) -> list[dict]:
    results: list[dict] = []
    accounting = ic_entries[ic_entries["entry_type"].isin(["RECEIVABLE", "PAYABLE"])]

    def _elim(statuses: set[str]) -> pd.DataFrame:
        if ic_elimination.empty:
            return ic_elimination
        return ic_elimination[ic_elimination["check_status"].isin(statuses)]

    one_sided = _elim({"MISSING_RECEIVABLE", "MISSING_PAYABLE"})
    results.append(
        _count_control(
            "ICO-01", "Every pair has both sides", "ICO", RECONCILIATION,
            "Each intercompany pair has exactly one receivable and one payable.",
            CRITICAL, one_sided, one_sided.get("pair_id", pd.Series(dtype=str)), config,
            "Every pair carries both sides.",
            "{count} pair(s) are one-sided; one entity's books carry a balance the "
            "counterparty does not.",
            error_reference="E5",
        )
    )

    duplicate_sides = _elim({"DUPLICATE_SIDE"})
    results.append(
        _count_control(
            "ICO-02", "No duplicate sides", "ICO", RECONCILIATION,
            "No pair carries more than one receivable or more than one payable.",
            CRITICAL, duplicate_sides, duplicate_sides.get("pair_id", pd.Series(dtype=str)), config,
            "No duplicated intercompany sides.",
            "{count} pair(s) carry duplicated sides, so a recharge is posted twice.",
        )
    )

    # ICO-03 accounting amounts complete --------------------------------------
    required = ["local_amount", "eur_equivalent", "currency", "pair_id"]
    if accounting.empty:
        incomplete = accounting
    else:
        blank = pd.Series(False, index=accounting.index)
        for column in required:
            values = accounting[column]
            blank = blank | values.isna()
            if values.dtype == object:
                blank = blank | (values.fillna("").astype(str).str.strip() == "")
        incomplete = accounting[blank]
    results.append(
        _count_control(
            "ICO-03", "Accounting amounts complete", "ICO", COMPLETENESS,
            "Every intercompany entry carries a local amount, an EUR equivalent, a "
            "currency and a pair id.",
            CRITICAL, incomplete, incomplete.get("entry_id", pd.Series(dtype=str)), config,
            "Every intercompany entry is complete.",
            "{count} entr(y/ies) are missing a required amount or identifier; the posting "
            "cannot be booked or verified.",
            error_reference="E6",
        )
    )

    # ICO-04 elimination to zero ---------------------------------------------
    broken = _elim({"AMOUNT_MISMATCH"})
    worst = None
    if not broken.empty and "difference_eur" in broken:
        differences = _numeric(broken["difference_eur"]).abs()
        worst = float(differences.max()) if not differences.empty else None
    results.append(
        _result(
            "ICO-04",
            "Pairs eliminate to zero",
            "ICO",
            RECONCILIATION,
            "Receivable minus payable is zero in EUR for every pair, within tolerance.",
            CRITICAL,
            _verdict(CRITICAL, len(broken) > 0),
            expected_value=0,
            actual_value=len(broken),
            difference=worst,
            failed_count=len(broken),
            sample_refs=_samples(broken.get("pair_id", pd.Series(dtype=str)), config.max_sample_refs),
            explanation=(
                "Every pair eliminates within tolerance."
                if broken.empty
                else f"{len(broken)} pair(s) do not eliminate; largest difference EUR {worst}."
            ),
        )
    )

    # ICO-05 aggregate elimination -------------------------------------------
    receivable_total = float(
        _numeric(accounting.loc[accounting["entry_type"] == "RECEIVABLE", "eur_equivalent"]).sum()
    )
    payable_total = float(
        _numeric(accounting.loc[accounting["entry_type"] == "PAYABLE", "eur_equivalent"]).sum()
    )
    net = round(receivable_total - payable_total, 2)
    results.append(
        _result(
            "ICO-05",
            "Aggregate elimination",
            "ICO",
            RECONCILIATION,
            "Total intercompany receivables equal total payables in EUR.",
            CRITICAL,
            _verdict(CRITICAL, abs(net) > config.amount_tolerance),
            expected_value=round(receivable_total, 2),
            actual_value=round(payable_total, 2),
            difference=net,
            failed_count=0 if abs(net) <= config.amount_tolerance else 1,
            explanation=(
                f"Receivables and payables both total EUR {receivable_total:,.2f}."
                if abs(net) <= config.amount_tolerance
                else f"Group intercompany position does not net to zero: EUR {net:,.2f}."
            ),
        )
    )

    # ICO-06 entry status valid ----------------------------------------------
    invalid_entries = accounting[accounting["status"] != "VALID"]
    exceptions = ic_entries[ic_entries["entry_type"] == "EXCEPTION"]
    offenders = pd.concat([invalid_entries, exceptions]) if len(exceptions) else invalid_entries
    results.append(
        _count_control(
            "ICO-06", "Entry status valid", "ICO", RECONCILIATION,
            "Every generated entry is VALID and no shared cost ended as an exception.",
            CRITICAL, offenders, offenders.get("cost_id", pd.Series(dtype=str)), config,
            "All entries are valid and no shared cost was rejected.",
            "{count} entr(y/ies) are not valid, so at least one shared cost was not "
            "recharged.",
        )
    )

    # ICO-07 entities valid ---------------------------------------------------
    if accounting.empty:
        bad_entities = accounting
    else:
        known = config.entities
        bad_entities = accounting[
            ~accounting["entity"].isin(known)
            | ~accounting["counterparty_entity"].isin(known)
            | (accounting["entity"] == accounting["counterparty_entity"])
        ]
    results.append(
        _count_control(
            "ICO-07", "Entities valid", "ICO", RECONCILIATION,
            "Both sides name a known entity and an entity never faces itself.",
            CRITICAL, bad_entities, bad_entities.get("entry_id", pd.Series(dtype=str)), config,
            "Every entry names known, distinct entities.",
            "{count} entr(y/ies) name an unknown entity or the same entity on both sides.",
        )
    )

    # ICO-08 currency matches entity -----------------------------------------
    if accounting.empty:
        bad_currency = accounting
    else:
        expected_currency = accounting["entity"].map(config.entity_currency)
        bad_currency = accounting[accounting["currency"] != expected_currency]
    results.append(
        _count_control(
            "ICO-08", "Currency matches entity", "ICO", RECONCILIATION,
            "Each side is booked in its entity's functional currency.",
            CRITICAL, bad_currency, bad_currency.get("entry_id", pd.Series(dtype=str)), config,
            "Every entry is booked in the entity's own currency.",
            "{count} entr(y/ies) are booked in the wrong currency for the entity.",
        )
    )

    # ICO-09 FX consistent within a pair --------------------------------------
    inconsistent = _elim({"FX_INCONSISTENT"})
    results.append(
        _count_control(
            "ICO-09", "FX consistent within pair", "ICO", RECONCILIATION,
            "Both sides of a pair use the same FX rate.",
            CRITICAL, inconsistent, inconsistent.get("pair_id", pd.Series(dtype=str)), config,
            "Both sides of every pair use the same rate.",
            "{count} pair(s) used different FX rates on the two sides.",
        )
    )

    # ICO-10 pair traceability ------------------------------------------------
    if accounting.empty:
        ambiguous: list[str] = []
    else:
        grouped = accounting.groupby("pair_id", dropna=False)[
            ["cost_id", "paying_entity", "receiving_entity"]
        ].nunique()
        ambiguous = grouped[(grouped > 1).any(axis=1)].index.tolist()
    results.append(
        _count_control(
            "ICO-10", "Pair traceability", "ICO", RECONCILIATION,
            "Each pair id maps to exactly one cost, payer and receiver.",
            CRITICAL, ambiguous, ambiguous, config,
            "Every pair id is traceable to one recharge.",
            "{count} pair id(s) map to more than one cost or entity combination.",
        )
    )
    return results


# ---------------------------------------------------------------------------
# SRC - shared-cost source controls
# ---------------------------------------------------------------------------
def _src_controls(
    ic_entries: pd.DataFrame,
    shared_costs: pd.DataFrame,
    config: ControlConfig,
) -> list[dict]:
    results: list[dict] = []
    exceptions = ic_entries[ic_entries["entry_type"] == "EXCEPTION"]

    # SRC-01 shared-cost coverage --------------------------------------------
    source_costs = {str(v).strip() for v in shared_costs["cost_id"].dropna()}
    covered = {str(v).strip() for v in ic_entries["cost_id"].dropna()}
    uncovered = sorted(source_costs - covered)
    results.append(
        _count_control(
            "SRC-01", "Shared-cost coverage", "SRC", COMPLETENESS,
            "Every shared cost produced entries or an explicit exception.",
            CRITICAL, uncovered, uncovered, config,
            "Every shared cost is accounted for.",
            "{count} shared cost(s) vanished: neither recharged nor reported as an exception.",
        )
    )

    # SRC-02 no invalid shared costs -----------------------------------------
    invalid = exceptions[exceptions["status"].isin(INVALID_COST_STATUSES)]
    results.append(
        _count_control(
            "SRC-02", "No invalid shared costs", "SRC", RECONCILIATION,
            "No shared cost was rejected for invalid shares, amounts, currency, entity "
            "or month.",
            CRITICAL, invalid, invalid.get("cost_id", pd.Series(dtype=str)), config,
            "Every shared cost passed input validation.",
            "{count} shared cost(s) failed validation and were not recharged.",
        )
    )

    # SRC-03 FX available where required -------------------------------------
    missing_fx = exceptions[exceptions["status"] == "MISSING_FX"]
    results.append(
        _count_control(
            "SRC-03", "FX available where required", "SRC", COMPLETENESS,
            "Every cost needing conversion had a rate for its month.",
            CRITICAL, missing_fx, missing_fx.get("cost_id", pd.Series(dtype=str)), config,
            "A rate was available wherever conversion was needed.",
            "{count} shared cost(s) could not be converted: no rate for the month.",
        )
    )

    # SRC-04 costs fully recharged or explained ------------------------------
    no_recharge = exceptions[exceptions["status"] == "NO_RECHARGE"]
    results.append(
        _count_control(
            "SRC-04", "Costs recharged or explained", "SRC", RECONCILIATION,
            "Shared costs borne entirely by the paying entity are reported for review.",
            SEV_WARNING, no_recharge, no_recharge.get("cost_id", pd.Series(dtype=str)), config,
            "Every shared cost generated at least one recharge.",
            "{count} shared cost(s) were borne wholly by the payer and generated no recharge.",
        )
    )

    # SRC-05 no negative amounts or shares ------------------------------------
    share_columns = [c for c in shared_costs.columns if c.startswith("share_")]
    amounts = _numeric(shared_costs["amount"])
    bad = amounts.isna() | (amounts <= 0)
    for column in share_columns:
        shares = _numeric(shared_costs[column])
        bad = bad | shares.isna() | (shares < 0) | (shares > 1)
    negative = shared_costs[bad]
    results.append(
        _count_control(
            "SRC-05", "No negative amounts or shares", "SRC", RECONCILIATION,
            "Shared-cost amounts are positive and every share lies between 0 and 1. The "
            "specification defines no credit-note treatment.",
            CRITICAL, negative, negative.get("cost_id", pd.Series(dtype=str)), config,
            "All amounts and shares are within their supported range.",
            "{count} shared cost(s) carry a non-positive amount or an out-of-range share.",
        )
    )

    # SRC-06 month within scope -----------------------------------------------
    months = shared_costs["month"].map(_month_key)
    out_of_scope = shared_costs[~months.isin(config.period_months) | months.isna()]
    results.append(
        _count_control(
            "SRC-06", "Shared-cost month in scope", "SRC", COMPLETENESS,
            f"Every shared cost falls within {config.period_months[0]}..{config.period_months[-1]}.",
            CRITICAL, out_of_scope, out_of_scope.get("cost_id", pd.Series(dtype=str)), config,
            "Every shared cost falls inside the close period.",
            "{count} shared cost(s) fall outside the close period or carry an unparseable month.",
        )
    )
    return results


# ---------------------------------------------------------------------------
# CVG - period and entity coverage
# ---------------------------------------------------------------------------
def _cvg_controls(
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    shared_costs: pd.DataFrame,
    ic_entries: pd.DataFrame,
    config: ControlConfig,
) -> list[dict]:
    results: list[dict] = []
    invoice_months = invoices["invoice_date"].map(_month_key)
    cost_months = shared_costs["month"].map(_month_key)

    # CVG-01 months in scope ---------------------------------------------------
    out_of_scope = sorted(
        {m for m in pd.concat([invoice_months, cost_months]) if m not in config.period_months}
    )
    results.append(
        _count_control(
            "CVG-01", "Months in scope", "CVG", COMPLETENESS,
            "No transaction falls outside the close period.",
            CRITICAL, out_of_scope, out_of_scope, config,
            "All transactions fall inside the close period.",
            "{count} month(s) outside the close period appear in the data.",
        )
    )

    # CVG-02 expected months present ------------------------------------------
    present = {m for m in invoice_months if m}
    absent = sorted(set(config.period_months) - present)
    results.append(
        _count_control(
            "CVG-02", "Expected months present", "CVG", COMPLETENESS,
            "Each month of the close period shows invoice activity.",
            SEV_WARNING, absent, absent, config,
            "Every month of the period shows activity.",
            "{count} month(s) of the period show no invoice activity.",
        )
    )

    # CVG-03 entities known ----------------------------------------------------
    seen: set[str] = set()
    for frame, column in (
        (invoices, "entity"),
        (payments, "entity"),
        (shared_costs, "paying_entity"),
        (ic_entries, "entity"),
    ):
        if column in frame:
            seen.update(str(v).strip() for v in frame[column].dropna())
    unknown = sorted(seen - config.entities)
    results.append(
        _count_control(
            "CVG-03", "Entities known", "CVG", COMPLETENESS,
            "Every entity referenced in the data is one of the group entities.",
            CRITICAL, unknown, unknown, config,
            "Only known entities appear in the data.",
            "{count} unknown entit(y/ies) appear in the data.",
        )
    )

    # CVG-04 intercompany output coverage --------------------------------------
    accounting = ic_entries[ic_entries["entry_type"].isin(["RECEIVABLE", "PAYABLE"])]
    entry_months = {m for m in accounting["month"].map(_month_key) if m}
    cost_month_set = {m for m in cost_months if m}
    uncovered = sorted(cost_month_set - entry_months)
    results.append(
        _count_control(
            "CVG-04", "Intercompany output coverage", "CVG", COMPLETENESS,
            "Every month carrying shared costs also produced intercompany entries.",
            CRITICAL, uncovered, uncovered, config,
            "Every month with shared costs produced entries.",
            "{count} month(s) have shared costs but no intercompany entries.",
        )
    )
    return results


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
_GROUPS: dict[str, Callable] = {}  # placeholder kept for future group registration


def run_controls(
    match_results: pd.DataFrame,
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    ic_entries: pd.DataFrame,
    ic_elimination: pd.DataFrame,
    shared_costs: pd.DataFrame,
    purchase_orders: pd.DataFrame | None = None,
    config: ControlConfig | None = None,
    dataset_label: str = "",
    run_timestamp: str | None = None,
) -> pd.DataFrame:
    """Evaluate every Phase 1 control and return the results table.

    Pure: reads nothing, writes nothing, mutates nothing. A control that cannot
    be executed returns status ERROR rather than being skipped, so a broken
    control can never be mistaken for a passing one.
    """
    config = config or ControlConfig()
    timestamp = run_timestamp or _now_iso()

    groups: list[tuple[str, Callable[[], list[dict]]]] = [
        ("MAT", lambda: _mat_controls(match_results, invoices, payments, config)),
        ("ICO", lambda: _ico_controls(ic_entries, ic_elimination, config)),
        ("SRC", lambda: _src_controls(ic_entries, shared_costs, config)),
        ("CVG", lambda: _cvg_controls(invoices, payments, shared_costs, ic_entries, config)),
    ]

    rows: list[dict] = []
    for group_name, runner in groups:
        try:
            rows.extend(runner())
        except Exception as exc:  # noqa: BLE001 - a failed control must surface, not vanish
            rows.append(
                _result(
                    f"{group_name}-ERROR",
                    f"{group_name} control group could not be evaluated",
                    group_name,
                    RECONCILIATION,
                    "The control group raised an exception and its conclusions are unknown.",
                    CRITICAL,
                    ERROR,
                    failed_count=1,
                    explanation=f"{type(exc).__name__}: {exc}",
                )
            )

    results = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    results["dataset_label"] = dataset_label
    results["run_timestamp"] = timestamp
    return results


def decide_close(
    control_results: pd.DataFrame,
    config: ControlConfig | None = None,
    dataset_label: str = "",
    run_timestamp: str | None = None,
) -> dict:
    """Turn control results into a deterministic close decision.

    ERROR anywhere               -> FAIL
    any CRITICAL control FAILed  -> FAIL
    any WARNING control WARNed   -> PASS_WITH_WARNINGS
    otherwise                    -> PASS
    """
    config = config or ControlConfig()
    timestamp = run_timestamp or _now_iso()

    errored = control_results[control_results["status"] == ERROR]
    critical_failed = control_results[
        (control_results["severity"] == CRITICAL) & (control_results["status"] == FAIL)
    ]
    warned = control_results[
        (control_results["severity"] == SEV_WARNING) & (control_results["status"] == WARNING)
    ]

    if len(errored):
        close_status = CLOSE_FAIL
    elif len(critical_failed):
        close_status = CLOSE_FAIL
    elif len(warned):
        close_status = CLOSE_PASS_WITH_WARNINGS
    else:
        close_status = CLOSE_PASS

    blocking = sorted(
        list(errored["check_id"].astype(str)) + list(critical_failed["check_id"].astype(str))
    )
    return {
        "close_status": close_status,
        "report_allowed": close_status in (CLOSE_PASS, CLOSE_PASS_WITH_WARNINGS),
        "critical_failed": int(len(critical_failed)),
        "errors": int(len(errored)),
        "warnings": int(len(warned)),
        "blocking_controls": blocking,
        "config_used": asdict(config),
        "run_timestamp": timestamp,
        "dataset_label": dataset_label,
        "controls_version": CONTROLS_VERSION,
    }


def summarise(control_results: pd.DataFrame) -> pd.DataFrame:
    """Compact view for printing: everything that is not a clean PASS."""
    return control_results.loc[
        control_results["status"] != PASS,
        ["check_id", "check_name", "severity", "status", "expected_value",
         "actual_value", "failed_count", "explanation"],
    ]


# ---------------------------------------------------------------------------
# CLI - the only part of this module that touches the filesystem
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
    from match import three_way_match  # noqa: E402

    parser = argparse.ArgumentParser(description="Run the Phase 1 close controls.")
    parser.add_argument("--data-dir", default="data/raw",
                        help="directory holding the source CSVs")
    parser.add_argument("--purchase-orders", default=None)
    parser.add_argument("--invoices", default=None)
    parser.add_argument("--payments", default=None)
    parser.add_argument("--shared-costs", default=None)
    parser.add_argument("--fx-rates", default=None)
    parser.add_argument("--ic-entries", default=None,
                        help="pre-generated intercompany entries CSV (used for E5/E6 testing)")
    parser.add_argument("--dataset-label", default=None)
    parser.add_argument("--out", default=None, help="path for control_results.csv")
    parser.add_argument("--out-decision", default=None, help="path for close_decision.json")
    args = parser.parse_args()

    base = Path(args.data_dir)
    read = lambda p: pd.read_csv(p, dtype=str)  # noqa: E731
    pos_df = read(args.purchase_orders or base / "purchase_orders.csv")
    invoices_df = read(args.invoices or base / "invoices.csv")
    payments_df = read(args.payments or base / "payments.csv")
    costs_df = read(args.shared_costs or base / "shared_costs.csv")
    fx_df = read(args.fx_rates or base / "fx_rates.csv")

    match_df = three_way_match(pos_df, invoices_df, payments_df)
    if args.ic_entries:
        entries_df = pd.read_csv(args.ic_entries)
    else:
        entries_df = generate_intercompany_entries(costs_df, fx_df)
    elimination_df = check_elimination(entries_df)

    label = args.dataset_label or str(base)
    results_df = run_controls(
        match_results=match_df,
        invoices=invoices_df,
        payments=payments_df,
        ic_entries=entries_df,
        ic_elimination=elimination_df,
        shared_costs=costs_df,
        purchase_orders=pos_df,
        dataset_label=label,
    )
    decision = decide_close(results_df, dataset_label=label)

    print(f"\nDataset: {label}")
    print(results_df["status"].value_counts().to_string())
    issues = summarise(results_df)
    if not issues.empty:
        print("\nControls not passing")
        print(issues.to_string(index=False))

    print("\nClose decision")
    for key in ("close_status", "report_allowed", "critical_failed", "errors", "warnings"):
        print(f"  {key}: {decision[key]}")
    if decision["blocking_controls"]:
        print(f"  blocking_controls: {', '.join(decision['blocking_controls'])}")

    if args.out:
        results_df.to_csv(args.out, index=False)
        print(f"\nControl results written to {args.out}")
    if args.out_decision:
        Path(args.out_decision).write_text(json.dumps(decision, indent=2), encoding="utf-8")
        print(f"Close decision written to {args.out_decision}")

    sys.exit(0 if decision["report_allowed"] else 1)

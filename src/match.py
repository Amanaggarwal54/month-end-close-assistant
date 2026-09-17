"""
Month-End Close Assistant - Step 4: three-way matching.

Connects Purchase Order -> Invoice -> Payment and assigns exactly one audit
status to every record, so that no invoice and no payment can disappear
silently during the close.

Design notes
------------
* Pure pandas + standard library. No external services, no database.
* Input DataFrames are never modified in place (every function works on copies).
* Output is deterministic: rows are sorted before returning.
* The amount tolerance is configurable and defaults to 0.01.

Status precedence (first match wins)
------------------------------------
1. DUPLICATE_INVOICE        - the same invoice_id appears more than once
2. NO_PO                    - invoice references a PO that does not exist (or no PO at all)
3. AMOUNT_MISMATCH          - invoice net != PO net, or an amount/currency cannot be
                              compared, or the invoice total is missing
4. UNPAID                   - no payment recorded against the invoice
5. PARTIALLY_PAID           - total paid < invoice total (beyond tolerance)
6. OVERPAID                 - total paid > invoice total (beyond tolerance)
7. MATCHED                  - PO, invoice and payments all agree within tolerance

PAYMENT_WITHOUT_INVOICE is assigned to payment records whose invoice_id does
not exist in the invoice ledger. Those records are reported as their own rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

DEFAULT_TOLERANCE: float = 0.01

# Kept as constants so reports and tests can refer to them without typos.
STATUS_MATCHED = "MATCHED"
STATUS_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
STATUS_NO_PO = "NO_PO"
STATUS_UNPAID = "UNPAID"
STATUS_PARTIALLY_PAID = "PARTIALLY_PAID"
STATUS_OVERPAID = "OVERPAID"
STATUS_PAYMENT_WITHOUT_INVOICE = "PAYMENT_WITHOUT_INVOICE"
STATUS_DUPLICATE_INVOICE = "DUPLICATE_INVOICE"

OUTPUT_COLUMNS: list[str] = [
    "record_type",
    "invoice_id",
    "po_id",
    "entity",
    "supplier",
    "invoice_currency",
    "po_currency",
    "po_net_amount",
    "invoice_net_amount",
    "invoice_total_amount",
    "total_paid",
    "payment_count",
    "payment_ids",
    "difference_to_po",
    "difference_to_invoice",
    "invoice_row_count",
    "status",
    "status_reason",
    "data_quality_flag",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_numeric(series: pd.Series) -> pd.Series:
    """Coerce a column to float; anything unparseable becomes NaN instead of raising."""
    return pd.to_numeric(series, errors="coerce")


def _clean_key(series: pd.Series) -> pd.Series:
    """Normalise an identifier column: string, trimmed, empty string -> NA.

    Keys arriving from CSV can carry stray whitespace or be read as floats when
    a column contains blanks. Normalising once here keeps every later join and
    grouping consistent.
    """
    cleaned = series.astype("string").str.strip()
    return cleaned.mask(cleaned.isin(["", "nan", "None", "<NA>"]))


def _within_tolerance(left: float, right: float, tolerance: float) -> bool:
    """True when both values are present and differ by no more than the tolerance."""
    if pd.isna(left) or pd.isna(right):
        return False
    return abs(float(left) - float(right)) <= tolerance


def _join_unique(values: Iterable) -> str:
    """Stable, readable list of ids for the audit trail (e.g. 'PAY9001; PAY9002')."""
    seen = [str(v) for v in values if pd.notna(v)]
    return "; ".join(sorted(dict.fromkeys(seen)))


# ---------------------------------------------------------------------------
# preparation steps
# ---------------------------------------------------------------------------
def _prepare_pos(pos: pd.DataFrame) -> pd.DataFrame:
    """One row per po_id with the fields needed for matching.

    If the PO ledger itself contains duplicate po_ids we keep the first one but
    record how many rows existed, so the anomaly is visible rather than hidden.
    """
    prepared = pos.copy()
    prepared["po_id"] = _clean_key(prepared["po_id"])
    prepared["net_amount"] = _to_numeric(prepared["net_amount"])

    prepared = prepared.rename(
        columns={
            "net_amount": "po_net_amount",
            "currency": "po_currency",
            "entity": "po_entity",
            "supplier": "po_supplier",
        }
    )
    keep = [c for c in ["po_id", "po_entity", "po_supplier", "po_currency", "po_net_amount"] if c in prepared]
    prepared = prepared[keep]

    counts = prepared.groupby("po_id", dropna=True).size().rename("po_row_count")
    prepared = prepared.drop_duplicates(subset="po_id", keep="first").merge(
        counts, left_on="po_id", right_index=True, how="left"
    )
    return prepared


def _aggregate_payments(payments: pd.DataFrame) -> pd.DataFrame:
    """Total the payments per invoice_id.

    An invoice can be settled in several instalments, so payment status must be
    judged on the aggregate, not on any single payment row.
    """
    prepared = payments.copy()
    prepared["invoice_id"] = _clean_key(prepared["invoice_id"])
    prepared["amount"] = _to_numeric(prepared["amount"])

    grouped = prepared.groupby("invoice_id", dropna=False).agg(
        total_paid=("amount", "sum"),
        payment_count=("payment_id", "size"),
        payment_ids=("payment_id", _join_unique),
        payment_currencies=("currency", _join_unique),
        payment_entities=("entity", _join_unique),
        payment_amounts_missing=("amount", lambda s: int(s.isna().sum())),
    )
    # sum() of an all-NaN group returns 0.0; keep that visible as a missing amount flag.
    return grouped.reset_index()


# ---------------------------------------------------------------------------
# status assignment
# ---------------------------------------------------------------------------
def _assign_status(row: pd.Series, tolerance: float) -> tuple[str, str]:
    """Return (status, reason) for one invoice row, applying the precedence above."""
    if row["invoice_row_count"] > 1:
        return (
            STATUS_DUPLICATE_INVOICE,
            f"invoice_id appears {int(row['invoice_row_count'])} times in the invoice ledger",
        )

    if pd.isna(row["po_id"]):
        return STATUS_NO_PO, "invoice carries no po_id"
    if not row["po_found"]:
        return STATUS_NO_PO, f"po_id {row['po_id']} not found in the purchase order ledger"

    if pd.isna(row["invoice_net_amount"]) or pd.isna(row["po_net_amount"]):
        return STATUS_AMOUNT_MISMATCH, "invoice or PO net amount is missing, amounts cannot be compared"

    po_currency = row.get("po_currency")
    inv_currency = row.get("invoice_currency")
    if pd.notna(po_currency) and pd.notna(inv_currency) and str(po_currency) != str(inv_currency):
        return (
            STATUS_AMOUNT_MISMATCH,
            f"currency differs between PO ({po_currency}) and invoice ({inv_currency})",
        )

    if not _within_tolerance(row["invoice_net_amount"], row["po_net_amount"], tolerance):
        return (
            STATUS_AMOUNT_MISMATCH,
            f"invoice net {row['invoice_net_amount']:.2f} vs PO net {row['po_net_amount']:.2f}",
        )

    # Data quality is checked before payment status: an invoice whose total is
    # unusable cannot be settled or chased, so the broken amount is the finding
    # that has to reach the reviewer, not the payment state derived from it.
    if pd.isna(row["invoice_total_amount"]):
        return STATUS_AMOUNT_MISMATCH, "invoice total amount is missing, payment cannot be verified"

    if row["payment_count"] == 0:
        return STATUS_UNPAID, "no payment recorded against this invoice"

    if row["payment_amounts_missing"] > 0:
        return STATUS_AMOUNT_MISMATCH, "one or more payment amounts are missing"

    difference = float(row["total_paid"]) - float(row["invoice_total_amount"])
    if abs(difference) <= tolerance:
        return STATUS_MATCHED, "PO, invoice and payments agree within tolerance"
    if difference < 0:
        return (
            STATUS_PARTIALLY_PAID,
            f"paid {row['total_paid']:.2f} of invoice total {row['invoice_total_amount']:.2f}",
        )
    return (
        STATUS_OVERPAID,
        f"paid {row['total_paid']:.2f} against invoice total {row['invoice_total_amount']:.2f}",
    )


def _data_quality_flag(row: pd.Series) -> str:
    """List the unusable fields on a row, independently of its status.

    A record can carry only one status, so this column keeps the second fact
    visible: an invoice reported as UNPAID or MATCHED may still have a missing
    VAT amount or a payment booked in another entity.
    """
    issues: list[str] = []
    if pd.isna(row.get("invoice_net_amount")):
        issues.append("invoice net amount missing")
    if pd.isna(row.get("invoice_total_amount")):
        issues.append("invoice total amount missing")
    if row.get("payment_amounts_missing", 0):
        issues.append("payment amount missing")
    if pd.isna(row.get("po_id")):
        issues.append("no po_id on invoice")
    return "; ".join(issues)


def _payment_only_rows(
    payment_totals: pd.DataFrame, known_invoice_ids: set[str]
) -> pd.DataFrame:
    """Build result rows for payments that reference no existing invoice.

    These are the records a naive inner join would delete, which is exactly the
    kind of exception a close is supposed to surface.
    """
    orphans = payment_totals[~payment_totals["invoice_id"].isin(known_invoice_ids)].copy()
    if orphans.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    out = pd.DataFrame(
        {
            "record_type": "PAYMENT_ONLY",
            "invoice_id": orphans["invoice_id"],
            "po_id": pd.NA,
            "entity": orphans["payment_entities"],
            "supplier": pd.NA,
            "invoice_currency": orphans["payment_currencies"],
            "po_currency": pd.NA,
            "po_net_amount": pd.NA,
            "invoice_net_amount": pd.NA,
            "invoice_total_amount": pd.NA,
            "total_paid": orphans["total_paid"],
            "payment_count": orphans["payment_count"],
            "payment_ids": orphans["payment_ids"],
            "difference_to_po": pd.NA,
            "difference_to_invoice": pd.NA,
            "invoice_row_count": 0,
            "status": STATUS_PAYMENT_WITHOUT_INVOICE,
            "status_reason": "payment references an invoice_id that is not in the invoice ledger",
            "data_quality_flag": "",
        }
    )
    return out[OUTPUT_COLUMNS]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def three_way_match(
    pos: pd.DataFrame,
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    tolerance: float = DEFAULT_TOLERANCE,
) -> pd.DataFrame:
    """Match purchase orders, invoices and payments and assign an audit status.

    Parameters
    ----------
    pos, invoices, payments:
        Ledgers in the project schema. They are not modified.
    tolerance:
        Absolute amount tolerance in currency units (default 0.01).

    Returns
    -------
    pandas.DataFrame
        One row per invoice record (duplicates kept as separate rows) plus one
        row per orphan payment group, sorted by status then invoice_id.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be zero or positive")

    po_ledger = _prepare_pos(pos)
    payment_totals = _aggregate_payments(payments)

    work = invoices.copy()
    work["invoice_id"] = _clean_key(work["invoice_id"])
    work["po_id"] = _clean_key(work["po_id"])
    for column in ("net_amount", "vat_amount", "total_amount"):
        if column in work:
            work[column] = _to_numeric(work[column])

    work = work.rename(
        columns={
            "net_amount": "invoice_net_amount",
            "total_amount": "invoice_total_amount",
            "currency": "invoice_currency",
        }
    )

    # Duplicate detection happens before any join, so both copies survive into
    # the output as evidence instead of being silently deduplicated.
    work["invoice_row_count"] = work.groupby("invoice_id", dropna=False)["invoice_id"].transform("size")

    work = work.merge(po_ledger, on="po_id", how="left")
    work["po_found"] = work["po_net_amount"].notna() | work.get(
        "po_row_count", pd.Series(pd.NA, index=work.index)
    ).notna()

    work = work.merge(payment_totals, on="invoice_id", how="left")
    work["payment_count"] = work["payment_count"].fillna(0).astype(int)
    work["payment_amounts_missing"] = work["payment_amounts_missing"].fillna(0).astype(int)
    work.loc[work["payment_count"] == 0, "total_paid"] = float("nan")
    work["payment_ids"] = work["payment_ids"].fillna("")

    # A duplicated invoice_id would otherwise double-count the same payments;
    # the payment figures stay on the row only for information, and the status
    # rule for duplicates fires first in any case.
    work["difference_to_po"] = work["invoice_net_amount"] - work["po_net_amount"]
    work["difference_to_invoice"] = work["total_paid"] - work["invoice_total_amount"]

    statuses = work.apply(lambda row: _assign_status(row, tolerance), axis=1)
    work["status"] = [s for s, _ in statuses]
    work["status_reason"] = [r for _, r in statuses]
    work["data_quality_flag"] = work.apply(_data_quality_flag, axis=1)
    work["record_type"] = "INVOICE"

    for column in OUTPUT_COLUMNS:
        if column not in work:
            work[column] = pd.NA
    invoice_rows = work[OUTPUT_COLUMNS]

    known_ids = set(work["invoice_id"].dropna().astype(str))
    orphan_rows = _payment_only_rows(payment_totals, known_ids)

    # Concatenating an empty frame of all-NA columns raises a pandas
    # FutureWarning, so the orphan block is only appended when it has rows.
    result = (
        pd.concat([invoice_rows, orphan_rows], ignore_index=True)
        if not orphan_rows.empty
        else invoice_rows.copy()
    )
    result = result.sort_values(
        by=["record_type", "invoice_id", "payment_ids"], na_position="last", kind="mergesort"
    ).reset_index(drop=True)

    for column in ("po_net_amount", "invoice_net_amount", "invoice_total_amount",
                   "total_paid", "difference_to_po", "difference_to_invoice"):
        result[column] = _to_numeric(result[column]).round(2)

    return result


def load_ledgers(
    po_path: str | Path,
    invoice_path: str | Path,
    payment_path: str | Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the three ledgers from CSV without any type guessing on id columns."""
    read = lambda path: pd.read_csv(path, dtype=str)  # noqa: E731 - keep ids as text
    return read(po_path), read(invoice_path), read(payment_path)


# ---------------------------------------------------------------------------
# manual run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the three-way match and print a summary.")
    parser.add_argument("--po", default="data/raw/purchase_orders.csv")
    parser.add_argument("--invoices", default="data/raw/invoices.csv")
    parser.add_argument("--payments", default="data/raw/payments.csv")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--out", default=None, help="optional path to write the result as CSV")
    args = parser.parse_args()

    pos_df, invoices_df, payments_df = load_ledgers(args.po, args.invoices, args.payments)
    result_df = three_way_match(pos_df, invoices_df, payments_df, tolerance=args.tolerance)

    print(f"\nInvoices in:  {len(invoices_df)}")
    print(f"Payments in:  {len(payments_df)}")
    print(f"Result rows:  {len(result_df)}\n")
    print("Status summary")
    print(result_df["status"].value_counts().to_string())

    exceptions = result_df[result_df["status"] != STATUS_MATCHED]
    if not exceptions.empty:
        print("\nExceptions")
        print(
            exceptions[
                ["invoice_id", "po_id", "entity", "po_net_amount", "invoice_net_amount",
                 "invoice_total_amount", "total_paid", "status", "status_reason"]
            ].to_string(index=False)
        )

    if args.out:
        result_df.to_csv(args.out, index=False)
        print(f"\nWritten to {args.out}")

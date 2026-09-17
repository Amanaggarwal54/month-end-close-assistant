"""
Month-End Close Assistant - Step 5: intercompany recharges and FX.

Turns the shared-cost ledger into balanced, two-sided intercompany entries and
provides an elimination check that proves the two sides agree.

Design notes
------------
* Pure pandas + standard library. No external services, no database.
* Input DataFrames are never modified in place.
* Nothing is hard-coded: entity currencies, markup, tolerance and FX all come
  from arguments or from the files passed in. In particular the module never
  substitutes a "known good" FX rate - it calculates from whatever rate table
  it is given, which is what makes the E4 test meaningful.
* Invalid input never becomes a plausible-looking accounting entry. It becomes
  an EXCEPTION row that carries the reason.

Accounting model
----------------
For one shared cost paid by entity P and shared with entity R:

    allocated_cost  = cost_amount * share_R          (in the cost's currency)
    recharge_amount = allocated_cost * (1 + markup)  (5% by default)

That recharge produces exactly two accounting sides:

    RECEIVABLE in P, booked in P's local currency
    PAYABLE    in R, booked in R's local currency

Both sides describe the same economic amount, so both carry the same
eur_equivalent, and both are linked by a stable pair_id.

The payer's own share is never recharged: it is the payer's own cost.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

DEFAULT_MARKUP_PCT: float = 0.05
DEFAULT_TOLERANCE: float = 0.01
BASE_CURRENCY: str = "EUR"

# Entity -> local (functional) currency. Passed in rather than assumed, so a new
# entity does not require a code change.
ENTITY_CURRENCY: dict[str, str] = {
    "BE01": "EUR",
    "US01": "USD",
    "BE02": "EUR",
}

ENTRY_RECEIVABLE = "RECEIVABLE"
ENTRY_PAYABLE = "PAYABLE"
ENTRY_EXCEPTION = "EXCEPTION"

STATUS_VALID = "VALID"
STATUS_NO_RECHARGE = "NO_RECHARGE"
STATUS_INVALID_SHARE_TOTAL = "INVALID_SHARE_TOTAL"
STATUS_MISSING_SHARE = "MISSING_SHARE"
STATUS_MISSING_AMOUNT = "MISSING_AMOUNT"
STATUS_MISSING_CURRENCY = "MISSING_CURRENCY"
STATUS_MISSING_FX = "MISSING_FX"
STATUS_INVALID_ENTITY = "INVALID_ENTITY"
STATUS_INVALID_MONTH = "INVALID_MONTH"

ENTRY_COLUMNS: list[str] = [
    "entry_id",
    "pair_id",
    "cost_id",
    "month",
    "entity",
    "counterparty_entity",
    "paying_entity",
    "receiving_entity",
    "entry_type",
    "currency",
    "local_amount",
    "eur_equivalent",
    "source_amount",
    "source_currency",
    "share_pct",
    "allocated_cost",
    "markup_pct",
    "fx_rate_eur_usd",
    "fx_rate_required",
    "description",
    "status",
    "status_reason",
    "data_quality_flag",
]

ELIMINATION_COLUMNS: list[str] = [
    "pair_id",
    "cost_id",
    "month",
    "paying_entity",
    "receiving_entity",
    "receivable_eur",
    "payable_eur",
    "difference_eur",
    "receivable_local",
    "payable_local",
    "fx_rate_eur_usd",
    "check_status",
    "check_reason",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _round2(value: float | Decimal | None) -> float | None:
    """Round to 2 decimals with ROUND_HALF_UP.

    Python's round() uses banker's rounding, so 2.675 -> 2.67. Accounting
    expects 2.68. Decimal with ROUND_HALF_UP gives the result a finance
    reviewer would get by hand, which matters when someone re-performs a
    calculation in Excel during a review.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _to_float(value) -> float | None:
    """Parse a value to float, returning None for blanks and unparseable input."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


def _clean_text(value) -> str | None:
    """Normalise a text field: trimmed string, or None when empty."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _month_key(value) -> str | None:
    """Normalise any month representation to 'YYYY-MM'.

    The ledger uses month-end dates ('2026-01-31') while the FX table is keyed
    the same way, but a plain '2026-01' must also resolve. Normalising both
    sides to a period key makes the join independent of that formatting.
    """
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return pd.Period(pd.to_datetime(text), freq="M").strftime("%Y-%m")
    except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime):
        return None


def _build_fx_lookup(fx_rates: pd.DataFrame) -> dict[str, float]:
    """month key -> EUR/USD rate, read from whichever rate file was supplied."""
    lookup: dict[str, float] = {}
    for _, row in fx_rates.copy().iterrows():
        key = _month_key(row.get("month_end"))
        rate = _to_float(row.get("eur_usd"))
        if key is not None and rate is not None and rate > 0:
            lookup[key] = rate
    return lookup


def _convert(amount: float, from_currency: str, to_currency: str, eur_usd: float | None) -> float | None:
    """Convert between EUR and USD using 1 EUR = eur_usd USD.

    Returns None when the conversion needs a rate that was not supplied, so the
    caller can raise MISSING_FX instead of inventing a number.
    """
    if from_currency == to_currency:
        return amount
    if eur_usd is None or eur_usd <= 0:
        return None
    if from_currency == "EUR" and to_currency == "USD":
        return amount * eur_usd
    if from_currency == "USD" and to_currency == "EUR":
        return amount / eur_usd
    return None


def _exception_row(
    cost_row: pd.Series,
    status: str,
    reason: str,
    flag: str = "",
) -> dict:
    """An EXCEPTION row keeps an unusable shared cost visible in the output."""
    cost_id = _clean_text(cost_row.get("cost_id")) or "UNKNOWN"
    return {
        "entry_id": f"{cost_id}-EXC",
        "pair_id": pd.NA,
        "cost_id": cost_id,
        "month": _clean_text(cost_row.get("month")),
        "entity": _clean_text(cost_row.get("paying_entity")),
        "counterparty_entity": pd.NA,
        "paying_entity": _clean_text(cost_row.get("paying_entity")),
        "receiving_entity": pd.NA,
        "entry_type": ENTRY_EXCEPTION,
        "currency": _clean_text(cost_row.get("currency")),
        "local_amount": pd.NA,
        "eur_equivalent": pd.NA,
        "source_amount": _to_float(cost_row.get("amount")),
        "source_currency": _clean_text(cost_row.get("currency")),
        "share_pct": pd.NA,
        "allocated_cost": pd.NA,
        "markup_pct": pd.NA,
        "fx_rate_eur_usd": pd.NA,
        "fx_rate_required": pd.NA,
        "description": _clean_text(cost_row.get("description")),
        "status": status,
        "status_reason": reason,
        "data_quality_flag": flag or status,
    }


# ---------------------------------------------------------------------------
# entry generation
# ---------------------------------------------------------------------------
def generate_intercompany_entries(
    shared_costs: pd.DataFrame,
    fx_rates: pd.DataFrame,
    markup_pct: float = DEFAULT_MARKUP_PCT,
    tolerance: float = DEFAULT_TOLERANCE,
    entity_currency: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Generate two-sided intercompany recharge entries from shared costs.

    Every valid share produces one RECEIVABLE in the paying entity and one
    PAYABLE in the receiving entity, linked by pair_id and carrying the same
    eur_equivalent. Shared costs that cannot be processed produce an EXCEPTION
    row instead, never a balanced-looking entry.

    Parameters
    ----------
    shared_costs, fx_rates:
        Ledgers in the project schema; not modified.
    markup_pct:
        Intercompany markup applied to the receiver's allocated share (0.05).
    tolerance:
        Tolerance used when testing whether the share columns sum to 1.00.
    entity_currency:
        Entity -> local currency map; defaults to the project's three entities.

    Returns
    -------
    pandas.DataFrame
        Entry-level output sorted by cost_id, receiving entity and entry type.
    """
    if markup_pct < 0:
        raise ValueError("markup_pct must be zero or positive")
    if tolerance < 0:
        raise ValueError("tolerance must be zero or positive")

    currencies = dict(entity_currency or ENTITY_CURRENCY)
    fx_lookup = _build_fx_lookup(fx_rates)
    share_columns = [c for c in shared_costs.columns if c.startswith("share_")]

    records: list[dict] = []

    for _, cost in shared_costs.copy().iterrows():
        cost_id = _clean_text(cost.get("cost_id")) or "UNKNOWN"
        payer = _clean_text(cost.get("paying_entity"))
        source_currency = _clean_text(cost.get("currency"))
        source_amount = _to_float(cost.get("amount"))
        month_label = _clean_text(cost.get("month"))
        month_key = _month_key(cost.get("month"))
        description = _clean_text(cost.get("description"))

        # --- row-level validation, before any arithmetic ---------------------
        if payer is None or payer not in currencies:
            records.append(_exception_row(cost, STATUS_INVALID_ENTITY,
                                          f"paying entity '{payer}' is not a known entity"))
            continue
        if source_amount is None:
            records.append(_exception_row(cost, STATUS_MISSING_AMOUNT,
                                          "shared cost amount is missing or not numeric"))
            continue
        if source_currency is None or source_currency not in {"EUR", "USD"}:
            records.append(_exception_row(cost, STATUS_MISSING_CURRENCY,
                                          f"shared cost currency '{source_currency}' is missing or unsupported"))
            continue
        if month_key is None:
            records.append(_exception_row(cost, STATUS_INVALID_MONTH,
                                          f"month '{month_label}' could not be parsed"))
            continue

        shares: dict[str, float | None] = {}
        for column in share_columns:
            shares[column.replace("share_", "")] = _to_float(cost.get(column))

        unknown_entities = [e for e in shares if e not in currencies]
        if unknown_entities:
            records.append(_exception_row(cost, STATUS_INVALID_ENTITY,
                                          f"share column(s) for unknown entit(ies): {', '.join(sorted(unknown_entities))}"))
            continue
        if any(value is None for value in shares.values()):
            missing = sorted(e for e, v in shares.items() if v is None)
            records.append(_exception_row(cost, STATUS_MISSING_SHARE,
                                          f"share missing for {', '.join(missing)}"))
            continue

        share_total = sum(float(v) for v in shares.values())
        if abs(share_total - 1.0) > tolerance:
            records.append(_exception_row(cost, STATUS_INVALID_SHARE_TOTAL,
                                          f"shares sum to {share_total:.4f}, expected 1.0000"))
            continue

        rate = fx_lookup.get(month_key)

        # The EUR equivalent of the underlying cost needs a rate whenever the
        # cost itself is not in EUR.
        if source_currency != BASE_CURRENCY and rate is None:
            records.append(_exception_row(cost, STATUS_MISSING_FX,
                                          f"no EUR/USD rate for {month_key}"))
            continue

        # --- generate one pair per receiving entity --------------------------
        receivers = [e for e, v in shares.items() if e != payer and float(v) > 0]
        if not receivers:
            records.append(_exception_row(cost, STATUS_NO_RECHARGE,
                                          "no other entity carries a share, the payer bears the whole cost"))
            continue

        payer_currency = currencies[payer]

        for receiver in sorted(receivers):
            receiver_currency = currencies[receiver]
            share = float(shares[receiver])

            # A recharge crossing currencies needs the rate even when the cost
            # is in EUR, so check per pair rather than per cost.
            if rate is None and len({source_currency, payer_currency, receiver_currency}) > 1:
                records.append(_exception_row(cost, STATUS_MISSING_FX,
                                              f"no EUR/USD rate for {month_key} (pair {payer}->{receiver})"))
                continue

            allocated_cost = source_amount * share
            recharge_source = allocated_cost * (1.0 + markup_pct)

            # One economic amount, computed once in EUR at full precision and
            # rounded once. Both sides then present that same value in their own
            # local currency, so the pair can never drift apart by rounding.
            eur_amount = _convert(recharge_source, source_currency, BASE_CURRENCY, rate)
            if eur_amount is None:
                records.append(_exception_row(cost, STATUS_MISSING_FX,
                                              f"cannot convert {source_currency} to EUR for {month_key}"))
                continue

            eur_equivalent = _round2(eur_amount)
            pair_id = f"{cost_id}-{payer}-{receiver}"
            fx_required = len({source_currency, payer_currency, receiver_currency}) > 1

            for entry_type, entity, counterparty in (
                (ENTRY_RECEIVABLE, payer, receiver),
                (ENTRY_PAYABLE, receiver, payer),
            ):
                local_currency = currencies[entity]
                local_amount = _convert(eur_amount, BASE_CURRENCY, local_currency, rate)
                records.append(
                    {
                        "entry_id": f"{pair_id}-{'R' if entry_type == ENTRY_RECEIVABLE else 'P'}",
                        "pair_id": pair_id,
                        "cost_id": cost_id,
                        "month": month_label,
                        "entity": entity,
                        "counterparty_entity": counterparty,
                        "paying_entity": payer,
                        "receiving_entity": receiver,
                        "entry_type": entry_type,
                        "currency": local_currency,
                        "local_amount": _round2(local_amount),
                        "eur_equivalent": eur_equivalent,
                        "source_amount": _round2(source_amount),
                        "source_currency": source_currency,
                        "share_pct": round(share, 6),
                        "allocated_cost": _round2(allocated_cost),
                        "markup_pct": markup_pct,
                        "fx_rate_eur_usd": rate,
                        "fx_rate_required": fx_required,
                        "description": description,
                        "status": STATUS_VALID,
                        "status_reason": "recharge generated from validated shared cost",
                        "data_quality_flag": "",
                    }
                )

    entries = pd.DataFrame.from_records(records, columns=ENTRY_COLUMNS)
    if entries.empty:
        return entries

    entries = entries.sort_values(
        by=["cost_id", "receiving_entity", "entry_type", "entity"],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    return entries


# ---------------------------------------------------------------------------
# elimination check
# ---------------------------------------------------------------------------
def check_elimination(entries: pd.DataFrame, tolerance: float = DEFAULT_TOLERANCE) -> pd.DataFrame:
    """Verify that every intercompany pair eliminates to zero in EUR.

    Detects, per pair: a missing receivable, a missing payable, a blank amount,
    duplicated sides, an EUR difference beyond tolerance, and an FX rate that
    differs between the two sides. These are exactly the conditions the later
    E5 (deleted side) and E6 (blanked amount) tests will create.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be zero or positive")

    accounting = entries[entries["entry_type"].isin([ENTRY_RECEIVABLE, ENTRY_PAYABLE])].copy()
    if accounting.empty:
        return pd.DataFrame(columns=ELIMINATION_COLUMNS)

    accounting["eur_equivalent"] = pd.to_numeric(accounting["eur_equivalent"], errors="coerce")
    accounting["local_amount"] = pd.to_numeric(accounting["local_amount"], errors="coerce")

    rows: list[dict] = []
    for pair_id, group in accounting.groupby("pair_id", dropna=False, sort=True):
        receivables = group[group["entry_type"] == ENTRY_RECEIVABLE]
        payables = group[group["entry_type"] == ENTRY_PAYABLE]

        first = group.iloc[0]
        record = {
            "pair_id": pair_id,
            "cost_id": first.get("cost_id"),
            "month": first.get("month"),
            "paying_entity": first.get("paying_entity"),
            "receiving_entity": first.get("receiving_entity"),
            "receivable_eur": receivables["eur_equivalent"].sum(min_count=1),
            "payable_eur": payables["eur_equivalent"].sum(min_count=1),
            "receivable_local": receivables["local_amount"].sum(min_count=1),
            "payable_local": payables["local_amount"].sum(min_count=1),
            "fx_rate_eur_usd": first.get("fx_rate_eur_usd"),
            "difference_eur": pd.NA,
            "check_status": "OK",
            "check_reason": "receivable and payable eliminate within tolerance",
        }

        if receivables.empty:
            record.update(check_status="MISSING_RECEIVABLE",
                          check_reason="no receivable side found for this pair")
        elif payables.empty:
            record.update(check_status="MISSING_PAYABLE",
                          check_reason="no payable side found for this pair")
        elif len(receivables) > 1 or len(payables) > 1:
            record.update(check_status="DUPLICATE_SIDE",
                          check_reason=f"expected one receivable and one payable, found "
                                       f"{len(receivables)} and {len(payables)}")
        elif pd.isna(record["receivable_eur"]) or pd.isna(record["payable_eur"]):
            record.update(check_status="MISSING_AMOUNT",
                          check_reason="one side has no EUR amount, elimination cannot be tested")
        else:
            difference = float(record["receivable_eur"]) - float(record["payable_eur"])
            record["difference_eur"] = _round2(difference)
            rates = {r for r in group["fx_rate_eur_usd"].dropna().unique()}
            if abs(difference) > tolerance:
                record.update(check_status="AMOUNT_MISMATCH",
                              check_reason=f"receivable and payable differ by EUR {difference:.2f}")
            elif len(rates) > 1:
                record.update(check_status="FX_INCONSISTENT",
                              check_reason=f"the two sides used different FX rates: {sorted(rates)}")

        rows.append(record)

    result = pd.DataFrame(rows, columns=ELIMINATION_COLUMNS)
    return result.sort_values(by=["check_status", "pair_id"], kind="mergesort").reset_index(drop=True)


def elimination_summary(checks: pd.DataFrame) -> dict:
    """Condense the pair-level checks into a single pass/fail result."""
    if checks.empty:
        return {"pairs": 0, "passed": 0, "failed": 0, "all_eliminated": True,
                "total_receivable_eur": 0.0, "total_payable_eur": 0.0, "net_eur": 0.0}

    failed = checks[checks["check_status"] != "OK"]
    receivable = pd.to_numeric(checks["receivable_eur"], errors="coerce").sum()
    payable = pd.to_numeric(checks["payable_eur"], errors="coerce").sum()
    return {
        "pairs": int(len(checks)),
        "passed": int(len(checks) - len(failed)),
        "failed": int(len(failed)),
        "all_eliminated": bool(failed.empty),
        "total_receivable_eur": _round2(receivable),
        "total_payable_eur": _round2(payable),
        "net_eur": _round2(receivable - payable),
    }


def load_inputs(shared_costs_path: str | Path, fx_rates_path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the shared-cost ledger and FX table, keeping ids and dates as text."""
    return pd.read_csv(shared_costs_path, dtype=str), pd.read_csv(fx_rates_path, dtype=str)


# ---------------------------------------------------------------------------
# manual run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate intercompany recharges and check elimination.")
    parser.add_argument("--shared-costs", default="data/raw/shared_costs.csv")
    parser.add_argument("--fx-rates", default="data/raw/fx_rates.csv")
    parser.add_argument("--markup", type=float, default=DEFAULT_MARKUP_PCT)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--out", default=None, help="optional path for the entries CSV")
    parser.add_argument("--out-checks", default=None, help="optional path for the elimination CSV")
    args = parser.parse_args()

    costs_df, fx_df = load_inputs(args.shared_costs, args.fx_rates)
    entries_df = generate_intercompany_entries(
        costs_df, fx_df, markup_pct=args.markup, tolerance=args.tolerance
    )
    checks_df = check_elimination(entries_df, tolerance=args.tolerance)
    summary = elimination_summary(checks_df)

    print(f"\nShared cost rows:      {len(costs_df)}")
    print(f"FX rates supplied:     {len(fx_df)}")
    print(f"Intercompany entries:  {len(entries_df[entries_df['entry_type'] != ENTRY_EXCEPTION])}")
    print(f"Exception rows:        {len(entries_df[entries_df['entry_type'] == ENTRY_EXCEPTION])}\n")

    print("Status summary")
    print(entries_df["status"].value_counts().to_string())

    print("\nFX rates used by month")
    used = (
        entries_df.loc[entries_df["entry_type"] != ENTRY_EXCEPTION, ["month", "fx_rate_eur_usd"]]
        .drop_duplicates()
        .sort_values("month")
    )
    print(used.to_string(index=False))

    print("\nElimination check")
    print(f"  pairs tested:        {summary['pairs']}")
    print(f"  passed:              {summary['passed']}")
    print(f"  failed:              {summary['failed']}")
    print(f"  receivables (EUR):   {summary['total_receivable_eur']:,.2f}")
    print(f"  payables (EUR):      {summary['total_payable_eur']:,.2f}")
    print(f"  net (EUR):           {summary['net_eur']:,.2f}")
    print(f"  all eliminated:      {summary['all_eliminated']}")

    failures = checks_df[checks_df["check_status"] != "OK"]
    if not failures.empty:
        print("\nElimination exceptions")
        print(failures.to_string(index=False))

    exceptions_df = entries_df[entries_df["entry_type"] == ENTRY_EXCEPTION]
    if not exceptions_df.empty:
        print("\nShared-cost exceptions")
        print(exceptions_df[["cost_id", "month", "paying_entity", "status", "status_reason"]].to_string(index=False))

    if args.out:
        entries_df.to_csv(args.out, index=False)
        print(f"\nEntries written to {args.out}")
    if args.out_checks:
        checks_df.to_csv(args.out_checks, index=False)
        print(f"Elimination checks written to {args.out_checks}")

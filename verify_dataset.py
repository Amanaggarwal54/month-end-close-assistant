"""
Verify the regenerated datasets against the nine acceptance checks.

Read-only: it opens the CSVs and reports. It changes nothing.

    python verify_dataset.py
    python verify_dataset.py --raw data/raw --error data/error_data
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

SCOPE = {"2026-01", "2026-02", "2026-03"}
CLEAN_FX = {"2026-01-31": 1.08, "2026-02-28": 1.09, "2026-03-31": 1.07}
ERROR_FX = {"2026-01-31": 1.08, "2026-02-28": 1.19, "2026-03-31": 1.07}
PLANTED_IDS = {"E1", "E2", "E3", "E4", "E5", "E6"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def month_keys(series: pd.Series) -> set[str]:
    return {
        pd.Period(pd.to_datetime(v), freq="M").strftime("%Y-%m")
        for v in series.dropna()
    }


def main(raw: Path, error: Path) -> int:
    inv = pd.read_csv(raw / "invoices.csv", dtype=str)
    pay = pd.read_csv(raw / "payments.csv", dtype=str)
    pos = pd.read_csv(raw / "purchase_orders.csv", dtype=str)
    costs = pd.read_csv(raw / "shared_costs.csv", dtype=str)
    fx = pd.read_csv(raw / "fx_rates.csv")
    inv_e = pd.read_csv(error / "invoices.csv", dtype=str)
    pay_e = pd.read_csv(error / "payments.csv", dtype=str)
    fx_e = pd.read_csv(error / "fx_rates.csv")
    # The manifest is written by the --with-errors run; fall back to the clean
    # directory only if the error directory has none.
    manifest_path = error / "planted_errors.csv"
    if not manifest_path.exists() or manifest_path.stat().st_size < 30:
        manifest_path = raw / "planted_errors.csv"
    planted = pd.read_csv(manifest_path, dtype=str)

    # 1 clean invoice dates in scope
    out_of_scope = month_keys(inv["invoice_date"]) - SCOPE
    check("1. clean invoice dates within Jan-Mar 2026", not out_of_scope,
          f"out of scope: {sorted(out_of_scope)}" if out_of_scope else "")

    # 1b invoice month equals PO month (the actual rule being fixed)
    merged = inv.merge(pos[["po_id", "po_date"]], on="po_id", how="left")
    same_month = (
        merged["invoice_date"].map(lambda v: str(v)[:7])
        == merged["po_date"].map(lambda v: str(v)[:7])
    )
    check("1b. every invoice falls in the same month as its PO", bool(same_month.all()),
          f"{(~same_month).sum()} invoice(s) drift to another month: "
          f"{merged.loc[~same_month, 'invoice_id'].tolist()[:5]}")

    # 2 error invoice dates in scope
    out_e = month_keys(inv_e["invoice_date"]) - SCOPE
    check("2. error invoice dates within Jan-Mar 2026", not out_e,
          f"out of scope: {sorted(out_e)}" if out_e else "")

    # 3 E1-E4 still planted
    e1 = inv_e[inv_e["invoice_id"] == "INV5001"]
    e1_ok = (not e1.empty) and float(e1.iloc[0]["net_amount"]) == 780.00
    po_for_e1 = pos[pos["po_id"] == (e1.iloc[0]["po_id"] if not e1.empty else "")]
    e1_po_ok = (not po_for_e1.empty) and float(po_for_e1.iloc[0]["net_amount"]) == 800.00
    check("3a. E1 invoice net 780 vs PO 800", e1_ok and e1_po_ok)

    dupes = inv_e[inv_e["invoice_id"].duplicated(keep=False)]["invoice_id"].unique()
    check("3b. E2 duplicate invoice_id present", len(dupes) == 1, f"duplicated: {list(dupes)}")

    orphan = pay_e[~pay_e["invoice_id"].isin(inv_e["invoice_id"])]
    check("3c. E3 orphan payment present", len(orphan) == 1,
          f"orphans: {orphan['payment_id'].tolist()}")

    feb_error = fx_e.loc[fx_e["month_end"] == "2026-02-28", "eur_usd"]
    check("3d. E4 February rate is 1.19 in error data",
          not feb_error.empty and float(feb_error.iloc[0]) == 1.19)

    # 4 clean population sizes
    check("4. clean data has 60 invoices, 60 payments, 10 shared costs",
          len(inv) == 60 and len(pay) == 60 and len(costs) == 10,
          f"invoices={len(inv)} payments={len(pay)} shared_costs={len(costs)}")

    # 5 / 6 error population sizes
    check("5. error data has 61 invoice rows", len(inv_e) == 61, f"rows={len(inv_e)}")
    check("6. error data has 61 payment rows", len(pay_e) == 61, f"rows={len(pay_e)}")

    # 7 / 8 FX tables
    actual_clean = {r.month_end: float(r.eur_usd) for r in fx.itertuples()}
    check("7. clean FX is 1.08 / 1.09 / 1.07", actual_clean == CLEAN_FX, str(actual_clean))
    actual_error = {r.month_end: float(r.eur_usd) for r in fx_e.itertuples()}
    check("8. error FX is 1.08 / 1.19 / 1.07", actual_error == ERROR_FX, str(actual_error))

    # 9 manifest
    check("9. planted_errors.csv lists E1-E6",
          set(planted["error_id"]) == PLANTED_IDS,
          f"{sorted(planted['error_id'])} in {manifest_path}")

    # extra: invoice ids unchanged in the clean set
    check("10. clean invoice ids are contiguous INV5001..INV5060",
          list(inv["invoice_id"]) == [f"INV{5000 + i}" for i in range(1, 61)])

    width = max(len(name) for name, _, _ in results)
    failures = 0
    for name, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        line = f"{flag}  {name.ljust(width)}"
        print(line if ok or not detail else f"{line}  {detail}")
    print(f"\n{len(results) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the regenerated datasets.")
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--error", default="data/error_data")
    args = parser.parse_args()
    raise SystemExit(main(Path(args.raw), Path(args.error)))

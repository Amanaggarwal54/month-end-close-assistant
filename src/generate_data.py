from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ENTITIES = {
    "BE01": {"currency": "EUR", "vat_rate": 0.21},
    "US01": {"currency": "USD", "vat_rate": 0.00},
    "BE02": {"currency": "EUR", "vat_rate": 0.21},
}
MONTH_ENDS = pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"])
CANONICAL_FX = {"2026-01-31": 1.08, "2026-02-28": 1.09, "2026-03-31": 1.07}
SUPPLIERS = [
    "Northstar Components", "Blue River Services", "Delta Industrial",
    "Maple Leaf Supplies", "Orion Office Solutions", "Vertex Logistics",
    "Summit Technology", "Acorn Facilities", "Atlas Consulting", "Harbor Trading",
]


def _money(x: float) -> float:
    return round(float(x), 2)


def generate_data(with_errors: bool = False, seed: int = 20260917) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)

    # -------------------------
    # Purchase orders (~60 rows)
    # -------------------------
    po_rows = []
    po_counter = 1001
    invoice_counter = 5001
    payment_counter = 9001

    for entity, meta in ENTITIES.items():
        for i in range(20):
            month = MONTH_ENDS[i % 3]
            po_date = month - pd.Timedelta(days=int(rng.integers(3, 24)))
            supplier = str(rng.choice(SUPPLIERS))
            # Lock the first PO at 800.00 so planted error E1 is exactly 780 vs 800.
            net_amount = 800.00 if po_counter == 1001 else _money(rng.uniform(600, 18000))
            po_rows.append({
                "po_id": f"PO{po_counter}",
                "entity": entity,
                "supplier": supplier,
                "currency": meta["currency"],
                "net_amount": net_amount,
                "po_date": po_date.date().isoformat(),
            })
            po_counter += 1

    pos = pd.DataFrame(po_rows)

    # -------------------------
    # Invoices (one per PO)
    # -------------------------
    invoice_rows = []
    for idx, po in pos.iterrows():
        meta = ENTITIES[po.entity]
        invoice_date = pd.to_datetime(po.po_date) + pd.Timedelta(days=int(rng.integers(1, 12)))
        net = _money(po.net_amount)
        vat = _money(net * meta["vat_rate"])
        total = _money(net + vat)
        invoice_rows.append({
            "invoice_id": f"INV{invoice_counter}",
            "po_id": po.po_id,
            "entity": po.entity,
            "supplier": po.supplier,
            "invoice_date": invoice_date.date().isoformat(),
            "currency": po.currency,
            "net_amount": net,
            "vat_amount": vat,
            "total_amount": total,
        })
        invoice_counter += 1

    invoices = pd.DataFrame(invoice_rows)

    # -------------------------
    # Payments: clean base = exact payment of every invoice
    # -------------------------
    payment_rows = []
    for _, inv in invoices.iterrows():
        payment_date = pd.to_datetime(inv.invoice_date) + pd.Timedelta(days=int(rng.integers(2, 20)))
        payment_rows.append({
            "payment_id": f"PAY{payment_counter}",
            "invoice_id": inv.invoice_id,
            "entity": inv.entity,
            "payment_date": payment_date.date().isoformat(),
            "currency": inv.currency,
            "amount": inv.total_amount,
        })
        payment_counter += 1
    payments = pd.DataFrame(payment_rows)

    # -------------------------
    # Shared costs: payer's own share = 0; receivers split 100%
    # -------------------------
    shared_rows = []
    cost_counter = 1
    descriptions = [
        "Cloud hosting", "Group audit", "Insurance", "Legal services",
        "IT support", "Shared HR platform", "Travel booking platform",
        "Group software licence", "Treasury fees", "Office facilities",
    ]
    for cost_index in range(10):
        month = MONTH_ENDS[cost_index % len(MONTH_ENDS)]
        payer = str(rng.choice(list(ENTITIES)))
        receivers = [e for e in ENTITIES if e != payer]
        share_a = round(float(rng.uniform(0.30, 0.70)), 4)
        share_b = round(1 - share_a, 4)
        shares = {e: 0.0 for e in ENTITIES}
        shares[receivers[0]] = share_a
        shares[receivers[1]] = share_b
        currency = ENTITIES[payer]["currency"]
        amount = _money(rng.uniform(3000, 30000))
        shared_rows.append({
            "cost_id": f"COST{cost_counter:03d}",
            "paying_entity": payer,
            "description": descriptions[cost_counter - 1],
            "month": month.date().isoformat(),
            "currency": currency,
            "amount": amount,
            "share_BE01": shares["BE01"],
            "share_US01": shares["US01"],
            "share_BE02": shares["BE02"],
        })
        cost_counter += 1
    shared_costs = pd.DataFrame(shared_rows)

    fx_rates = pd.DataFrame({
        "month_end": [d.date().isoformat() for d in MONTH_ENDS],
        "eur_usd": [CANONICAL_FX[d.date().isoformat()] for d in MONTH_ENDS],
    })
    fx_rates_expected = fx_rates.copy()

    planted_errors = []
    planted_error_columns = ["error_id", "file", "reference", "description"]

    if with_errors:
        # E1: invoice net differs from its PO by 20.00.
        target = invoices.iloc[0].copy()
        old_net = float(target.net_amount)
        new_net = _money(old_net - 20.00)
        vat_rate = ENTITIES[target.entity]["vat_rate"]
        invoices.loc[invoices.invoice_id == target.invoice_id, "net_amount"] = new_net
        invoices.loc[invoices.invoice_id == target.invoice_id, "vat_amount"] = _money(new_net * vat_rate)
        invoices.loc[invoices.invoice_id == target.invoice_id, "total_amount"] = _money(new_net * (1 + vat_rate))
        planted_errors.append({"error_id": "E1", "file": "invoices.csv", "reference": target.invoice_id,
                               "description": f"invoice net {new_net:.2f} vs PO {old_net:.2f}"})

        # E2: add an additional invoice row with an existing invoice_id.
        duplicate = invoices.iloc[1].copy()
        invoices = pd.concat([invoices, duplicate.to_frame().T], ignore_index=True)
        planted_errors.append({"error_id": "E2", "file": "invoices.csv", "reference": duplicate.invoice_id,
                               "description": "duplicate invoice_id row added"})

        # E3: payment referencing an invoice_id that does not exist.
        orphan = payments.iloc[0].copy()
        orphan["payment_id"] = f"PAY{payment_counter}"
        orphan["invoice_id"] = "INV99999"
        orphan["amount"] = _money(float(orphan.amount) * 0.75)
        payments = pd.concat([payments, orphan.to_frame().T], ignore_index=True)
        planted_errors.append({"error_id": "E3", "file": "payments.csv", "reference": "INV99999",
                               "description": "payment points to non-existent invoice"})

        # E4: corrupt a single FX rate. The clean/reference rate remains in fx_rates_expected.csv
        fx_rates.loc[fx_rates.month_end == "2026-02-28", "eur_usd"] = 1.19
        planted_errors.append({"error_id": "E4", "file": "fx_rates.csv", "reference": "2026-02-28",
                               "description": "EUR/USD rate deliberately changed from 1.09 to 1.19"})

        # E5 and E6 are downstream posting/output errors. Record the test intent without changing raw sources.
        planted_errors.append({"error_id": "E5", "file": "DOWNSTREAM", "reference": "intercompany_entries",
                               "description": "delete one side of a generated intercompany pair"})
        planted_errors.append({"error_id": "E6", "file": "DOWNSTREAM", "reference": "intercompany_entries",
                               "description": "blank one generated intercompany amount"})

    out = {
        "purchase_orders.csv": pos,
        "invoices.csv": invoices,
        "payments.csv": payments,
        "shared_costs.csv": shared_costs,
        "fx_rates.csv": fx_rates,
        # Test-control copy: this is intentionally not an operational source file.
        "fx_rates_expected.csv": fx_rates_expected,
    }

    manifest = pd.DataFrame(planted_errors, columns=planted_error_columns)
    return out, manifest


def write_outputs(output_dir: Path, with_errors: bool, seed: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames, manifest = generate_data(with_errors=with_errors, seed=seed)
    for filename, frame in frames.items():
        frame.to_csv(output_dir / filename, index=False)
    manifest.to_csv(output_dir / "planted_errors.csv", index=False)

    print(f"Generated {len(frames['purchase_orders.csv'])} POs, "
          f"{len(frames['invoices.csv'])} invoice rows, "
          f"{len(frames['payments.csv'])} payments, "
          f"{len(frames['shared_costs.csv'])} shared costs.")
    if with_errors:
        print("Planted errors:")
        for row in manifest.to_dict(orient="records"):
            print(f"  {row['error_id']}: {row['description']}")
    else:
        print("Clean dataset generated (no planted errors).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic month-end close data.")
    parser.add_argument("--with-errors", action="store_true", help="Inject E1-E4 into raw data and record E5-E6 as downstream test cases.")
    parser.add_argument("--seed", type=int, default=20260917, help="Random seed for deterministic data generation.")
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw"), help="Directory for generated CSV files.")
    args = parser.parse_args()
    write_outputs(args.output_dir, args.with_errors, args.seed)

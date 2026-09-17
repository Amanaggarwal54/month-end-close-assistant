from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
import pdfplumber

FIELDS = [
    "invoice_id", "po_id", "invoice_date", "entity", "supplier", "currency",
    "net_amount", "vat_amount", "total_amount",
]


def _value(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}:\s*(.+)"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"Could not find '{label}'")
    return match.group(1).strip()


def _line(text: str, label: str) -> str:
    pattern = rf"^{re.escape(label)}\s+(.+)$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find line starting with '{label}'")
    return match.group(1).strip()


def _amount(text: str, label: str) -> float:
    raw = _line(text, label)
    number = raw.split()[-1].replace(",", "")
    return float(number)


def _currency(text: str) -> str:
    raw = _line(text, "Goods / services per purchase order")
    currency = raw.split()[0]
    if currency not in {"EUR", "USD"}:
        raise ValueError(f"Unexpected currency '{currency}'")
    return currency


def extract_one(pdf_path: Path) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    return {
        "invoice_id": _value(text, "Invoice ID"),
        "po_id": _value(text, "Purchase Order"),
        "invoice_date": _value(text, "Invoice Date"),
        "entity": _value(text, "Entity"),
        "supplier": _value(text, "Supplier"),
        "currency": _currency(text),
        "net_amount": _amount(text, "Goods / services per purchase order"),
        "vat_amount": _amount(text, "VAT"),
        "total_amount": _amount(text, "TOTAL"),
    }


def extract_directory(pdf_dir: Path) -> pd.DataFrame:
    rows = []
    for pdf_path in sorted(pdf_dir.glob("INV*.pdf")):
        rows.append(extract_one(pdf_path))
    return pd.DataFrame(rows, columns=FIELDS)


def compare_to_source(extracted: pd.DataFrame, source_csv: Path) -> pd.DataFrame:
    source = pd.read_csv(source_csv)
    source = source[source.invoice_id.isin(extracted.invoice_id)].copy()
    merged = extracted.merge(source, on="invoice_id", suffixes=("_pdf", "_csv"), how="outer", indicator=True)

    checks = []
    compare_fields = ["po_id", "invoice_date", "entity", "supplier", "currency", "net_amount", "vat_amount", "total_amount"]
    for _, row in merged.iterrows():
        mismatches = []
        for field in compare_fields:
            left = row.get(f"{field}_pdf")
            right = row.get(f"{field}_csv")
            if pd.isna(left) or pd.isna(right):
                if not (pd.isna(left) and pd.isna(right)):
                    mismatches.append(field)
            elif field.endswith("amount"):
                if abs(float(left) - float(right)) > 0.01:
                    mismatches.append(field)
            elif str(left) != str(right):
                mismatches.append(field)
        checks.append({
            "invoice_id": row.get("invoice_id"),
            "status": "MATCH" if not mismatches and row.get("_merge") == "both" else "MISMATCH",
            "fields": ",".join(mismatches),
        })
    return pd.DataFrame(checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-dir", type=Path, default=Path("data/generated_pdfs"))
    parser.add_argument("--source-csv", type=Path, default=Path("data/raw/invoices.csv"))
    parser.add_argument("--output-csv", type=Path, default=Path("data/raw/extracted_invoices.csv"))
    parser.add_argument("--comparison-csv", type=Path, default=Path("data/raw/invoice_extraction_comparison.csv"))
    args = parser.parse_args()

    extracted = extract_directory(args.pdf_dir)
    extracted.to_csv(args.output_csv, index=False)
    comparison = compare_to_source(extracted, args.source_csv)
    comparison.to_csv(args.comparison_csv, index=False)
    print(comparison.to_string(index=False))
    print(f"\nExtracted {len(extracted)} invoices to {args.output_csv}")

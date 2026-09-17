from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def generate_pdfs(invoice_csv: Path, output_dir: Path, limit: int = 15) -> None:
    invoices = pd.read_csv(invoice_csv).head(limit)
    output_dir.mkdir(parents=True, exist_ok=True)

    for _, row in invoices.iterrows():
        pdf_path = output_dir / f"{row.invoice_id}.pdf"
        c = canvas.Canvas(str(pdf_path), pagesize=A4)
        width, height = A4

        c.setFont("Helvetica-Bold", 20)
        c.drawString(25 * mm, height - 30 * mm, "SYNTHETIC SUPPLIER INVOICE")
        c.setFont("Helvetica", 10)
        c.drawString(25 * mm, height - 40 * mm, f"Invoice ID: {row.invoice_id}")
        c.drawString(25 * mm, height - 47 * mm, f"Purchase Order: {row.po_id}")
        c.drawString(25 * mm, height - 54 * mm, f"Invoice Date: {row.invoice_date}")
        c.drawString(25 * mm, height - 61 * mm, f"Entity: {row.entity}")
        c.drawString(25 * mm, height - 68 * mm, f"Supplier: {row.supplier}")

        y = height - 100 * mm
        c.setFont("Helvetica-Bold", 11)
        c.drawString(25 * mm, y, "Description")
        c.drawRightString(180 * mm, y, "Amount")
        c.line(25 * mm, y - 3 * mm, 185 * mm, y - 3 * mm)

        c.setFont("Helvetica", 11)
        y -= 13 * mm
        c.drawString(25 * mm, y, "Goods / services per purchase order")
        c.drawRightString(180 * mm, y, f"{row.currency} {row.net_amount:,.2f}")
        y -= 10 * mm
        c.drawString(25 * mm, y, "VAT")
        c.drawRightString(180 * mm, y, f"{row.currency} {row.vat_amount:,.2f}")
        y -= 10 * mm
        c.setFont("Helvetica-Bold", 11)
        c.drawString(25 * mm, y, "TOTAL")
        c.drawRightString(180 * mm, y, f"{row.currency} {row.total_amount:,.2f}")

        c.setFont("Helvetica-Oblique", 8)
        c.drawString(25 * mm, 18 * mm, "Synthetic document generated for Month-End Close Assistant testing.")
        c.save()

    print(f"Generated {len(invoices)} PDF invoices in {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--invoice-csv", type=Path, default=Path("data/raw/invoices.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/generated_pdfs"))
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    generate_pdfs(args.invoice_csv, args.output_dir, args.limit)

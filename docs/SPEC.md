# Month-End Close Assistant - Specification

## Project
Synthetic month-end close assistant for BE01, US01 and BE02 across Jan-Mar 2026.

## Files
- `purchase_orders.csv`: po_id, entity, supplier, currency, net_amount, po_date
- `invoices.csv`: invoice_id, po_id, entity, supplier, invoice_date, currency, net_amount, vat_amount, total_amount
- `payments.csv`: payment_id, invoice_id, entity, payment_date, currency, amount
- `shared_costs.csv`: cost_id, paying_entity, description, month, currency, amount, share_BE01, share_US01, share_BE02
- `fx_rates.csv`: month_end, eur_usd

`fx_rates_expected.csv` is a test-control copy used only for synthetic error validation.

## Rules
- Three-way match: invoice.net_amount must equal PO net_amount within EUR/USD 0.01 tolerance; payments should reconcile to invoice.total_amount.
- Match statuses: MATCHED, AMOUNT_MISMATCH, NO_PO, UNPAID, PARTIALLY_PAID, OVERPAID, PAYMENT_WITHOUT_INVOICE, DUPLICATE_INVOICE.
- Recharges: shared costs allocated by share keys, 5% markup, converted at month-end EUR/USD rate, posted in both entities (receivable in payer, payable in receiver).
- Elimination: intercompany receivables minus payables must net to 0.00 per entity pair.
- If any critical check fails, the month-end report is not produced.

## Planted errors
- E1 invoice net 780 vs PO 800
- E2 duplicate invoice_id
- E3 payment with no invoice
- E4 wrong FX rate on one recharge
- E5 one-sided intercompany entry
- E6 missing amount

E1-E4 are represented in the raw synthetic input mode. E5-E6 are downstream posting corruption cases: later tests will mutate generated intercompany entries so the raw accounting-source tables remain clean.

## Engineering principles
- Source data is deterministic with a fixed seed.
- Financial calculations are implemented in ordinary Python/pandas, not generated at runtime by an LLM.
- AI is used as a development aid and for reasoning-heavy review, not as the system of record for accounting numbers.

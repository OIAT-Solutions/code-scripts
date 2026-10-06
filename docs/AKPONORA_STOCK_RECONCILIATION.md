# Stock reconciliation: evidence and review

Local implementation prepared 6 October 2026. Not deployed or enabled. Company A only.

## Evidence before correction

`stock_reconciliation investigate` joins a saved stock snapshot to the executed W7 register, deliberate opening exclusions, pending bill lines, and captured EPOS adjustments. It retains multiple explanations per item. An equal quantity is a clue, not proof of a delivery or a physical count. The snapshot's timing flag is a heuristic, not a reconciled transaction bridge.

```sh
python -m code_scripts.akponora_ops.stock_reconciliation investigate \
  --snapshot /path/snapshot.json --opening-register /path/register.csv \
  --opening-exclusions /path/opening_qty_exclusions.csv \
  --bills-review /path/bills_review.csv --bill-lines /path/bill_lines.csv \
  --movements /path/movements/summary.json --out outputs/stock_review
```

Keep all operational evidence under ignored `outputs/` or server `STATE_ROOT`; never commit live exports.

## Daily detection and staff classification

`stock_movements` reads both stock-take and stock-movement lists using the existing view-only EPOS reader. Each live capture gets a fresh directory; no page/row cursor is reused. Capture windows are limited to seven days. Details are deduplicated by TransferID and product occurrence; conflicting or missing details mark the capture incomplete. Only uniquely identified stock-tracked products with approved Inventory mappings receive canonical quantities. Ambiguous names and child units remain held. EPOS display dates are retained without an assumed timezone.

```sh
python -m code_scripts.akponora_ops.stock_movements \
  --capture-live --captures outputs/stock_review/captures \
  --catalogue /path/catalogue.json --mapping /path/approved.csv \
  --from-date 2026-10-04 --through-date 2026-10-06 \
  --out outputs/stock_review/movements
```

The daily stock step runs this check only with `OIAT_COMPANY_A_STOCK_MOVEMENTS_ENABLED=1` (default off), using a three-day overlap and the snapshot's catalogue and mapping sources. **Deployment and enabling this mode require specific owner approval.** It never changes stock or posts bills. The overlap does not recover older missed days; run an explicit bounded capture for those days.

The portal reads `daily/<date>/<run>/movements/summary.json` and `portal_reads/<read>/movements/summary.json`. Stable event IDs prevent duplicate cards. Staff can record delivery, count correction, or loss using existing permissions, signed confirmations, evidence checksums, background jobs and audit records. Classification remains open until the accounting follow-up is completed; it cannot authorise a stock correction. A PO link is currently **unverified**, so cards ask for a PO check rather than assert that no PO exists. Automatic PO matching and accounting follow-up closure remain to be implemented.

## Count draft: deliberately not postable

`stock_reconciliation count-plan` accepts explicit verified count decisions and an accountant-specified cutoff and offset account. Each decision requires `sku`, `cutoff`, `verified_qty`, `qbo_qty_at_cutoff`, `count_source`, `confirmed_by`, `reason`, and `transactions_reconciled_by`. Only active, unflagged new Inventory items are eligible. Negative targets, legacy items, NonInventory items, duplicate decisions and missing evidence are rejected.

```sh
python -m code_scripts.akponora_ops.stock_reconciliation count-plan \
  --snapshot /path/snapshot.json --decisions /path/verified_counts.json \
  --cutoff 2026-10-06 --offset-account-id ACCOUNT_ID \
  --accounting-basis 'Accountant-reviewed treatment' --out outputs/count_draft
```

The output binds the source snapshot and decisions to a checksum, with `postable=false`. There is no posting adapter or portal count approval in this milestone. Before adding them: validate the supported QBO adjustment operation and FIFO valuation in a sandbox; prove quantity and GL/subledger effects; add durable duplicate prevention and recovery after partial failure; require fresh mapping, transaction-cutoff and quantity checks; then add a signed portal approval and post-write reconciliation. Opening omissions and current-period count variance need separate accountant-approved treatment. A current EPOS balance alone cannot establish a historical physical count or choose an accounting account.

## Validation

Run the reconciliation, snapshot and daily-run unit suites, plus Django stock-movement and attention suites. Fixtures perform no live financial calls. Production activation and live acceptance are separate milestones.

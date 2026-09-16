# Agent plan: Akponora (Company A) COGS / inventory cutover

**Read this before any QBO write, inventory change, or Company A pipeline change.**
This is the current operating plan for all agents (Codex, Claude, Cursor, etc.).
Longer background: `docs/AKPONORA_COGS_RECOVERY_PLAN.md`. If that file conflicts with this one, **this file wins**.

**Company:** AKPONORA VENTURES LTD. (`company_a`, QBO realm `9341455406194328`, production)
**As of:** 16 September 2026
**Goal:** Keep the shop selling, make Jan–Sep books close enough, and stop QBO FIFO COGS from firing on till sales before October automation.

Sales cannot be paused. Historical EPOS quantities were messy. Close enough is acceptable for the past. The next sale must not recreate the COGS problem.

---

## Non-negotiables

1. **Do not delete** existing QBO products or historical sales receipts.
2. **Do not bulk-inactivate** legacy QBO Inventory items. Inactivation can zero QtyOnHand and post value to COGS / Inventory Shrinkage.
3. **Do not patch QtyOnHand** on the existing 4,315 Inventory items to “match EPOS.”
4. **Do not post QBO `InventoryAdjustment`** transactions (quantity-apply is removed on purpose).
5. **Do not import** a fresh catalogue onto **existing QBO names**. January already did a fake opening (importer default QtyOnHand **10**; sales upload auto-creates Inventory items).
6. **Do not use barcodes** as product keys. Use EPOS Product ID, then SKU, then approved unique exact name.
7. **Do not infer pack size from trailing `*N`** once conversion mode is on. Use the staff-approved sale multiplier only.
8. **Do not copy carton/pack cost** onto an each/unit item unchanged.
9. Legacy QBO Inventory items are **frozen**: preserved for history, excluded from future sales mapping.
10. New sales targets are **Non-inventory** (or Service). They record revenue and VAT only. They must not change QtyOnHand or post FIFO COGS.

---

## End state

| Layer | Source of truth |
| --- | --- |
| Physical quantity, packs, till buttons | EPOS |
| Product identity going forward | Approved Product Conversion List (canonical SKU + unit + sale multiplier) |
| Daily sales in QBO | New **Non-inventory** items (new names/SKUs, e.g. `AKP-…`) |
| Financial Inventory Asset and COGS | QBO GL, via month-end journals, not live item FIFO |
| Month-end COGS | `opening + verified purchases − EPOS closing value = COGS` |
| Unmapped till products | One catch-all Non-inventory item; EPOS name on the line description. Never auto-create Inventory. |

A full QBO perpetual-inventory rebuild (new Inventory items, Oct 1 start qty, purchase-before-sale) is a **later optional project**. It is not required to stop the COGS issue and is not this cutover.

---

## What is already done

- Inventory-sync quantity apply is removed in code. Do not re-enable it.
- Transfer **`68049`** (31 Mar 2026, ₦58,659,919, Moniepoint `6397730972` → `120000 - Inventory`, memo “moniepoint sales for march 2026”) has been **deleted**. Same amount still exists as Transfer **`68091`** (22 Apr, same Moniepoint → Undeposited Funds). Leave `68091`.
- Jan–Jun zero-floor journals posted: `COGS-ZF-2026-01` … `COGS-ZF-2026-06` (debit Inventory Asset id `77`, credit `COGS Historical Correction - Jan-Jun 2026` id `1150040049`, total ₦281,069,104.71). QBO JournalEntry Ids `74403`–`74407` (Jan–May, live-verified 16 Sep 2026) plus `74556` (June, TxnDate 2026-06-30, ₦40,321,957.23, posted 16 Sep 2026 per chat approval — see `outputs/akponora_from_2026-08-22/QBO_WRITES_2026-09-16.json`). **Jul/Aug/Sep are not posted.** Draft June payload (now applied): `outputs/akponora_from_2026-08-22/journal_june_zf_draft.json`.
- Catch-all Non-inventory item **`AKP-UNMAPPED-EPOS-SALES`** created in production QBO 16 Sep 2026 per chat approval: Item Id `15030`, Sku `AKP-UNMAPPED`, Type `NonInventory`, `IncomeAccountRef` `1150040024` (400100 - Revenue - Grocery), Taxable, SalesTaxIncluded, `TrackQtyOnHand` false. No `AssetAccountRef`/`ExpenseAccountRef`/`QtyOnHand`. Not yet wired into the pipeline (conversion mode is still off; see `catch_all_item_draft.json` and `QBO_WRITES_2026-09-16.json`).
- Bill **`66251`**: vehicle line on `150000 - Fixed Assets`. **Paid 16 Sep 2026** as BillPayment `74555` (₦7,279,300 from MONIEPOINT `4686987227`, TxnDate 30 Jan 2026). Confirm that payment was an approved recording of a past payment, not a new cash movement. Not a COGS blocker.
- Codex staff workbook (16 Sep): ~162 auto-handle rows; **237 products** still need staff pack/cost/duplicate confirmation.
- Latest dated EPOS pack on disk: **16 September 2026** under `../MISC/AKPONORA Investigation/COGS Analysis/As of 16th September 2026/` — BookKeeping 1 Jun–15 Sep (`BookKeeping_June 1st till Sept 15.csv`, 159,984 lines); Stock Levels `StockReport_2026_09_16_2108.csv` (zero-floor **₦142,028,049.94**); Product List (25 parts); Stock History month-ends **30 Jun ₦103,312,429.96 / 31 Jul ₦133,682,112.36 / 31 Aug ₦135,620,665.76**. Mid-month Stock Levels: 23 Jul ₦128,718,923.44; 22 Aug ₦127,846,799.11.
- Conversion rebuild from that pack (16 Sep 2026): `outputs/akponora_from_2026-08-22/` (gitignored). **237 BLOCK** rows for staff; 5,801 provisional. Staff file: `akponora_staff_review_237.csv`.
- Product conversion library exists but is **off**: `code_scripts/product_conversion.py`, wired in `transform.py` / `qbo_upload.py`, `company_a.json` has `transform.product_conversion.enabled: false`.
- Live QBO (16 Sep, before the two chat-approved writes below): ~4,315 active Inventory items, ~6,146 inactive, **1** Non-inventory, 1,588 InventoryAdjustments. Now **2** Non-inventory items after `AKP-UNMAPPED-EPOS-SALES` (Id `15030`) was created. **Two inventory ledgers:** `Inventory Asset` (id `77`) ~₦250.09m **plus** `120000 - Inventory` family with sub-accounts (`120100`/`120200`/`120201`/`120202`/`120300`) **~₦235.63m** (`CurrentBalanceWithSubAccounts`). Combined ~₦485.7m. Header `120000` alone is only ~₦9.18m — do not use that as the inventory total. Item subledger vs GL gap remains large. **Do not close that gap by editing item quantities.**
- A human bookkeeper is still posting vendor **Bills** to legacy Inventory items (Jun–16 Sep item-based bills ~₦199.3m). That is independent of the sales pipeline. Redirect new bills off Inventory items until the Non-inventory cutover. New Inventory items were still created in September (zero cost placeholders).

---

## Workstreams (run in parallel)

### A. Pipeline / future-proof (Cursor / this agent)

Own `code_scripts/qbo_upload.py`, `transform.py`, `product_conversion.py`, `company_config.py`, `company_a.json`, tests.

- Sales must **not** create QBO Inventory items for Company A. `qbo_upload.py` now **hard-blocks** Company A sales upload until conversion is enabled, and `create_inventory_item` refuses Company A / conversion-mode creates.
- Guard/remove importer default QtyOnHand **10** (`qbo_inv_manager.py` `cmd_import_products` `default_qty=10`).
- Turn on approval-driven conversion: mapped products → existing Non-inventory only; collisions with legacy Inventory names fail closed.
- Unmapped products → catch-all Non-inventory, original EPOS name in description. Do not fail the whole daily upload once catch-all is in place (shop cannot pause sales).
- Bypass trailing `*N` expansion when conversion is on (already started in uncommitted `transform.py`).
- Tests for: no Inventory create, Non-inventory accept/reject, unmapped catch-all, nested pack multiplier once, barcode never a key.

Do not write QBO item quantities. Do not clash with Codex journal work.

### B. QBO books / history (Codex)

- Prepare **June–September** COGS journals on the same zero-floor basis. Close enough. EPOS qty history was weird; do not chase item FIFO layers.
- Propose **one** Inventory GL true-up for the ledger vs valuation gap, to an accountant-approved account (not Inventory Shrinkage unless they choose it).
- Create **new** Non-inventory QBO items from the approved mapping (new names/SKUs). Dry-run first.
- Do not inactivate/delete/merge legacy Inventory items.
- Bill `66251` payment only when staff give date + account.

### C. Mapping / staff workbook (Codex + staff, Claude QA)

- Staff complete yellow columns only: pack size / sale multiplier, cost, keep vs duplicate.
- Claude: QA mapping CSV — no barcode keys, no double `*N`, no carton cost on each, no legacy Inventory name collision, Target type Non-inventory, Effective Date, Approved By.
- Output: approved `akponora_product_conversion.csv` for the pipeline.

### D. Fresh EPOS snapshot (human)

Dated export as soon as possible (today is better than a perfect 31 Aug file we do not have). Used for mapping refresh and month-end closing **value**, not for writing QBO QtyOnHand.

---

## Historical correction rules

```text
Opening inventory value + verified purchases − EPOS closing value = COGS
```

- Negative EPOS/QBO product qty valued at **zero** unless staff confirm stock exists.
- 23k+ zero-cost EPOS sales lines: do not invent cost; disclose and move on.
- Missing sales vs EPOS revenue: backfill as **sales**, not a COGS journal, and only when matched. Not a blocker for the Non-inventory cutover.
- Direct purchases already on Inventory (~₦11.2m rice/oil/frozen/eggs) stay as purchases.
- Item-level QBO subledger may remain ugly. P&L COGS and Inventory **balance** should become plausible.

---

## Code traps (do not repeat January)

| Trap | Where |
| --- | --- |
| Importer default QtyOnHand **10** | `code_scripts/scripts/qbo_inv_manager.py` `default_qty=10` |
| Sales upload creates Inventory with `config.default_qty_on_hand` (now 0) | `code_scripts/qbo_upload.py` `create_inventory_item` |
| `enable_inventory_items: true` + `allow_negative_inventory: true` + `auto_fix_wrong_type_items: true` | `code_scripts/companies/company_a.json` |
| Trailing `*N` pack expansion | `transform.py` `aggregate_products` / `strip_pack_multiplier` |
| Conversion flag off | `transform.product_conversion.enabled: false` |

Conversion mode, when enabled, must keep: Approved rows only; ID → SKU → optional unique exact name; NonInventory/Service only; no Inventory create/patch.

---

## QBO write policy

Allowed without extra approval:

- Read-only queries and reports.
- Pipeline code + tests.
- Mapping CSV / workbook updates on disk.

Needs explicit human approval in chat before doing:

- Posting journals.
- Creating Non-inventory items in production QBO.
- Enabling conversion in production `company_a.json` / portal DB.
- Any Bill Payment on `66251`.
- Any Inventory item create/update/inactivate/delete.

Forbidden:

- InventoryAdjustment posting.
- Bulk inactivation or delete of products.
- QtyOnHand edits on legacy items.
- Re-running January-style import with qty 10.

---

## Evidence locations

- EPOS 22 Aug pack: `../MISC/AKPONORA Investigation/COGS Analysis/As of 22 August 2026/`
- Mapping rebuild from that pack: `outputs/akponora_from_2026-08-22/`
  (`akponora_product_conversion.csv`, `akponora_product_exceptions.csv`, `akponora_staff_review_237.csv`, `sales_audit.json`)
- Wait-work 16 Sep 2026 (read-only): same folder — `open_work_summary.json`, `journal_candidates.csv`, `backfill_dates.csv`, `inventory_gl_purchases.csv`, `journal_june_zf_draft.json`, `catch_all_item_draft.json`, `WHAT_HUMAN_MUST_CONFIRM.txt`
- BookKeeping 1 Jun–15 Sep totals: `outputs/akponora_bookkeeping_2026-09-16/` — missing-from-QBO 12 Jun–15 Sep **₦366,389,735** TOTAL (backfill as sales, not a COGS journal)
- QBO exports 23 Jul: `../MISC/AKPONORA Investigation/COGS Analysis/As of 23 July 2026/QBO/`
- Conversion builder: `akponora_build_product_conversion.py`
- Codex staff workbook (16 Sep): `outputs/019f8fd0-de6d-70f0-9526-2d79eaddbc52/akponora_staff_actions_2026-09-16/akponora_product_decisions_2026-09-16.xlsx` (if present in this worktree)
- Do not commit QBO exports, tokens, or staff workbooks with live stock.

Company B (Goldplates) is out of scope unless asked.

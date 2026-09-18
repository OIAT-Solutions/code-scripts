# Agent plan: Akponora (Company A) — 1 Oct 2026 Inventory go-live

**Read this before any QBO write, inventory change, or Company A pipeline change.**
Longer background: `docs/AKPONORA_COGS_RECOVERY_PLAN.md`. If that file conflicts with this one, **this file wins**.
Start-now execution status: `docs/AKPONORA_OCT1_START_NOW_STATUS_18_Sep_2026.md`.
Full calendar/workstreams: `../MISC/AKPONORA Investigation/Chart of Accounts/AKPONORA_Oct1_Inventory_Golive_Plan_18_Sep_2026.md`.

**Company:** AKPONORA VENTURES LTD. (`company_a`, QBO realm `9341455406194328`, production)
**As of:** 18 September 2026
**Goal:** Solid **Inventory-tracked** restart ~1 Oct 2026. Catch-all / Non-inventory is **history only** (Jan–Sep). It is not the October end-state.

Sales cannot be paused. Historical EPOS quantities were messy. Close enough is acceptable for the past. From 1 Oct, till sales must hit **new Inventory Ids only**.

---

## Non-negotiables

1. **Do not delete** existing QBO products or historical sales receipts.
2. **Do not bulk-inactivate** legacy QBO Inventory items. Inactivation zeros QtyOnHand and posts value to COGS / Inventory Shrinkage. `qbo_inv_manager.py inactivate-all` is **forbidden** on Company A except sequenced W10 dry-run batches after chat yes.
3. **Do not patch QtyOnHand** on the existing ~4,316 legacy Inventory items to “match EPOS.”
4. **Do not post QBO `InventoryAdjustment`** on the sales/sync path (quantity-apply stays removed). After Oct go-live, the **only** allowed legacy adjustment is qty → 0 offset to **`300150`**, never Shrinkage, never “match EPOS,” and only after a per-batch dry-run + chat yes.
5. **Do not import** a fresh catalogue onto **existing QBO names**. January already did a fake opening (importer default QtyOnHand **10**; sales upload auto-creates Inventory items).
6. **Do not use barcodes** as product keys. Use EPOS Product ID, then SKU, then approved unique exact name.
7. **Do not infer pack size from trailing `*N`** once conversion mode is on. Use the staff-approved sale multiplier only.
8. **Do not copy carton/pack cost** onto an each/unit item unchanged.
9. Legacy QBO Inventory items are **frozen** until W5 (rename `LEGACY — {name}` to free a display name) and W10 (safe inactivation). They are excluded from Oct sales mapping.
10. **October sales targets are new Inventory items** (new Ids, `AKP-` SKU, InvStartDate **2026-10-01**, Asset account **Inventory Asset id `77` only** — never `120000`). Catch-all Non-inventory `AKP-UNMAPPED-EPOS-SALES` Id `15030` is **Jan–Sep history only**. TxnDate ≥ **2026-10-01** is fail-closed: no catch-all, no auto-create.
11. **Do not create Inventory items on the sales path.** Company A `create_inventory_item` stays refused. Oct creates are a **separate approved batch** after chat yes.
12. Bookkeeper freeze: **no new Inventory items**; **no bills to Inventory products or `120000`**. Use `200100` / `200201` / `200202` / `200300` until the new catalogue exists. The 17 Sep Red Bull create is the example of what must stop.

---

## End state (October)

| Layer | Source of truth |
| --- | --- |
| Physical quantity, packs, till buttons | EPOS |
| Product identity going forward | Approved Product Conversion List → **new** Inventory name + `AKP-` SKU + **new QBO Item Id** |
| Daily sales in QBO from 1 Oct | New **Inventory** items only (approved Ids). Unmapped line → fail the day |
| Financial Inventory Asset | New-item subledger + GL. Opening qty from **30 Sep** EPOS zero-floor, created with InvStartDate 2026-10-01 |
| Jan–Sep sales history | Catch-all Non-inventory `15030` (already posted through 15 Sep; 16–30 Sep still to backfill) |
| Month-end COGS through Sep | `opening + verified purchases − EPOS closing value`, or option-1 stock-movement journal when purchases already sit on `200xxx` |
| Legacy catalogue | Frozen, then renamed `LEGACY —` where needed, then inactivated **after** go-live (W10). Not left active forever |

A Non-inventory / catch-all October is **not** the plan.

**Critical create-night rule:** creating new Inventory with QtyOnHand **debits Inventory Asset**. The 16 Sep equity true-up already set IA to EPOS. Same-day offset required (credit IA / debit `300150`) so IA after create equals **30 Sep EPOS only**, not EPOS + leftover ghost. Recalc live that night. Chat yes before posting.

---

## What is already done

- Inventory-sync quantity apply is removed in code. Do not re-enable it.
- Transfer **`68049`** (31 Mar 2026, ₦58,659,919, Moniepoint `6397730972` → `120000 - Inventory`, memo “moniepoint sales for march 2026”) has been **deleted**. Same amount still exists as Transfer **`68091`** (22 Apr, same Moniepoint → Undeposited Funds). Leave `68091`.
- Jan–Jun zero-floor journals posted: `COGS-ZF-2026-01` … `COGS-ZF-2026-06` (debit Inventory Asset id `77`, credit `COGS Historical Correction - Jan-Jun 2026` id `1150040049`, total ₦281,069,104.71). QBO JournalEntry Ids `74403`–`74407` (Jan–May) plus `74556` (June). **Sep month-end COGS is not posted.**
- July/August journals **updated 16 Sep 2026** (option 1): DocNumbers `COGS-2026-07` JournalEntry `75097` and `COGS-2026-08` `75098`. Inventory-movement only: debit Inventory Asset `77` / credit `200000 - Cost of sales` `76`.
- Catch-all Non-inventory **`AKP-UNMAPPED-EPOS-SALES`** Id `15030` created 16 Sep 2026 for **history**. Income `1150040024` (400100). Do not use it for TxnDate ≥ 2026-10-01.
- Inventory GL true-up 16 Sep 2026: Equity **`300150`** Id `86`; `INV-CONS-2026-09-16` `75153`; `INV-EQ-2026-09-16` `75154` writing IA down to EPOS 16 Sep zero-floor **₦142,028,049.94**.
- Sales backfill to catch-all through **15 Sep**. **16 Sep onward** not posted. Pipeline/server **off**.
- Product conversion **on**. Empty approved map still in config until W7 fills new Inventory Ids. `fail_closed_from`: **2026-10-01**. `auto_fix_wrong_type_items`: **false**. Sales upload hard-blocks unless conversion is on; `create_inventory_item` refuses Company A / conversion-mode creates.
- Live QBO 18 Sep W1 dump (read-only): **4,316** active Inventory; **2** active Non-inventory; IA still **₦142,028,049.94**. `120100` ₦207,339.54 and `120202` ₦22,379.81 have **refilled** after the 16 Sep zero. 9 Inventory items created in September, including **REDBULL WATERMELON** Id `15031` on 17 Sep.
- 16 Sep EPOS pack build-now (W2): **5,504** non-negative unique-name rows plus **160** negatives at opening 0 = **5,664** dry-run create payloads (qty 0). **3,786** of those collide with a **live active** QBO Inventory name (W5). Staff 237 yellow columns: **0 filled**. 237 does **not** block the 5,664.

---

## Workstreams

| ID | What | Status 18 Sep |
| --- | --- | --- |
| W0 | Bookkeeper freeze | Note written; human must send/enforce |
| W1 | Live QBO item dump | **Done** (read-only artifacts) |
| W2 | Mapping workbook from 16 Sep pack | **Done** (no live creates) |
| W3 | Staff 237 yellow columns (till-sold first) | Open — 0 filled; does not block W2/W4 |
| W4 | Pipeline: Inventory targets, fail-closed from 1 Oct, no auto-create | **Code + tests in this PR**; server still off |
| W5 | Rename colliding legacy names `LEGACY — {name}` | **Wait for chat yes** |
| W6 | 30 Sep EPOS pack | Wait |
| W7 | Create new Inventory (qty from 30 Sep, InvStartDate 1 Oct) + IA offset | **Wait for chat yes** — dry-run qty-0 payloads exist |
| W8 | Sep close: 16–30 Sep catch-all backfill + option-1 Sep COGS | Wait for 30 Sep + chat yes |
| W9 | Pipeline on | Wait |
| W10 | Safe legacy inactivation (lab → Z0 → qty→0 to 300150 → inactivate) | After W9 stable |

Do **not** wait on 237 or 30 Sep to keep mapping/code current. Do **not** go live onto legacy items to “make the date.”

---

## Historical correction rules

```text
Opening inventory value + verified purchases − EPOS closing value = COGS
```

When purchase bills already sit on `200100`/`200201`/`200202`/`200300`, the month-end journal is **only the stock movement** (`closing − opening`). Jul/Aug 2026 use this method. September waits for the 30 Sep pack.

- Negative EPOS/QBO product qty valued at **zero** unless staff confirm stock exists.
- Missing cost on positive stock: create with PurchaseCost 0, flag for staff; do not invent cost.
- 16–30 Sep missing sales: backfill as **sales onto catch-all `15030`**, not onto new Oct Inventory (InvStartDate would reject).

---

## Code traps (do not repeat January)

| Trap | Where |
| --- | --- |
| Importer default QtyOnHand **10** | `code_scripts/scripts/qbo_inv_manager.py` — default is now **0**; live Company A import is disabled |
| Sales upload creates Inventory | `qbo_upload.py` `create_inventory_item` — refused for Company A / conversion |
| `auto_fix_wrong_type_items: true` | Must stay **false** for Company A |
| Catch-all on Oct runs | `fail_closed_from: 2026-10-01` — unmapped Oct lines fail the batch |
| Mapping to a legacy Inventory **name** whose Id is not in the approved new-Id list | Fail closed |
| Trailing `*N` pack expansion | Bypassed when conversion is on |
| Conversion flag off | Recreates FIFO COGS on the next sales upload. Do not set false |
| `inactivate-all` on Company A | Forbidden (zeros qty → Shrinkage) |

Conversion mode must keep: Approved rows only; ID → SKU → optional unique exact name; Oct targets = **approved Inventory Ids**; no sales-path Inventory create/patch.

---

## QBO write policy

Allowed without extra approval:

- Read-only queries and reports.
- Pipeline code + tests.
- Mapping CSV / workbook updates on disk.
- Dry-run create payloads (not posted).

Needs explicit human approval in chat before doing:

- Posting journals (Sep COGS, create-night IA offset).
- Creating Inventory items in production QBO (W7).
- Renaming legacy items `LEGACY —` (W5).
- Inactivating any Inventory item (W10 lab/batches).
- 16–30 Sep sales backfill.
- Turning the pipeline/server on.
- Any Bill Payment on `66251`.
- Any InventoryAdjustment, including qty → 0.

Forbidden:

- InventoryAdjustment posting on the sales/sync path.
- Bulk inactivation or delete of products.
- QtyOnHand edits on legacy items to match EPOS.
- Re-running January-style import with qty 10.
- Catch-all as the October sales target.
- Banking Undeposited Funds as part of this cutover.

---

## Evidence locations

- W1/W2/dry-run artifacts (gitignored live stock): `outputs/akponora_oct1_golive_2026-09-18/`
- 16 Sep conversion rebuild: `outputs/akponora_from_2026-08-22/`
- EPOS 16 Sep pack: `../MISC/AKPONORA Investigation/COGS Analysis/As of 16th September 2026/`
- Bookkeeper freeze note: `docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md`
- Do not commit QBO exports, tokens, or workbooks with live stock.

Company B (Goldplates) is out of scope unless asked.

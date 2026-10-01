# Agent plan: Akponora (Company A) — 1 Oct 2026 Inventory go-live

**Read this before any QBO write, inventory change, or Company A pipeline change.** This file wins over every other plan document.

| Need | Read |
| --- | --- |
| Tonight/tomorrow, step by step with commands | [`docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md`](docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md) |
| What has already been posted, and when | [`docs/AKPONORA_CUTOVER_LOG.md`](docs/AKPONORA_CUTOVER_LOG.md) |
| Code contract, posting controls, incident response | [`docs/AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md`](docs/AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md), [`docs/AKPONORA_OPERATIONS_CONTROLS.md`](docs/AKPONORA_OPERATIONS_CONTROLS.md) |
| Operator tools (EPOS pulls, mapping, renames, creates, journals) | [`code_scripts/scripts/akponora_cutover/README.md`](code_scripts/scripts/akponora_cutover/README.md) |
| Bookkeeper rules | [`docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md`](docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md) |
| Background (Jan–Sep recovery) | `docs/AKPONORA_COGS_RECOVERY_PLAN.md` (historical; superseded where it conflicts) |

**Company:** AKPONORA VENTURES LTD. / NORA MINI MART (`company_a`, QBO realm `9341455406194328`, production).
**As of:** 30 September 2026.
**Goal:** from **1 Oct 2026**, till sales post only to **new QBO items**. These are Inventory `AKP-{EPOS master ProductID}`, or NonInventory `AKP-NS-{ProductID}` for products EPOS does not stock-track. QBO perpetual FIFO then gives COGS. Jan–Sep is history on the catch-all.

---

## Non-negotiables

1. **Never delete** QBO products or historical sales receipts.
2. **Never bulk-inactivate** legacy Inventory items: inactivation zeros qty and posts value to COGS/Shrinkage. `qbo_inv_manager.py inactivate-all` is forbidden on Company A.
3. **Never patch QtyOnHand** on legacy items "to match EPOS". **Never post `InventoryAdjustment`** on the sales/sync path. After go-live the only allowed legacy adjustment is qty → 0 offset to `300150` (W10, per-batch dry-run + chat yes).
4. **Never import a catalogue onto existing QBO names**, and never create items with default qty (January's importer used qty 10).
5. **Product identity = EPOS Product ID**, then SKU, then approved unique exact name. **Never barcodes.** Never infer pack size from a trailing `*N`. Use the approved sale multiplier (= what EPOS deducts).
6. **One physical product = one QBO Inventory item.** Crate/pack/each EPOS buttons map to that item with a multiplier; purchases and costs use the same unit. Never copy a carton cost onto a unit item.
7. **October sales targets are the new items only.** TxnDate ≥ 2026-10-01 fails closed: no catch-all `15030`, no legacy items, no auto-create. The sales path never creates or patches items (`create_inventory_item` refuses Company A).
8. **Legacy items are frozen.** They are renamed `LEGACY — {name}` (W5, name only) before the creates, and inactivated safely only after stable operation (W10).
9. **Asset account for new Inventory is Inventory Asset `77` only.** The `120000` family must end at ₦0 and receive nothing new.
10. **Bookkeeper: bills and customer invoices are paused** until the owner says the fix is complete. No backdating. Nothing on legacy Inventory items. No new items.
11. **Every production QBO write needs a chat yes for that specific action** (see Write policy). Dry-run first, verify after.

---

## October end state

| Layer | Source of truth |
| --- | --- |
| Physical stock, packs, till buttons, pack deductions | EPOS |
| Product identity | Approved mapping `STATE_ROOT/mappings/company_a/approved.csv` (EPOS ProductID → new QBO Item Id + multiplier) |
| Daily sales from 1 Oct | New Inventory / `AKP-NS-` NonInventory items only. Any unmapped line fails the day |
| Inventory value | New-item subledger = IA `77` GL. Opening = 30 Sep EPOS count, InvStartDate 2026-10-01 |
| COGS from 1 Oct | QBO FIFO on new items; month-end posts only a verified count variance |
| Purchases after the fix | Item lines on the **new** `AKP-` items in the item's unit (e.g. 5 cartons × 24 = 120 cans). `AKP-NS-` items: purchases expense to 200xxx |
| Jan–Sep sales history | Catch-all `15030` (posted through 24 Sep; 25–30 Sep pending) |
| Legacy catalogue | Renamed `LEGACY —` (W5), excluded from mapping, inactivated later (W10) |

**Create-night rule:** creating Inventory with QtyOnHand debits IA. A same-night offset (credit IA `77` / debit `300150` = `B + C − V`) makes IA after the creates equal the **30 Sep EPOS value V** only. Recalculate live. Chat yes.

---

## Status (30 Sep 2026)

| ID | What | Status |
| --- | --- | --- |
| W0 | Bookkeeper freeze (bills + invoices paused) | Note updated 26 Sep; owner to send/enforce |
| W2/W3 | Canonical mapping | **Done**: final map approved by the owner (6,150 products, 99.89% of Sep till value). Item Ids are filled after W7 |
| W4 | Pipeline code (October contract, controls, scheduler gate) | **Merged in PR #62**. Not deployed |
| W5 | Rename 3,877 legacy items `LEGACY — …` | **Approved by owner; not executed.** The owner runs it (agent QBO writes are blocked by the permission system) |
| — | QBO closing date 31 Aug 2026 | Owner sets in the QBO UI |
| W6 | 30 Sep EPOS pack (stock count, product list, sales, POs) | Tonight after close |
| W8 | 25–30 Sep sales backfill to `15030`; September close journals | After W6, chat yes |
| W7 | Create 3,939 Inventory + 536 NonInventory items; IA offset; install mapping | After W5 + W6, chat yes |
| W9 | Pipeline on for Company A (`OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1`) | After a clean 1 Oct dry-run, chat yes |
| W10 | Safe legacy inactivation | After W9 is stable |

Open items that do **not** block go-live (tracked in the runbook):

- 536 EPOS-untracked products. The owner may turn frozen food, eggs and rice into tracked EPOS masters (one master per family in grams/each, children deduct). Re-pull and rebuild the mapping when done; until then they go NonInventory.
- 109 pack-named children where EPOS deducts 1 (`pricing_review.csv`): check the EPOS Master Products amount.
- Staff questions: Ernest's 19 Sep stock adds (deliveries or recounts?), and duplicate POs `3828`/`3829`/`3855`/`3861`.
- Undeposited Funds is ₦0 through 24 Sep. Receipts posted from 25 Sep on land in `100900` and need depositing by the same till-sheet method (`akponora_cutover/uf_*`).

---

## Accounting rules

- **Through September (periodic):** `opening stock + purchases − closing count = COGS`. The September close is one journal set (see runbook):
  - IA `77` to the 30 Sep EPOS value;
  - zero every 120xxx sub-account into the matching 200xxx (this clears the GPFH invoice COGS);
  - a GRNI accrual for unbilled September POs: post-16-Sep receipts to COGS, pre-reset receipts against `300150`, ex-tax; possible-duplicate POs excluded.
- Negative counts are valued at 0. Missing cost: create at 0 and flag; never invent cost.
- Don't edit or void old bills or invoices. Correct by journal in the open period (the August-dated GPFH COGS is corrected in September with a memo).
- **From October (perpetual):** QBO FIFO. Month-end posts only the verified variance between the new-item subledger and the EPOS count.

---

## Code traps (do not repeat January)

| Trap | Guard |
| --- | --- |
| Importer default QtyOnHand 10 | `qbo_inv_manager.py` default 0; live Company A import disabled |
| Sales upload creating Inventory | `create_inventory_item` refused for Company A / conversion mode |
| `auto_fix_wrong_type_items` | Must stay `false` for Company A |
| Conversion flag off | Recreates legacy FIFO COGS. Never set false |
| Catch-all or legacy target in October | `october_target_error` fails the day |
| Missing mapping file | Local: `runtime/mappings/company_a/approved.csv` must exist (header-only before W7). Never fall back to an old map |
| Scheduler posting Company A early | Excluded unless `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1` |
| Running backfills from the wrong place | Run from the repo root: `OIAT_COMPANIES_DIR=code_scripts/companies python run_pipeline.py --company company_a …` (tokens in `runtime/`) |

---

## Write policy

**Allowed without asking:** read-only QBO/EPOS queries and reports; code and tests; mapping/workbook files on disk; dry-runs.

**Needs a chat yes for the specific action:** sales backfills; journals (Sep close, GRNI, IA offset); W5 renames; W7 creates; installing the mapping; turning the pipeline/scheduler on; any InventoryAdjustment; inactivating any item; Undeposited Funds deposits/transfers; any Bill Payment on `66251`; creating accounts (e.g. the GRNI liability); changing QBO settings.

**Forbidden:** InventoryAdjustment on the sales path; bulk inactivation or deletion of products; legacy qty edits to match EPOS; January-style qty-10 import; catch-all or legacy items as October targets.

---

## Evidence locations (gitignored — never commit)

- `outputs/nora_gaps_2026-09-25/`: final mapping, W5 plan, catalogue pulls, Sep close draft, bills/PO drafts, stock bridge, invoice drift, backfill verification.
- `outputs/akponora_uf_allocation_2026-09-26/`: Undeposited Funds work.
- EPOS packs: `../MISC/AKPONORA Investigation/COGS Analysis/As of …/`.
- Do not commit QBO exports, tokens, workbooks with live stock, or `runtime/`.

Company B (Goldplates) is out of scope unless asked.

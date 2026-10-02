# Agent plan: Akponora (Company A) — 1 Oct 2026 Inventory go-live

**Read this before any QBO write, inventory change, or Company A pipeline change.** This file wins over every other plan document.

| Need | Read |
| --- | --- |
| Tonight/tomorrow, step by step with commands | [`docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md`](docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md) |
| What has already been posted, and when | [`docs/AKPONORA_CUTOVER_LOG.md`](docs/AKPONORA_CUTOVER_LOG.md) |
| Code contract, posting controls, incident response | [`docs/AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md`](docs/AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md), [`docs/AKPONORA_OPERATIONS_CONTROLS.md`](docs/AKPONORA_OPERATIONS_CONTROLS.md) |
| Daily running from October: catalogue sync, bills from POs, item guard | [`docs/AKPONORA_DAILY_OPERATIONS.md`](docs/AKPONORA_DAILY_OPERATIONS.md) |
| Server: the one scheduled Company A routine (`daily_run`), env, holds, approvals | [`docs/SERVER_SETUP.md`](docs/SERVER_SETUP.md) |
| Operator tools (EPOS pulls, mapping, renames, creates, journals) | [`code_scripts/scripts/akponora_cutover/README.md`](code_scripts/scripts/akponora_cutover/README.md) |
| Bookkeeper rules | [`docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md`](docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md) |
| Background (Jan–Sep recovery) | `docs/archive/AKPONORA_COGS_RECOVERY_PLAN.md` (historical; superseded where it conflicts) |
| Everything outstanding (checklist) | [`docs/AKPONORA_ROADMAP.md`](docs/AKPONORA_ROADMAP.md) |

**Company:** AKPONORA VENTURES LTD. / NORA MINI MART (`company_a`, QBO realm `9341455406194328`, production).
**As of:** 2 October 2026 — live on the new items since 1 Oct (see Status).
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
| Jan–Sep sales history | Catch-all `15030` (posted through 30 Sep) |
| New EPOS products after 1 Oct | `catalogue_sync` (never by hand in QBO) |
| Purchases from October | EPOS received POs → `bills_sync` → reviewed unpaid Bills |
| Legacy catalogue | Renamed `LEGACY —` (W5), excluded from mapping, inactivated later (W10) |

**Create-night rule:** creating Inventory with QtyOnHand debits IA. A same-night offset (credit IA `77` / debit `300150` = `B + C − V`) makes IA after the creates equal the **30 Sep EPOS value V** only. Recalculate live. Chat yes.

---

## Status (2 Oct 2026) — LIVE

The cutover is done. Details and receipts: [`docs/AKPONORA_CUTOVER_LOG.md`](docs/AKPONORA_CUTOVER_LOG.md). Day-to-day running: [`docs/AKPONORA_DAILY_OPERATIONS.md`](docs/AKPONORA_DAILY_OPERATIONS.md).

| ID | What | Status |
| --- | --- | --- |
| W0 | Bookkeeper freeze | Bills **can resume on the new `AKP-` items once the owner says so** (prefer the bills sync below). September deliveries billed late go against GRNI `210200`, never items. Still no new items by hand, nothing on `LEGACY —` items |
| W2/W3 | Canonical mapping v3 | **Done and installed**: 6,150 rules → 4,430 Item Ids, sha `b4d8c640…` |
| W4 | Pipeline code | Merged (PR #62) plus later fixes on branch `cursor/post-akponora-qbo-writes-51f3` (not pushed yet). **Not deployed to OIAT-SRV-01** |
| W5 | Legacy rename | **Done** 1 Oct: 3,879 items `LEGACY — …` |
| — | QBO closing date | Owner sets **30 Sep 2026** in the QBO UI (not yet confirmed) |
| W6 / W8 | 30 Sep pack; 25–30 Sep backfill; Sep close journals; GRNI | **Done** (JE 76552, 76553, 76554; GRNI account `210200` Id 87). IA `77` = V ₦148,824,877.40 at 30 Sep; every 120xxx ₦0 |
| W7 | 3,938 Inventory + 492 NonInventory creates; IA offset JE 80493; equity reclass JE 80500 | **Done** 2 Oct |
| — | 1 Oct sales | **Posted** 2 Oct (SalesReceipts 80494–80499, ₦3,211,950.00, MATCH, FIFO COGS ₦1,933,536.56) |
| W9 | Company A daily automation on the server | **Not on.** One routine: `code_scripts/akponora_ops/daily_run.py` (catalogue → vendors+bills → sales → guard, 06:00 Lagos). Branch `claude/akponora-daily-run`. Steps in [`docs/SERVER_SETUP.md`](docs/SERVER_SETUP.md): push + deploy, copy mapping/vendors/cursors/tokens to `/data`, dry-run smoke test, env switches with chat yes. Until then each day is run by hand from this repo |
| W10 | Safe legacy inactivation | After W9 is stable |

### Being built now (2 Oct) — `code_scripts/akponora_ops/`

| Job | What | Writes |
| --- | --- | --- |
| `catalogue_sync` | Finds new/changed EPOS products; maps pack children to their master using the EPOS Master Products amount; creates new `AKP-`/`AKP-NS-` items at **qty 0**; checks EPOS stock and PO history and flags unexplained stock; installs the new mapping version. Also runs just before the Company A transform when `OIAT_COMPANY_A_CATALOGUE_SYNC_BEFORE_SALES=1` | Creates + mapping install only with an approval ref (or the automated env gate, capped) |
| `bills_sync` | Turns received EPOS POs (received ≥ 1 Oct) into unpaid QBO Bills on the mapped items in the item's unit; vendor map, duplicate checks, review sheet + Slack | Posts only bills a human marked `Approve=yes` (or the automated env gate, capped). Never pays |
| `item_guard` | Daily read-only QBO scan: non-`AKP-` items, lines on `LEGACY —`/`15030`/unmapped items, 120xxx activity, wrong asset account, near-duplicate names, negative stock. Slack alert | None (GET only) |
| `ops_scheduler` | Cron runner (`akponora-ops` container). With `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1` runs only `daily_run`, and the portal scheduler skips Company A (no double runs) | — |
| `daily_run` | The single scheduled Company A routine, in order; exit 0/3/2; one Slack summary; global run lock. New suppliers → QBO vendor only with `OIAT_COMPANY_A_VENDOR_AUTO_CREATE` (+ ref, cap 5); near matches hold | Only through each step's own gates |

Enabling any automated write mode on production is a chat-yes action.

Open items:

- Undeposited Funds `100900` ₦29,233,799.99 (25 Sep–1 Oct receipts) awaits the till-sheet deposits; 25, 26 and 29 Sep sheets were blank. A teammate is building the "Daily Sales Account Breakdown" sheet → server → QBO deposit flow (review later).
- 11 items went negative on 1 Oct (deliveries not yet billed); they clear when the October bills are posted.
- **438 legacy items were never renamed** (W5 renamed only the 3,879 whose names clashed with a new item). They have no Sku and no `LEGACY —` prefix; 428 still carry qty (~₦9.14M), so a bookkeeper could pick them. `item_guard` treats any October line on them as an ALERT. Owner to decide: a W5 follow-up rename (chat yes) or accept. List: `outputs/item_guard_firstrun/report.json` → `legacy_not_renamed`.
- Equity: `300100` −₦531,210,337.71, `300150` ₦427,977,166.81; full clean-up deferred to year end.

- 492 final NonInventory products. The owner may later turn frozen food, eggs and rice into tracked EPOS masters (one master per family in grams/each, children deduct); any such EPOS change requires a fresh pull and map rebuild before posting.
- Pack multipliers are no longer inferred from blank `VolumeOfSale`: all 2,212 non-owner products were checked against EPOS Master Products evidence. Five name-suffix and 14 cost-ratio disagreements remain flagged for review, but the map follows EPOS and the 1 Oct stock proof is 100% for comparable families.
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

**Needs a chat yes for the specific action:** sales backfills; journals (Sep close, GRNI, IA offset); W5 renames; W7 creates; installing the mapping; `catalogue_sync apply`; `bills_sync post`; turning on any automated mode (`OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE`, `OIAT_COMPANY_A_BILLS_AUTO_POST`, `OIAT_COMPANY_A_VENDOR_AUTO_CREATE`, `OIAT_COMPANY_A_DAILY_RUN_ENABLED`, the before-sales hook) or the pipeline/scheduler; any InventoryAdjustment; inactivating any item; Undeposited Funds deposits/transfers; any Bill Payment on `66251`; creating accounts (e.g. the GRNI liability); changing QBO settings.

**Forbidden:** InventoryAdjustment on the sales path; bulk inactivation or deletion of products; legacy qty edits to match EPOS; January-style qty-10 import; catch-all or legacy items as October targets.

---

## Evidence locations (gitignored — never commit)

- `outputs/nora_gaps_2026-09-25/`: final mapping, W5 plan, catalogue pulls, Sep close draft, bills/PO drafts, stock bridge, invoice drift, backfill verification.
- `outputs/akponora_uf_allocation_2026-09-26/`: Undeposited Funds work.
- EPOS packs: `../MISC/AKPONORA Investigation/COGS Analysis/As of …/`.
- Do not commit QBO exports, tokens, workbooks with live stock, or `runtime/`.

Company B (Goldplates) is out of scope unless asked.

# Akponora Oct 1 Inventory go-live — start-now status (18 Sep 2026)

Executed the plan’s **start now** slice. No live QBO creates, renames, inactivations, journals, or pipeline start.

## Files produced

Live stock / dumps are gitignored. Durable docs and code are in this PR.

| Artifact | Path |
| --- | --- |
| W1 all QBO items | `outputs/akponora_oct1_golive_2026-09-18/w1_qbo_items_live.csv` |
| W1 Inventory only | `outputs/akponora_oct1_golive_2026-09-18/w1_qbo_inventory_items_live.csv` |
| W1 summary | `outputs/akponora_oct1_golive_2026-09-18/w1_qbo_item_dump_summary.json` |
| W2 workbook | `outputs/akponora_oct1_golive_2026-09-18/w2_oct1_inventory_mapping_16sep.xlsx` |
| W2 build-now CSV | `outputs/akponora_oct1_golive_2026-09-18/w2_build_now.csv` |
| W2 collisions | `outputs/akponora_oct1_golive_2026-09-18/w2_collisions_legacy_rename.csv` |
| W2 negatives | `outputs/akponora_oct1_golive_2026-09-18/w2_negative_opening_0.csv` |
| W2 237 blocked | `outputs/akponora_oct1_golive_2026-09-18/w2_staff_237_blocked.csv` |
| W2 counts | `outputs/akponora_oct1_golive_2026-09-18/w2_summary.json` |
| Dry-run create payloads (qty 0) | `outputs/akponora_oct1_golive_2026-09-18/w4_dryrun_create_payloads_qty0.jsonl` (+ `.csv`) |
| Dump / mapping scripts | `code_scripts/scripts/akponora_oct1_w1_item_dump.py`, `akponora_oct1_w2_mapping.py` |
| Operating plan | `AGENTS.md` |
| Bookkeeper freeze note | `docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md` |

## W2 counts (16 Sep pack + live dump)

- Product List: **6,038** (5,801 PROVISIONAL / 237 BLOCK)
- **Build-now non-negative unique-name / not-in-237-family: 5,504** (matches the plan)
- Plus **160** negatives in that set → opening 0 → **5,664** dry-run create payloads (qty 0, InvStartDate 2026-10-01)
- Of 5,664: **2,347** have EPOS Product ID; **5,002** sell on till; **662** not on till
- **3,786** collide with a **live active** QBO Inventory name → W5 `LEGACY —` rename before create
- Name length rejects: **0**
- Staff 237: **159** sell-on-till; **7** Priority 1 (Backwoods); yellow columns **0 filled**

## Live dump vs 16 Sep pack (surprises)

- Active Inventory **4,316** (plan). Inactive Inventory also in QBO: **6,146** (total Inventory objects 10,462).
- July snapshot had **1,747** negative-qty items. Live **active** negative-qty: **1,666**. Active positive: **2,588**. Active zero: **62**.
- Conversion CSV “QBO Exact Match” (July export): **3,932**. Live active name hits across all 6,038 EPOS rows: **4,003**. Build-now collisions vs live: **3,786** vs plan **3,630** (+156) because the live catalogue moved since July.
- QBO Item query returns **AvgCost = 0**, so FIFO AssetValue is not in the dump. Do not treat the AssetValue column as subledger value. QtyOnHand is populated.
- IA still **₦142,028,049.94**. `120100` **₦207,339.54** and `120202` **₦22,379.81** refilled after 16 Sep zero. Freeze is not optional.
- 9 Inventory creates in September, including Red Bull Watermelon **17 Sep** Id `15031` qty 12.

## W4 code (this PR)

- Conversion CSV may target **Inventory** (or Non-inventory / Service). Optional `Target QBO Item Id`.
- Sales path accepts Inventory **only** if that Id is on the approved new-Id list; legacy name collisions fail closed.
- `create_inventory_item` still refused for Company A / conversion. No catch-all for TxnDate ≥ **2026-10-01**. Catch-all remains for pre-Oct history.
- `company_a.json`: `fail_closed_from=2026-10-01`, `auto_fix_wrong_type_items=false`, `auto_fix_inv_start_date_blockers=false`.
- Company A live `inactivate-all` and Inventory import stay blocked. Quantity-apply stays removed.
- Tests in `code_scripts/tests/test_product_conversion.py` (28 tests).

## Ready for Marvin’s next yes

1. Send the freeze note to the bill-enterer (W0).
2. **W5** — rename the 3,786 colliding live names to `LEGACY — {name}` (no inactivate).
3. After 30 Sep pack: **W7** live creates (qty from that file, not from these qty-0 dry-runs) + same-day IA offset to `300150`.
4. **W8** Sep backfill to catch-all `15030` + option-1 Sep COGS.
5. **W9** turn the pipeline on only after approved Ids are in the map and a dry-run day is 100% mapped.

## Still blocked

| Blocker | What it blocks |
| --- | --- |
| Human freeze | Stops more Red Bull-style items and `120000` refill |
| Staff 237 yellow (till-sold ~159, Priority 1 = 7) | Those families only — not the 5,664. Server-on still needs till-sold 237 or those buttons off the till |
| 30 Sep EPOS pack | Opening qty/value, Sep COGS, 16–30 Sep sales backfill, create-night IA offset amount |
| Chat yes | Live creates, LEGACY renames, journals, pipeline on, inactivation lab |

## Not done (and not in this slice)

Live item creates; live LEGACY renames; Sep COGS; sales backfill 16–30 Sep; create-night IA offset; pipeline on; bulk inactivate; qty-apply / patching legacy to EPOS.

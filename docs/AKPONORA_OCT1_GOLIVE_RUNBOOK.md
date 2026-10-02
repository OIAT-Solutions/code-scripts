# Akponora (Company A) — 30 Sep close and 1 Oct go-live runbook

> **Status (2 Oct 2026):** the cutover steps in this file are **done** (see [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md)). For daily running from October, use [`AKPONORA_DAILY_OPERATIONS.md`](AKPONORA_DAILY_OPERATIONS.md). Remaining: W9 server deploy (chat yes), QBO closing date in the UI, UF till-sheet deposits, owner decision on the 438 unrenamed legacy items.

Step-by-step for the team. Rules and policy: [`AGENTS.md`](../AGENTS.md). History: [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md). Tool details: [`code_scripts/scripts/akponora_cutover/README.md`](../code_scripts/scripts/akponora_cutover/README.md).

**Every step marked ✋ is a production QBO write: get the chat yes for that step, run the dry-run first, and verify afterwards.** Stop at the first failed verification. Never retry a write blindly: read QBO first.

---

## 0. Setup (each team member, once)

```bash
git fetch origin
git checkout cursor/post-akponora-qbo-writes-51f3
git pull
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Run everything from the **repo root** with:

```bash
export OIAT_COMPANIES_DIR=code_scripts/companies
```

- **Production writes run on one machine only (the ops Mac).** It holds `.env` (EPOS + QBO credentials) and `runtime/` (QBO tokens, upload ledger, mapping). These are never in Git; don't copy tokens around.
- The mapping file must exist: `runtime/mappings/company_a/approved.csv`. Before W7 it is the header-only template (`cp templates/product_conversion_empty.csv runtime/mappings/company_a/approved.csv`).
- Check the tools work: `python -m code_scripts.scripts.akponora_cutover.epos_catalogue_pull --help`.

---

## 1. Before the store closes on 30 Sep

### 1a. ✋ W5 — rename legacy items (owner runs)

Name only. The current v3 plan has 3,879 rows, including QBO Ids 11796 and 10688 added by the master-link correction. Quantities, costs, accounts and the IA balance are verified unchanged after every item. The script stops on the first difference.

```bash
python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --test --execute
```

Check `outputs/nora_gaps_2026-09-25/w5_legacy_rename/results.csv`: 10 rows `RENAMED`, balances unchanged. Then run the rest (~45–60 min, resumable):

```bash
python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --all --execute
```

Rollback, if ever needed: `--rollback` (dry-run by default; uses `rollback.csv`).

### 1b. QBO closing date (owner, in the QBO UI)

Gear → Account and settings → Advanced → Accounting → **Close the books**, date **31/08/2026**, with a password only the owner holds. Move it to 30/09/2026 after the step 5 journals are verified.

### 1c. Bookkeeper

Send the freeze note. Bills and invoices stay paused until step 7 is done.

---

## 2. After the store closes on 30 Sep — W6 data pack

Business day = **05:00 Lagos to 05:00 Lagos**. Take the pack after the last sale, ideally before 05:00 on 1 Oct.

```bash
# Product list with IDs, pack sizes, tracking (read-only)
python -m code_scripts.scripts.akponora_cutover.epos_catalogue_pull

# Sales 25–30 Sep, downloaded only (no posting), with business-day totals
python -m code_scripts.scripts.akponora_cutover.epos_sales_download --from 2026-09-25 --to 2026-09-30
```

Staff also export from EPOS into `../MISC/AKPONORA Investigation/COGS Analysis/As of 30th September 2026/`:

- **Stock Levels report** (full + loose units, cost). Masters newly tracked in EPOS need a **physical count** entered first.
- Purchase orders / goods received since 16 Sep. Ernest's answer on the 19 Sep stock adds. Confirmation of duplicate POs `3828`/`3829`/`3855`/`3861`.

If the catalogue changed since 26 Sep (e.g. new tracked masters for frozen food/eggs), rebuild the mapping (`build_canonical` → `review_approval` → `build_final_mapping`, see the tools README), check the coverage, and re-run the W5 collision check for any new names.

---

## 3. ✋ W8 — post 25–30 Sep sales to the catch-all

```bash
python run_pipeline.py --company company_a --from-date 2026-09-25 --to-date 2026-09-30
python -m code_scripts.scripts.akponora_cutover.verify_backfill --from 2026-09-25 --to 2026-09-30 --bookkeeping "<CSV from epos_sales_download>"
```

- Every day must say `MATCH`.
- `verify_backfill` must report ALL OK: only item `15030`, totals = EPOS, no duplicate DocNumbers.
- Reference: 25 Sep EPOS gross ₦4,905,275.00.

Then deposit those receipts out of Undeposited Funds the same way as 26 Sep (till sheet; `akponora_cutover/uf_*`, dry-run first). ✋

---

## 4. W7 dry-run first: fix the opening value V

V (the 30 Sep opening stock value) must be **one number used in both the September close and the create-night offset**. Take it from the W7 dry-run on the **physical 30 Sep count**. Rows staff could not count are not stock: leave them at 0, never at a "ghost" quantity.

```bash
M=code_scripts.scripts.akponora_cutover
F=outputs/final_mapping_2026-10-01
python -m $M.w7_create_items create --mapping $F/approved_mapping_final_v3.csv --inventory-list $F/create_list_inventory.csv --noninventory-list $F/create_list_noninventory_v3.csv --stock-report "<StockReport_2026_09_30_close_for_opening.csv>" --catalogue "<30 Sep catalogue_products.json>" --as-of 2026-09-30 --out outputs/w7_2026-09-30_v3
```

- The output folder has `payloads.jsonl`, the summary with **V** and the payload SHA, and the live preflight.
- **Zero collisions are required.** Any collision means W5 is incomplete.
- Definitive v3 dry-run: 3,938 Inventory + 492 NonInventory, **V ₦148,824,877.40**, payload SHA `9854f694def575675639160f5b283bcdea4c5a61895799ffcdee6ec64b6594e8`, 0 account/tax problems. It correctly refuses until the 3,879-row W5 plan has executed.

---

## 5. ✋ September close journals

Post from reviewed JSON specs (templates in `code_scripts/scripts/akponora_cutover/journal_templates/`, all DRAFT at ₦0.00):
- **dry-run** prints the payload + SHA;
- **execute** requires `--expect-sha` to match.

| DocNumber | Lines | Amount |
| --- | --- | --- |
| `GRNI-2026-09` | Dr purchases 200xxx (POs received after 16 Sep) / Dr `300150` (POs received before the 16 Sep reset) / Cr **GRNI liability** | From `bills_from_epos_pos`, ex-tax, possible duplicates excluded, plus Ernest's 19 Sep adds if they were deliveries. ✋ **The GRNI liability account does not exist yet**: create it first (`200xxx`-style liability, e.g. "Goods received not invoiced"), then put its Id in the spec |
| `SEP-120XXX-CLEAR` | Dr `120100` / Cr `200100`; Dr `120202` / Cr `200202` (every 120xxx to ₦0) | Live balances that night (were −₦13,430,840.54 / −₦29,398.70) |
| `COGS-2026-09` | IA `77` ↔ `200000 Cost of sales` `76` | **V − IA `77` balance** after the two journals above |

```bash
python -m $M.post_journal post --spec <spec.json>
python -m $M.post_journal post --spec <spec.json> --execute --expect-sha <sha> --approval-ref "<chat yes>"
```

After all three: IA `77` = **V**, every 120xxx = ₦0. Move the QBO closing date to 30/09/2026.

---

## 6. ✋ W7 — create the new items (night of 30 Sep)

Requires W5 done and step 5 posted (so B, the IA before creates, includes them). Re-run the step 4 dry-run if anything changed, then:

```bash
# Pilot 3 items; check them in QBO
python -m $M.w7_create_items create <same args as step 4> --execute --test 3 --approval-ref "<chat yes>" --expect-payloads-sha <sha from dry-run>
# All (resumable; skips SKUs that already exist)
python -m $M.w7_create_items create <same args as step 4> --execute --approval-ref "<chat yes>" --expect-payloads-sha <sha from dry-run>
```

Expected: 3,938 Inventory (`AKP-…`, InvStartDate 2026-10-01, asset `77`) + 492 NonInventory (`AKP-NS-…`).

### 6a. ✋ IA offset (same night, dated 1 Oct)

```bash
python -m $M.post_journal offset --w7-summary outputs/w7_2026-09-30_v3/summary_execute.json --approved-v 148824877.40 --out outputs/w7_2026-09-30_v3/offset.json
python -m $M.post_journal post --spec outputs/w7_2026-09-30_v3/offset.json
python -m $M.post_journal post --spec outputs/w7_2026-09-30_v3/offset.json --execute --expect-sha <sha> --approval-ref "<chat yes>"
```

DocNumber `INV-EQ-2026-10-01`: credit IA `77` / debit `300150` = `B + C − V`. Afterwards IA `77` must equal **V** exactly.

### 6b. ✋ Install the mapping with Item Ids

```bash
python -m $M.w7_create_items fill-ids --mapping $F/approved_mapping_final_v3.csv --register outputs/w7_2026-09-30_v3/register.csv --out-csv outputs/w7_2026-09-30_v3/approved_mapping_with_ids.csv
python -m code_scripts.scripts.install_conversion_mapping --source outputs/w7_2026-09-30_v3/approved_mapping_with_ids.csv --destination runtime/mappings/company_a/approved.csv --sha256 <sha printed by fill-ids> --approval-ref "<chat yes>"
```

---

## 7. 1 Oct — first live day

Sales for business day 1 Oct are posted on the 2nd (after 05:00).

1. **Dry-run** (no writes):
   ```bash
   python run_pipeline.py --company company_a --target-date 2026-10-01 --dry-run
   ```
   It must be 100% mapped: no unmapped lines, no legacy or catch-all ItemRefs, gross = EPOS. Unmapped lines name the EPOS product; fix the mapping (step 2 rebuild, then 6b), never the catch-all.
2. ✋ **Manifest + post:**
   ```bash
   python -m code_scripts.operations_controls make-manifest --evidence runtime/conversion_preflight/company_a_sales_batch_2026-10-01.json --out <secure>/approval_2026-10-01.json --approved-by "<name>" --chat-ref "<chat ref>" --expires-hours 24
   export COMPANY_A_POSTING_APPROVAL_FILE=<secure>/approval_2026-10-01.json
   python run_pipeline.py --company company_a --target-date 2026-10-01
   ```
3. **Verify:**
   - `verify_backfill --from 2026-10-01 --to 2026-10-01 --bookkeeping <1 Oct CSV> --allowed-from-mapping` is ALL OK.
   - QBO item quantities moved by the expected units, FIFO COGS posted, IA decreased accordingly.
   - Re-running the day posts nothing.
4. ✋ **W9: unattended daily operation (standing approval).** After a clean day and the owner's chat yes, add to the server `.env` (loaded into the `scheduler` and `web` containers via `env_file`):
   ```bash
   OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1
   OIAT_COMPANY_A_STANDING_APPROVAL_REF="owner standing approval, <date>, <chat ref>"
   # optional cap on the day's EPOS gross, naira
   OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS=15000000
   ```
   Then `docker compose up -d scheduler web`. Check with `docker compose exec scheduler env | grep OIAT_COMPANY_A`.
   - **When it runs:** the system schedule fires at 18:00 Lagos (`SCHEDULE_CRON=0 18 * * *`, `SCHEDULE_TZ=Africa/Lagos`) and posts "yesterday's" business day. The run on **2 Oct posts 1 Oct**, the run on 3 Oct posts 2 Oct, and so on.
   - **What it checks before posting (all must pass):**
     - The dry-run preflight is complete: every line is on an approved `AKP-`/`AKP-NS-` item, with no catch-all or legacy items.
     - Payload gross = EPOS raw gross for the day (±₦1), and the receipt/line counts match.
     - There is no posting hold.
     - The mapping SHA in the evidence = the installed mapping.
     - The optional gross cap.
   - **If it passes:** it writes a 6-hour manifest (`approved_by auto:scheduler`, your ref) in `/data/approvals/`, posts with it, then reconciles. A re-run posts nothing.
   - **If anything fails:** nothing is posted. The posting hold is written with the failed gate(s), Slack gets the failure message with the reason, and the job exits 1. Fix the cause and check QBO. Then run `python -m code_scripts.operations_controls show-hold` / `clear-hold --approved-by NAME --reason '…'`. Re-run the missed day(s) with `python run_pipeline.py --company company_a --target-date <day>`.
   - **To stop:** remove `OIAT_COMPANY_A_STANDING_APPROVAL_REF` (back to manual manifests) or set `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=0` (Company A out of the schedule).
   - Details: the canonical runbook, section "Unattended daily operation (standing approval)".
5. **Bookkeeper resumes:** bills as item lines on the **new** items, in their unit. Customer invoices on new items with the matching EPOS stock-out. September deliveries are billed against the GRNI account, not stock items. No `SR-` invoice numbers.

If anything fails in October, the posting hold stops later posts. Investigate, then `python -m code_scripts.operations_controls show-hold` / `clear-hold` (see the controls doc). Never switch conversion off or use the catch-all.

---

## Open items (post go-live)

| Item | Owner | Action |
| --- | --- | --- |
| W9: deploy + standing approval on OIAT-SRV-01 | Owner | Push branch, copy mapping to server `STATE_ROOT`, set env, chat yes. Until then post each day by hand from this repo |
| QBO closing date 30 Sep 2026 | Owner | Set in the QBO UI |
| Undeposited Funds ₦29.23M (25 Sep–1 Oct) | Team | Till-sheet deposits (`uf_*`); blank sheets for 25/26/29 Sep; teammate's sheet→server→QBO flow later |
| 11 items negative after 1 Oct sales | Bookkeeper / `bills_sync` | Post October received POs as Bills on the new items |
| 438 legacy items never renamed | Owner | W5 follow-up rename (chat yes) or accept; list in `outputs/item_guard_firstrun/report.json` → `legacy_not_renamed` |
| 492 NonInventory (frozen food, eggs, rice, …) | Owner / team | Optional: one tracked EPOS master per family, then `catalogue_sync` / map rebuild |
| Duplicate POs `3828`/`3829`/`3855`/`3861` | Store | Confirm before bill entry |
| W10 legacy inactivation | Later | Only after stable October; qty→0 to `300150`, chat yes per batch |

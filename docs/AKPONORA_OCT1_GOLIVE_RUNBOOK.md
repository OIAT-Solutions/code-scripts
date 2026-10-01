# Akponora (Company A) — 30 Sep close and 1 Oct go-live runbook

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

Name only. Quantities, costs, accounts and the IA balance are verified unchanged after every item. The script stops on the first difference.

```bash
python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --test --execute
```

Check `outputs/nora_gaps_2026-09-25/w5_legacy_rename/results.csv`: 10 rows `RENAMED`, balances unchanged. Then run the rest (~45–60 min, resumable):

```bash
python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --all --execute
```

Rollback, if ever needed: `--rollback` (dry-run by default; uses `rollback.csv`).

### 1b. QBO closing date (owner, in the QBO UI)

Gear → Account and settings → Advanced → Accounting → **Close the books**, date **31/08/2026**, with a password only the owner holds. Move it to 30/09/2026 after step 4.

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
F=outputs/nora_gaps_2026-09-25/final_mapping_2026-09-26
python -m $M.w7_create_items create --mapping $F/approved_mapping_final.csv --inventory-list $F/create_list_inventory.csv --noninventory-list $F/create_list_noninventory.csv --stock-report "<StockReport_2026_09_30_*.csv>" --catalogue "<30 Sep catalogue_products.json>" --as-of 2026-09-30 --out outputs/w7_2026-09-30
```

- The output folder has `payloads.jsonl`, the summary with **V** and the payload SHA, and the live preflight.
- **Zero collisions are required.** Any collision means W5 is incomplete.
- 25 Sep rehearsal: 3,939 Inventory + 536 NonInventory, V ≈ ₦160.8M, 0 account/tax problems, collisions only from the not-yet-run W5.

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

Expected: 3,939 Inventory (`AKP-…`, InvStartDate 2026-10-01, asset `77`) + 536 NonInventory (`AKP-NS-…`).

### 6a. ✋ IA offset (same night, dated 1 Oct)

```bash
python -m $M.post_journal offset --w7-summary outputs/w7_2026-09-30/summary_execute.json --approved-v <V> --out outputs/w7_2026-09-30/offset.json
python -m $M.post_journal post --spec outputs/w7_2026-09-30/offset.json
python -m $M.post_journal post --spec outputs/w7_2026-09-30/offset.json --execute --expect-sha <sha> --approval-ref "<chat yes>"
```

DocNumber `INV-EQ-2026-10-01`: credit IA `77` / debit `300150` = `B + C − V`. Afterwards IA `77` must equal **V** exactly.

### 6b. ✋ Install the mapping with Item Ids

```bash
python -m $M.w7_create_items fill-ids --mapping $F/approved_mapping_final.csv --register outputs/w7_2026-09-30/register.csv --out-csv outputs/w7_2026-09-30/approved_mapping_with_ids.csv
python -m code_scripts.scripts.install_conversion_mapping --source outputs/w7_2026-09-30/approved_mapping_with_ids.csv --destination runtime/mappings/company_a/approved.csv --sha256 <sha printed by fill-ids> --approval-ref "<chat yes>"
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
4. ✋ **W9:** after a clean day, set `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1` for the scheduler (Company A sales schedule only).
5. **Bookkeeper resumes:** bills as item lines on the **new** items, in their unit. Customer invoices on new items with the matching EPOS stock-out. September deliveries are billed against the GRNI account, not stock items. No `SR-` invoice numbers.

If anything fails in October, the posting hold stops later posts. Investigate, then `python -m code_scripts.operations_controls show-hold` / `clear-hold` (see the controls doc). Never switch conversion off or use the catch-all.

---

## Open items (not blocking go-live)

| Item | Owner | Action |
| --- | --- | --- |
| 536 EPOS-untracked products (frozen by kg, eggs, rice, bags) | Owner / team | Optional: make one tracked EPOS master per family (unit g or Each; children deduct grams/units; test-sale-and-void one family first). Then re-pull and rebuild the mapping; they become Inventory. List: `MISC/…/As of 25th September/AKPONORA_536_untracked_products_2026-09-26.xlsx` |
| 109 pack-named children deducting 1 (e.g. EVA BAR SOAP150g*4) | Team | Check the EPOS Master Products amount; fix EPOS if it should deduct N, then rebuild the mapping |
| Pricing anomalies | Team | `final_mapping_2026-09-26/pricing_review.csv` |
| Staff questions | Ernest / store | 19 Sep stock adds; duplicate POs |
| W10 legacy inactivation | Later | Only after stable October operation; lab → small batches, qty→0 to `300150`, chat yes per batch |

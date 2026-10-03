# Akponora / NORA (company_a) cutover tools

Reusable versions of the September 2026 cutover scripts. Read [`AGENTS.md`](../../../AGENTS.md) before any QBO write, inventory change or Company A pipeline change.

**Cutover is done (1–2 Oct 2026).** Day-to-day work (new products, bills from POs, item guard) lives in [`code_scripts/akponora_ops/`](../../akponora_ops/) — see [`docs/AKPONORA_DAILY_OPERATIONS.md`](../../../docs/AKPONORA_DAILY_OPERATIONS.md). The tools below stay for evidence re-runs, W10 and incident recovery.

- Run every tool from the repo root as `.venv/bin/python -m code_scripts.scripts.akponora_cutover.<tool> --help`.
- `OIAT_COMPANIES_DIR` defaults to `code_scripts/companies`. QBO tokens come from `code_scripts.token_manager.get_access_token` and are never printed.
- Outputs go to `outputs/<tool>_<timestamp>/` unless you pass `--out`.
- Evidence packs (EPOS exports, stock reports and BookKeeping CSVs) resolve under `--evidence-root`, which defaults to `../MISC/AKPONORA Investigation/COGS Analysis`. Any explicit file argument overrides it.
- Defaults reproduce the 25–26 Sep 2026 runs in `outputs/nora_gaps_2026-09-25/`.

In the table, `<COGS>` means `../MISC/AKPONORA Investigation/COGS Analysis` and `G` means `outputs/nora_gaps_2026-09-25`.

| Tool | Purpose | Access | Inputs | Outputs | Example |
| --- | --- | --- | --- | --- | --- |
| `epos_catalogue_pull` | Full EPOS product list with IDs, captured from the JSON that the Catalogue page loads | EPOS view only (`--from-captures`: offline) | EPOS login (pipeline credentials), or an existing `catalogue_api_pages.json` | `catalogue_api_pages.json`, `catalogue_products.json`, `catalogue_products.csv` (Id, Name, CategoryName, IsStockTracked, SellOnTill, UnitOfSale, VolumeOfSale, costs, SalePriceIncTax, Barcode, Sku, ArticleCode); `--dom-rows` adds `catalogue_list_rows.csv` | `python -m code_scripts.scripts.akponora_cutover.epos_catalogue_pull` |
| `epos_product_families families` | Staff workbook: each master with its children, plus a "No master found" sheet and a Read me | Offline | `catalogue_products.json`, `mapping_evidence.csv` | `AKPONORA_EPOS_Product_Families_<date>.xlsx` | `... epos_product_families families --catalogue G/epos_catalogue_2026-09-26_1158/catalogue_products.json --evidence G/canonical_2026-09-26_1158/mapping_evidence.csv --as-of "26 Sep 2026 11:58"` |
| `epos_product_families untracked` | List of untracked (NonInventory) products with Sep sales lines and value | Offline | catalogue, evidence, BookKeeping CSV | `AKPONORA_<n>_untracked_products_<date>.xlsx` | `... epos_product_families untracked` |
| `build_canonical` | Proposes canonical families and the EPOS-to-QBO mapping (tiers A/B/C) | Offline | catalogue, StockReport, BookKeeping (coverage and history), `--qbo-items` CSV (required), optional `--staff-review` | `families.csv`, `mapping_proposal.csv`, `mapping_evidence.csv`, `qbo_create_draft.csv`, `staff_blockers.csv`, `data_problems.csv`, `summary.json` | `... build_canonical --qbo-items <nora_readiness>/qbo_items.csv --staff-review <staff_review>/review.json` |
| `review_approval` | Deterministic accountant checks of tiers A/B, with seeded sampling of tier B | Offline | `--base` canonical folder, `--catalogue`, `--sales`, `--seed` | `approved_mapping_staging.csv`, `still_blocked.csv`, `families_approved.csv`, `review_out.json` | `... review_approval --base G/canonical_2026-09-26_1937` |
| `build_final_mapping` | Applies the 26 Sep owner rules (a)–(e) and builds the final map and create lists | Offline | canonical and approval folders, catalogue, sales, `--items-dump`, `--w5-plan` | `approved_mapping_final.csv`, `create_list_inventory.csv`, `create_list_noninventory.csv`, `pricing_review.csv`, `opening_qty_review.csv`, `account_mapping.csv`, `w5_additional_collisions.csv`, `stats.json` | `... build_final_mapping --w5-plan G/w5_legacy_rename/plan_before_26sep_additions.csv` |
| `w5_legacy_rename` | Renames colliding legacy items to `LEGACY — …` (`--test`, `--all`) or restores them from `rollback.csv` (`--rollback`) | Dry-run: QBO GET only. **`--execute`: writes to QBO** (sparse Name update; owner chat yes required) | `--w5-dir` (`plan.csv`, `rollback.csv`, `results.csv`) | results CSV (`results_dryrun.csv`, `results.csv`, `rollback_results*.csv`) | `... w5_legacy_rename --test` |
| `verify_backfill` | Checks SalesReceipts per business day: allowed item IDs only, totals within ₦1 of EPOS, no duplicate DocNumbers | QBO GET only | `--from`/`--to`, plus `--controls` CSV or `--bookkeeping` CSV (05:00 cutoff); `--allowed-items` / `--allowed-from-mapping` | `verification_<from>_<to>.json`; exit code 1 on problems | `... verify_backfill --from 2026-09-16 --to 2026-09-24 --bookkeeping "<COGS>/As of 25th September/BookKeeping_2026_09_25_1245.csv"` |
| `month_close_draft` | Draft of the option-1 month-end COGS movement journal, plus the create-night IA offset draft | QBO GET only (`--no-qbo`: offline). Never posts | `--month`, StockReport, Products dir, BookKeeping, opening report and value | `sep_close_draft_<tag>.json`, `sep_close_cost_mismatches_<tag>.csv`, `sep_purchases_<tag>.csv` | `... month_close_draft --month 2026-09 --stock-report <30 Sep report> --closing-label 2026-09-30 --out-tag 2026-09-30` |
| `bills_from_epos_pos` | `capture-pos` (EPOS view only), `capture-qbo` (GET only), `parse-pdf` and `build` (offline): one draft bill per unbilled EPOS PO | Never posts | PO list and details, QBO snapshot, catalogue | `po_received_<YYYY-MM>.csv`, `po_bill_match_<YYYY-MM>.csv`, `bills_to_enter.csv`, `bills_to_enter_totals.csv`, `summary.json` | `... bills_from_epos_pos build` |
| `stock_bridge` | Per-product stock and value bridge between two stock reports. `scrape-adjustments` captures EPOS Stock Takes and Movements (view only) | Offline / EPOS view only | two StockReports, BookKeeping, catalogue, evidence, `po_received` CSV, adjustments JSONL | `bridge.csv`, `summary.json` | `... stock_bridge build` |
| `epos_master_links` | Reads each product's real "Master Products" link from the Advanced Edit page | EPOS view only (resumable) | `--ids-file` | `products/<id>.json` | `... epos_master_links --ids-file ids.txt --out <dir>` |
| `epos_sales_download` | Downloads a BookKeeping CSV for a date range without posting, then prints business-day totals | EPOS export only (`--summarize`: offline) | `--from`/`--to`, or `--summarize CSV` | the CSV plus `business_day_totals.csv` | `... epos_sales_download --from 2026-09-25 --to 2026-09-30` |
| `uf_allocation_draft` | Draft Undeposited Funds allocation from the Nora daily till sheet | Offline | `--sheet` (.xlsx download of the till sheet), `--bookkeeping` | `proposed_deposits.csv`, `daily_allocation.csv`, `monthly_by_bank.csv`, `terminal_to_qbo_map.csv`, `flags.csv`, `summary.json`, `uf_allocation_draft.xlsx` | `... uf_allocation_draft --sheet nora_sales.xlsx` |
| `uf_list_transfers` | Lists QBO Transfers that involve Undeposited Funds | QBO GET only | none | `qbo_transfers_uf_readonly.json` | `... uf_list_transfers` |
| `archive/uf_reverse_and_allocate` (archived 3 Oct 2026; replaced by `akponora_ops/uf_deposits`) | `probe` (default, GET only). **`delete` / `deposit` / `tail` / `all` write to QBO and require `--execute`** | **Already executed on 26 Sep 2026. Do not re-run delete or deposit.** | `--inputs-dir` (`qbo_transfers_uf_readonly.json`, `proposed_deposits.csv`) | `qbo_uf_probe.json` (and, for write phases, the evidence JSONs and the report) | `... uf_reverse_and_allocate --phase probe` |

| `w7_create_items create` | Builds the new Inventory (`AKP-`) and NonInventory (`AKP-NS-`) items from the final map and the 30 Sep count, computes the opening value V, runs a live collision/account preflight, and with `--execute` creates them (resumable; `--test N` for a pilot) | Dry-run: QBO GET only. **`--execute`: creates items in QBO** (chat yes, `--approval-ref`, `--expect-payloads-sha`) | `--mapping`, `--inventory-list`, `--noninventory-list`, `--stock-report`, `--catalogue`, `--as-of` | `payloads.jsonl`, summary (V, B, payload SHA), `register.csv`, `results.csv` | see the runbook, steps 4 and 6 |
| `w7_create_items fill-ids` | Fills the new QBO Item Ids into the approved map from the create register and checks it loads | Offline | `--mapping`, `--register` | `approved_mapping_with_ids.csv` (then `install_conversion_mapping`) | runbook step 6b |
| `post_journal post` | Posts one reviewed JournalEntry from a JSON spec: balance, account, DocNumber and closing-date checks | Dry-run prints the payload + SHA. **`--execute --expect-sha`: writes to QBO** | `--spec` (templates in `journal_templates/`: `cogs_2026_09`, `sep_120xxx_clear`, `grni_2026_09`, `create_night_ia_offset`) | receipt JSON | runbook step 5 |
| `post_journal offset` | Computes the create-night IA offset `B + C − V` and writes a ready spec (`INV-EQ-2026-10-01`) | Offline | `--w7-summary`, `--approved-v` | offset spec JSON | runbook step 6a |

Step-by-step use on cutover night: [`docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md`](../../../docs/AKPONORA_OCT1_GOLIVE_RUNBOOK.md).

`archive/` holds the June–September investigation helpers, unchanged, for reference only. They contain hard-coded paths. See `archive/README.md`.

## Re-run checks (30 Sep 2026)

Each of these was re-run into a scratch folder and compared with the September originals:

- **`build_canonical`** on the 19:37 catalogue: every CSV is byte-identical to `canonical_2026-09-26_1937`.
- **`review_approval`**: the 1937 run is byte-identical. On the base without a time suffix (`canonical_2026-09-26`), the CSVs are identical.
- **`build_final_mapping`**: with `plan_before_26sep_additions.csv` (the plan in use when the original ran), every output and `stats.json` is identical.
- **`epos_product_families`**: both workbooks match cell for cell (3,940 masters, 1,680 children, 535 with no master; 536 untracked).
- **`bills_from_epos_pos build`**: all outputs are identical.
- **`stock_bridge build`**: `summary.json` is identical. `bridge.csv` has the same rows; only the order of tied rows differs, because that order is now deterministic.
- **`epos_catalogue_pull --from-captures`**: rebuilds all three `catalogue_products.json` files exactly.

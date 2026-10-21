# Akponora / NORA (company_a) cutover modules and operator tools

Read [`AGENTS.md`](../../../AGENTS.md) before any QBO write, inventory change or Company A pipeline change.

**Cutover is done (1–2 Oct 2026).** Day-to-day work lives in [`code_scripts/akponora_ops/`](../../akponora_ops/) — see [`docs/AKPONORA_DAILY_OPERATIONS.md`](../../../docs/AKPONORA_DAILY_OPERATIONS.md). What is left here is either imported by `akponora_ops` or still used by hand:

- **Shared by `akponora_ops` (do not remove):** `_common` (env setup, business date, read-only QBO, EPOS login), `w7_create_items` (QBO client, hashing helpers), `bills_from_epos_pos` (PO capture, name matching), `epos_catalogue_pull`, `epos_master_links`, `build_final_mapping` (`ns_name`).
- **Operator tools still in use:** `vendor_admin`, `post_journal` (with `journal_templates/`), `w5_legacy_rename` (W10 legacy inactivation).

The one-off September builders and checks (`build_canonical`, `review_approval`, `epos_product_families`, `verify_backfill`, `month_close_draft`, `stock_bridge`, `epos_sales_download`, `uf_allocation_draft`, `uf_list_transfers`, `archive/`) were removed on 5 Oct 2026; they are in git history.

- Run every tool from the repo root as `.venv/bin/python -m code_scripts.scripts.akponora_cutover.<tool> --help`.
- `OIAT_COMPANIES_DIR` defaults to `code_scripts/companies`. QBO tokens come from `code_scripts.token_manager.get_access_token` and are never printed.
- Outputs go to `outputs/<tool>_<timestamp>/` unless you pass `--out`.
- Evidence packs resolve under `--evidence-root`, which defaults to `../MISC/AKPONORA Investigation/COGS Analysis`. Any explicit file argument overrides it.

In the table, `G` means `outputs/nora_gaps_2026-09-25`.

| Tool | Purpose | Access | Inputs | Outputs | Example |
| --- | --- | --- | --- | --- | --- |
| `epos_catalogue_pull` | Full EPOS product list with IDs, captured from the JSON that the Catalogue page loads | EPOS view only (`--from-captures`: offline) | EPOS login (pipeline credentials), or an existing `catalogue_api_pages.json` | `catalogue_api_pages.json`, `catalogue_products.json`, `catalogue_products.csv` (Id, Name, CategoryName, IsStockTracked, SellOnTill, UnitOfSale, VolumeOfSale, costs, SalePriceIncTax, Barcode, Sku, ArticleCode); `--dom-rows` adds `catalogue_list_rows.csv` | `python -m code_scripts.scripts.akponora_cutover.epos_catalogue_pull` |
| `build_final_mapping` | Applies the 26 Sep owner rules (a)–(e) and builds the final map and create lists | Offline | canonical and approval folders, catalogue, sales, `--items-dump`, `--w5-plan` | `approved_mapping_final.csv`, `create_list_inventory.csv`, `create_list_noninventory.csv`, `pricing_review.csv`, `opening_qty_review.csv`, `account_mapping.csv`, `w5_additional_collisions.csv`, `stats.json` | `... build_final_mapping --w5-plan G/w5_legacy_rename/plan_before_26sep_additions.csv` |
| `w5_legacy_rename` | Renames colliding legacy items to `LEGACY — …` (`--test`, `--all`) or restores them from `rollback.csv` (`--rollback`) | Dry-run: QBO GET only. **`--execute`: writes to QBO** (sparse Name update; owner chat yes required) | `--w5-dir` (`plan.csv`, `rollback.csv`, `results.csv`) | results CSV (`results_dryrun.csv`, `results.csv`, `rollback_results*.csv`) | `... w5_legacy_rename --test` |
| `bills_from_epos_pos` | `capture-pos` (EPOS view only), `capture-qbo` (GET only), `parse-pdf` and `build` (offline): one draft bill per unbilled EPOS PO | Never posts | PO list and details, QBO snapshot, catalogue | `po_received_<YYYY-MM>.csv`, `po_bill_match_<YYYY-MM>.csv`, `bills_to_enter.csv`, `bills_to_enter_totals.csv`, `summary.json` | `... bills_from_epos_pos build` |
| `epos_master_links` | Reads each product's real "Master Products" link from the Advanced Edit page | EPOS view only (resumable) | `--ids-file` | `products/<id>.json` | `... epos_master_links --ids-file ids.txt --out <dir>` |
| `w7_create_items create` | Builds the new Inventory (`AKP-`) and NonInventory (`AKP-NS-`) items from the final map and the 30 Sep count, computes the opening value V, runs a live collision/account preflight, and with `--execute` creates them (resumable; `--test N` for a pilot) | Dry-run: QBO GET only. **`--execute`: creates items in QBO** (chat yes, `--approval-ref`, `--expect-payloads-sha`) | `--mapping`, `--inventory-list`, `--noninventory-list`, `--stock-report`, `--catalogue`, `--as-of` | `payloads.jsonl`, summary (V, B, payload SHA), `register.csv`, `results.csv` | see the runbook, steps 4 and 6 |
| `w7_create_items fill-ids` | Fills the new QBO Item Ids into the approved map from the create register and checks it loads | Offline | `--mapping`, `--register` | `approved_mapping_with_ids.csv` (then `install_conversion_mapping`) | runbook step 6b |
| `post_journal post` | Posts one reviewed JournalEntry from a JSON spec: balance, account, DocNumber and closing-date checks | Dry-run prints the payload + SHA. **`--execute --expect-sha`: writes to QBO** | `--spec` (templates in `journal_templates/`: `cogs_2026_09`, `sep_120xxx_clear`, `grni_2026_09`, `create_night_ia_offset`) | receipt JSON | runbook step 5 |
| `post_journal offset` | Computes the create-night IA offset `B + C − V` and writes a ready spec (`INV-EQ-2026-10-01`) | Offline | `--w7-summary`, `--approved-v` | offset spec JSON | runbook step 6a |
| `vendor_admin` | Renames or creates company_a QBO vendors from a reviewed JSON spec, then syncs `vendors.csv` for the bills job | Dry-run: QBO GET only. **`--execute`: writes to QBO** | `--spec` | updated `STATE_ROOT/mappings/company_a/vendors.csv` | `... vendor_admin --spec spec.json` |


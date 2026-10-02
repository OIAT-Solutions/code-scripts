# QBO query and debug scripts

Run from the repo root. All scripts take `--company` (required) and use the company config for realm_id and tokens.

> **Company A (AKPONORA, `company_a`):** read [`AGENTS.md`](../../../AGENTS.md) first. The query scripts below are read-only and safe.
> In `qbo_inv_manager.py`, `set-invstart`, `set-invstart-bulk`, `set-invstart-from-csv`, `recreate-invstart`, live `inactivate-all`
> and live `import-products` are refused for Company A. Never delete QBO products or historical sales receipts, never bulk-inactivate.

**Inventory manager** (item lookup and InvStartDate):

| Script | Purpose | Example |
|--------|---------|---------|
| `code_scripts/scripts/qbo_inv_manager.py` | Get item by ID/name; list InvStartDate issues; set InvStartDate (single, bulk, or from CSV; not Company A); **import products** from a QBO-style CSV (not Company A live) | `python code_scripts/scripts/qbo_inv_manager.py --company company_a get --item-id 7220`; `list-invstart --cutoff-date 2026-01-01`; `--company company_b set-invstart-bulk --cutoff-date 2026-01-01 --new-date 2026-01-01` |

**Query scripts** (under `code_scripts/scripts/qbo_queries/`):

| Script | Purpose | Example |
|--------|---------|---------|
| `qbo_query.py` | Run an arbitrary QBO SQL-like query (read-only) | `python code_scripts/scripts/qbo_queries/qbo_query.py --company company_a query "select Id, Name from Item maxresults 5"` |
| `qbo_account_query.py` | Run Account queries (name-based matching) | `python code_scripts/scripts/qbo_queries/qbo_account_query.py --company company_a --account-name "120300 - Non - Food Items"` |
| `qbo_verify_mapping_accounts.py` | Verify Product.Mapping.csv accounts exist in QBO | `python code_scripts/scripts/qbo_queries/qbo_verify_mapping_accounts.py --company company_b` |

**Export scripts** (under `code_scripts/scripts/bills/`):

| Script | Purpose | Example |
|--------|---------|---------|
| `qbo_export_bills.py` | Export Bills to CSV by date range (bills_header.csv + bills_lines.csv); read-only | `python code_scripts/scripts/bills/qbo_export_bills.py --company company_a --from 2020-01-01 --to 2026-01-31 --out ./exports/company_a_bills` |

Optional: `--verbose` or `--raw-json` where supported to print full JSON. For `qbo_export_bills.py`: `--page-size` (default 1000), `--dry-run` to print count and first 3 bill IDs without writing files.

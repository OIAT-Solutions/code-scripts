# QBO query scripts

Run from the repo root. All scripts take `--company` (required) and use the company config for realm_id and tokens. Both are read-only.

> **Company A (AKPONORA, `company_a`):** read [`AGENTS.md`](../../../AGENTS.md) first.

| Script | Purpose | Example |
|--------|---------|---------|
| `qbo_query.py` | Run an arbitrary QBO SQL-like query (read-only). `run_pipeline.py` calls it as `python -m code_scripts.scripts.qbo_queries.qbo_query` for reconciliation; the portal Tools page runs it too | `python code_scripts/scripts/qbo_queries/qbo_query.py --company company_a query "select Id, Name from Item maxresults 5"` |
| `qbo_verify_mapping_accounts.py` | Verify Product.Mapping.csv accounts exist in QBO (portal Tools page) | `python code_scripts/scripts/qbo_queries/qbo_verify_mapping_accounts.py --company company_b` |

Optional: `--verbose` or `--raw-json` where supported to print full JSON.

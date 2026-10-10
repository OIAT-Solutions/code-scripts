# Scripts

Run all commands from the **repo root** (`code-scripts`).

> **Company A (AKPONORA, `company_a`, realm 9341455406194328):** follow [`AGENTS.md`](../../AGENTS.md) before any QBO write.
> Daily Akponora operations live in `code_scripts/akponora_ops/` (see `docs/AKPONORA_DAILY_OPERATIONS.md`).

| Folder / script | Purpose | Used by |
|-----------------|---------|---------|
| [qbo_queries/](qbo_queries/) | Read-only QBO queries: `qbo_query.py` (reconciliation), `qbo_verify_mapping_accounts.py` | `run_pipeline.py` reconcile step; portal Tools page |
| [akponora_cutover/](akponora_cutover/) | Company A modules shared with `akponora_ops` (QBO client, EPOS catalogue and PO capture, mapping helpers) plus the few operator tools still in use (vendor admin, journals, W5/W10 legacy rename) | `akponora_ops/*`; operator CLI |
| `install_conversion_mapping.py` | Validate and atomically install an operator-approved conversion mapping (no QBO writes) | `akponora_ops/catalogue_sync.py` |

The old manual tools (bill/invoice CSV import/export, SalesReceipt delete, one-off cutover builders and checks) were removed on 5 Oct 2026. They are in git history if ever needed.

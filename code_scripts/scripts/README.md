# Scripts

Run all commands from the **repo root** (`code-scripts`), e.g. `python code_scripts/scripts/qbo_inv_manager.py --company company_b ...`.

> **Company A (AKPONORA, `company_a`, realm 9341455406194328):** follow [`AGENTS.md`](../../AGENTS.md) before any QBO write.
> Legacy write tools here are hard-refused for Company A in code: SalesReceipt delete, InvStartDate patches,
> live bill re-import, live invoice import for TxnDate on/after 2026-10-01. Daily Akponora operations live in
> `code_scripts/akponora_ops/` (see `docs/AKPONORA_DAILY_OPERATIONS.md`).

| Folder / script | Purpose | Company A |
|-----------------|---------|-----------|
| [invoice/](invoice/) | Transform raw invoice CSV, prepare (alias/fuzzy match), import invoices into QBO | Live import refused for TxnDate >= 2026-10-01 |
| [bills/](bills/) | Export QBO Bills to CSV, re-import bills from CSV | Export OK; `--create` refused (use `akponora_ops/bills_sync.py`) |
| [qbo_queries/](qbo_queries/) | Ad-hoc read-only QBO queries (items, accounts, etc.) | OK (read-only) |
| [akponora_cutover/](akponora_cutover/) | Akponora / NORA (company_a) cutover tools (catalogue pull, mapping, W5, backfill checks, month close, bills, stock bridge, UF) | Company A only; follow AGENTS.md |
| `install_conversion_mapping.py` | Validate and atomically install an operator-approved conversion mapping (no QBO writes) | Company A; follow AGENTS.md |
| `akponora_canonical_catalog.py` | Rebuild a provisional family catalogue from preserved conversion evidence (read-only) | Company A; follow AGENTS.md |
| `qbo_delete_sales_receipts.py` | Delete sales receipts in QBO (by date or DocNumber) | **Refused** in every mode |
| `qbo_inv_manager.py` | Inventory / InvStartDate management | Read-only subcommands only |

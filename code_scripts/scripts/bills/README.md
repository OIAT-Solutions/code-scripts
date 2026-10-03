# Bills scripts

Scripts for exporting and re-importing QBO Bills (e.g. for InvStartDate fixes or bulk operations).

**Run from repo root** (code-scripts).

> **Company A (AKPONORA):** `qbo_import_bills.py --create` is refused for `company_a`. It matches
> item lines by name; Akponora bills are created from POs by `code_scripts/akponora_ops/bills_sync.py`.
> See [AGENTS.md](../../../AGENTS.md). Never delete Company A bills to "re-import" them.

## Export Bills to CSV

Export bills in a date range to header + line CSVs for backup or re-import.

```bash
python scripts/bills/qbo_export_bills.py --company company_a --from 2020-01-01 --to 2026-01-31 --out ./exports/company_a_bills/
```

## Re-import Bills

Re-create one or more bills from exported CSVs (e.g. after deleting in QBO and updating InvStartDate).

```bash
# Dry run
python scripts/bills/qbo_import_bills.py --company company_a --bill-id 123 --dry-run

# Create (not company_a)
python scripts/bills/qbo_import_bills.py --company company_b --bill-id 123 --create
python scripts/bills/qbo_import_bills.py --company company_b --bill-ids 58984 58985 58986 --create
python scripts/bills/qbo_import_bills.py --company company_b --all --create
```

Pass exactly one of: `--bill-id`, `--bill-ids`, or `--all`. See script docstring for tax and DocNumber options.

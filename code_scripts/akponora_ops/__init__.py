"""Ongoing (post-cutover) operations for AKPONORA / NORA (company_a).

- ``catalogue_sync``: new EPOS products -> mapping rows and QBO items (qty 0).
- ``bills_sync``: received EPOS purchase orders -> reviewable QBO Bills on AKP- items.
- ``item_guard``: read-only daily scan of QBO for items / lines that break the October contract.

Every production write is off by default and gated (see AGENTS.md and
docs/AKPONORA_DAILY_OPERATIONS.md).
"""

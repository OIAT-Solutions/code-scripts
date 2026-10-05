"""Ongoing (post-cutover) operations for AKPONORA / NORA (company_a).

- ``catalogue_sync``: new EPOS products -> mapping rows and QBO items (qty 0).
- ``bills_sync``: received EPOS purchase orders -> reviewable QBO Bills on AKP- items.
- ``item_guard``: read-only daily scan of QBO for items / lines that break the October contract.
- ``vendors``: fuzzy vendor matching + gated automatic vendor creation (used by ``bills_sync``).
- ``uf_deposits``: Undeposited Funds -> bank deposits + true-up transfers from the till sheet
  (``till_sheet`` reads the Google Sheet / xlsx).
- ``daily_run``: the single scheduled routine (catalogue -> vendors+bills -> sales -> guard -> uf),
  started by the portal schedule worker (the "Daily routine" schedule; see docs/SCHEDULING_AUTHORITY.md).

Every production write is off by default and gated (see AGENTS.md and
docs/AKPONORA_DAILY_OPERATIONS.md).
"""

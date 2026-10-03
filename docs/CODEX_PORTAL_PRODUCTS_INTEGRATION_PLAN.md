# Products & Stock: shared pipeline integration

Updated 3 October 2026 after Marvin confirmed Claude's pipeline work.

## Agreed source and refresh

- Read `STATE_ROOT/ops/company_a/stock_snapshot/latest.json`.
- Refresh through a monitored background job: `python -m code_scripts.akponora_ops.stock_snapshot run`.
- The snapshot owns EPOS vs QuickBooks quantities, canonical units, status and products not yet mapped. The portal displays those results; it does not reproduce the comparison or conversion logic.
- Remove the provisional per-product live reads. The draft product layout is preserved outside the checkout for reuse after the documented schema is available.
- Keep all pipeline changes in Claude's scope. Do not edit `code_scripts/akponora_ops/`.

## Page behaviour

Search the current saved list by product, category and identifier. Show the source check time, approved QuickBooks link, stock unit, both stock figures, difference and a readable status. Separate missing links from broken or unchecked links. New unmapped EPOS products must appear even though they have no QuickBooks item.

Use the last successful snapshot while a refresh is running or fails. Show stale, incomplete or unreadable source records explicitly; missing quantities do not mean zero. Keep sales buttons and main stock products distinguishable and avoid counting shared stock twice.

The refresh action requires login, run permission and CSRF. Record who requested it, when it ran and its outcome in the existing job queue. No page load starts a remote pull. No refresh creates products, changes stock, installs a mapping or posts an accounting transaction.

## Approval contracts to connect after documentation

- Per-product catalogue approval/exclusion through `catalogue_sync apply --only/--exclude`.
- Permanent exclusions through `review_exclusions add|remove|list`.
- Supplier approval/create through `vendors approve --link-to | --create`, retaining its dry-run SHA.
- Approval references come from the authenticated user and confirmation time: `Approved by <user> in the portal, <date time>`. Keep the explicit confirmation, reason, permissions, evidence checks and audit record.

Exact schemas and CLI signatures are pending their addition to `docs/CODEX_BRIEF_PORTAL_REDESIGN.md`. Do not guess fields, status meanings or command arguments.

## Verification before calling this page complete

Use fixtures matching the documented snapshot contract, including missing links, negative stock, pack children, stale data, partial reads and failed refreshes. Test search and pagination at the live catalogue's approximate size, plus job permissions, CSRF, duplicate requests and command scope. Run the full portal and pipeline suites; check desktop and phone layouts with screenshots.

Read-only live inspection on 3 October verified that daily catalogue captures and the installed mapping exist on the server. The latest inspected capture contains 6,146 products, IDs, categories, stock-tracking and volume fields; it does not contain live stock quantities. This verifies the existing source shape, not the pending stock-snapshot contract or a deployed portal refresh.

After approved deployment, verify the page reads the live snapshot, a read-only refresh publishes updated results, new unmapped products appear and the UI shows the same quantities/statuses as the pipeline record. Production deployment and financial writes remain separate actions requiring the applicable authorization.

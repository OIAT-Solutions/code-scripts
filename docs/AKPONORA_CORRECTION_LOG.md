# AKPONORA correction log

Live exports and receipts remain under server STATE_ROOT, never Git.

## 6 October 2026 — Coke and Colgate pack deductions

- Approval: Marvin's chat instruction, “act as the accountant, put reasonable notes where possible, in easy clear writing, apply the fixes”.
- EPOS Product 1689065 (Coke six-pack): master 1609946, deduction 12 → 6 Each. Verified after saving and again after navigating back.
- EPOS Product 1673462 (Colgate single): master 1673466, deduction 12 → 1 Each. Verified after saving.
- Both map to their existing QBO Inventory items: 15540 and 16203. Sale and purchase multipliers corrected together, without changing product identities or item units.
- Pipeline global lock held during the paired EPOS/sales-map correction. Purchase-unit installation also took the global lock. No concurrent pipeline could post during those operations.
- Previous mapping: `b4d8c6402c2c24145c9e0b08f508f418695631983a38a8a4268021d01a7f73e6`.
- Intermediate sale-map SHA: `e2fcb3f09256729acc775e8d27214e4b522eb81cafe0d69b77fdfdc7b7f4a7a6`.
- Final mapping SHA: `d71115bdabd84b3664099ff60433537ce3a5eea8642c1bdd71ee71d194e40262`, installed `2026-10-06T23:14:49Z` (19:14 Toronto, 00:14 Lagos on 7 October).
- Versions and approval receipts: `/data/mappings/company_a/versions/`. Additional preflight, verification and mapping evidence: `/data/ops/company_a/stock_recovery_2026_10_06/`.
- No QBO stock quantity adjustment, journal, bill or historical receipt rewrite was performed.
- Accounting notes: [stock correction decisions](AKPONORA_STOCK_ACCOUNTING_NOTES.md). Use existing COGS account 82 for supported count variance; opening exclusions remain separate and unposted.
- Automatic approval review rejected creating a new variance account and initially rejected code deployment/build. The new account is unnecessary with account 82 available. Marvin then explicitly approved deploying `a8867bc` and enabling the read-only daily check; both completed. Web and scheduler checks passed, and the signed-in live portal rendered 123 classification-only stock cards with the incomplete-capture warning. A live read of 6 October produced 17 events and one incomplete transfer (5644927), not a false clean result.
- QBO GET verification confirmed both affected item quantities were unchanged by the pack correction and that the global lock was released. InventoryAdjustment GET is supported, but no quantity-posting adapter or FIFO/value sandbox proof exists. No Company A sandbox config was found in the server's Company A config directory.
- Bread POs 3976 and 3986 still have no `EPOS-PO-` Bill in QBO. The current supplier mapping already matches active vendor 34, so the older supplier-name hold is stale. No new supplier mapping, bill or payment was written by this work.
- Final acceptance identified the sales-date versus capture-date issue; the narrow follow-up changes the movement window to include today's Lagos date, with a regression test using yesterday's sales date.

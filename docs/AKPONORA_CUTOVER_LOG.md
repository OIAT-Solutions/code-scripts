# Akponora (Company A) cutover log

Dated record of production QBO writes and key findings. Newest last. The governing plan is [`AGENTS.md`](../AGENTS.md); tomorrow's steps are in [`AKPONORA_OCT1_GOLIVE_RUNBOOK.md`](AKPONORA_OCT1_GOLIVE_RUNBOOK.md). Evidence folders are under `outputs/` (gitignored, contains live stock and financial data; never commit).

Company: AKPONORA VENTURES LTD. / NORA MINI MART (`company_a`, QBO realm `9341455406194328`, production).

## Before 16 Sep 2026

- Inventory-sync quantity apply removed in code. Do not re-enable it.
- Transfer `68049` (31 Mar 2026, ₦58,659,919, Moniepoint `6397730972` → `120000 - Inventory`) deleted.
- Jan–Jun zero-floor journals `COGS-ZF-2026-01` … `COGS-ZF-2026-06`: debit Inventory Asset `77`, credit `COGS Historical Correction - Jan-Jun 2026` `1150040049`, total ₦281,069,104.71. JournalEntry Ids `74403`–`74407`, `74556`.

## 16 Sep 2026

- July/August journals (option 1, stock movement only): `COGS-2026-07` JE `75097`, `COGS-2026-08` JE `75098` (Dr IA `77` / Cr `200000 - Cost of sales` `76`).
- Catch-all NonInventory `AKP-UNMAPPED-EPOS-SALES` Id `15030` created (income `1150040024` / 400100). **Jan–Sep history only.**
- Inventory GL true-up: equity `300150` Id `86`; `INV-CONS-2026-09-16` JE `75153`, `INV-EQ-2026-09-16` JE `75154`. IA set to the EPOS 16 Sep zero-floor value **₦142,028,049.94**.
- Sales backfill to catch-all through 15 Sep.

## 18 Sep 2026

- Live W1 item dump (read-only): 4,316 active Inventory items; IA ₦142,028,049.94. `120100`/`120202` had refilled from bills after the 16 Sep zero. REDBULL WATERMELON Id `15031` created 17 Sep (example of what the freeze stops).
- Bookkeeper freeze note issued (`docs/AKPONORA_BOOKKEEPER_FREEZE_NOTE_18_Sep_2026.md`).
- The 18 Sep W2 "5,664 creates" list was **withdrawn** on 19 Sep (it made one item per EPOS row, double-counting packs). Superseded by the canonical family model.

## 25 Sep 2026

- **Sales backfill 16–24 Sep** to catch-all `15030` (chat yes): 51 SalesReceipts, ₦38,412,440. Every day MATCHes EPOS and independent source controls. No creates, patches or adjustments (`outputs/nora_gaps_2026-09-25/backfill_0916_0924_verification.json`).
- Finding: `120100` −₦13,430,840.54 / `120202` −₦29,398.70 are fully explained by **22 backdated Invoices to GPFH (Gold Plates Feast House)** created 21–23 Sep on legacy Inventory items (COGS ₦13,689,958.59, ₦3.08M dated in closed August). They are off-till sales and not in EPOS. Invoices are kept (customer AR); the COGS is corrected in the September close (`outputs/nora_gaps_2026-09-25/invoice_drift_reconciliation.md`).
- Bill `75164` (₦290k, entered 18 Sep) broke the freeze; no edit needed.

## 26 Sep 2026

- **Owner decisions:** bills and customer invoices **paused** until the QBO fix; act as accountant using best practice.
- EPOS catalogue pulled read-only for the first time with Product ID, VolumeOfSale (= sale multiplier) and IsStockTracked for every product.
- **Undeposited Funds (separate session, chat yes):**
  - Deleted the 37 Bank→UF "monthly sales" transfers (₦1,382,097,453.05), including `68091`.
  - Deposited all 1 Jan–24 Sep sales receipts to the till-sheet banks: 1,266 Bank Deposits (Ids `75245`–`76510`) plus 5 true-up transfers (`76511`–`76515`).
  - JE `76516` `UF-OPEN-WRITEOFF-2025` (Dr `100900` / Cr `300100` ₦20,710,565.14). **UF = ₦0.**
  - Report: `outputs/akponora_uf_allocation_2026-09-26/UF_REVERSE_AND_ALLOCATE_REPORT.md`.
- September purchases: 231 EPOS POs received 1–25 Sep (₦81.8M inc). All 77 QBO September bills (₦24.6M, item lines on legacy Inventory) match POs. **154 POs (₦57.1M) are unbilled.** Treatment: GRNI accrual in the Sep close (see runbook). POs `3828`/`3829`/`3855`/`3861` are possible duplicates (`outputs/nora_gaps_2026-09-25/bills_draft_2026-09/`).
- Stock bridge 16→25 Sep: the ₦13.46M "added without a PO" is ₦9.45M manual EPOS stock adjustments plus ₦4.69M formula artefact. Open question for staff: Ernest's 19 Sep "New Stock" (₦2.35M liquor) and "System" adds (₦5.96M) — deliveries or recounts? (`outputs/nora_gaps_2026-09-25/stock_bridge_0916_0925/`).
- Team fixed EPOS product costs via a Chrome agent (26 tasks, verified live). Owner confirmed **EPOS product setup is final**.
- **Final approved mapping:** all 6,150 products in the 26 Sep 19:37 catalogue, 99.89% of Sep till value.
  - 5,614 rows go to **3,939 new Inventory families** (`AKP-{owner EPOS ProductID}`, asset `77`, COGS 200xxx by category).
  - **536 EPOS-untracked products** go to NonInventory `AKP-NS-{ProductID}`. The owner thinks many (e.g. frozen food by kg) should become tracked EPOS masters; see the runbook's open item.
  - Price/setup anomalies are mapped with the multiplier EPOS actually deducts (`pricing_review.csv`).
  - Files: `outputs/nora_gaps_2026-09-25/final_mapping_2026-09-26/`.
- **W5 rename plan:** 3,877 active legacy items → `LEGACY — {name}` (name only). Dry-run verified. **Not yet executed** (the permission system blocks agent QBO writes; the owner runs it) (`outputs/nora_gaps_2026-09-25/w5_legacy_rename/`).
- Code: cutover branch merged into PR #62.
  - From 1 Oct: Inventory `AKP-` or NonInventory `AKP-NS-` targets only.
  - Approval manifest and posting hold apply to October onward only. September refunds/0-qty lines still post.
  - The scheduler excludes Company A unless `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1`.
  - Duplicate EPOS names are allowed when each row has a Product ID.

## 30 Sep 2026

- Tools organized under `code_scripts/scripts/akponora_cutover/`, docs consolidated, PR #62 updated.
- Last QBO SalesReceipt date: **24 Sep**. 25 Sep EPOS gross ₦4,905,275.00 (not yet posted).

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

## 1–2 Oct 2026 (read-only recovery; no production writes in this work)

- Live QBO verification confirms the 25–30 Sep catch-all backfill is now present and balanced: 35 SalesReceipts, only item `15030`, no duplicate DocNumbers. Daily totals are ₦4,905,275.00; ₦4,859,500.00; ₦4,080,500.00; ₦3,113,300.00; ₦5,097,324.99; and ₦3,965,950.00.
- A multiplier defect was found in the mapping builders: blank child `VolumeOfSale` could silently become x1. All 2,212 mapped non-owner products were read through the view-only EPOS Master Products page. The builders now fail closed without an explicit child multiplier, and the full 409-test suite passes.
- Mapping v3 has 6,150 rules and 258 evidence-backed corrections: 112 multiplier fixes, 95 real-master retargets and 51 rows moved to their own NonInventory item because no live tracked master exists. The final create set is 3,938 Inventory + 492 NonInventory.
- The 1 Oct stock proof improved from 451/485 sold families matching (92.99%) to 496/496 (100%). All 3,936 families with usable Stock History match; the two `NO_HISTORY` rows had no activity.
- The definitive v3 W7 read-only preflight calculated opening **V = ₦148,824,877.40** and payload SHA `9854f694def575675639160f5b283bcdea4c5a61895799ffcdee6ec64b6594e8`. It refused, as designed, because W5 is not executed.
- The W5 plan now has 3,879 rows. Two v3 additions (QBO Ids 11796 and 10688) passed a fresh read-only dry-run with all monitored balances unchanged. **W5 remains unexecuted.**
- September close journals, W7 creates/offset, mapping installation and Company A scheduler activation remain approval-gated and were not run.

## 1 Oct 2026, evening (production writes, owner chat yes 22:07 EDT)

- **September close journals**, all `POSTED_VERIFIED` (receipts next to each spec):
  - `SEP-120XXX-CLEAR` JE `76552`: Dr `120100` ₦13,430,840.54 / Cr `200100`; Dr `120202` ₦29,398.70 / Cr `200202`. Payload SHA `0e7c38ce…`.
  - `COGS-2026-09` JE `76553`: Dr IA `77` / Cr `200000` `76` ₦6,796,827.46. Payload SHA `27ac8fe8…`.
  - Account `210200 - Goods Received Not Invoiced` created (Id `87`, Other Current Liability / AccruedLiabilities).
  - `GRNI-2026-09` JE `76554`: Dr 200xxx ₦35,579,577.33 + Dr `300150` ₦11,654,130.30 / Cr `87` ₦47,233,707.63. Payload SHA `32ee13dd…`. Not auto-reversed: the real September bills are entered against `210200`.
- Live check after the journals: IA `77` = **₦148,824,877.40 = V**; every 120xxx account ₦0.
- **W5 rename done**: all 3,879 legacy items renamed `LEGACY — …` (3,878 `RENAMED` + 1 `ALREADY_RENAMED` from an interrupted first run). Balances unchanged at every 250-item check (`w5_legacy_rename/results.csv`, `run_all_2026-10-01.log`).

## 2 Oct 2026, early hours (production writes, owner chat yes 22:28 EDT)

- **W7 creates done**: post-W5 dry-run had zero collisions, V ₦148,824,877.40, payload SHA `9854f694…` unchanged. Pilot 3 (Ids `15032`–`15034`) verified, then all: **3,938 Inventory + 492 NonInventory created**, 0 failures. B ₦148,824,877.40; C ₦148,824,877.40 (C difference ₦0.00). Evidence `outputs/w7_2026-09-30_v3/` (`results.csv`, `register.csv`, `summary_execute.json`).
- **IA offset** `INV-EQ-2026-10-01` JE `80493`: Dr `300150` / Cr IA `77` ₦148,824,877.40, `POSTED_VERIFIED`. IA `77` = **₦148,824,877.40** as of 30 Sep and 1 Oct.
- **Mapping installed**: `runtime/mappings/company_a/approved.csv`, 6,150 rules → 4,430 Item Ids, sha256 `b4d8c640…`.
- **1 Oct dry-run**: first run refused on a zero-quantity HENNESSY VS 70cl line (same-tender sale +1 and refund −1). The transform now drops aggregated rows whose quantity and all amounts are zero (October conversion path only; real refunds still fail the day). Re-run clean: EPOS gross ₦3,211,950.00 = processed, 6 receipts / 892 lines / 568 items, all on registered new items, nothing created or patched. **Not posted yet.**

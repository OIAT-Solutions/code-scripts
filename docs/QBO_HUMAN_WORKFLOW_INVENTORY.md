# Human QBO Workflow Inventory

Comprehensive checklist of every QuickBooks Online task a human operator performs
(or should perform) for this business. Derived from repo scripts, docs, company
configs, PR history, and the AGENTS.md COGS recovery plan.

**Companies in scope:**
- **Company A** — AKPONORA VENTURES LTD. (inventory-enabled, COGS remediation in progress)
- **Company B** — GOLDPLATES FEASTHOUSE LTD. (non-inventory / Service items)

**Legend — automation status:**
- **Automated** — pipeline handles end-to-end; human only monitors.
- **Partially automated** — script does heavy lifting but human triggers, reviews, or completes a step.
- **Manual** — entirely human-performed in the QBO UI or via external process.
- **Recommended-but-missing** — should exist based on business logic but has no script, doc, or process today.

**Confidence flags:**
- Items marked **⚠ NEEDS HUMAN CONFIRMATION** include a yes/no question for Marvin.
  The repo provides evidence the task *probably* exists, but I cannot verify it
  is actually being done (or done at the stated cadence) without human input.

---

## A. Routine Operational QBO Work

### A1. Sales / Revenue

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A1.1 | **Daily Sales Receipt upload** | Pipeline downloads EPOS BookKeeping CSV, transforms it, uploads Sales Receipts to QBO via API, archives files, and reconciles totals. | `run_pipeline.py`, `qbo_upload.py`, `epos_playwright.py`, `epos_to_qb_single.py`, `qbo_query.py` (reconcile) | **Automated** | Daily (scheduled via Windows Task Scheduler or Docker scheduler at ~18:00–19:00 WAT) | Runs for both companies. Deduplication (local ledger + QBO query) prevents duplicates. Slack notifications on start/success/failure/reconciliation. **⚠ NEEDS HUMAN CONFIRMATION:** Is the daily pipeline actually running unattended for both companies right now, or is it being triggered manually? (The AGENTS.md notes that Company A sales upload is currently *blocked* until conversion mode is enabled — is Company A upload still paused?) |
| A1.2 | **Review reconciliation mismatches** | When Phase 5 reconciliation shows EPOS ↔ QBO total mismatch, a human must investigate the difference. | `qbo_query.py reconcile`, Slack notification | **Partially automated** | After each pipeline run (when mismatch detected) | The script compares totals and sends Slack; the human decides whether to re-upload, delete duplicates, or adjust. |
| A1.3 | **Delete and re-upload failed/duplicate Sales Receipts** | When uploads are duplicated or incorrect, use `qbo_query.py delete` then re-run the pipeline for that date. | `qbo_query.py delete`, `run_pipeline.py --target-date` | **Partially automated** | As needed (after reconciliation mismatch) | Script handles deletion with confirmation prompt; human must decide when to invoke. |
| A1.4 | **Monitor Slack pipeline notifications** | Check Slack for start/success/failure/reconciliation messages. Act on failures (token expired, EPOS login changed, network errors). | `slack_notify.py` | **Manual** | Daily | Failure notifications include error reason extraction. Common actions: refresh OAuth tokens, check EPOS credentials, restart pipeline. |
| A1.5 | **Custom date range backfill** | When days are missed (holidays, outages, catch-up), run the custom pipeline for a date range. | `run_pipeline_custom.py --from-date --to-date` | **Partially automated** | As needed | Human decides which dates to backfill. Script handles download, transform, upload, archive, reconcile. |

### A2. Accounts Payable / Bills

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A2.1 | **Create Bills in QBO** | Record vendor purchase invoices (food supplies, beverages, utilities, etc.) as Bills in QBO. | `AGENTS.md` ("A human bookkeeper is still posting vendor Bills to legacy Inventory items"), `code_scripts/scripts/bills/` | **Manual** | Ongoing, as invoices are received | The bookkeeper currently posts bills against legacy Inventory items. Per AGENTS.md, new bills should be redirected off Inventory items until Non-inventory cutover is complete. Bill import scripts exist (`qbo_import_bills.py`) but are for re-import/remediation, not routine entry. **⚠ NEEDS HUMAN CONFIRMATION:** Is it the same person creating bills for both companies, or do Company A and Company B have separate bookkeepers? Are bills entered daily, weekly, or in batches? |
| A2.2 | **Mark Bills as paid (Bill Payments)** | Record payment against existing Bills — link to the correct bank account (Moniepoint, GTB, etc.). | `AGENTS.md` (Bill `66251` payment `74555`), QBO UI | **Manual** | As payments are made | Example from AGENTS.md: Bill `66251` (vehicle, ₦7,279,300) was paid as BillPayment `74555` on 16 Sep 2026. Each payment must specify bank account, date, and amount. **⚠ NEEDS HUMAN CONFIRMATION:** Are Bill Payments recorded promptly when paid, or batched later? Are there bills that remain unpaid/overdue that nobody tracks? |
| A2.3 | **Verify Bill posting accounts** | Ensure bills are posted to the correct expense/asset accounts (e.g., `150000 - Fixed Assets` for vehicles, correct Inventory sub-accounts for stock). | `AGENTS.md`, `docs/BUSINESS_LOGIC.md` | **Manual** | When creating bills | Currently no automated validation. Bills posted to wrong accounts create GL errors. |
| A2.4 | **Redirect new bills away from Inventory items (Company A)** | Until Non-inventory cutover is complete, new purchase bills should not reference legacy Inventory items — use expense accounts or new Non-inventory items instead. | `AGENTS.md` ("Redirect new bills off Inventory items until the Non-inventory cutover") | **Recommended-but-missing** | Ongoing (until cutover) | No script or guard exists to enforce this. **⚠ NEEDS HUMAN CONFIRMATION:** Has the bookkeeper actually been told to stop posting bills against Inventory items? Or are bills still going to legacy Inventory items today? |
| A2.5 | **Record direct expenses / purchases (non-bill)** | Record purchases that aren't formal vendor bills — e.g. cash purchases, petty cash, POS-terminal purchases for supplies. | QBO UI (+ menu → Expense or Check) | **⚠ NEEDS HUMAN CONFIRMATION** | As they occur? | AGENTS.md mentions "Direct purchases already on Inventory (~₦11.2m rice/oil/frozen/eggs) stay as purchases." This implies some purchases are recorded as Expenses/Checks rather than Bills. **Question: Do people record direct expenses (not Bills) in QBO? For what kinds of purchases — cash market buys, petty cash, etc.?** |
| A2.6 | **Track vendor credits / returns** | Record Vendor Credits when goods are returned to suppliers or credits are received. | QBO UI | **⚠ NEEDS HUMAN CONFIRMATION** | As they occur? | QBO webhooks track VendorCredit entity changes, suggesting this entity type is in use. **Question: Do you receive vendor credits or return goods to suppliers? If so, who records them in QBO?** |

### A3. Bank / Undeposited Funds

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A3.1 | **Transfer funds from Undeposited Funds to bank accounts** | Sales Receipts are posted to Undeposited Funds (`100900` for Company A, `1250000` for Company B). A human must create Bank Deposits in QBO to move funds from Undeposited Funds into the correct bank account (Moniepoint, GTB, etc.). | `company_a.json` (`deposit_account: "100900 - Undeposited Funds"`), `company_b.json` (`deposit_account: "1250000 - Undeposited Funds"`) | **Manual** | Daily or weekly (after Sales Receipts are uploaded) | This is the most critical routine human step. Without it, the Undeposited Funds balance grows indefinitely and bank balances in QBO don't reflect reality. Group deposits by payment method (Cash, Card, Transfer) and match to bank statement entries. **⚠ NEEDS HUMAN CONFIRMATION:** Is anyone actually doing this today? How often — daily, weekly, monthly, or not at all? Which bank accounts are used (Moniepoint, GTB, others)? Is the Undeposited Funds balance currently large / backlogged? |
| A3.2 | **Bank reconciliation** | Match QBO bank account balances against actual bank statements. Identify missing transactions, duplicates, or errors. | QBO UI (Banking → Reconcile) | **Manual** | Monthly (or more frequently) | No script support exists. Essential for financial accuracy. **⚠ NEEDS HUMAN CONFIRMATION:** Is bank reconciliation being done at all? By whom — bookkeeper, accountant, Marvin? How far behind is it? |
| A3.3 | **Record bank transfers** | Record inter-account transfers (e.g., between Moniepoint accounts, between banks). | QBO UI (+ menu → Transfer) | **Manual** | As they occur | Example from AGENTS.md: Transfer `68091` (₦58,659,919, Moniepoint → Undeposited Funds). Must ensure these don't create phantom revenue. **⚠ NEEDS HUMAN CONFIRMATION:** How many bank accounts does each company have in QBO? Are inter-bank transfers a regular occurrence? |
| A3.4 | **Record non-sales deposits** | Record deposits that aren't from daily sales — owner contributions, loan disbursements, refunds received, etc. | QBO UI | **Manual** | As they occur | Must be categorized correctly to avoid inflating revenue. **⚠ NEEDS HUMAN CONFIRMATION:** Do the companies receive non-sales income (e.g. owner contributions, loans, investment)? If so, who records them? |
| A3.5 | **Record customer refunds** | If customers receive refunds for sales (returns, overcharges), record Refund Receipts in QBO. | QBO UI | **⚠ NEEDS HUMAN CONFIRMATION** | As they occur? | QBO webhooks track `RefundReceipt` entity changes, implying the entity type exists. **Question: Do the restaurants issue refunds to customers? If so, are they recorded in QBO or just handled in EPOS?** |

### A4. Vendors / Customers / Items Setup

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A4.1 | **Configure Payment Methods** | Ensure QBO Payment Methods match EPOS tender types: Cash, Card, Transfer, Cash/Transfer, Card/Transfer, Card/Cash, Card/Cash/Transfer, Cheque, Credit Card, Direct Debit. | `qbo_upload.py` (`PAYMENT_METHOD_BY_NAME` dict), README §4 "Configure Payment Methods" | **Manual (one-time, then as needed)** | Before first pipeline run; when new tender types appear | Script maps tender text → Payment Method ID. If a new tender appears in EPOS, human must create the Payment Method in QBO and update the mapping dict. |
| A4.2 | **Configure Departments (Locations)** | Ensure QBO Departments exist with names matching EPOS Location column values. | `qbo_upload.py` (`get_department_id`), `company_b.json` (`department_mapping`) | **Manual (one-time, then as needed)** | Before first pipeline run; when new locations open | Company B has 15+ locations. If a new location opens in EPOS, human must create the Department in QBO. Script logs a warning if unmatched. |
| A4.3 | **Manage QBO Items (Products/Services)** | Review auto-created items. Ensure correct Type (Inventory vs Service vs NonInventory), income account, expense account, and tax settings. | `qbo_upload.py` (`get_or_create_item_id`, `AUTO_CREATE_ITEMS`), `docs/BUSINESS_LOGIC.md` | **Partially automated** | Ongoing | Auto-creation creates Service items with a default income account. For Company A (inventory-enabled), items are created as Inventory with category mapping. Human must verify accounts and pricing are correct after auto-creation. |
| A4.4 | **Maintain Product.Mapping.csv (Company A)** | Category-to-account mapping CSV that controls which GL accounts Inventory items use. | `docs/BUSINESS_LOGIC.md` ("Category-to-account mapping enforcement"), `code_scripts/` | **Manual** | When new EPOS categories appear | Missing mapping causes upload failure. Human must add category → (Inventory account, Revenue account, COGS account) mapping. |
| A4.5 | **Manage Vendor records** | Create and maintain vendor records for bill posting. | QBO UI | **Manual** | As new vendors appear | No automation exists. Vendor data is not in EPOS. **⚠ NEEDS HUMAN CONFIRMATION:** Roughly how many active vendors does each company have? Are new vendors added regularly, or is the list fairly stable? |
| A4.6 | **Manage Customer records** | Create and maintain customer records (for invoices). | `code_scripts/scripts/invoice/`, QBO UI | **Partially automated** | As needed | Invoice import reports unmatched customers (`invoice_missing_customers_*.csv`). Human must create missing customers in QBO. Sales Receipts currently don't assign a customer. **⚠ NEEDS HUMAN CONFIRMATION:** Is invoice import (A5) actively being used today, or was it a one-off project? Are there regular repeat customers who need invoices? |
| A4.7 | **Maintain item aliases and spelling corrections** | Keep `templates/item_aliases.csv` and `templates/spelling_corrections.csv` updated so invoice import matches items correctly. | `code_scripts/scripts/invoice/README.md` | **Manual** | Before each invoice import | Required for fuzzy matching between invoice item names and QBO item names. |

### A5. Invoices (Customer Invoicing)

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A5.1 | **Prepare raw invoice CSV** | Transform raw invoice data into the template format. | `scripts/invoice/transform_invoice_raw.py` | **Partially automated** | As invoices are created | Human provides the raw invoice CSV. Script transforms format. **⚠ NEEDS HUMAN CONFIRMATION:** Is this invoice workflow actively used today, or was it a one-time setup? Who creates the raw invoice CSV — is it exported from another system or manually typed? |
| A5.2 | **Prepare and import invoices into QBO** | Run prepare (alias/fuzzy match) then import into QBO. Review unmatched items. | `scripts/invoice/prepare_invoice_csv.py`, `scripts/invoice/qbo_import_invoices.py` | **Partially automated** | As needed (Company A only currently) | Human reviews `*_unmatched.csv` report and resolves missing items/customers before import. |
| A5.3 | **Record invoice payments** | When customers pay invoices, record Receive Payment in QBO against the invoice. | QBO UI | **Manual** | As payments are received | No automation exists. **⚠ NEEDS HUMAN CONFIRMATION:** Are there outstanding customer invoices? Does anyone track A/R aging? |
| A5.4 | **Chase overdue invoices** | Follow up on unpaid invoices past their due date. | QBO UI (Reports → A/R Aging) | **Manual** | Weekly or as needed | No automation exists. |
| A5.5 | **Issue credit notes** | Record Credit Memos in QBO when customer credits are issued (overcharges, returns, adjustments). | QBO UI | **⚠ NEEDS HUMAN CONFIRMATION** | As needed? | QBO webhooks track `CreditMemo` entity changes. **Question: Are customer credit memos ever issued? If so, who creates them?** |

### A6. Tax / VAT

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A6.1 | **Verify VAT on Sales Receipts** | Confirm that uploaded Sales Receipts have correct VAT treatment (7.5% for Company A; 7.5% VAT + 5% Lagos State for Company B). | `qbo_upload.py` (tax handling), `company_a.json`, `company_b.json` | **Automated** (verify periodically) | After pipeline changes | Tax config is in company JSON. Script sends `GlobalTaxCalculation: TaxInclusive`. Human should spot-check a few receipts in QBO after major changes. |
| A6.2 | **File VAT/WHT returns** | Compile VAT collected and paid, file with FIRS / LIRS. | QBO Reports | **Manual** | Monthly / quarterly | No script support. Use QBO Tax Center or export data for the accountant. **⚠ NEEDS HUMAN CONFIRMATION:** Is VAT/WHT filing being done? By whom — in-house or external accountant? What is the filing cadence (monthly, quarterly)? Is Company B subject to Lagos State consumption tax (the 5% component in their config)? |
| A6.3 | **Review tax codes on items** | Ensure all QBO Items have the correct tax code assigned. | QBO UI, `docs/BUSINESS_LOGIC.md` | **Manual** | Periodically | Some auto-created items may have incorrect tax codes. |

### A7. Period Close / Reporting

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| A7.1 | **Month-end close checklist** | Verify all sales uploaded, all bills entered, bank recs done, Undeposited Funds cleared, depreciation posted, accruals recorded, payroll posted. | QBO UI | **Recommended-but-missing** | Monthly | No documented checklist exists. Should be created. **⚠ NEEDS HUMAN CONFIRMATION:** Is any month-end close process being followed today, even informally? Who is responsible — Marvin, a bookkeeper, an accountant? |
| A7.2 | **Close books in QBO** | Set the "Closing date" in QBO to prevent edits to prior periods. | QBO Settings → Advanced → Accounting → Close the books | **⚠ NEEDS HUMAN CONFIRMATION** | Monthly (after month-end close) | Prevents accidental edits to historical data. **Question: Is the QBO closing date being set? If so, what's the last closed period?** |
| A7.3 | **Generate monthly P&L and Balance Sheet** | Pull Profit & Loss and Balance Sheet for the period. Review for anomalies. | QBO Reports | **⚠ NEEDS HUMAN CONFIRMATION** | Monthly | Essential for business oversight. **Question: Is anyone reviewing P&L and Balance Sheet monthly? Or are reports only pulled ad hoc during the COGS remediation?** |
| A7.4 | **Review and approve COGS** | Verify Cost of Goods Sold makes sense relative to revenue and purchases. | QBO Reports, `AGENTS.md` (COGS formula) | **Manual** | Monthly | Per AGENTS.md: `opening + verified purchases − EPOS closing value = COGS`. Currently only relevant for Company A due to inventory tracking. |
| A7.5 | **Post depreciation journal entries** | Record monthly depreciation for fixed assets. | QBO UI (+ menu → Journal Entry) | **⚠ NEEDS HUMAN CONFIRMATION** | Monthly? | **Question: Does either company own fixed assets that need depreciation (vehicles, kitchen equipment, furniture)? Bill `66251` shows a vehicle on `150000 - Fixed Assets` for Company A. Is depreciation being posted?** |
| A7.6 | **Post payroll journal entries** | If payroll is not run through QBO Payroll, post summary journal entries for salaries, taxes, benefits. | QBO UI | **⚠ NEEDS HUMAN CONFIRMATION** | Monthly? | **Question: How is staff payroll handled — through QBO Payroll, an external provider, or not tracked in QBO at all? If external, are summary journals posted?** |
| A7.7 | **Review suspense / clearing accounts** | Check that Undeposited Funds, Opening Balance Equity, and other clearing accounts don't have stale balances. | QBO Reports | **Manual** | Monthly | Undeposited Funds should be near zero if deposits are recorded promptly. AGENTS.md shows Opening Balance Equity is used for inventory corrections (`300100`). |
| A7.8 | **Post accruals / prepayments** | Record accrued expenses (e.g. rent, utilities due but not yet billed) or prepaid expenses at month-end. | QBO UI (Journal Entry) | **⚠ NEEDS HUMAN CONFIRMATION** | Monthly? | **Question: Are any accruals or prepayments recorded in QBO? (e.g. rent paid in advance, utilities accrued)** |
| A7.9 | **Owner's equity / draws** | Record owner draws, dividends, or personal expenses paid from business accounts. | QBO UI | **⚠ NEEDS HUMAN CONFIRMATION** | As they occur? | **Question: Do owners take draws from business accounts? If so, are these tracked in QBO?** |

---

## B. One-Off Remediation / Cleanup Work (COGS Recovery)

These tasks relate to the Company A COGS remediation effort documented in
`AGENTS.md` and `docs/QBO_INVENTORY_REMEDIATION.md`. Most are one-time or
limited-duration tasks that will complete when the Non-inventory cutover is done.

### B1. COGS Journals

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B1.1 | **Post zero-floor COGS journals (Jul–Sep 2026)** | Jan–Jun journals are posted (`COGS-ZF-2026-01` through `COGS-ZF-2026-06`). Jul/Aug/Sep remain. Formula: debit Inventory Asset (id `77`), credit `COGS Historical Correction` (id `1150040049`). | `AGENTS.md` ("Jul/Aug/Sep are not posted"), `outputs/akponora_from_2026-08-22/` | **Partially automated** (Codex prepares drafts, human approves in chat before posting) | Monthly (Jul, Aug, Sep) | Requires EPOS stock history month-end values. Stock History: 30 Jun ₦103M, 31 Jul ₦134M, 31 Aug ₦136M. Sep TBD. Human must approve each journal in chat before any agent posts it. |
| B1.2 | **Propose Inventory GL true-up journal** | One journal to reconcile Inventory Asset GL vs EPOS valuation gap. Two inventory ledgers exist: `Inventory Asset` (id `77`, ~₦250M) + `120000 - Inventory` family (~₦236M). Combined ~₦486M vs EPOS value. | `AGENTS.md` ("Propose one Inventory GL true-up") | **Recommended-but-missing** | One-time (after all monthly COGS journals are posted) | Accountant must approve the target account (could be Inventory Shrinkage or a custom correction account). Codex should prepare; human/accountant approves. |

### B2. Product Conversion / Non-Inventory Cutover

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B2.1 | **Complete staff product mapping workbook** | 237 products need staff confirmation of pack size, sale multiplier, cost, and keep-vs-duplicate decisions. Fill yellow columns in the workbook. | `AGENTS.md` ("237 products still need staff pack/cost/duplicate confirmation"), `outputs/.../akponora_staff_review_237.csv` | **Manual** (staff) | One-time (ASAP) | This blocks the full Non-inventory cutover. Without it, the catch-all item handles everything (acceptable but lossy). |
| B2.2 | **QA the approved product conversion CSV** | Verify: no barcode keys, no double `*N`, no carton cost on each-item, no legacy Inventory name collision, Target type = NonInventory, Effective Date, Approved By. | `AGENTS.md` (Workstream C) | **Manual** (Claude/human QA) | One-time (after staff complete workbook) | Output: `akponora_product_conversion.csv` for the pipeline. |
| B2.3 | **Enable product conversion in pipeline** | Flip `transform.product_conversion.enabled: true` in `company_a.json` and enable in portal DB. | `AGENTS.md` (Workstream A), `code_scripts/product_conversion.py`, `company_a.json` | **Manual** (with agent support) | One-time (after QA is complete) | Requires explicit human approval in chat. Guards exist: mapped products → existing Non-inventory only; collisions with legacy Inventory names fail closed. |
| B2.4 | **Create approved Non-inventory QBO items** | Codex creates new Non-inventory items from the approved mapping (new names/SKUs like `AKP-...`). Dry-run first. | `AGENTS.md` (Workstream B) | **Partially automated** (Codex dry-run, human approves) | One-time (after conversion CSV is approved) | Must not inactivate/delete/merge legacy Inventory items. Must not clash names. |
| B2.5 | **Wire catch-all item into pipeline** | `AKP-UNMAPPED-EPOS-SALES` (Item Id `15030`) is created but not yet wired. When enabled, unmapped EPOS products go to this item with the original EPOS name in the description. | `AGENTS.md` ("Not yet wired into the pipeline"), `catch_all_item_draft.json` | **Manual** (code change + human approval) | One-time (before or during cutover) | Prevents pipeline failure for unknown products. The shop cannot pause sales. |

### B3. Inventory Remediation

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B3.1 | **Delete erroneous InventoryAdjustment transactions** | Remove `INVCON`-prefixed InventoryAdjustments posted by prior automation. Plan-only default; delete requires `--apply --confirm-delete-inventory-adjustments`. | `code_scripts/qbo_inventory_remediation.py`, `docs/QBO_INVENTORY_REMEDIATION.md` | **Partially automated** | One-time (in batches) | After each batch, re-export QBO Inventory Shrinkage / Inventory Asset reports and compare before next batch. Do not run during sales or inventory sync. |
| B3.2 | **Adjust QBO starting quantities (manual)** | Use QBO UI "Adjust starting value" with `300100 - Opening Balance Equity` for items where quantity is wrong. Pipeline generates preview reports but does not post adjustments. | `docs/INVENTORY_SYNC.md` ("Use the preview outputs to perform QBO UI Adjust starting value corrections"), inventory_pipeline reports | **Manual** (with automated preview) | As needed per inventory audit | Automated apply was intentionally removed. Human uses pipeline audit CSV to determine correct values, then enters in QBO UI. |
| B3.3 | **Review inventory audit reports** | Read pipeline-generated audit CSVs and JSON summaries. Identify `missing_from_qbo`, `needs_adjustment`, `blocked` items. Decide action for each. | `code_scripts/inventory_pipeline.py`, portal Runs → Inventory tab | **Manual** | After each inventory sync run | Reports are in `runtime/code_scripts/reports/inventory_sync/` and `inventory_pipeline/`. |
| B3.4 | **Provide fresh EPOS stock snapshot** | Download a dated EPOS stock export for mapping refresh and month-end closing value. | `AGENTS.md` (Workstream D) | **Manual** (human) | ASAP + monthly | Used for mapping rebuild and COGS formula closing value. Pipeline can auto-download, but human may need to pull specific reports from EPOS admin. **⚠ NEEDS HUMAN CONFIRMATION:** Who pulls the EPOS stock snapshot — Marvin, site staff, or is it the pipeline's auto-download? Does EPOS admin have reports the pipeline can't reach (e.g. Stock History month-end, Product List)? |
| B3.5 | **Confirm past bill payment details** | Example: Bill `66251` (vehicle) — confirm the payment was a recording of a past payment, not a new cash movement. Confirm date and account. | `AGENTS.md` ("Confirm that payment was an approved recording") | **Manual** | One-time | Staff must confirm to the agent/accountant. |

### B4. Missing Sales Backfill

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B4.1 | **Backfill missing sales from EPOS (Jun–Sep 2026)** | BookKeeping data 12 Jun–15 Sep shows ₦366M TOTAL of EPOS sales missing from QBO. These should be backfilled as **Sales Receipts**, not COGS journals. | `AGENTS.md` ("backfill as sales, not a COGS journal, and only when matched"), `outputs/akponora_bookkeeping_2026-09-16/` | **Partially automated** (pipeline can upload; human must identify specific date ranges) | One-time (in date-range batches) | Must verify each batch against EPOS to avoid duplicates. Use `run_pipeline_custom.py --from-date --to-date` after confirming which dates are missing. |

### B5. Transfer Cleanup

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B5.1 | **Audit existing Transfers** | Review Transfers in QBO for correctness. Example: Transfer `68049` (₦58.6M, Moniepoint → Inventory) was deleted. Transfer `68091` (same amount, Moniepoint → Undeposited Funds) remains — verify it's correct. | `AGENTS.md` ("Transfer 68049 has been deleted… Leave 68091") | **Manual** | One-time | Incorrect transfers inflate or deflate account balances. |

### B6. Company B Specific (Uncertain)

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| B6.1 | **Company B QBO operational scope** | AGENTS.md says "Company B (Goldplates) is out of scope unless asked." This implies Company B has separate QBO needs that may or may not mirror Company A's. | `AGENTS.md`, `company_b.json` | **⚠ NEEDS HUMAN CONFIRMATION** | N/A | **Question: Does Company B have the same human QBO workflows as Company A (bills, bill payments, bank deposits, etc.)? Or is Company B simpler — just automated sales upload with minimal manual QBO work? Does Company B have a separate bookkeeper?** |
| B6.2 | **Company B inventory / COGS concerns** | Company B is non-inventory (`enable_inventory_items: false`). No COGS remediation is documented for it. | `company_b.json` | **⚠ NEEDS HUMAN CONFIRMATION** | N/A | **Question: Is Company B's COGS just whatever QBO calculates from bills/expenses, with no EPOS-based inventory matching? Are there any Company B QBO problems that need human attention?** |

---

## C. Ongoing Maintenance / Infrastructure

### C1. OAuth / Authentication

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| C1.1 | **Refresh QBO OAuth tokens** | Refresh tokens expire ~100 days. When expired, human must re-authenticate via Intuit Developer Portal and update `qbo_tokens.json`. | `qbo_auth.py`, README §"Token Refresh", portal QuickBooks Connections page | **Manual** (with automated detection) | Every ~90 days (proactively) | Pipeline auto-refreshes access tokens (60-min expiry) but cannot auto-refresh expired refresh tokens. Slack failure notification will alert on token expiry. Token broker mode (Windows) can help but still needs initial auth. |
| C1.2 | **Monitor token health on portal** | Check QuickBooks Connections page in the OIAT Portal for token status per company. | Portal → QuickBooks Connections | **Manual** | Weekly | Portal shows token metadata, last refreshed timestamp, and health status. |
| C1.3 | **Maintain EPOS credentials** | If EPOS Now password changes, update `.env` file with new `EPOS_USERNAME` / `EPOS_PASSWORD`. | `.env`, `epos_playwright.py` | **Manual** | As needed | Pipeline will fail on Phase 1 if credentials are wrong. Slack notification alerts. |

### C2. QBO Webhooks

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| C2.1 | **Monitor QBO webhook Slack notifications** | QBO webhooks notify on Item changes, Bill changes, etc. via Slack. Review for unexpected changes. | `apps/epos_qbo/webhooks.py`, `apps/epos_qbo/services/qbo_webhook_notifications.py` | **Manual** | Ongoing | Webhooks are passive alerts. Human decides whether action is needed (e.g., someone created a wrong-type item). |
| C2.2 | **Maintain Cloudflare Tunnel for webhook ingress** | Ensure the `cloudflared` service is running so Intuit can reach the webhook endpoint. | `docker-compose.yml` (cloudflared service), PR #54 | **Manual** (infrastructure) | Check after deployments | If tunnel goes down, QBO events stop flowing. Check `CLOUDFLARE_TUNNEL_TOKEN` env var. |

### C3. Environment & Scheduling

| # | Task | Description | Where it shows up | Status | Trigger / Cadence | Notes |
|---|------|-------------|-------------------|--------|-------------------|-------|
| C3.1 | **Verify Windows Task Scheduler / Docker scheduler** | Confirm the daily pipeline schedule is firing correctly. Check logs for missed runs. | `run_pipeline.cmd`, `docker-compose.yml` (scheduler service) | **Manual** | Weekly | If the scheduled task stops, sales won't be uploaded until someone notices and runs manually. |
| C3.2 | **Monitor disk space for artifacts** | Pipeline archives files locally. Over time, `Uploaded/`, `reports/`, and `logs/` can fill up disk. | `docs/ARTIFACT_RETENTION_PLAN.md` | **Manual** | Monthly | Retention plan exists but no automated janitor yet. Manual cleanup of old files needed. |
| C3.3 | **Keep Playwright browser updated** | Playwright's bundled Chromium must be kept in sync with EPOS Now's website. | `requirements.txt` (playwright), `playwright install chromium` | **Manual** | When EPOS login fails | If EPOS updates their website, Playwright selectors may break. |

---

## D. Identified Gaps (Recommended-but-Missing)

These are tasks that should exist based on the business operations but have no
documentation, script, or defined process today.

| # | Gap | Why it matters | Suggested fix |
|---|-----|----------------|---------------|
| D1 | **Undeposited Funds → Bank Deposit process** | The most common daily human task. No documented procedure, no checklist, no matching guidance. | Write a short SOP: how to match Sales Receipts in Undeposited Funds to bank statement deposits, grouped by payment method and date. |
| D2 | **Month-end close checklist** | No documented checklist for closing a month in QBO. Critical for accurate financials. | Create a checklist covering: all sales uploaded, all bills entered, bank rec done, Undeposited Funds cleared, depreciation posted, payroll posted, period locked. |
| D3 | **New location / payment method onboarding** | When a new restaurant location opens or a new tender type appears in EPOS, there's no procedure for adding the corresponding QBO Department or Payment Method. | Document the steps: create Department in QBO, update `department_mapping` in company JSON, verify pipeline picks it up. Same for Payment Methods. |
| D4 | **Bill posting account guard for Company A** | During COGS remediation, new bills should not go to Inventory items. No enforcement exists. | Either add a QBO rule (if possible) or brief the bookkeeper explicitly. Consider a webhook-based alert when a Bill is posted to an Inventory account. |
| D5 | **Cost price review alerts** | When EPOS cost is lower than QBO cost, the system should alert for review. Currently not implemented. | See `docs/BUSINESS_LOGIC.md` "Proposed Follow-up for Lower Cost Review" for the recommended implementation. |
| D6 | **Automated bank feed matching** | QBO bank feeds (if connected) could auto-match deposits, but this isn't set up or documented. | Evaluate connecting QBO bank feeds for the primary bank accounts. Would reduce A3.1 manual work. |
| D7 | **Stale / orphan item cleanup** | Auto-created items accumulate (4,315+ active Inventory items for Company A). No process to review, merge, or clean up. | Per AGENTS.md: do not bulk inactivate. But a periodic review of recently auto-created items would catch miscategorization early. |
| D8 | **Backup QBO data** | No documented backup process for QBO data. | QBO data is cloud-hosted by Intuit, but periodic exports (Trial Balance, Item List, Chart of Accounts) provide a safety net. |
| D9 | **VAT return filing process** | No documented process for compiling and filing VAT/WHT returns from QBO data. | Document which reports to pull, filing deadlines, and who files. |
| D10 | **Payroll recording** | No payroll process documented. If payroll is external, summary journal entries should be posted. | Document whether payroll is in QBO, external, or not tracked. Add to month-end checklist. |
| D11 | **Purchase Order workflow** | QBO webhooks track `PurchaseOrder` entity changes but no repo script or doc references POs. | **⚠ NEEDS HUMAN CONFIRMATION:** Do either company use QBO Purchase Orders before creating Bills? Or are Bills entered directly from vendor invoices without a PO step? |
| D12 | **Estimates / Quotes** | QBO webhooks track `Estimate` entity changes. No repo evidence of estimates being used. | **⚠ NEEDS HUMAN CONFIRMATION:** Does either company create Estimates/Quotes in QBO for customers? |
| D13 | **Inter-company transactions** | Company A and Company B are separate QBO realms. If there are transactions between them (shared purchases, inter-company loans, shared staff), these need recording in both QBOs. | **⚠ NEEDS HUMAN CONFIRMATION:** Are there inter-company transactions between Akponora and Goldplates that need to be recorded? |
| D14 | **Rent / lease payments** | Restaurants typically have significant rent obligations. No rent or lease transactions are mentioned in the repo. | **⚠ NEEDS HUMAN CONFIRMATION:** How is rent recorded — as a recurring Bill, a direct expense, or a journal entry? |
| D15 | **Petty cash tracking** | Restaurants commonly use petty cash for small daily purchases. No petty cash process is documented. | **⚠ NEEDS HUMAN CONFIRMATION:** Is there a petty cash fund? If so, are petty cash purchases recorded in QBO? |

---

## E. Inventory Tracking: Inventory vs Non-Inventory Assumption

Per the user's stated preference, **Inventory tracking should be kept in QBO
long-term** for Company A. The Non-inventory approach described in `AGENTS.md` is
a **temporary bridge** to stop the COGS problem while the cutover is completed.

**Current state and implications for human workflows:**

| Aspect | Current reality | Human workflow impact |
|--------|----------------|----------------------|
| Legacy Inventory items (4,315 active) | Frozen — must not be edited, inactivated, or deleted | Human must not touch these. Sales pipeline is blocked from using them (guard in code). |
| New sales items | Currently not uploading for Company A (pipeline blocked until conversion enabled) OR using legacy items pre-block | Once conversion is enabled, new items will be Non-inventory (temporary) or mapped to approved items. |
| Catch-all item (`AKP-UNMAPPED-EPOS-SALES`) | Created in QBO but not wired into pipeline | Once wired, unmapped sales flow here. Human must periodically review catch-all revenue and map products properly. |
| Future Inventory rebuild | Optional later project: new Inventory items, Oct 1 start qty, purchase-before-sale | Would restore full perpetual inventory in QBO. Requires clean opening balances and purchase flow. All current manual inventory-adjustment workflows would change. |
| Company B | Non-inventory / Service items only | No inventory tracking in QBO. Simpler human workflows — just sales + bills + deposits. |

**If Inventory tracking is restored (future):**
- Human must ensure purchase Bills are posted **before** related sales (QBO FIFO requires stock on hand).
- Human must set correct opening quantities for each item.
- Inventory audits (EPOS vs QBO quantity) become a regular human review task.
- The existing inventory pipeline audit reports become the primary tool for monitoring discrepancies.

---

## Quick Reference: Daily Human Checklist

For a typical operating day (after automation runs):

1. **Check Slack** for pipeline success/failure notifications (A1.4)
2. **Review reconciliation** results if mismatch flagged (A1.2)
3. **Create Bank Deposits** — move Undeposited Funds to correct bank accounts (A3.1)
4. **Enter Bills** for any vendor invoices received today (A2.1)
5. **Record Bill Payments** for any bills paid today (A2.2)
6. **Record non-sales deposits** or transfers if applicable (A3.3, A3.4)

Monthly additions:
7. **Bank reconciliation** (A3.2)
8. **Review P&L and Balance Sheet** (A7.3)
9. **Post depreciation and payroll journals** (A7.5, A7.6)
10. **Clear Undeposited Funds** — balance should be near zero (A7.7)
11. **Close books** for the prior month (A7.2)
12. **File VAT returns** (A6.2)

---

---

## F. Confirmation Questions for Marvin

These are items where the repo gives evidence a task *probably* exists but I
cannot verify from code alone. Each needs a yes/no (or short) answer.
Ref numbers link back to the full description above.

### Critical (affect accuracy of the whole inventory)

| Ref | Question |
|-----|----------|
| A1.1 | Is the daily sales pipeline actually running unattended for both companies right now? Is Company A upload still paused (blocked until conversion mode enabled per AGENTS.md)? |
| A3.1 | Is anyone transferring funds from Undeposited Funds to bank accounts today? How often — daily, weekly, monthly, or not at all? Is the balance backlogged? |
| A3.2 | Is bank reconciliation being done at all? By whom? How far behind? |
| A7.1 | Is there any month-end close process being followed today, even informally? Who is responsible? |

### Important (would add/remove tasks from the checklist)

| Ref | Question |
|-----|----------|
| A2.1 | Is it the same bookkeeper for both companies? Are bills entered daily, weekly, or in batches? |
| A2.4 | Has the bookkeeper actually been told to stop posting bills against Inventory items? Or is this still happening? |
| A2.5 | Do people record direct expenses (not Bills) in QBO — e.g. cash market purchases, petty cash? |
| A2.6 | Do you receive vendor credits or return goods to suppliers? Who records them in QBO? |
| A3.3 | How many bank accounts does each company have in QBO? Are inter-bank transfers regular? |
| A3.4 | Do the companies receive non-sales income (owner contributions, loans)? Who records them? |
| A3.5 | Do the restaurants issue customer refunds? Recorded in QBO or just EPOS? |
| A4.5 | Roughly how many active vendors per company? Are new vendors added regularly? |
| A4.6 | Is the invoice import workflow (A5) actively used, or was it a one-off? |
| A5.5 | Are customer credit memos ever issued? |
| A6.2 | Is VAT/WHT filing being done? By whom? Monthly or quarterly? |
| A7.5 | Does either company have depreciable fixed assets? Is depreciation being posted? |
| A7.6 | How is payroll handled — QBO Payroll, external, or not tracked? |
| A7.8 | Are any accruals or prepayments recorded in QBO? |
| A7.9 | Do owners take draws from business accounts? Are these tracked in QBO? |

### Nice-to-know (fill in gaps and edge cases)

| Ref | Question |
|-----|----------|
| B3.4 | Who pulls the EPOS stock snapshot? Are there EPOS admin reports the pipeline can't auto-download? |
| B6.1 | Does Company B have the same human QBO workflows as Company A, or is it simpler? Separate bookkeeper? |
| B6.2 | Does Company B have any QBO problems or manual work needs beyond what the pipeline automates? |
| D11 | Do either company use QBO Purchase Orders? |
| D12 | Does either company create Estimates/Quotes in QBO? |
| D13 | Are there inter-company transactions between Akponora and Goldplates? |
| D14 | How is rent recorded — recurring Bill, direct expense, or journal entry? |
| D15 | Is there a petty cash fund? Are small purchases tracked in QBO? |

---

*Last updated: September 2026. Source: repo analysis of `OIAT-Solutions/code-scripts` main branch,
`AGENTS.md`, `docs/BUSINESS_LOGIC.md`, `docs/INVENTORY_SYNC.md`, `docs/QBO_INVENTORY_REMEDIATION.md`,
company configs, PR history (#28, #29, #46, #50, #51, #54), and script code review.*

# AKPONORA operations controls and automation boundaries

19 September 2026. Company A only. Status: implemented offline foundations and sales safety gates; integration of new bank/AP workflows remains future work. No scheduled automation or posting approval was created.

## Operational review

| Area | Existing implementation / evidence | Control and next integration |
|---|---|---|
| EPOS sales retrieval | `code_scripts/epos_playwright.py`; `run_pipeline.py` downloads, splits Lagos trading days, spills future rows and archives source files | Preserve raw bytes and hash, timestamp, source line IDs, report control totals and date window. Never silently drop errors or count overlapping downloads twice. No live login/download was run in this task. |
| Sales transform | `transform.py`, `product_conversion.py` | Explicit approved sale multiplier; source proof survives aggregation. Unknown IDs do not fall back to names/barcodes. Monetary totals preserved. Invalid source dates and nonfinite numbers fail. |
| QBO upload | `qbo_upload.py` | Entire conversion day preflight; exact live Item Id/type/SKU/date/account, history cutoff, no create/patch/bypass. From 1 Oct only: approved payload hashes, proposal queue, and a requestid that stays stable for uncertain retries but changes after a confirmed 4xx failure. Pre-October catch-all history posts as before (no manifest). See the runbook section "Pre-October vs October behaviour". |
| Existing-document detection | `check_qbo_existing_docnumbers`, local uploaded DocNumber ledger | For October receipts, conversion preflight compares existing receipt date, document, deposit/payment references, exact item quantities/amounts and VAT before skipping. Pre-October receipts keep the legacy harmless skip. Raw source-ID reconciliation remains required; `reconcile_documents` is an offline comparison primitive. Do not heal/suppress a materially changed source day solely because DocNumber exists. |
| Posting/reconciliation | `run_pipeline.reconcile_company` records totals/counts; portal artifact ingestion displays status | For October-or-later dates, a failed/not-run Company A reconciliation (or a failed receipt POST) writes a persistent posting hold before archive. Later October posts block until reviewed and cleared with `python -m code_scripts.operations_controls clear-hold --approved-by NAME --reason TEXT` (the hold file is archived, not deleted). Pre-October days never write the hold. |
| Inventory retrieval/review | `epos_stocklevels_playwright.py`, `inventory_sync.py`, `inventory_pipeline.py` | Report quantities, missing items and cost/unit exceptions only. Quantity-apply and catalog-cleanup apply removed. Review-create ultimately calls Company A-refused Inventory creator. Never use legacy matching stock corrections. |
| Purchases | `scripts/bills/qbo_import_bills.py` is a legacy re-importer, resolves item-based lines by **name**, ignoring CSV ItemId | Not suitable for new cutover item-based purchase automation. New proposed Bill must use exact canonical Id, Qty and UnitPrice. `purchase_units` supports units/cost math; compiler below prepares safe item-based drafts. Legacy importer is not approved for Company A cutover. |
| Suppliers / AP | Bills export/import utilities; no automatic supplier-payment matching integration found in reviewed code | Match vendor Id + invoice number/date/currency/amount + receipt evidence + duplicate search. Three-way PO/GRN/invoice match. Partial deliveries, credits, deposits and disputed balances remain separate. Supplier approval needed for new vendors or changed banking details. |
| Bill payments | Historical Bill 66251 payment recorded in plan; no new payment authorized | Query Bill balance and linked BillPayments immediately before proposal. Require real source bank/cash account, vendor, payment date, evidence/reference, exact applied bills/credits and remaining balance. Never infer payment from goods received or EPOS sale. |
| Undeposited Funds | Company A receipt destination configured as `100900 - Undeposited Funds` | Reconcile by tender/location/day. Match POS/Moniepoint settlement references to independent bank statement entries. Gross receipts less refunds/fees/withholding equals net settlement, with timing differences carried explicitly. `settlement_match` produces no bank posting. Leave Transfer 68091 untouched; do not re-create deleted 68049. |
| VAT | Current configured inclusive 7.5% and code 2; gross/net/VAT tests | Configuration is not evidence every item is taxable. Bookkeeper must verify exemptions, input recovery and cost tax basis. Invoice-backed purchase tax, sale refunds and rounding reconcile to VAT control/returns; no blanket cost division by 1.075 without tax approval. |
| Month end | Historical Jan–Aug COGS corrections; September pending | Sep stock-movement-only if purchases expensed; October FIFO primary. New-item valuation vs GL vs physical stock; only verified variance journal. AP/bank/VAT remain distinct reconciliations. |
| Audit / idempotency | Existing job locks, run artifacts and DocNumber ledger | New `ProposalQueue` uses realm/entity/stable source key and immutable payload digest; repeats do not create proposals twice. A changed payload blocks unless the earlier attempt is confirmed FAILED, or is reset with `operations_controls mark-failed` after a live read proves nothing was posted. Audit stores source evidence references and confirmed QBO IDs / failed / unknown outcomes for Company A sales. SQLite access control/backup is required. It is not a deployed multi-user approval system. |

## Automatic read/reconcile/flag lane

Safe future schedule: download/export sources, validate date completeness and schema, hash/preserve bytes, compare changed Product IDs/SKUs/master links, recalculate canonical stock, detect missing costs/negatives, compare source/QBO documents and VAT, reconcile AP/settlements, prepare exceptions and immutable proposals. No financial mutation, email/Slack send, product rename or account change follows automatically from a flag. Human notification configuration is separate.

Daily alert record should contain company/realm, business date, severity, stable product/document identity, evidence path/hash, expected and actual values, specific reason, owner and next action. Block on: unmapped October sale; changed master/quantity multiplier; missing live item; SKU/type/date/account drift; missing/duplicate day; negative canonical stock after purchases; source/QBO totals or VAT mismatch; unknown posting outcome. Flag and assign: zero cost with stock, zero-floor negatives, stock adjustments, AP ageing, unreceived PO, fees/timing discrepancies.

## Proposed-write lane

| Proposal | Required evidence before approval |
|---|---|
| SalesReceipt/backfill | Complete source day, unique source IDs or preserved row ledger, mapping hash, exact live items, gross/net/VAT/count tie, live existing-receipt search, exact payload hashes |
| Inventory create | Approved family/stock owner/unit relationships, cost tax basis, cutoff count, unique names/SKUs, new-ID register, InvStartDate/IA/account/tax validations, paired offset calculation |
| Supplier Bill | Invoice + GRN + vendor identity, duplicate search, item purchase-unit conversions, VAT/accounts, payable total and date |
| BillPayment | Bank/cash statement evidence, exact payment account and vendor, bill balance/linked-payment refresh, applied amounts, remaining balance, no duplicates |
| Deposit/Transfer | Actual independent bank settlement/transfer evidence, source/destination account IDs, references, fee/tax/timing breakdown. EPOS alone insufficient. |
| Credit/refund | Original receipt/bill links, physical return/restock decision, canonical units, revenue/VAT reversal, payment evidence |
| Month-end journal | Signed reconciliation, exact debit/credit accounts/date/amount, no duplicate FIFO COGS, closed-period review |

An approval is bound to one realm, entity, exact immutable payload digest(s), approver/reference and expiry. Changing any financial field invalidates approval. Re-read target balances/SyncTokens before posting. After uncertain response, query by stable identifiers; do not resubmit with a new idempotency key. Capture Intuit transaction/request IDs, reconcile totals, then advance a durable source cursor. Failed or ambiguous batches stop subsequent posting; automatic deletion or compensation is forbidden.

## Next engineering integrations (not activation)

1. Extend the implemented receipt-content comparison and reconciliation hold with source transaction/line-ID cursors. Test partial-day late arrivals and tender ordering: current legacy DocNumbers depend on tender encounter order, so changed input order can block on content mismatch. Do not regenerate IDs blindly.
2. Read-only supplier/statement ingestion and matching using agreed export schemas; turn matches into reviewable proposals, never infer a payment.
3. Authenticated approval queue with role separation, immutable evidence retention and expiring approvals. Keep runtime write credentials out of reporting workers.
4. Alert delivery only after an explicit messaging/scheduling instruction. No monitor was created by this task.

## Final acceptance sign-off

Record code revision, mapping digest, cutoff pack hash, canonical family count, approved variant count, quantity/value total, final exception decisions, receipt gross/net/VAT/count reconciliation, AP/bank/VAT owners, pre-create IA, actual create increase, offset journal Id, post-offset IA and new-item subledger, first October receipt Ids and FIFO COGS, duplicate replay result, server/schedule state and approving chat references. Every unverified field stays blank and blocks its dependent action.

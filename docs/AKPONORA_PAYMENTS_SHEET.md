# Nora Mart Payments workbook

Owner decisions (10 Oct 2026): one Google workbook; staff enter **one row per payment** (a part payment, or a
payment into two accounts, is another row); payments post automatically; every account is listed, no "Other".

Tabs (headers are a contract with `code_scripts/akponora_ops/payments_sheet.py`; change both together):

| Tab | Filled by | Headers |
|---|---|---|
| Credit sales | system | Invoice No, Invoice Date, Customer, Total, Paid, Balance, Status, Last Updated |
| Credit payments | staff A–G, system H–J | Invoice No, Amount Received, Date Received, Received Into, Reference, Entered By, Notes, Status, QuickBooks Payment No, Processed At |
| Bills to pay | system | Bill No, Bill Date, Supplier, Total, Paid, Balance, Days Outstanding, Last Updated |
| Bill payments | staff A–G, system H–J | Bill No, Amount Paid, Date Paid, Paid From, Reference, Entered By, Notes, Status, QuickBooks Payment No, Processed At |
| Lists | system (B, C); A fixed | Account, Open Credit Invoices, Open Bills |

Accounts (Lists!A): Petty Cash 100100, Moniepoint 100201–100207, Zenith 100301. The 6-digit number in the label
picks the QBO bank account.

Nightly (`daily_run` step `payments`, after banking): refresh system tabs; for each new payment row check it
(open invoice/bill, listed account, amount > 0 and ≤ balance left, real date not in the future, not before the
document, not in a closed period), then post a ReceivePayment / BillPayment into or from that account with a
stable Intuit requestid, re-read the balance, and write Status ("Posted" / "Held: reason" / "Waiting for
approval" over the cap) back. A row posts once (`STATE_ROOT/ops/company_a/payments_sheet/posted.json` and the
row key in the QBO memo); a row edited after posting is reported, never re-posted.

Settings (env or `STATE_ROOT/ops/company_a/payments_sheet/settings.env`): `OIAT_COMPANY_A_PAYMENTS_SHEET_ID`
(step off when blank), `OIAT_COMPANY_A_PAYMENTS_POST=1` (else checks only), `OIAT_COMPANY_A_PAYMENTS_CAP`
(default 2,000,000). Google access: the till sheet's service account
`oiat-sheets-reader@oiat-ops.iam.gserviceaccount.com` (Editor on the workbook).

Keep credit repayments out of the till sales breakdown: they are not sales, and counting them there would show
up as till overage on banking.

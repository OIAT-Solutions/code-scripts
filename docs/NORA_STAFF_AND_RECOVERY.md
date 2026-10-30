# Nora staff practice and recovery

Read AGENTS.md and the active process-hardening checkpoint first. These changes preserve financial approval identities and limits. Never test by replaying a real payment or running a financial backfill.

## Simple daily rules

- EPOS tells us what was sold, received or changed in stock. Received POs cannot change; a later delivery needs a new PO. We still refresh the PO list and check its identity before reusing saved details.
- A PO creates an **unpaid bill**. `MODE OF PAYMENT: CASH` is a cash-method hint; it does not prove a payment happened. Staff record actual supplier payments in **Bill payments**, with the real payment date, amount and account.
- Customer repayments go in **Credit/Invoice payments**. A restored EPOS credit limit is not payment evidence. Repayments must stay outside the daily sales figure.
- Staff continue to use the existing Daily Till Sales Breakdown, Bill payments and Credit/Invoice payments sheets. Payment column K is the system's permanent Payment ID. Leave it alone, include it when sorting whole rows, and never copy it to a new payment. Columns A:J keep their current meaning.
- Record a real second partial payment on a new row, even if its amount is identical. Once Posted, use a new correction/review request; do not change or erase the original row.
- Count changes and deliveries can look similar. Unexplained/New Stock adjustments wait for a PO review. A positive count adjustment matching a nearby PO also waits. Stock Take/Wastage within the existing limit can follow their existing approval gate; a whole transfer's absolute value counts toward the limit.

## Practice before November

Use a COPY of the workbook with posting disabled. OIAT must verify the copied workbook ID, Company A test/fake client and disabled posting before practice. Never paste synthetic practice rows into the production workbook.

Practice these cases and check the expected result:

| Case | Expected result |
| --- | --- |
| One supplier bill, payment smaller than balance | One partial payment; remaining balance decreases by that amount |
| Two separate equal partial payments | Two different Payment IDs; both count once |
| Sort rows including column K, then refresh | No extra payment |
| Copy an existing Payment ID | Stop and request review before posting |
| Change a Posted row or clear its Posted mark | Show edit/review; no second payment |
| Wrong account, missing date, amount greater than balance | Hold; no posting |
| Customer repayment plus a till overage on the same day | Review possible double counting before banking sales |
| A lost response after a payment/stock adjustment was accepted | Recover the original matching transaction; never blindly send another |
| A positive Stock Take matching a PO | Hold for delivery review |
| Older stock record edited | Fresh detail check exposes it; posted event is not posted again |

OIAT's synthetic regression cases are in `code_scripts/tests/test_process_hardening.py`; run with `python -m unittest code_scripts.tests.test_process_hardening -q`. Passing code tests does not mean staff practice or opening balances have been signed off.

Before real November entries, capture current QBO outstanding balances on the SERVER:

`python -m code_scripts.akponora_ops.onboarding --out /data/ops/company_a/onboarding/outstanding_review.json`

Compare every outstanding invoice/bill with actual payment records and statements. Resolve unrecorded payments through the existing reviewed process. Owner/bookkeeper must sign the dated evidence separately. Re-capture at the November opening cutoff; a 24 October snapshot is not a 1 November opening balance. Never assume an EPOS PO's old payment hint is a paid bill.

## Cash and bank evidence

The routine reports missing evidence honestly. An optional server input at `/data/ops/company_a/money_evidence/YYYY-MM-DD.json` can contain:

```json
{
  "cash": {
    "date": "YYYY-MM-DD", "counted_by": "staff name",
    "opening_float": "0", "cash_sales": "0", "cash_repayments": "0",
    "supplier_cash_payments": "0", "cash_banked": "0",
    "other_approved_outflows": "0", "closing_count": "0"
  },
  "banks": [{
    "date": "YYYY-MM-DD", "account_id": "actual QBO account ID",
    "statement_balance": "actual balance", "book_balance": "actual QBO balance",
    "verified_outstanding_net": "verified signed reconciling items; explicitly 0 if none",
    "statement_reference": "retained statement reference", "checked_by": "reviewer"
  }]
}
```

The zeroes above illustrate fields, not actual counts. Supply every field from real evidence. Cash expected = opening float + cash sales + cash repayments − supplier cash payments − cash banked − other approved outflows. Bank difference = statement balance + verified outstanding net − book balance. A matching calculation checks the supplied numbers; it does not authenticate a statement or prove a physical count happened.

## Recovery and backup

Financial operations retain their existing request IDs and live duplicate checks. New durable work records retain uncertain payment/stock outcomes. If a process stops after sending, reconcile the original reference, date, amount, account and lines. Zero or multiple matches stay on hold. Never clear the work database or ledger to force a retry.

JSON completion files replace atomically. Work SQLite uses durable transactions and keeps first-seen age. Read-only collectors have deadlines and fresh heartbeats; a financial writer is never killed merely to meet a time target.

Protected local backup configuration lives in `/data/ops/company_a/recovery/config.json`:

```json
{"destination":"/data/nora_backups","key_file":"/data/recovery_keys/company_a.key","daily":30,"monthly":12}
```

Create a key once using `python -m code_scripts.akponora_ops.ops_backup init-key --key-file /data/recovery_keys/company_a.key`. Never print, commit or overwrite it. Keep a separately protected recovery copy with the approved key custodian. A key and backup only on the same server do not protect against losing that server.

The daily routine takes consistent SQLite copies, encrypts changed files into immutable AES-GCM blobs, writes an encrypted complete index, restores into an isolated temporary folder and checks hashes/SQLite integrity. Retention keeps recent daily and monthly recovery points; blobs referenced by retained indexes survive. Archives, mappings, operation evidence and shared portal/token databases are included; shared databases are opaque infrastructure copies, not Company B accounting work. Host `.env`, container images and external service configuration require separately protected retention.

Restore only into an EMPTY isolated folder:

`python -m code_scripts.akponora_ops.ops_backup restore --key-file /secure/recovery.key --manifest /backups/KEY_NAMESPACE/manifests/SET.nora.enc --destination /isolated/restore`

Never point restore at `/data` or a live database. Investigate with the restored copy; switching production to restored financial state needs reconciliation and explicit approval because QBO may already contain writes made after that recovery point.

`offsite_destination` may name an approved mounted destination. Only ciphertext is copied and checked; the key is never copied there. Offsite location, access, separate key custody and a restore from that destination must be accepted before claiming offsite recovery. The owner's destination/key-custodian decision is pending.

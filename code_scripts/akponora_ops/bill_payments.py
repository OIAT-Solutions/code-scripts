"""Pay company_a bills that EPOS says were paid in cash on delivery (part of the bills step).

Owner decision (Marvin, chat yes 4 Oct 2026, "A"): a bill whose EPOS PO says MODE OF PAYMENT: CASH
is paid from Petty Cash (``100100``, QBO Id 29) on the bill's own date, so it does not sit in Accounts
Payable. Bills paid by TRANSFER (or with no payment mode) stay open: bank reconciliation matches them to
the Moniepoint statement and pays them from the right account on the right date.

Which bills: ``Bill`` from ``FROM_DAY`` (2026-10-01) with a balance, DocNumber ``EPOS-PO-<ref>`` (made by
``bills_sync``) and the memo ``Payment hint: CASH`` that ``bills_sync`` writes from the EPOS PO note.
That also catches up cash bills posted before this existed. Idempotent: a paid bill has balance 0 and is
never picked again; each payment has a stable requestid. Every payment is re-read (the bill must be at
balance 0). Caps per run: ``OIAT_COMPANY_A_BILL_CASH_PAY_MAX`` payments (30) and
``OIAT_COMPANY_A_BILL_CASH_PAY_MAX_AMOUNT`` per bill (N2,000,000). ``OIAT_COMPANY_A_BILL_CASH_PAY=0``
turns it off.
"""
from __future__ import annotations

import os
from decimal import Decimal

from code_scripts.akponora_ops.common import REALM
from code_scripts.scripts.akponora_cutover.w7_create_items import sha256_text

FROM_DAY = "2026-10-01"
PETTY_CASH_ID = "29"  # 100100 - Petty Cash
CASH_HINT = "Payment hint: CASH"
DOC_PREFIX = "EPOS-PO-"
APPROVAL_REF = "Marvin chat yes 2026-10-04 (A: cash-on-delivery POs paid from Petty Cash)"
ENABLED_ENV = "OIAT_COMPANY_A_BILL_CASH_PAY"
MAX_ENV = "OIAT_COMPANY_A_BILL_CASH_PAY_MAX"
MAX_AMOUNT_ENV = "OIAT_COMPANY_A_BILL_CASH_PAY_MAX_AMOUNT"


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENABLED_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"}


def cash_bills(bills: list[dict]) -> list[dict]:
    """Open bills_sync bills from FROM_DAY whose EPOS PO was paid in cash, oldest first."""
    out = []
    for b in bills:
        if (str(b.get("TxnDate") or "") >= FROM_DAY and Decimal(str(b.get("Balance") or 0)) > 0
                and str(b.get("DocNumber") or "").startswith(DOC_PREFIX)
                and CASH_HINT in str(b.get("PrivateNote") or "")):
            out.append(b)
    return sorted(out, key=lambda b: (b.get("TxnDate") or "", str(b.get("DocNumber") or "")))


def payment_payload(bill: dict) -> dict:
    amount = float(Decimal(str(bill["Balance"])))
    po = str(bill.get("DocNumber") or "")[len(DOC_PREFIX):]
    return {
        "VendorRef": {"value": (bill.get("VendorRef") or {}).get("value")},
        "PayType": "Check",
        "CheckPayment": {"BankAccountRef": {"value": PETTY_CASH_ID}},
        "TxnDate": bill["TxnDate"],
        "TotalAmt": amount,
        "PrivateNote": (f"Cash paid on delivery (EPOS PO {po}: MODE OF PAYMENT CASH) | {bill.get('DocNumber')} | "
                        f"auto by bills_sync | approval {APPROVAL_REF}"),
        "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": bill["Id"], "TxnType": "Bill"}]}],
    }


def requestid(bill: dict) -> str:
    return sha256_text(f"bill_cash_pay|{REALM}|{bill['Id']}")[:36]


def pay_cash_bills(client, *, env=None, dry_run: bool = False) -> dict:
    """Pay every open cash-on-delivery bill (see the module doc). Returns counts and per-bill rows."""
    env = os.environ if env is None else env
    res = {"enabled": enabled(env), "paid": [], "planned": [], "capped": [], "failed": [], "total": "0.00"}
    if not res["enabled"]:
        return res
    cap_n = int(str(env.get(MAX_ENV) or "").strip() or 30)
    cap_amount = Decimal(str(env.get(MAX_AMOUNT_ENV) or "").strip() or "2000000")
    bills = cash_bills(client.query_all(f"select * from Bill where TxnDate >= '{FROM_DAY}'", "Bill"))
    total = Decimal(0)
    for b in bills:
        row = {"bill_id": b["Id"], "doc": b.get("DocNumber"), "date": b["TxnDate"], "amount": str(b["Balance"]),
               "vendor": (b.get("VendorRef") or {}).get("name", "")}
        if Decimal(str(b["Balance"])) > cap_amount or len(res["paid"]) + len(res["planned"]) >= cap_n:
            res["capped"].append(row)
            continue
        if dry_run:
            res["planned"].append(row)
            continue
        resp = client.post_json("/billpayment", payment_payload(b), requestid(b))
        live = client.get_json(f"/bill/{b['Id']}").get("Bill") or {}
        if Decimal(str(live.get("Balance") or 0)) != 0:
            detail = f"HTTP {getattr(resp, 'status_code', '?')}: {str(getattr(resp, 'text', ''))[:200]}"
            res["failed"].append({**row, "detail": f"bill still has balance {live.get('Balance')} ({detail})"})
            break  # stop on the first problem; the next run retries (balance > 0 means not paid)
        payment = (resp.json() or {}).get("BillPayment", {}) if getattr(resp, "status_code", 0) == 200 else {}
        res["paid"].append({**row, "payment_id": payment.get("Id", "")})
        total += Decimal(str(b["Balance"]))
    res["total"] = f"{total:.2f}"
    return res

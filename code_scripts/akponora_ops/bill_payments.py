"""Pay company_a bills that the EPOS PO says were already paid (part of the bills step).

Owner decision (Marvin, chat yes 4 Oct 2026, "A"): a bill whose EPOS PO says MODE OF PAYMENT: CASH
is paid from Petty Cash (``100100``, QBO Id 29) on the bill's own date, so it does not sit in Accounts
Payable. Bills paid by TRANSFER (or with no payment mode) stay open: bank reconciliation matches them to
the Moniepoint statement and pays them from the right account on the right date.

Staff convention (owner, 4 Oct 2026) read from the bill memo ``Payment hint: <MODE> [PAID|NOT PAID]
[from <digits>]`` that ``bills_sync`` writes from the PO note:
* CASH (not marked NOT PAID) -> paid from Petty Cash on the bill date;
* TRANSFER PAID from <digits> -> paid from the one active Bank account whose name contains those digits
  (e.g. MONIEPOINT 4686987227); no unique match -> left open;
* CREDIT / NOT PAID / anything else -> left open.

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
import re
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


_HINT_RE = re.compile(r"Payment hint: (?P<mode>[A-Z]+)(?P<paid> NOT PAID| PAID)?(?: from (?P<acct>\d{4,}))?")


def hint(bill: dict) -> dict:
    m = _HINT_RE.search(str(bill.get("PrivateNote") or ""))
    if not m:
        return {"mode": "", "paid": "", "account": ""}
    return {"mode": m.group("mode"), "paid": (m.group("paid") or "").strip(), "account": m.group("acct") or ""}


def pay_from(bill: dict, banks: list[dict]) -> str:
    """QBO account Id to pay this bill from, or '' to leave it open (see the module doc)."""
    h = hint(bill)
    if h["mode"] == "CASH" and h["paid"] != "NOT PAID":
        return PETTY_CASH_ID
    if h["mode"] == "TRANSFER" and h["paid"] == "PAID" and h["account"]:
        hits = [a for a in banks if h["account"] in re.sub(r"\D", "", str(a.get("Name") or ""))]
        return str(hits[0]["Id"]) if len(hits) == 1 else ""
    return ""


def cash_bills(bills: list[dict], banks: list[dict] = ()) -> list[dict]:
    """Open bills_sync bills from FROM_DAY that the PO says were paid, oldest first (key ``_pay_from``)."""
    out = []
    for b in bills:
        if (str(b.get("TxnDate") or "") >= FROM_DAY and Decimal(str(b.get("Balance") or 0)) > 0
                and str(b.get("DocNumber") or "").startswith(DOC_PREFIX)):
            acct = pay_from(b, list(banks))
            if acct:
                out.append({**b, "_pay_from": acct})
    return sorted(out, key=lambda b: (b.get("TxnDate") or "", str(b.get("DocNumber") or "")))


def payment_payload(bill: dict) -> dict:
    amount = float(Decimal(str(bill["Balance"])))
    po = str(bill.get("DocNumber") or "")[len(DOC_PREFIX):]
    return {
        "VendorRef": {"value": (bill.get("VendorRef") or {}).get("value")},
        "PayType": "Check",
        "CheckPayment": {"BankAccountRef": {"value": bill.get("_pay_from") or PETTY_CASH_ID}},
        "TxnDate": bill["TxnDate"],
        "TotalAmt": amount,
        "PrivateNote": (f"Paid on delivery per EPOS PO {po} ({str(bill.get('PrivateNote') or '').split(' (EPOS')[0]}) | "
                        f"{bill.get('DocNumber')} | auto by bills_sync | approval {APPROVAL_REF}"),
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
    banks = client.query_all("select * from Account where AccountType = 'Bank' and Active = true", "Account")
    bills = cash_bills(client.query_all(f"select * from Bill where TxnDate >= '{FROM_DAY}'", "Bill"), banks)
    total = Decimal(0)
    for b in bills:
        row = {"bill_id": b["Id"], "doc": b.get("DocNumber"), "date": b["TxnDate"], "amount": str(b["Balance"]),
               "vendor": (b.get("VendorRef") or {}).get("name", ""), "account": b["_pay_from"]}
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

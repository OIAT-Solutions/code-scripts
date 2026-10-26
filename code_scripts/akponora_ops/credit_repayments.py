"""EPOS customer account payments -> QBO Payments (company_a / Nora Mart).

Owner (Marvin, chat 26 Oct 2026): know from EPOS when a credit sale has been paid, not only from the staff
Payments workbook (which starts in November). Found in the EPOS back office (read-only):

* Reporting > Customer > **Customer Credit** (``CustomerCredits.aspx``, grid ``MainContent_gvCredit``): every
  payment taken onto a customer's account - customer, location, date/time, amount. Example: VERA AKPOREHA
  09/10/2026 14:19:15 N534,800.00 (that day's till sheet was N535,250 over EPOS sales).
* Classic reports > **Completed Transactions** (``CompleteTransactions.aspx``, grid ``MainContent_gvTransactions``):
  the same payment as a transaction with staff, till and **tender** (Transfer for Vera's).
* Reporting > Customer > **Customer Report**: each customer's CURRENT BALANCE (negative = owes), MAX CREDIT.

Each nightly run reads Customer Credit only from the last good read (owner, 26 Oct: EPOS is read once; a
payment never changes), finds each new payment's tender, keeps it in ``seen.json``, and plans one QBO Payment
per EPOS payment:

* Customer: the QBO customer with the same name (credit_invoices.resolve_customer rules); otherwise HOLD.
* Deposited to Undeposited Funds (``72``) like the till's SalesReceipts, so uf_deposits banks it with that
  day's till money instead of booking it as till overage. Tender is kept in the memo for the banking step.
* Applied to the customer's oldest open credit-sale invoices (``CR…``); any remainder stays as unapplied credit.
* Identity ``PaymentRefNum`` ``EPR-<key>`` and ``| epos-repay <key> |`` in the memo, key = hash of customer +
  EPOS date/time + amount. A payment already in QBO (by tag) or in the ledger is never posted again.
* HOLD (review, never posted): no tender found or a mixed tender; customer not exactly one QBO customer; the
  day is already banked by uf_deposits (a correction, e.g. Vera's 9 Oct payment, is done by hand with a yes);
  a closed period; before FROM_DAY. Over the cap (N2,000,000) waits for approval.
* ``OIAT_COMPANY_A_CREDIT_REPAYMENTS=post`` posts READY payments in the daily run; anything else (default) only
  plans, and ``post --plan-dir … --approval-ref … --expect-sha …`` posts an approved plan.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import COMPANY, dump_json, state_dir, write_csv

TOOL = "credit_repayments"
LAGOS = ZoneInfo("Africa/Lagos")
FROM_DAY = "2026-10-05"  # the Credit tender was switched on in EPOS on 5 Oct 2026
MODE_ENV, CAP_ENV = "OIAT_COMPANY_A_CREDIT_REPAYMENTS", "OIAT_COMPANY_A_CREDIT_REPAYMENT_MAX"
UF_ACCOUNT_ID = "72"
CREDITS_URL = "https://www.eposnowhq.com/Pages/Reporting/CustomerCredits.aspx"
TRANSACTIONS_URL = "https://www.eposnowhq.com/Pages/Reporting/CompleteTransactions.aspx"
CUSTOMERS_URL = "https://www.eposnowhq.com/Pages/Reporting/CustomerReport.aspx"
EPOS_TIME = "%d/%m/%Y %H:%M:%S"
TENDERS = {"cash": "Cash", "transfer": "Transfer", "card": "Card"}
READY, HOLD, WAIT, DONE = "READY", "HOLD", "WAIT", "DONE"
POSTED, FAILED = "POSTED", "FAILED"
TAG_RE = re.compile(r"\| epos-repay ([0-9a-f]{16}) \|")
RANGES = (7, 14, 30, 60, 90)  # Customer Credit "Last N days" options


class StopPost(RuntimeError):
    pass


# ---------------------------------------------------------------- small helpers
def money(value) -> Decimal | None:
    """'₦534,800.00' / '-₦623,149.99' / 534800 -> Decimal; None when not a number."""
    text = re.sub(r"[₦N,\s]", "", str(value if value is not None else ""))
    if not text:
        return None
    try:
        out = Decimal(text)
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def parse_when(text: str) -> datetime | None:
    try:
        return datetime.strptime(" ".join(str(text).split()), EPOS_TIME)
    except ValueError:
        return None


def norm_name(name: str) -> str:
    from code_scripts.akponora_ops.credit_invoices import norm_name as ci_norm

    return ci_norm(name)


def payment_key(customer: str, when: str, amount: Decimal) -> str:
    raw = f"{norm_name(customer)}|{' '.join(str(when).split())}|{amount:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def ref_number(key: str) -> str:
    return f"EPR-{key[:12]}"  # QBO PaymentRefNum is at most 21 characters


def tender_kind(text: str) -> str:
    """'transfer' / 'cash' / 'card' for a single tender, '' for blank or mixed (e.g. 'Cash/Transfer')."""
    parts = {p.strip().lower() for p in re.split(r"[/+,&]", str(text or "")) if p.strip()}
    parts = {"card" if p in ("pos", "card") else p for p in parts}
    return next(iter(parts)) if len(parts) == 1 and next(iter(parts)) in TENDERS else ""


def settings_path() -> Path:
    return state_dir(TOOL) / "settings.env"


def setting(env=None) -> str:
    """'off' / 'plan' / 'post': the environment wins, else ``<state>/credit_repayments/settings.env``
    (``OIAT_COMPANY_A_CREDIT_REPAYMENTS=plan``), so the owner can switch it without editing the server .env."""
    env = os.environ if env is None else env
    value = str(env.get(MODE_ENV, "")).strip().lower()
    if not value:
        try:
            for line in settings_path().read_text().splitlines():
                key, _, val = line.split("#", 1)[0].partition("=")
                if key.strip() == MODE_ENV:
                    value = val.strip().lower()
        except OSError:
            pass
    return value if value in ("plan", "post") else "off"


def mode(env=None) -> str:
    return "post" if setting(env) == "post" else "plan"


def cap(env=None) -> Decimal:
    env = os.environ if env is None else env
    return money(env.get(CAP_ENV)) or Decimal("2000000")


def ledger_path() -> Path:
    return state_dir(TOOL) / "posted.json"


def ledger() -> dict:
    p = ledger_path()
    return json.loads(p.read_text()) if p.exists() else {}


def seen_path() -> Path:
    return state_dir(TOOL) / "seen.json"


def seen() -> dict:
    """Payments already read from EPOS: ``{"through": day read up to, "payments": {key: payment}}``."""
    p = seen_path()
    return json.loads(p.read_text()) if p.exists() else {"through": "", "payments": {}}


def window_days(through: str, today: date) -> int:
    """The smallest Customer Credit range covering the day after ``through`` up to today (30 on a first read)."""
    if not through:
        return 30
    need = (today - date.fromisoformat(through)).days + 1
    return next((n for n in RANGES if n >= need), RANGES[-1])


def remember(store: dict, cap_data: dict, today: date) -> dict:
    """Keep every payment read; a tender found later replaces a missing one. Move ``through`` on only when
    the Customer Credit report itself was read (``credit_read``), so a failed night is read again."""
    for pay in cap_data.get("payments", []):
        key = payment_key(pay["customer"], pay["when"], money(pay["amount"]))
        if pay.get("tender") or "tender" not in store["payments"].get(key, {}):
            store["payments"][key] = pay
    if cap_data.get("credit_read"):
        store["through"] = today.isoformat()
    return store


def payloads_sha(payloads: list[dict]) -> str:
    return hashlib.sha256("\n".join(json.dumps(p, sort_keys=True) for p in payloads).encode()).hexdigest()


# ---------------------------------------------------------------- parse report grids (pure)
def parse_credit_rows(rows: list[list[str]]) -> list[dict]:
    """Customer Credit grid rows -> [{customer, location, when, amount}]; header/total rows are skipped."""
    out = []
    for cells in rows:
        cells = [" ".join(str(c).split()) for c in cells]
        if len(cells) < 4 or not parse_when(cells[2]):
            continue
        amount = money(cells[3])
        if amount is None:
            continue
        out.append({"customer": cells[0], "location": cells[1], "when": cells[2], "amount": str(amount)})
    return out


def parse_customer_rows(rows: list[list[str]]) -> list[dict]:
    """Customer Report grid (first row = headers) -> [{name, balance, max_credit, last_transaction}]."""
    if not rows:
        return []
    head = [h.strip().upper() for h in rows[0]]
    col = {name: head.index(name) for name in ("FIRST NAME", "LAST NAME", "CURRENT BALANCE", "MAX CREDIT",
                                                 "LAST TRANSACTION DATE") if name in head}
    if "CURRENT BALANCE" not in col:
        raise ValueError("EPOS Customer Report has no CURRENT BALANCE column")
    out = []
    for cells in rows[1:]:
        get = lambda k: cells[col[k]].strip() if k in col and col[k] < len(cells) else ""  # noqa: E731
        name = " ".join(f"{get('FIRST NAME')} {get('LAST NAME')}".split())
        if not name:
            continue  # the totals row
        out.append({"name": name, "balance": f"{money(get('CURRENT BALANCE')) or Decimal(0):.2f}",
                    "max_credit": f"{money(get('MAX CREDIT')) or Decimal(0):.2f}",
                    "last_transaction": get("LAST TRANSACTION DATE")})
    return out


def match_transaction(payment: dict, rows: list[list[str]]) -> dict | None:
    """The Completed Transactions row for this account payment: same date/time, amount and customer."""
    want_name, want_amount = norm_name(payment["customer"]), money(payment["amount"])
    for cells in rows:
        cells = [" ".join(str(c).split()) for c in cells]
        if len(cells) < 10 or cells[4] != payment["when"]:
            continue
        customer = re.sub(r"\s*\([^)]*\)\s*$", "", cells[5])  # "VERA AKPOREHA (No Type)"
        if norm_name(customer) == want_name and money(cells[8]) == want_amount:
            return {"staff": cells[0], "device": cells[2], "tender": cells[9]}
    return None


# ---------------------------------------------------------------- EPOS capture (view only, browser)
GRID_JS = """(gid) => { const t = document.getElementById(gid); if (!t) return null;
  return Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim())); }"""
CUSTOMER_GRID_JS = """() => { const t = Array.from(document.querySelectorAll('table'))
  .find(t => /FIRST NAME/i.test((t.rows[0] || {}).innerText || '') && /CURRENT BALANCE/i.test((t.rows[0] || {}).innerText || ''));
  return t ? Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim())) : null; }"""


def totals_of(rows: list[list[str]]) -> list[Decimal]:
    return [t for t in (money(r[8]) for r in rows if len(r) > 9) if t is not None]


def capture(out: Path, *, days: int = 30, recheck: list[dict] = (), company_key: str = COMPANY) -> dict:
    """Read Customer Credit (last ``days``), each payment's Completed Transactions row, and the Customer Report.
    ``recheck``: payments read on an earlier night whose tender was not found yet, looked up again.
    Clicks only login, the report filters/Apply, the transactions calendar, sort and page links. Never edits."""
    from playwright.sync_api import sync_playwright

    from code_scripts.akponora_ops.epos_adjustments import select_calendar_date
    from code_scripts.scripts.akponora_cutover._common import company_config, epos_login, goto_retry

    cfg = company_config(company_key)
    started = time.monotonic()
    errors: list[str] = []

    def settle(page, ms=2500):
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(ms)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # EPOS shows times in the viewer's zone; Lagos makes them business-day times
        page = browser.new_context(viewport={"width": 1600, "height": 1000}, timezone_id="Africa/Lagos").new_page()
        epos_login(page, cfg)

        goto_retry(page, CREDITS_URL)
        settle(page)
        page.select_option("#MainContent_timeControl_ddlDateRange", str(days))
        settle(page)
        page.click("#MainContent_FetchFromServer")
        settle(page)
        grid = page.evaluate(GRID_JS, "MainContent_gvCredit")
        if grid is None and "no data has been found" not in page.content().lower():
            raise RuntimeError("EPOS Customer Credit report did not load")
        payments = parse_credit_rows(grid or [])
        new = {(x["customer"], x["when"], x["amount"]) for x in payments}
        payments += [dict(x) for x in recheck if (x["customer"], x["when"], x["amount"]) not in new]

        by_day: dict[str, list[dict]] = {}
        for pay in payments:
            by_day.setdefault(parse_when(pay["when"]).date().isoformat(), []).append(pay)
        for day, todo in sorted(by_day.items()):
            goto_retry(page, TRANSACTIONS_URL)
            settle(page)
            select_calendar_date(page, "MainContent_calDate", datetime.fromisoformat(day), settle)
            page.evaluate("__doPostBack('ctl00$MainContent$gvTransactions','Sort$Total')")
            settle(page)
            totals = totals_of(page.evaluate(GRID_JS, "MainContent_gvTransactions") or [])
            if len(totals) > 1 and totals[0] < totals[-1]:  # EPOS may remember the sort and flip it: largest first
                page.evaluate("__doPostBack('ctl00$MainContent$gvTransactions','Sort$Total')")
                settle(page)
            smallest = min(money(x["amount"]) for x in todo)
            for pageno in range(1, 60):
                rows = page.evaluate(GRID_JS, "MainContent_gvTransactions") or []
                for pay in todo:
                    if "tender" not in pay:
                        hit = match_transaction(pay, rows)
                        if hit:
                            pay.update(hit)
                totals = totals_of(rows)
                largest_first = totals == sorted(totals, reverse=True)
                # stop early only once the page is truly largest-first and already below every wanted amount
                if all("tender" in x for x in todo) or not totals or (largest_first and max(totals) < smallest):
                    break
                if page.locator(f"#MainContent_gvTransactions a[href*=\"Page${pageno + 1}'\"]").count() == 0:
                    break
                page.evaluate(f"__doPostBack('ctl00$MainContent$gvTransactions','Page${pageno + 1}')")
                settle(page)
            for pay in todo:
                if "tender" not in pay:
                    errors.append(f"{pay['customer']} {pay['when']} N{pay['amount']}: transaction not found")

        goto_retry(page, CUSTOMERS_URL)
        settle(page)
        page.click("#MainContent_FetchFromServer")
        settle(page, 4000)
        balances = parse_customer_rows(page.evaluate(CUSTOMER_GRID_JS) or [])
        browser.close()

    result = {"tool": TOOL, "company": company_key, "captured_at": datetime.now(LAGOS).isoformat(timespec="seconds"),
              "timezone": "Africa/Lagos", "days": days, "credit_read": True, "payments": payments, "balances": balances,
              "errors": errors, "seconds": round(time.monotonic() - started, 1), "financial_writes": False}
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "capture.json", result)
    return result


# ---------------------------------------------------------------- QBO context (GET only)
def load_qbo(client, since: str = FROM_DAY) -> dict:
    customers = client.query_all("select * from Customer", "Customer")
    invoices = [i for i in client.query_all(f"select * from Invoice where TxnDate >= '{since}'", "Invoice")
                if str(i.get("DocNumber") or "").startswith("CR")]
    payments = client.query_all(f"select * from Payment where TxnDate >= '{since}'", "Payment")
    prefs = client.get_json("/preferences").get("Preferences", {})
    close = str(((prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")) or "")
    return {"customers": customers, "invoices": invoices, "payments": payments, "book_close": close}


def banked_days() -> set[str]:
    """Days uf_deposits has already moved to the banks (a late payment on such a day is a manual correction)."""
    from code_scripts.akponora_ops.common import state_dir as sd

    path = sd("uf_deposits") / "days.json"
    try:
        days = json.loads(path.read_text()).get("days", {})
    except (OSError, ValueError):
        return set()
    return {d for d, v in days.items() if isinstance(v, dict) and v.get("status") == "DEPOSITED"}


# ---------------------------------------------------------------- plan (pure apart from inputs)
def plan(cap_data: dict, qbo: dict, *, done: dict, banked: set[str], limit: Decimal) -> dict:
    from code_scripts.akponora_ops.credit_invoices import resolve_customer

    existing = {}
    for pmt in qbo["payments"]:
        m = TAG_RE.search(str(pmt.get("PrivateNote") or ""))
        if m:
            existing[m.group(1)] = pmt
    open_inv: dict[str, list[dict]] = {}
    for inv in sorted(qbo["invoices"], key=lambda i: (str(i.get("TxnDate")), int(str(i.get("Id") or 0)))):
        if money(inv.get("Balance")) and money(inv.get("Balance")) > 0:
            open_inv.setdefault(str((inv.get("CustomerRef") or {}).get("value")), []).append(
                {"id": str(inv["Id"]), "doc": inv.get("DocNumber"), "left": money(inv["Balance"])})
    rows, payloads = [], []
    for pay in sorted(cap_data.get("payments") or [], key=lambda x: parse_when(x["when"])):
        amount = money(pay["amount"])
        when = parse_when(pay["when"])
        day = when.date().isoformat()
        key = payment_key(pay["customer"], pay["when"], amount)
        row = {"key": key, "day": day, "when": pay["when"], "customer": pay["customer"], "amount": f"{amount:.2f}",
               "tender": pay.get("tender", ""), "staff": pay.get("staff", ""), "status": "", "reason": "",
               "qbo_customer": "", "applied_to": "", "qbo_id": ""}
        reasons = []
        if key in done or key in existing:
            row["status"], row["qbo_id"] = DONE, str((existing.get(key) or {}).get("Id") or done.get(key, {}).get("qbo_id", ""))
            rows.append(row)
            continue
        if day < FROM_DAY:
            reasons.append(f"before {FROM_DAY} (the Credit tender start)")
        if amount <= 0:
            reasons.append("not a positive amount (a refund or reversal: review by hand)")
        kind = tender_kind(pay.get("tender", ""))
        if not kind:
            reasons.append(f"payment method {'not found in Completed Transactions' if not pay.get('tender') else repr(pay['tender']) + ' is mixed or unknown'}")
        cust = resolve_customer("", pay["customer"], qbo["customers"], {})
        if "id" not in cust:
            reasons.append(f"customer: {cust.get('hold') or 'no QuickBooks customer named ' + repr(pay['customer'])}")
        if day in banked:
            reasons.append(f"{day} is already banked; record this payment by hand with a yes (it is probably in till overage)")
        if qbo.get("book_close") and day <= qbo["book_close"]:
            reasons.append(f"{day} is in the closed period (books closed to {qbo['book_close']})")
        if reasons:
            row["status"], row["reason"] = HOLD, "; ".join(reasons)
            rows.append(row)
            continue
        row["qbo_customer"] = cust["name"]
        lines, left = [], amount
        for inv in open_inv.get(cust["id"], []):
            if left <= 0:
                break
            use = min(left, inv["left"])
            if use > 0:
                lines.append({"Amount": float(use), "LinkedTxn": [{"TxnId": inv["id"], "TxnType": "Invoice"}]})
                inv["left"] -= use
                left -= use
                row["applied_to"] += f"{inv['doc']} N{use:,.2f}; "
        if left > 0:
            row["applied_to"] += f"unapplied credit N{left:,.2f}"
        note = (f"EPOS account payment {pay['customer']} {pay['when']} Lagos, {TENDERS[kind]}"
                f"{' by ' + pay['staff'] if pay.get('staff') else ''}{' on ' + pay['device'] if pay.get('device') else ''}"
                f" | epos-repay {key} | auto by {TOOL}")
        payload = {"CustomerRef": {"value": cust["id"]}, "TotalAmt": float(amount), "TxnDate": day,
                   "DepositToAccountRef": {"value": UF_ACCOUNT_ID}, "PaymentRefNum": ref_number(key),
                   "PrivateNote": note[:4000], "Line": lines, "_key": key}
        row["status"] = WAIT if amount > limit else READY
        if row["status"] == WAIT:
            row["reason"] = f"N{amount:,.2f} is over the N{limit:,.0f} limit: approve to post"
        payloads.append(payload)
        rows.append(row)
    return {"rows": rows, "payloads": payloads}


def write_plan(out: Path, cap_data: dict, p: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "payloads.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in p["payloads"]))
    write_csv(out / "review.csv", p["rows"], ["day", "when", "customer", "amount", "tender", "staff", "status",
                                              "reason", "qbo_customer", "applied_to", "qbo_id", "key"])
    counts = {}
    for r in p["rows"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {"tool": TOOL, "company": COMPANY, "status": "READY" if p["payloads"] else "DONE", "counts": counts,
               "payments": len(p["rows"]), "to_post": len(p["payloads"]),
               "total_to_post": f"{sum(Decimal(str(x['TotalAmt'])) for x in p['payloads']):.2f}",
               "balances": cap_data.get("balances") or [], "capture_errors": cap_data.get("errors") or [],
               "payloads_sha256": payloads_sha(p["payloads"]) if p["payloads"] else "", "financial_writes": False}
    dump_json(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- post (writes)
def verify(record: dict, want: dict) -> list[str]:
    problems = []
    if str((record.get("CustomerRef") or {}).get("value")) != want["CustomerRef"]["value"]:
        problems.append("customer differs")
    if money(record.get("TotalAmt")) != money(want["TotalAmt"]):
        problems.append(f"total {record.get('TotalAmt')} != {want['TotalAmt']}")
    if str(record.get("TxnDate")) != want["TxnDate"]:
        problems.append(f"date {record.get('TxnDate')} != {want['TxnDate']}")
    if str((record.get("DepositToAccountRef") or {}).get("value")) != UF_ACCOUNT_ID:
        problems.append("not in Undeposited Funds")
    return problems


def post(plan_dir: Path, *, client, approval_ref: str, expect_sha: str, include_over_limit: bool = False) -> dict:
    """Post an approved plan. Every payment is looked up by its tag first, so a retry never posts twice."""
    if not approval_ref.strip():
        raise StopPost("an approval reference is required")
    payloads = [json.loads(x) for x in (Path(plan_dir) / "payloads.jsonl").read_text().splitlines() if x.strip()]
    if not payloads or payloads_sha(payloads) != expect_sha:
        raise StopPost("plan changed since it was approved (payloads sha differs); re-plan")
    with (Path(plan_dir) / "review.csv").open(newline="", encoding="utf-8") as fh:
        rows = {r["key"]: r for r in csv.DictReader(fh)}
    book = ledger()
    results = []
    for p in payloads:
        key = p.pop("_key")
        if rows.get(key, {}).get("status") == WAIT and not include_over_limit:
            results.append({"key": key, "status": "WAITING", "qbo_id": "", "detail": "over the limit"})
            continue
        if key in book:
            results.append({"key": key, "status": "ALREADY_DONE", "qbo_id": book[key]["qbo_id"], "detail": "ledger"})
            continue
        same_day = client.query_all(f"select * from Payment where TxnDate = '{p['TxnDate']}'", "Payment")
        found = [x for x in same_day if f"| epos-repay {key} |" in str(x.get("PrivateNote") or "")]
        if found:
            problems = verify(found[0], p)
            book[key] = {"qbo_id": str(found[0]["Id"]), "at": "found in QuickBooks"}
            dump_json(ledger_path(), book)
            results.append({"key": key, "status": "ALREADY_DONE" if not problems else FAILED,
                            "qbo_id": str(found[0]["Id"]), "detail": "; ".join(problems)})
            continue
        body = dict(p, PrivateNote=f"{p['PrivateNote']} | approval {approval_ref}"[:4000])
        try:
            made = client.post_json("/payment", body, f"epos-repay-{key}").get("Payment") or {}
        except Exception as exc:  # noqa: BLE001 - recorded; the tag lookup makes a retry safe
            results.append({"key": key, "status": FAILED, "qbo_id": "", "detail": str(exc)[:300]})
            continue
        book[key] = {"qbo_id": str(made.get("Id")), "at": datetime.now(LAGOS).isoformat(timespec="seconds"),
                     "approval": approval_ref}
        dump_json(ledger_path(), book)
        back = client.get_json(f"/payment/{made.get('Id')}").get("Payment") or {}
        problems = verify(back, p)
        results.append({"key": key, "status": POSTED if not problems else FAILED, "qbo_id": str(made.get("Id")),
                        "detail": "; ".join(problems)})
    write_csv(Path(plan_dir) / "results.csv", results, ["key", "status", "qbo_id", "detail"])
    res = {"approval_ref": approval_ref, "results": results,
           "complete": all(r["status"] in (POSTED, "ALREADY_DONE", "WAITING") for r in results)}
    dump_json(Path(plan_dir) / "post_summary.json", res)
    return res


# ---------------------------------------------------------------- corrections: payments on already-banked days
# Owner, 26 Oct 2026. Before this step existed, uf_deposits booked a day's account payments as till overage (the
# till sheet holds the money, EPOS sales do not). Correction, per payment, approved before any write: record the
# Payment (Undeposited Funds, oldest invoices first), then put it into that day's existing UF deposit and lower the
# deposit's Till Overage line by the same amount. The deposit total and the banks do not change.
def own_overage_lines(deposits: list[dict], day: str, overage_id: str) -> list[dict]:
    from code_scripts.akponora_ops.uf_deposits import deposit_key, is_own_deposit_doc

    out = []
    for dep in deposits:
        if str(dep.get("TxnDate")) != day or not is_own_deposit_doc(deposit_key(dep), day):
            continue
        for ln in dep.get("Line") or []:
            ref = ((ln.get("DepositLineDetail") or {}).get("AccountRef") or {}).get("value")
            if str(ref) == str(overage_id) and not ln.get("LinkedTxn"):
                out.append({"deposit_id": str(dep["Id"]), "sync": str(dep.get("SyncToken")), "line_id": str(ln.get("Id")),
                            "left": money(ln.get("Amount")), "doc": deposit_key(dep)})
    return sorted(out, key=lambda x: -x["left"])


def plan_corrections(cap_data: dict, qbo: dict, deposits: list[dict], overage_id: str, *, done: dict,
                     banked: set[str], limit: Decimal) -> dict:
    # a correction is finished only when its deposit is fixed too: a Payment already made (ledger or QBO tag)
    # without its deposit change is planned again; posting then finds that Payment by its tag
    finished = {k: v for k, v in done.items() if v.get("deposit")}
    def unfinished(x):
        m = TAG_RE.search(str(x.get("PrivateNote") or ""))
        return bool(m) and m.group(1) not in finished

    q = dict(qbo, payments=[x for x in qbo["payments"] if not unfinished(x)])
    base = plan(cap_data, q, done=finished, banked=set(), limit=Decimal("1e30"))
    bodies = {p["_key"]: p for p in base["payloads"]}
    lines: dict[str, list[dict]] = {}
    rows, items = [], []
    for row in base["rows"]:
        if row["day"] not in banked or row["status"] == DONE:
            continue
        row = dict(row)
        if row["status"] != READY:
            rows.append(row)
            continue
        amount = money(row["amount"])
        pool = lines.setdefault(row["day"], own_overage_lines(deposits, row["day"], overage_id))
        line = next((x for x in pool if x["left"] >= amount), None)
        if line is None:
            row["status"], row["reason"] = HOLD, f"no {row['day']} UF deposit has N{amount:,.2f} of till overage to move"
            rows.append(row)
            continue
        line["left"] -= amount
        row["reason"] = f"move N{amount:,.2f} out of till overage in deposit {line['deposit_id']} ({line['doc']})"
        items.append({"key": row["key"], "day": row["day"], "amount": f"{amount:.2f}", "payment": bodies[row["key"]],
                      "deposit_id": line["deposit_id"], "deposit_sync": line["sync"], "overage_line_id": line["line_id"]})
        rows.append(row)
    return {"rows": rows, "items": items}


def write_corrections(out: Path, p: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "corrections.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in p["items"]))
    write_csv(out / "corrections.csv", p["rows"], ["day", "when", "customer", "amount", "tender", "staff", "status",
                                                   "reason", "qbo_customer", "applied_to", "key"])
    summary = {"tool": TOOL, "kind": "corrections", "items": len(p["items"]),
               "total": f"{sum(Decimal(x['amount']) for x in p['items']):.2f}",
               "held": [r for r in p["rows"] if r["status"] == HOLD],
               "sha256": payloads_sha(p["items"]) if p["items"] else "", "financial_writes": False}
    dump_json(out / "corrections_summary.json", summary)
    return summary


def corrected_deposit(dep: dict, item: dict, payment_id: str) -> dict:
    """The deposit with the Payment linked in and its Till Overage line lowered by the same amount."""
    amount = money(item["amount"])
    lines = []
    for ln in dep.get("Line") or []:
        if str(ln.get("Id")) == item["overage_line_id"] and not ln.get("LinkedTxn"):
            rest = money(ln.get("Amount")) - amount
            if rest < 0:
                raise StopPost(f"deposit {dep['Id']} overage line is now N{money(ln.get('Amount')):,.2f}; re-plan")
            if rest > 0:
                lines.append(dict(ln, Amount=float(rest)))
        else:
            lines.append(ln)
    lines.append({"Amount": float(amount), "LinkedTxn": [{"TxnId": payment_id, "TxnType": "Payment", "TxnLineId": "0"}]})
    return {**dep, "Line": lines,
            "PrivateNote": f"{dep.get('PrivateNote') or ''} | {item['amount']} of till overage was account payment "
                           f"{payment_id} (epos-repay {item['key']})"[:4000]}


def post_corrections(plan_dir: Path, *, client, approval_ref: str, expect_sha: str) -> dict:
    if not approval_ref.strip():
        raise StopPost("an approval reference is required")
    items = [json.loads(x) for x in (Path(plan_dir) / "corrections.jsonl").read_text().splitlines() if x.strip()]
    if not items or payloads_sha(items) != expect_sha:
        raise StopPost("corrections changed since they were approved; re-plan")
    book, results = ledger(), []
    for item in items:
        key, pay = item["key"], dict(item["payment"])
        pay.pop("_key", None)
        rec = book.get(key) or {}
        pid = rec.get("qbo_id")
        dep = client.get_json(f"/deposit/{item['deposit_id']}").get("Deposit") or {}
        linked_now = {str(t.get("TxnId")) for ln in dep.get("Line") or [] for t in ln.get("LinkedTxn") or []}
        if not (pid and pid in linked_now) and str(dep.get("SyncToken")) != item["deposit_sync"]:
            results.append({"key": key, "status": FAILED, "payment": pid or "",
                            "detail": f"deposit {item['deposit_id']} changed since the plan; re-plan"})
            break
        if not pid:
            same_day = client.query_all(f"select * from Payment where TxnDate = '{pay['TxnDate']}'", "Payment")
            found = [x for x in same_day if f"| epos-repay {key} |" in str(x.get("PrivateNote") or "")]
            if found:
                pid = str(found[0]["Id"])
            else:
                body = dict(pay, PrivateNote=f"{pay['PrivateNote']} | correction, approval {approval_ref}"[:4000])
                made = client.post_json("/payment", body, f"epos-repay-{key}").get("Payment") or {}
                pid = str(made.get("Id") or "")
                if not pid:
                    results.append({"key": key, "status": FAILED, "detail": "QuickBooks did not return a payment"})
                    break
            problems = verify((client.get_json(f"/payment/{pid}").get("Payment") or {}), pay)
            book[key] = {"qbo_id": pid, "at": datetime.now(LAGOS).isoformat(timespec="seconds"), "approval": approval_ref}
            dump_json(ledger_path(), book)
            if problems:
                results.append({"key": key, "status": FAILED, "payment": pid, "detail": "; ".join(problems)})
                break
        dep = client.get_json(f"/deposit/{item['deposit_id']}").get("Deposit") or {}
        linked = {str(t.get("TxnId")) for ln in dep.get("Line") or [] for t in ln.get("LinkedTxn") or []}
        if pid in linked:
            book[key]["deposit"] = item["deposit_id"]
            dump_json(ledger_path(), book)
            results.append({"key": key, "status": "ALREADY_DONE", "payment": pid, "deposit": item["deposit_id"]})
            continue
        if str(dep.get("SyncToken")) != item["deposit_sync"]:
            results.append({"key": key, "status": FAILED, "payment": pid,
                            "detail": f"deposit {item['deposit_id']} changed since the plan; re-plan"})
            break
        before = money(dep.get("TotalAmt"))
        try:
            client.post_json("/deposit", corrected_deposit(dep, item, pid), f"epos-repay-dep-{key}")
        except Exception as exc:  # noqa: BLE001 - the link check above makes a retry safe
            results.append({"key": key, "status": FAILED, "payment": pid, "detail": str(exc)[:300]})
            break
        after = client.get_json(f"/deposit/{item['deposit_id']}").get("Deposit") or {}
        ok = (money(after.get("TotalAmt")) == before and
              pid in {str(t.get("TxnId")) for ln in after.get("Line") or [] for t in ln.get("LinkedTxn") or []})
        book[key]["deposit"] = item["deposit_id"]
        dump_json(ledger_path(), book)
        results.append({"key": key, "status": POSTED if ok else FAILED, "payment": pid, "deposit": item["deposit_id"],
                        "detail": "" if ok else f"deposit total {after.get('TotalAmt')} (was {before}) or link missing"})
        if not ok:
            break
        # the next item on the same deposit needs its fresh SyncToken
        for later in items:
            if later["deposit_id"] == item["deposit_id"]:
                later["deposit_sync"] = str(after.get("SyncToken"))
    res = {"approval_ref": approval_ref, "results": results,
           "complete": len(results) == len(items) and all(r["status"] in (POSTED, "ALREADY_DONE") for r in results)}
    dump_json(Path(plan_dir) / "corrections_post.json", res)
    return res


# ---------------------------------------------------------------- daily_run hook
def run(out: Path, *, client, write_client=None, dry_run: bool = False, env=None, cap_data: dict | None = None,
        approval_ref: str = "") -> dict:
    out = Path(out)
    if cap_data is None:
        today, done = datetime.now(LAGOS).date(), ledger()
        store = seen()
        recheck = [x for k, x in store["payments"].items() if not x.get("tender") and k not in done]
        cap_data = capture(out, days=window_days(store["through"], today), recheck=recheck)
        store = remember(store, cap_data, today)
        if not dry_run:
            dump_json(seen_path(), store)
        # plan over everything read so far, so a payment waiting for a yes is not lost when it leaves the window
        cap_data = {**cap_data, "payments": list(store["payments"].values())}
    p = plan(cap_data, load_qbo(client), done=ledger(), banked=banked_days(), limit=cap(env))
    summary = write_plan(out, cap_data, p)
    res = {"mode": mode(env), "summary": summary, "rows": p["rows"], "posted": []}
    if summary["status"] == "READY" and mode(env) == "post" and not dry_run and write_client is not None:
        r = post(out, client=write_client, approval_ref=approval_ref or f"{MODE_ENV}=post (daily run)",
                 expect_sha=summary["payloads_sha256"])
        res["posted"] = r["results"]
    return res


def main(argv=None) -> int:
    from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="READ-ONLY: read EPOS and QBO, write a plan (no QBO writes)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--capture", type=Path, help="reuse a capture.json instead of reading EPOS")
    p = sub.add_parser("plan-corrections", help="READ-ONLY: corrections for payments on already-banked days")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--capture", type=Path, help="reuse a capture.json instead of reading EPOS")
    p = sub.add_parser("post-corrections", help="WRITES: approved corrections (Payment + deposit overage move)")
    p.add_argument("--plan-dir", type=Path, required=True)
    p.add_argument("--approval-ref", required=True)
    p.add_argument("--expect-sha", required=True)
    p = sub.add_parser("post", help="WRITES QBO Payments from an approved plan")
    p.add_argument("--plan-dir", type=Path, required=True)
    p.add_argument("--approval-ref", required=True)
    p.add_argument("--expect-sha", required=True)
    p.add_argument("--include-over-limit", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        data = json.loads(a.capture.read_text()) if a.capture else None
        r = run(a.out, client=QBOClient.for_company_a(allow_writes=False), dry_run=True, cap_data=data)
        print(json.dumps({k: r["summary"][k] for k in ("status", "counts", "to_post", "total_to_post",
                                                       "payloads_sha256", "capture_errors")}, indent=1))
        for row in r["rows"]:
            print(f"  {row['when']} {row['customer']} N{row['amount']} {row['tender'] or '?'}: {row['status']} "
                  f"{row['reason'] or row['applied_to']}")
        return 0
    if a.cmd == "plan-corrections":
        from code_scripts.akponora_ops.uf_deposits import OVERAGE_ACCOUNT_DEFAULT

        client = QBOClient.for_company_a(allow_writes=False)
        data = json.loads(a.capture.read_text()) if a.capture else capture(a.out)
        q = load_qbo(client)
        overage = [x for x in client.query_all("select * from Account where Active = true", "Account")
                   if OVERAGE_ACCOUNT_DEFAULT in (x.get("Name"), x.get("FullyQualifiedName"))]
        if len(overage) != 1:
            print("STOP: Till Overage account not found exactly once")
            return 2
        deposits = client.query_all(f"select * from Deposit where TxnDate >= '{FROM_DAY}'", "Deposit")
        p = plan_corrections(data, q, deposits, str(overage[0]["Id"]), done=ledger(), banked=banked_days(),
                             limit=cap())
        summ = write_corrections(a.out, p)
        print(json.dumps({k: summ[k] for k in ("items", "total", "sha256")}, indent=1))
        for row in p["rows"]:
            print(f"  {row['when']} {row['customer']} N{row['amount']} {row['tender']}: {row['status']} {row['reason']}"
                  f" | {row['applied_to']}")
        return 0
    if a.cmd == "post-corrections":
        try:
            r = post_corrections(a.plan_dir, client=QBOClient.for_company_a(allow_writes=True),
                                 approval_ref=a.approval_ref, expect_sha=a.expect_sha)
        except StopPost as exc:
            print(f"STOP: {exc}")
            return 2
        print(json.dumps(r, indent=1))
        return 0 if r["complete"] else 2
    try:
        r = post(a.plan_dir, client=QBOClient.for_company_a(allow_writes=True), approval_ref=a.approval_ref,
                 expect_sha=a.expect_sha, include_over_limit=a.include_over_limit)
    except StopPost as exc:
        print(f"STOP: {exc}")
        return 2
    print(json.dumps(r, indent=1))
    return 0 if r["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

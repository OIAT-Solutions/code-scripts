"""Nora Mart Payments workbook <-> QuickBooks (part of the daily routine, step ``payments``).

Owner decisions (Marvin, chat 10 Oct 2026): one workbook; staff enter one row per payment; payments post
automatically (like banking), every account is listed and there is no "Other".

Each run:
1. Writes the system tabs from QuickBooks: ``Credit sales`` (credit invoices ``CR...`` from 1 Oct with
   their balance), ``Bills to pay`` (every open bill) and ``Lists`` B/C (open invoice / bill numbers for
   the dropdowns). Staff never edit these.
2. Reads ``Credit payments`` and ``Bill payments``. Staff pick a line such as
   ``Mrs VERA AKPOREHA · 5 Oct 2026 · ₦58,200.00 · CR261005-389764`` (who · when · total · number); the
   number at the end identifies the invoice/bill, so staff never need QuickBooks numbers. A row is processed once: its key (sha of the row's
   staff cells + how many identical rows come before it) is stored in ``posted.json`` and in the QBO memo.
   Checks: the invoice/bill exists and is open, the account is one of the listed accounts and maps to
   exactly one active QBO Bank account by its number, the amount is > 0 and not more than the balance
   left, the date is a real date, not in the future, not before the invoice/bill, not in a closed period.
   Over ``OIAT_COMPANY_A_PAYMENTS_CAP`` (N2,000,000) waits for approval. Then posts a ReceivePayment
   (credit) or BillPayment (bill) into/from that account, with a stable Intuit requestid, re-reads the
   invoice/bill (its balance must fall by the amount) and writes Status / QuickBooks Payment No /
   Processed At back on the row, then locks and greys the row (only the owner and the service account can
   change it). A problem is written as ``Held: <reason>``; the row is retried next run.

Settings (env or ``STATE_ROOT/ops/company_a/payments_sheet/settings.env``):
``OIAT_COMPANY_A_PAYMENTS_SHEET_ID`` (off when blank), ``OIAT_COMPANY_A_PAYMENTS_POST=1`` to post (else
the run only checks and writes "Ready (posting is off)"), ``OIAT_COMPANY_A_PAYMENTS_CAP``.
Google access: the till sheet's service account (``uf_deposits.key_path``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import REALM, dump_json, state_dir
from code_scripts.scripts.akponora_cutover.w7_create_items import sha256_text
from code_scripts.akponora_ops.work_state import WorkState, digest

TOOL = "payments_sheet"
TZ = ZoneInfo("Africa/Lagos")
FROM_DAY = "2026-10-01"
EPOS_REPAY_TAG = re.compile(r"\| epos-repay [0-9a-f]{16} \|")
SHEET_ENV, POST_ENV, CAP_ENV = "OIAT_COMPANY_A_PAYMENTS_SHEET_ID", "OIAT_COMPANY_A_PAYMENTS_POST", "OIAT_COMPANY_A_PAYMENTS_CAP"
CREDIT_PREFIX = "CR"

CREDIT_SALES, CREDIT_PAYMENTS, BILLS, BILL_PAYMENTS, LISTS = (
    "Credit sales", "Credit payments", "Bills to pay", "Bill payments", "Lists")
CREDIT_SALES_COLS = ["Invoice No", "Invoice Date", "Customer", "Total", "Paid", "Balance", "Status", "Last Updated"]
BILLS_COLS = ["Bill No", "Bill Date", "Supplier", "Total", "Paid", "Balance", "Days Outstanding", "Last Updated"]
CREDIT_PAY_COLS = ["Invoice No", "Amount Received", "Date Received", "Received Into", "Reference", "Entered By", "Notes",
                   "Status", "QuickBooks Payment No", "Processed At"]
BILL_PAY_COLS = ["Bill No", "Amount Paid", "Date Paid", "Paid From", "Reference", "Entered By", "Notes",
                 "Status", "QuickBooks Payment No", "Processed At"]
LIST_COLS = ["Account", "Open Credit Invoices", "Open Bills"]
POSTED, READY_OFF, WAITING = "Posted", "Ready (posting is off)", "Waiting for approval"


class SheetError(RuntimeError):
    pass


# ---------------------------------------------------------------- small helpers
def clean(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def money(value) -> Decimal | None:
    text = clean(value).replace("₦", "").replace(",", "")
    try:
        return Decimal(text).quantize(Decimal("0.01")) if text else None
    except InvalidOperation:
        return None


def parse_day(value) -> str:
    """A sheet date (dd/mm/yyyy text, ISO text, or a Sheets serial number) -> ISO, or ''."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(value)).isoformat()
    text = clean(value)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


EPOCH = date(1899, 12, 30)  # Google Sheets day 0


def serial(iso: str | None):
    """An ISO date as a Sheets date number, so the column's date format applies ('' when not a date)."""
    try:
        return (date.fromisoformat(str(iso)) - EPOCH).days
    except ValueError:
        return ""


def now_serial() -> float:
    n = datetime.now(TZ).replace(tzinfo=None)
    return round((n - datetime(1899, 12, 30)).total_seconds() / 86400, 6)


def now_text() -> str:
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def account_number(label: str) -> str:
    m = re.search(r"\b(100\d{3})\b", clean(label))
    return m.group(1) if m else ""


def settings(env=None) -> dict:
    env = dict(os.environ if env is None else env)
    path = state_dir(TOOL) / "settings.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return {"sheet_id": clean(env.get(SHEET_ENV)), "post": clean(env.get(POST_ENV)) in {"1", "true", "yes", "on"},
            "cap": money(env.get(CAP_ENV)) or Decimal("2000000")}


FIRST_COL_NAMES = {"Invoice No": {"Invoice No", "Credit Sale"}, "Bill No": {"Bill No", "Bill"}}


def headers_ok(rows: list[list], cols: list[str], tab: str) -> None:
    got = [clean(c) for c in (rows[0] if rows else [])][:len(cols)]
    if got and got[0] in FIRST_COL_NAMES.get(cols[0], ()) and tab in (CREDIT_PAYMENTS, BILL_PAYMENTS):
        got[0] = cols[0]  # the staff-facing name of the pick column ("Credit Sale" / "Bill") is accepted
    if got != cols:
        raise SheetError(f"tab '{tab}' headers are {got}, expected {cols} - fix the sheet, nothing was posted")


def row_keys(tab: str, rows: list[list]) -> list[str]:
    """Stable key per payment row: its staff cells (A-G) + how many identical rows come before it."""
    seen, out = {}, []
    for r in rows:
        cells = [clean(c) for c in (list(r) + [""] * 7)[:7]]
        base = json.dumps([tab] + cells)
        n = seen[base] = seen.get(base, 0) + 1
        out.append(hashlib.sha256(f"{base}|{n}".encode()).hexdigest()[:20])
    return out


# ---------------------------------------------------------------- Google Sheets
class GoogleSheet:
    def __init__(self, sheet_id: str, key_path: str | Path, service=None):
        self.id = sheet_id
        if service is None:
            from code_scripts.akponora_ops.till_sheet_marks import writer_service
            service = writer_service(key_path)
        self.service = service
        self.values = service.spreadsheets().values()

    def read(self, tab: str, cols: int) -> list[list]:
        last = chr(ord("A") + cols - 1)
        resp = self.values.get(spreadsheetId=self.id, range=f"'{tab}'!A1:{last}",
                               valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="FORMATTED_STRING").execute()
        return [list(r) for r in resp.get("values") or []]

    def replace_body(self, tab: str, cols: int, rows: list[list], *, first_col: str = "A") -> None:
        last = chr(ord(first_col) + cols - 1)
        self.values.clear(spreadsheetId=self.id, range=f"'{tab}'!{first_col}2:{last}").execute()
        if rows:
            self.values.update(spreadsheetId=self.id, range=f"'{tab}'!{first_col}2", valueInputOption="RAW",
                               body={"values": rows}).execute()

    def lock_rows(self, tab: str, rows: list[int], editor: str) -> None:
        """Lock posted rows (only the owner and ``editor`` may change them) and shade them grey."""
        if not rows:
            return
        meta = self.service.spreadsheets().get(spreadsheetId=self.id, fields="sheets.properties").execute()
        sid = next(sh["properties"]["sheetId"] for sh in meta["sheets"] if sh["properties"]["title"] == tab)
        reqs = []
        for n in sorted(set(rows)):
            rng = {"sheetId": sid, "startRowIndex": n - 1, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": 11}
            reqs.append({"addProtectedRange": {"protectedRange": {
                "range": rng, "description": f"{TOOL}: posted payment (row {n})", "editors": {"users": [editor]}}}})
            reqs.append({"repeatCell": {"range": rng, "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}}}, "fields": "userEnteredFormat.backgroundColor"}})
        self.service.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": reqs}).execute()

    def ensure_payment_ids(self, tab: str, rows: list[list]) -> list[list]:
        """Persist immutable identities BEFORE any financial operation; A:J stays compatible."""
        rows = [list(r) + [""] * max(0, 11 - len(r)) for r in rows]
        if not rows:
            raise SheetError(f"{tab}: payment headers are missing")
        if clean(rows[0][10]) not in ("", "Payment ID"):
            raise SheetError(f"{tab}: column K is occupied; cannot install payment identities")
        updates, seen = [(f"'{tab}'!K1", ["Payment ID"])], set()
        rows[0][10] = "Payment ID"
        for n, row in enumerate(rows[1:], 2):
            if not any(clean(c) for c in row[:7]):
                continue
            pid = clean(row[10])
            if pid and not re.fullmatch(r"NP-[0-9a-f]{20}", pid):
                raise SheetError(f"{tab} row {n}: invalid Payment ID")
            if not pid:
                pid = "NP-" + uuid.uuid4().hex[:20]
                row[10] = pid
                updates.append((f"'{tab}'!K{n}", [pid]))
            if pid in seen:
                raise SheetError(f"{tab}: duplicate Payment ID; copied rows need a new identity")
            seen.add(pid)
        self.write_cells(updates)
        fresh = [list(r) + [""] * max(0, 11 - len(r)) for r in self.read(tab, 11)]
        if len(fresh) != len(rows) or any([clean(x) for x in old[:7] + [old[10]]] !=
                                         [clean(x) for x in new[:7] + [new[10]]] for old, new in zip(rows, fresh)):
            raise SheetError(f"{tab}: rows changed while payment identities were installed; refresh before posting")
        return fresh

    def write_cells(self, updates: list[tuple[str, list]]) -> None:
        if updates:
            self.values.batchUpdate(spreadsheetId=self.id, body={"valueInputOption": "RAW", "data": [
                {"range": rng, "values": [vals]} for rng, vals in updates]}).execute()


# ---------------------------------------------------------------- QuickBooks reads
def load_qbo(client) -> dict:
    banks = client.query_all("select * from Account where AccountType = 'Bank' and Active = true", "Account")
    invoices = [i for i in client.query_all(f"select * from Invoice where TxnDate >= '{FROM_DAY}'", "Invoice")
                if str(i.get("DocNumber") or "").startswith(CREDIT_PREFIX)]
    bills = client.query_all("select * from Bill where Balance > '0'", "Bill")
    prefs = client.get_json("/preferences").get("Preferences", {})
    close = str(((prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")) or "")
    # account payments credit_repayments recorded from EPOS (memo tag): a staff row for the same money links to them
    epos_payments = [p for p in client.query_all(f"select * from Payment where TxnDate >= '{FROM_DAY}'", "Payment")
                     if EPOS_REPAY_TAG.search(str(p.get("PrivateNote") or ""))]
    return {"banks": banks, "invoices": invoices, "bills": bills, "book_close": close, "epos_payments": epos_payments}


def epos_twin(cells: list, qbo: dict, used: set) -> tuple[dict | None, str]:
    """(EPOS-recorded Payment this staff credit row describes, '') or (None, reason to hold) or (None, '').
    Same customer as the row's invoice, same amount, within a day: the same money, never posted twice
    (26 Oct 2026). Same customer and day but a different amount (e.g. split over invoices): hold for review."""
    doc = doc_of(cells[0])
    inv = next((i for i in qbo.get("invoices") or [] if str(i.get("DocNumber")) == doc), None)
    day, amount = parse_day(cells[2]), money(cells[1])
    if not inv or not day or amount is None:
        return None, ""
    cust = str((inv.get("CustomerRef") or {}).get("value"))
    near = []
    for p in qbo.get("epos_payments") or []:
        try:
            gap = abs((date.fromisoformat(str(p.get("TxnDate"))) - date.fromisoformat(day)).days)
        except ValueError:
            continue
        if str((p.get("CustomerRef") or {}).get("value")) == cust and gap <= 1 and str(p.get("Id")) not in used:
            near.append(p)
    exact = [p for p in near if money(p.get("TotalAmt")) == amount]
    if exact:
        return exact[0], ""
    if near:
        p = near[0]
        return None, (f"EPOS already recorded a N{money(p.get('TotalAmt')):,.2f} payment from this customer on "
                      f"{p.get('TxnDate')} (QuickBooks payment {p.get('Id')}); check before posting this row")
    return None, ""


def bill_no(bill: dict, dup: set) -> str:
    doc = clean(bill.get("DocNumber"))
    return doc if doc and doc not in dup else f"QBO-{bill['Id']}"


def system_rows(qbo: dict) -> dict:
    stamp, ref = now_serial(), date.fromisoformat(today())
    credit = []
    for i in sorted(qbo["invoices"], key=lambda x: (x.get("TxnDate") or "", x.get("DocNumber") or "")):
        total, bal = Decimal(str(i.get("TotalAmt") or 0)), Decimal(str(i.get("Balance") or 0))
        status = "Paid" if bal == 0 else "Open" if bal == total else "Part paid"
        credit.append([i["DocNumber"], serial(i.get("TxnDate")), (i.get("CustomerRef") or {}).get("name", ""), float(total),
                       float(total - bal), float(bal), status, stamp])
    docs = [clean(b.get("DocNumber")) for b in qbo["bills"]]
    dup = {d for d in docs if d and docs.count(d) > 1}
    bills = []
    for b in sorted(qbo["bills"], key=lambda x: (x.get("TxnDate") or "", x.get("Id"))):
        total, bal = Decimal(str(b.get("TotalAmt") or 0)), Decimal(str(b.get("Balance") or 0))
        days = (ref - date.fromisoformat(b["TxnDate"])).days if b.get("TxnDate") else ""
        bills.append([bill_no(b, dup), serial(b.get("TxnDate")), (b.get("VendorRef") or {}).get("name", ""), float(total),
                      float(total - bal), float(bal), days, stamp])
    day = lambda n: (EPOCH + timedelta(days=n)).isoformat() if isinstance(n, int) else ""  # noqa: E731
    pick = lambda rows: sorted((label(r[2], day(r[1]), r[3], r[0]) for r in rows), key=str.casefold)  # noqa: E731
    return {"credit": credit, "bills": bills,
            "open_invoices": pick([r for r in credit if r[5] > 0]),
            # staff pick only current bills; older ones are cleared through bank reconciliation (owner, 12 Oct 2026)
            "open_bills": pick([r for r in bills if day(r[1]) >= FROM_DAY])}


SEP = " · "


def nice_day(iso: str) -> str:
    try:
        d = date.fromisoformat(str(iso))
    except ValueError:
        return str(iso or "")
    return f"{d.day} {d:%b %Y}"


def label(name: str, day: str, total, doc: str) -> str:
    """What staff pick: who · when · how much · the QuickBooks number (read back by ``doc_of``)."""
    return SEP.join([clean(name) or "?", nice_day(day), f"₦{Decimal(str(total or 0)):,.2f}", doc])


def doc_of(cell) -> str:
    """The QuickBooks number from a picked label (its last part); a bare number is accepted too."""
    return clean(cell).rsplit(SEP.strip(), 1)[-1].strip() if SEP.strip() in clean(cell) else clean(cell)


def current_payment_row(sheet, tab, identity, source_sha):
    rows = sheet.read(tab, 11)
    matches = [(n, list(row) + [""] * max(0, 11-len(row))) for n, row in enumerate(rows[1:], 2)
               if len(row) > 10 and clean(row[10]) == identity]
    if len(matches) != 1 or digest([clean(c) for c in matches[0][1][:7]]) != source_sha:
        raise SheetError(f"{tab}: payment row changed or its identity is missing/duplicated; refresh before posting")
    return matches[0][0]


def relocate_updates(sheet, tab, source_rows, updates):
    """A sort during a run must not put a Posted mark on another payment."""
    live = sheet.read(tab, 11)
    positions = {}
    for n, row in enumerate(live[1:], 2):
        if len(row) > 10 and clean(row[10]):
            positions.setdefault(clean(row[10]), []).append((n, row))
    moved = []
    for n, value in updates:
        row = source_rows[n-1];hits = positions.get(clean(row[10]), [])
        if len(hits) != 1 or [clean(c) for c in hits[0][1][:7]] != [clean(c) for c in row[:7]]:
            raise SheetError(f"{tab}: payment rows changed before status update; durable results retained for review")
        moved.append((hits[0][0], value))
    return moved


def bank_for(label: str, banks: list[dict]) -> tuple[dict | None, str]:
    number = account_number(label)
    if not number:
        return None, f"'{label}' is not one of the listed accounts"
    hits = [a for a in banks if clean(a.get("Name")).startswith(f"{number} ")]
    if len(hits) != 1:
        return None, f"account {number} is not exactly one active QuickBooks bank account"
    return hits[0], ""


# ---------------------------------------------------------------- payments
def check_row(kind: str, cells: list, target: dict | None, left: Decimal | None, banks, book_close: str, cap: Decimal):
    """(payload parts or None, problem). ``left`` = balance still open after earlier rows this run."""
    amount, day, label = money(cells[1]), parse_day(cells[2]), clean(cells[3])
    if target is None:
        return None, f"{'credit sale' if kind == 'credit' else 'bill'} {doc_of(cells[0]) or '(blank)'} is not open in QuickBooks"
    if amount is None or amount <= 0:
        return None, "the amount is missing or not a positive number"
    if amount > left:
        return None, f"N{amount:,.2f} is more than the N{left:,.2f} still owed"
    if not day:
        return None, "the date is missing or not a date (dd/mm/yyyy)"
    if day > today():
        return None, f"the date {day} is in the future"
    if day < str(target.get("TxnDate") or ""):
        return None, f"the date {day} is before the {'invoice' if kind == 'credit' else 'bill'} ({target.get('TxnDate')})"
    if book_close and day <= book_close:
        return None, f"the date {day} is in a closed period (books closed to {book_close})"
    bank, why = bank_for(label, banks)
    if bank is None:
        return None, why
    if amount > cap:
        return {"wait": True, "amount": amount, "day": day, "bank": bank}, ""
    return {"amount": amount, "day": day, "bank": bank}, ""


def payload(kind: str, target: dict, parts: dict, cells: list, key: str) -> tuple[str, dict]:
    amount = float(parts["amount"])
    note = (f"{'Credit sale payment' if kind == 'credit' else 'Supplier payment'} {doc_of(cells[0])} | "
            f"entered by {clean(cells[5]) or '?'} in the Nora Mart Payments sheet | row {key} | auto by {TOOL}")
    ref = clean(cells[4])[:21]
    if kind == "credit":
        body = {"CustomerRef": {"value": (target.get("CustomerRef") or {}).get("value")}, "TotalAmt": amount,
                "TxnDate": parts["day"], "DepositToAccountRef": {"value": str(parts["bank"]["Id"])},
                "PrivateNote": note, "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": target["Id"], "TxnType": "Invoice"}]}]}
        if ref:
            body["PaymentRefNum"] = ref
        return "/payment", body
    body = {"VendorRef": {"value": (target.get("VendorRef") or {}).get("value")}, "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": str(parts["bank"]["Id"])}}, "TotalAmt": amount,
            "TxnDate": parts["day"], "PrivateNote": note,
            "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": target["Id"], "TxnType": "Bill"}]}]}
    if ref:
        body["DocNumber"] = ref
    return "/billpayment", body


def state_path() -> Path:
    return state_dir(TOOL) / "posted.json"


def payment_matches(record: dict, expected: dict, kind: str) -> bool:
    parent = "CustomerRef" if kind == "credit" else "VendorRef"
    bank = lambda r: str(((r.get("DepositToAccountRef") or {}) if kind == "credit" else
                         ((r.get("CheckPayment") or {}).get("BankAccountRef") or {})).get("value") or "")
    links = lambda r: sorted((str(t.get("TxnId")), str(t.get("TxnType")), str(money(l.get("Amount"))))
                             for l in r.get("Line") or [] for t in l.get("LinkedTxn") or [])
    return (record.get("TxnDate") == expected["TxnDate"] and money(record.get("TotalAmt")) == money(expected["TotalAmt"])
            and str((record.get(parent) or {}).get("value")) == str((expected.get(parent) or {}).get("value"))
            and bank(record) == bank(expected) and links(record) == links(expected))


def recover_payment(client, kind: str, expected: dict, keys: list[str], cache: dict) -> dict | None:
    entity = "Payment" if kind == "credit" else "BillPayment"
    ck = (entity, expected["TxnDate"])
    if ck not in cache:
        cache[ck] = client.query_all(f"select * from {entity} where TxnDate = '{expected['TxnDate']}'", entity)
    matches = [r for r in cache[ck] if any(f"row {key} |" in str(r.get("PrivateNote") or "") for key in keys)]
    if len(matches) > 1 or (matches and (not matches[0].get("Id") or not payment_matches(matches[0], expected, kind))):
        raise SheetError("existing payment identity has conflicting accounting details; review required")
    return matches[0] if matches else None


def process(kind: str, rows: list[list], targets: dict, qbo: dict, *, s: dict, client, write_client, posted: dict) -> tuple[list, list]:
    """Returns (cell updates [(row number, [Status, Payment No, Processed At])], results)."""
    updates, results = [], []
    left = {k: Decimal(str(t.get("Balance") or 0)) for k, t in targets.items()}
    body = rows[1:]
    journal = WorkState(state_dir(TOOL) / "work.sqlite3")
    epos_used = {str(v.get("payment_id")) for v in posted.values() if v.get("from_epos")}
    def save_posted():
        dump_json(state_path(), posted)
    for n, (cells, legacy_key) in enumerate(zip(body, row_keys(kind, body)), start=2):
        key = clean(cells[10]) if len(cells) > 10 and clean(cells[10]) else legacy_key
        source_sha = digest([clean(c) for c in (list(cells) + [""] * 7)[:7]])
        cells = (list(cells) + [""] * 10)[:10]
        if not any(clean(c) for c in cells[:7]):
            continue
        status = clean(cells[7])
        if key not in posted and legacy_key in posted:
            posted[key] = {**posted[legacy_key], "source_sha": source_sha, "legacy_key": legacy_key}
        if key in posted:
            if posted[key].get("source_sha", source_sha) != source_sha or posted[key].get("verification_required"):
                results.append({"row": n, "status": "edited after posting", "reason": "payment identity changed or verification remains outstanding"})
                continue
            if status != POSTED:  # sheet lost the mark (sorted/edited): write it back, never post again
                updates.append((n, [POSTED, posted[key]["payment_id"], posted[key]["at"]]))
            continue
        if status == POSTED:  # changed after posting: the old key is gone; never re-post an edited row
            results.append({"row": n, "status": "edited after posting"})
            continue
        prior = journal.get("payment", key)
        if prior and prior["status"] in ("sending", "unknown"):
            # Reconcile BEFORE checking the remaining balance: a fully accepted payment may
            # already have closed the bill/invoice and removed it from the open target list.
            try:
                if prior["source_sha"] != source_sha:
                    raise SheetError("payment was edited after an uncertain write")
                expected = prior["evidence"].get("payload")
                if not expected:
                    raise SheetError("original payment evidence needs operator review")
                found = recover_payment(client, kind, expected, [key, legacy_key], qbo.setdefault("payment_lookup", {}))
                if not found:
                    raise SheetError("previous payment result is uncertain; verify it before retrying")
                posted[key] = {"payment_id": str(found["Id"]), "at": now_text(), "kind": kind,
                               "doc": doc_of(cells[0]), "source_sha": source_sha}
                save_posted()
                journal.put("payment", key, source_sha, "verified", posted[key])
                updates.append((n, [POSTED, str(found["Id"]), posted[key]["at"]]))
                results.append({"row": n, "status": "posted", "recovered": True, "payment_id": str(found["Id"])})
            except Exception as exc:
                updates.append((n, [f"Held: {exc}", "", now_text()]))
                results.append({"row": n, "status": "held", "reason": str(exc)})
            continue
        if kind == "credit":
            twin, hold = epos_twin(cells, qbo, epos_used)
            if twin:  # the same money EPOS already recorded: link the row to it, never a second payment
                epos_used.add(str(twin["Id"]))
                posted[key] = {"payment_id": str(twin["Id"]), "at": now_text(), "kind": kind, "doc": doc_of(cells[0]),
                               "source_sha": source_sha, "from_epos": True}
                save_posted()
                journal.put("payment", key, source_sha, "verified", posted[key])
                updates.append((n, [POSTED, str(twin["Id"]), posted[key]["at"]]))
                results.append({"row": n, "doc": doc_of(cells[0]), "status": "posted", "payment_id": str(twin["Id"]),
                                "linked_epos": True})
                continue
            if hold:
                updates.append((n, [f"Held: {hold}", "", now_text()]))
                results.append({"row": n, "doc": doc_of(cells[0]), "status": "held", "reason": hold})
                continue
        target_key = doc_of(cells[0])
        target = targets.get(target_key)
        parts, why = check_row(kind, cells, target, left.get(target_key), qbo["banks"], qbo["book_close"], s["cap"])
        row = {"row": n, "doc": target_key, "amount": str(money(cells[1]) or ""), "account": clean(cells[3])}
        if why:
            updates.append((n, [f"Held: {why}", "", now_text()]))
            results.append({**row, "status": "held", "reason": why})
            continue
        if parts.get("wait"):
            updates.append((n, [f"{WAITING} (over N{s['cap']:,.0f})", "", now_text()]))
            results.append({**row, "status": "waiting"})
            continue
        if not s["post"] or write_client is None:
            updates.append((n, [READY_OFF, "", now_text()]))
            results.append({**row, "status": "ready"})
            left[target_key] -= parts["amount"]
            continue
        path, body_json = payload(kind, target, parts, cells, key)
        before = left[target_key]
        prior = journal.get("payment", key)
        if prior and prior["source_sha"] != source_sha:
            updates.append((n, ["Held: payment identity was edited; review the original operation", "", now_text()]))
            results.append({**row, "status": "held", "reason": "payment identity changed"})
            continue
        if s.get("_row_checker"):
            try:
                s["_row_checker"](key, source_sha)
            except SheetError as exc:
                results.append({**row, "status": "held", "reason": str(exc)})
                continue
        try:
            found = recover_payment(client, kind, body_json, [key, legacy_key], qbo.setdefault("payment_lookup", {})) \
                if prior or key != legacy_key else None
            if found:
                posted[key] = {"payment_id": str(found["Id"]), "at": now_text(), "kind": kind,
                               "doc": target_key, "source_sha": source_sha}
                save_posted()
                journal.put("payment", key, source_sha, "verified", posted[key])
                updates.append((n, [POSTED, str(found["Id"]), posted[key]["at"]]))
                results.append({**row, "status": "posted", "payment_id": str(found["Id"]), "recovered": True})
                continue
            if prior and prior["status"] in ("sending", "unknown"):
                raise SheetError("previous payment result is uncertain; verify it before retrying")
            journal.put("payment", key, source_sha, "sending", {"kind": kind, "payload": body_json})
            resp = write_client.post_json(path, body_json, sha256_text(f"{TOOL}|{REALM}|{key}")[:36])
        except Exception as exc:
            if not prior or prior["status"] != "verified":
                journal.put("payment", key, source_sha, "unknown", {"reason": str(exc), "kind": kind, "payload": body_json})
            updates.append((n, [f"Held: {exc}", "", now_text()]))
            results.append({**row, "status": "failed", "reason": str(exc)})
            break
        entity = "Payment" if kind == "credit" else "BillPayment"
        made = (resp.json() or {}).get(entity, {}) if getattr(resp, "status_code", 0) == 200 else {}
        live = client.get_json(f"/{'invoice' if kind == 'credit' else 'bill'}/{target['Id']}")
        live = live.get("Invoice" if kind == "credit" else "Bill") or {}
        after = Decimal(str(live.get("Balance") or 0))
        if not made.get("Id") or after != before - parts["amount"]:
            why = (f"QuickBooks did not take it (HTTP {getattr(resp, 'status_code', '?')}: "
                   f"{str(getattr(resp, 'text', ''))[:150]})" if not made.get("Id")
                   else f"posted as payment {made.get('Id')} but the balance is N{after:,.2f}, expected "
                        f"N{before - parts['amount']:,.2f} - check in QuickBooks")
            if made.get("Id"):
                posted[key] = {"payment_id": made["Id"], "at": now_text(), "kind": kind, "doc": target_key,
                               "source_sha": source_sha, "verification_required": True}
                save_posted()
            journal.put("payment", key, source_sha, "unknown", {"reason": why, "kind": kind, "payload": body_json})
            updates.append((n, [f"Held: {why}", made.get("Id", ""), now_text()]))
            results.append({**row, "status": "failed", "reason": why})
            break  # stop on the first problem; the rest wait for the next run
        posted[key] = {"payment_id": made["Id"], "at": now_text(), "kind": kind, "doc": target_key, "source_sha": source_sha}
        save_posted()
        journal.put("payment", key, source_sha, "verified", posted[key])
        qbo.pop("payment_lookup", None)
        left[target_key] = after
        updates.append((n, [POSTED, made["Id"], posted[key]["at"]]))
        results.append({**row, "status": "posted", "payment_id": made["Id"]})
    return updates, results


def run(*, client, write_client=None, sheet=None, env=None, dry_run: bool = False, out: Path | None = None) -> dict:
    s = settings(env)
    res = {"enabled": bool(s["sheet_id"]), "post": s["post"] and not dry_run, "credit": [], "bills": [], "error": ""}
    if not s["sheet_id"]:
        return res
    editor = ""
    if sheet is None:
        from code_scripts.akponora_ops import uf_deposits as ufd
        key = ufd.key_path(ufd.settings())
        sheet = GoogleSheet(s["sheet_id"], key)
        editor = json.loads(Path(key).read_text()).get("client_email", "")
    qbo = load_qbo(client)
    sys_rows = system_rows(qbo)
    credit_rows, bill_rows = sheet.read(CREDIT_PAYMENTS, 11), sheet.read(BILL_PAYMENTS, 11)
    for tab, rows, cols in ((CREDIT_PAYMENTS, credit_rows, CREDIT_PAY_COLS), (BILL_PAYMENTS, bill_rows, BILL_PAY_COLS),
                            (CREDIT_SALES, sheet.read(CREDIT_SALES, 8), CREDIT_SALES_COLS),
                            (BILLS, sheet.read(BILLS, 8), BILLS_COLS), (LISTS, sheet.read(LISTS, 3), LIST_COLS)):
        headers_ok(rows, cols, tab)
    if not dry_run and hasattr(sheet, "ensure_payment_ids"):
        credit_rows = sheet.ensure_payment_ids(CREDIT_PAYMENTS, credit_rows)
        bill_rows = sheet.ensure_payment_ids(BILL_PAYMENTS, bill_rows)
    posted = json.loads(state_path().read_text()) if state_path().exists() else {}
    docs = [clean(b.get("DocNumber")) for b in qbo["bills"]]
    dup = {d for d in docs if d and docs.count(d) > 1}
    invoices = {i["DocNumber"]: i for i in qbo["invoices"] if Decimal(str(i.get("Balance") or 0)) > 0}
    bills = {bill_no(b, dup): b for b in qbo["bills"]}
    wc = None if dry_run else write_client
    def payment_settings(tab):
        result = {**s, "post": res["post"]}
        if not dry_run and hasattr(sheet, "ensure_payment_ids"):
            result["_row_checker"] = lambda identity, sha: current_payment_row(sheet, tab, identity, sha)
        return result
    cu, res["credit"] = process("credit", credit_rows, invoices, qbo, s=payment_settings(CREDIT_PAYMENTS), client=client,
                                write_client=wc, posted=posted)
    bu, res["bills"] = process("bill", bill_rows, bills, qbo, s=payment_settings(BILL_PAYMENTS), client=client,
                               write_client=wc, posted=posted)
    if not dry_run:
        dump_json(state_path(), posted)
        if hasattr(sheet, "ensure_payment_ids"):
            cu = relocate_updates(sheet, CREDIT_PAYMENTS, credit_rows, cu)
            bu = relocate_updates(sheet, BILL_PAYMENTS, bill_rows, bu)
        # system tabs after posting, so balances shown include tonight's payments
        sys_rows = system_rows(load_qbo(client)) if any(r["status"] == "posted" for r in res["credit"] + res["bills"]) else sys_rows
        sheet.replace_body(CREDIT_SALES, 8, sys_rows["credit"])
        sheet.replace_body(BILLS, 8, sys_rows["bills"])
        sheet.replace_body(LISTS, 1, [[d] for d in sys_rows["open_invoices"]], first_col="B")
        sheet.replace_body(LISTS, 1, [[d] for d in sys_rows["open_bills"]], first_col="C")
        sheet.write_cells([(f"'{CREDIT_PAYMENTS}'!H{n}:J{n}", v) for n, v in cu] +
                          [(f"'{BILL_PAYMENTS}'!H{n}:J{n}", v) for n, v in bu])
        # rows that became Posted this run (or got their mark back) are locked and greyed for staff
        for tab, ups in ((CREDIT_PAYMENTS, cu), (BILL_PAYMENTS, bu)):
            sheet.lock_rows(tab, [n for n, v in ups if v[0] == POSTED], editor or "oiat-sheets-reader@oiat-ops.iam.gserviceaccount.com")
    res["open_credit"] = len(sys_rows["open_invoices"])
    res["open_bills"] = len(sys_rows["open_bills"])
    if out:
        dump_json(Path(out) / "summary.json", res)
    return res

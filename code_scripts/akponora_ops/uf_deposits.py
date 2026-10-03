"""Daily Undeposited Funds deposits from the Nora Mart till sheet (company_a, step 5 of daily_run).

Realm 9341455406194328 (production). Moves each business day's Company A SalesReceipts out of
Undeposited Funds (``100900``, QBO Id 72) into the banks the till sheet says the money went to,
by the method used on 26 Sep 2026 for 1 Jan-24 Sep (``akponora_cutover/archive/uf_reverse_and_allocate``):
"deposits follow the receipts; the mix follows the sheet".

Days are independent (owner, 3 Oct 2026). Every run looks at each business day from the floor
2026-09-25 up to the run's business date that is not yet DEPOSITED in
``STATE_ROOT/ops/company_a/uf_deposits/days.json`` and posts each day whose gates pass; a held or
missing day never blocks a later one and is simply re-evaluated next run. Per-day state:
DEPOSITED (final, never re-planned) / READY (plan only) / HELD (reason) / WAITING_SHEET (sheet
block missing or unfinished) / NO_SALES (no SalesReceipts yet). An old ``cursor.json`` is migrated
on first read (floor .. its last_complete_business_date = DEPOSITED); it is not written any more.

Per business day:

1. Receipts: that day's SalesReceipts (TxnDate = day, TotalAmt > 0) still in Undeposited Funds
   (DepositToAccountRef 72 or unset) and not linked to any Deposit. Receipts already in a deposit
   made by this tool (DocNumber ``UF<yymmdd><bank no>``) count as done; receipts deposited any
   other way hold the day (unless every receipt of the day was, then the day is done).
2. Sheet: the day's block of the "Nora Mart Daily Sales Account Breakdown" Google Sheet (live via
   a read-only service account, or ``--sheet-xlsx``). Each box is mapped to a QBO bank through
   ``STATE_ROOT/mappings/company_a/till_accounts.csv`` (by terminal TID; CASH by Kind ``cash``).
3. Gates (any failure holds that day only): sheet day present; no
   non-numeric box; both CASH boxes and SYSTEM filled; boxes total > 0; every filled box mapped
   to an active row; |sheet total - receipts total| <= max(OIAT_COMPANY_A_UF_TOLERANCE (N1,000),
   OIAT_COMPANY_A_UF_TOLERANCE_PCT (0.5) % of receipts) - read fresh every run from the env and
   then from ``STATE_ROOT/ops/company_a/uf_deposits/settings.env`` (no restart needed); bank
   accounts exist, active, Bank type,
   numbers as in the mapping; the day is after the QBO closing date; no conflicting deposit /
   true-up already in QBO; in automatic mode, receipts total <= OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL
   (N15,000,000).
4. Allocation (whole receipts; QBO cannot deposit part of a receipt):
   * target per bank = sheet amount x receipts total / sheet total (cents; rounding residue to the
     bank with the largest sheet amount);
   * receipts are placed largest first, by tender: Cash -> cash banks, Card -> card banks,
     Transfer -> transfer banks (Kind column), mixed tenders (``Card/Cash`` ...) -> the union of
     their pools; within the pool the bank whose remaining target fits the receipt best, else the
     bank with the most target left (= sheet proportions);
   * one Bank Deposit per bank (tag ``UF<yymmdd><bank no>``, sent as DocNumber and in the memo; QBO drops
     the DocNumber on Deposits here, so the memo tag ``| UF... ->`` is what identifies them), ``LinkedTxn`` SalesReceipt with
     ``TxnLineId 0``, minorversion 65 - the form that worked on 26 Sep);
   * then Bank->Bank true-up Transfers (largest surplus to largest deficit) so every bank's day
     total equals its target exactly. No transfer when the receipts already fit. Each transfer's
     PrivateNote carries ``UFTU <day> <from no>><to no>`` so reruns find it.
   Every Deposit / Transfer PrivateNote starts ``UF deposit <day> from till sheet; approval <ref>``.

Subcommands
-----------
``plan`` (READ-ONLY: sheet + QBO GET). ``--date D`` for one day, else ``--from`` (default: every
    day since the floor not yet DEPOSITED) ``--to`` (default last closed Lagos business day).
    Writes ``<out>/<day>/plan.json``,
    ``review.csv`` (per bank), ``receipts.csv``, ``payloads.jsonl`` and ``summary.json`` (sha256 of
    the payloads), plus ``<out>/summary.json`` for the window.
``post`` (WRITES; needs a chat yes): ``--plan-dir <out>/<day> --approval-ref '<chat yes>'
    --expect-sha <payloads_sha256>``. Re-checks every receipt and DocNumber live, posts the
    deposits then the transfers, re-reads and verifies each, writes ``results.csv`` (resumable)
    and marks the day DEPOSITED in days.json once fully done.
``scheduled``: plan, then post every READY day when ``OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1``,
    ``OIAT_COMPANY_A_UF_AUTO_POST=1`` and ``OIAT_COMPANY_A_UF_APPROVAL_REF`` are all set; otherwise
    plan only. A post that stops (QBO error / failed verification) ends posting for the run.
    Exit 0 = nothing waiting, 3 = days wait (HELD / WAITING_SHEET / NO_SALES / READY), 2 = stopped.
    Writes the till-sheet status report into ``summary.json`` / ``scheduled.json``.
``status`` (READ-ONLY: sheet + days.json, no QBO): last day entered on the till sheet, missing /
    incomplete days since the floor, complete days not yet deposited, deposited days, and the
    per-day state.

Sheet access: ``OIAT_COMPANY_A_TILL_SHEET_SA_KEY`` (default STATE_ROOT/secrets/google_service_account.json)
and ``OIAT_COMPANY_A_TILL_SHEET_ID``. See docs/SERVER_SETUP.md "Till sheet access".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import till_sheet
from code_scripts.akponora_ops.common import (
    COMPANY, REALM, dump_json, read_csv, run_dir, send_slack, sha256_file, state_dir, write_csv,
)
from code_scripts.scripts.akponora_cutover._common import business_date, setup_env
from code_scripts.scripts.akponora_cutover.w7_create_items import (
    D, QBOClient, StopRun, canonical_json, now_iso, q, qbo_escape, sha256_text,
)

TOOL = "uf_deposits"
TZ = ZoneInfo("Africa/Lagos")
FLOOR = "2026-09-25"  # 1 Jan-24 Sep deposited on 26 Sep 2026
UF_ACCOUNT_ID = "72"
UF_ACCOUNT_NUMBER = "100900"
DEPOSIT_MINORVERSION = "65"
REF_PLACEHOLDER = "<APPROVAL_REF>"
KINDS = ("cash", "card", "transfer")
CENT = Decimal("0.01")

ENABLED_ENV = "OIAT_COMPANY_A_UF_DEPOSIT_ENABLED"
AUTO_ENV = "OIAT_COMPANY_A_UF_AUTO_POST"
REF_ENV = "OIAT_COMPANY_A_UF_APPROVAL_REF"
TOL_ENV = "OIAT_COMPANY_A_UF_TOLERANCE"
TOL_PCT_ENV = "OIAT_COMPANY_A_UF_TOLERANCE_PCT"
CAP_ENV = "OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL"
KEY_ENV = "OIAT_COMPANY_A_TILL_SHEET_SA_KEY"
SHEET_ENV = "OIAT_COMPANY_A_TILL_SHEET_ID"
ACCOUNTS_ENV = "OIAT_COMPANY_A_TILL_ACCOUNTS_FILE"

ACCOUNT_COLS = ["Till sheet line", "Terminal / TID", "QBO account number", "QBO account Id", "Kind", "Active", "Note"]
REVIEW_COLS = ["Day", "Bank No", "QBO Account Id", "Kind", "Sheet Lines", "Sheet Amount", "Target", "Deposited",
               "Receipts", "Deposit DocNumbers", "Transfer Out", "Transfer In", "Final", "Final - Target", "Status"]
RECEIPT_COLS = ["Day", "Receipt Id", "DocNumber", "Amount", "Tender", "State", "Bank No", "Deposit DocNumber"]
RESULT_COLS = ["ts", "day", "kind", "key", "status", "qbo_id", "amount", "payload_sha256", "requestid",
               "approval_ref", "mode", "detail"]
DONE_RESULTS = {"POSTED", "ADOPTED"}
READY, DONE, HOLD = "READY", "DONE", "HOLD"  # plan status of one day folder
# stored per-day state (days.json) and the scheduled run's day status
DEPOSITED, HELD, WAITING_SHEET, NO_SALES = "DEPOSITED", "HELD", "WAITING_SHEET", "NO_SALES"
OPEN_STATES = (READY, HELD, WAITING_SHEET, NO_SALES)
TAG_RE = re.compile(r"UFTU (\d{4}-\d{2}-\d{2}) (\d+)>(\d+)")


# ---------------------------------------------------------------- small helpers
def r2(value) -> Decimal:
    return q(Decimal(value), "0.01")


def money(value) -> str:
    return f"{Decimal(value):.2f}"


def naira(value) -> str:
    return f"N{Decimal(value):,.2f}"


def as_list(rows) -> list:
    if rows is None:
        return []
    return rows if isinstance(rows, list) else [rows]


def clean(value) -> str:
    return str(value if value is not None else "").strip()


def truthy(value) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "on", "y"}


def yymmdd(day: str) -> str:
    return day[2:4] + day[5:7] + day[8:10]


def deposit_doc(day: str, bank_no: str, n: int = 1) -> str:
    doc = f"UF{yymmdd(day)}{bank_no}" + (f"-{n}" if n > 1 else "")
    if len(doc) > 21:
        raise StopRun(f"DocNumber {doc} longer than 21 characters")
    return doc


def is_own_deposit_doc(doc: str, day: str | None = None) -> bool:
    m = re.fullmatch(r"UF(\d{6})\d{6}(-\d+)?", clean(doc))
    return bool(m) and (day is None or m.group(1) == yymmdd(day))


def deposit_key(dep: dict) -> str:
    """Our tag for a Deposit: its DocNumber, else the ``| UF<yymmdd><bank no>[-n] ->`` tag in the memo.

    This QBO company does not keep DocNumber on Deposits (found 3 Oct 2026: Deposit 80514 and the 26 Sep
    deposits read back with DocNumber null), so the memo tag is what identifies our deposits."""
    doc = clean(dep.get("DocNumber"))
    if doc:
        return doc
    m = re.search(r"\| (UF\d{12}(?:-\d+)?) ->", clean(dep.get("PrivateNote")))
    return m.group(1) if m else ""


def own_deposits(client: QBOClient, day: str, key: str) -> list[dict]:
    """Live Deposits dated ``day`` that carry ``key`` (DocNumber or memo tag)."""
    rows = client.query_all(f"select * from Deposit where TxnDate = '{day}'", "Deposit")
    return [d for d in rows if deposit_key(d) == key]


def transfer_tag(day: str, src_no: str, dst_no: str) -> str:
    return f"UFTU {day} {src_no}>{dst_no}"


def requestid_for(key: str, payload_sha: str) -> str:
    return sha256_text(f"{TOOL}|{REALM}|{key}|{payload_sha}")[:36]


def note_head(day: str) -> str:
    return f"UF deposit {day} from till sheet; approval {REF_PLACEHOLDER}"


def with_ref(payload: dict, approval_ref: str) -> dict:
    out = json.loads(json.dumps(payload))
    out["PrivateNote"] = out.get("PrivateNote", "").replace(REF_PLACEHOLDER, clean(approval_ref)[:200])[:4000]
    return out


def lagos_now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def last_closed_day(now: datetime | None = None) -> str:
    return (business_date(now or lagos_now()) - timedelta(days=1)).isoformat()


def day_range(start: str, end: str) -> list[str]:
    out, d, last = [], date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- settings
def overrides_path() -> Path:
    """``STATE_ROOT/ops/company_a/uf_deposits/settings.env``: KEY=VALUE lines read at the start of
    every run (no restart). Only the tolerance keys are honoured."""
    from code_scripts.paths import STATE_ROOT

    return Path(STATE_ROOT) / "ops" / COMPANY / TOOL / "settings.env"


RUNTIME_KEYS = (TOL_ENV, TOL_PCT_ENV)


def runtime_overrides() -> dict:
    path = overrides_path()
    if not path.exists():
        return {}
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key in RUNTIME_KEYS and value:
            out[key] = value
    return out


def _dec(env, key: str, default: str) -> Decimal:
    text = clean(env.get(key)) or default
    try:
        value = Decimal(text)
    except ArithmeticError:
        raise StopRun(f"{key}={text!r} is not a number") from None
    if not value.is_finite() or value < 0:
        raise StopRun(f"{key}={text!r} must be a number >= 0")
    return value


def settings(env=None) -> dict:
    """Read fresh on every call (every run): the process env (``.env``) and then
    ``settings.env`` under the state dir for the tolerance keys."""
    env = dict(os.environ if env is None else env)
    over = runtime_overrides()
    env.update(over)
    enabled, auto, ref = truthy(env.get(ENABLED_ENV)), truthy(env.get(AUTO_ENV)), clean(env.get(REF_ENV))
    return {
        "enabled": enabled, "auto_flag": auto, "ref": ref, "auto": bool(enabled and auto and ref),
        "tol_abs": _dec(env, TOL_ENV, "1000"),
        "tol_pct": _dec(env, TOL_PCT_ENV, "0.5"),
        "tol_source": f"{overrides_path()}" if over else "environment",
        "cap": _dec(env, CAP_ENV, "15000000"),
        "sheet_id": clean(env.get(SHEET_ENV)) or till_sheet.DEFAULT_SHEET_ID,
        "key_path": clean(env.get(KEY_ENV)),
        "accounts_path": clean(env.get(ACCOUNTS_ENV)),
    }


def tolerance(receipts_total: Decimal, s: dict) -> Decimal:
    return max(s["tol_abs"], r2(receipts_total * s["tol_pct"] / Decimal(100)))


def _state_root() -> Path:
    setup_env()
    from code_scripts.paths import STATE_ROOT

    return Path(STATE_ROOT)


def accounts_path(s: dict | None = None) -> Path:
    explicit = (s or {}).get("accounts_path") or clean(os.getenv(ACCOUNTS_ENV))
    return Path(explicit) if explicit else _state_root() / "mappings" / COMPANY / "till_accounts.csv"


def key_path(s: dict | None = None) -> Path:
    explicit = (s or {}).get("key_path") or clean(os.getenv(KEY_ENV))
    return Path(explicit) if explicit else _state_root() / "secrets" / "google_service_account.json"


def sheet_source(s: dict, *, xlsx: str | Path | None = None):
    if xlsx:
        return till_sheet.XlsxSheetSource(xlsx)
    return till_sheet.GoogleSheetSource(s["sheet_id"], key_path(s))


# ---------------------------------------------------------------- per-day state (days.json)
def day_state(status: str, hold_kind: str = "") -> str:
    """Plan status -> the stored per-day state."""
    if status == DONE:
        return DEPOSITED
    if status == HOLD:
        return {"no_sales": NO_SALES, "sheet": WAITING_SHEET}.get(hold_kind, HELD)
    return status  # READY (plan only / not posted)


def days_path() -> Path:
    return state_dir(TOOL) / "days.json"


def cursor_path() -> Path:
    """Old single forward cursor (before 3 Oct 2026); read only to migrate."""
    return state_dir(TOOL) / "cursor.json"


def read_cursor() -> dict:
    path = cursor_path()
    return json.loads(path.read_text()) if path.exists() else {}


def migrate_cursor() -> dict:
    """A fresh state; every day from the floor up to an old cursor.json counts as DEPOSITED."""
    state = {"floor": FLOOR, "days": {}}
    last = clean(read_cursor().get("last_complete_business_date"))
    if last and last >= FLOOR:
        for d in day_range(FLOOR, last):
            state["days"][d] = {"status": DEPOSITED, "reason": f"migrated from cursor.json (done up to {last})",
                                "updated_at": now_iso()}
        state["migrated_from_cursor"] = {"last_complete_business_date": last, "at": now_iso()}
    return state


def read_state() -> dict:
    path = days_path()
    if path.exists():
        state = json.loads(path.read_text())
        state.setdefault("floor", FLOOR)
        state.setdefault("days", {})
        return state
    return migrate_cursor()


def write_state(state: dict) -> None:
    dump_json(days_path(), {**state, "floor": FLOOR, "updated_at": now_iso(),
                            "days": dict(sorted(state["days"].items()))})


def is_deposited(state: dict, day: str) -> bool:
    return (state["days"].get(day) or {}).get("status") == DEPOSITED


def open_days(state: dict, start: str, end: str) -> list[str]:
    """Days in start..end that are not yet DEPOSITED (each is re-evaluated every run)."""
    return [d for d in day_range(start, end) if not is_deposited(state, d)] if start <= end else []


def default_from(state: dict | None = None) -> str:
    """First day from the floor that is not DEPOSITED."""
    state = read_state() if state is None else state
    day = FLOOR
    while is_deposited(state, day):
        day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return day


def record_day(state: dict, day: str, status: str, *, reasons=(), **extra) -> None:
    entry = {"status": status, "reason": (list(reasons) or [""])[0], "updated_at": now_iso()}
    if status != DEPOSITED and reasons:
        entry["reasons"] = list(reasons)[:10]
    entry.update({k: v for k, v in extra.items() if v not in (None, "")})
    state["days"][day] = entry


# ---------------------------------------------------------------- till-sheet status report
def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {d.strftime('%b')}"


def fmt_days(days: list[str]) -> str:
    """'25, 26, 29 Sep; 1-4 Oct' (runs of 3+ days collapse to a range)."""
    if not days:
        return "none"
    days = sorted(days)
    groups: list[list[date]] = []
    for d in (date.fromisoformat(x) for x in days):
        if groups and groups[-1][-1].month == d.month and (d - groups[-1][-1]).days == 1:
            groups[-1].append(d)
        else:
            groups.append([d])
    by_month: dict[str, list[str]] = {}
    for g in groups:
        key = g[0].strftime("%b")
        if len(g) >= 3:
            by_month.setdefault(key, []).append(f"{g[0].day}-{g[-1].day}")
        else:
            by_month.setdefault(key, []).extend(str(x.day) for x in g)
    return "; ".join(f"{', '.join(v)} {k}" for k, v in by_month.items())


def sheet_report(source, *, upto: str, state: dict) -> dict:
    """Till-sheet status since the floor: last complete day, missing / incomplete days, days
    complete on the sheet but not deposited, days deposited. Reads the sheet only (no QBO)."""
    days = day_range(FLOOR, upto) if FLOOR <= upto else []
    complete, missing, incomplete = [], [], []
    for d in days:
        status, why = till_sheet.day_completeness(source, d)
        if status == till_sheet.COMPLETE:
            complete.append(d)
        elif status == till_sheet.MISSING:
            missing.append(d)
        else:
            incomplete.append({"day": d, "reason": (why or [""])[0]})
    deposited = [d for d in days if is_deposited(state, d)]
    waiting = [{"day": d, "status": (state["days"].get(d) or {}).get("status") or "NOT RUN",
                "reason": (state["days"].get(d) or {}).get("reason", "")}
               for d in complete if not is_deposited(state, d)]
    report = {"as_of": upto, "floor": FLOOR, "last_complete_day": complete[-1] if complete else None,
              "missing": missing, "incomplete": incomplete, "complete_not_deposited": waiting,
              "deposited": deposited}
    report["text"] = report_text(report)
    return report


def report_text(r: dict) -> str:
    if r.get("error"):
        return f"Till sheet: status unavailable ({r['error'][:160]})."
    last = fmt_day(r["last_complete_day"]) if r.get("last_complete_day") else "none since " + fmt_day(r["floor"])
    text = f"Till sheet: last day entered {last}. Missing: {fmt_days(r['missing'])}."
    if r["incomplete"]:
        text += f" Incomplete: {fmt_days([x['day'] for x in r['incomplete']])}."
    text += f" Waiting to deposit: {fmt_days([x['day'] for x in r['complete_not_deposited']])}."
    text += f" Deposited: {fmt_days(r['deposited'])}."
    return text


# ---------------------------------------------------------------- till accounts mapping
def norm_tid(value) -> str:
    return re.sub(r"\D", "", clean(value))


def load_accounts(path: Path) -> dict:
    """Active rows of till_accounts.csv -> {by_tid, cash, banks{id: {id, number, kind, lines}}, inactive_tids,
    problems, sha256}. A missing file or a bad row raises StopRun (configuration error)."""
    if not Path(path).exists():
        raise StopRun(f"till accounts mapping {path} not found (seeded from templates/till_accounts_company_a.csv)")
    rows = read_csv(path)
    if rows and set(ACCOUNT_COLS) - set(rows[0]):
        raise StopRun(f"{path}: missing column(s) {sorted(set(ACCOUNT_COLS) - set(rows[0]))}")
    by_tid, banks, inactive, cash = {}, {}, set(), None
    for n, r in enumerate(rows, start=2):
        kind = clean(r["Kind"]).lower()
        tid = norm_tid(r["Terminal / TID"])
        acct_id, acct_no = clean(r["QBO account Id"]), clean(r["QBO account number"])
        if not truthy(r["Active"]):
            if tid:
                inactive.add(tid)
            continue
        if kind not in KINDS:
            raise StopRun(f"{path} line {n}: Kind {r['Kind']!r} is not cash|card|transfer")
        if not acct_id or not acct_no:
            raise StopRun(f"{path} line {n}: QBO account number and Id are required")
        if acct_id == UF_ACCOUNT_ID:
            raise StopRun(f"{path} line {n}: Undeposited Funds cannot be a deposit target")
        if kind == "cash":
            if cash is not None:
                raise StopRun(f"{path}: more than one active cash row")
            cash = r
        elif not tid:
            raise StopRun(f"{path} line {n}: card/transfer rows need a Terminal / TID")
        if tid:
            if tid in by_tid:
                raise StopRun(f"{path}: TID {tid} appears on more than one active row")
            by_tid[tid] = r
        bank = banks.setdefault(acct_id, {"id": acct_id, "number": acct_no, "kind": kind, "lines": []})
        if bank["number"] != acct_no:
            raise StopRun(f"{path}: QBO account Id {acct_id} has two account numbers")
        if bank["kind"] != kind:
            raise StopRun(f"{path}: QBO account {acct_no} is both {bank['kind']} and {kind}")
        bank["lines"].append(clean(r["Till sheet line"]))
    return {"by_tid": by_tid, "cash": cash, "banks": banks, "inactive_tids": inactive, "path": str(path),
            "sha256": sha256_file(path)}


def resolve_sheet(day: dict, accounts: dict) -> tuple[dict, list, list]:
    """(sheet amount per bank id, box rows, reasons). Blank / zero boxes never need a mapping."""
    amounts, rows, reasons = defaultdict(Decimal), [], []
    for box in day["boxes"]:
        value = box["value"]
        row, why = None, ""
        if box["kind"] == "cash":
            row = accounts["cash"]
            why = "" if row else "no active cash row in till_accounts.csv"
        elif box["tid"]:
            row = accounts["by_tid"].get(box["tid"])
            if row is None:
                why = (f"TID {box['tid']} is marked Active=no in till_accounts.csv" if box["tid"] in accounts["inactive_tids"]
                       else f"TID {box['tid']} not in till_accounts.csv")
        else:
            why = "unknown till line (no terminal number) - add it to till_accounts.csv / fix the sheet"
        acct = clean(row["QBO account Id"]) if row else ""
        rows.append({**box, "bank_id": acct, "unmapped": why})
        if value is None or value == 0:
            continue
        if why:
            reasons.append(f"till line '{box['label']}' = {naira(value)}: {why}")
            continue
        if value < 0:
            reasons.append(f"till line '{box['label']}' is negative ({naira(value)})")
            continue
        amounts[acct] += value
    return {k: r2(v) for k, v in amounts.items()}, rows, reasons


def sheet_reasons(day: dict) -> list[str]:
    return till_sheet.completeness_reasons(day)


# ---------------------------------------------------------------- receipts
def tender_kinds(sr: dict) -> tuple[str, set]:
    """(tender text, {cash, card, transfer}) from the PaymentMethod name, else the memo."""
    pm = clean((sr.get("PaymentMethodRef") or {}).get("name"))
    memo = clean(sr.get("PrivateNote"))
    for text in (pm, memo):
        found = set()
        for token in re.findall(r"cash|card|transfer|pos|bank", text.lower()):
            found.add({"pos": "card", "bank": "transfer"}.get(token, token))
        if found:
            return text, found
    return pm or memo, set()


def deposit_links(deposits: list[dict]) -> dict:
    """{SalesReceipt Id: deposit info} from every Deposit line's LinkedTxn."""
    out = {}
    for dep in deposits:
        info = {"deposit_id": clean(dep.get("Id")), "doc": deposit_key(dep),
                "bank_id": clean((dep.get("DepositToAccountRef") or {}).get("value")),
                "txn_date": clean(dep.get("TxnDate"))}
        for line in dep.get("Line") or []:
            for link in line.get("LinkedTxn") or []:
                if clean(link.get("TxnType")) == "SalesReceipt" and link.get("TxnId"):
                    out[clean(link["TxnId"])] = info
    return out


def classify_receipts(day: str, srs: list[dict], links: dict) -> dict:
    pending, fixed, outside, zero = [], [], [], []
    for sr in sorted(srs, key=lambda s: int(clean(s.get("Id")) or 0)):
        amt = r2(D(sr.get("TotalAmt"), Decimal(0)))
        text, kinds = tender_kinds(sr)
        rec = {"id": clean(sr.get("Id")), "doc": clean(sr.get("DocNumber")), "amount": amt, "tender": text,
               "kinds": sorted(kinds), "sync": clean(sr.get("SyncToken"))}
        if amt <= 0:
            zero.append(rec)
            continue
        link = links.get(rec["id"])
        dep_to = clean((sr.get("DepositToAccountRef") or {}).get("value"))
        sr_dep = [ln for ln in sr.get("LinkedTxn") or [] if clean(ln.get("TxnType")) == "Deposit"]
        if link and is_own_deposit_doc(link["doc"], day):
            fixed.append({**rec, "bank_id": link["bank_id"], "deposit_doc": link["doc"],
                          "deposit_id": link["deposit_id"]})
        elif link:
            outside.append({**rec, "why": f"in Deposit {link['deposit_id']} {link['doc'] or '(no DocNumber)'} "
                                          f"{link['txn_date']}"})
        elif sr_dep:
            outside.append({**rec, "why": f"linked to Deposit {clean(sr_dep[0].get('TxnId'))}"})
        elif dep_to and dep_to != UF_ACCOUNT_ID:
            outside.append({**rec, "why": f"deposited straight to account {dep_to}"})
        else:
            pending.append(rec)
    return {"pending": pending, "fixed": fixed, "outside": outside, "zero": zero}


# ---------------------------------------------------------------- allocation (pure)
def scale_targets(sheet: dict, total: Decimal, banks: dict) -> dict:
    """Sheet amount per bank scaled to ``total`` (cents); the residue goes to the largest sheet bank."""
    s_total = sum(sheet.values(), Decimal(0))
    if s_total <= 0 or total <= 0:
        return {}
    out = {b: r2(v * total / s_total) for b, v in sheet.items() if v > 0}
    residue = total - sum(out.values(), Decimal(0))
    if residue:
        big = sorted(out, key=lambda b: (-sheet[b], banks.get(b, {}).get("number", b)))[0]
        out[big] += residue
    return out


def allocate(pending: list[dict], fixed: list[dict], sheet: dict, banks: dict) -> dict:
    """Assign whole receipts to banks by tender and sheet share, then the true-up transfers.

    Returns {targets, assigned {receipt id: bank id}, deposited {bank: amount}, transfers [{from, to,
    amount}], final {bank: amount}}. ``final`` equals ``targets`` for every bank."""
    number = lambda b: banks.get(b, {}).get("number", b)  # noqa: E731
    total = sum((r["amount"] for r in pending + fixed), Decimal(0))
    targets = scale_targets(sheet, total, banks)
    remaining = {b: targets.get(b, Decimal(0)) for b in banks}
    deposited = defaultdict(Decimal)
    for r in fixed:
        remaining[r["bank_id"]] = remaining.get(r["bank_id"], Decimal(0)) - r["amount"]
        deposited[r["bank_id"]] += r["amount"]
    order = sorted(banks, key=number)
    assigned = {}
    for r in sorted(pending, key=lambda x: (len(x["kinds"]) != 1, -x["amount"], int(x["id"] or 0))):
        pool = [b for b in order if banks[b]["kind"] in r["kinds"]] or list(order)
        live = [b for b in pool if remaining[b] > 0] or pool
        fits = [b for b in live if remaining[b] >= r["amount"]]
        if fits:
            bank = min(fits, key=lambda b: (remaining[b] - r["amount"], number(b)))
        else:
            bank = max(live, key=lambda b: remaining[b])  # first max in account-number order
        assigned[r["id"]] = bank
        remaining[bank] -= r["amount"]
        deposited[bank] += r["amount"]
    surplus = {b: deposited.get(b, Decimal(0)) - targets.get(b, Decimal(0)) for b in set(deposited) | set(targets)}
    sources = sorted([[b, v] for b, v in surplus.items() if v > 0], key=lambda x: (-x[1], number(x[0])))
    sinks = sorted([[b, -v] for b, v in surplus.items() if v < 0], key=lambda x: (-x[1], number(x[0])))
    transfers, i, j = [], 0, 0
    while i < len(sources) and j < len(sinks):
        move = min(sources[i][1], sinks[j][1])
        if move > 0:
            transfers.append({"from": sources[i][0], "to": sinks[j][0], "amount": move})
        sources[i][1] -= move
        sinks[j][1] -= move
        if sources[i][1] == 0:
            i += 1
        if sinks[j][1] == 0:
            j += 1
    final = defaultdict(Decimal, deposited)
    for t in transfers:
        final[t["from"]] -= t["amount"]
        final[t["to"]] += t["amount"]
    return {"total": total, "targets": targets, "assigned": assigned, "deposited": dict(deposited),
            "transfers": transfers, "final": {b: v for b, v in final.items() if v or b in targets}}


# ---------------------------------------------------------------- QBO context (GET only)
def fetch_context(client: QBOClient, days: list[str], banks: dict) -> dict:
    first, last = days[0], days[-1]
    srs = client.query_all(f"select * from SalesReceipt where TxnDate >= '{first}' and TxnDate <= '{last}'",
                           "SalesReceipt")
    by_day = defaultdict(list)
    for sr in srs:
        by_day[clean(sr.get("TxnDate"))].append(sr)
    deposits = client.query_all(f"select * from Deposit where TxnDate >= '{first}'", "Deposit")
    transfers = client.query_all(f"select * from Transfer where TxnDate >= '{first}' and TxnDate <= '{last}'",
                                 "Transfer")
    ids = sorted(set(banks) | {UF_ACCOUNT_ID})
    accts = as_list(client.query("select * from Account where Id in (" + ",".join(f"'{qbo_escape(i)}'" for i in ids)
                                 + ")").get("Account"))
    prefs = client.get_json("/preferences").get("Preferences", {})
    return {"receipts": by_day, "deposits": deposits, "transfers": transfers,
            "accounts": {clean(a.get("Id")): a for a in accts},
            "book_close": clean((prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate"))}


def account_problems(bank_ids, banks: dict, accounts: dict) -> list[str]:
    out = []
    for b in sorted(bank_ids, key=lambda x: banks.get(x, {}).get("number", x)):
        meta, live = banks.get(b, {}), accounts.get(b)
        if live is None:
            out.append(f"QBO account Id {b} ({meta.get('number')}) not found")
            continue
        if live.get("Active") is False:
            out.append(f"QBO account {meta.get('number')} (Id {b}) is inactive")
        if clean(live.get("AccountType")) and clean(live.get("AccountType")) not in ("Bank", "Other Current Asset"):
            out.append(f"QBO account {meta.get('number')} (Id {b}) is {live.get('AccountType')}, not Bank")
        if clean(live.get("AcctNum")) and clean(live.get("AcctNum")) != meta.get("number"):
            out.append(f"QBO account Id {b} has number {live.get('AcctNum')}, till_accounts.csv says {meta.get('number')}")
    return out


def own_transfers(transfers: list[dict], day: str) -> dict:
    out = {}
    for t in transfers:
        m = TAG_RE.search(clean(t.get("PrivateNote")))
        if m and m.group(1) == day:
            out[transfer_tag(day, m.group(2), m.group(3))] = t
    return out


# ---------------------------------------------------------------- plan one day (pure apart from the sheet)
def plan_day(day: str, *, source, accounts: dict, ctx: dict, s: dict, auto_cap: Decimal | None = None) -> dict:
    banks = accounts["banks"]
    e = {"day": day, "status": "", "reasons": [], "warnings": [], "sheet": None, "sheet_sha256": "",
         "sheet_total": Decimal(0), "receipts_total": Decimal(0), "receipts": {}, "boxes": [], "alloc": None,
         "actions": [], "sheet_by_bank": {}, "hold_kind": ""}
    reasons, warns = e["reasons"], e["warnings"]
    rec = classify_receipts(day, ctx["receipts"].get(day, []), deposit_links(ctx["deposits"]))
    e["receipts"] = rec
    if rec["zero"]:
        warns.append(f"{len(rec['zero'])} receipt(s) with a zero total ignored")
    active = rec["pending"] + rec["fixed"]
    e["receipts_total"] = sum((r["amount"] for r in active), Decimal(0))
    if not active and not rec["outside"]:
        e["status"], e["hold_kind"] = HOLD, "no_sales"
        reasons.append(f"no SalesReceipts in QBO for {day} yet (sales not posted?)")
        return e
    if rec["outside"]:
        if not active:
            e["status"] = DONE
            reasons.append(f"all {len(rec['outside'])} receipt(s) already deposited outside this tool")
            return e
        reasons.append(f"{len(rec['outside'])} receipt(s) already deposited outside this tool - resolve by hand: "
                       + "; ".join(f"{r['doc'] or r['id']} {naira(r['amount'])} {r['why']}" for r in rec["outside"][:5]))
    sheet_day, why, sha = till_sheet.find_day(source, day)
    e["sheet_sha256"] = sha
    if sheet_day is None:
        e["status"], e["hold_kind"] = HOLD, "sheet"
        reasons.append(why)
        return e
    e["sheet"] = {k: sheet_day[k] for k in ("date", "tab", "row", "actual", "system", "excess")}
    unfinished = sheet_reasons(sheet_day)
    reasons += unfinished
    if unfinished:
        e["hold_kind"] = "sheet"
    sheet_by_bank, e["boxes"], map_reasons = resolve_sheet(sheet_day, accounts)
    reasons += map_reasons
    e["sheet_by_bank"] = sheet_by_bank
    e["sheet_total"] = sum(sheet_by_bank.values(), Decimal(0))
    diff = e["sheet_total"] - e["receipts_total"]
    tol = tolerance(e["receipts_total"], s)
    if abs(diff) > tol:
        reasons.append(f"sheet total {naira(e['sheet_total'])} vs receipts {naira(e['receipts_total'])}: "
                       f"difference {naira(diff)} is over the tolerance {naira(tol)}")
    elif diff:
        warns.append(f"sheet total {naira(e['sheet_total'])} vs receipts {naira(e['receipts_total'])} "
                     f"(difference {naira(diff)}, within {naira(tol)}); deposits follow the receipts")
    if sheet_day.get("system") is not None and abs(Decimal(sheet_day["system"]) - e["receipts_total"]) > tol:
        warns.append(f"sheet SYSTEM {naira(sheet_day['system'])} differs from the receipts {naira(e['receipts_total'])}")
    if ctx.get("book_close") and day <= ctx["book_close"]:
        reasons.append(f"{day} is inside the QBO closed period (BookCloseDate {ctx['book_close']})")
    used = set(sheet_by_bank) | {r["bank_id"] for r in rec["fixed"]}
    reasons += account_problems(used, banks, ctx["accounts"])
    unknown_fixed = [r for r in rec["fixed"] if r["bank_id"] not in banks]
    if unknown_fixed:
        reasons.append(f"existing UF deposit(s) {sorted({r['deposit_doc'] for r in unknown_fixed})} go to a bank "
                       "that is not in till_accounts.csv")
    if auto_cap is not None and e["receipts_total"] > auto_cap:
        reasons.append(f"receipts total {naira(e['receipts_total'])} is over the automatic cap {naira(auto_cap)} "
                       f"({CAP_ENV}) - post by hand with a chat yes")
    if reasons:
        e["status"] = HOLD
        return e
    alloc = allocate(rec["pending"], rec["fixed"], sheet_by_bank, banks)
    e["alloc"] = alloc
    e["actions"], conflicts = build_actions(day, rec, alloc, banks, ctx)
    if conflicts:
        e["status"] = HOLD
        reasons += conflicts
        return e
    e["status"] = READY if any(a["state"] == "NEW" for a in e["actions"]) else DONE
    if e["status"] == DONE:
        reasons.append("already deposited by uf_deposits (all deposits and true-ups exist)")
    return e


def build_actions(day: str, rec: dict, alloc: dict, banks: dict, ctx: dict) -> tuple[list, list]:
    actions, conflicts = [], []
    existing_docs = {deposit_key(d): d for d in ctx["deposits"] if deposit_key(d)}
    by_bank_new = defaultdict(list)
    pending = {r["id"]: r for r in rec["pending"]}
    for rid, bank in alloc["assigned"].items():
        by_bank_new[bank].append(pending[rid])
    for r in rec["fixed"]:
        actions.append({"kind": "deposit", "key": r["deposit_doc"], "state": "EXISTS", "bank_id": r["bank_id"],
                        "qbo_id": r["deposit_id"], "receipt_ids": [r["id"]], "amount": r["amount"]})
    for bank in sorted(by_bank_new, key=lambda b: banks[b]["number"]):
        group = sorted(by_bank_new[bank], key=lambda r: int(r["id"] or 0))
        n, doc = 1, deposit_doc(day, banks[bank]["number"])
        while doc in existing_docs:
            n += 1
            doc = deposit_doc(day, banks[bank]["number"], n)
        amount = sum((r["amount"] for r in group), Decimal(0))
        payload = {
            "DepositToAccountRef": {"value": bank},
            "TxnDate": day, "DocNumber": doc,
            "PrivateNote": (f"{note_head(day)} | {doc} -> {banks[bank]['number']} | {len(group)} receipt(s) "
                            f"{naira(amount)} | created by {TOOL}"),
            "Line": [{"Amount": float(r["amount"]),
                      "LinkedTxn": [{"TxnId": r["id"], "TxnType": "SalesReceipt", "TxnLineId": "0"}]} for r in group],
        }
        actions.append({"kind": "deposit", "key": doc, "state": "NEW", "bank_id": bank,
                        "receipt_ids": [r["id"] for r in group], "receipt_docs": [r["doc"] for r in group],
                        "amount": amount, "payload": payload})
    existing_tu = own_transfers(ctx["transfers"], day)
    planned = set()
    for t in alloc["transfers"]:
        src_no, dst_no = banks[t["from"]]["number"], banks[t["to"]]["number"]
        tag = transfer_tag(day, src_no, dst_no)
        planned.add(tag)
        live = existing_tu.get(tag)
        if live is not None:
            same = (clean((live.get("FromAccountRef") or {}).get("value")) == t["from"]
                    and clean((live.get("ToAccountRef") or {}).get("value")) == t["to"]
                    and abs(D(live.get("Amount"), Decimal(0)) - t["amount"]) <= CENT / 2)
            if same:
                actions.append({"kind": "transfer", "key": tag, "state": "EXISTS", "qbo_id": clean(live.get("Id")),
                                "from": t["from"], "to": t["to"], "amount": t["amount"]})
            else:
                conflicts.append(f"true-up Transfer {live.get('Id')} ({tag}) is in QBO for "
                                 f"{naira(D(live.get('Amount'), Decimal(0)))} but the plan needs {naira(t['amount'])}")
            continue
        payload = {"FromAccountRef": {"value": t["from"]}, "ToAccountRef": {"value": t["to"]},
                   "Amount": float(t["amount"]), "TxnDate": day,
                   "PrivateNote": (f"{note_head(day)} | true-up {src_no} -> {dst_no} so each bank matches the "
                                   f"till-sheet mix | {tag} | created by {TOOL}")}
        actions.append({"kind": "transfer", "key": tag, "state": "NEW", "from": t["from"], "to": t["to"],
                        "amount": t["amount"], "payload": payload})
    for tag, live in existing_tu.items():
        if tag not in planned:
            conflicts.append(f"true-up Transfer {live.get('Id')} ({tag}) is in QBO but not in this plan")
    for a in actions:
        if a.get("payload") is not None:
            a["payload_sha256"] = sha256_text(canonical_json(a["payload"]))
            a["requestid"] = requestid_for(a["key"], a["payload_sha256"])
    return actions, conflicts


# ---------------------------------------------------------------- evidence
def bank_rows(e: dict, banks: dict) -> list[dict]:
    alloc = e.get("alloc") or {}
    ids = set(e["sheet_by_bank"]) | set(alloc.get("deposited", {})) | set(alloc.get("targets", {}))
    out_tr, in_tr = defaultdict(Decimal), defaultdict(Decimal)
    for t in alloc.get("transfers", []):
        out_tr[t["from"]] += t["amount"]
        in_tr[t["to"]] += t["amount"]
    docs = defaultdict(list)
    count = Counter()
    for a in e["actions"]:
        if a["kind"] == "deposit":
            docs[a["bank_id"]].append(a["key"])
            count[a["bank_id"]] += len(a["receipt_ids"])
    rows = []
    for b in sorted(ids, key=lambda x: banks.get(x, {}).get("number", x)):
        target = alloc.get("targets", {}).get(b, Decimal(0))
        final = alloc.get("final", {}).get(b, Decimal(0))
        rows.append({"Day": e["day"], "Bank No": banks.get(b, {}).get("number", ""), "QBO Account Id": b,
                     "Kind": banks.get(b, {}).get("kind", ""),
                     "Sheet Lines": "; ".join(f"{x['label']} {money(x['value'])}" for x in e["boxes"]
                                              if x.get("bank_id") == b and x["value"]),
                     "Sheet Amount": money(e["sheet_by_bank"].get(b, 0)), "Target": money(target) if alloc else "",
                     "Deposited": money(alloc.get("deposited", {}).get(b, 0)) if alloc else "",
                     "Receipts": count.get(b, 0), "Deposit DocNumbers": " ".join(docs.get(b, [])),
                     "Transfer Out": money(out_tr[b]), "Transfer In": money(in_tr[b]),
                     "Final": money(final) if alloc else "", "Final - Target": money(final - target) if alloc else "",
                     "Status": e["status"]})
    return rows


def receipt_rows(e: dict, banks: dict) -> list[dict]:
    rec, alloc = e["receipts"], e.get("alloc") or {}
    rows = []
    docs = {rid: a["key"] for a in e["actions"] if a["kind"] == "deposit" for rid in a["receipt_ids"]}
    for state, items in (("pending", rec.get("pending", [])), ("in UF deposit", rec.get("fixed", [])),
                         ("deposited outside", rec.get("outside", [])), ("zero", rec.get("zero", []))):
        for r in items:
            bank = r.get("bank_id") or alloc.get("assigned", {}).get(r["id"], "")
            rows.append({"Day": e["day"], "Receipt Id": r["id"], "DocNumber": r["doc"], "Amount": money(r["amount"]),
                         "Tender": r["tender"], "State": state + (f" ({r['why']})" if r.get("why") else ""),
                         "Bank No": banks.get(bank, {}).get("number", ""), "Deposit DocNumber": docs.get(r["id"], "")})
    return rows


def payload_lines(e: dict) -> list[dict]:
    return [{"day": e["day"], "kind": a["kind"], "key": a["key"], "amount": money(a["amount"]),
             "bank_id": a.get("bank_id", ""), "from": a.get("from", ""), "to": a.get("to", ""),
             "receipt_ids": a.get("receipt_ids", []), "payload": a["payload"], "payload_sha256": a["payload_sha256"],
             "requestid": a["requestid"]}
            for a in e["actions"] if a["state"] == "NEW" and e["status"] == READY]


def day_summary(e: dict, banks: dict) -> dict:
    alloc = e.get("alloc") or {}
    num = lambda b: banks.get(b, {}).get("number", b)  # noqa: E731
    return {
        "day": e["day"], "status": e["status"], "state": day_state(e["status"], e.get("hold_kind", "")),
        "reasons": e["reasons"], "warnings": e["warnings"],
        "sheet_total": money(e["sheet_total"]), "receipts_total": money(e["receipts_total"]),
        "receipts": {k: len(v) for k, v in e["receipts"].items()},
        "sheet_by_bank": {num(b): money(v) for b, v in sorted(e["sheet_by_bank"].items(), key=lambda kv: num(kv[0]))},
        "target_by_bank": {num(b): money(v) for b, v in sorted(alloc.get("targets", {}).items(), key=lambda kv: num(kv[0]))},
        "final_by_bank": {num(b): money(v) for b, v in sorted(alloc.get("final", {}).items(), key=lambda kv: num(kv[0]))
                          if v},
        "deposits_new": sum(1 for a in e["actions"] if a["kind"] == "deposit" and a["state"] == "NEW"),
        "transfers_new": sum(1 for a in e["actions"] if a["kind"] == "transfer" and a["state"] == "NEW"),
        "existing": [f"{a['kind']} {a['key']} (QBO {a.get('qbo_id')})" for a in e["actions"] if a["state"] == "EXISTS"],
        "sheet": e["sheet"], "sheet_sha256": e["sheet_sha256"],
    }


def write_day(out: Path, e: dict, banks: dict, meta: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "review.csv", bank_rows(e, banks), REVIEW_COLS)
    write_csv(out / "receipts.csv", receipt_rows(e, banks), RECEIPT_COLS)
    lines = payload_lines(e)
    (out / "payloads.jsonl").write_text("".join(canonical_json(p) + "\n" for p in lines), encoding="utf-8")
    summary = {**meta, **day_summary(e, banks), "payload_count": len(lines),
               "payloads_sha256": sha256_file(out / "payloads.jsonl")}
    summary["post_command"] = (f"python -m code_scripts.akponora_ops.{TOOL} post --plan-dir {out} "
                               f"--approval-ref '<chat yes>' --expect-sha {summary['payloads_sha256']}"
                               if lines else "")
    dump_json(out / "plan.json", {**summary, "boxes": e["boxes"], "receipts_detail": e["receipts"],
                                  "allocation": e.get("alloc"),
                                  "actions": [{k: v for k, v in a.items() if k != "payload"} for a in e["actions"]]})
    dump_json(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- plan (window)
def run_plan(out: Path, *, days: list[str], client: QBOClient, source, s: dict, accounts: dict,
             auto_cap: Decimal | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    meta = {"tool": TOOL, "realm": REALM, "mode": "plan", "planned_at": now_iso(),
            "sheet_source": source.describe(), "accounts_file": {"path": accounts["path"], "sha256": accounts["sha256"]},
            "tolerance": {"abs": money(s["tol_abs"]), "pct": str(s["tol_pct"]),
                          "source": s.get("tol_source", "environment")},
            "auto_cap": money(auto_cap) if auto_cap is not None else ""}
    entries = []
    days = sorted(days)
    ctx = fetch_context(client, days, accounts["banks"]) if days else None
    for day in days:  # days are independent: a held day never stops a later one
        e = plan_day(day, source=source, accounts=accounts, ctx=ctx, s=s, auto_cap=auto_cap)
        e["summary"] = write_day(out / day, e, accounts["banks"], meta)
        entries.append(e)
    summary = {**meta, "window": [days[0], days[-1]] if days else [], "qbo_requests": client.requests,
               "counts": dict(Counter(e["status"] for e in entries)),
               "days": [{k: e["summary"][k] for k in ("day", "status", "state", "reasons", "warnings", "receipts_total",
                                                       "sheet_total", "final_by_bank", "payloads_sha256",
                                                       "post_command")} | {"dir": str(out / e["day"])}
                        for e in entries]}
    dump_json(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- post
def load_results(path: Path) -> dict:
    return {r["key"]: r for r in read_csv(path)} if path.exists() else {}


def append_result(path: Path, row: dict) -> None:
    rows = read_csv(path) if path.exists() else []
    write_csv(path, rows + [row], RESULT_COLS)


def verify_deposit(dep: dict, p: dict) -> list[str]:
    problems = []
    want = p["payload"]
    if clean((dep.get("DepositToAccountRef") or {}).get("value")) != want["DepositToAccountRef"]["value"]:
        problems.append(f"deposit account {(dep.get('DepositToAccountRef') or {}).get('value')} != "
                        f"{want['DepositToAccountRef']['value']}")
    if clean(dep.get("TxnDate")) != want["TxnDate"]:
        problems.append(f"TxnDate {dep.get('TxnDate')} != {want['TxnDate']}")
    if deposit_key(dep) != want["DocNumber"]:
        problems.append(f"deposit tag {deposit_key(dep) or '(none)'} != {want['DocNumber']}")
    if abs(D(dep.get("TotalAmt"), Decimal(0)) - Decimal(p["amount"])) > CENT / 2:
        problems.append(f"TotalAmt {dep.get('TotalAmt')} != {p['amount']}")
    linked = sorted(clean(link.get("TxnId")) for line in dep.get("Line") or [] for link in line.get("LinkedTxn") or []
                    if clean(link.get("TxnType")) == "SalesReceipt")
    if linked != sorted(p["receipt_ids"]):
        problems.append(f"linked receipts {linked} != {sorted(p['receipt_ids'])}")
    if clean((dep.get("CashBack") or {}).get("Amount")) not in ("", "0", "0.0"):
        problems.append("deposit has cash back")
    return problems


def verify_transfer(t: dict, p: dict) -> list[str]:
    problems = []
    for field, want in (("FromAccountRef", p["from"]), ("ToAccountRef", p["to"])):
        if clean((t.get(field) or {}).get("value")) != want:
            problems.append(f"{field} {(t.get(field) or {}).get('value')} != {want}")
    if abs(D(t.get("Amount"), Decimal(0)) - Decimal(p["amount"])) > CENT / 2:
        problems.append(f"Amount {t.get('Amount')} != {p['amount']}")
    if clean(t.get("TxnDate")) != p["payload"]["TxnDate"]:
        problems.append(f"TxnDate {t.get('TxnDate')} != {p['payload']['TxnDate']}")
    if p["key"] not in clean(t.get("PrivateNote")):
        problems.append(f"PrivateNote lacks {p['key']}")
    return problems


def post_with_minorversion(client: QBOClient, path: str, body: dict, requestid: str, minorversion: str | None):
    prior = client.minorversion
    if minorversion:
        client.minorversion = minorversion
    try:
        return client.post_json(path, body, requestid)
    finally:
        client.minorversion = prior


def recheck_receipts(client: QBOClient, p: dict) -> str:
    """'' when every receipt is still in Undeposited Funds with the planned amount."""
    expected = {}
    for line in p["payload"]["Line"]:
        expected[line["LinkedTxn"][0]["TxnId"]] = Decimal(str(line["Amount"]))
    for rid in p["receipt_ids"]:
        sr = client.get_json(f"/salesreceipt/{rid}").get("SalesReceipt") or {}
        if abs(D(sr.get("TotalAmt"), Decimal(0)) - expected[rid]) > CENT / 2:
            return f"receipt {rid} total is now {sr.get('TotalAmt')} (plan {expected[rid]}); re-plan"
        dep_to = clean((sr.get("DepositToAccountRef") or {}).get("value"))
        if dep_to and dep_to != UF_ACCOUNT_ID:
            return f"receipt {rid} is no longer in Undeposited Funds (account {dep_to}); re-plan"
        if any(clean(ln.get("TxnType")) == "Deposit" for ln in sr.get("LinkedTxn") or []):
            return f"receipt {rid} is already linked to a Deposit; re-plan"
    return ""


def post_one(client: QBOClient, p: dict, approval_ref: str) -> tuple[str, dict | None, str]:
    """(state POSTED|ADOPTED|STOP, entity, detail). Re-checks live first; never double-posts."""
    payload = with_ref(p["payload"], approval_ref)
    if p["kind"] == "deposit":
        doc = p["key"]
        existing = own_deposits(client, p["payload"]["TxnDate"], doc)
        if existing:
            if len(existing) == 1 and not verify_deposit(existing[0], p):
                return "ADOPTED", existing[0], f"Deposit {existing[0].get('Id')} already exists and matches"
            return "STOP", None, f"Deposit(s) {[d.get('Id') for d in existing]} carry {doc} but do not match the plan"
        why = recheck_receipts(client, p)
        if why:
            return "STOP", None, why
        resp = post_with_minorversion(client, "/deposit", payload, p["requestid"], DEPOSIT_MINORVERSION)
        if resp.status_code != 200:
            again = own_deposits(client, p["payload"]["TxnDate"], doc)
            if len(again) == 1 and not verify_deposit(again[0], p):
                return "POSTED", again[0], ""
            return "STOP", None, f"POST /deposit failed {resp.status_code}: {resp.text[:400]}"
        dep = resp.json().get("Deposit") or {}
        return ("POSTED", dep, "") if dep.get("Id") else ("STOP", None, "200 without a Deposit Id; re-run to reconcile")
    day = p["payload"]["TxnDate"]
    live = own_transfers(client.query_all(f"select * from Transfer where TxnDate = '{day}'", "Transfer"), day).get(p["key"])
    if live is not None:
        if not verify_transfer(live, p):
            return "ADOPTED", live, f"Transfer {live.get('Id')} already exists and matches"
        return "STOP", None, f"Transfer {live.get('Id')} carries {p['key']} but does not match the plan"
    resp = client.post_json("/transfer", payload, p["requestid"])
    if resp.status_code != 200:
        live = own_transfers(client.query_all(f"select * from Transfer where TxnDate = '{day}'", "Transfer"), day).get(p["key"])
        if live is not None and not verify_transfer(live, p):
            return "POSTED", live, ""
        return "STOP", None, f"POST /transfer failed {resp.status_code}: {resp.text[:400]}"
    t = resp.json().get("Transfer") or {}
    return ("POSTED", t, "") if t.get("Id") else ("STOP", None, "200 without a Transfer Id; re-run to reconcile")


def post_day(day_dir: Path, *, client: QBOClient, approval_ref: str, expect_sha: str, auto: bool = False,
             s: dict | None = None) -> dict:
    """Post one planned day. Raises StopRun on a gate failure before anything is written."""
    if not clean(approval_ref):
        raise StopRun("post requires --approval-ref (the chat yes reference)")
    summary = json.loads((day_dir / "summary.json").read_text())
    actual = sha256_file(day_dir / "payloads.jsonl")
    if not clean(expect_sha) or expect_sha != actual or summary.get("payloads_sha256") != actual:
        raise StopRun(f"--expect-sha mismatch: payloads.jsonl is {actual}; re-review the plan")
    if summary.get("realm") != REALM:
        raise StopRun("plan realm mismatch")
    if summary.get("status") != READY:
        raise StopRun(f"{summary.get('day')} is {summary.get('status')} in the plan; only READY days post")
    if auto:
        s = s or settings()
        if not s["auto"]:
            raise StopRun(f"automatic posting needs {ENABLED_ENV}=1, {AUTO_ENV}=1 and {REF_ENV}")
        if Decimal(summary["receipts_total"]) > s["cap"]:
            raise StopRun(f"day total {summary['receipts_total']} over the automatic cap {money(s['cap'])}")
    payloads = []
    for line in (day_dir / "payloads.jsonl").read_text().splitlines():
        if line.strip():
            p = json.loads(line)
            if sha256_text(canonical_json(p["payload"])) != p["payload_sha256"]:
                raise StopRun(f"{p['key']}: payload does not match its sha256")
            payloads.append(p)
    payloads.sort(key=lambda p: (p["kind"] != "deposit", p["key"]))  # all deposits, then the true-ups
    results_path = day_dir / "results.csv"
    results = load_results(results_path)
    counts, stop = Counter(), None
    for p in payloads:
        if results.get(p["key"], {}).get("status") in DONE_RESULTS:
            counts["ALREADY_DONE"] += 1
            continue
        base = {"ts": now_iso(), "day": summary["day"], "kind": p["kind"], "key": p["key"], "amount": p["amount"],
                "payload_sha256": p["payload_sha256"], "requestid": p["requestid"], "approval_ref": approval_ref,
                "mode": "auto" if auto else "manual"}
        state, entity, detail = post_one(client, p, approval_ref)
        if state == "STOP":
            append_result(results_path, {**base, "status": "STOPPED", "detail": detail})
            stop = detail
            break
        path = f"/{p['kind']}/{entity['Id']}"
        live = client.get_json(path).get("Deposit" if p["kind"] == "deposit" else "Transfer") or {}
        problems = verify_deposit(live, p) if p["kind"] == "deposit" else verify_transfer(live, p)
        append_result(results_path, {**base, "status": state if not problems else "VERIFY_FAILED",
                                     "qbo_id": live.get("Id"), "detail": "; ".join(problems) or detail})
        if problems:
            stop = f"{p['kind']} {live.get('Id')} failed verification: {'; '.join(problems)}"
            break
        counts[state] += 1
    results = load_results(results_path)
    complete = not stop and all(results.get(p["key"], {}).get("status") in DONE_RESULTS for p in payloads)
    out = {"day": summary["day"], "run_at": now_iso(), "approval_ref": approval_ref, "mode": "auto" if auto else "manual",
           "counts": dict(counts), "stopped": stop, "complete": complete,
           "final_by_bank": summary.get("final_by_bank", {}), "receipts_total": summary.get("receipts_total"),
           "qbo_requests": client.requests}
    dump_json(day_dir / f"post_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", out)
    return out


# ---------------------------------------------------------------- UF balance
def uf_balance(client: QBOClient) -> str:
    rows = as_list(client.query(f"select * from Account where Id = '{UF_ACCOUNT_ID}'").get("Account"))
    return money(D(rows[0].get("CurrentBalance"), Decimal(0))) if rows else ""


# ---------------------------------------------------------------- scheduled
def run_scheduled(out: Path, *, business_day: str, client: QBOClient, source, s: dict, accounts: dict,
                  write_client: QBOClient | None = None, dry_run: bool = False, from_day: str | None = None) -> dict:
    """Every business day from the floor (or ``from_day``) to ``business_day`` that is not yet
    DEPOSITED is planned on its own; each READY day is posted when the automatic gates are on (never
    on a dry run), whatever happened to the days before it. Per-day state goes to days.json (not on
    a dry run). A post that stops (QBO error / verification) ends posting for this run."""
    state = read_state()
    days = open_days(state, from_day or FLOOR, business_day)
    auto = s["auto"] and not dry_run
    plan = run_plan(out, days=days, client=client, source=source, s=s, accounts=accounts,
                    auto_cap=s["cap"] if auto else None)
    posted, stopped, results = [], None, {}
    if auto:
        for d in plan["days"]:
            if d["status"] != READY:
                continue
            wclient = write_client or QBOClient.for_company_a(allow_writes=True)
            write_client = wclient
            res = post_day(Path(d["dir"]), client=wclient, approval_ref=s["ref"], expect_sha=d["payloads_sha256"],
                           auto=True, s=s)
            posted.append(res)
            results[d["day"]] = res
            if res["stopped"]:
                stopped = f"{d['day']}: {res['stopped']}"
                break
    days_out = []
    for d in plan["days"]:
        res = results.get(d["day"])
        reasons = list(d["reasons"])
        if res and res["complete"]:
            status, posted_now = DEPOSITED, True
        elif res:
            status, posted_now = HELD, False
            reasons.insert(0, f"post stopped: {res['stopped'] or 'incomplete'}")
        else:
            status, posted_now = d["state"], False
        days_out.append({**{k: d[k] for k in ("day", "warnings", "receipts_total", "sheet_total", "final_by_bank",
                                               "payloads_sha256", "dir", "post_command")},
                         "status": status, "reasons": reasons, "posted_now": posted_now})
        record_day(state, d["day"], status, reasons=reasons, run_dir=d["dir"], receipts_total=d["receipts_total"],
                   approval_ref=s["ref"] if posted_now else "")
    if not dry_run:
        write_state(state)
    try:
        report = sheet_report(source, upto=business_day, state=state)
    except Exception as exc:  # noqa: BLE001 - the report is informational
        report = {"error": str(exc)[:200]}
        report["text"] = report_text(report)
    try:
        balance = uf_balance(write_client or client)
    except Exception as exc:  # noqa: BLE001 - the balance is informational
        balance = f"unavailable ({str(exc)[:80]})"
    waiting = [d for d in days_out if d["status"] in OPEN_STATES]
    result = {"tool": TOOL, "business_date": business_day, "window": plan["window"], "auto_post": auto,
              "dry_run": dry_run, "days": days_out, "posted": posted, "stopped": stopped,
              "uf_balance": balance, "waiting": len(waiting), "run_dir": str(out), "till_sheet": report,
              "counts": dict(Counter(d["status"] for d in days_out))}
    plan["till_sheet"] = report
    dump_json(out / "summary.json", plan)
    dump_json(out / "scheduled.json", result)
    return result


def slack_text(res: dict) -> str:
    lines = [f"Akponora UF deposits {'..'.join(res['window']) or '(nothing to do)'}: "
             f"{', '.join(f'{k} {v}' for k, v in sorted(res['counts'].items())) or 'no days'}; "
             f"Undeposited Funds {res['uf_balance']}"]
    for d in res["days"]:
        if d["status"] == DEPOSITED:
            lines.append(f"- {d['day']} {'deposited' if d.get('posted_now') else 'already deposited'} "
                         f"{naira(d['receipts_total'])}"
                         + (": " + ", ".join(f"{k} {naira(v)}" for k, v in d["final_by_bank"].items())
                            if d.get("posted_now") else ""))
        elif d["status"] in (HELD, WAITING_SHEET, NO_SALES):
            lines.append(f"- {d['day']} {d['status']}: {(d['reasons'] or [''])[0][:200]}")
        elif d["status"] == READY:
            lines.append(f"- {d['day']} READY (plan only) {naira(d['receipts_total'])}: {d['post_command'][:300]}")
    if res.get("stopped"):
        lines.append(f"STOPPED: {res['stopped']}")
    if res.get("till_sheet"):
        lines.append(res["till_sheet"]["text"])
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def _accounts(a, s) -> dict:
    return load_accounts(Path(a.accounts) if getattr(a, "accounts", None) else accounts_path(s))


def cmd_plan(a) -> int:
    s = settings()
    if a.date:
        days = [a.date]
    else:
        end = a.date_to or last_closed_day()
        days = day_range(a.date_from, end) if a.date_from else open_days(read_state(), FLOOR, end)
        days = [d for d in days if d <= end]
    out = run_dir(TOOL, a.out)
    accounts = _accounts(a, s)
    source = sheet_source(s, xlsx=a.sheet_xlsx)
    client = QBOClient.for_company_a(allow_writes=False)
    summary = run_plan(out, days=days, client=client, source=source, s=s, accounts=accounts)
    for d in summary["days"]:
        print(f"{d['day']} {d['status']:8} receipts {d['receipts_total']} sheet {d['sheet_total']} "
              f"{(d['reasons'] or [''])[0][:160]}")
    print(f"-> {out}")
    return 0


def cmd_post(a) -> int:
    client = QBOClient.for_company_a(allow_writes=True)
    res = post_day(Path(a.plan_dir), client=client, approval_ref=a.approval_ref, expect_sha=a.expect_sha)
    state = read_state()
    if res["complete"]:
        record_day(state, res["day"], DEPOSITED, reasons=["posted by hand (uf_deposits post)"], run_dir=a.plan_dir,
                   receipts_total=res.get("receipts_total"), approval_ref=a.approval_ref)
    elif res["stopped"]:
        record_day(state, res["day"], HELD, reasons=[f"post stopped: {res['stopped']}"], run_dir=a.plan_dir)
    write_state(state)
    res["day_state"] = state["days"][res["day"]]["status"]
    res["uf_balance"] = uf_balance(client)
    print(json.dumps(res, indent=1, default=str))
    if not a.no_slack:
        send_slack(f"Akponora UF deposits post {res['day']}: {res['counts']}; stopped: {res['stopped'] or 'no'}; "
                   f"day {res['day_state']}; Undeposited Funds {res['uf_balance']}. Folder: {a.plan_dir}")
    return 2 if res["stopped"] else 0


def status_lines(state: dict, report: dict, *, upto: str) -> list[str]:
    lines = [report["text"], "", f"Per-day deposit state ({FLOOR} .. {upto}):"]
    for d in day_range(FLOOR, upto) if FLOOR <= upto else []:
        entry = state["days"].get(d) or {}
        lines.append(f"{d} {entry.get('status') or 'NOT RUN':13} {clean(entry.get('reason'))[:160]}")
    return lines


def cmd_status(a) -> int:
    s = settings()
    upto = a.date or last_closed_day()
    state = read_state()
    report = sheet_report(sheet_source(s, xlsx=a.sheet_xlsx), upto=upto, state=state)
    if a.json:
        print(json.dumps({"till_sheet": report, "days": state["days"]}, indent=1, default=str))
    else:
        print("\n".join(status_lines(state, report, upto=upto)))
    return 0


def cmd_scheduled(a) -> int:
    s = settings()
    out = run_dir(TOOL, a.out)
    accounts = _accounts(a, s)
    source = sheet_source(s, xlsx=a.sheet_xlsx)
    client = QBOClient.for_company_a(allow_writes=False)
    res = run_scheduled(out, business_day=a.date or last_closed_day(), client=client, source=source, s=s,
                        accounts=accounts, dry_run=a.dry_run)
    print(slack_text(res))
    if not a.no_slack:
        send_slack(slack_text(res))
    if res["stopped"]:
        return 2
    return 3 if res["waiting"] else 0


def main(argv=None) -> int:
    setup_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "scheduled"):
        p = sub.add_parser(name, help="READ-ONLY plan" if name == "plan" else "plan + gated automatic post")
        p.add_argument("--out", default=None, help="evidence folder (default <runs>/uf_deposits_<UTC>/)")
        p.add_argument("--sheet-xlsx", default=None, help="offline: an .xlsx download of the till sheet")
        p.add_argument("--accounts", default=None, help="override STATE_ROOT/mappings/company_a/till_accounts.csv")
        p.add_argument("--no-slack", action="store_true")
        p.add_argument("--date", default=None, help="plan: this one business day; scheduled: the run's business date")
        if name == "plan":
            p.add_argument("--from", dest="date_from", default=None, help="first business day (default cursor)")
            p.add_argument("--to", dest="date_to", default=None, help="last business day (default last closed)")
        else:
            p.add_argument("--dry-run", action="store_true", help="plan only; never posts or writes days.json")
    p = sub.add_parser("status", help="READ-ONLY (sheet + days.json, no QBO): till-sheet and deposit status")
    p.add_argument("--date", default=None, help="report up to this business day (default last closed)")
    p.add_argument("--sheet-xlsx", default=None, help="offline: an .xlsx download of the till sheet")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("post", help="WRITES: post one READY day")
    p.add_argument("--plan-dir", required=True, help="<plan out>/<day>")
    p.add_argument("--approval-ref", default="")
    p.add_argument("--expect-sha", default="", help="payloads_sha256 from <day>/summary.json")
    p.add_argument("--no-slack", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plan":
            return cmd_plan(a)
        if a.cmd == "post":
            return cmd_post(a)
        if a.cmd == "status":
            return cmd_status(a)
        return cmd_scheduled(a)
    except StopRun as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

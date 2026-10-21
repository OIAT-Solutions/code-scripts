"""Write each day's banking status next to its title in the Nora Mart till sheet (owner, 4 Oct 2026).

Column B of a day's title row ("Thursday 1st October 2026 NORA MINI MART") shows, from the deposit
state in ``days.json``:

    ✅ Banked · ₦4,868,700 · 4 Oct 18:12                   (DEPOSITED)
    ⏸ Not banked: till sheet and sales don't agree          (HELD: a problem on a completed day)

Days still being filled in (WAITING_SHEET) or whose sales aren't in QuickBooks yet (NO_SALES) get no
note (owner, 4 Oct 2026): staff complete them anyway. A ⏸ note that no longer applies is cleared.

Rules: only that one cell per day is written; a cell is written only when it is empty or already holds
one of these marks (never over something a person typed); nothing is written on a dry run. Every call
re-syncs all days in ``days.json``, so days banked before this existed get their mark too, and a held
day's mark turns into "Banked" once it is banked. The service account needs Editor on the sheet (given
4 Oct 2026). ``OIAT_COMPANY_A_TILL_SHEET_MARKS=0`` turns it off. Failures never affect banking.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import till_sheet

WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
ENABLED_ENV = "OIAT_COMPANY_A_TILL_SHEET_MARKS"
BANKED, NOT_BANKED = "✅", "⏸"
TZ = ZoneInfo("Africa/Lagos")


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENABLED_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"}


def short_reason(reason: str) -> str:
    text = (reason or "").lower()
    if "box is blank" in text or "sheet day is blank" in text:
        return "till sales breakdown incomplete"
    if "no salesreceipts" in text or "sales not posted" in text:
        return "sales not in QuickBooks yet"
    if "sheet total" in text and ("tolerance" in text or "differ" in text):
        return "till sheet and sales don't agree"
    if "post stopped" in text:
        return "banking stopped part-way; OIAT is checking"
    if "closing date" in text or "closed" in text:
        return "day is in a closed period"
    if "outside this tool" in text:
        return "some sales were banked by hand"
    if "not in till_accounts" in text or "unknown till line" in text:
        return "a till line isn't recognised"
    if "cap" in text or "limit" in text:
        return "over the automatic limit; needs approval"
    return "see the daily run"


def mark_text(entry: dict) -> str:
    status = entry.get("status")
    if status == "DEPOSITED":
        bits = [f"{BANKED} Banked"]
        try:
            bits.append(f"₦{Decimal(str(entry['receipts_total'])):,.0f}")
        except (KeyError, InvalidOperation, ValueError):
            pass
        try:
            when = datetime.fromisoformat(str(entry["updated_at"])).astimezone(TZ)
            bits.append(f"{when.day} {when:%b %H:%M}")
        except (KeyError, ValueError):
            pass
        return " · ".join(bits)
    if status == "HELD":
        return f"{NOT_BANKED} Not banked: {short_reason(entry.get('reason') or '')}"
    if status == "READY":
        return f"{NOT_BANKED} Not banked yet: waiting for approval"
    return ""  # WAITING_SHEET / NO_SALES: no note while the day is still being filled in


def is_ours(value) -> bool:
    text = str(value or "").strip()
    return not text or text.startswith((BANKED, NOT_BANKED))


def writer_service(key_path: str | Path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=[WRITE_SCOPE])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def sync_marks(state: dict, *, sheet_id: str, service=None, key_path: str | Path | None = None,
               upto: str | None = None) -> dict:
    """Bring column B of every day's title row in line with ``days.json``. Returns counts."""
    res = {"written": [], "unchanged": 0, "not_found": [], "kept_other_content": [], "error": ""}
    wanted = {day: mark_text(e) for day, e in (state.get("days") or {}).items() if (not upto or day <= upto)}
    if not wanted:
        return res
    service = service or writer_service(key_path)
    values = service.spreadsheets().values()
    by_tab: dict[str, list[str]] = {}
    for day in sorted(wanted):
        by_tab.setdefault(till_sheet.tab_name(day), []).append(day)
    updates = []
    for tab, days in by_tab.items():
        resp = values.get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:B{till_sheet.MAX_ROWS}",
                          valueRenderOption="UNFORMATTED_VALUE").execute()
        rows = [list(r) for r in resp.get("values") or []]
        found = {d["date"]: d["row"] for d in till_sheet.parse_rows(rows, tab=tab)}
        for day in days:
            row = found.get(day)
            if not row:
                if wanted[day]:
                    res["not_found"].append(day)
                continue
            current = rows[row - 1][1] if len(rows[row - 1]) > 1 else ""
            if not wanted[day] and not str(current or "").strip().startswith(NOT_BANKED):
                continue  # nothing to show, and no old ⏸ note of ours to clear
            if str(current or "").strip() == wanted[day]:
                res["unchanged"] += 1
            elif is_ours(current):
                updates.append({"range": f"'{tab}'!B{row}", "values": [[wanted[day]]]})
                res["written"].append(day)
            else:
                res["kept_other_content"].append(day)
    if updates:
        values.batchUpdate(spreadsheetId=sheet_id,
                           body={"valueInputOption": "RAW", "data": updates}).execute()
    return res


def sync_safely(state: dict, *, sheet_id: str, key_path, upto: str | None = None, env=None, service=None) -> dict:
    """``sync_marks`` that never raises (banking already happened)."""
    if not enabled(env):
        return {"written": [], "error": "", "disabled": True}
    try:
        return sync_marks(state, sheet_id=sheet_id, key_path=key_path, upto=upto, service=service)
    except Exception as exc:  # noqa: BLE001 - a sheet write must never undo or block banking
        return {"written": [], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

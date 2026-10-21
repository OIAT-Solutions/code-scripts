#!/usr/bin/env python3
"""Post ONE reviewed JournalEntry to AKPONORA (company_a, realm 9341455406194328).

Journals are QBO writes that need an explicit chat yes (AGENTS.md). DEFAULT IS DRY-RUN.

Subcommands
-----------
``post --spec SPEC.json``
    Validates the spec offline (debits == credits in Decimal at 2 dp, positive
    amounts, DocNumber <= 21 chars, ISO date, not DRAFT, no blank account),
    builds the exact QBO payload and prints it with its SHA-256. Unless
    ``--no-live``, also runs read-only checks: every account exists and is
    active, the DocNumber is not already used by a JournalEntry, and the
    TxnDate is after the books close date (when QBO exposes one).
    ``--execute --expect-sha <sha from the dry-run>`` re-runs every check,
    POSTs with an Intuit requestid derived from the sha (so a retry cannot
    double-post), re-reads the entry, verifies DocNumber/TxnDate/lines and
    writes a receipt JSON. A verification failure exits 2 and is never
    auto-reversed.

``offset --w7-summary summary_execute.json --approved-v V``
    Computes the create-night IA offset ``B + C - V`` with
    ``operations_controls.create_night_offset`` (B = IA 77 before the first W7
    create, B + C = IA 77 after, both as of 2026-10-01 from the W7 summary) and
    writes a ready spec: credit IA 77 / debit 300150 (Id 86); a negative amount
    reverses the sides. Refuses when the actual C differs from the registered
    items' expected opening value by more than ``--tolerance`` unless
    ``--accept-c-difference`` is given (explain it in the chat approval).

Spec format::

    {"doc_number": "COGS-2026-09", "txn_date": "2026-09-30", "private_note": "...",
     "status": "REVIEWED",            # anything starting DRAFT is refused on --execute
     "lines": [{"account_id": "77", "posting_type": "Debit", "amount": "123.45",
                "description": "...", "tax_code_id": "7"}]}   # tax_code_id optional, default 7

Lines carry ``TaxCodeRef 7`` (No VAT), ``TaxApplicableOn Purchase`` and
``TaxAmount 0`` like the earlier Akponora journals (INV-EQ-2026-09-16,
COGS-ZF-*): the company is VAT-enabled and that is how they were posted.
Keys starting with ``_`` and ``notes`` are documentation only (not posted).

Templates (DRAFT, placeholder amounts): ``journal_templates/*.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_scripts.operations_controls import create_night_offset  # noqa: E402
from code_scripts.scripts.akponora_cutover.w7_create_items import (  # noqa: E402
    REALM,
    QBOClient,
    StopRun,
    canonical_json,
    qbo_escape,
)

DOC_MAX = 21  # QBO DocNumber limit
DEFAULT_TAX_CODE = "7"
IA_ID, EQUITY_300150_ID = "77", "86"
SPEC_KEYS = {"doc_number", "txn_date", "private_note", "lines", "status", "notes"}
LINE_KEYS = {"account_id", "posting_type", "amount", "description", "tax_code_id", "notes"}
TWO_DP = Decimal("0.01")


# ---------------------------------------------------------------- offline validation
def money(value, where) -> Decimal:
    try:
        n = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{where}: amount {value!r} is not a number") from exc
    if not n.is_finite():
        raise ValueError(f"{where}: amount must be finite")
    if n != n.quantize(TWO_DP):
        raise ValueError(f"{where}: amount {value} has more than 2 decimal places (round it in the spec)")
    return n.quantize(TWO_DP)


def validate_spec(spec: dict) -> dict:
    """Return {'payload', 'debits', 'credits', 'problems', 'draft'}; raise ValueError on structure errors."""
    unknown = {k for k in spec if not k.startswith("_")} - SPEC_KEYS
    if unknown:
        raise ValueError(f"unknown spec keys: {sorted(unknown)}")
    problems = []
    doc = str(spec.get("doc_number") or "").strip()
    if not doc:
        problems.append("doc_number is required")
    elif len(doc) > DOC_MAX:
        problems.append(f"doc_number {doc!r} is longer than {DOC_MAX} characters (QBO limit)")
    try:
        txn = date.fromisoformat(str(spec.get("txn_date") or ""))
    except ValueError:
        txn = None
        problems.append("txn_date must be YYYY-MM-DD")
    lines = spec.get("lines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise ValueError("a journal needs at least two lines")
    debits = credits = Decimal(0)
    out_lines = []
    for i, line in enumerate(lines, start=1):
        where = f"line {i}"
        unknown = {k for k in line if not k.startswith("_")} - LINE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        account = str(line.get("account_id") or "").strip()
        if not account:
            problems.append(f"{where}: account_id is blank")
        elif not account.isdigit():
            problems.append(f"{where}: account_id {account!r} must be a QBO Account Id")
        posting = str(line.get("posting_type") or "")
        if posting not in ("Debit", "Credit"):
            raise ValueError(f"{where}: posting_type must be Debit or Credit")
        amount = money(line.get("amount"), where)
        if amount <= 0:
            problems.append(f"{where}: amount must be > 0 (placeholder?)")
        if posting == "Debit":
            debits += amount
        else:
            credits += amount
        tax = str(line.get("tax_code_id") or DEFAULT_TAX_CODE)
        out_lines.append({
            "Description": str(line.get("description") or "")[:4000],
            "Amount": float(amount),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {"PostingType": posting, "AccountRef": {"value": account},
                                       "TaxCodeRef": {"value": tax}, "TaxApplicableOn": "Purchase",
                                       "TaxAmount": 0.0},
        })
    if debits != credits:
        problems.append(f"debits {debits} != credits {credits}")
    draft = str(spec.get("status") or "").strip().upper().startswith("DRAFT")
    payload = {"DocNumber": doc, "TxnDate": txn.isoformat() if txn else str(spec.get("txn_date")),
               "PrivateNote": str(spec.get("private_note") or "")[:4000], "Line": out_lines}
    return {"payload": payload, "debits": str(debits), "credits": str(credits), "problems": problems,
            "draft": draft}


def payload_sha(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- live read-only checks
def live_checks(client: QBOClient, payload: dict) -> list[str]:
    problems = []
    for acct_id in sorted({ln["JournalEntryLineDetail"]["AccountRef"]["value"] for ln in payload["Line"]}):
        if not acct_id:
            continue
        resp = client.call("GET", f"/account/{acct_id}")
        if resp.status_code != 200:
            problems.append(f"account {acct_id} not found ({resp.status_code})")
            continue
        acct = resp.json().get("Account", {})
        if acct.get("Active") is not True:
            problems.append(f"account {acct_id} {acct.get('Name')!r} is inactive")
    doc = payload["DocNumber"]
    rows = client.query(f"select Id, DocNumber, TxnDate from JournalEntry where DocNumber = '{qbo_escape(doc)}'"
                        ).get("JournalEntry", [])
    rows = rows if isinstance(rows, list) else [rows]
    if rows:
        problems.append(f"DocNumber {doc} already used by JournalEntry Id(s) {[r.get('Id') for r in rows]}")
    prefs = client.get_json("/preferences").get("Preferences", {})
    close = (prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")
    if close and payload["TxnDate"] <= close:
        problems.append(f"TxnDate {payload['TxnDate']} is on/before the books close date {close}")
    return problems


def line_signature(lines) -> list:
    sig = []
    for ln in lines:
        d = ln.get("JournalEntryLineDetail") or {}
        sig.append((str((d.get("AccountRef") or {}).get("value")), d.get("PostingType"),
                    str(Decimal(str(ln.get("Amount"))).quantize(TWO_DP))))
    return sorted(sig)


def verify_posted(je: dict, payload: dict) -> list[str]:
    problems = []
    for key in ("DocNumber", "TxnDate"):
        if je.get(key) != payload[key]:
            problems.append(f"{key} {je.get(key)!r} != {payload[key]!r}")
    got = [ln for ln in je.get("Line", []) if ln.get("DetailType") == "JournalEntryLineDetail"]
    if line_signature(got) != line_signature(payload["Line"]):
        problems.append(f"lines differ: posted {line_signature(got)} vs approved {line_signature(payload['Line'])}")
    return problems


def post(client: QBOClient, payload: dict, sha: str) -> dict:
    resp = client.post_json("/journalentry", payload, requestid=sha[:36])
    if resp.status_code != 200:
        rows = client.query(f"select * from JournalEntry where DocNumber = '{qbo_escape(payload['DocNumber'])}'"
                            ).get("JournalEntry", [])
        rows = rows if isinstance(rows, list) else [rows]
        if len(rows) == 1 and not verify_posted(rows[0], payload):
            return rows[0]
        raise StopRun(f"POST journalentry failed {resp.status_code}: {resp.text[:800]}")
    je = resp.json().get("JournalEntry") or {}
    if not je.get("Id"):
        raise StopRun("200 without a JournalEntry Id; query by DocNumber before retrying")
    return je


def run_post(spec_path: Path, *, execute: bool, expect_sha: str, client: QBOClient | None, receipt_dir: Path,
             approval_ref: str = "") -> int:
    spec = json.loads(spec_path.read_text())
    checked = validate_spec(spec)
    payload, problems = checked["payload"], list(checked["problems"])
    sha = payload_sha(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"debits {checked['debits']}  credits {checked['credits']}")
    print(f"payload_sha256 {sha}")
    if checked["draft"]:
        problems.append("spec status is DRAFT; review it, fill real amounts and set status REVIEWED")
    if client is not None:
        problems += live_checks(client, payload)
    elif execute:
        problems.append("live read-only checks skipped (--no-live)")
    for p in problems:
        print("PROBLEM:", p)
    if not execute:
        print("dry-run only; nothing posted")
        return 1 if problems else 0
    if client is None:
        raise SystemExit("--execute requires the live checks")
    if expect_sha != sha:
        raise SystemExit(f"--expect-sha mismatch: payload is {sha}; re-review the dry-run")
    if not approval_ref.strip():
        raise SystemExit("--execute requires --approval-ref (the chat yes reference)")
    if problems:
        raise SystemExit("--execute refused: " + "; ".join(problems))
    je = post(client, payload, sha)
    live = client.get_json(f"/journalentry/{je['Id']}")["JournalEntry"]
    verify = verify_posted(live, payload)
    receipt = {"realm": REALM, "status": "POSTED_VERIFIED" if not verify else "POSTED_VERIFY_FAILED",
               "doc_number": payload["DocNumber"], "txn_date": payload["TxnDate"], "id": live.get("Id"),
               "sync_token": live.get("SyncToken"), "payload_sha256": sha, "approval_ref": approval_ref,
               "spec": str(spec_path), "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
               "debits": checked["debits"], "credits": checked["credits"], "lines": line_signature(live.get("Line", [])),
               "verify_problems": verify, "created_time": (live.get("MetaData") or {}).get("CreateTime"),
               "receipt_written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / f"receipt_{payload['DocNumber']}_{live.get('Id')}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    print(f"receipt: {path}")
    return 2 if verify else 0


# ---------------------------------------------------------------- offset spec
def offset_spec(w7_summary: dict, approved_v, *, txn_date="2026-10-01", doc_number="INV-EQ-2026-10-01",
                tolerance="1.00", accept_c_difference=False) -> dict:
    b, after = Decimal(w7_summary["B"]), Decimal(w7_summary["B_plus_C"])
    c = after - b
    expected = Decimal(w7_summary.get("C_expected_registered_items", c))
    if w7_summary.get("pending_after_run"):
        raise ValueError(f"W7 still has {w7_summary['pending_after_run']} pending creates; finish them first")
    if w7_summary.get("stopped"):
        raise ValueError("W7 run stopped; reconcile it before computing the offset")
    if abs(c - expected) > Decimal(str(tolerance)) and not accept_c_difference:
        raise ValueError(f"actual C {c} differs from the registered opening value {expected} by {c - expected}; "
                         "explain it (or pass --accept-c-difference with the reason in the approval)")
    result = create_night_offset(b, c, approved_v)
    amount = Decimal(result["credit_inventory_77"]).quantize(TWO_DP)
    if amount == 0:
        raise ValueError("offset is zero; no journal needed")
    credit_ia = amount > 0
    amt = str(abs(amount))
    lines = [
        {"account_id": EQUITY_300150_ID, "posting_type": "Debit" if credit_ia else "Credit", "amount": amt,
         "description": "Create-night offset: legacy IA balance not in 30 Sep EPOS opening (300150)"},
        {"account_id": IA_ID, "posting_type": "Credit" if credit_ia else "Debit", "amount": amt,
         "description": f"Bring Inventory Asset 77 to approved 30 Sep EPOS opening {Decimal(str(approved_v)).quantize(TWO_DP)}"},
    ]
    return {
        "doc_number": doc_number, "txn_date": txn_date, "status": "REVIEWED (computed; needs chat yes)",
        "private_note": (f"AKPONORA W7 create-night IA offset. B {b} + C {c} - V {approved_v} = {amount}. "
                         "Credit IA 77 / debit 300150 (negative reverses). IA 77 after = V. "
                         "Per chat approval."),
        "lines": lines,
        "_computation": {**result, "C_expected_registered_items": str(expected), "C_difference": str(c - expected),
                         "source": "operations_controls.create_night_offset"},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post", help="dry-run (default) or --execute one journal spec")
    p.add_argument("--spec", required=True, type=Path)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="the default")
    g.add_argument("--execute", action="store_true")
    p.add_argument("--expect-sha", default="")
    p.add_argument("--approval-ref", default="")
    p.add_argument("--receipt-dir", type=Path, default=None, help="default: next to the spec")
    p.add_argument("--no-live", action="store_true", help="offline validation only")
    o = sub.add_parser("offset", help="compute B + C - V and write a journal spec")
    o.add_argument("--w7-summary", required=True, type=Path, help="w7 out/summary_execute.json")
    o.add_argument("--approved-v", required=True, help="approved 30 Sep opening valuation V (ex-tax)")
    o.add_argument("--out", required=True, type=Path)
    o.add_argument("--txn-date", default="2026-10-01", help="same day as InvStartDate so IA as of 1 Oct = V")
    o.add_argument("--doc-number", default="INV-EQ-2026-10-01")
    o.add_argument("--tolerance", default="1.00")
    o.add_argument("--accept-c-difference", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "offset":
            spec = offset_spec(json.loads(a.w7_summary.read_text()), a.approved_v, txn_date=a.txn_date,
                               doc_number=a.doc_number, tolerance=a.tolerance,
                               accept_c_difference=a.accept_c_difference)
            validate_spec(spec)
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(json.dumps(spec, indent=2) + "\n")
            print(json.dumps(spec, indent=2))
            return 0
        client = None if a.no_live else QBOClient.for_company_a(allow_writes=a.execute)
        return run_post(a.spec, execute=a.execute, expect_sha=a.expect_sha, client=client,
                        receipt_dir=a.receipt_dir or a.spec.parent, approval_ref=a.approval_ref)
    except (StopRun, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

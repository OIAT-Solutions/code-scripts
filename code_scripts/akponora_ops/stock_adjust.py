"""Akponora stock count corrections -> QuickBooks InventoryAdjustment (plan, then approved post).

Input is a checked count draft from ``stock_reconciliation count-plan`` (every line carries the
count source, who confirmed it, the reason and the reconciled transaction cutoff). This tool adds
what the draft deliberately lacks:

``plan``  READ-ONLY. Re-checks the draft checksum, the offset account (an active Cost of Goods
          Sold account, e.g. 82 Inventory Shrinkage) and every item live in QuickBooks (active,
          Inventory, tracks quantity, same SKU). Builds one InventoryAdjustment per 100 lines,
          dated the cutoff, with QtyDiff = verified count - book quantity at the cutoff.
          Writes ``summary.json`` (``payloads_sha256``), ``payloads.jsonl``, ``review.csv``.
``post``  WRITES. Needs ``--approval-ref`` and ``--expect-sha`` (the plan's payloads_sha256).
          Never posts an adjustment twice (DocNumber and the draft checksum in the memo), sends an
          Intuit requestid, reads the adjustment back and checks each item's quantity moved by
          exactly QtyDiff. Results go to ``results.csv`` / ``post_summary.json``.

    python -m code_scripts.akponora_ops.stock_adjust plan --draft <count_draft/plan.json> --out <dir>
    python -m code_scripts.akponora_ops.stock_adjust post --plan-dir <dir> --approval-ref "..." --expect-sha <sha>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from code_scripts.akponora_ops.common import COMPANY, dump_json, write_csv
from code_scripts.akponora_ops.stock_reconciliation import digest

TOOL = "stock_adjust"
MAX_LINES = 100
COGS = "Cost of Goods Sold"
READY, HOLD, DONE = "READY", "HOLD", "DONE"
POSTED, ALREADY_DONE, FAILED = "POSTED", "ALREADY_DONE", "FAILED"


class StopPost(RuntimeError):
    pass


def dec(value) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def qty_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def doc_number(cutoff: str, draft_sha: str, n: int) -> str:
    return f"SC{cutoff[2:4]}{cutoff[5:7]}{cutoff[8:10]}-{draft_sha[:6]}-{n}"  # <= 21 characters


def payloads_sha(payloads: list[dict]) -> str:
    return hashlib.sha256("\n".join(json.dumps(p, sort_keys=True) for p in payloads).encode()).hexdigest()


def check_draft(draft: dict) -> str:
    if draft.get("company") != COMPANY or draft.get("kind") != "count_variance_draft":
        raise StopPost("not an Akponora count draft from stock_reconciliation count-plan")
    body = {k: v for k, v in draft.items() if k != "plan_sha256"}
    if digest(body) != draft.get("plan_sha256"):
        raise StopPost("the count draft was changed after it was made (plan_sha256 does not match)")
    if date.fromisoformat(draft["cutoff"]) < date(2026, 10, 1):
        raise StopPost("only counts from 1 October 2026 are supported")
    if not draft.get("entries"):
        raise StopPost("the count draft has no lines")
    return draft["plan_sha256"]


def existing_adjustments(client, cutoff: str, draft_sha: str) -> list[dict]:
    """This draft's adjustments already in QuickBooks (DocNumber prefix or the checksum in the memo)."""
    rows = client.query_all(f"select * from InventoryAdjustment where TxnDate = '{cutoff}'", "InventoryAdjustment")
    prefix = doc_number(cutoff, draft_sha, 0)[:-1]
    return [r for r in rows if str(r.get("DocNumber") or "").startswith(prefix) or draft_sha[:16] in str(r.get("PrivateNote") or "")]


def plan(draft: dict, client, out: Path) -> dict:
    draft_sha = check_draft(draft)
    cutoff, holds, rows = draft["cutoff"], [], []
    acct = client.get_json(f"/account/{draft['offset_account_id']}")["Account"]
    if acct.get("Active") is False or acct.get("AccountType") != COGS:
        holds.append(f"offset account {acct.get('Id')} {acct.get('Name')} is not an active {COGS} account")
    lines = []
    for e in draft["entries"]:
        change = dec(e["quantity_change"])
        row = {"sku": e["sku"], "qbo_item_id": str(e["qbo_item_id"]), "book_qty_at_cutoff": e["quantity_at_cutoff"],
               "verified_qty": e["verified_quantity"], "qty_diff": qty_text(change), "live_qty": "", "problem": ""}
        item = client.get_json(f"/item/{e['qbo_item_id']}")["Item"]
        row["live_qty"] = qty_text(dec(item.get("QtyOnHand")))
        row["name"] = item.get("Name", "")
        if item.get("Type") != "Inventory" or item.get("Active") is False or item.get("TrackQtyOnHand") is not True:
            row["problem"] = "not an active quantity-tracked Inventory item in QuickBooks"
        elif str(item.get("Sku") or "") != e["sku"]:
            row["problem"] = f"QuickBooks SKU is {item.get('Sku')!r}, the draft says {e['sku']!r}"
        if row["problem"]:
            holds.append(f"{e['sku']}: {row['problem']}")
        rows.append(row)
        if change:
            lines.append({"DetailType": "ItemAdjustmentLineDetail",
                          "ItemAdjustmentLineDetail": {"ItemRef": {"value": str(e["qbo_item_id"])}, "QtyDiff": float(change)}})
    payloads = []
    for n, start in enumerate(range(0, len(lines), MAX_LINES), 1):
        doc = doc_number(cutoff, draft_sha, n)
        payloads.append({"TxnDate": cutoff, "DocNumber": doc, "AdjustAccountRef": {"value": str(acct.get("Id"))},
                         "PrivateNote": (f"Stock count variance {cutoff} | {doc} | draft {draft_sha[:16]} | "
                                         f"{str(draft.get('accounting_basis') or '')[:200]} | created by {TOOL}")[:4000],
                         "Line": lines[start:start + MAX_LINES]})
    done = existing_adjustments(client, cutoff, draft_sha)
    status = DONE if done and len(done) >= len(payloads) else HOLD if holds or not payloads else READY
    if not payloads:
        holds.append("every line already matches its count (no quantity to change)")
    summary = {"tool": TOOL, "company": COMPANY, "cutoff": cutoff, "status": status, "draft_sha256": draft_sha,
               "offset_account": {"id": str(acct.get("Id")), "name": acct.get("Name"), "type": acct.get("AccountType")},
               "lines": len(lines), "adjustments": len(payloads), "already_in_qbo": [r.get("Id") for r in done],
               "units_added": qty_text(sum((dec(r["qty_diff"]) for r in rows if dec(r["qty_diff"]) > 0), Decimal(0))),
               "units_removed": qty_text(-sum((dec(r["qty_diff"]) for r in rows if dec(r["qty_diff"]) < 0), Decimal(0))),
               "holds": holds, "payloads_sha256": payloads_sha(payloads) if status == READY else "",
               "accounting_basis": draft.get("accounting_basis", ""), "financial_writes": False}
    out.mkdir(parents=True, exist_ok=True)
    (out / "payloads.jsonl").write_text("".join(json.dumps(p, sort_keys=True) + "\n" for p in payloads))
    write_csv(out / "review.csv", rows, ["sku", "name", "qbo_item_id", "book_qty_at_cutoff", "verified_qty",
                                         "qty_diff", "live_qty", "problem"])
    dump_json(out / "draft.json", draft)
    dump_json(out / "summary.json", summary)
    return summary


def post(plan_dir: Path, *, client, approval_ref: str, expect_sha: str) -> dict:
    if not approval_ref.strip():
        raise StopPost("an approval reference is required")
    summary = json.loads((plan_dir / "summary.json").read_text())
    payloads = [json.loads(x) for x in (plan_dir / "payloads.jsonl").read_text().splitlines() if x.strip()]
    if summary.get("status") != READY or not expect_sha or payloads_sha(payloads) != expect_sha \
            or summary.get("payloads_sha256") != expect_sha:
        raise StopPost("the plan is not READY or changed since it was approved (payloads_sha256 mismatch)")
    check_draft(json.loads((plan_dir / "draft.json").read_text()))
    results, done = [], {str(r.get("DocNumber") or ""): r for r in
                         existing_adjustments(client, summary["cutoff"], summary["draft_sha256"])}
    for p in payloads:
        doc = p["DocNumber"]
        if doc in done:
            results.append({"doc": doc, "status": ALREADY_DONE, "qbo_id": done[doc].get("Id"), "detail": ""})
            continue
        ids = [ln["ItemAdjustmentLineDetail"]["ItemRef"]["value"] for ln in p["Line"]]
        before = {i: dec(client.get_json(f"/item/{i}")["Item"].get("QtyOnHand")) for i in ids}
        body = dict(p, PrivateNote=f"{p['PrivateNote']} | approval {approval_ref}"[:4000])
        rid = hashlib.sha256(f"{TOOL}|{doc}|{expect_sha}".encode()).hexdigest()[:36]
        resp = client.post_json("/inventoryadjustment", body, rid)
        if resp.status_code != 200:
            results.append({"doc": doc, "status": FAILED, "qbo_id": "", "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"})
            break  # stop: later adjustments wait for a person
        made = resp.json().get("InventoryAdjustment", {})
        back = client.get_json(f"/inventoryadjustment/{made.get('Id')}")["InventoryAdjustment"]
        wrong = []
        if [(ln["ItemAdjustmentLineDetail"]["ItemRef"]["value"], dec(ln["ItemAdjustmentLineDetail"]["QtyDiff"]))
                for ln in back.get("Line", [])] != [(i, dec(ln["ItemAdjustmentLineDetail"]["QtyDiff"]))
                                                     for i, ln in zip(ids, p["Line"])]:
            wrong.append("QuickBooks saved different lines than were approved")
        for i, ln in zip(ids, p["Line"]):
            after = dec(client.get_json(f"/item/{i}")["Item"].get("QtyOnHand"))
            if after != before[i] + dec(ln["ItemAdjustmentLineDetail"]["QtyDiff"]):
                wrong.append(f"item {i}: quantity {qty_text(before[i])} -> {qty_text(after)}, "
                             f"expected {qty_text(before[i] + dec(ln['ItemAdjustmentLineDetail']['QtyDiff']))} "
                             "(a sale or bill may have posted at the same time; check)")
        results.append({"doc": doc, "status": POSTED, "qbo_id": made.get("Id"), "detail": "; ".join(wrong)})
    complete = all(r["status"] in (POSTED, ALREADY_DONE) for r in results) and len(results) == len(payloads)
    out = {"tool": TOOL, "cutoff": summary["cutoff"], "approval_ref": approval_ref, "complete": complete,
           "results": results, "warnings": [r["detail"] for r in results if r["status"] == POSTED and r["detail"]]}
    write_csv(plan_dir / "results.csv", results, ["doc", "status", "qbo_id", "detail"])
    dump_json(plan_dir / "post_summary.json", out)
    return out


def main(argv=None) -> int:
    from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="READ-ONLY")
    p.add_argument("--draft", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("post", help="WRITES InventoryAdjustment")
    p.add_argument("--plan-dir", required=True, type=Path)
    p.add_argument("--approval-ref", default="")
    p.add_argument("--expect-sha", default="")
    p.add_argument("--no-slack", action="store_true")  # accepted for the portal's tool call shape
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plan":
            res = plan(json.loads(a.draft.read_text()), QBOClient.for_company_a(allow_writes=False), a.out)
            print(json.dumps({k: v for k, v in res.items() if k != "accounting_basis"}, indent=1))
            return 0 if res["status"] in (READY, DONE) else 3
        res = post(a.plan_dir, client=QBOClient.for_company_a(allow_writes=True), approval_ref=a.approval_ref,
                   expect_sha=a.expect_sha)
    except StopPost as exc:
        print(f"STOP: {exc}")
        return 2
    print(json.dumps(res, indent=1, default=str))
    return 0 if res["complete"] and not res["warnings"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

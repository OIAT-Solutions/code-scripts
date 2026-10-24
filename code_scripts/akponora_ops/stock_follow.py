"""QuickBooks follows EPOS stock adjustments (owner decisions, Marvin chat 11 Oct 2026).

Staff use two EPOS reasons: ``Stock Take`` (a recount before a new PO: EPOS corrected to the shelf) and
``Wastage`` (spoilt stock). Each such adjustment becomes a QuickBooks InventoryAdjustment on the same day:

* Stock Take -> account 82 Inventory Shrinkage (count difference), DocNumber ``ST<yymmdd>-<hash>``
* Wastage    -> account 83 Stock Wastage, DocNumber ``WS<yymmdd>-<hash>`` (hash of the event ids: re-runs give
  the same number, so an adjustment can never be posted twice)
* no reason (EPOS "n/a"), New Stock, New Product -> delivery/PO review before any posting
  (whole-process hardening, owner 24 Oct 2026). A possible delivery must not also become a count correction.
* anything else (Returns, ...), a line whose product/unit is not
  certain (``holds`` / no quantity), or one worth more than the cap (N100,000 by EPOS cost) is not posted
  automatically: over-cap Stock Take/Wastage go to a plan the owner approves in Needs your attention;
  the rest stay as classification cards.

Source: the read-only movement captures (``stock_movements``) - each event is one product line of one EPOS
adjustment, keyed by transfer + product + occurrence (``event_id``; capture time zone does not change it).
Display times are converted to the Lagos business date per capture (server captures show UTC; the
6 Oct recovery capture was read on a Toronto-time Mac).

Never twice: ``posted.json`` (event_id -> adjustment) plus the event ids in each adjustment's memo; an
adjustment whose DocNumber already exists is not re-posted. Every post re-reads each item's quantity.

``plan``   READ-ONLY: build ``summary.json`` / ``payloads.jsonl`` (``payloads_sha256``) for a set of events.
``post``   WRITES with ``--approval-ref`` + ``--expect-sha`` (portal Approve, or the nightly auto post).
``nightly`` daily_run hook: after the catch-up is posted, auto-post this run's eligible events under the cap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import COMPANY, REALM, dump_json, state_dir, write_csv
from code_scripts.akponora_ops.work_state import WorkState, digest

TOOL = "stock_follow"
LAGOS = ZoneInfo("Africa/Lagos")
FLOOR = "2026-10-01"
KINDS = {"stock take": ("ST", "82"), "wastage": ("WS", "83"),
         "n/a": ("NA", "82"), "": ("NA", "82"), "new stock": ("NA", "82"), "new product": ("NA", "82")}
CAP_ENV, POST_ENV = "OIAT_COMPANY_A_STOCK_FOLLOW_CAP", "OIAT_COMPANY_A_STOCK_FOLLOW_POST"
ST_ACCT_ENV, WS_ACCT_ENV = "OIAT_COMPANY_A_STOCK_FOLLOW_ST_ACCOUNT", "OIAT_COMPANY_A_STOCK_FOLLOW_WS_ACCOUNT"
MAX_LINES = 100
READY, HOLD, DONE = "READY", "HOLD", "DONE"
# Capture folders read on a Toronto-time Mac (EPOS shows times in the viewer's zone); everything else is UTC.
TORONTO_CAPTURES = ("stock_recovery_20261006",)


def ops_root() -> Path:
    """STATE_ROOT/ops/company_a."""
    return state_dir(TOOL).parent


def money(text) -> Decimal:
    t = str(text or "").replace("₦", "").replace(",", "").strip()
    try:
        return Decimal(t) if t else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def lagos_day(display: str, source: str) -> str:
    zone = ZoneInfo("America/Toronto") if any(m in str(source) for m in TORONTO_CAPTURES) else ZoneInfo("UTC")
    when = datetime.strptime(display.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=zone)
    return when.astimezone(LAGOS).date().isoformat()


def settings(env=None) -> dict:
    env = os.environ if env is None else env
    return {"cap": money(env.get(CAP_ENV)) or Decimal("100000"),
            "post": str(env.get(POST_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"},
            "accounts": {"ST": str(env.get(ST_ACCT_ENV) or "82"), "WS": str(env.get(WS_ACCT_ENV) or "83"),
                         "NA": str(env.get(ST_ACCT_ENV) or "82")}}


# ---------------------------------------------------------------- events
def collect(sources) -> dict:
    """event_id -> event (+ ``_day`` Lagos date, ``_source``) from movement summary files."""
    out = {}
    for path in sources:
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if doc.get("company") != COMPANY:
            continue
        for e in doc.get("events") or []:
            try:
                day = lagos_day(e["occurred_at_epos"], path)
            except (KeyError, ValueError):
                continue
            prior = out.get(e["event_id"])
            if prior and "/daily/" in prior["_source"] and "/daily/" not in str(path):
                continue  # keep the server's own reading of an event
            out[e["event_id"]] = {**e, "_day": day, "_source": str(path)}
    return out


def all_captures() -> list[Path]:
    root = ops_root()
    return sorted(root.glob("daily/*/run_*/movements/summary.json")) + sorted(root.glob("portal_reads/*/movements/summary.json"))


def classify(e: dict, cap: Decimal) -> tuple[str, str]:
    """(kind 'ST'/'WS' or '', why it is not automatic or '')."""
    kind = KINDS.get(str(e.get("epos_reason") or "").strip().casefold())
    if not kind:
        return "", f"EPOS reason '{e.get('epos_reason')}' is not Stock Take, Wastage, New Stock or no reason"
    if e.get("holds") or e.get("canonical_qty_change") in (None, "") or not e.get("qbo_item_id"):
        return kind[0], "product or unit not certain: " + "; ".join(e.get("holds") or ["no QuickBooks quantity"])
    if Decimal(str(e["canonical_qty_change"])) == 0:
        return kind[0], "no quantity change"
    if e["_day"] < FLOOR:
        return kind[0], "before 1 October"
    if e.get("_audit_only"):
        return kind[0], "older movement discovered by completeness audit; explicit review required"
    if kind[0] == "NA":
        return kind[0], "delivery/PO review required for New Stock or an unexplained change"
    if e.get("delivery_overlap"):
        return kind[0], "possible delivery also recorded by a PO: " + ", ".join(e["delivery_overlap"])
    try:
        raw_value = str(e.get("source_value_change") or "").replace("₦", "").replace(",", "").strip()
        value = Decimal(raw_value)
        if not value.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return kind[0], "stock-change value is missing or invalid; approval review required"
    if money(e.get("_transfer_gross")) > cap:
        return kind[0], f"worth N{money(e['_transfer_gross']):,.2f} across this adjustment, over the N{cap:,.0f} limit"
    if abs(money(e.get("source_value_change"))) > cap:
        return kind[0], f"worth N{abs(money(e.get('source_value_change'))):,.2f}, over the N{cap:,.0f} limit"
    return kind[0], ""


def ledger_path() -> Path:
    return state_dir(TOOL) / "posted.json"


def ledger() -> dict:
    p = ledger_path()
    return json.loads(p.read_text()) if p.exists() else {}


def event_sha(e):
    return digest({k: e.get(k) for k in ("event_id", "transfer_id", "_day", "epos_product_id", "qbo_item_id",
                                        "canonical_qty_change", "epos_reason", "source_value_change")})


def delivery_overlaps(events, bills_dir):
    """Conservative review signal, never a claim that two records describe one delivery."""
    from code_scripts.akponora_ops.common import read_csv
    plan_path = Path(bills_dir) / "plan.json"
    lines_path = Path(bills_dir) / "review_lines.csv"
    if not plan_path.exists() or not lines_path.exists():
        return events
    plan = json.loads(plan_path.read_text())
    dates = {str(e["po"]["ref"]): e["po"]["received_date"] for e in plan.get("entries", [])}
    received = defaultdict(Decimal)
    for row in read_csv(lines_path):
        if row.get("QBO Qty") and row.get("QBO Item Id"):
            received[(row["PO"], row["QBO Item Id"])] += Decimal(row["QBO Qty"])
    out = {}
    for identity, event in events.items():
        e = dict(event)
        qty = Decimal(str(e.get("canonical_qty_change") or 0))
        matches = [ref for (ref, iid), amount in received.items() if qty > 0 and iid == e.get("qbo_item_id")
                   and qty == amount and ref in dates
                   and abs((datetime.fromisoformat(e["_day"]) - datetime.fromisoformat(dates[ref])).days) <= 1]
        if matches:
            e["delivery_overlap"] = sorted(set(matches))
        out[identity] = e
    return out


def with_transfer_totals(events):
    totals = defaultdict(Decimal)
    for e in events.values():
        totals[e["transfer_id"]] += abs(money(e.get("source_value_change")))
    return {k: {**e, "_transfer_gross": str(totals[e["transfer_id"]])} for k, e in events.items()}


# ---------------------------------------------------------------- plan
def build_payloads(events: list[dict], s: dict, *, tag: str) -> list[dict]:
    """One InventoryAdjustment per (day, kind), at most MAX_LINES lines; numbered per day/kind with ``tag``."""
    groups = defaultdict(list)
    for e in sorted(events, key=lambda x: (x["_day"], x["transfer_id"], x["event_id"])):
        groups[(e["_day"], classify(e, Decimal("1e30"))[0])].append(e)
    out = []
    for (day, kind), evs in sorted(groups.items()):
        for start in range(0, len(evs), MAX_LINES):
            chunk = evs[start:start + MAX_LINES]
            h = hashlib.sha256("|".join(e["event_id"] for e in chunk).encode()).hexdigest()[:5]
            doc = f"{kind}{day[2:4]}{day[5:7]}{day[8:10]}-{tag}{h}"[:21]  # same events -> same number, never two
            per_item = {}  # QuickBooks refuses the same item twice in one adjustment: one line per item
            for e in chunk:
                ln = per_item.setdefault(str(e["qbo_item_id"]), {"qty": Decimal(0), "refs": []})
                ln["qty"] += Decimal(str(e["canonical_qty_change"]))
                ln["refs"].append(f"EPOS {e['transfer_id']} {e['epos_reason']} by {e.get('staff') or '?'}: "
                                  f"{e['name']} {e['canonical_qty_change']}")
            lines = [{"DetailType": "ItemAdjustmentLineDetail", "Description": "; ".join(v["refs"])[:4000],
                      "ItemAdjustmentLineDetail": {"ItemRef": {"value": item}, "QtyDiff": float(v["qty"])}}
                     for item, v in per_item.items() if v["qty"] != 0]
            what = {"ST": "Stock count correction (EPOS Stock Take)", "WS": "Stock wastage (EPOS Wastage)",
                    "NA": "Stock change with no EPOS reason (n/a, New Stock, New Product)"}[kind]
            out.append({"TxnDate": day, "DocNumber": doc, "AdjustAccountRef": {"value": s["accounts"][kind]},
                        "PrivateNote": (f"{what} {day} | {doc} | events {','.join(e['event_id'][:10] for e in chunk)} "
                                        f"| created by {TOOL}")[:4000],
                        "Line": lines, "_events": [e["event_id"] for e in chunk],
                        "_source_shas": {e["event_id"]: event_sha(e) for e in chunk}})
    return out


def payloads_sha(payloads: list[dict]) -> str:
    return hashlib.sha256("\n".join(json.dumps(p, sort_keys=True) for p in payloads).encode()).hexdigest()


def plan(events: dict, out: Path, *, s: dict, approve_over_cap: bool, tag: str = "", title: str = "") -> dict:
    """Write a plan of eligible events not yet posted. ``approve_over_cap`` includes over-cap lines (the owner
    approves them explicitly); otherwise only lines within the cap."""
    done = ledger()
    events = with_transfer_totals(events)
    rows, chosen = [], []
    for e in sorted(events.values(), key=lambda x: (x["_day"], x["transfer_id"])):
        kind, why = classify(e, s["cap"])
        edited = done.get(e["event_id"], {}).get("source_sha") not in (None, event_sha(e))
        if edited:
            why = "previously posted stock movement was edited; review the original adjustment"
        over = why.startswith("worth ")
        take = kind and e["event_id"] not in done and (not why or (over and approve_over_cap))
        rows.append({"day": e["_day"], "epos_ref": e["transfer_id"], "staff": e.get("staff"), "reason": e.get("epos_reason"),
                     "product": e["name"], "qty_change": e.get("canonical_qty_change"),
                     "value": str(money(e.get("source_value_change"))),
                     "status": f"not automatic: {why}" if edited else "already posted" if e["event_id"] in done else "in this plan" if take else f"not automatic: {why}"})
        if take:
            chosen.append(e)
    payloads = build_payloads(chosen, s, tag=tag)
    value = sum((money(e.get("source_value_change")) for e in chosen), Decimal(0))
    summary = {"tool": TOOL, "company": COMPANY, "title": title, "status": READY if payloads else DONE,
               "events": len(chosen), "adjustments": len(payloads), "value_net": f"{value:.2f}",
               "added_units": str(sum(Decimal(str(e["canonical_qty_change"])) for e in chosen if Decimal(str(e["canonical_qty_change"])) > 0)),
               "removed_units": str(-sum(Decimal(str(e["canonical_qty_change"])) for e in chosen if Decimal(str(e["canonical_qty_change"])) < 0)),
               "days": sorted({e["_day"] for e in chosen}), "accounts": s["accounts"],
               "payloads_sha256": payloads_sha(payloads) if payloads else "", "financial_writes": False}
    summary["exposure_absolute"] = str(sum(abs(money(e.get("source_value_change"))) for e in chosen))
    summary["review_count"] = sum(str(r["status"]).startswith("not automatic:") for r in rows)
    out.mkdir(parents=True, exist_ok=True)
    (out / "payloads.jsonl").write_text("".join(json.dumps(p, sort_keys=True) + "\n" for p in payloads))
    write_csv(out / "review.csv", rows, ["day", "epos_ref", "staff", "reason", "product", "qty_change", "value", "status"])
    dump_json(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- post
class StopPost(RuntimeError):
    pass


def post(plan_dir: Path, *, client, approval_ref: str, expect_sha: str) -> dict:
    if not approval_ref.strip():
        raise StopPost("an approval reference is required")
    summary = json.loads((plan_dir / "summary.json").read_text())
    payloads = [json.loads(x) for x in (plan_dir / "payloads.jsonl").read_text().splitlines() if x.strip()]
    if summary.get("status") != READY or not expect_sha or payloads_sha(payloads) != expect_sha:
        raise StopPost("the plan is not READY or changed since it was approved (payloads_sha256 mismatch)")
    book = ledger()
    journal = WorkState(state_dir(TOOL) / "work.sqlite3")
    results = []
    for p in payloads:
        events, doc = p.pop("_events"), p["DocNumber"]
        source_shas = p.pop("_source_shas", {})
        if all(e in book for e in events):
            results.append({"doc": doc, "status": "ALREADY_DONE", "qbo_id": book[events[0]]["adjustment"], "detail": ""})
            continue
        existing = [x for x in client.query_all(f"select * from InventoryAdjustment where TxnDate = '{p['TxnDate']}'",
                                                "InventoryAdjustment") if str(x.get("DocNumber")) == doc]
        if existing:  # posted before the ledger was written (same events -> same number): record it, never re-post
            if len(existing) != 1 or adjustment_signature(existing[0]) != adjustment_signature(p):
                raise StopPost("existing stock adjustment has conflicting accounting details")
            for e in events:
                book[e] = {"adjustment": existing[0]["Id"], "doc": doc, "at": "found in QuickBooks",
                           "source_sha": source_shas.get(e)}
            dump_json(ledger_path(), book)
            journal.put("stock", doc, digest(p), "verified", {"adjustment": existing[0]["Id"], "events": events})
            results.append({"doc": doc, "status": "ALREADY_DONE", "qbo_id": existing[0]["Id"], "detail": "found in QuickBooks"})
            continue
        ids = [ln["ItemAdjustmentLineDetail"]["ItemRef"]["value"] for ln in p["Line"]]
        want = defaultdict(Decimal)
        for i, ln in zip(ids, p["Line"]):
            want[i] += Decimal(str(ln["ItemAdjustmentLineDetail"]["QtyDiff"]))
        before = {i: Decimal(str(client.get_json(f"/item/{i}")["Item"].get("QtyOnHand") or 0)) for i in want}
        body = dict(p, PrivateNote=f"{p['PrivateNote']} | approval {approval_ref}"[:4000])
        rid = hashlib.sha256(f"{TOOL}|{REALM}|{doc}|{expect_sha}".encode()).hexdigest()[:36]
        prior = journal.get("stock", doc)
        if prior and prior["status"] in ("sending", "unknown"):
            raise StopPost("previous adjustment outcome is uncertain; reconcile its original document before retrying")
        journal.put("stock", doc, digest(p), "sending", {"payload": p, "events": events, "source_shas": source_shas})
        try:
            resp = client.post_json("/inventoryadjustment", body, rid)
        except Exception as exc:
            journal.put("stock", doc, digest(p), "unknown", {"payload": p, "events": events, "source_shas": source_shas,
                                                             "reason": str(exc)})
            raise
        made = (resp.json() or {}).get("InventoryAdjustment", {}) if getattr(resp, "status_code", 0) == 200 else {}
        if not made.get("Id"):
            results.append({"doc": doc, "status": "FAILED", "qbo_id": "",
                            "detail": f"HTTP {getattr(resp, 'status_code', '?')}: {str(getattr(resp, 'text', ''))[:300]}"})
            break
        for e in events:
            book[e] = {"adjustment": made["Id"], "doc": doc, "at": datetime.now(LAGOS).isoformat(timespec="seconds"),
                       "source_sha": source_shas.get(e)}
        dump_json(ledger_path(), book)
        journal.put("stock", doc, digest(p), "verified", {"adjustment": made["Id"], "events": events})
        wrong = []
        for i, diff in want.items():
            after = Decimal(str(client.get_json(f"/item/{i}")["Item"].get("QtyOnHand") or 0))
            if after != before[i] + diff:
                wrong.append(f"item {i}: {before[i]} -> {after}, expected {before[i] + diff} (a sale or bill may have posted at the same time)")
        results.append({"doc": doc, "status": "POSTED", "qbo_id": made["Id"], "detail": "; ".join(wrong)})
    complete = len(results) == len(payloads) and all(r["status"] in ("POSTED", "ALREADY_DONE") for r in results)
    res = {"tool": TOOL, "approval_ref": approval_ref, "complete": complete, "results": results,
           "warnings": [r["detail"] for r in results if r["status"] == "POSTED" and r["detail"]]}
    write_csv(plan_dir / "results.csv", results, ["doc", "status", "qbo_id", "detail"])
    dump_json(plan_dir / "post_summary.json", res)
    return res


def adjustment_signature(p):
    return (p.get("TxnDate"), str((p.get("AdjustAccountRef") or {}).get("value")),
            sorted((str((l.get("ItemAdjustmentLineDetail") or {}).get("ItemRef", {}).get("value")),
                    Decimal(str((l.get("ItemAdjustmentLineDetail") or {}).get("QtyDiff", 0))))
                   for l in p.get("Line") or []))


def recover_stock_operations(client, journal):
    book, remaining = ledger(), []
    for row in journal.unresolved("stock"):
        if row["status"] not in ("sending", "unknown"):
            continue
        evidence = row["evidence"]
        payload = evidence.get("payload") or {}
        found = [x for x in client.query_all(
            f"select * from InventoryAdjustment where TxnDate = '{payload.get('TxnDate', '')}'", "InventoryAdjustment")
                 if x.get("DocNumber") == row["identity"]]
        if len(found) != 1 or adjustment_signature(found[0]) != adjustment_signature(payload):
            remaining.append(row["identity"])
            continue
        for eid in evidence["events"]:
            book[eid] = {"adjustment": found[0]["Id"], "doc": row["identity"], "at": "recovered in QuickBooks",
                         "source_sha": evidence.get("source_shas", {}).get(eid)}
        dump_json(ledger_path(), book)
        journal.put("stock", row["identity"], row["source_sha"], "verified", {"adjustment": found[0]["Id"]})
    return remaining


# ---------------------------------------------------------------- nightly (daily_run)
def catchup_done() -> bool:
    return (state_dir(TOOL) / "catchup_posted.json").exists()


def nightly(movements_summary: Path, out: Path, *, client=None, write_client=None, env=None, dry_run=False,
            bills_dir=None, auto_from_date=None) -> dict:
    """Auto-post this run's eligible events (within the cap) once the catch-up is posted; over-cap ones go to
    an approval plan under portal_reads/stock_follow/."""
    s = settings(env)
    capture = json.loads(movements_summary.read_text())
    events = with_transfer_totals(collect([movements_summary]))
    if auto_from_date:
        events = {identity: {**e, "_audit_only": e["_day"] < auto_from_date} for identity, e in events.items()}
    if bills_dir is not None:
        events = delivery_overlaps(events, bills_dir)
    res = {"events": len(events), "posted": [], "waiting_approval": 0, "not_automatic": 0, "note": ""}
    if capture.get("errors") or capture.get("status") == "INCOMPLETE":
        res.update(not_automatic=len(events), note="stock capture incomplete; no automatic adjustment is allowed",
                   warnings=["complete the missing stock-movement evidence before posting"])
        return res
    if not dry_run:
        journal = WorkState(state_dir(TOOL) / "work.sqlite3")
        checker = client or write_client
        pending = recover_stock_operations(checker, journal) if checker else [
            r for r in journal.unresolved("stock") if r["status"] in ("sending", "unknown")]
        if pending:
            res.update(not_automatic=len(events), note="uncertain prior stock write needs reconciliation",
                       warnings=["reconcile the original adjustment document before preparing another event group"])
            return res
    done = ledger()
    for e in events.values():
        if e["event_id"] in done:
            original = done[e["event_id"]].get("source_sha")
            prior_source = journal.get("stock_source", e["event_id"]) if not dry_run else None
            expected = original or (prior_source or {}).get("source_sha")
            if expected not in (None, event_sha(e)):
                res["not_automatic"] += 1
                res.setdefault("warnings", []).append("a previously posted stock movement was edited; reconcile the original adjustment")
            if not original and not prior_source:
                res["historical_baselines_needed"] = res.get("historical_baselines_needed", 0) + 1
            if not dry_run and not prior_source:
                journal.put("stock_source", e["event_id"], event_sha(e), "verified", {
                    "transfer_id": e["transfer_id"], "original_posting_checksum_available": bool(original)})
            continue
        kind, why = classify(e, s["cap"])
        if why.startswith("worth "):
            res["waiting_approval"] += 1
        elif why or not kind:
            res["not_automatic"] += 1
    if not catchup_done():
        res["note"] = "waiting for the catch-up (1 Oct onward) to be approved and posted"
        return res
    p = plan(events, Path(out), s=s, approve_over_cap=False, title="nightly")
    if res["waiting_approval"]:
        stamp = datetime.now(LAGOS).strftime("%Y%m%d")
        plan(events, ops_root() / "portal_reads" / "stock_follow" / f"over_cap_{stamp}", s=s, approve_over_cap=True,
             tag="L", title=f"Large stock adjustments read {stamp}")
    if p["status"] != READY or not s["post"] or dry_run or write_client is None:
        res["note"] = "nothing new" if p["status"] != READY else "planned, not posted (posting off or dry run)"
        return res
    r = post(Path(out), client=write_client, approval_ref="Marvin chat yes 2026-10-11/12: QBO follows EPOS Stock Take, Wastage and no-reason changes",
             expect_sha=p["payloads_sha256"])
    res["posted"] = r["results"]
    res["warnings"] = r["warnings"]
    return res


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("catchup", help="READ-ONLY: plans for every captured event since 1 Oct (within / over cap)")
    p.add_argument("--out", type=Path, default=None)
    p = sub.add_parser("post", help="WRITES InventoryAdjustments from an approved plan")
    p.add_argument("--plan-dir", required=True, type=Path)
    p.add_argument("--approval-ref", default="")
    p.add_argument("--expect-sha", default="")
    a = ap.parse_args(argv)
    if a.cmd == "catchup":
        s = settings()
        events = collect(all_captures())
        root = a.out or (ops_root() / "portal_reads" / "stock_follow")
        small = dict(events)
        big = {k: v for k, v in events.items() if classify(v, s["cap"])[1].startswith("worth ")}
        r1 = plan({k: v for k, v in small.items() if k not in big}, root / "catchup_within_cap", s=s, approve_over_cap=False,
                  tag="C", title="Catch-up since 1 Oct: stock takes and wastage within the limit")
        large = {}
        for k, e in big.items():  # one card per large line: each is decided on its own
            name = f"catchup_large_{e['_day']}_{e['transfer_id']}_{k[:6]}"
            large[name] = plan({k: e}, root / name, s=s, approve_over_cap=True, tag="CL",
                               title=f"Large stock adjustment {e['_day']}: {e['name']} {e['canonical_qty_change']} "
                                     f"(EPOS {e['transfer_id']}, {e.get('epos_reason')} by {e.get('staff')})")
        print(json.dumps({"within_cap": r1, "large": large}, indent=1, default=str))
        return 0
    try:
        res = post(a.plan_dir, client=QBOClient.for_company_a(allow_writes=True), approval_ref=a.approval_ref,
                   expect_sha=a.expect_sha)
    except StopPost as exc:
        print(f"STOP: {exc}")
        return 2
    if res["complete"] and "catchup" in a.plan_dir.name:
        dump_json(state_dir(TOOL) / f"{a.plan_dir.name}_posted.json", res)
        if (state_dir(TOOL) / "catchup_within_cap_posted.json").exists():
            dump_json(state_dir(TOOL) / "catchup_posted.json", {"at": datetime.now(LAGOS).isoformat(timespec="seconds")})
    print(json.dumps(res, indent=1, default=str))
    return 0 if res["complete"] and not res["warnings"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

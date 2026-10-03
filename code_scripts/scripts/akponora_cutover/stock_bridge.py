"""Per-product EPOS stock bridge between two stock reports (AKPONORA, company_a).

Subcommands:
  build               OFFLINE. For every stock-report row (tracked "owner" product, qty in owner
                      units = TotalStock):
                        expected_q1 = q0 + PO received qty - sold qty (owner units; child sales
                                      converted via the canonical sale multiplier / owner VolumeOfSale)
                        unexplained_q = q1 - expected_q1
                      Value bridge (zero-floor):
                        close_zf = open_zf + PO ex-tax + COGS booked(-) + revaluation of opening qty
                                   + unexplained qty @ c1 + PO cost-vs-c1 diff + sales cost-vs-c1 diff
                                   + COGS on products with no stock row + zero-floor effect + unmatched rows
                      EPOS manual stock adjustments (from scrape-adjustments) explain the unexplained part.
                      Writes bridge.csv and summary.json.
  scrape-adjustments  VIEW-ONLY EPOS: Stock Takes list (every manual stock-level change) and Stock
                      Movements list from --from-date, opening each row's View Details page. Never
                      clicks Add / Edit / Receive / Delete. Writes epos_stocktakes_list.json,
                      epos_stocktakes_details.jsonl (+ stockmovements). Resumable.

Example (reproduces outputs/nora_gaps_2026-09-25/stock_bridge_0916_0925):
  python -m code_scripts.scripts.akponora_cutover.stock_bridge build --out <dir>
Next window:
  python -m code_scripts.scripts.akponora_cutover.stock_bridge scrape-adjustments --from-date 2026-09-25 --out <ev>
  python -m code_scripts.scripts.akponora_cutover.stock_bridge build --t0 "2026-09-25 13:08" --t1 "2026-09-30 21:00" \
      --stock-open <25 Sep report> --stock-close <30 Sep report> --bookkeeping <BookKeeping CSV> \
      --po-received <bills po_received CSV> --adjustments <ev>/epos_stocktakes_details.jsonl --out <dir>
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import (
    DEFAULT_EVIDENCE_ROOT, GAPS_DIR, company_config, epos_login, resolve_out,
)

TOOL = "stock_bridge"
SEP_DIR = GAPS_DIR / "stock_bridge_0916_0925"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def f(v) -> float:
    t = str(v or "").replace("₦", "").replace(",", "").strip()
    try:
        return float(t) if t else 0.0
    except ValueError:
        return 0.0


def load_catalogue(cat_path):
    cat = json.loads(Path(cat_path).read_text())
    byid = {p["Id"]: p for p in cat}
    byname, bybc = defaultdict(list), defaultdict(list)
    for p in cat:
        byname[norm(p["Name"])].append(p)
        for bc in str(p.get("Barcode") or "").split(","):
            if bc.strip():
                bybc[bc.strip()].append(p)
    return byid, byname, bybc


def read_stock(path, byname, bybc):
    out, how = {}, {}
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        if r["Name"].lower().startswith("total:"):
            continue
        c = byname.get(norm(r["Name"]), [])
        c = [p for p in c if p["IsStockTracked"]] or c
        method = "name"
        if len(c) != 1:
            bc = {p["Id"]: p for b in r["Barcode"].split(",") if b.strip() for p in bybc.get(b.strip(), []) if p["IsStockTracked"]}
            c, method = list(bc.values()), "barcode"
        key = c[0]["Id"] if len(c) == 1 else None
        if key is None or key in out:
            key, method = "NAME:" + norm(r["Name"]), "unmatched"
            while key in out:
                key += "#"
        out[key], how[key] = r, method
    return out, how


def build(a):
    """Offline bridge build; ``a`` carries paths (stock_open, stock_close, bookkeeping, catalogue,
    evidence, po_received, adjustments, out) and the window (t0, t1, shift_hours)."""
    sh = timedelta(hours=a.shift_hours)
    t0 = datetime.strptime(a.t0, "%Y-%m-%d %H:%M") + sh
    t1 = datetime.strptime(a.t1, "%Y-%m-%d %H:%M") + sh

    byid, byname, bybc = load_catalogue(a.catalogue)
    ev = {int(r["EPOS Product ID"]): r for r in csv.DictReader(Path(a.evidence).open(encoding="utf-8-sig"))}
    s16, h16 = read_stock(Path(a.stock_open), byname, bybc)
    s25, h25 = read_stock(Path(a.stock_close), byname, bybc)

    def owner_units(pid: int, qty: float):
        """Return (owner_key, qty in owner stock units, basis) or (None, 0, reason)."""
        e = ev.get(pid)
        p = byid.get(pid)
        if p and p["IsStockTracked"] and (pid in s16 or pid in s25):
            return pid, qty, "own row"
        if not e or not e["Owner Product ID"]:
            return None, 0.0, (e["Role"] if e else "not in catalogue")
        o = int(e["Owner Product ID"])
        ovos = (byid[o].get("VolumeOfSale") or 1) if o in byid else 1
        m = f(e["Proposed sale multiplier (canonical units)"]) or f(e["EPOS VolumeOfSale"]) or 1.0
        basis = "child tier " + e["Tier"] + ("" if e["Proposed sale multiplier (canonical units)"] else " (mult fallback VoS/1)")
        return o, qty * m / ovos, basis

    rec = defaultdict(lambda: defaultdict(float))
    no_row_cogs = defaultdict(float)
    no_row_po = 0.0
    sales_lines = 0
    for r in csv.DictReader(Path(a.bookkeeping).open(encoding="utf-8-sig")):
        if not r["Date/Time"]:
            continue
        ts = datetime.strptime(r["Date/Time"], "%d/%m/%Y %H:%M:%S")
        if not (t0 <= ts < t1):
            continue
        sales_lines += 1
        cost = f(r["Cost Price"])
        pid = int(r["ProductId"]) if r["ProductId"] else None
        o, q, basis = owner_units(pid, f(r["Quantity"])) if pid else (None, 0, "no ProductId")
        if o is None or (o not in s16 and o not in s25):
            no_row_cogs[basis if o is None else "owner has no stock row"] += cost
            continue
        rec[o]["sold_q"] += q
        rec[o]["cogs"] += cost
        if pid != o:
            rec[o]["child_sold_cogs"] += cost
    po_n = set()
    for r in csv.DictReader(Path(a.po_received).open()):
        ts = datetime.fromisoformat(r["received_at_epos_local"])
        if not (t0 <= ts < t1):
            continue
        po_n.add(r["po_ref"])
        o, q, _ = owner_units(int(r["epos_product_id"]), f(r["qty_received"]))
        if o is None or (o not in s16 and o not in s25):
            no_row_po += f(r["line_total_ex_tax"])
            continue
        rec[o]["po_q"] += q
        rec[o]["po_val"] += f(r["line_total_ex_tax"])

    # EPOS stock adjustments ("Stock Takes" list = every manual stock-level change) in the window
    name_key = {}
    for src in (s16, s25):
        for k, r in src.items():
            name_key.setdefault(norm(r["Name"]), k)
    adj = defaultdict(lambda: {"q": 0.0, "val": 0.0, "n": 0, "who": set(), "why": set(), "dates": []})
    adj_unmatched, adj_docs, adj_outside = defaultdict(float), 0, 0
    adj_path = Path(a.adjustments) if a.adjustments else None
    if adj_path and adj_path.exists():
        seen = set()
        for line in adj_path.open():
            d = json.loads(line)
            if d["url"] in seen:
                continue
            seen.add(d["url"])
            info = {r[0]: r[1] for r in (d.get("info") or []) if len(r) == 2}
            when = datetime.strptime(info.get("Date", ""), "%d/%m/%Y %H:%M:%S")
            if not (t0 <= when < t1):
                adj_outside += 1
                continue
            adj_docs += 1
            for it in (d.get("items") or [])[1:]:
                if len(it) < 6 or not it[0]:
                    continue
                k = name_key.get(norm(it[0]))
                if k is None:
                    c = [p for p in byname.get(norm(it[0]), []) if p["IsStockTracked"]]
                    k = c[0]["Id"] if len(c) == 1 else None
                vos = (byid[k].get("VolumeOfSale") or 1) if k in byid else 1
                q = f(it[2]) + f(it[3]) / vos
                if k is None:
                    adj_unmatched[it[0]] += f(it[4])
                    continue
                a_ = adj[k]
                a_["q"] += q
                a_["val"] += f(it[4])
                a_["n"] += 1
                a_["who"].add(info.get("Staff", ""))
                a_["why"].add(it[5])
                a_["dates"].append(when.strftime("%d/%m %H:%M"))

    rows = []
    keys = set(s16) | set(s25) | set(rec)
    T = defaultdict(float)
    for k in sorted(keys, key=str):  # deterministic tie order (rows are re-sorted by size below)
        a16, a25, x = s16.get(k), s25.get(k), rec.get(k, {})
        q16, c16, tc16 = (f(a16["TotalStock"]), f(a16["MeasuredCostPrice"]), f(a16["TotalCost"])) if a16 else (0, 0, 0)
        q25, c25, tc25 = (f(a25["TotalStock"]), f(a25["MeasuredCostPrice"]), f(a25["TotalCost"])) if a25 else (0, 0, 0)
        cref = c25 if a25 else c16
        po_q, po_val, sold_q, cogs = x.get("po_q", 0), x.get("po_val", 0), x.get("sold_q", 0), x.get("cogs", 0)
        zf16, zf25 = max(tc16, 0) if q16 > 0 else 0, max(tc25, 0) if q25 > 0 else 0
        unexpl_q = q25 - q16 - po_q + sold_q
        # ID-keyed rows are reconciled even when absent on one side (new product / dropped row);
        # only rows we could not tie to a catalogue ID go to the "unmatched" bucket.
        matched = not str(k).startswith("NAME:")
        reval = q16 * (c25 - c16) if (a16 and a25) else 0.0
        po_diff = po_q * cref - po_val
        sale_diff = cogs - sold_q * cref
        if matched:
            unexpl_val = tc25 - tc16 - reval - po_q * cref + sold_q * cref
            unm = 0.0
        else:  # renamed / unmatched rows: whole movement goes to its own bucket
            unexpl_val = 0.0
            unm = tc25 - tc16 - po_q * cref + sold_q * cref
        zfe = (zf25 - tc25) - (zf16 - tc16)
        name = (a25 or a16 or {}).get("Name") or (byid[k]["Name"] if k in byid else str(k))
        ad = adj.get(k)
        adj_q = ad["q"] if ad else 0.0
        adj_val_c = adj_q * cref if matched else 0.0
        resid_q = unexpl_q - adj_q
        row = {
            "key": k, "name": name, "category": (a25 or a16 or {}).get("CategoryName", ""),
            "match_16": h16.get(k, "absent"), "match_25": h25.get(k, "absent"),
            "q16": round(q16, 5), "q25": round(q25, 5), "po_q": round(po_q, 5), "sold_q": round(sold_q, 5),
            "expected_q25": round(q16 + po_q - sold_q, 5), "unexplained_q": round(unexpl_q, 5),
            "cost16": c16, "cost25": c25, "cost_change_pct": round((c25 / c16 - 1) * 100, 2) if c16 and a16 and a25 else "",
            "open_zf": round(zf16, 2), "close_zf": round(zf25, 2), "po_val_ex": round(po_val, 2), "cogs_booked": round(cogs, 2),
            "child_sold_cogs": round(x.get("child_sold_cogs", 0), 2),
            "reval": round(reval, 2), "unexplained_val": round(unexpl_val, 2),
            "po_cost_diff": round(po_diff, 2), "sale_cost_diff": round(sale_diff, 2),
            "zero_floor_effect": round(zfe, 2), "unmatched_row_movement": round(unm, 2),
            "epos_adjust_q": round(adj_q, 5), "epos_adjust_val_at_c25": round(adj_val_c, 2),
            "epos_adjust_val_epos": round(ad["val"], 2) if ad else 0.0,
            "residual_q_after_adjust": round(resid_q, 5), "residual_val_after_adjust": round(unexpl_val - adj_val_c, 2),
            "epos_adjust_n": ad["n"] if ad else 0, "epos_adjust_staff": "; ".join(sorted(ad["who"])) if ad else "",
            "epos_adjust_reason": "; ".join(sorted(ad["why"])) if ad else "",
            "epos_adjust_dates": ", ".join(ad["dates"][:8]) if ad else "",
        }
        rows.append(row)
        for fld in ("open_zf", "close_zf", "po_val_ex", "cogs_booked", "reval", "unexplained_val", "po_cost_diff",
                    "sale_cost_diff", "zero_floor_effect", "unmatched_row_movement"):
            T[fld] += row[fld]
        T["epos_adjust_val_at_c25"] += row["epos_adjust_val_at_c25"]
        T["epos_adjust_val_epos"] += row["epos_adjust_val_epos"]
        T["residual_val_after_adjust"] += row["residual_val_after_adjust"]
        T["adjust_pos"] += max(row["epos_adjust_val_at_c25"], 0)
        T["adjust_neg"] += min(row["epos_adjust_val_at_c25"], 0)
        T["unexpl_pos"] += max(unexpl_val, 0)
        T["unexpl_neg"] += min(unexpl_val, 0)
        T["reval_pos"] += max(reval, 0)
        T["reval_neg"] += min(reval, 0)
    T["cogs_no_stock_row"] = sum(no_row_cogs.values())
    T["cogs_total"] = T["cogs_booked"] + T["cogs_no_stock_row"]
    T["po_no_stock_row"] = no_row_po
    T["po_total"] = T["po_val_ex"] + no_row_po
    check = (T["open_zf"] + T["po_val_ex"] - T["cogs_booked"] + T["reval"] + T["unexplained_val"] + T["po_cost_diff"]
             + T["sale_cost_diff"] + T["zero_floor_effect"] + T["unmatched_row_movement"])
    T["bridge_check_close"] = check
    T["implied_receipts"] = T["close_zf"] - T["open_zf"] + T["cogs_total"]
    T["gap_vs_po"] = T["implied_receipts"] - T["po_total"]

    rows.sort(key=lambda r: -abs(r["unexplained_val"] + r["unmatched_row_movement"]))
    out = Path(a.out)
    with (out / "bridge.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    summary = {
        "window": [str(t0), str(t1)], "shift_hours": a.shift_hours, "sales_lines": sales_lines, "pos_in_window": len(po_n),
        "totals": {k: round(v, 2) for k, v in T.items()},
        "cogs_no_stock_row_by_reason": {k: round(v, 2) for k, v in no_row_cogs.items()},
        "rows": len(rows),
        "epos_adjust_docs_in_window": adj_docs, "epos_adjust_docs_outside_window": adj_outside,
        "epos_adjust_unmatched_names_val": {k: round(v, 2) for k, v in adj_unmatched.items()},
        "rows_exact_qty": sum(1 for r in rows if abs(r["unexplained_q"]) < 0.01),
        "rows_unexplained_up": sum(1 for r in rows if r["unexplained_val"] > 1),
        "rows_unexplained_down": sum(1 for r in rows if r["unexplained_val"] < -1),
        "rows_cost_changed": sum(1 for r in rows if r["cost16"] and r["cost25"] and abs(r["cost25"] - r["cost16"]) > 0.005),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return summary



# ---------------------------------------------------------------- scrape-adjustments (EPOS, view only)
ROWS_JS = """(gid) => { const t = document.getElementById(gid); if (!t) return [];
  return Array.from(t.rows).filter(r => r.querySelector('input[type=submit], a[id*=view], input[id*=view]'))
    .map(r => ({cells: Array.from(r.cells).map(c => c.innerText.trim()),
                btn: (r.querySelector('input[id*=iew], a[id*=iew]') || {}).id || null})); }"""
DETAIL_JS = """() => { const g = t => { const e = document.getElementById(t); return e ? Array.from(e.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim())) : null; };
  return {url: location.href, info: g('MainContent_dvTransfers'), items: g('MainContent_gvItems'),
          tables: Array.from(document.querySelectorAll('table[id]')).map(t => t.id)}; }"""
TAKES_URL = "https://www.eposnowhq.com/Pages/BackOffice/StockTakeList.aspx"
MOVES_URL = "https://www.eposnowhq.com/Pages/BackOffice/StockMovementList.aspx"


def scrape_adjustments(out: Path, from_date: str, what: str, company_key: str) -> None:
    from urllib.parse import unquote, urljoin

    from playwright.sync_api import sync_playwright

    cfg = company_config(company_key)
    start = datetime.strptime(from_date, "%Y-%m-%d")
    from_day = f"{start.day} {start.strftime('%B')}"  # calendar link title, e.g. "16 September"
    stop_before = start

    def settle(page, ms=2500):
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(ms)

    def open_list(page, url, cal_id, grid, pageno):
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.locator(f"#{cal_id} a[title='{from_day}']").first.click()  # From-date filter (view only)
        settle(page)
        if pageno > 1:
            page.evaluate(f"__doPostBack('ctl00$MainContent${grid}','Page${pageno}')")
            settle(page)

    def scrape(page, name, url, cal_id, grid):
        out_list, out_det = out / f"epos_{name}_list.json", out / f"epos_{name}_details.jsonl"
        done = set()
        if out_det.exists():
            for line in out_det.open():
                d = json.loads(line)
                done.add((d["_page"], d["_row"]))
        listing = []
        pageno = 1
        open_list(page, url, cal_id, grid, 1)
        while True:
            rows = page.evaluate(ROWS_JS, f"MainContent_{grid}")
            print(name, "page", pageno, len(rows), rows[0]["cells"] if rows else None, flush=True)
            if not rows:
                break
            for i, r in enumerate(rows):
                listing.append({"page": pageno, "row": i, "cells": r["cells"]})
                if (pageno, i) in done or not r["btn"]:
                    continue
                sig = r["cells"]
                for attempt in range(3):
                    try:
                        # the browser submits the list's own "View Details" postback; we read the redirect
                        # target and answer the navigation with 204 so the filtered list page stays put
                        hit = {}

                        def handler(route, request):
                            if request.method != "POST":
                                return route.continue_()
                            resp = route.fetch(max_redirects=0)
                            hit["loc"] = resp.headers.get("location")
                            hit["status"] = resp.status
                            if not hit["loc"]:  # async (UpdatePanel) postback: redirect is in the body
                                m = re.search(r"StockAdjustmentDetails\.aspx\?TransferID=\d+", unquote(resp.text()))
                                hit["loc"] = "/Pages/BackOffice/" + m.group(0) if m else None
                                hit["body_head"] = resp.text()[:300]
                            if hit["loc"]:
                                route.fulfill(status=204, body="")
                            else:
                                route.fulfill(response=resp)

                        page.route(url.split("?")[0] + "*", handler)
                        try:
                            page.click(f"#{r['btn']}")
                            for _ in range(900):
                                if hit:
                                    break
                                page.wait_for_timeout(250)
                        finally:
                            page.unroute(url.split("?")[0] + "*")
                        loc = hit.get("loc")
                        if not loc:
                            raise RuntimeError(f"no redirect ({hit})")
                        det = page.context.new_page()
                        det.goto(urljoin(url, loc), wait_until="domcontentloaded")
                        det.wait_for_timeout(800)
                        d = det.evaluate(DETAIL_JS)
                        k = 2  # item grid pager (view only)
                        while det.locator(f"#MainContent_gvItems a[href*=\"Page${k}'\"]").count():
                            det.evaluate(f"__doPostBack('ctl00$MainContent$gvItems','Page${k}')")
                            det.wait_for_load_state("domcontentloaded")
                            det.wait_for_timeout(1200)
                            d["items"] += (det.evaluate(DETAIL_JS)["items"] or [])[1:]
                            k += 1
                        det.close()
                        d.update({"_page": pageno, "_row": i, "_list_cells": sig})
                        with out_det.open("a") as fh:
                            fh.write(json.dumps(d) + "\n")
                        break
                    except Exception as exc:  # noqa: BLE001
                        print("  retry", pageno, i, str(exc)[:150], flush=True)
                        for extra in page.context.pages[1:]:
                            extra.close()
                        try:
                            page.unroute_all(behavior="ignoreErrors")
                        except Exception:  # noqa: BLE001
                            pass
                        page.wait_for_timeout(3000)
                        open_list(page, url, cal_id, grid, pageno)
            dates = []
            for r in rows:
                for c in r["cells"]:
                    try:
                        dates.append(datetime.strptime(c, "%d/%m/%Y %H:%M:%S"))
                    except ValueError:
                        pass
            if dates and min(dates) < stop_before:
                break
            nxt = page.locator(f"#MainContent_{grid} a[href*=\"Page${pageno + 1}'\"]")
            if nxt.count() == 0:
                break
            pageno += 1
            page.evaluate(f"__doPostBack('ctl00$MainContent${grid}','Page${pageno}')")
            settle(page)
        out_list.write_text(json.dumps(listing, indent=0))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        epos_login(page, cfg)
        if what in ("takes", "both"):
            scrape(page, "stocktakes", TAKES_URL, "MainContent_calFromDate", "gvTransfers")
        if what in ("moves", "both"):
            page.goto(MOVES_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            grids = page.eval_on_selector_all("table[id^=MainContent_]", "t=>t.map(x=>x.id)")
            cals = [g for g in grids if "cal" in g.lower()]
            gv = [g.replace("MainContent_", "") for g in grids if g.startswith("MainContent_gv")]
            print("movement page tables", grids, flush=True)
            if cals:
                scrape(page, "stockmovements", MOVES_URL, cals[0], gv[0] if gv else "gvTransfers")
        browser.close()


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="OFFLINE stock bridge")
    b.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    b.add_argument("--stock-open", type=Path, default=None,
                   help="opening StockReport (default <evidence-root>/As of 16th September 2026/StockReport_2026_09_16_2108.csv)")
    b.add_argument("--stock-close", type=Path, default=None,
                   help="closing StockReport (default <evidence-root>/As of 25th September/Stock Levels/StockReport_2026_09_25_1308.csv)")
    b.add_argument("--bookkeeping", type=Path, default=None,
                   help="BookKeeping CSV covering the window (default <evidence-root>/As of 25th September/BookKeeping_2026_09_25_1245.csv)")
    b.add_argument("--catalogue", type=Path, default=GAPS_DIR / "epos_catalogue_2026-09-26" / "catalogue_products.json")
    b.add_argument("--evidence", type=Path, default=GAPS_DIR / "canonical_2026-09-26" / "mapping_evidence.csv",
                   help="build_canonical mapping_evidence.csv (owner links + multipliers)")
    b.add_argument("--po-received", type=Path, default=GAPS_DIR / "bills_draft_2026-09" / "po_received_2026-09.csv",
                   help="bills_from_epos_pos po_received CSV")
    b.add_argument("--adjustments", type=Path, default=SEP_DIR / "evidence" / "epos_stocktakes_details.jsonl",
                   help="scrape-adjustments epos_stocktakes_details.jsonl (optional)")
    b.add_argument("--t0", default="2026-09-16 21:08")
    b.add_argument("--t1", default="2026-09-25 13:08")
    b.add_argument("--shift-hours", type=float, default=0.0, help="shift the sales/PO window (timezone test)")
    s = sub.add_parser("scrape-adjustments", help="VIEW-ONLY EPOS stock adjustments capture")
    s.add_argument("--from-date", required=True, help="YYYY-MM-DD; list filter + stop when rows are older")
    s.add_argument("--what", choices=["takes", "moves", "both"], default="both")
    s.add_argument("--company", default="company_a")
    for x in (b, s):
        x.add_argument("--out", type=Path, default=None, help="output folder (default outputs/stock_bridge_<timestamp>/)")
    a = ap.parse_args(argv)
    a.out = resolve_out(a.out, TOOL)
    if a.cmd == "scrape-adjustments":
        scrape_adjustments(a.out, a.from_date, a.what, a.company)
    else:
        root = a.evidence_root
        a.stock_open = a.stock_open or root / "As of 16th September 2026" / "StockReport_2026_09_16_2108.csv"
        a.stock_close = a.stock_close or root / "As of 25th September" / "Stock Levels" / "StockReport_2026_09_25_1308.csv"
        a.bookkeeping = a.bookkeeping or root / "As of 25th September" / "BookKeeping_2026_09_25_1245.csv"
        build(a)
    print("->", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

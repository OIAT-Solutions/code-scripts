"""View-only EPOS adjustment capture, retained from the September bridge reader.

Never clicks Add, Receive, Save, Edit or Delete. Callers use fresh capture folders
and validate list/detail completeness separately. Product identity is resolved by
stock_movements, never by barcode. Runtime browser automation is isolated here.
"""
from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path
from code_scripts.scripts.akponora_cutover._common import company_config, epos_login

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
        if page.locator(f"#{cal_id} a[title='{from_day}']").count() == 0:
            # The daily three-day overlap can cross a month boundary.
            page.locator(f"#{cal_id}").get_by_text("<", exact=True).click()
            settle(page)
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
                        if d.get("items") is None:  # no item grid yet: read once more after a longer wait
                            det.wait_for_timeout(3000)
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


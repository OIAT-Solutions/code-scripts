"""VIEW-ONLY pull of the EPOS Now Catalogue product list for company_a.

Logs in with the pipeline's configured EPOS credentials, opens
products.eposnowhq.com/list, sets 250 rows per page and pages through the list.
The product JSON that the page itself loads from /api/v4/catalogue/products is
captured. The only clicks are login, Clear (search), page size and Next page.
Nothing is edited in EPOS.

Outputs (in --out, default outputs/epos_catalogue_pull_<timestamp>/):
  catalogue_api_pages.json  every captured API response (raw)
  catalogue_products.json   deduplicated product list (first-seen order), the input
                            for build_canonical / epos_product_families
  catalogue_products.csv    flat "full product list with IDs" for the team
  catalogue_list_rows.csv   only with --dom-rows: the rendered list rows

Offline rebuild of the JSON/CSV from an earlier capture (no EPOS login):
  python -m code_scripts.scripts.akponora_cutover.epos_catalogue_pull \\
      --from-captures outputs/nora_gaps_2026-09-25/epos_catalogue_2026-09-26_1937/catalogue_api_pages.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import company_config, epos_login, resolve_out

TOOL = "epos_catalogue_pull"
CSV_FIELDS = ["Id", "Name", "CategoryName", "IsStockTracked", "SellOnTill", "UnitOfSale", "VolumeOfSale",
              "CostPriceExTax", "CostPriceIncTax", "SalePriceIncTax", "Barcode", "Sku", "ArticleCode"]
LIST_URL = "https://products.eposnowhq.com/list?archived=false&limit=50&page=1"


def products_from_captures(captured: list[dict]) -> list[dict]:
    """Deduplicate products across captured API pages by Id, keeping first-seen order.

    Captures of a search-filtered list (a search still active when the page opened, before
    Clear) are only used for products no unfiltered page returned.
    """
    seen: dict = {}
    unfiltered = [c for c in captured if "search=" not in (c.get("url") or "")]
    filtered = [c for c in captured if "search=" in (c.get("url") or "")]
    for cap in unfiltered + filtered:
        body = cap.get("body")
        items = body if isinstance(body, list) else ((body or {}).get("Data") or [])
        for item in items:
            if isinstance(item, dict) and "Id" in item:
                seen.setdefault(item["Id"], item)
    return list(seen.values())


def write_products(out: Path, products: list[dict]) -> None:
    (out / "catalogue_products.json").write_text(json.dumps(products), encoding="utf-8")
    with (out / "catalogue_products.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for p in products:
            w.writerow({k: ("" if p.get(k) is None else p.get(k)) for k in CSV_FIELDS})


def pull(out: Path, company_key: str, dom_rows: bool, headless: bool = True) -> list[dict]:
    from playwright.sync_api import sync_playwright

    cfg = company_config(company_key)
    captured: list[dict] = []

    def on_response(resp):
        if "/api/v4/catalogue/products" in resp.url and resp.request.method == "GET":
            try:
                captured.append({"url": resp.url.split("?")[1] if "?" in resp.url else "", "body": resp.json()})
            except Exception as exc:  # noqa: BLE001
                print("capture failed", exc)

    rows_out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        page.on("response", on_response)
        epos_login(page, cfg)
        page.goto(LIST_URL)
        page.wait_for_selector("text=Items per page", timeout=60000)
        clear = page.get_by_role("button", name="Clear")
        if clear.count():
            clear.first.click()
            page.wait_for_timeout(3000)
        page.get_by_role("combobox").last.click()
        page.get_by_role("option", name="250", exact=True).click()
        page.wait_for_timeout(5000)

        page_no = 1
        while True:
            page.wait_for_timeout(2500)
            rows = []
            if dom_rows:
                rows = page.eval_on_selector_all(
                    "tr, [role=row]",
                    "trs => trs.map(tr => { const c=[...tr.querySelectorAll('td, [role=cell], [role=gridcell]')];"
                    " return c.length ? c.map(td => td.innerText.trim()) : null; }).filter(r => r && r[0])",
                )
            pager = None
            for _ in range(60):
                pager = page.evaluate("() => (document.body.innerText.match(/\\d[\\d,]* - [\\d,]+ of [\\d,]+/)||[null])[0]")
                if pager:
                    break
                page.wait_for_timeout(1000)
            if not pager:
                raise RuntimeError("could not read the list pager text")
            (out / "catalogue_api_pages.json").write_text(json.dumps(captured), encoding="utf-8")
            print(f"page {page_no}: ({pager}) api_captures={len(captured)}", flush=True)
            rows_out.extend([page_no] + r for r in rows)
            nxt = page.get_by_role("button", name="Next page")
            _start, end_total = pager.split(" - ")
            end, total = [int(x.replace(",", "")) for x in end_total.split(" of ")]
            if end >= total or nxt.is_disabled():
                break
            nxt.click()
            page_no += 1
            page.wait_for_function("prev => !document.body.innerText.includes(prev)", arg=pager, timeout=60000)
        browser.close()

    (out / "catalogue_api_pages.json").write_text(json.dumps(captured), encoding="utf-8")
    if dom_rows:
        with (out / "catalogue_list_rows.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["page", "name", "category", "sale_ex_tax", "sale_inc_tax", "button_colour", "actions"])
            w.writerows(rows_out)
    return captured


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output folder (default outputs/epos_catalogue_pull_<timestamp>/)")
    ap.add_argument("--company", default="company_a")
    ap.add_argument("--from-captures", type=Path,
                    help="rebuild catalogue_products.json/.csv from an existing catalogue_api_pages.json (no EPOS login)")
    ap.add_argument("--dom-rows", action="store_true", help="also save the rendered list rows (catalogue_list_rows.csv)")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    a = ap.parse_args(argv)
    out = resolve_out(a.out, TOOL)
    if a.from_captures:
        captured = json.loads(a.from_captures.read_text())
    else:
        captured = pull(out, a.company, a.dom_rows, headless=not a.headed)
    products = products_from_captures(captured)
    write_products(out, products)
    totals = {(c.get("body") or {}).get("Metadata", {}).get("TotalRecords") for c in captured if isinstance(c.get("body"), dict)}
    print(f"api pages {len(captured)}; products {len(products)} "
          f"(tracked {sum(1 for p in products if p.get('IsStockTracked'))}); EPOS TotalRecords seen {sorted(t for t in totals if t)} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

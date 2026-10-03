"""VIEW-ONLY scrape of each product's real "Master Products" link in EPOS Now (company_a).

For each EPOS product id, opens the Classic "Advanced Edit" page
(www.eposnowhq.com/Pages/BackOffice/StockAdjustment.aspx?ProductID=<id>, the link behind the
Catalogue list's "Advanced Edit" menu item) with a plain GET and reads the rendered
MainContent_MasterProductList table (master name, "Advanced" link -> master ProductID,
amount text e.g. "10Each of 100Each", %), plus the "Sub Products" text and the stock-tracked
checkbox state. No clicks except login. Never presses Save/Add/Edit/Remove. No cookies/tokens are
read or saved; only the rendered master table/text is written to <out>/products/<id>.json.
Resumable (existing files are skipped); >= --min-seconds per page.

Example:
  python -m code_scripts.scripts.akponora_cutover.epos_master_links \\
      --ids-file outputs/nora_gaps_2026-09-25/untracked_review_2026-09-26/evidence/all_ids.txt \\
      --out outputs/nora_gaps_2026-09-25/untracked_review_2026-09-26/evidence
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import company_config, epos_login, resolve_out

TOOL = "epos_master_links"
PAGE_URL = "https://www.eposnowhq.com/Pages/BackOffice/StockAdjustment.aspx?ProductID={pid}"
JS = r"""() => {
  const t = document.getElementById('MainContent_MasterProductList');
  const rows = t ? [...t.rows].slice(1).map(r => {
      const adv = r.querySelector('input[value=Advanced]');
      const m = adv ? (adv.getAttribute('onclick')||'').match(/ProductID=(\d+)/) : null;
      return {cells: [...r.cells].map(c => c.innerText.trim()), master_id: m ? m[1] : null};
  }) : null;
  const body = document.body.innerText;
  const i = body.lastIndexOf('Sub Products'), j = body.lastIndexOf('Multiple Choice Products\n');
  const cur = (body.match(/Currently Selected Product: ([^\n]*)/)||[null,null])[1];
  const trk = [...document.querySelectorAll('input[type=checkbox]')].filter(c => /stock/i.test(c.id) && /track|enable|chk/i.test(c.id)).map(c => [c.id, c.checked]);
  return {url: location.pathname + location.search, current: cur, master_table_html: t ? t.outerHTML : null,
          master_rows: rows, sub_products_text: (i>=0 && j>i) ? body.slice(i, j).trim() : null, stock_checkboxes: trk};
}"""


def scrape_product(page, cfg, pid: str, extra_js: str | None = None) -> dict | None:
    """GET one product's Advanced Edit page (view only) and read it; None after 3 failed attempts.

    ``extra_js`` is evaluated on the same page and stored under ``"extra"``.
    """
    for _attempt in range(3):
        try:
            page.goto(PAGE_URL.format(pid=pid), wait_until="domcontentloaded", timeout=60000)
            if "login" in page.url.lower() or "identity" in page.url.lower():
                epos_login(page, cfg)
                continue
            page.wait_for_selector("#stock-master-products", timeout=30000)
            d = page.evaluate(JS)
            if extra_js:
                d["extra"] = page.evaluate(extra_js)
            d["product_id"] = pid
            d["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return d
        except Exception as exc:  # noqa: BLE001
            print("retry", pid, exc, flush=True)
            time.sleep(3)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids-file", type=Path, required=True, help="text file, one EPOS ProductID per line")
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/epos_master_links_<timestamp>/)")
    ap.add_argument("--min-seconds", type=float, default=1.2, help="minimum seconds per page (default %(default)s)")
    ap.add_argument("--company", default="company_a")
    a = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    out = resolve_out(a.out, TOOL) / "products"
    out.mkdir(parents=True, exist_ok=True)
    ids = [x.strip() for x in a.ids_file.read_text().splitlines() if x.strip()]
    cfg = company_config(a.company)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
        epos_login(page, cfg)
        for n, pid in enumerate(ids, 1):
            f = out / f"{pid}.json"
            if f.exists():
                continue
            t0 = time.time()
            d = scrape_product(page, cfg, pid)
            if d is None:
                print("FAILED", pid, flush=True)
                continue
            f.write_text(json.dumps(d, indent=1))
            print(n, pid, d["current"], [(r["master_id"], r["cells"][2:4]) for r in (d["master_rows"] or [])], flush=True)
            time.sleep(max(0, a.min_seconds - (time.time() - t0)))
        b.close()
    print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

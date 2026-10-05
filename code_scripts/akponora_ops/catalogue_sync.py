"""Catalogue sync for AKPONORA / NORA (company_a): new EPOS products -> mapping rows + QBO items.

The daily sales upload fails the whole day when a till line has an EPOS Product ID that is not
in the installed mapping (``runtime/mappings/company_a/approved.csv``). This job finds new EPOS
products and proposes (``plan``) or performs (``apply``) exactly what the October contract allows:

* untracked child whose EPOS "Master Products" link points at a live stock-tracked master that is
  mapped (or created in the same run) -> MAPPING_ONLY row on the master's QBO item, multiplier =
  X x master multiplier / Y for an EPOS amount "X of Y" (same master listed twice: amounts summed);
* stock-tracked product -> CREATE_INVENTORY ``AKP-{id}``: QtyOnHand 0 always, InvStartDate
  2026-10-01, asset 77, PurchaseCost ex-tax per unit, UnitPrice inc-tax per unit;
* untracked product with no link to a live tracked master (no link, link to an untracked master,
  link to an archived master) -> CREATE_NONINVENTORY ``AKP-NS-{id}``.

Identity is the EPOS Product ID only (never barcodes or names). A trailing ``*N`` in a name is never
read and a blank child multiplier never defaults to 1: no parsable EPOS amount -> HOLD.

Every new product gets a review level:
  AUTO    may be applied by the automated mode;
  REVIEW  applied only by a manual ``apply`` with a chat-yes approval ref (ZERO_COST on a tracked
          item, UNEXPLAINED_STOCK, AMOUNT_BASE_DIFFERS, or a master that needs review);
  HOLD    never applied (collision with a live QBO Name/Sku, undetermined multiplier, unknown
          category / tax group, scrape failure, tracked product linked to another tracked master,
          child of a master that is itself on hold, product not in the live catalogue).
CHANGED (name / tracked flag / VolumeOfSale vs mapping; cost / price / category vs the last
snapshot) and REMOVED (mapped, gone from the live catalogue) are reported only.
EXCLUDED: EPOS products listed in ``review_exclusions.csv`` (kind ``product``, next to the mapping)
are never planned (no create, no mapping row) and are reported as ``excluded``; a child of an
excluded master is HOLD ``MASTER_EXCLUDED``. Excluding a product does NOT make its till sales post.

New tracked products: the current EPOS stock is read from the same Advanced Edit page. Non-zero stock
is compared with October EPOS PO receipts (``bills_from_epos_pos.capture_pos``). Explained stock is
noted (it reaches QBO through the PO's Bill); unexplained stock is flagged UNEXPLAINED_STOCK. The item
is still created at qty 0: this job NEVER posts an InventoryAdjustment (that needs a chat yes).

Subcommands (run from the repo root with ``.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync``):

  plan       (default) READ-ONLY: EPOS view-only pages + QBO GETs. Writes plan.json, review.csv,
             payloads.jsonl, proposed_mapping.csv, summary.json to --out (default
             outputs/catalogue_sync_<UTC stamp>/). Exit 0 = nothing to review, 3 = HOLD/REVIEW rows.
  apply      WRITES QBO items + installs the mapping. Manual: --plan-dir, --approval-ref and
             --expect-sha (alias --expect-plan-sha; summary.json "plan_sha256"). Selection (manual
             only): --only ID,... and/or --exclude ID,... apply just those decisions; a child needs
             its in-plan master selected too (else refused); every decision carries
             decision_sha256 (summary.json "decision_shas", review.csv "Decision SHA") and a changed
             decision is refused; --expect-decision-shas pid=sha,... pins each selected one. A plan
             can be applied in parts (plan-dir applied_state.json); the receipt
             (apply_receipt.json, "applied") lists exactly what was applied. Automated: --auto, only when
             OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE=1 and OIAT_COMPANY_A_CATALOGUE_APPROVAL_REF is set;
             builds a fresh plan and applies AUTO rows only, capped by
             OIAT_COMPANY_A_CATALOGUE_AUTO_MAX_CREATES (default 25; above -> refuse, alert, exit 4).
  scheduled  for daily_run: ``apply --auto`` when automation is on, else ``plan`` + Slack alert.

Never: delete / inactivate / rename / patch existing QBO items, post InventoryAdjustment, set
QtyOnHand > 0, target 15030 or LEGACY items, or touch Company B.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from code_scripts.akponora_ops import review_exclusions
from code_scripts.akponora_ops.common import (
    AKP_NS_SKU_PREFIX,
    AKP_SKU_PREFIX,
    ASSET_ID,
    CATCH_ALL_ITEM_ID,
    COMPANY,
    INV_START,
    LEGACY_PREFIX,
    MAPPING_COLUMNS,
    dump_json,
    env_flag,
    mapping_file,
    read_csv,
    run_dir,
    send_slack,
    sha256_file,
    state_dir,
    write_csv,
)
from code_scripts.cutover_drafts import NONSTOCK_ACCOUNTS, NONSTOCK_CATEGORY_BAND, noninventory_drafts
from code_scripts.product_conversion import (
    ProductConversionRegistry,
    canonical_product_id,
    canonical_target_type,
    clean,
    october_target_error,
)
from code_scripts.scripts.akponora_cutover import w7_create_items as w7
from code_scripts.scripts.akponora_cutover._common import company_config, epos_login
from code_scripts.scripts.akponora_cutover.bills_from_epos_pos import capture_pos
from code_scripts.scripts.akponora_cutover.build_final_mapping import ns_name
from code_scripts.scripts.akponora_cutover.epos_catalogue_pull import products_from_captures, pull, write_products
from code_scripts.scripts.akponora_cutover.epos_master_links import scrape_product
from code_scripts.scripts.install_conversion_mapping import install

TOOL = "catalogue_sync"
AUTO_ENV = "OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE"
APPROVAL_ENV = "OIAT_COMPANY_A_CATALOGUE_APPROVAL_REF"
CAP_ENV = "OIAT_COMPANY_A_CATALOGUE_AUTO_MAX_CREATES"
DEFAULT_CAP = 25
PIPELINE_STATUS = "CATALOGUE_SYNC"
MARKER = "catalogue_sync EPOS product"
BUSINESS_TZ = ZoneInfo("Africa/Lagos")

AUTO, REVIEW, HOLD = "AUTO", "REVIEW", "HOLD"
CREATE_INV, CREATE_NS, MAPPING_ONLY = "CREATE_INVENTORY", "CREATE_NONINVENTORY", "MAPPING_ONLY"
CREATE_ACTIONS = {CREATE_INV, CREATE_NS}
REVIEW_FLAGS = {"ZERO_COST_TRACKED", "UNEXPLAINED_STOCK", "AMOUNT_BASE_DIFFERS", "MASTER_NEEDS_REVIEW"}
TAX_CODES = {"VAT": "2", "NoTax": "7"}
SNAPSHOT_FIELDS = ("Name", "IsStockTracked", "VolumeOfSale", "CostPriceExTax", "SalePriceIncTax", "CategoryName")
SHA_EXCLUDED_COLUMNS = {"Approved By", "Target QBO Item Id", "Review Status"}
REVIEW_COLUMNS = ["EPOS Product ID", "EPOS Name", "Status", "Action", "Review", "Target Type", "Target SKU",
                  "Target Name", "Target QBO Item Id", "Multiplier", "Master EPOS ID", "EPOS amount",
                  "EPOS stock", "Flags", "Reasons", "Notes", "Decision SHA"]
UNIT = {"each": ("each", Decimal(1)), "g": ("g", Decimal(1)), "kg": ("g", Decimal(1000)),
        "ml": ("ml", Decimal(1)), "cl": ("ml", Decimal(10)), "l": ("ml", Decimal(1000)),
        "ltr": ("ml", Decimal(1000)), "": ("each", Decimal(1))}
AMOUNT_RE = re.compile(r"^\s*([\d.,]+)\s*([A-Za-z ]*?)\s+of\s+([\d.,]+)\s*([A-Za-z ]*?)\s*$")
STOCK_JS = r"""() => [...document.querySelectorAll('[id^=MainContent_StockListView_CurrentStockTextBox_]')].map(inp => {
  const i = inp.id.split('_').pop();
  const loc = document.getElementById('MainContent_StockListView_LocationIDLabel_' + i);
  const vol = document.getElementById('MainContent_StockListView_txtVol_' + i);
  return {location: loc ? loc.innerText.trim() : null, current_stock: inp.value, current_volume: vol ? vol.value : null};
})"""

StopRun = w7.StopRun


class ApplyRefused(StopRun):
    """apply refused before any QBO write (exit 4)."""


# ---------------------------------------------------------------- small helpers
def fmt(value: Decimal) -> str:
    return f"{value.normalize():f}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truthy(value) -> bool:
    return value is True or clean(value).lower() in {"true", "yes", "1"}


def norm(value) -> str:
    return clean(value).casefold()


def parse_amount(text):
    """EPOS amount "X<unit> of Y<unit>" -> (X in Y's unit, Y); None when unparsable or units differ."""
    m = AMOUNT_RE.match(text or "")
    if not m:
        return None
    x, ux = w7.D(m[1]), m[2].strip().casefold()
    y, uy = w7.D(m[3]), m[4].strip().casefold()
    if x is None or y is None or x <= 0 or y <= 0:
        return None
    if ux == uy:
        return x, y
    if ux not in UNIT or uy not in UNIT or UNIT[ux][0] != UNIT[uy][0]:
        return None
    return x * UNIT[ux][1] / UNIT[uy][1], y


def owner_multiplier(product: dict) -> Decimal:
    """A stock-tracked product is its own family unit: VolumeOfSale sub-units per EPOS unit, else 1."""
    vos = w7.D(product.get("VolumeOfSale"))
    return vos if vos is not None and vos > 0 else Decimal(1)


def automated_mode_enabled() -> bool:
    return env_flag(AUTO_ENV) and bool(clean(os.getenv(APPROVAL_ENV)))


def auto_cap() -> int:
    raw = clean(os.getenv(CAP_ENV))
    return int(raw) if raw.isdigit() else DEFAULT_CAP


def business_today() -> str:
    return datetime.now(BUSINESS_TZ).date().isoformat()


# ---------------------------------------------------------------- catalogue
def normalize_product(p: dict) -> dict:
    out = dict(p)
    out["Id"] = canonical_product_id(p.get("Id"))
    out["Name"] = clean(p.get("Name"))
    out["IsStockTracked"] = truthy(p.get("IsStockTracked"))
    for key in ("VolumeOfSale", "CostPriceExTax", "SalePriceIncTax", "CostPriceIncTax"):
        if isinstance(out.get(key), str):
            out[key] = clean(out[key]) or None
    return out


def load_catalogue(path) -> list[dict]:
    """catalogue_products.json (list), catalogue_api_pages.json (captures) or catalogue_products.csv."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        rows = read_csv(path)
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if rows and isinstance(rows[0], dict) and "body" in rows[0] and "Id" not in rows[0]:
            rows = products_from_captures(rows)
    return [normalize_product(p) for p in rows]


def catalogue_index(products: list[dict]) -> dict[str, dict]:
    out = {}
    for p in products:
        if p["Id"] in out:
            raise StopRun(f"duplicate EPOS Id in catalogue: {p['Id']}")
        out[p["Id"]] = p
    return out


def snapshot_of(catalogue: dict[str, dict]) -> dict:
    return {pid: {k: p.get(k) for k in SNAPSHOT_FIELDS} for pid, p in catalogue.items()}


def diff_catalogue(catalogue: dict, mapping_rows: list[dict], snapshot: dict | None, only_ids=None):
    """Return (new ids, changed rows, removed rows). ``only_ids`` limits the diff to those ids."""
    by_pid = {canonical_product_id(r.get("EPOS Product ID")): r for r in mapping_rows}
    if only_ids is not None:
        ids = {canonical_product_id(x) for x in only_ids}
        return sorted((i for i in ids if i not in by_pid), key=lambda x: (len(x), x)), [], []
    new = sorted((pid for pid in catalogue if pid not in by_pid), key=lambda x: (len(x), x))
    changed = []
    for pid, row in by_pid.items():
        p = catalogue.get(pid)
        if p is None:
            continue
        changes = []
        if clean(row.get("EPOS Name")) != p["Name"]:
            changes.append(f"name {clean(row.get('EPOS Name'))!r} -> {p['Name']!r}")
        typ, sku = canonical_target_type(row.get("Target QBO Item Type")), clean(row.get("Target QBO SKU"))
        own_family = typ == "Inventory" and sku == AKP_SKU_PREFIX + pid
        if p["IsStockTracked"] and typ != "Inventory":
            changes.append(f"now stock-tracked in EPOS but mapped {typ or '?'} {sku}")
        if not p["IsStockTracked"] and own_family:
            changes.append(f"no longer stock-tracked in EPOS but owns Inventory family {sku}")
        if own_family and p["IsStockTracked"]:
            mapped = w7.D(row.get("Staff Approved Sale Multiplier"))
            if mapped != owner_multiplier(p):
                changes.append(f"VolumeOfSale now {fmt(owner_multiplier(p))}, mapping multiplier {row.get('Staff Approved Sale Multiplier')}")
        before = (snapshot or {}).get(pid)
        if before:
            for key in SNAPSHOT_FIELDS:
                if key == "Name" or (key == "IsStockTracked" and changes):
                    continue
                old, cur = before.get(key), p.get(key)
                if key in ("CostPriceExTax", "SalePriceIncTax", "VolumeOfSale"):
                    same = w7.D(old, Decimal(0)) == w7.D(cur, Decimal(0))
                else:
                    same = clean(old) == clean(cur)
                if not same:
                    changes.append(f"{key} {old!r} -> {cur!r} (since last snapshot)")
        if changes:
            changed.append({"pid": pid, "name": p["Name"], "target_sku": sku, "target_id": clean(row.get("Target QBO Item Id")),
                            "changes": changes})
    removed = [{"pid": pid, "name": clean(r.get("EPOS Name")), "target_sku": clean(r.get("Target QBO SKU")),
                "target_id": clean(r.get("Target QBO Item Id"))}
               for pid, r in by_pid.items() if pid not in catalogue]
    return new, changed, removed


# ---------------------------------------------------------------- EPOS sources
def receipts_from_po_capture(po_list_path, po_details_path, ids) -> dict[str, list[dict]]:
    """Per EPOS product id: PO lines with QuantityReceived != 0 from a capture_pos evidence folder."""
    ids = set(ids)
    orders = json.loads(Path(po_list_path).read_text())["body"]["orders"]
    details = {}
    for line in Path(po_details_path).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            details[d.get("OrderRef")] = d
    out = defaultdict(list)
    for o in orders:
        d = details.get(o.get("OrderRef"))
        for p in (d or {}).get("Products") or []:
            pid = canonical_product_id(p.get("ProductId"))
            qty = w7.D(p.get("QuantityReceived"), Decimal(0))
            if pid in ids and qty != 0:
                out[pid].append({"po": str(o.get("OrderRef")), "status": o.get("StatusName"),
                                 "received": (o.get("DateReceived") or "")[:19], "qty": str(qty)})
    missing = [str(o.get("OrderRef")) for o in orders if o.get("OrderRef") not in details]
    if missing:
        out["_missing_details"] = missing
    return dict(out)


PULL_ATTEMPTS = 3


class LiveEpos:
    """VIEW-ONLY EPOS: catalogue list, Advanced Edit pages (master links + stock), PO list/details."""

    def __init__(self, out: Path, *, headless: bool = True, min_seconds: float = 1.2):
        self.out = Path(out)
        self.headless = headless
        self.min_seconds = max(1.2, min_seconds)

    def catalogue(self) -> list[dict]:
        ev = self.out / "epos_catalogue"
        ev.mkdir(parents=True, exist_ok=True)
        # The list page occasionally drops one page's API response (5 Oct 2026: 5893 of 6143); pull again.
        for attempt in range(1, PULL_ATTEMPTS + 1):
            captured = pull(ev, COMPANY, False, headless=self.headless)
            products = products_from_captures(captured)
            write_products(ev, products)
            totals = [(c.get("body") or {}).get("Metadata", {}).get("TotalRecords") for c in captured
                      if isinstance(c.get("body"), dict)]
            total = max((t for t in totals if t), default=0)
            if not total or len(products) >= total:
                return [normalize_product(p) for p in products]
            print(f"EPOS catalogue pull incomplete (attempt {attempt}/{PULL_ATTEMPTS}): "
                  f"{len(products)} of {total} products", flush=True)
        raise StopRun(f"EPOS catalogue pull incomplete: {len(products)} of {total} products")

    def scrape(self, ids) -> dict[str, dict | None]:
        ev = self.out / "epos_products"
        ev.mkdir(parents=True, exist_ok=True)
        cfg = company_config(COMPANY)
        out = {}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_context(viewport={"width": 1400, "height": 1000}).new_page()
            epos_login(page, cfg)
            for pid in ids:
                t0 = time.time()
                d = scrape_product(page, cfg, pid, extra_js=STOCK_JS)
                if d is not None:
                    d.pop("master_table_html", None)
                    (ev / f"{pid}.json").write_text(json.dumps(d, indent=1))
                out[pid] = d
                time.sleep(max(0, self.min_seconds - (time.time() - t0)))
            browser.close()
        return out

    def po_receipts(self, ids) -> dict[str, list[dict]]:
        ev = self.out / "epos_pos"
        ev.mkdir(parents=True, exist_ok=True)
        capture_pos(ev, INV_START, business_today(), 100000, COMPANY)
        return receipts_from_po_capture(ev / "po_list_raw.json", ev / "po_details.jsonl", ids)


class OfflineEpos:
    """Re-run from saved evidence: ``<dir>/<id>.json`` or ``<dir>/products/<id>.json`` and a PO capture."""

    def __init__(self, products_dir=None, po_list=None, po_details=None):
        self.products_dir = Path(products_dir) if products_dir else None
        self.po_list, self.po_details = po_list, po_details

    def scrape(self, ids) -> dict[str, dict | None]:
        out = {}
        for pid in ids:
            out[pid] = None
            for f in ((self.products_dir / f"{pid}.json", self.products_dir / "products" / f"{pid}.json")
                      if self.products_dir else ()):
                if f.exists():
                    out[pid] = json.loads(f.read_text())
                    break
        return out

    def po_receipts(self, ids) -> dict[str, list[dict]]:
        if not (self.po_list and self.po_details):
            return {"_missing_details": ["no PO capture given (--po-list/--po-details)"]}
        return receipts_from_po_capture(self.po_list, self.po_details, ids)


# ---------------------------------------------------------------- classification
def new_decision(pid: str, product: dict | None) -> dict:
    return {"pid": pid, "name": (product or {}).get("Name", ""), "tracked": bool((product or {}).get("IsStockTracked")),
            "action": "", "review": AUTO, "reasons": [], "flags": [], "notes": [],
            "target": {"type": "", "sku": "", "name": "", "qbo_id": ""}, "multiplier": "", "purchase_multiplier": "",
            "family_key": "", "canonical_unit": "", "master_pid": "", "master_amount": "", "stock": None,
            "payload": None, "payload_sha256": "", "adopt_qbo_id": ""}


def hold(d: dict, reason: str) -> None:
    d["review"] = HOLD
    if reason not in d["reasons"]:
        d["reasons"].append(reason)


def link_rows(scrape: dict | None):
    """(rows, problem): the Master Products rows of a scrape, or why they are unusable."""
    if scrape is None:
        return None, "SCRAPE_FAILED (Advanced Edit page not read)"
    rows = scrape.get("master_rows")
    if rows is None:
        return None, "SCRAPE_NO_MASTER_TABLE"
    return rows, ""


def amount_text(row: dict) -> str:
    cells = row.get("cells") or []
    return clean(cells[3]) if len(cells) > 3 else ""


def tax_code(group) -> str:
    return TAX_CODES.get(clean(group), "")


def category_accounts(product: dict):
    band = NONSTOCK_CATEGORY_BAND.get(clean(product.get("CategoryName")).upper())
    return NONSTOCK_ACCOUNTS[band] if band else None


def qbo_name(product: dict) -> str:
    return ns_name(product.get("Name") or "")


def description(pid: str, product: dict) -> str:
    return f"{MARKER} {pid}: {clean(product.get('Name'))}"[:4000]


def stock_from_scrape(scrape: dict | None, m: Decimal):
    """EPOS stock of a tracked product in its own EPOS units (full + loose/m); None when unreadable."""
    rows = (scrape or {}).get("extra")
    if not isinstance(rows, list) or not rows:
        return None
    full, loose = Decimal(0), Decimal(0)
    for r in rows:
        f, v = w7.D(r.get("current_stock")), w7.D(r.get("current_volume"), Decimal(0))
        if f is None or v is None:
            return None
        full, loose = full + f, loose + v
    flags = []
    if len(rows) > 1:
        flags.append(f"MULTIPLE_LOCATIONS({len(rows)})")
    if m == 1 and loose != 0:
        flags.append(f"LOOSE_STOCK_WITHOUT_VOS({fmt(loose)})")
    units = full + (loose / m if m != 1 else Decimal(0))
    return {"full": fmt(full), "loose": fmt(loose), "units": fmt(units), "flags": flags,
            "locations": [r.get("location") for r in rows]}


def classify_tracked(pid, p, scrape, catalogue) -> dict:
    d = new_decision(pid, p)
    d["action"] = CREATE_INV
    m = owner_multiplier(p)
    name = qbo_name(p)
    sku = AKP_SKU_PREFIX + pid
    d.update(multiplier=fmt(m), purchase_multiplier=fmt(m), family_key=sku,
             canonical_unit="Each" if m == 1 else f"Each (1/{fmt(m)} of {p['Name']})")
    d["target"].update(type="Inventory", sku=sku, name=name)
    rows, problem = link_rows(scrape)
    if problem:
        hold(d, problem)
    else:
        tracked_links = [r for r in rows if (catalogue.get(canonical_product_id(r.get("master_id"))) or {}).get("IsStockTracked")]
        if tracked_links:
            hold(d, "TRACKED_PRODUCT_LINKED_TO_TRACKED_MASTER " + ", ".join(
                f"{r.get('master_id')}:{amount_text(r)}" for r in tracked_links))
        elif rows:
            d["notes"].append("ignored link(s) to untracked/archived master(s): " + ", ".join(
                f"{r.get('master_id')}:{amount_text(r)}" for r in rows))
    accounts = category_accounts(p)
    sale_code, cost_code = tax_code(p.get("SalePriceTaxGroupName")), tax_code(p.get("CostPriceTaxGroupName"))
    if accounts is None:
        hold(d, f"UNKNOWN_CATEGORY({p.get('CategoryName')})")
    if not sale_code or not cost_code:
        hold(d, f"UNKNOWN_TAX_GROUP(sale {p.get('SalePriceTaxGroupName')}, cost {p.get('CostPriceTaxGroupName')})")
    if not name or name != " ".join(name.split()) or len(name) > 100:
        hold(d, f"INVALID_QBO_NAME({name!r})")
    cost_ex = w7.D(p.get("CostPriceExTax"), Decimal(0))
    unit_cost = w7.q(cost_ex / m, "0.00001") if cost_ex > 0 else Decimal(0)
    unit_price = w7.q(w7.D(p.get("SalePriceIncTax"), Decimal(0)) / m, "0.01")
    if unit_cost <= 0:
        d["flags"].append("ZERO_COST_TRACKED")
    if unit_price <= 0:
        d["flags"].append("ZERO_PRICE")
    if d["review"] != HOLD:
        d["payload"] = {
            "Name": name, "Sku": sku, "Type": "Inventory", "TrackQtyOnHand": True, "QtyOnHand": 0,
            "InvStartDate": INV_START, "AssetAccountRef": {"value": ASSET_ID},
            "IncomeAccountRef": {"value": accounts["income_id"]}, "ExpenseAccountRef": {"value": accounts["expense_id"]},
            "PurchaseCost": float(unit_cost), "PurchaseTaxIncluded": False, "UnitPrice": float(unit_price),
            "SalesTaxIncluded": True, "Taxable": clean(p.get("SalePriceTaxGroupName")) == "VAT",
            "SalesTaxCodeRef": {"value": sale_code}, "PurchaseTaxCodeRef": {"value": cost_code},
            "Description": description(pid, p),
        }
    stock = stock_from_scrape(scrape, m)
    d["stock"] = stock
    if stock is None:
        hold(d, "EPOS_STOCK_UNREADABLE")
    else:
        d["flags"].extend(stock["flags"])
        if Decimal(stock["units"]) < 0:
            d["flags"].append(f"NEGATIVE_EPOS_STOCK({stock['units']})")
    return d


def classify_untracked(pid, p, scrape, catalogue, by_pid, tracked_decisions, excluded_ids=None) -> dict:
    d = new_decision(pid, p)
    rows, problem = link_rows(scrape)
    if problem:
        d["action"] = MAPPING_ONLY
        hold(d, problem)
        return d
    live_tracked = [r for r in rows if (catalogue.get(canonical_product_id(r.get("master_id"))) or {}).get("IsStockTracked")]
    ignored = [r for r in rows if r not in live_tracked]
    if ignored:
        d["notes"].append("link(s) to untracked/archived master(s) deduct nothing: " + ", ".join(
            f"{r.get('master_id')}:{amount_text(r)}" for r in ignored))
    if not live_tracked:
        return noninventory_decision(d, pid, p)
    d["action"] = MAPPING_ONLY
    masters = {canonical_product_id(r.get("master_id")) for r in live_tracked}
    d["master_amount"] = " + ".join(f"{r.get('master_id')}:{amount_text(r)}" for r in live_tracked)
    if len(masters) > 1:
        hold(d, "MULTIPLE_TRACKED_MASTERS")
        return d
    mid = masters.pop()
    d["master_pid"] = mid
    parts = [parse_amount(amount_text(r)) for r in live_tracked]
    if any(x is None for x in parts) or len({y for _x, y in parts}) > 1:
        hold(d, "MULTIPLIER_UNDETERMINED (EPOS amount blank/unparsable)")
        return d
    x, y = sum((a for a, _b in parts), Decimal(0)), parts[0][1]
    if len(live_tracked) > 1:
        d["notes"].append(f"master listed {len(live_tracked)} times; amounts summed")
    master = catalogue[mid]
    master_row = by_pid.get(mid)
    if master_row is not None:
        typ, sku = canonical_target_type(master_row.get("Target QBO Item Type")), clean(master_row.get("Target QBO SKU"))
        tid, tname = clean(master_row.get("Target QBO Item Id")), clean(master_row.get("Target QBO Name"))
        om = w7.D(master_row.get("Staff Approved Sale Multiplier"))
        problem = october_target_error(typ, sku, tid, tname)
        if (norm(master_row.get("Review Status")) != "approved" or typ != "Inventory" or problem
                or not tid.isdigit() or om is None or om <= 0 or tname.startswith(LEGACY_PREFIX)):
            hold(d, f"MASTER_MAPPING_UNUSABLE({mid} -> {typ} {sku} Id {tid or '-'} {problem})")
            return d
        d["target"].update(type="Inventory", sku=sku, name=tname, qbo_id=tid)
        d.update(family_key=clean(master_row.get("Canonical Family Key")) or sku,
                 canonical_unit=clean(master_row.get("Canonical Unit")))
    else:
        md = tracked_decisions.get(mid)
        if mid in (excluded_ids or ()):
            hold(d, f"MASTER_EXCLUDED({mid}) - master is in review_exclusions; map or exclude this child too")
            return d
        if md is None or md["review"] == HOLD:
            hold(d, f"MASTER_NOT_CREATABLE({mid})")
            return d
        om = Decimal(md["multiplier"])
        d["target"].update(type="Inventory", sku=md["target"]["sku"], name=md["target"]["name"])
        d.update(family_key=md["family_key"], canonical_unit=md["canonical_unit"])
        d["notes"].append(f"master {mid} is created in this run")
    if y != owner_multiplier(master):
        d["flags"].append("AMOUNT_BASE_DIFFERS")
        d["notes"].append(f"amount base {fmt(y)} != master VolumeOfSale {fmt(owner_multiplier(master))}")
    mult = x * om / y
    if mult <= 0:
        hold(d, "MULTIPLIER_UNDETERMINED (non-positive)")
        return d
    d.update(multiplier=fmt(mult), purchase_multiplier=fmt(mult))
    return d


def noninventory_decision(d: dict, pid: str, p: dict) -> dict:
    d["action"] = CREATE_NS
    name = qbo_name(p)
    sku = AKP_NS_SKU_PREFIX + pid
    d.update(multiplier="1", purchase_multiplier="1", family_key=sku, canonical_unit="Each")
    d["target"].update(type="NonInventory", sku=sku, name=name)
    sale_code, cost_code = tax_code(p.get("SalePriceTaxGroupName")), tax_code(p.get("CostPriceTaxGroupName"))
    if category_accounts(p) is None:
        hold(d, f"UNKNOWN_CATEGORY({p.get('CategoryName')})")
    if not sale_code or not cost_code:
        hold(d, f"UNKNOWN_TAX_GROUP(sale {p.get('SalePriceTaxGroupName')}, cost {p.get('CostPriceTaxGroupName')})")
    if not name or len(name) > 100:
        hold(d, f"INVALID_QBO_NAME({name!r})")
    if d["review"] == HOLD:
        return d
    try:
        draft = noninventory_drafts([{"epos_product_id": pid, "name": name, "category": p.get("CategoryName"),
                                      "approved_by": TOOL, "approval_ref": TOOL, "stock_tracked": False,
                                      "tax_code_id": sale_code}])["drafts"][0]
    except ValueError as exc:
        hold(d, f"NONINVENTORY_DRAFT_REFUSED({exc})")
        return d
    cost = w7.q(w7.D(p.get("CostPriceExTax"), Decimal(0)), "0.00001")
    price = w7.q(w7.D(p.get("SalePriceIncTax"), Decimal(0)), "0.01")
    if cost <= 0:
        d["flags"].append("ZERO_COST")
    payload = draft["payload"]
    payload.update(SalesTaxCodeRef={"value": sale_code}, PurchaseTaxCodeRef={"value": cost_code},
                   Taxable=clean(p.get("SalePriceTaxGroupName")) == "VAT", SalesTaxIncluded=True,
                   PurchaseTaxIncluded=False, UnitPrice=float(price), PurchaseCost=float(max(cost, Decimal(0))),
                   Description=description(pid, p))
    d["payload"] = payload
    return d


def apply_stock_check(decisions: list[dict], epos) -> None:
    """Non-zero EPOS stock on a new tracked product must be explained by October PO receipts."""
    pending = [d for d in decisions if d["action"] == CREATE_INV and d["stock"] and Decimal(d["stock"]["units"]) > 0]
    if not pending:
        return
    receipts = epos.po_receipts([d["pid"] for d in pending])
    missing = receipts.get("_missing_details") or []
    for d in pending:
        units = Decimal(d["stock"]["units"])
        lines = receipts.get(d["pid"], [])
        received = sum((Decimal(x["qty"]) for x in lines), Decimal(0))
        refs = ", ".join(f"PO#{x['po']} ({x['qty']})" for x in lines)
        d["stock"]["po_received"] = fmt(received)
        d["stock"]["po_lines"] = lines
        if lines and units <= received + Decimal("0.001"):
            d["flags"].append("STOCK_VIA_PO")
            d["notes"].append(f"EPOS stock {fmt(units)} arrives via bill {refs}; created at qty 0")
        else:
            d["flags"].append("UNEXPLAINED_STOCK")
            d["notes"].append(f"EPOS stock {fmt(units)} vs October PO receipts {fmt(received)}"
                              + (f" ({refs})" if refs else "") + "; created at qty 0. A human decides whether an "
                              "InventoryAdjustment is needed (chat yes); this job never posts one")
            if missing:
                d["notes"].append(f"{len(missing)} PO(s) without captured details")


def check_batch_names(decisions: list[dict], mapping_rows: list[dict]) -> None:
    mapped_names = defaultdict(set)
    for r in mapping_rows:
        mapped_names[norm(r.get("Target QBO Name"))].add(clean(r.get("Target QBO SKU")))
    creates = [d for d in decisions if d["action"] in CREATE_ACTIONS]
    counts = Counter(norm(d["target"]["name"]) for d in creates)
    for d in creates:
        key = norm(d["target"]["name"])
        if counts[key] > 1:
            hold(d, "DUPLICATE_NAME_IN_BATCH")
        if mapped_names.get(key, set()) - {d["target"]["sku"]}:
            hold(d, f"NAME_ALREADY_MAPPED_TO {sorted(mapped_names[key])}")


def propagate(decisions: list[dict]) -> None:
    """Children follow their in-run master: HOLD -> HOLD, REVIEW -> REVIEW; then set review levels."""
    by_pid = {d["pid"]: d for d in decisions}
    for d in decisions:
        if d["review"] != HOLD and any(f in REVIEW_FLAGS for f in d["flags"]):
            d["review"] = REVIEW
    for d in decisions:
        md = by_pid.get(d["master_pid"]) if d["action"] == MAPPING_ONLY else None
        if md is None or d["review"] == HOLD:
            continue
        if md["review"] == HOLD:
            hold(d, f"MASTER_ON_HOLD({md['pid']})")
        elif md["review"] == REVIEW:
            d["flags"].append("MASTER_NEEDS_REVIEW")
            d["review"] = REVIEW


# ---------------------------------------------------------------- QBO (GET only in plan)
def load_ledger(state: Path) -> dict[str, str]:
    path = state / "ledger.csv"
    return {r["Sku"]: r["QBO Id"] for r in (read_csv(path) if path.exists() else []) if r["status"] in w7.DONE}


def qbo_checks(client, decisions: list[dict], ledger: dict[str, str]) -> dict:
    """Read-only: name/SKU collisions, adoption of this job's own earlier creates, master items."""
    creates = [d for d in decisions if d["action"] in CREATE_ACTIONS and d["review"] != HOLD]
    info = {"items_read": 0, "collisions": [], "run_problems": [], "adopted": []}
    for d in creates:
        d["adopt_qbo_id"] = ""
        d["target"]["qbo_id"] = ""
    if creates:
        entries = [{"name": d["target"]["name"], "sku": d["target"]["sku"], "kind": d["target"]["type"],
                    "payload": d["payload"]} for d in creates]
        pf = w7.preflight(client, entries, own_ids=frozenset(ledger.values()))
        info["items_read"] = pf["items_read"]
        info["run_problems"] = pf["account_problems"] + pf["tax_problems"]
        info["collisions"] = pf["collisions"]
        coll = defaultdict(list)
        for c in pf["collisions"]:
            coll[c["New Sku"]].append(c)
        resumable = {r["Sku"]: r["QBO Id"] for r in pf["resumable_existing"]}
        for d in creates:
            sku = d["target"]["sku"]
            if coll.get(sku):
                hold(d, "QBO_COLLISION " + "; ".join(
                    f"{c['Collision']} Id {c['Live Id']} {c['Live Name']!r} Sku {c['Live Sku']!r} active {c['Live Active']}"
                    for c in coll[sku]))
                continue
            candidate, source = (ledger[sku], "ledger") if sku in ledger else (resumable.get(sku), "qbo")
            if not candidate:
                continue
            try:
                live = client.get_json(f"/item/{candidate}")["Item"]
            except StopRun as exc:
                hold(d, f"QBO_EXISTING_ITEM_UNREADABLE({candidate}: {exc})")
                continue
            problems = w7.verify_matches(live, d["payload"], item_id=candidate)
            ours = source == "ledger" or clean(live.get("Description")).startswith(MARKER)
            if ours and not problems and live.get("Active") is True:
                d["adopt_qbo_id"] = str(candidate)
                d["target"]["qbo_id"] = str(candidate)
                d["notes"].append(f"adopting QBO Id {candidate} created by an earlier catalogue_sync run ({source})")
                info["adopted"].append({"sku": sku, "qbo_id": str(candidate)})
            else:
                hold(d, f"QBO_EXISTING_ITEM Id {candidate} ({'ours' if ours else 'not created by this job'}"
                        + (f": {'; '.join(problems)}" if problems else "") + ")")
    cache = {}
    for d in decisions:
        tid = d["target"]["qbo_id"]
        if d["action"] != MAPPING_ONLY or d["review"] == HOLD or not tid:
            continue
        if tid not in cache:
            try:
                cache[tid] = client.get_json(f"/item/{tid}")["Item"]
            except StopRun as exc:
                cache[tid] = {"_error": str(exc)}
        it = cache[tid]
        problems = []
        if it.get("_error"):
            problems.append(it["_error"])
        else:
            if it.get("Active") is not True:
                problems.append("inactive")
            if it.get("Type") != "Inventory":
                problems.append(f"Type {it.get('Type')}")
            if clean(it.get("Sku")) != d["target"]["sku"]:
                problems.append(f"Sku {it.get('Sku')!r}")
            if norm(it.get("Name")) != norm(d["target"]["name"]):
                problems.append(f"Name {it.get('Name')!r}")
            if str((it.get("AssetAccountRef") or {}).get("value", ASSET_ID)) != ASSET_ID:
                problems.append("asset account not 77")
        if problems:
            hold(d, f"MASTER_QBO_MISMATCH(Id {tid}: {', '.join(problems)})")
    return info


# ---------------------------------------------------------------- plan outputs
def mapping_row(d: dict, approved_by: str, review_status: str) -> dict:
    return {"Row ID": f"EPOS-{d['pid']}", "EPOS Product ID": d["pid"], "EPOS Existing SKU": "", "EPOS Name": d["name"],
            "Pipeline Status": PIPELINE_STATUS, "Review Status": review_status,
            "Target QBO Item Type": d["target"]["type"], "Target QBO Name": d["target"]["name"],
            "Target QBO SKU": d["target"]["sku"], "Target QBO Item Id": d["target"]["qbo_id"],
            "Staff Approved Sale Multiplier": d["multiplier"], "Effective Date": INV_START,
            "Approved By": approved_by, "Canonical Family Key": d["family_key"], "Canonical Unit": d["canonical_unit"],
            "Staff Approved Purchase Multiplier": d["purchase_multiplier"]}


def plan_digest(decisions: list[dict]) -> str:
    """sha256 over every non-HOLD payload + mapping addition (excluding approver / Id / review status)."""
    live = [d for d in decisions if d["review"] != HOLD]
    body = {"payloads": [{"sku": d["target"]["sku"], "kind": d["target"]["type"], "payload": d["payload"]}
                         for d in live if d["action"] in CREATE_ACTIONS],
            "mapping": [{k: v for k, v in mapping_row(d, "", "").items() if k not in SHA_EXCLUDED_COLUMNS}
                        for d in live]}
    return w7.sha256_text(w7.canonical_json(body))


def decision_digest(d: dict) -> str:
    """sha256 of ONE decision: what apply would do for this EPOS product (action, review level, flags,
    QBO payload, item to adopt, master, mapping row without approver / Id / review status). A portal
    approval of one decision carries this sha; apply refuses when it no longer matches."""
    body = {"pid": d["pid"], "action": d["action"], "review": d["review"], "flags": sorted(d["flags"]),
            "master_pid": d["master_pid"], "adopt_qbo_id": d["adopt_qbo_id"], "payload": d["payload"],
            "mapping": {k: v for k, v in mapping_row(d, "", "").items() if k not in SHA_EXCLUDED_COLUMNS}}
    return w7.sha256_text(w7.canonical_json(body))


def review_rows(plan: dict) -> list[dict]:
    rows = []
    for d in plan["decisions"]:
        st = d.get("stock") or {}
        rows.append({"EPOS Product ID": d["pid"], "EPOS Name": d["name"], "Status": "NEW", "Action": d["action"],
                     "Review": d["review"], "Target Type": d["target"]["type"], "Target SKU": d["target"]["sku"],
                     "Target Name": d["target"]["name"], "Target QBO Item Id": d["target"]["qbo_id"],
                     "Multiplier": d["multiplier"], "Master EPOS ID": d["master_pid"], "EPOS amount": d["master_amount"],
                     "EPOS stock": st.get("units", ""), "Flags": "; ".join(d["flags"]),
                     "Reasons": "; ".join(d["reasons"]), "Notes": "; ".join(d["notes"]),
                     "Decision SHA": d.get("decision_sha256", "")})
    for x in plan.get("excluded", []):
        rows.append({"EPOS Product ID": x["pid"], "EPOS Name": x["name"], "Status": "EXCLUDED", "Action": "NONE",
                     "Review": "EXCLUDED", "Notes": f"review_exclusions: {x['reason']} (by {x['added_by']}"
                                                   + (f", until {x['expires_at']}" if x["expires_at"] else "") + ")"})
    for c in plan["changed"]:
        rows.append({"EPOS Product ID": c["pid"], "EPOS Name": c["name"], "Status": "CHANGED", "Action": "REPORT_ONLY",
                     "Review": "INFO", "Target SKU": c["target_sku"], "Target QBO Item Id": c["target_id"],
                     "Notes": "; ".join(c["changes"])})
    for r in plan["removed"]:
        rows.append({"EPOS Product ID": r["pid"], "EPOS Name": r["name"], "Status": "REMOVED", "Action": "REPORT_ONLY",
                     "Review": "INFO", "Target SKU": r["target_sku"], "Target QBO Item Id": r["target_id"],
                     "Notes": "mapped but not in the live EPOS catalogue (archived/deleted); QBO untouched"})
    return rows


def write_plan_outputs(out: Path, plan: dict, mapping_rows: list[dict]) -> None:
    decisions = plan["decisions"]
    creates = [d for d in decisions if d["action"] in CREATE_ACTIONS and d["review"] != HOLD]
    lines = [w7.canonical_json({"pid": d["pid"], "kind": d["target"]["type"], "sku": d["target"]["sku"],
                                "name": d["target"]["name"], "review": d["review"], "adopt_qbo_id": d["adopt_qbo_id"],
                                "flags": d["flags"], "payload": d["payload"], "payload_sha256": d["payload_sha256"]})
             for d in creates]
    text = "\n".join(lines) + ("\n" if lines else "")
    (out / "payloads.jsonl").write_text(text, encoding="utf-8")
    plan["payloads_sha256"] = w7.sha256_text(text)
    pending_by = f"PENDING approval (catalogue_sync plan {plan['created_at']})"
    additions = [mapping_row(d, pending_by, "Approved" if d["review"] == AUTO else "Pending Approval")
                 for d in decisions if d["review"] != HOLD]
    write_csv(out / "proposed_mapping.csv", mapping_rows + additions, MAPPING_COLUMNS)
    write_csv(out / "review.csv", review_rows(plan), REVIEW_COLUMNS)
    if plan["only_ids"] is None:
        dump_json(out / "catalogue_snapshot.json", plan.pop("_snapshot"))
    plan.pop("_snapshot", None)
    dump_json(out / "plan.json", plan)
    dump_json(out / "summary.json", {k: v for k, v in plan.items() if k not in ("decisions", "changed", "removed")})


def counts_of(plan: dict) -> dict:
    ds = plan["decisions"]
    return {"new": len(ds), "changed": len(plan["changed"]), "removed": len(plan["removed"]),
            "create_inventory": sum(d["action"] == CREATE_INV and d["review"] != HOLD for d in ds),
            "create_noninventory": sum(d["action"] == CREATE_NS and d["review"] != HOLD for d in ds),
            "mapping_only": sum(d["action"] == MAPPING_ONLY and d["review"] != HOLD for d in ds),
            "adopt_existing": sum(bool(d["adopt_qbo_id"]) for d in ds),
            "auto": sum(d["review"] == AUTO for d in ds), "review": sum(d["review"] == REVIEW for d in ds),
            "hold": sum(d["review"] == HOLD for d in ds),
            "unexplained_stock": sum("UNEXPLAINED_STOCK" in d["flags"] for d in ds),
            "excluded": len(plan.get("excluded", []))}


def needs_attention(plan: dict) -> bool:
    c = plan["counts"]
    return bool(c["hold"] or c["review"] or plan["run_problems"])


def excluded_entry(pid: str, product: dict | None, exclusions) -> dict:
    row = exclusions.get("product", pid) or {}
    return {"pid": pid, "name": (product or {}).get("Name", ""), "reason": row.get("reason", ""),
            "added_by": row.get("added_by", ""), "added_at": row.get("added_at", ""),
            "expires_at": row.get("expires_at", "")}


def build_plan(*, out: Path, catalogue: list[dict], mapping_path: Path, state: Path, epos, client=None,
               only_ids=None, exclusions=None) -> dict:
    """Read-only plan. ``client`` is a GET-only ``QBOClient`` (None = skip the QBO checks).

    ``exclusions`` (``review_exclusions.Exclusions``; default: the file next to the mapping): excluded
    EPOS products are never planned; they are listed in ``plan["excluded"]``."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if exclusions is None:
        exclusions = review_exclusions.load(review_exclusions.path_near(mapping_path))
    cat = catalogue_index(catalogue)
    mapping_rows = read_csv(mapping_path)
    if not mapping_rows or list(mapping_rows[0].keys()) != MAPPING_COLUMNS:
        raise StopRun(f"installed mapping {mapping_path} is empty or its columns differ from MAPPING_COLUMNS")
    by_pid = {canonical_product_id(r.get("EPOS Product ID")): r for r in mapping_rows}
    snap_path = state / "catalogue_snapshot.json"
    snapshot = json.loads(snap_path.read_text()) if snap_path.exists() else None
    new_ids, changed, removed = diff_catalogue(cat, mapping_rows, snapshot, only_ids)
    if snapshot is None and only_ids is None:
        dump_json(snap_path, snapshot_of(cat))
    excluded_ids = exclusions.keys("product")
    excluded = [excluded_entry(pid, cat.get(pid), exclusions) for pid in new_ids if pid in excluded_ids]
    new_ids = [pid for pid in new_ids if pid not in excluded_ids]
    in_cat = [pid for pid in new_ids if pid in cat]
    scrapes = epos.scrape(in_cat) if in_cat else {}
    masters = set()
    for pid in in_cat:
        if not cat[pid]["IsStockTracked"]:
            for r in link_rows(scrapes.get(pid))[0] or []:
                mid = canonical_product_id(r.get("master_id"))
                if (cat.get(mid) or {}).get("IsStockTracked") and mid not in by_pid and mid not in in_cat:
                    if mid in excluded_ids:
                        if mid not in {x["pid"] for x in excluded}:
                            excluded.append(excluded_entry(mid, cat.get(mid), exclusions))
                        continue
                    masters.add(mid)
    if masters:
        extra = sorted(masters, key=lambda x: (len(x), x))
        scrapes.update(epos.scrape(extra))
        in_cat += extra
        new_ids += extra
    decisions, tracked = [], {}
    for pid in new_ids:
        if pid not in cat:
            d = new_decision(pid, None)
            d["action"] = "NONE"
            hold(d, "NOT_IN_LIVE_EPOS_CATALOGUE (archived/deleted product sold on the till?)")
            decisions.append(d)
    for pid in in_cat:
        if cat[pid]["IsStockTracked"]:
            tracked[pid] = classify_tracked(pid, cat[pid], scrapes.get(pid), cat)
            decisions.append(tracked[pid])
    for pid in in_cat:
        if not cat[pid]["IsStockTracked"]:
            decisions.append(classify_untracked(pid, cat[pid], scrapes.get(pid), cat, by_pid, tracked, excluded_ids))
    apply_stock_check(decisions, epos)
    check_batch_names(decisions, mapping_rows)
    propagate(decisions)
    qbo = {"checked": False}
    if client is not None:
        qbo = {"checked": True, **qbo_checks(client, decisions, load_ledger(state))}
        propagate(decisions)
    order = {CREATE_INV: 0, CREATE_NS: 1, MAPPING_ONLY: 2}
    decisions.sort(key=lambda d: (order.get(d["action"], 3), len(d["pid"]), d["pid"]))
    for d in decisions:
        if d["payload"] is not None:
            d["payload_sha256"] = w7.sha256_text(w7.canonical_json(d["payload"]))
        d["decision_sha256"] = decision_digest(d)
    plan = {"tool": TOOL, "company": COMPANY, "created_at": now_iso(), "only_ids": sorted(only_ids) if only_ids else None,
            "inputs": {"mapping": {"path": str(mapping_path), "sha256": sha256_file(mapping_path), "rows": len(mapping_rows)},
                       "catalogue_products": len(cat), "snapshot": str(snap_path) if snapshot is not None else None,
                       "exclusions": {"path": str(exclusions.path or ""), "sha256": exclusions.sha256(),
                                      "active": len(exclusions.rows)}},
            "qbo": {k: v for k, v in qbo.items() if k != "collisions"}, "qbo_collisions": qbo.get("collisions", []),
            "run_problems": qbo.get("run_problems", []), "decisions": decisions, "changed": changed, "removed": removed,
            "excluded": excluded, "_snapshot": snapshot_of(cat)}
    plan["counts"] = counts_of(plan)
    plan["plan_sha256"] = plan_digest(decisions)
    plan["decision_shas"] = {d["pid"]: d["decision_sha256"] for d in decisions if d["review"] != HOLD}
    write_plan_outputs(out, plan, mapping_rows)
    return plan


def plan_text(plan: dict, out: Path) -> str:
    c = plan["counts"]
    lines = [f"Akponora catalogue sync plan ({out}): new {c['new']}, changed {c['changed']}, removed {c['removed']}",
             f"  create Inventory {c['create_inventory']}, NonInventory {c['create_noninventory']}, "
             f"mapping-only {c['mapping_only']} (adopt {c['adopt_existing']}); AUTO {c['auto']}, REVIEW {c['review']}, "
             f"HOLD {c['hold']}, unexplained stock {c['unexplained_stock']}",
             f"  plan sha256 {plan['plan_sha256']}"]
    if plan.get("excluded"):
        lines.append(f"  EXCLUDED (review_exclusions, not planned) {len(plan['excluded'])}: "
                     + ", ".join(f"{x['pid']} {x['name']!r}" for x in plan["excluded"][:20]))
    for d in plan["decisions"]:
        if d["review"] != AUTO:
            lines.append(f"  {d['review']} {d['pid']} {d['name']!r} {d['action']}: "
                         + "; ".join(d["reasons"] + d["flags"]))
    for p in plan["run_problems"]:
        lines.append(f"  PROBLEM {p}")
    return "\n".join(lines)


# ---------------------------------------------------------------- apply (writes)
def verify_created(live: dict, payload: dict, item_id) -> list[str]:
    problems = w7.verify_matches(live, payload, item_id=item_id)
    if live.get("Active") is not True:
        problems.append("item is not active")
    if payload["Type"] == "Inventory":
        if w7.D(live.get("QtyOnHand"), Decimal(0)) != 0:
            problems.append(f"QtyOnHand {live.get('QtyOnHand')} != 0")
        if str((live.get("AssetAccountRef") or {}).get("value")) != ASSET_ID:
            problems.append("AssetAccountRef is not 77")
    if october_target_error(live.get("Type"), live.get("Sku"), live.get("Id"), live.get("Name")):
        problems.append("not a valid October target")
    return problems


def create_or_adopt(client, d: dict) -> tuple[str, str]:
    """Create (or adopt) one item; returns (status, QBO Id). Raises StopRun."""
    payload = d["payload"]
    if payload.get("Type") == "Inventory" and payload.get("QtyOnHand") != 0:
        raise StopRun(f"{d['target']['sku']}: refusing a create with QtyOnHand {payload.get('QtyOnHand')}")
    if d["adopt_qbo_id"]:
        return "ADOPTED", d["adopt_qbo_id"]
    resp = client.post_json("/item", payload, d["payload_sha256"][:36])
    if resp.status_code == 200 and (resp.json().get("Item") or {}).get("Id"):
        return "CREATED", str(resp.json()["Item"]["Id"])
    again = [it for it in w7.find_existing(client, payload) if clean(it.get("Description")).startswith(MARKER)]
    if len(again) == 1 and not w7.verify_matches(again[0], payload):
        return "CREATED", str(again[0]["Id"])
    raise StopRun(f"{d['target']['sku']}: create failed {resp.status_code}: {resp.text[:500]}")


APPLIED_STATE = "applied_state.json"


def select_for_apply(plan: dict, automated: bool, *, only=None, exclude=None, excluded_now=frozenset(),
                     already=frozenset(), mapped=frozenset()) -> tuple[list[dict], list[dict]]:
    """(selected decisions, not selected [{pid, name, why}]).

    Without ``only`` / ``exclude`` every AUTO (automated) or AUTO+REVIEW (manual) decision is taken and
    a child whose in-plan master is not taken is dropped silently. With an explicit selection the
    dependencies are enforced: a MAPPING_ONLY child whose master is created by this plan needs the
    master selected too (or applied earlier from this plan / already in the installed mapping),
    else ``ApplyRefused``. ``excluded_now`` (review_exclusions added after the plan) and ``already``
    (applied by an earlier partial apply of this plan) are never selected."""
    explicit = only is not None or exclude is not None
    by_pid = {d["pid"]: d for d in plan["decisions"]}
    if explicit:
        if automated:
            raise ApplyRefused("--only / --exclude are for a manual apply, not --auto")
        unknown = sorted((set(only or ()) | set(exclude or ())) - set(by_pid))
        if unknown:
            raise ApplyRefused(f"EPOS id(s) {', '.join(unknown)} are not decisions in this plan "
                               f"(excluded / already mapped / not new?)")
        held = sorted(pid for pid in (only or ()) if by_pid[pid]["review"] == HOLD)
        if held:
            raise ApplyRefused("cannot apply HOLD decision(s) " + "; ".join(
                f"{pid}: {'; '.join(by_pid[pid]['reasons'])}" for pid in held))
        both = sorted(set(only or ()) & set(exclude or ()))
        if both:
            raise ApplyRefused(f"EPOS id(s) {', '.join(both)} are in both --only and --exclude")
    allowed = {AUTO} if automated else {AUTO, REVIEW}
    chosen, skipped = [], []
    for d in plan["decisions"]:
        if d["review"] not in allowed:
            continue
        if only is not None and d["pid"] not in only:
            skipped.append({"pid": d["pid"], "name": d["name"], "why": "not in --only"})
        elif exclude is not None and d["pid"] in exclude:
            skipped.append({"pid": d["pid"], "name": d["name"], "why": "--exclude"})
        elif d["pid"] in excluded_now:
            skipped.append({"pid": d["pid"], "name": d["name"], "why": "excluded in review_exclusions since the plan"})
        elif d["pid"] in already:
            skipped.append({"pid": d["pid"], "name": d["name"], "why": "already applied from this plan"})
        else:
            chosen.append(d)
    ids = {d["pid"] for d in chosen}
    out, problems = [], []
    for d in chosen:
        mid = d["master_pid"]
        if d["action"] != MAPPING_ONLY or mid not in by_pid or mid in ids or mid in already or mid in mapped:
            out.append(d)
            continue
        why = "excluded in review_exclusions" if mid in excluded_now else (
            "on HOLD" if by_pid[mid]["review"] == HOLD else "not selected")
        if explicit:
            problems.append(f"{d['pid']} {d['name']!r} maps onto master {mid} {by_pid[mid]['name']!r}, which this "
                            f"plan creates but is {why}; select {mid} too (or apply it first)")
        else:
            skipped.append({"pid": d["pid"], "name": d["name"], "why": f"master {mid} {why}"})
    if problems:
        raise ApplyRefused("dependency: " + "; ".join(problems))
    return out, skipped


def read_applied_state(plan_dir: Path) -> dict:
    path = Path(plan_dir) / APPLIED_STATE
    return json.loads(path.read_text()) if path.exists() else {"mapping_sha256": "", "applied": {}}


def apply_plan(plan_dir: Path, *, approval_ref: str, expect_sha: str, automated: bool, client, mapping_path: Path,
               state: Path, slack: bool = True, max_creates: int | None = None, only=None, exclude=None,
               expect_decision_shas: dict | None = None, exclusions=None) -> dict:
    """Create the plan's items, fill Ids, validate and install the new mapping. Fail-closed.

    Selection contract (manual apply): ``only`` / ``exclude`` (EPOS ids) pick decisions; each selected
    decision's stored ``decision_sha256`` must equal its recomputed digest (and ``expect_decision_shas``
    when given). A plan may be applied in several parts: ``applied_state.json`` in the plan folder
    records what was applied and the mapping sha it installed, which a later apply of the same plan
    accepts as the current mapping."""
    plan_dir = Path(plan_dir)
    if not clean(approval_ref):
        raise ApplyRefused("apply requires an approval reference (chat yes, or OIAT_COMPANY_A_CATALOGUE_APPROVAL_REF)")
    plan = json.loads((plan_dir / "plan.json").read_text())
    digest = plan_digest(plan["decisions"])
    if digest != plan["plan_sha256"] or digest != clean(expect_sha):
        raise ApplyRefused(f"plan sha mismatch: plan.json gives {digest}, expected {expect_sha or '(none)'}; re-review the plan")
    only = {canonical_product_id(x) for x in only} if only is not None else None
    exclude = {canonical_product_id(x) for x in exclude} if exclude is not None else None
    expect_decision_shas = {canonical_product_id(k): clean(v) for k, v in (expect_decision_shas or {}).items()}
    mismatched = [d["pid"] for d in plan["decisions"]
                  if "decision_sha256" in d and d["decision_sha256"] != decision_digest(d)]
    if mismatched:
        raise ApplyRefused(f"decision(s) {', '.join(mismatched)} changed since the plan (decision_sha256 differs); "
                           "re-run plan")
    if (only is not None or exclude is not None or expect_decision_shas) and any(
            "decision_sha256" not in d for d in plan["decisions"]):
        raise ApplyRefused("this plan has no per-decision digests (built by an older catalogue_sync); re-run plan "
                           "before a selective apply")
    applied_state = read_applied_state(plan_dir)
    current = sha256_file(mapping_path)
    if current not in {plan["inputs"]["mapping"]["sha256"], applied_state.get("mapping_sha256")}:
        raise ApplyRefused("the installed mapping changed since the plan was built; re-run plan")
    if exclusions is None:
        exclusions = review_exclusions.load(review_exclusions.path_near(mapping_path))
    mapping_by_pid = {canonical_product_id(r.get("EPOS Product ID")): r for r in read_csv(mapping_path)}
    excluded_now = {d["pid"] for d in plan["decisions"] if exclusions.get("product", d["pid"])}
    already = set(applied_state.get("applied", {}))
    selected, not_selected = select_for_apply(plan, automated, only=only, exclude=exclude, excluded_now=excluded_now,
                                              already=already, mapped=set(mapping_by_pid))
    for d in selected:
        want = expect_decision_shas.get(d["pid"])
        if expect_decision_shas and want is None:
            raise ApplyRefused(f"{d['pid']} is selected but has no expected decision sha")
        if want is not None and want != d.get("decision_sha256"):
            raise ApplyRefused(f"{d['pid']}: decision changed (plan {d.get('decision_sha256')}, approved {want}); "
                               "re-review it")
        mid = d["master_pid"]
        if d["action"] == MAPPING_ONLY and not d["target"]["qbo_id"] and mid not in {x["pid"] for x in selected}:
            row = mapping_by_pid.get(mid)  # master applied earlier from this plan
            if row is not None and clean(row.get("Target QBO SKU")) == d["target"]["sku"]:
                d["target"]["qbo_id"] = clean(row.get("Target QBO Item Id"))
    selected_ids = {d["pid"] for d in selected}
    creates = [d for d in selected if d["action"] in CREATE_ACTIONS]
    new_creates = [d for d in creates if not d["adopt_qbo_id"]]
    if automated and max_creates is not None and len(new_creates) > max_creates:
        msg = (f":warning: Akponora catalogue sync refused: {len(new_creates)} new QBO items exceed the automated cap "
               f"{max_creates} ({CAP_ENV}). Review {plan_dir} and apply manually.")
        if slack:
            send_slack(msg)
        raise ApplyRefused(msg)
    receipt = {"tool": TOOL, "plan_dir": str(plan_dir), "plan_sha256": digest, "approval_ref": approval_ref,
               "automated": automated, "started_at": now_iso(), "created": [], "adopted": [], "mapping_only": [],
               "skipped_review": [{"pid": d["pid"], "name": d["name"], "flags": d["flags"]} for d in plan["decisions"]
                                  if d["review"] != HOLD and d["pid"] not in selected_ids],
               "holds": [{"pid": d["pid"], "name": d["name"], "reasons": d["reasons"]}
                         for d in plan["decisions"] if d["review"] == HOLD],
               "unexplained_stock": [{"pid": d["pid"], "name": d["name"], "stock": (d["stock"] or {}).get("units")}
                                     for d in selected if "UNEXPLAINED_STOCK" in d["flags"]],
               "changed": plan["changed"], "removed": plan["removed"], "installed": None, "stopped": None,
               "selection": {"mode": "explicit" if only is not None or exclude is not None else "all",
                             "only": sorted(only) if only is not None else None,
                             "exclude": sorted(exclude) if exclude is not None else None,
                             "selected": sorted(selected_ids, key=lambda x: (len(x), x)),
                             "decision_shas": {d["pid"]: d.get("decision_sha256", "") for d in selected}},
               "not_selected": not_selected, "applied": [],
               "excluded": list(plan.get("excluded", [])) + [
                   {"pid": d["pid"], "name": d["name"], "reason": "excluded in review_exclusions after the plan"}
                   for d in plan["decisions"] if d["pid"] in excluded_now],
               "already_applied": sorted(already & ({d["pid"] for d in plan["decisions"]}))}
    if not selected:
        receipt["finished_at"] = now_iso()
        finish(receipt, plan_dir, state, plan, slack)
        return receipt
    before = {d["pid"]: d["review"] for d in creates}
    info = qbo_checks(client, creates + [d for d in selected if d["action"] == MAPPING_ONLY], load_ledger(state))
    changed_now = [d for d in creates if d["review"] == HOLD and before[d["pid"]] != HOLD]
    changed_now += [d for d in selected if d["action"] == MAPPING_ONLY and d["review"] == HOLD]
    if info["run_problems"] or changed_now:
        raise ApplyRefused("QBO preflight changed since the plan: " + "; ".join(
            info["run_problems"] + [f"{d['pid']}: {'; '.join(d['reasons'])}" for d in changed_now]))
    results_path, ledger_path = plan_dir / "results.csv", state / "ledger.csv"
    created_ids = {}
    try:
        for seq, d in enumerate(sorted(creates, key=lambda x: x["action"] != CREATE_INV), start=1):
            status, item_id = create_or_adopt(client, d)
            live = client.get_json(f"/item/{item_id}")["Item"]
            problems = verify_created(live, d["payload"], item_id)
            row = {"ts": now_iso(), "seq": seq, "kind": d["target"]["type"], "Sku": d["target"]["sku"],
                   "Name": d["target"]["name"], "status": status if not problems else "VERIFY_FAILED",
                   "QBO Id": item_id, "SyncToken": live.get("SyncToken"), "payload_sha256": d["payload_sha256"],
                   "requestid": d["payload_sha256"][:36], "approval_ref": approval_ref, "detail": "; ".join(problems)}
            w7.append_result(results_path, row)
            w7.append_result(ledger_path, row)
            if problems:
                raise StopRun(f"{d['target']['sku']} (Id {item_id}) failed verification: {'; '.join(problems)}")
            created_ids[d["pid"]] = item_id
            d["target"]["qbo_id"] = item_id
            receipt["created" if status == "CREATED" else "adopted"].append(
                {"pid": d["pid"], "sku": d["target"]["sku"], "name": d["target"]["name"], "qbo_id": item_id,
                 "type": d["target"]["type"], "flags": d["flags"]})
        by_pid = {d["pid"]: d for d in plan["decisions"]}
        for d in selected:
            if d["action"] == MAPPING_ONLY:
                if not d["target"]["qbo_id"]:
                    master = by_pid.get(d["master_pid"])
                    d["target"]["qbo_id"] = created_ids.get(d["master_pid"], "") or (master or {}).get("target", {}).get("qbo_id", "")
                receipt["mapping_only"].append({"pid": d["pid"], "name": d["name"], "target_sku": d["target"]["sku"],
                                                "qbo_id": d["target"]["qbo_id"], "multiplier": d["multiplier"]})
        receipt["installed"] = install_mapping(plan_dir, selected, approval_ref, mapping_path)
        stamp = now_iso()
        receipt["applied"] = [{"pid": d["pid"], "name": d["name"], "action": d["action"], "review": d["review"],
                               "decision_sha256": d.get("decision_sha256", ""), "target_sku": d["target"]["sku"],
                               "qbo_id": d["target"]["qbo_id"], "multiplier": d["multiplier"]} for d in selected]
        applied_state["mapping_sha256"] = sha256_file(mapping_path)
        for x in receipt["applied"]:
            applied_state.setdefault("applied", {})[x["pid"]] = {
                "qbo_id": x["qbo_id"], "decision_sha256": x["decision_sha256"], "approval_ref": approval_ref,
                "at": stamp}
        dump_json(plan_dir / APPLIED_STATE, applied_state)
    except StopRun as exc:
        receipt["stopped"] = str(exc)
        receipt["finished_at"] = now_iso()
        finish(receipt, plan_dir, state, plan, slack)
        raise
    receipt["finished_at"] = now_iso()
    finish(receipt, plan_dir, state, plan, slack)
    return receipt


def install_mapping(plan_dir: Path, selected: list[dict], approval_ref: str, mapping_path: Path) -> dict:
    rows = read_csv(mapping_path)
    approved_by = f"{approval_ref} (catalogue_sync {now_iso()[:10]})"
    additions = [mapping_row(d, approved_by, "Approved") for d in selected]
    for r in additions:
        if not clean(r["Target QBO Item Id"]).isdigit():
            raise StopRun(f"{r['EPOS Product ID']}: no QBO Item Id for the new mapping row")
    new_csv = plan_dir / "approved_mapping_new.csv"
    with open(new_csv, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MAPPING_COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows + additions)
    registry = ProductConversionRegistry.from_csv(new_csv)
    for r in additions:
        rule = registry.resolve(product_name=r["EPOS Name"], product_id=r["EPOS Product ID"],
                                transaction_date=date.fromisoformat(INV_START))
        reason = october_target_error(rule.target_qbo_type, rule.target_qbo_sku, rule.target_qbo_item_id,
                                      rule.target_qbo_name)
        if reason or rule.target_qbo_item_id in {CATCH_ALL_ITEM_ID} or rule.target_qbo_name.startswith(LEGACY_PREFIX):
            raise StopRun(f"{r['EPOS Product ID']}: {reason or 'forbidden target'}")
    return install(new_csv, mapping_path, sha256_file(new_csv), approval_ref)


def apply_text(receipt: dict) -> str:
    head = ":white_check_mark:" if receipt["installed"] and not receipt["stopped"] else (
        ":x:" if receipt["stopped"] else ":information_source:")
    lines = [f"{head} Akponora catalogue sync apply ({'automated' if receipt['automated'] else 'manual'}, "
             f"approval {receipt['approval_ref']})"]
    for key, label in (("created", "QBO items created"), ("adopted", "QBO items adopted")):
        if receipt[key]:
            lines.append(f"{label} ({len(receipt[key])}): " + ", ".join(
                f"{x['sku']} {x['name']!r} Id {x['qbo_id']}" for x in receipt[key][:30]))
    if receipt["mapping_only"]:
        lines.append(f"Mapping-only rows ({len(receipt['mapping_only'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r} -> {x['target_sku']} x{x['multiplier']}" for x in receipt["mapping_only"][:30]))
    if receipt["installed"]:
        lines.append(f"Mapping installed: sha256 {receipt['installed']['sha256']} ({receipt['installed']['rows']} rows)")
    if receipt["unexplained_stock"]:
        lines.append(":warning: UNEXPLAINED_STOCK (created at qty 0; decide on an InventoryAdjustment, chat yes): "
                     + ", ".join(f"{x['pid']} {x['name']!r} stock {x['stock']}" for x in receipt["unexplained_stock"]))
    if receipt["skipped_review"]:
        lines.append(f"Needs manual review ({len(receipt['skipped_review'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r} [{'; '.join(x['flags'])}]" for x in receipt["skipped_review"][:30]))
    if receipt.get("not_selected"):
        lines.append(f"Not selected ({len(receipt['not_selected'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r} [{x['why']}]" for x in receipt["not_selected"][:30]))
    if receipt.get("excluded"):
        lines.append(f"Excluded (review_exclusions) ({len(receipt['excluded'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r}" for x in receipt["excluded"][:20]))
    if receipt["holds"]:
        lines.append(f"HOLD ({len(receipt['holds'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r} [{'; '.join(x['reasons'])}]" for x in receipt["holds"][:30]))
    if receipt["changed"]:
        lines.append(f"Changed in EPOS ({len(receipt['changed'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r}" for x in receipt["changed"][:20]))
    if receipt["removed"]:
        lines.append(f"Removed from EPOS ({len(receipt['removed'])}): " + ", ".join(
            f"{x['pid']} {x['name']!r}" for x in receipt["removed"][:20]))
    if receipt["stopped"]:
        lines.append(f"STOPPED: {receipt['stopped']}")
    lines.append(f"Evidence: {receipt['plan_dir']}")
    return "\n".join(lines)


def plan_slack_text(plan: dict, out: Path) -> str:
    receipt = {"automated": False, "approval_ref": "none (plan only)", "created": [], "adopted": [], "mapping_only": [],
               "installed": None, "stopped": None, "plan_dir": str(out),
               "unexplained_stock": [{"pid": d["pid"], "name": d["name"], "stock": (d["stock"] or {}).get("units")}
                                     for d in plan["decisions"] if "UNEXPLAINED_STOCK" in d["flags"]],
               "skipped_review": [{"pid": d["pid"], "name": d["name"], "flags": d["flags"] or [d["action"]]}
                                  for d in plan["decisions"] if d["review"] != HOLD],
               "holds": [{"pid": d["pid"], "name": d["name"], "reasons": d["reasons"]}
                         for d in plan["decisions"] if d["review"] == HOLD],
               "changed": plan["changed"], "removed": plan["removed"]}
    text = apply_text(receipt).split("\n", 1)[-1]
    return (f":mag: Akponora catalogue sync plan: {plan['counts']['new']} new EPOS product(s) need mapping "
            f"(plan sha {plan['plan_sha256'][:12]}; nothing written)\n{text}")


def finish(receipt: dict, plan_dir: Path, state: Path, plan: dict, slack: bool) -> None:
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S_%fZ')
    dump_json(plan_dir / "apply_receipt.json", receipt)  # latest (daily_run reads this one)
    dump_json(plan_dir / f"apply_receipt_{stamp}.json", receipt)  # one per apply (partial applies)
    receipts = state / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    dump_json(receipts / f"{stamp}.json", receipt)
    snap = plan_dir / "catalogue_snapshot.json"
    if not receipt["stopped"] and plan.get("only_ids") is None and snap.exists():
        shutil.copyfile(snap, state / "catalogue_snapshot.json")
    if slack:
        send_slack(apply_text(receipt))


# ---------------------------------------------------------------- orchestration
def run_plan(out: Path, *, mapping_path: Path, state: Path, catalogue_path=None, products_dir=None, po_list=None,
             po_details=None, only_ids=None, qbo: bool = True, headless: bool = True, min_seconds: float = 1.2,
             client=None, epos=None, catalogue=None) -> dict:
    out = Path(out)
    live = LiveEpos(out, headless=headless, min_seconds=min_seconds)
    if catalogue is None:
        catalogue = load_catalogue(catalogue_path) if catalogue_path else live.catalogue()
    if epos is None:
        epos = OfflineEpos(products_dir, po_list, po_details) if products_dir else live
    if client is None and qbo:
        client = w7.QBOClient.for_company_a(allow_writes=False)
    return build_plan(out=out, catalogue=catalogue, mapping_path=Path(mapping_path), state=Path(state), epos=epos,
                      client=client if qbo else None, only_ids=only_ids)


def run_automated(out: Path, *, mapping_path: Path, state: Path, slack: bool = True, write_client=None,
                  **plan_kwargs) -> tuple[dict, dict | None]:
    """Fresh plan, then apply AUTO rows only (env-gated). Returns (plan, receipt or None)."""
    if not automated_mode_enabled():
        raise ApplyRefused(f"automated mode needs {AUTO_ENV}=1 and {APPROVAL_ENV}")
    plan = run_plan(out, mapping_path=mapping_path, state=state, **plan_kwargs)
    if not plan["decisions"] and not plan["changed"] and not plan["removed"]:
        return plan, None
    client = write_client or w7.QBOClient.for_company_a(allow_writes=True)
    receipt = apply_plan(out, approval_ref=clean(os.getenv(APPROVAL_ENV)), expect_sha=plan["plan_sha256"],
                         automated=True, client=client, mapping_path=mapping_path, state=state, slack=slack,
                         max_creates=auto_cap())
    return plan, receipt


def ensure_products_mapped(product_ids: set[str], *, out_dir, mapping_path=None, state=None, slack: bool = True,
                           **plan_kwargs) -> dict:
    """Pipeline hook: plan (and, in automated mode, apply) only the till product ids not yet mapped.

    Returns {"unmapped", "unresolved", "applied", "plan_sha256", "holds", "review", "error", "out"}. The caller
    still fails the day while ``unresolved`` is non-empty.
    """
    mapping_path = Path(mapping_path or mapping_file())
    state = Path(state or state_dir(TOOL))
    ids = {canonical_product_id(x) for x in product_ids if clean(x)}
    registry = ProductConversionRegistry.from_csv(mapping_path)
    unmapped = sorted(i for i in ids if i.casefold() not in registry.by_product_id)
    result = {"unmapped": unmapped, "unresolved": list(unmapped), "applied": False, "plan_sha256": "",
              "holds": [], "review": [], "excluded": [], "error": "", "out": ""}
    if not unmapped:
        return result
    out = run_dir(TOOL, out_dir)
    result["out"] = str(out)
    try:
        if automated_mode_enabled():
            plan, receipt = run_automated(out, mapping_path=mapping_path, state=state, slack=slack,
                                          only_ids=unmapped, **plan_kwargs)
            result["applied"] = bool(receipt and receipt.get("installed"))
        else:
            plan = run_plan(out, mapping_path=mapping_path, state=state, only_ids=unmapped, **plan_kwargs)
            if slack:
                send_slack(plan_slack_text(plan, out))
        result["plan_sha256"] = plan["plan_sha256"]
        result["holds"] = [d["pid"] for d in plan["decisions"] if d["review"] == HOLD]
        result["excluded"] = [x["pid"] for x in plan.get("excluded", [])]
        result["review"] = [d["pid"] for d in plan["decisions"] if d["review"] == REVIEW]
    except StopRun as exc:
        result["error"] = str(exc)
        if slack:
            send_slack(f":x: Akponora catalogue sync for till products {', '.join(unmapped[:20])} failed: {exc}")
    after = ProductConversionRegistry.from_csv(mapping_path)
    result["unresolved"] = [i for i in unmapped if i.casefold() not in after.by_product_id]
    return result


# ---------------------------------------------------------------- CLI
def parse_ids(a) -> list[str] | None:
    ids = []
    if a.ids:
        ids += [x.strip() for x in a.ids.split(",") if x.strip()]
    if a.ids_file:
        ids += [x.strip() for x in Path(a.ids_file).read_text().splitlines() if x.strip()]
    return sorted({canonical_product_id(x) for x in ids}) if ids else None


def split_ids(text) -> list[str] | None:
    """``--only`` / ``--exclude``: None when the flag is absent; refuses an empty list."""
    if text is None:
        return None
    ids = [canonical_product_id(x.strip()) for x in str(text).split(",") if x.strip()]
    if not ids:
        raise ApplyRefused("--only / --exclude need at least one EPOS Product ID")
    return ids


def parse_decision_shas(text) -> dict:
    out = {}
    for part in (x.strip() for x in str(text or "").split(",")):
        if not part:
            continue
        pid, sep, sha = part.partition("=")
        if not sep or not pid.strip() or not sha.strip():
            raise ApplyRefused(f"--expect-decision-shas entry {part!r} is not pid=sha")
        out[canonical_product_id(pid.strip())] = sha.strip()
    return out


def plan_kwargs(a) -> dict:
    return {"catalogue_path": a.catalogue, "products_dir": a.master_links_dir, "po_list": a.po_list,
            "po_details": a.po_details, "only_ids": parse_ids(a), "qbo": not a.no_qbo, "headless": not a.headed,
            "min_seconds": a.min_seconds}


def add_plan_args(p) -> None:
    p.add_argument("--out", help="evidence folder (default outputs/catalogue_sync_<UTC stamp>/)")
    p.add_argument("--catalogue", help="offline catalogue_products.json / .csv / catalogue_api_pages.json")
    p.add_argument("--master-links-dir", help="offline Advanced Edit scrapes (<id>.json or products/<id>.json)")
    p.add_argument("--po-list", help="offline po_list_raw.json (capture_pos)")
    p.add_argument("--po-details", help="offline po_details.jsonl (capture_pos)")
    p.add_argument("--ids", help="comma-separated EPOS Product IDs to consider (default: whole catalogue)")
    p.add_argument("--ids-file", help="file with one EPOS Product ID per line")
    p.add_argument("--no-qbo", action="store_true", help="skip the read-only QBO checks (offline)")
    p.add_argument("--headed", action="store_true", help="show the EPOS browser")
    p.add_argument("--min-seconds", type=float, default=1.2, help="minimum seconds per EPOS product page (>= 1.2)")
    p.add_argument("--mapping", help="installed mapping (default: company_a product_conversion_file)")
    p.add_argument("--no-slack", action="store_true")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="READ-ONLY plan (default)")
    add_plan_args(p)
    p.add_argument("--slack", action="store_true", help="send the plan summary to Slack when it needs attention")
    p.add_argument("--update-snapshot", action="store_true",
                   help="accept the current catalogue as the CHANGED baseline (state_dir snapshot)")
    ap_ = sub.add_parser("apply", help="WRITES: create items + install mapping")
    add_plan_args(ap_)
    ap_.add_argument("--plan-dir", help="folder of a reviewed plan (manual apply)")
    ap_.add_argument("--approval-ref", default="", help="chat-yes reference for this apply")
    ap_.add_argument("--expect-sha", "--expect-plan-sha", dest="expect_plan_sha", default="",
                     help="plan_sha256 from the reviewed summary.json")
    ap_.add_argument("--only", default=None, help="comma-separated EPOS Product IDs: apply only these decisions")
    ap_.add_argument("--exclude", default=None, help="comma-separated EPOS Product IDs: apply all but these")
    ap_.add_argument("--expect-decision-shas", default="",
                     help="pid=decision_sha256,... (summary.json decision_shas); every selected decision must match")
    ap_.add_argument("--json", action="store_true", help="print the apply receipt as JSON")
    ap_.add_argument("--auto", action="store_true", help=f"automated mode ({AUTO_ENV}=1 + {APPROVAL_ENV})")
    s = sub.add_parser("scheduled", help="daily_run entry: apply --auto when enabled, else plan + Slack")
    add_plan_args(s)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"plan", "apply", "scheduled", "-h", "--help"}:
        argv = ["plan", *argv]
    a = ap.parse_args(argv)
    try:
        mapping_path = Path(a.mapping) if a.mapping else mapping_file()
        state = state_dir(TOOL)
        slack = not a.no_slack
        if a.cmd == "plan":
            out = run_dir(TOOL, a.out)
            plan = run_plan(out, mapping_path=mapping_path, state=state, **plan_kwargs(a))
            print(plan_text(plan, out))
            if a.update_snapshot and plan["only_ids"] is None:
                shutil.copyfile(out / "catalogue_snapshot.json", state / "catalogue_snapshot.json")
            if a.slack and slack and (plan["decisions"] or plan["changed"] or plan["removed"]):
                send_slack(plan_slack_text(plan, out))
            return 3 if needs_attention(plan) else 0
        if a.cmd == "scheduled" or (a.cmd == "apply" and a.auto):
            if getattr(a, "only", None) or getattr(a, "exclude", None):
                raise ApplyRefused("--only / --exclude are for a manual apply with --plan-dir, not --auto")
            out = run_dir(TOOL, a.out)
            if a.cmd == "scheduled" and not automated_mode_enabled():
                plan = run_plan(out, mapping_path=mapping_path, state=state, **plan_kwargs(a))
                print(plan_text(plan, out))
                if plan["decisions"] or plan["changed"] or plan["removed"]:
                    if slack:
                        send_slack(plan_slack_text(plan, out))
                    if plan["only_ids"] is None:
                        shutil.copyfile(out / "catalogue_snapshot.json", state / "catalogue_snapshot.json")
                return 3 if needs_attention(plan) else 0
            plan, receipt = run_automated(out, mapping_path=mapping_path, state=state, slack=slack, **plan_kwargs(a))
            print(plan_text(plan, out))
            if receipt:
                print(apply_text(receipt))
            return 3 if needs_attention(plan) else 0
        if not a.plan_dir:
            raise ApplyRefused("manual apply needs --plan-dir (a reviewed plan), --approval-ref and --expect-sha")
        receipt = apply_plan(Path(a.plan_dir), approval_ref=a.approval_ref, expect_sha=a.expect_plan_sha,
                             automated=False, client=w7.QBOClient.for_company_a(allow_writes=True),
                             mapping_path=mapping_path, state=state, slack=slack, only=split_ids(a.only),
                             exclude=split_ids(a.exclude), expect_decision_shas=parse_decision_shas(a.expect_decision_shas))
        print(json.dumps(receipt, indent=1, default=str) if a.json else apply_text(receipt))
        return 0
    except ApplyRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 4
    except StopRun as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

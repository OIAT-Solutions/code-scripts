#!/usr/bin/env python3
"""W7: create the October Inventory / NonInventory catalogue for AKPONORA (company_a).

Realm 9341455406194328 (production). Follow AGENTS.md: creating items is a QBO
write that needs an explicit chat yes. DEFAULT IS DRY-RUN.

Subcommands
-----------
``create`` (default dry-run)
    Builds one Inventory item per approved canonical family (opening qty from the
    30 Sep EPOS StockReport, zero-floor, cost per canonical unit from the EPOS
    catalogue) via ``cutover_drafts.catalogue_drafts``, and one NonInventory
    item per untracked EPOS product via ``cutover_drafts.noninventory_drafts``.
    Writes ``payloads.jsonl``, ``flags.csv``, ``summary.json``; runs a live
    read-only preflight (name / SKU collisions, accounts, tax codes, IA 77
    balance ``B``) and refuses on any collision.

    ``--execute`` (needs ``--approval-ref`` and ``--expect-payloads-sha`` from
    the dry-run) POSTs creates at <=5 req/s with an Intuit ``requestid`` per
    payload, retrying 429/5xx/network with the same requestid. Before every
    create the item is looked up by Sku and Name; an existing exact match is
    adopted (verified), a mismatch stops the run. Every created item is re-read
    and verified; the first verification failure stops the run (exit 2).
    Resumable via ``results.csv``. ``--test N`` creates only the first N pending
    payloads (pilot). ``register.csv`` maps family / EPOS ids -> QBO Item Id,
    SyncToken. ``ia_before.json`` pins ``B`` at the first execute; every execute
    run records the IA 77 balance after creates (``B + C``) in
    ``summary_execute.json`` for ``post_journal.py offset``.

``fill-ids``
    Fills ``Target QBO Item Id`` on every approved mapping row from the register
    (by Target QBO SKU), writes ``approved_mapping_with_ids.csv``, loads it with
    ``ProductConversionRegistry`` and checks every October target Id is in the
    register with the same name / SKU / type. Prints the sha256 to pin with
    ``install_conversion_mapping``.

Field choices (documented, match legacy items and the 26 Sep create lists)
-------------------------------------------------------------------------
* Inventory: Name, Sku AKP-{owner ProductID}, Type Inventory, TrackQtyOnHand,
  QtyOnHand (canonical units, zero floor), InvStartDate 2026-10-01,
  PurchaseCost = owner CostPriceExTax / owner multiplier (5 dp, ex-tax, so
  PurchaseTaxIncluded false), UnitPrice = owner SalePriceIncTax / multiplier
  (2 dp, SalesTaxIncluded true), Income 400xxx, Expense (COGS) 200xxx,
  AssetAccountRef 77. No ParentRef (top-level, so FQN == Name).
* Tax codes: legacy Inventory items use SalesTaxCodeRef/PurchaseTaxCodeRef 2
  ("7.5% S") with SalesTaxIncluded true; NoTax products use 7 ("No VAT").
  The create lists carry the approved code per item (VAT -> 2, NoTax -> 7) and
  Taxable follows the EPOS sale tax group. Legacy items store a tax-inclusive
  PurchaseCost with PurchaseTaxIncluded true; the new items store the ex-tax
  cost, so PurchaseTaxIncluded is false (opening value = qty x ex-tax cost).
  The catch-all 15030 (NonInventory) has Taxable/SalesTaxIncluded true and no
  explicit codes; sales receipts set TaxCodeRef per line either way.
* NonInventory: Name, Sku AKP-NS-{ProductID}, Type NonInventory, income and
  purchases (200xxx) accounts by category, UnitPrice / PurchaseCost (ex-tax)
  from the catalogue, same tax-code rule. No asset / qty / start date.

The opening rehearsal on an earlier stock report is allowed (``--as-of``
labels it) but ``--execute`` refuses unless ``--as-of 2026-09-30`` and the
stock report file name is dated 2026_09_30. ``catalogue_drafts`` only accepts
the final 30 Sep cutoff, so rehearsal counts are fed with that cutoff and
labelled with their real date in ``source_ref`` and the summary.

Run from the repo root:
  python -m code_scripts.scripts.akponora_cutover.w7_create_items create --help
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_scripts.conversion_contract import validate_live_item  # noqa: E402
from code_scripts.cutover_drafts import NONSTOCK_CATEGORY_BAND, catalogue_drafts, noninventory_drafts  # noqa: E402
from code_scripts.product_conversion import (  # noqa: E402
    ProductConversionRegistry,
    canonical_product_id,
    canonical_target_type,
    clean,
    october_target_error,
)

COMPANY = "company_a"
REALM = "9341455406194328"
FINAL_CUTOFF = "2026-09-30"
INV_START = "2026-10-01"
ASSET_ID = "77"
INCOME_IDS = {"1150040024", "1150040025", "1150040031", "1150040032"}  # 400100/400300/400202/400201
EXPENSE_IDS = {"74", "1150040020", "1150040033", "1150040034"}  # 200100/200300/200201/200202
TAX_CODE_IDS = {"2", "7"}  # 7.5% S / No VAT
NONFOOD_FALLBACK_CATEGORY = "HOUSEHOLD GOODS & PACKAGING MATERIALS"
MAX_RPS = 5.0
RESULT_COLS = ["ts", "seq", "kind", "Sku", "Name", "status", "QBO Id", "SyncToken", "payload_sha256",
               "requestid", "approval_ref", "detail"]
DONE = {"CREATED", "ADOPTED"}
REGISTER_COLS = ["Family Key", "Kind", "Target QBO SKU", "Target QBO Name", "Target QBO Item Type",
                 "EPOS Product IDs", "QBO Item Id", "SyncToken", "QtyOnHand", "PurchaseCost",
                 "Opening value", "Status"]


class StopRun(Exception):
    """Fail-closed stop; the message says what to reconcile."""


# ---------------------------------------------------------------- small helpers
def D(value, default=None) -> Decimal | None:
    text = clean(value).replace(",", "")
    if not text:
        return default
    try:
        n = Decimal(text)
    except InvalidOperation:
        return default
    return n if n.is_finite() else default


def q(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_csv(path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def norm_name(value) -> str:
    return clean(value).casefold()


def truthy(value) -> bool:
    return clean(value).lower() in {"true", "yes", "1"}


def qbo_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- QBO client
class QBOClient:
    """Throttled QBO client. Non-GET calls are refused unless ``allow_writes``.

    ``session`` needs ``request(method, url, params=, headers=, data=, timeout=)``
    (``requests.Session`` or a test fake). ``token_provider()`` returns a bearer
    token; ``token_refresh()`` is called once on 401.
    """

    def __init__(self, session, token_provider, base_url, *, allow_writes=False, max_rps=MAX_RPS,
                 token_refresh=None, minorversion="75", sleep=time.sleep, retries=6):
        if max_rps > MAX_RPS:
            raise ValueError("max_rps must be <= 5")
        self.session = session
        self._token_provider = token_provider
        self._token_refresh = token_refresh or token_provider
        self._token = None
        self.base = base_url.rstrip("/")
        self.allow_writes = allow_writes
        self.min_interval = 1.0 / max_rps
        self.minorversion = minorversion
        self.sleep = sleep
        self.retries = retries
        self._last = 0.0
        self.requests = 0

    @classmethod
    def for_company_a(cls, *, allow_writes=False, max_rps=MAX_RPS):
        os.environ.setdefault("OIAT_COMPANIES_DIR", str(REPO_ROOT / "code_scripts" / "companies"))
        import requests

        from code_scripts.company_config import get_qbo_api_base_url, load_company_config
        from code_scripts.token_manager import get_access_token, refresh_access_token

        cfg = load_company_config(COMPANY)
        if str(cfg.realm_id) != REALM:
            raise StopRun(f"company_a realm is {cfg.realm_id}, expected {REALM}")
        base = f"{get_qbo_api_base_url(cfg.qbo_environment)}/v3/company/{REALM}"
        return cls(requests.Session(), lambda: get_access_token(COMPANY, REALM), base,
                   allow_writes=allow_writes, max_rps=max_rps,
                   token_refresh=lambda: refresh_access_token(COMPANY, REALM)["access_token"])

    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def call(self, method, path, *, params=None, body=None, requestid=None):
        """Return the final response. 429/5xx/network are retried; for writes the
        caller must pass a ``requestid`` so a retry cannot double-post."""
        method = method.upper()
        if method != "GET":
            if not self.allow_writes:
                raise RuntimeError("refusing non-GET QBO request outside --execute")
            if not requestid:
                raise RuntimeError("writes require an Intuit requestid")
        params = dict(params or {}, minorversion=self.minorversion)
        if requestid:
            params["requestid"] = requestid
        if self._token is None:
            self._token = self._token_provider()
        delay, refreshed, last_exc = 2.0, False, None
        for attempt in range(self.retries + 1):
            self._throttle()
            self.requests += 1
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body)
            try:
                resp = self.session.request(method, self.base + path, params=params, headers=headers,
                                            data=data, timeout=120)
            except Exception as exc:  # network / timeout
                last_exc = exc
                if attempt < self.retries:
                    self.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise StopRun(f"{method} {path}: network error after retries ({exc}); outcome unknown, "
                              "re-run to reconcile by Sku/DocNumber") from exc
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._token = self._token_refresh()
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self.retries:
                self.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            return resp
        raise StopRun(f"{method} {path}: retries exhausted ({last_exc})")

    def get_json(self, path, params=None):
        resp = self.call("GET", path, params=params)
        if resp.status_code != 200:
            raise StopRun(f"GET {path} failed {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def query(self, sql) -> dict:
        return self.get_json("/query", {"query": sql}).get("QueryResponse", {})

    def query_all(self, sql, entity, page=1000) -> list:
        out, start = [], 1
        while True:
            rows = self.query(f"{sql} startposition {start} maxresults {page}").get(entity, [])
            rows = rows if isinstance(rows, list) else [rows]
            out.extend(rows)
            if len(rows) < page:
                return out
            start += page

    def post_json(self, path, body, requestid):
        return self.call("POST", path, body=body, requestid=requestid)

    # ---- account balance helpers
    def account(self, account_id) -> dict:
        return self.get_json(f"/account/{account_id}")["Account"]

    def trial_balance_amount(self, account_id, as_of) -> Decimal:
        """Debit-positive balance of one account from the TrialBalance report as of a date."""
        report = self.get_json("/reports/TrialBalance", {"start_date": as_of, "end_date": as_of,
                                                         "accounting_method": "Accrual"})
        found = []

        def walk(rows):
            for row in (rows or {}).get("Row", []) or []:
                cols = row.get("ColData")
                if cols and str(cols[0].get("id", "")) == str(account_id):
                    found.append(cols)
                walk(row.get("Rows"))

        walk(report.get("Rows"))
        if not found:
            return Decimal(0)
        cols = found[0]
        debit = D(cols[1].get("value"), Decimal(0)) if len(cols) > 1 else Decimal(0)
        credit = D(cols[2].get("value"), Decimal(0)) if len(cols) > 2 else Decimal(0)
        return debit - credit


def ia_snapshot(client: QBOClient, as_of: str) -> dict:
    acct = client.account(ASSET_ID)
    tb = client.trial_balance_amount(ASSET_ID, as_of)
    current = D(acct.get("CurrentBalance"), Decimal(0))
    return {"account_id": ASSET_ID, "name": acct.get("Name"), "current_balance": str(current),
            "trial_balance_as_of": as_of, "trial_balance": str(tb), "read_at": now_iso(),
            "agree": abs(current - tb) <= Decimal("0.01")}


# ---------------------------------------------------------------- inputs → drafts
def mult_of(product: dict) -> Decimal:
    v = product.get("VolumeOfSale")
    return Decimal(int(v)) if v not in (None, 0, "") else Decimal(1)


def load_catalogue(path) -> dict:
    data = json.loads(Path(path).read_text())
    by_id = {}
    for p in data:
        pid = canonical_product_id(p.get("Id"))
        if pid in by_id:
            raise StopRun(f"duplicate EPOS Id in catalogue: {pid}")
        by_id[pid] = p
    return by_id


def stock_by_owner(stock_rows: list[dict], catalogue: dict):
    """Join stock rows to tracked catalogue products by exact normalized name
    (the StockReport has no ProductID; same rule as the canonical build)."""
    names = defaultdict(list)
    for p in catalogue.values():
        names[norm_name(p.get("Name"))].append(p)
    owned, unassigned = defaultdict(list), []
    for r in stock_rows:
        name = clean(r.get("Name"))
        if not name or name == "Total:":
            continue
        tracked = [p for p in names.get(norm_name(name), []) if p.get("IsStockTracked")]
        if len(tracked) == 1:
            owned[canonical_product_id(tracked[0]["Id"])].append(r)
        else:
            reason = "AMBIGUOUS_TRACKED_NAME" if tracked else (
                "UNTRACKED_PRODUCT" if names.get(norm_name(name)) else "NOT_IN_CATALOGUE")
            unassigned.append({"Name": name, "reason": reason, "TotalStock": r.get("TotalStock"),
                               "TotalCost": r.get("TotalCost")})
    return owned, unassigned


def owner_count(rows: list[dict], m: Decimal, has_vos: bool):
    """Return (full, loose, full_multiplier, loose_multiplier, flags)."""
    flags = []
    if not rows:
        return Decimal(0), Decimal(0), m, Decimal(1), ["NO_STOCK_ROW"]
    if len(rows) > 1:
        flags.append(f"MULTIPLE_STOCK_ROWS({len(rows)})")
    full = sum((D(r.get("MeasuredCurrentStock"), Decimal(0)) for r in rows), Decimal(0))
    loose = sum((D(r.get("CurrentVolume"), Decimal(0)) for r in rows), Decimal(0))
    total = sum((D(r.get("TotalStock"), Decimal(0)) for r in rows), Decimal(0))
    if has_vos:
        if abs(total * m - (full * m + loose)) > Decimal("0.6"):
            flags.append(f"STOCK_VOS_INCONSISTENT(full {full}+loose {loose} vs total {total}x{m})")
        return full, loose, m, Decimal(1), flags
    if loose != 0:
        flags.append(f"LOOSE_STOCK_WITHOUT_VOS({loose})")
    return total, Decimal(0), m, Decimal(1), flags


def mapping_index(mapping_rows: list[dict]):
    by_sku = defaultdict(list)
    for r in mapping_rows:
        if clean(r.get("Review Status")).casefold() != "approved":
            continue
        by_sku[clean(r["Target QBO SKU"])].append(r)
    return by_sku


def build_plan(*, mapping_path, inventory_path, noninventory_path, stock_path, catalogue_path,
               as_of, inv_start_date=INV_START):
    """Return (plan dict, entries list). No HTTP."""
    if inv_start_date != INV_START:
        raise StopRun("InvStartDate must be 2026-10-01 (conversion contract)")
    mapping_rows = read_csv(mapping_path)
    inv_rows, non_rows = read_csv(inventory_path), read_csv(noninventory_path)
    catalogue = load_catalogue(catalogue_path)
    stock_rows = read_csv(stock_path)
    by_sku = mapping_index(mapping_rows)
    blocking: list[str] = []
    approval_base = f"approved_mapping_final.csv sha256 {sha256_file(mapping_path)[:16]}"

    # -- mapping ↔ create list consistency
    list_skus = {}
    for kind, rows in (("Inventory", inv_rows), ("NonInventory", non_rows)):
        for r in rows:
            sku, name = clean(r["Sku"]), clean(r["Name"])
            if clean(r.get("Type")) != kind:
                blocking.append(f"{sku}: create-list Type {r.get('Type')} != {kind}")
            if sku in list_skus:
                blocking.append(f"duplicate create-list Sku {sku}")
            list_skus[sku] = (kind, name)
            if clean(r.get("ParentRef")) or truthy(r.get("SubItem")):
                blocking.append(f"{sku}: ParentRef/SubItem not allowed")
            rows_for = by_sku.get(sku, [])
            if not rows_for:
                blocking.append(f"{sku}: no approved mapping row targets this SKU")
            for m in rows_for:
                if clean(m["Target QBO Name"]) != name or canonical_target_type(m["Target QBO Item Type"]) != kind:
                    blocking.append(f"{sku}: mapping row {m.get('Row ID')} name/type differs from create list")
            reason = october_target_error(kind, sku, "", name)
            if reason:
                blocking.append(f"{sku}: {reason}")
    for sku in by_sku:
        if sku not in list_skus:
            blocking.append(f"mapping target {sku} has no create-list row")

    # -- Inventory families + counts
    owned, unassigned = stock_by_owner(stock_rows, catalogue)
    families, counts, extra = [], [], {}
    for r in inv_rows:
        sku, owner = clean(r["Sku"]), canonical_product_id(r["Owner EPOS Product ID"])
        p = catalogue.get(owner)
        if p is None:
            blocking.append(f"{sku}: owner {owner} not in catalogue")
            continue
        if not p.get("IsStockTracked"):
            blocking.append(f"{sku}: owner {owner} is no longer stock-tracked in EPOS")
        m = mult_of(p)
        list_m = D(r.get("Owner multiplier"))
        if list_m != m:
            blocking.append(f"{sku}: owner VolumeOfSale changed ({list_m} approved, {m} now) - re-approve unit")
        owner_map = [x for x in by_sku.get(sku, []) if canonical_product_id(x.get("EPOS Product ID")) == owner]
        if len(owner_map) != 1:
            blocking.append(f"{sku}: owner {owner} must have exactly one mapping row to its family")
            continue
        om = owner_map[0]
        cost_ex = D(p.get("CostPriceExTax"), Decimal(0))
        unit_cost = q(cost_ex / m, "0.00001") if cost_ex > 0 else Decimal(0)
        unit_price = q(D(p.get("SalePriceIncTax"), Decimal(0)) / m, "0.01")
        tax_group = p.get("CostPriceTaxGroupName") or ""
        basis = "exempt" if tax_group == "NoTax" else "exclusive"
        full, loose, fm, lm, cflags = owner_count(owned.get(owner, []), m, bool(p.get("VolumeOfSale")))
        flags = list(cflags)
        if cost_ex <= 0:
            flags.append("ZERO_COST")
        if D(r.get("PurchaseCost")) is not None and abs(D(r["PurchaseCost"]) - unit_cost) > Decimal("0.00001"):
            flags.append(f"COST_CHANGED_SINCE_LIST({r['PurchaseCost']}->{unit_cost})")
        if D(r.get("UnitPrice")) is not None and abs(D(r["UnitPrice"]) - unit_price) > Decimal("0.005"):
            flags.append(f"PRICE_CHANGED_SINCE_LIST({r['UnitPrice']}->{unit_price})")
        for code in (clean(r.get("SalesTaxCodeId")), clean(r.get("PurchaseTaxCodeId"))):
            if code not in TAX_CODE_IDS:
                blocking.append(f"{sku}: unexpected tax code {code}")
        if clean(r.get("AssetAccountId")) != ASSET_ID:
            blocking.append(f"{sku}: AssetAccountId must be 77")
        if clean(r.get("IncomeAccountId")) not in INCOME_IDS or clean(r.get("ExpenseAccountId (COGS)")) not in EXPENSE_IDS:
            blocking.append(f"{sku}: income/COGS account not in the approved category set")
        families.append({
            "family_key": clean(om.get("Canonical Family Key")) or sku, "name": clean(r["Name"]), "sku": sku,
            "canonical_unit": clean(r.get("Canonical Unit")) or clean(om.get("Canonical Unit")),
            "stock_owner_id": owner, "approved_by": clean(om.get("Approved By")),
            "approval_ref": f"{clean(r.get('Approval'))}; {approval_base}",
            "unit_evidence": f"EPOS catalogue owner {owner} VolumeOfSale {p.get('VolumeOfSale')} -> x{m}",
            "cost_evidence": f"catalogue CostPriceExTax {p.get('CostPriceExTax')} / {m} (group {tax_group or 'n/a'})",
            "cost_tax_basis": basis, "income_account_id": clean(r["IncomeAccountId"]),
            "cogs_account_id": clean(r["ExpenseAccountId (COGS)"]),
            "tax_code_id": clean(r["PurchaseTaxCodeId"]), "full_multiplier": str(fm),
            "loose_multiplier": str(lm),
            "purchase_multiplier": clean(om.get("Staff Approved Purchase Multiplier")) or "1",
            "canonical_unit_cost": str(unit_cost),
        })
        counts.append({"stock_owner_id": owner, "full_count": str(full), "loose_count": str(loose),
                       "cutoff_date": FINAL_CUTOFF,
                       "source_ref": f"{Path(stock_path).name} as-of {as_of} ({len(owned.get(owner, []))} row(s))"})
        extra[sku] = {"row": r, "flags": flags, "unit_price": unit_price, "owner": owner,
                      "raw_qty": full * fm + loose * lm}
    inv_out = {"drafts": []}
    if families:
        try:
            inv_out = catalogue_drafts(families, counts, cutoff_date=FINAL_CUTOFF)
        except ValueError as exc:
            blocking.append(f"catalogue_drafts refused: {exc}")

    entries = []
    total_exact, total_2dp, zero_cost_pos, neg_floor = Decimal(0), Decimal(0), 0, 0
    for d in inv_out["drafts"]:
        payload = d["payload"]
        sku = payload["Sku"]
        x = extra[sku]
        r = x["row"]
        payload["InvStartDate"] = inv_start_date
        payload["SalesTaxCodeRef"] = {"value": clean(r["SalesTaxCodeId"])}
        payload["PurchaseTaxCodeRef"] = {"value": clean(r["PurchaseTaxCodeId"])}
        payload["UnitPrice"] = float(x["unit_price"])
        payload["SalesTaxIncluded"] = True
        payload["Taxable"] = truthy(r.get("Taxable"))
        if clean(r.get("Description")):
            payload["Description"] = clean(r["Description"])[:4000]
        qty, cost = Decimal(d["quantity"]), Decimal(d["unit_cost"])
        value_2dp = q(qty * cost, "0.01")
        total_exact += qty * cost
        total_2dp += value_2dp
        flags = list(dict.fromkeys(d["flags"] + x["flags"]))
        if "MISSING_COST_POSITIVE_STOCK" in flags:
            zero_cost_pos += 1
        if "NEGATIVE_ZERO_FLOOR" in flags:
            neg_floor += 1
        epos_ids = sorted({canonical_product_id(m.get("EPOS Product ID")) for m in by_sku.get(sku, [])})
        entries.append({"kind": "Inventory", "family_key": d["family_key"], "sku": sku, "name": payload["Name"],
                        "epos_product_ids": epos_ids, "owner_epos_id": x["owner"], "raw_qty": str(x["raw_qty"]),
                        "quantity": d["quantity"], "unit_cost": d["unit_cost"], "value": str(value_2dp),
                        "flags": flags, "source_ref": d["source_ref"], "payload": payload})

    # -- NonInventory
    products, non_extra = [], {}
    for r in non_rows:
        sku, pid = clean(r["Sku"]), canonical_product_id(r["EPOS Product ID"])
        p = catalogue.get(pid)
        flags = []
        if p is None:
            blocking.append(f"{sku}: EPOS product {pid} not in catalogue")
            continue
        rows_for = by_sku.get(sku, [])
        category = clean(r.get("EPOS Category")) or clean(p.get("CategoryName"))
        if not category or category.upper() not in NONSTOCK_CATEGORY_BAND:
            flags.append("CATEGORY_FALLBACK_NON_FOOD(accountant to confirm)")
            category = NONFOOD_FALLBACK_CATEGORY
        products.append({"epos_product_id": pid, "name": clean(r["Name"]), "category": category,
                         "approved_by": clean(rows_for[0].get("Approved By")) if rows_for else "",
                         "approval_ref": approval_base, "stock_tracked": bool(p.get("IsStockTracked")),
                         "tax_code_id": clean(r["SalesTaxCodeId"])})
        cost = q(D(p.get("CostPriceExTax"), Decimal(0)), "0.00001")
        price = q(D(p.get("SalePriceIncTax"), Decimal(0)), "0.01")
        if cost <= 0:
            flags.append("ZERO_COST")
        non_extra[sku] = {"row": r, "flags": flags, "cost": cost, "price": price, "pid": pid}
    try:
        non_out = noninventory_drafts(products)
    except ValueError as exc:
        blocking.append(f"noninventory_drafts refused: {exc}")
        non_out = {"drafts": []}
    for d in non_out["drafts"]:
        payload = d["payload"]
        sku = payload["Sku"]
        x = non_extra[sku]
        r = x["row"]
        if payload["IncomeAccountRef"]["value"] != clean(r["IncomeAccountId"]) or \
                payload["ExpenseAccountRef"]["value"] != clean(r["ExpenseAccountId (purchase)"]):
            blocking.append(f"{sku}: category accounts differ from the approved create list")
        for code in (clean(r.get("SalesTaxCodeId")), clean(r.get("PurchaseTaxCodeId"))):
            if code not in TAX_CODE_IDS:
                blocking.append(f"{sku}: unexpected tax code {code}")
        payload.update(SalesTaxCodeRef={"value": clean(r["SalesTaxCodeId"])},
                       PurchaseTaxCodeRef={"value": clean(r["PurchaseTaxCodeId"])},
                       Taxable=truthy(r.get("Taxable")), SalesTaxIncluded=True, PurchaseTaxIncluded=False,
                       UnitPrice=float(x["price"]), PurchaseCost=float(x["cost"]))
        if clean(r.get("Description")):
            payload["Description"] = clean(r["Description"])[:4000]
        epos_ids = sorted({canonical_product_id(m.get("EPOS Product ID")) for m in by_sku.get(sku, [])})
        entries.append({"kind": "NonInventory", "family_key": sku, "sku": sku, "name": payload["Name"],
                        "epos_product_ids": epos_ids, "owner_epos_id": x["pid"], "raw_qty": "", "quantity": "",
                        "unit_cost": str(x["cost"]), "value": "0.00", "flags": x["flags"], "source_ref": "",
                        "payload": payload})

    # -- batch-wide uniqueness (Inventory and NonInventory together)
    seen_names, seen_skus = {}, {}
    for e in entries:
        for key, store in ((e["name"].casefold(), seen_names), (e["sku"].casefold(), seen_skus)):
            if key in store:
                blocking.append(f"duplicate name/SKU in batch: {key}")
            store[key] = e["sku"]
        if len(e["name"]) > 100 or ":" in e["name"] or e["name"] != " ".join(e["name"].split()):
            blocking.append(f"{e['sku']}: invalid QBO name {e['name']!r}")
    for i, e in enumerate(entries, start=1):
        e["seq"] = i
        e["payload_sha256"] = sha256_text(canonical_json(e["payload"]))

    rehearsal = as_of != FINAL_CUTOFF or "2026_09_30" not in Path(stock_path).name
    plan = {
        "realm": REALM, "as_of": as_of, "rehearsal": rehearsal,
        "rehearsal_note": ("counts are NOT the final 30 Sep count; --execute refused" if rehearsal else ""),
        "inv_start_date": inv_start_date,
        "inputs": {k: {"path": str(v), "sha256": sha256_file(v)} for k, v in (
            ("mapping", mapping_path), ("inventory_list", inventory_path),
            ("noninventory_list", noninventory_path), ("stock_report", stock_path),
            ("catalogue", catalogue_path))},
        "counts": {"inventory": sum(e["kind"] == "Inventory" for e in entries),
                   "noninventory": sum(e["kind"] == "NonInventory" for e in entries),
                   "total": len(entries),
                   "inventory_qty_positive": sum(1 for e in entries if e["kind"] == "Inventory" and Decimal(e["quantity"]) > 0),
                   "negative_zero_floor": neg_floor, "missing_cost_positive_stock": zero_cost_pos,
                   "zero_cost_items": sum(1 for e in entries if "ZERO_COST" in e["flags"]),
                   "stock_rows_unassigned": len(unassigned)},
        "V_opening_value_ex_tax_exact": str(total_exact),
        "V_opening_value_ex_tax_item_rounded_2dp": str(total_2dp),
        "V_basis": "sum over Inventory of zero-floor canonical qty x (owner CostPriceExTax / owner multiplier, 5 dp)",
        "stock_rows_unassigned_value_floor": str(sum((max(D(u.get("TotalCost"), Decimal(0)), Decimal(0)) for u in unassigned), Decimal(0))),
        "blocking": blocking,
    }
    return plan, entries, unassigned


# ---------------------------------------------------------------- preflight
def verify_matches(item: dict, payload: dict, item_id=None) -> list[str]:
    """Return mismatches between a live QBO item and the approved payload."""
    problems = []
    expected = {"Name": payload["Name"], "Sku": payload["Sku"], "Type": payload["Type"]}
    if item_id:
        expected["Id"] = str(item_id)
    if payload["Type"] == "Inventory":
        expected["InvStartDate"] = payload["InvStartDate"]
    try:
        validate_live_item(item, expected)
    except ValueError as exc:
        problems.append(str(exc))
    if item.get("ParentRef") or item.get("SubItem") is True:
        problems.append("item has a ParentRef / SubItem")
    for ref in ("IncomeAccountRef", "ExpenseAccountRef", "AssetAccountRef", "SalesTaxCodeRef", "PurchaseTaxCodeRef"):
        if ref in payload and str((item.get(ref) or {}).get("value", "")) != str(payload[ref]["value"]):
            problems.append(f"{ref} {(item.get(ref) or {}).get('value')} != {payload[ref]['value']}")
    for flag in ("TrackQtyOnHand", "Taxable", "SalesTaxIncluded", "PurchaseTaxIncluded"):
        if flag in payload and bool(item.get(flag)) != bool(payload[flag]):
            problems.append(f"{flag} {item.get(flag)} != {payload[flag]}")
    for num, tol in (("QtyOnHand", Decimal("0.000001")), ("PurchaseCost", Decimal("0.00001")),
                     ("UnitPrice", Decimal("0.005"))):
        if num in payload:
            got = D(item.get(num), Decimal(0))
            if abs(got - Decimal(str(payload[num]))) > tol:
                problems.append(f"{num} {got} != {payload[num]}")
    return problems


def preflight(client: QBOClient, entries: list[dict], *, own_ids=frozenset()) -> dict:
    """Read-only checks. ``own_ids``: QBO Ids already created/adopted by this tool."""
    items = client.query_all("select Id, Name, FullyQualifiedName, Sku, Type, Active, ParentRef from Item "
                             "where Active in (true,false)", "Item")
    by_fqn, by_leaf, by_sku = defaultdict(list), defaultdict(list), defaultdict(list)
    for it in items:
        by_fqn[norm_name(it.get("FullyQualifiedName") or it.get("Name"))].append(it)
        by_leaf[norm_name(it.get("Name"))].append(it)
        if clean(it.get("Sku")):
            by_sku[norm_name(it.get("Sku"))].append(it)
    collisions, resumable = [], []
    for e in entries:
        name, sku = norm_name(e["name"]), norm_name(e["sku"])
        hits = {}
        for kind, idx, key in (("FQN", by_fqn, name), ("LEAF", by_leaf, name), ("SKU", by_sku, sku)):
            for it in idx.get(key, []):
                hits.setdefault(it["Id"], (it, set()))[1].add(kind)
        for item_id, (it, kinds) in hits.items():
            if item_id in own_ids:
                continue
            same = (norm_name(it.get("Name")) == name and norm_name(it.get("Sku")) == sku
                    and it.get("Type") == e["kind"] and not it.get("ParentRef"))
            if same:
                resumable.append({"Sku": e["sku"], "QBO Id": item_id})
                continue
            if "FQN" in kinds:
                kind = "FQN_HARD" if it.get("Active") else "FQN_INACTIVE"
            elif "SKU" in kinds:
                kind = "SKU"
            else:
                if not it.get("Active"):
                    continue  # inactive leaf names are "(deleted)"-suffixed; no lookup ambiguity
                kind = "LEAF_SOFT"
            collisions.append({"New Sku": e["sku"], "New Name": e["name"], "New Type": e["kind"],
                               "Collision": kind, "Live Id": item_id, "Live Type": it.get("Type"),
                               "Live Active": it.get("Active"), "Live Name": it.get("Name"),
                               "Live FullyQualifiedName": it.get("FullyQualifiedName"), "Live Sku": it.get("Sku")})
    # accounts + tax codes
    acct_ids = sorted({ref["value"] for e in entries for k, ref in e["payload"].items()
                       if k in ("IncomeAccountRef", "ExpenseAccountRef", "AssetAccountRef")})
    account_problems, accounts = [], {}
    for aid in acct_ids:
        try:
            a = client.account(aid)
        except StopRun as exc:
            account_problems.append(f"account {aid}: {exc}")
            continue
        accounts[aid] = {"Name": a.get("FullyQualifiedName") or a.get("Name"), "AccountType": a.get("AccountType"),
                         "AccountSubType": a.get("AccountSubType"), "Active": a.get("Active")}
        if a.get("Active") is not True:
            account_problems.append(f"account {aid} inactive")
        want = ("Other Current Asset", "Inventory") if aid == ASSET_ID else (
            ("Income", None) if aid in INCOME_IDS else (("Cost of Goods Sold", None) if aid in EXPENSE_IDS else None))
        if want is None:
            account_problems.append(f"account {aid} is not in the approved account set")
        elif a.get("AccountType") != want[0] or (want[1] and a.get("AccountSubType") != want[1]):
            account_problems.append(f"account {aid} type {a.get('AccountType')}/{a.get('AccountSubType')} != {want}")
    tax_ids = sorted({e["payload"][k]["value"] for e in entries for k in ("SalesTaxCodeRef", "PurchaseTaxCodeRef")
                      if k in e["payload"]})
    tax_problems = []
    if tax_ids:
        got = client.query("select * from TaxCode where Id in (" + ",".join(f"'{t}'" for t in tax_ids) + ")").get("TaxCode", [])
        got = {t["Id"]: t for t in (got if isinstance(got, list) else [got])}
        for t in tax_ids:
            if t not in got or got[t].get("Active") is not True:
                tax_problems.append(f"tax code {t} missing or inactive")
    kinds = defaultdict(int)
    for c in collisions:
        kinds[c["Collision"]] += 1
    return {"items_read": len(items), "collisions": collisions, "collision_counts": dict(kinds),
            "resumable_existing": resumable, "accounts": accounts, "account_problems": account_problems,
            "tax_problems": tax_problems}


# ---------------------------------------------------------------- outputs
def write_payloads(out: Path, entries: list[dict]) -> str:
    lines = [canonical_json({k: e[k] for k in ("seq", "kind", "family_key", "sku", "name", "epos_product_ids",
                                                "quantity", "unit_cost", "value", "flags", "payload",
                                                "payload_sha256")}) for e in entries]
    text = "\n".join(lines) + "\n"
    (out / "payloads.jsonl").write_text(text, encoding="utf-8")
    return sha256_text(text)


def write_csv(path: Path, rows: list[dict], cols: list[str]):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_results(path: Path) -> list[dict]:
    return read_csv(path) if path.exists() else []


def append_result(path: Path, row: dict):
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


def write_register(out: Path, entries: list[dict], results: list[dict]):
    done = {r["Sku"]: r for r in results if r["status"] in DONE}
    rows = []
    for e in entries:
        r = done.get(e["sku"])
        if not r:
            continue
        rows.append({"Family Key": e["family_key"], "Kind": e["kind"], "Target QBO SKU": e["sku"],
                     "Target QBO Name": e["name"], "Target QBO Item Type": e["kind"],
                     "EPOS Product IDs": " ".join(e["epos_product_ids"]), "QBO Item Id": r["QBO Id"],
                     "SyncToken": r["SyncToken"], "QtyOnHand": e["quantity"], "PurchaseCost": e["unit_cost"],
                     "Opening value": e["value"], "Status": r["status"]})
    write_csv(out / "register.csv", rows, REGISTER_COLS)
    return rows


# ---------------------------------------------------------------- execute
def find_existing(client: QBOClient, payload: dict) -> list[dict]:
    found = {}
    for field in ("Sku", "Name"):
        rows = client.query(f"select * from Item where {field} = '{qbo_escape(payload[field])}' "
                            "and Active in (true,false)").get("Item", [])
        for it in (rows if isinstance(rows, list) else [rows]):
            # A legacy sub-item that only shares the leaf Name does not block a
            # top-level create (preflight reports it as LEAF_SOFT).
            fqn = norm_name(it.get("FullyQualifiedName") or it.get("Name"))
            if field == "Sku" or fqn == norm_name(payload["Name"]):
                found[it["Id"]] = it
    return list(found.values())


def create_one(client: QBOClient, entry: dict) -> tuple[str, dict]:
    """Create (or adopt) one item. Returns (status, live item). Raises StopRun."""
    payload = entry["payload"]
    requestid = entry["payload_sha256"][:36]
    existing = find_existing(client, payload)
    if existing:
        if len(existing) == 1 and not verify_matches(existing[0], payload):
            return "ADOPTED", existing[0]
        details = "; ".join(f"Id {it['Id']} {it.get('Name')!r} Sku {it.get('Sku')!r}: "
                            + (", ".join(verify_matches(it, payload)) or "duplicate") for it in existing)
        raise StopRun(f"{entry['sku']}: existing QBO item(s) do not match the approved payload: {details}")
    resp = client.post_json("/item", payload, requestid)
    if resp.status_code != 200:
        # 4xx: QBO rejected. 5xx after retries: unknown -> re-query before deciding.
        again = find_existing(client, payload)
        if again and len(again) == 1 and not verify_matches(again[0], payload):
            return "CREATED", again[0]
        raise StopRun(f"{entry['sku']}: create failed {resp.status_code}: {resp.text[:500]}")
    created = resp.json().get("Item") or {}
    if not created.get("Id"):
        raise StopRun(f"{entry['sku']}: 200 without an Item Id; re-run to reconcile by Sku")
    return "CREATED", created


def execute(client: QBOClient, entries: list[dict], out: Path, *, approval_ref: str, limit: int | None,
            as_of_tb: str) -> dict:
    results_path = out / "results.csv"
    results = load_results(results_path)
    done = {r["Sku"] for r in results if r["status"] in DONE}
    ia_before_path = out / "ia_before.json"
    if not ia_before_path.exists():
        snap = ia_snapshot(client, as_of_tb)
        snap["note"] = "B: IA 77 before the first W7 create (pinned; never overwritten)"
        ia_before_path.write_text(json.dumps(snap, indent=2) + "\n")
    pending = [e for e in entries if e["sku"] not in done]
    if limit is not None:
        pending = pending[:limit]
    processed, stop = [], None
    for e in pending:
        try:
            status, item = create_one(client, e)
            live = client.get_json(f"/item/{item['Id']}")["Item"]
            problems = verify_matches(live, e["payload"], item_id=item["Id"])
        except StopRun as exc:
            append_result(results_path, {"ts": now_iso(), "seq": e["seq"], "kind": e["kind"], "Sku": e["sku"],
                                          "Name": e["name"], "status": "STOPPED", "payload_sha256": e["payload_sha256"],
                                          "requestid": e["payload_sha256"][:36], "approval_ref": approval_ref,
                                          "detail": str(exc)[:1000]})
            stop = str(exc)
            break
        row = {"ts": now_iso(), "seq": e["seq"], "kind": e["kind"], "Sku": e["sku"], "Name": e["name"],
               "status": status if not problems else "VERIFY_FAILED", "QBO Id": live.get("Id"),
               "SyncToken": live.get("SyncToken"), "payload_sha256": e["payload_sha256"],
               "requestid": e["payload_sha256"][:36], "approval_ref": approval_ref, "detail": "; ".join(problems)}
        append_result(results_path, row)
        processed.append(row)
        if problems:
            stop = f"{e['sku']} (Id {live.get('Id')}) failed verification: {'; '.join(problems)}"
            break
    results = load_results(results_path)
    register = write_register(out, entries, results)
    ia_after = ia_snapshot(client, as_of_tb)
    ia_before = json.loads(ia_before_path.read_text())
    expected_c = sum((Decimal(r["Opening value"]) for r in register if r["Kind"] == "Inventory"), Decimal(0))
    b = Decimal(ia_before["trial_balance"])
    after = Decimal(ia_after["trial_balance"])
    summary = {"realm": REALM, "approval_ref": approval_ref, "run_at": now_iso(),
               "processed_this_run": len(processed), "registered_total": len(register),
               "pending_after_run": len([e for e in entries if e["sku"] not in {r["Target QBO SKU"] for r in register}]),
               "stopped": stop, "ia_before": ia_before, "ia_after": ia_after,
               "B": str(b), "B_plus_C": str(after), "C_actual": str(after - b),
               "C_expected_registered_items": str(expected_c),
               "C_difference": str((after - b) - expected_c),
               "requests": client.requests}
    (out / "summary_execute.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# ---------------------------------------------------------------- fill-ids
def fill_ids(mapping_path, register_path, out_csv) -> dict:
    register = read_csv(register_path)
    by_sku = {}
    for r in register:
        sku = clean(r["Target QBO SKU"])
        if sku in by_sku or not clean(r["QBO Item Id"]).isdigit():
            raise StopRun(f"register row for {sku} is duplicate or has no numeric Id")
        by_sku[sku] = r
    with open(mapping_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)
    if "Target QBO Item Id" not in fields:
        raise StopRun("mapping has no 'Target QBO Item Id' column")
    missing = []
    for row in rows:
        if clean(row.get("Review Status")).casefold() != "approved":
            continue
        reg = by_sku.get(clean(row["Target QBO SKU"]))
        if reg is None:
            missing.append(clean(row["Target QBO SKU"]))
            continue
        if clean(reg["Target QBO Name"]) != clean(row["Target QBO Name"]):
            raise StopRun(f"{row['Target QBO SKU']}: register name differs from mapping")
        prior = clean(row.get("Target QBO Item Id"))
        if prior and prior != clean(reg["QBO Item Id"]):
            raise StopRun(f"{row['Target QBO SKU']}: mapping already has Id {prior}, register says {reg['QBO Item Id']}")
        row["Target QBO Item Id"] = clean(reg["QBO Item Id"])
    if missing:
        raise StopRun(f"{len(missing)} approved mapping rows have no register Id (first: {sorted(set(missing))[:5]}); "
                      "finish the creates first")
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    registry = ProductConversionRegistry.from_csv(out_csv)
    reg_ids = {clean(r["QBO Item Id"]): r for r in register}
    for rule in registry.rules:
        reg = reg_ids.get(rule.target_qbo_item_id)
        if reg is None:
            raise StopRun(f"row {rule.row_id}: Id {rule.target_qbo_item_id} not in register")
        if (clean(reg["Target QBO SKU"]), clean(reg["Target QBO Item Type"])) != (rule.target_qbo_sku, rule.target_qbo_type):
            raise StopRun(f"row {rule.row_id}: register SKU/type differ for Id {rule.target_qbo_item_id}")
        reason = october_target_error(rule.target_qbo_type, rule.target_qbo_sku, rule.target_qbo_item_id,
                                      rule.target_qbo_name)
        if reason or rule.effective_date.isoformat() != INV_START:
            raise StopRun(f"row {rule.row_id}: {reason or 'Effective Date must be 2026-10-01'}")
    return {"out": str(out_csv), "sha256": registry.source_sha256, "rules": len(registry.rules),
            "distinct_target_ids": len(registry.approved_target_item_ids),
            "register_rows": len(register)}


# ---------------------------------------------------------------- CLI
def cmd_create(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    plan, entries, unassigned = build_plan(mapping_path=a.mapping, inventory_path=a.inventory_list,
                                           noninventory_path=a.noninventory_list, stock_path=a.stock_report,
                                           catalogue_path=a.catalogue, as_of=a.as_of,
                                           inv_start_date=a.inv_start_date)
    payloads_sha = write_payloads(out, entries)
    write_csv(out / "flags.csv", [{"seq": e["seq"], "kind": e["kind"], "sku": e["sku"], "name": e["name"],
                                   "raw_qty": e["raw_qty"], "quantity": e["quantity"], "unit_cost": e["unit_cost"],
                                   "value": e["value"], "flags": "; ".join(e["flags"])}
                                  for e in entries if e["flags"]],
              ["seq", "kind", "sku", "name", "raw_qty", "quantity", "unit_cost", "value", "flags"])
    write_csv(out / "stock_rows_unassigned.csv", unassigned, ["Name", "reason", "TotalStock", "TotalCost"])
    plan["payloads_sha256"] = payloads_sha
    refuse = list(plan["blocking"])
    client = None
    if not a.no_live:
        client = QBOClient.for_company_a(allow_writes=a.execute)
        results = load_results(out / "results.csv")
        own = frozenset(r["QBO Id"] for r in results if r["status"] in DONE)
        pf = preflight(client, entries, own_ids=own)
        write_csv(out / "preflight_collisions.csv", pf["collisions"],
                  ["New Sku", "New Name", "New Type", "Collision", "Live Id", "Live Type", "Live Active",
                   "Live Name", "Live FullyQualifiedName", "Live Sku"])
        plan["preflight"] = {k: v for k, v in pf.items() if k != "collisions"}
        plan["preflight"]["resumable_existing"] = len(pf["resumable_existing"])
        plan["ia_77_now"] = ia_snapshot(client, a.inv_start_date)
        hard = {k: v for k, v in pf["collision_counts"].items() if k != "LEAF_SOFT" or not a.allow_leaf_collisions}
        if any(hard.values()):
            refuse.append(f"collisions with live items: {hard} (see preflight_collisions.csv; run W5 renames first)")
        refuse += pf["account_problems"] + pf["tax_problems"]
    else:
        plan["preflight"] = "SKIPPED (--no-live); --execute requires the live preflight"
    plan["refused"] = refuse
    plan["mode"] = "execute" if a.execute else "dry-run"
    (out / "summary.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps({k: plan[k] for k in ("counts", "V_opening_value_ex_tax_exact",
                                            "V_opening_value_ex_tax_item_rounded_2dp", "rehearsal",
                                            "payloads_sha256")}, indent=2))
    if isinstance(plan["preflight"], dict):
        print("preflight collisions:", plan["preflight"]["collision_counts"],
              "accounts problems:", plan["preflight"]["account_problems"],
              "IA 77 now:", plan["ia_77_now"]["trial_balance"])
    for r in refuse[:20]:
        print("REFUSE:", r)
    if not a.execute:
        print(f"dry-run only; outputs in {out}")
        return 1 if refuse else 0
    # ---- execute gates
    if a.no_live:
        raise SystemExit("--execute requires the live preflight (drop --no-live)")
    if plan["rehearsal"]:
        raise SystemExit("--execute refused: counts are a rehearsal (need --as-of 2026-09-30 and a 2026_09_30 stock report)")
    if not clean(a.approval_ref):
        raise SystemExit("--execute requires --approval-ref (the chat yes reference)")
    if a.expect_payloads_sha != payloads_sha:
        raise SystemExit(f"--expect-payloads-sha mismatch: built {payloads_sha}; re-review the dry-run")
    if refuse:
        raise SystemExit("--execute refused: " + "; ".join(refuse[:5]))
    summary = execute(client, entries, out, approval_ref=a.approval_ref, limit=a.test, as_of_tb=a.inv_start_date)
    print(json.dumps({k: summary[k] for k in ("processed_this_run", "registered_total", "pending_after_run",
                                               "stopped", "B", "B_plus_C", "C_actual",
                                               "C_expected_registered_items", "C_difference")}, indent=2))
    return 2 if summary["stopped"] else 0


def cmd_fill_ids(a) -> int:
    print(json.dumps(fill_ids(a.mapping, a.register, a.out_csv), indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="build payloads, preflight, optionally --execute")
    c.add_argument("--mapping", required=True)
    c.add_argument("--inventory-list", required=True)
    c.add_argument("--noninventory-list", required=True)
    c.add_argument("--stock-report", required=True, help="30 Sep EPOS StockReport CSV")
    c.add_argument("--catalogue", required=True, help="catalogue_products.json (EPOS, same night)")
    c.add_argument("--as-of", required=True, help="date of the stock report, e.g. 2026-09-30")
    c.add_argument("--inv-start-date", default=INV_START)
    c.add_argument("--out", required=True)
    g = c.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="the default")
    g.add_argument("--execute", action="store_true", help="POST creates (needs chat yes)")
    c.add_argument("--test", type=int, default=None, metavar="N", help="with --execute: only the first N pending")
    c.add_argument("--approval-ref", default="")
    c.add_argument("--expect-payloads-sha", default="")
    c.add_argument("--no-live", action="store_true", help="offline build; skip the read-only preflight")
    c.add_argument("--allow-leaf-collisions", action="store_true",
                   help="do not refuse LEAF_SOFT collisions (legacy sub-item with the same leaf name); "
                        "only with explicit owner approval")
    f = sub.add_parser("fill-ids", help="fill Target QBO Item Id from register.csv")
    f.add_argument("--mapping", required=True)
    f.add_argument("--register", required=True)
    f.add_argument("--out-csv", required=True, help="e.g. .../approved_mapping_with_ids.csv")
    a = ap.parse_args(argv)
    try:
        return cmd_create(a) if a.cmd == "create" else cmd_fill_ids(a)
    except StopRun as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Daily stock snapshot for AKPONORA / NORA (company_a): EPOS stock vs QBO QtyOnHand. READ-ONLY.

    python -m code_scripts.akponora_ops.stock_snapshot run [--no-epos] [--no-qbo]
        [--stock-report CSV] [--catalogue PATH] [--tolerance 0.001] [--out DIR] [--slack]

Writes ``STATE_ROOT/ops/company_a/stock_snapshot/latest.json`` (atomically) plus a dated copy
``history/stock_snapshot_<YYYY-MM-DD>.json`` (the last run of that Lagos day). The portal's
Products & Stock page reads ``latest.json``; the contract is in
``docs/CODEX_BRIEF_PORTAL_REDESIGN.md`` ("Stock snapshot contract").

Inputs
* EPOS stock: the StockReport CSV (``code_scripts/epos_stocklevels_playwright.py``, VIEW-ONLY download),
  or ``--stock-report`` for an existing CSV. The report has no ProductID: rows are joined to the
  stock-tracked catalogue product by exact normalized name (``w7_create_items.stock_by_owner``,
  the rule used for the opening counts). Canonical qty for a family = its EPOS master's count in
  canonical units (``w7_create_items.owner_count``: full x VolumeOfSale + loose; TotalStock when
  the master has no VolumeOfSale). Pack children are never added: only the master's row counts.
* Catalogue: ``--catalogue`` (catalogue_products.json / .csv / captures) or the catalogue_sync
  baseline ``STATE_ROOT/ops/company_a/catalogue_sync/catalogue_snapshot.json``.
* QBO: ``select * from Item`` (active + inactive), GET only, through ``code_scripts.token_manager``.
* Mapping: the installed approved mapping (``STATE_ROOT/mappings/company_a/approved.csv``).
* Business-day context: last posted sales day (daily_run summaries / archived pipeline metadata),
  and "likely timing" evidence from the last posted day's archived BookKeeping CSV (products sold)
  and the latest bills_sync review lines (POs received recently / bills not yet posted).

``--no-epos`` / ``--no-qbo`` reuse the side cached by the previous run (``epos_side.json`` /
``qbo_side.json`` in the snapshot folder), so the portal can refresh one side cheaply.

Exit 0 = snapshot written, 2 = failed (nothing replaced), 5 = another snapshot is running.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import (
    AKP_NS_SKU_PREFIX, AKP_SKU_PREFIX, COMPANY, dump_json, mapping_file, read_csv, state_dir,
)
from code_scripts.product_conversion import canonical_product_id, canonical_target_type, clean
from code_scripts.scripts.akponora_cutover import w7_create_items as w7
from code_scripts.scripts.akponora_cutover._common import business_date, setup_env

TOOL = "stock_snapshot"
SCHEMA_VERSION = 1
TZ = ZoneInfo("Africa/Lagos")
TOLERANCE_ENV = "OIAT_COMPANY_A_STOCK_TOLERANCE"
DEFAULT_TOLERANCE = Decimal("0.001")
EXIT_OK, EXIT_FAILED, EXIT_BUSY = 0, 2, 5

MATCH, DIFFERENT = "MATCH", "DIFFERENT"
NEGATIVE_QBO, NEGATIVE_EPOS = "NEGATIVE_QBO", "NEGATIVE_EPOS"
NOT_IN_EPOS_REPORT, NOT_TRACKED_IN_EPOS, NO_QBO_ITEM = "NOT_IN_EPOS_REPORT", "NOT_TRACKED_IN_EPOS", "NO_QBO_ITEM"
STATUSES = (MATCH, DIFFERENT, NEGATIVE_QBO, NEGATIVE_EPOS, NOT_IN_EPOS_REPORT, NOT_TRACKED_IN_EPOS, NO_QBO_ITEM)
STOCK_COLUMNS = ("Name", "Barcode", "MeasuredCurrentStock", "CurrentVolume", "TotalStock", "MeasuredCostPrice",
                 "TotalCost")
QBO_FIELDS = ("Id", "Sku", "Name", "QtyOnHand", "Active", "Type")


class SnapshotError(Exception):
    """Fail-closed: the message says what is missing."""


# ---------------------------------------------------------------- small helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def num(value: Decimal | None):
    """JSON number (rounded to 6 dp) or None."""
    if value is None:
        return None
    v = value.quantize(Decimal("0.000001")).normalize()
    return int(v) if v == v.to_integral_value() else float(v)


def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def tolerance_default() -> Decimal:
    return dec(os.getenv(TOLERANCE_ENV, "").strip()) or DEFAULT_TOLERANCE


def snapshot_dir() -> Path:
    return state_dir(TOOL)


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    dump_json(tmp, data)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- catalogue
def normalize_catalogue(data) -> dict[str, dict]:
    """``{pid: product}`` from a product list (catalogue pull) or the catalogue_sync snapshot dict."""
    if isinstance(data, dict):
        items = [{"Id": pid, **(fields or {})} for pid, fields in data.items()]
    else:
        items = list(data or [])
    out: dict[str, dict] = {}
    for p in items:
        pid = canonical_product_id(p.get("Id"))
        if not pid:
            continue
        if pid in out:
            raise SnapshotError(f"duplicate EPOS Id in catalogue: {pid}")
        vos = p.get("VolumeOfSale")
        if isinstance(vos, str):
            d = dec(vos)
            vos = int(d) if d is not None and d == d.to_integral_value() and d > 0 else None
        tracked = p.get("IsStockTracked")
        if isinstance(tracked, str):
            tracked = tracked.strip().lower() in {"true", "yes", "1"}
        out[pid] = {"Id": pid, "Name": clean(p.get("Name")), "IsStockTracked": bool(tracked),
                    "VolumeOfSale": vos or None, "CategoryName": clean(p.get("CategoryName"))}
    return out


def default_catalogue_path() -> Path:
    return state_dir("catalogue_sync") / "catalogue_snapshot.json"


def load_catalogue_file(path: Path) -> dict[str, dict]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return normalize_catalogue(read_csv(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list) and data and isinstance(data[0], dict) and "body" in data[0] and "Id" not in data[0]:
        from code_scripts.scripts.akponora_cutover.epos_catalogue_pull import products_from_captures

        data = products_from_captures(data)
    return normalize_catalogue(data)


# ---------------------------------------------------------------- EPOS side
def download_stock_report(out: Path) -> Path:
    """VIEW-ONLY StockReport CSV download (Playwright) into ``out``."""
    from playwright.sync_api import sync_playwright

    from code_scripts import epos_stocklevels_playwright as stock
    from code_scripts.scripts.akponora_cutover._common import company_config

    with sync_playwright() as pw:
        saved = stock.run(pw, company_config(COMPANY), output_dir=str(out), output_filename="stock_report.csv")
    return Path(saved)


def stock_rows_from_csv(path: Path) -> list[dict]:
    rows = read_csv(path)
    if rows and not {"Name", "TotalStock"} <= set(rows[0]):
        raise SnapshotError(f"{path}: not an EPOS StockReport (needs Name and TotalStock columns)")
    return [{k: r.get(k, "") for k in STOCK_COLUMNS} for r in rows]


def epos_master_counts(stock_rows: list[dict], catalogue: dict[str, dict]) -> tuple[dict, list[dict]]:
    """``{pid: {"qty": Decimal, "flags": [...], "rows": n}}`` for every tracked product with a stock row,
    plus the stock rows that could not be assigned (ambiguous / untracked / not in catalogue)."""
    owned, unassigned = w7.stock_by_owner(stock_rows, catalogue)
    counts = {}
    for pid, rows in owned.items():
        p = catalogue[pid]
        m = w7.mult_of(p)
        full, loose, fm, lm, flags = w7.owner_count(rows, m, bool(p.get("VolumeOfSale")))
        counts[pid] = {"qty": full * fm + loose * lm, "flags": list(flags), "rows": len(rows), "multiplier": m}
    return counts, unassigned


# ---------------------------------------------------------------- QBO side
def fetch_qbo_items(client) -> list[dict]:
    active = client.query_all("select * from Item", "Item")
    inactive = client.query_all("select * from Item where Active = false", "Item")
    seen, out = set(), []
    for item in [*active, *inactive]:
        iid = clean(item.get("Id"))
        if iid and iid not in seen:
            seen.add(iid)
            out.append({k: item.get(k) for k in QBO_FIELDS})
    return out


def relevant_qbo_items(items: list[dict], mapping_rows: list[dict]) -> list[dict]:
    """Keep the mapped items and any AKP- item (the rest of the books is not this page's business)."""
    ids = {clean(r.get("Target QBO Item Id")) for r in mapping_rows} - {""}
    return [i for i in items if clean(i.get("Id")) in ids or clean(i.get("Sku")).startswith(AKP_SKU_PREFIX)]


# ---------------------------------------------------------------- business-day context
def current_business_date(now: datetime | None = None) -> date:
    now = now or datetime.now(TZ)
    local = now.astimezone(TZ).replace(tzinfo=None) if now.tzinfo else now
    return business_date(local)


def _uploaded_dir() -> Path | None:
    try:
        from code_scripts.paths import OPS_UPLOADED_DIR

        return Path(OPS_UPLOADED_DIR)
    except Exception:  # noqa: BLE001
        return None


def _metadata_name() -> str:
    try:
        from code_scripts.scripts.akponora_cutover._common import company_config

        return company_config(COMPANY).metadata_file
    except Exception:  # noqa: BLE001
        return "last_epos_transform.json"


def last_posted_sales_date(daily_root: Path | None, uploaded: Path | None, metadata_name: str) -> str | None:
    """Latest business day whose Company A sales were posted: a daily_run summary with the sales step
    ``ok`` in post mode, or archived pipeline metadata in ``Uploaded/<date>/`` with uploads."""
    found: list[str] = []
    if daily_root and daily_root.is_dir():
        for d in daily_root.iterdir():
            s = read_json(d / "summary.json", {}) or {}
            for step in s.get("steps") or []:
                if (step.get("name") == "sales" and step.get("status") == "ok" and not s.get("dry_run")
                        and (step.get("counts") or {}).get("mode") == "post"):
                    found.append(str(s.get("business_date") or d.name))
    if uploaded and uploaded.is_dir():
        for d in uploaded.iterdir():
            meta = read_json(d / metadata_name, None)
            if not isinstance(meta, dict):
                continue
            stats = meta.get("upload_stats") or {}
            if stats.get("uploaded") or (meta.get("reconcile") or {}).get("status"):
                found.append(str(meta.get("target_date") or meta.get("business_date") or d.name)[:10])
    valid = [f for f in found if _is_date(f)]
    return max(valid) if valid else None


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def business_context(today: date, last_posted: str | None) -> dict:
    unposted = []
    if last_posted:
        d = date.fromisoformat(last_posted) + timedelta(days=1)
        while d <= today and len(unposted) < 31:
            unposted.append(d.isoformat())
            d += timedelta(days=1)
    note = ("EPOS stock is live; QuickBooks has Company A sales only up to "
            f"{last_posted or 'an unknown day'} and only the bills already posted. Sales on "
            f"{', '.join(unposted) if unposted else 'the current business day'} and EPOS deliveries not yet "
            "billed are in EPOS but not in QuickBooks, so differences on items that sold or arrived recently "
            "are expected until those post.")
    return {"current_business_date": today.isoformat(), "last_posted_sales_date": last_posted,
            "unposted_sales_days": unposted, "note": note}


def sold_product_ids(uploaded: Path | None, day: str | None, metadata_name: str) -> tuple[set[str], str | None]:
    """EPOS ProductIds in the archived raw BookKeeping CSV of ``day`` (cheap; empty when not found)."""
    if not uploaded or not day:
        return set(), None
    folder = uploaded / day
    if not folder.is_dir():
        return set(), None
    candidates = []
    meta = read_json(folder / metadata_name, {}) or {}
    raw = clean(meta.get("raw_file")) or Path(clean(meta.get("raw_file_path"))).name
    if raw and (folder / raw).is_file():
        candidates.append(folder / raw)
    candidates += sorted(p for p in folder.glob("*.csv") if p not in candidates
                         and ("BookKeeping" in p.name or "CombinedRaw" in p.name))
    for path in candidates:
        try:
            rows = read_csv(path)
        except (OSError, UnicodeDecodeError):
            continue
        if not rows:
            continue
        col = next((c for c in ("ProductId", "ProductID", "Product ID", "EPOS Product ID") if c in rows[0]), None)
        if col:
            return {canonical_product_id(r.get(col)) for r in rows} - {""}, str(path)
    return set(), None


def recent_receipts(daily_root: Path | None, since: str) -> tuple[dict[str, list[str]], str | None]:
    """``{pid: ["received <date> PO <ref> (<status>)"]}`` from the latest daily_run bills step's
    ``review_lines.csv`` / ``review.csv``: PO lines received on/after ``since`` or still held."""
    if not daily_root or not daily_root.is_dir():
        return {}, None
    for d in sorted((x for x in daily_root.iterdir() if x.is_dir()), reverse=True):
        s = read_json(d / "summary.json", {}) or {}
        bills = next((st for st in s.get("steps") or [] if st.get("name") == "bills" and st.get("out")), None)
        if not bills:
            continue
        out = Path(bills["out"])
        lines_path, review_path = out / "review_lines.csv", out / "review.csv"
        if not lines_path.is_file():
            continue
        po_info = {}
        if review_path.is_file():
            po_info = {clean(r.get("PO")): r for r in read_csv(review_path)}
        found: dict[str, list[str]] = defaultdict(list)
        for ln in read_csv(lines_path):
            po = clean(ln.get("PO"))
            info = po_info.get(po, {})
            status = clean(info.get("Status")) or "?"
            received = clean(info.get("Received Date"))
            if status == "HOLD" or (received and received >= since):
                pid = canonical_product_id(ln.get("EPOS Product ID"))
                if pid:
                    found[pid].append(f"received {received or '?'} on EPOS PO {po} (bill {status})")
        return dict(found), str(lines_path)
    return {}, None


# ---------------------------------------------------------------- build
def mapping_families(mapping_rows: list[dict]) -> dict[str, dict]:
    """One family per Target QBO SKU (falling back to Item Id)."""
    fams: dict[str, dict] = {}
    for r in mapping_rows:
        status = clean(r.get("Review Status")).casefold()
        if status and status != "approved":
            continue
        sku, iid = clean(r.get("Target QBO SKU")), clean(r.get("Target QBO Item Id"))
        key = sku or f"id:{iid}"
        if key == "id:":
            continue
        f = fams.setdefault(key, {"sku": sku, "item_id": iid, "name": clean(r.get("Target QBO Name")),
                                  "type": canonical_target_type(r.get("Target QBO Item Type")),
                                  "unit": clean(r.get("Canonical Unit")),
                                  "family_key": clean(r.get("Canonical Family Key")), "pids": []})
        f["item_id"] = f["item_id"] or iid
        f["unit"] = f["unit"] or clean(r.get("Canonical Unit"))
        pid = canonical_product_id(r.get("EPOS Product ID"))
        if pid and pid not in f["pids"]:
            f["pids"].append(pid)
    return fams


def owner_id(fam: dict) -> str:
    sku = fam["sku"]
    if sku.startswith(AKP_NS_SKU_PREFIX):
        return sku[len(AKP_NS_SKU_PREFIX):]
    if sku.startswith(AKP_SKU_PREFIX):
        return sku[len(AKP_SKU_PREFIX):]
    key = fam.get("family_key") or ""
    return key if key in fam["pids"] else ""


def classify(*, is_inventory: bool, qbo_found: bool, tracked: bool, epos_qty, qbo_qty, tol: Decimal):
    """Status precedence: NO_QBO_ITEM, NOT_TRACKED_IN_EPOS (NonInventory / untracked master),
    NEGATIVE_QBO, NOT_IN_EPOS_REPORT, NEGATIVE_EPOS, then MATCH / DIFFERENT."""
    if not qbo_found:
        return NO_QBO_ITEM
    if not is_inventory:
        return NOT_TRACKED_IN_EPOS
    if qbo_qty is not None and qbo_qty < 0:
        return NEGATIVE_QBO
    if not tracked:
        return NOT_TRACKED_IN_EPOS
    if epos_qty is None:
        return NOT_IN_EPOS_REPORT
    if epos_qty < 0:
        return NEGATIVE_EPOS
    return MATCH if abs(epos_qty - (qbo_qty or Decimal(0))) <= tol else DIFFERENT


def build_snapshot(*, mapping_rows: list[dict], catalogue: dict[str, dict], stock_rows: list[dict],
                   qbo_items: list[dict], tolerance: Decimal = DEFAULT_TOLERANCE, sources: dict | None = None,
                   context: dict | None = None, sold_ids: set[str] | None = None,
                   received: dict[str, list[str]] | None = None, timing_evidence: dict | None = None) -> dict:
    """Pure: no HTTP, no files."""
    counts, unassigned = epos_master_counts(stock_rows, catalogue)
    names = Counter(w7.norm_name(p["Name"]) for p in catalogue.values() if p.get("IsStockTracked"))
    by_id = {clean(i.get("Id")): i for i in qbo_items}
    by_sku = {clean(i.get("Sku")): i for i in qbo_items if clean(i.get("Sku"))}
    sold_ids, received = sold_ids or set(), received or {}
    last_posted = (context or {}).get("last_posted_sales_date")
    fams = mapping_families(mapping_rows)
    rows, mapped_ids = [], set()
    for fam in fams.values():
        mapped_ids.update(fam["pids"])
        item = by_id.get(fam["item_id"]) if fam["item_id"] else None
        item = item or (by_sku.get(fam["sku"]) if fam["sku"] else None)
        qtype = clean((item or {}).get("Type")) or fam["type"]
        is_inv = qtype == "Inventory"
        owner = owner_id(fam)
        p = catalogue.get(owner) if owner else None
        tracked = bool(p and p.get("IsStockTracked"))
        flags: list[str] = []
        c = counts.get(owner) if tracked else None
        epos_qty = c["qty"] if c else None
        if c:
            flags += c["flags"]
        if tracked and not c and names.get(w7.norm_name(p["Name"]), 0) > 1:
            flags.append("AMBIGUOUS_EPOS_NAME")
        if owner and p is None:
            flags.append("MASTER_NOT_IN_CATALOGUE")
        if not is_inv and tracked:
            flags.append("NONINVENTORY_BUT_EPOS_TRACKED")
        qbo_qty = dec(item.get("QtyOnHand")) if item and is_inv else None
        if item and is_inv and qbo_qty is None:
            qbo_qty = Decimal(0)
        if item is not None and item.get("Active") is False:
            flags.append("QBO_ITEM_INACTIVE")
        status = classify(is_inventory=is_inv, qbo_found=item is not None, tracked=tracked,
                          epos_qty=epos_qty, qbo_qty=qbo_qty, tol=tolerance)
        diff = epos_qty - qbo_qty if (epos_qty is not None and qbo_qty is not None) else None
        reasons = []
        if status == DIFFERENT:
            if sold_ids & set(fam["pids"]):
                reasons.append(f"sold on {last_posted} (last posted day); sells regularly, so today's "
                               "unposted sales likely explain part of the difference")
            for pid in fam["pids"]:
                reasons += received.get(pid, [])
        rows.append({
            "family_sku": fam["sku"], "qbo_item_id": clean((item or {}).get("Id")) or fam["item_id"] or None,
            "qbo_name": clean((item or {}).get("Name")) or fam["name"], "type": qtype,
            "canonical_unit": fam["unit"], "epos_master_id": owner or None,
            "epos_master_name": (p or {}).get("Name"), "epos_product_ids": fam["pids"],
            "epos_volume_of_sale": (p or {}).get("VolumeOfSale"),
            "epos_qty_canonical": num(epos_qty), "qbo_qty_on_hand": num(qbo_qty), "difference": num(diff),
            "status": status, "likely_timing": bool(reasons) if status == DIFFERENT else None,
            "timing_reasons": reasons[:5], "flags": flags, "qbo_active": (item or {}).get("Active"),
            "tolerance": num(tolerance),
        })
    rows.sort(key=lambda r: (STATUSES.index(r["status"]), r["family_sku"]))
    stock_by_pid = {pid: c["qty"] for pid, c in counts.items()}
    unmapped = sorted(({"epos_product_id": pid, "name": p["Name"], "tracked": bool(p.get("IsStockTracked")),
                        "category": p.get("CategoryName") or "", "epos_qty": num(stock_by_pid.get(pid))}
                       for pid, p in catalogue.items() if pid not in mapped_ids),
                      key=lambda r: (not r["tracked"], r["name"].casefold()))
    mapped_qbo_ids = {r["qbo_item_id"] for r in rows if r["qbo_item_id"]}
    extra_qbo = [{"qbo_item_id": clean(i.get("Id")), "sku": clean(i.get("Sku")), "name": clean(i.get("Name")),
                  "type": clean(i.get("Type")), "active": i.get("Active"), "qbo_qty_on_hand": num(dec(i.get("QtyOnHand")))}
                 for i in qbo_items if clean(i.get("Id")) not in mapped_qbo_ids
                 and clean(i.get("Sku")).startswith(AKP_SKU_PREFIX)]
    by_status = Counter(r["status"] for r in rows)
    summary = {
        "rows": len(rows), "by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "inventory_rows": sum(r["type"] == "Inventory" for r in rows),
        "noninventory_rows": sum(r["type"] == "NonInventory" for r in rows),
        "different_likely_timing": sum(1 for r in rows if r["likely_timing"]),
        "unmapped_epos_products": len(unmapped), "unmapped_tracked": sum(u["tracked"] for u in unmapped),
        "unassigned_stock_rows": len(unassigned), "qbo_akp_items_not_in_mapping": len(extra_qbo),
    }
    return {"schema_version": SCHEMA_VERSION, "company": COMPANY, "generated_at": now_iso(),
            "tolerance": num(tolerance), "sources": sources or {}, "business_context": context or {},
            "timing_evidence": timing_evidence or {}, "summary": summary, "rows": rows,
            "unmapped_epos_products": unmapped, "unassigned_stock_rows": unassigned,
            "qbo_akp_items_not_in_mapping": extra_qbo}


def summary_text(snapshot: dict) -> str:
    """'Stock check: 3,812 match, 41 different, 11 negative in QuickBooks' (+ the rest when non-zero)."""
    s = snapshot.get("summary") or {}
    b = s.get("by_status") or {}
    text = (f"Stock check: {b.get(MATCH, 0):,} match, {b.get(DIFFERENT, 0):,} different, "
            f"{b.get(NEGATIVE_QBO, 0):,} negative in QuickBooks")
    extra = []
    if s.get("different_likely_timing"):
        extra.append(f"{s['different_likely_timing']:,} of the differences likely timing")
    for key, label in ((NEGATIVE_EPOS, "negative in EPOS"), (NOT_IN_EPOS_REPORT, "not in the EPOS stock report"),
                       (NO_QBO_ITEM, "missing in QuickBooks")):
        if b.get(key):
            extra.append(f"{b[key]:,} {label}")
    if s.get("unmapped_tracked"):
        extra.append(f"{s['unmapped_tracked']:,} stock-tracked EPOS product(s) not mapped")
    return text + (f" ({'; '.join(extra)})" if extra else "")


# ---------------------------------------------------------------- run
def _daily_root() -> Path:
    from code_scripts.paths import STATE_ROOT

    return Path(STATE_ROOT) / "ops" / COMPANY / "daily"


def run(*, out: Path | None = None, use_epos: bool = True, use_qbo: bool = True, stock_report: Path | None = None,
        catalogue_path: Path | None = None, tolerance: Decimal | None = None, client=None, downloader=None,
        directory: Path | None = None, now: datetime | None = None) -> dict:
    """Build and write the snapshot. ``client`` / ``downloader`` are injectable for tests."""
    setup_env()
    folder = Path(directory) if directory else snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(out) if out else folder / "work"
    work.mkdir(parents=True, exist_ok=True)
    tol = tolerance if tolerance is not None else tolerance_default()
    mpath = mapping_file()
    mapping_rows = read_csv(mpath)

    # catalogue: always re-read (local file)
    cpath = Path(catalogue_path) if catalogue_path else default_catalogue_path()
    if not cpath.is_file():
        raise SnapshotError(f"EPOS catalogue not found at {cpath} (run catalogue_sync, or pass --catalogue)")
    catalogue = load_catalogue_file(cpath)

    # EPOS stock side
    epos_cache = folder / "epos_side.json"
    if use_epos:
        if stock_report:
            spath, stime = Path(stock_report), mtime_iso(Path(stock_report))
        else:
            spath = (downloader or download_stock_report)(work)
            stime = now_iso()
        epos_side = {"stock_report": str(spath), "stock_report_at": stime, "refreshed_at": now_iso(),
                     "rows": stock_rows_from_csv(spath)}
        write_json_atomic(epos_cache, epos_side)
    else:
        epos_side = read_json(epos_cache)
        if not epos_side:
            raise SnapshotError(f"--no-epos: no cached EPOS side at {epos_cache}; run once with EPOS")

    # QBO side
    qbo_cache = folder / "qbo_side.json"
    if use_qbo:
        if client is None:
            client = w7.QBOClient.for_company_a(allow_writes=False)
        items = relevant_qbo_items(fetch_qbo_items(client), mapping_rows)
        qbo_side = {"read_at": now_iso(), "items": items}
        write_json_atomic(qbo_cache, qbo_side)
    else:
        qbo_side = read_json(qbo_cache)
        if not qbo_side:
            raise SnapshotError(f"--no-qbo: no cached QBO side at {qbo_cache}; run once with QBO")

    # business-day context + timing evidence (best effort)
    daily_root, uploaded, meta_name = _daily_root(), _uploaded_dir(), _metadata_name()
    today = current_business_date(now)
    last_posted = last_posted_sales_date(daily_root, uploaded, meta_name)
    context = business_context(today, last_posted)
    sold, sold_file = sold_product_ids(uploaded, last_posted, meta_name)
    since = (today - timedelta(days=1)).isoformat()
    received, lines_file = recent_receipts(daily_root, since)
    evidence = {"sales_day": last_posted, "sales_file": sold_file, "products_sold": len(sold),
                "receipts_file": lines_file, "received_since": since, "products_received": len(received),
                "method": "DIFFERENT rows are flagged likely_timing when a family product sold on the last posted "
                          "day or is on an EPOS PO received since yesterday / whose bill is held"}
    sources = {
        "epos_stock_report": {"path": epos_side.get("stock_report"), "at": epos_side.get("stock_report_at"),
                              "refreshed_this_run": use_epos},
        "catalogue": {"path": str(cpath), "at": mtime_iso(cpath)},
        "qbo": {"read_at": qbo_side.get("read_at"), "refreshed_this_run": use_qbo},
        "mapping": {"path": str(mpath), "at": mtime_iso(mpath)},
    }
    snap = build_snapshot(mapping_rows=mapping_rows, catalogue=catalogue, stock_rows=epos_side["rows"],
                          qbo_items=qbo_side["items"], tolerance=tol, sources=sources, context=context,
                          sold_ids=sold, received=received, timing_evidence=evidence)
    snap["summary_text"] = summary_text(snap)
    write_json_atomic(folder / "latest.json", snap)
    write_json_atomic(folder / "history" / f"stock_snapshot_{today.isoformat()}.json", snap)
    if out:
        dump_json(Path(out) / "summary.json", {"summary": snap["summary"], "summary_text": snap["summary_text"],
                                               "latest": str(folder / "latest.json"), "sources": sources})
    return snap


class Busy(Exception):
    pass


def locked(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    fh = open(folder / ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise Busy(str(folder / ".lock"))
    return fh


def main(argv=None, *, client=None, downloader=None, slack=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="build latest.json (read-only)")
    p.add_argument("--no-epos", action="store_true", help="reuse the cached EPOS stock side")
    p.add_argument("--no-qbo", action="store_true", help="reuse the cached QBO side")
    p.add_argument("--stock-report", help="use this StockReport CSV instead of downloading")
    p.add_argument("--catalogue", help="catalogue file (default: catalogue_sync catalogue_snapshot.json)")
    p.add_argument("--tolerance", help=f"MATCH tolerance in canonical units (default {DEFAULT_TOLERANCE}, "
                                       f"env {TOLERANCE_ENV})")
    p.add_argument("--out", help="evidence folder (also gets summary.json)")
    p.add_argument("--slack", action="store_true", help="send the one-line summary to the ops Slack channel")
    p.add_argument("--no-slack", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    setup_env()
    if a.no_epos and a.stock_report:
        ap.error("--stock-report conflicts with --no-epos")
    tol = dec(a.tolerance) if a.tolerance else None
    if a.tolerance and (tol is None or tol < 0):
        ap.error("--tolerance must be a non-negative number")
    folder = snapshot_dir()
    try:
        lock = locked(folder)
    except Busy as exc:
        print(f"another stock snapshot is running ({exc}); try again shortly")
        return EXIT_BUSY
    try:
        snap = run(out=Path(a.out) if a.out else None, use_epos=not a.no_epos, use_qbo=not a.no_qbo,
                   stock_report=Path(a.stock_report) if a.stock_report else None,
                   catalogue_path=Path(a.catalogue) if a.catalogue else None, tolerance=tol, client=client,
                   downloader=downloader, directory=folder)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero; latest.json is left as it was
        print(f"stock snapshot FAILED: {type(exc).__name__}: {exc}")
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            dump_json(Path(a.out) / "summary.json", {"error": f"{type(exc).__name__}: {exc}"})
        return EXIT_FAILED
    finally:
        lock.close()
    print(snap["summary_text"])
    print(f"Wrote {folder / 'latest.json'}")
    if a.slack and not a.no_slack:
        try:
            if slack is None:
                from code_scripts.akponora_ops.common import send_slack as slack
            slack(snap["summary_text"])
        except Exception:  # noqa: BLE001
            pass
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

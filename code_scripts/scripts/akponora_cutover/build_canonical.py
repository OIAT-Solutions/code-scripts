#!/usr/bin/env python3
"""AKPONORA / NORA MINI MART (company_a) canonical EPOS -> QBO family mapping builder.

OFFLINE / READ-ONLY. Reads local files only; makes no QBO or EPOS calls and
writes only into --out. Nothing produced here is approved: every mapping row
has Review Status "Proposed", blank Target QBO Item Id and blank Approved By.

Model (AGENTS.md + AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md):
  * every EPOS stock-tracked product is a stock owner = one proposed new QBO
    Inventory item (family), SKU AKP-{owner EPOS ProductID};
  * canonical unit = owner's base unit (owner VolumeOfSale, or 1);
  * every untracked product is attached to exactly one stock owner only when
    the evidence is strong (base-name match, VoS <= owner VoS, unit cost
    consistent); sale multiplier = VolumeOfSale or 1, in canonical units;
  * no pack item is created in addition to a multiplied mapping.

Inputs: --catalogue (catalogue_products.json from epos_catalogue_pull), --stock-report,
--sales-coverage / --sales-history (EPOS BookKeeping CSVs), --qbo-items (QBO item export CSV
with Id, Name, Type, Active, IncomeAccountId/IncomeAccount, ExpenseAccountId/ExpenseAccount),
optional --staff-review (review.json). Relative evidence defaults resolve under --evidence-root.

Example (reproduces outputs/nora_gaps_2026-09-25/canonical_2026-09-26_1937):
  python -m code_scripts.scripts.akponora_cutover.build_canonical \
      --catalogue outputs/nora_gaps_2026-09-25/epos_catalogue_2026-09-26_1937/catalogue_products.json \
      --qbo-items <nora_readiness_2026-09-25>/qbo_items.csv \
      --staff-review <akponora_staff_review_2026-09-25>/review.json --out <dir>
Re-run for the 30 Sep pack: add --stock-report ".../StockReport_2026_09_30_xxxx.csv"
and --sales-coverage <new BookKeeping CSV>.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import DEFAULT_EVIDENCE_ROOT, GAPS_DIR, resolve_out

TOOL = "build_canonical"
# Evidence files resolve under --evidence-root; catalogue default is the 26 Sep 19:37 pull.
DEFAULTS = {
    "catalogue": GAPS_DIR / "epos_catalogue_2026-09-26_1937/catalogue_products.json",
    "stock_report": "As of 25th September/Stock Levels/StockReport_2026_09_25_1308.csv",
    "sales_coverage": "As of 25th September/BookKeeping_2026_09_25_1245.csv",
    "sales_history": "As of 16th September 2026/BookKeeping_June 1st till Sept 15.csv",
    "qbo_items": None,      # required: QBO item export CSV (nora_readiness qbo_items.csv)
    "staff_review": None,   # optional: staff review.json
}

# Exact header of templates/product_conversion_empty.csv (Codex branch); the
# main-repo loader code_scripts/product_conversion.py requires a subset of it.
CONVERSION_HEADER = [
    "Row ID", "EPOS Product ID", "EPOS Existing SKU", "EPOS Name", "Pipeline Status",
    "Review Status", "Target QBO Item Type", "Target QBO Name", "Target QBO SKU",
    "Target QBO Item Id", "Staff Approved Sale Multiplier", "Effective Date", "Approved By",
    "Canonical Family Key", "Canonical Unit", "Staff Approved Purchase Multiplier",
]

EFFECTIVE_DATE = "2026-10-01"
ASSET_ACCOUNT_ID = "77"
COST_TOLERANCE = Decimal("0.10")       # pass band (instruction: ~10%)
COST_SOFT_TOLERANCE = Decimal("0.25")  # 10-25%: soft (multiplier still unambiguous)
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*KG", re.I)
ZERO_FLOOR_REFERENCE = Decimal("153507737.70")
SUFFIX_RE = re.compile(r"\s*\*\s*(\d+)\s*$")


# ----------------------------------------------------------------- helpers
def clean(v) -> str:
    return " ".join(str(v if v is not None else "").split())


def dec(v) -> Decimal | None:
    try:
        d = Decimal(clean(v).replace(",", ""))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def q(d: Decimal | None, places="0.01") -> str:
    return "" if d is None else str(d.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def fmt_mult(d: Decimal) -> str:
    return str(int(d)) if d == d.to_integral() else format(d.normalize(), "f")


def name_suffix(name: str) -> int | None:
    m = SUFFIX_RE.search(clean(name))
    return int(m.group(1)) if m else None


def base_name(name: str) -> str:
    return SUFFIX_RE.sub("", clean(name)).strip()


def key_strict(name: str) -> str:
    return base_name(name).casefold()


def key_loose(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", base_name(name).casefold())


def norm_name(name: str) -> str:
    return clean(name).casefold()


def pid(v) -> int | None:
    t = clean(v)
    if not t:
        return None
    try:
        return int(Decimal(t))
    except (InvalidOperation, ValueError):
        return None


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_csv(p: Path) -> list[dict]:
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: list[dict], fields: list[str]) -> None:
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def mult_of(prod) -> Decimal:
    v = prod.get("VolumeOfSale")
    return Decimal(int(v)) if v not in (None, 0) else Decimal(1)


def explicit_mult_of(prod) -> Decimal | None:
    """Return a positive explicit sale multiplier, or None when EPOS leaves it blank/zero."""
    v = prod.get("VolumeOfSale")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return Decimal(n) if n > 0 else None


def child_multiplier_label(prod) -> str:
    """Render child evidence without converting a missing amount into x1."""
    value = explicit_mult_of(prod)
    return fmt_mult(value) if value is not None else "MISSING_MASTER_AMOUNT"


def cost_ex(prod) -> Decimal:
    return dec(prod.get("CostPriceExTax")) or Decimal(0)


def sales_totals(path: Path | None):
    """Return (per-product {pid: [lines, value, qty, name]}, rows, window)."""
    per = defaultdict(lambda: [0, Decimal(0), Decimal(0), ""])
    rows = 0
    dates = []
    missing_id = [0, Decimal(0)]
    if not path:
        return per, rows, None, missing_id
    for r in read_csv(path):
        if clean(r.get("Staff")) == "Total:" or not clean(r.get("Product")):
            continue
        rows += 1
        val = dec(r.get("TOTAL Sales")) or Decimal(0)
        p = pid(r.get("ProductId"))
        try:
            dates.append(datetime.strptime(clean(r.get("Date/Time")), "%d/%m/%Y %H:%M:%S"))
        except ValueError:
            pass
        if p is None:
            missing_id[0] += 1
            missing_id[1] += val
            continue
        e = per[p]
        e[0] += 1
        e[1] += val
        e[2] += dec(r.get("Quantity")) or Decimal(0)
        e[3] = clean(r.get("Product"))
    window = (min(dates).isoformat(sep=" "), max(dates).isoformat(sep=" ")) if dates else None
    return per, rows, window, missing_id


# ----------------------------------------------------------------- build
def build(args):
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    catalogue = json.loads(Path(args.catalogue).read_text())
    by_id = {p["Id"]: p for p in catalogue}
    assert len(by_id) == len(catalogue), "duplicate EPOS Ids in catalogue"
    tracked = [p for p in catalogue if p.get("IsStockTracked")]
    untracked = [p for p in catalogue if not p.get("IsStockTracked")]

    data_problems: list[dict] = []

    def problem(kind, detail, **kw):
        data_problems.append({"problem": kind, "detail": detail, **kw})

    # ---- sales
    cov, cov_rows, cov_window, cov_missing = sales_totals(args.sales_coverage)
    hist, _, hist_window, _ = sales_totals(args.sales_history)
    for p_id, (lines, val, _, nm) in sorted(cov.items(), key=lambda kv: -kv[1][1]):
        if p_id not in by_id:
            problem("SALES_ID_NOT_IN_CATALOGUE", f"{nm} sold {lines} lines / NGN {q(val)} in coverage window; ProductID {p_id} absent from 26 Sep catalogue (deleted/archived?)", epos_product_id=p_id)
    if cov_missing[0]:
        problem("SALES_LINE_WITHOUT_PRODUCT_ID", f"{cov_missing[0]} lines / NGN {q(cov_missing[1])} have no ProductId")

    # ---- stock report (joined by exact normalized name to catalogue)
    stock_rows = [r for r in read_csv(args.stock_report) if clean(r.get("Name")) != "Total:" and clean(r.get("Name"))]
    names_all = defaultdict(list)
    for p in catalogue:
        names_all[norm_name(p["Name"])].append(p)
    stock_by_owner: dict[int, list[dict]] = defaultdict(list)
    unmatched_stock_value_floor = Decimal(0)
    stock_floor_total = Decimal(0)
    stock_raw_total = Decimal(0)
    for r in stock_rows:
        tc = dec(r.get("TotalCost")) or Decimal(0)
        stock_raw_total += tc
        floor_val = max(tc, Decimal(0))
        stock_floor_total += floor_val
        cands = names_all.get(norm_name(r["Name"]), [])
        tcands = [p for p in cands if p.get("IsStockTracked")]
        if len(tcands) == 1:
            stock_by_owner[tcands[0]["Id"]].append(r)
        elif len(tcands) > 1:
            unmatched_stock_value_floor += floor_val
            problem("STOCK_ROW_AMBIGUOUS", f"stock row '{r['Name']}' matches {len(tcands)} tracked products {[p['Id'] for p in tcands]}; qty not assigned (zero-floor value NGN {q(floor_val)})")
        elif cands:
            unmatched_stock_value_floor += floor_val
            problem("STOCK_ON_UNTRACKED_PRODUCT", f"stock row '{r['Name']}' matches only untracked product(s) {[p['Id'] for p in cands]} (TotalStock {r.get('TotalStock')}, zero-floor value NGN {q(floor_val)})")
        else:
            unmatched_stock_value_floor += floor_val
            problem("STOCK_ROW_NOT_IN_CATALOGUE", f"stock row '{r['Name']}' has no catalogue name match (TotalStock {r.get('TotalStock')}, zero-floor value NGN {q(floor_val)})")

    # ---- live QBO names (active, all types) for collision checks
    qbo = read_csv(args.qbo_items)
    active_names = defaultdict(list)
    inactive_names = defaultdict(list)
    for it in qbo:
        (active_names if clean(it.get("Active")).lower() == "true" else inactive_names)[norm_name(it["Name"])].append(it)
    # Proposed income/COGS accounts per EPOS category from legacy exact-name matches.
    cat_income = defaultdict(Counter)
    cat_expense = defaultdict(Counter)
    for p in catalogue:
        for it in active_names.get(norm_name(p["Name"]), []):
            if it.get("Type") == "Inventory":
                cat_income[p.get("CategoryName")][(it["IncomeAccountId"], it["IncomeAccount"])] += 1
                cat_expense[p.get("CategoryName")][(it["ExpenseAccountId"], it["ExpenseAccount"])] += 1

    # ---- staff review annotations (optional)
    staff_note = defaultdict(list)
    if args.staff_review and Path(args.staff_review).exists():
        sr = json.loads(Path(args.staff_review).read_text())
        for s in sr.get("staff_comparison", []):
            note = []
            if s.get("archived_by_staff"):
                note.append("staff marked ARCHIVED")
            if s.get("staff_replacement_cost") not in (None, ""):
                note.append(f"staff replacement cost {s['staff_replacement_cost']}")
            if s.get("staff_unit"):
                note.append(f"staff unit '{s['staff_unit']}'")
            if note:
                staff_note[norm_name(s.get("staff_name", ""))].append("; ".join(note))

    # ---- families: one per tracked product
    strict_idx = defaultdict(list)
    loose_idx = defaultdict(list)
    for p in tracked:
        strict_idx[key_strict(p["Name"])].append(p)
        loose_idx[key_loose(p["Name"])].append(p)

    families: dict[int, dict] = {}
    for p in tracked:
        m = mult_of(p)
        flags = []
        siblings = [s["Id"] for s in strict_idx[key_strict(p["Name"])] if s["Id"] != p["Id"]]
        if siblings:
            flags.append(f"TRACKED_SIBLING({','.join(map(str, siblings))})")
        suf = name_suffix(p["Name"])
        if p.get("VolumeOfSale") and suf and suf != p["VolumeOfSale"]:
            flags.append(f"VOS_NAME_MISMATCH(VoS {p['VolumeOfSale']} vs name *{suf})")
        if not p.get("VolumeOfSale") and suf and suf > 1:
            flags.append(f"NAME_SUFFIX_WITHOUT_VOS(*{suf})")
        c = cost_ex(p)
        if c <= 0:
            flags.append("MISSING_COST")
        cost_tax = p.get("CostPriceTaxGroupName") or "UNKNOWN"
        # stock
        srows = stock_by_owner.get(p["Id"], [])
        qty = Decimal(0)
        raw_qty = None
        stock_cost = None
        stock_floor_val = Decimal(0)
        if not srows:
            flags.append("NO_STOCK_ROW")
        else:
            if len(srows) > 1:
                flags.append(f"MULTIPLE_STOCK_ROWS({len(srows)})")
            raw_qty = Decimal(0)
            for r in srows:
                full = dec(r.get("MeasuredCurrentStock")) or Decimal(0)
                loose = dec(r.get("CurrentVolume")) or Decimal(0)
                total = dec(r.get("TotalStock")) or Decimal(0)
                if p.get("VolumeOfSale"):
                    rq = full * m + loose
                    if abs(total * m - rq) > Decimal("0.6"):
                        flags.append(f"STOCK_VOS_INCONSISTENT(full {full}+loose {loose} vs total {total}xVoS {m})")
                else:
                    rq = total
                    if loose != 0:
                        flags.append(f"LOOSE_STOCK_WITHOUT_VOS(loose {loose})")
                raw_qty += rq
                stock_cost = dec(r.get("MeasuredCostPrice"))
                stock_floor_val += max(dec(r.get("TotalCost")) or Decimal(0), Decimal(0))
            if raw_qty < 0:
                flags.append(f"NEGATIVE_ZERO_FLOOR({fmt_mult(raw_qty)})")
            qty = max(raw_qty, Decimal(0))
            if qty != qty.to_integral():
                flags.append("FRACTIONAL_CANONICAL_QTY")
            if stock_cost is not None and stock_cost == 0 and c > 0 and qty > 0:
                flags.append("STOCK_REPORT_COST_ZERO(catalogue now has cost; value not in 25 Sep zero-floor total)")
            elif stock_cost is not None and c > 0 and abs(stock_cost - c) > max(Decimal("0.02"), c * Decimal("0.01")):
                inc = dec(p.get("CostPriceIncTax")) or Decimal(0)
                basis = "matches inc-tax" if abs(stock_cost - inc) <= Decimal("0.02") else "matches neither"
                flags.append(f"STOCK_COST_DIFFERS(stock {stock_cost} vs catalogue ex {c}; {basis})")
            if qty > 0 and c <= 0:
                flags.append("POSITIVE_STOCK_NO_COST")
        unit_cost = (c / m) if c > 0 else Decimal(0)
        for n in staff_note.get(norm_name(p["Name"]), []):
            flags.append(f"STAFF_NOTE[{n}]")
        families[p["Id"]] = {
            "owner": p, "mult": m, "flags": flags, "qty": qty, "raw_qty": raw_qty,
            "unit_cost": unit_cost, "cost_tax": cost_tax, "stock_floor_val": stock_floor_val,
            "stock_cost": stock_cost, "children": [],
        }

    # ---- family names (unique among families; collision check vs live QBO)
    def proposed_name(p):
        return base_name(p["Name"]) if mult_of(p) > 1 and name_suffix(p["Name"]) else clean(p["Name"])

    first = Counter(norm_name(proposed_name(f["owner"])) for f in families.values())
    used = Counter()
    for fid, f in sorted(families.items()):
        nm = proposed_name(f["owner"])
        if first[norm_name(nm)] > 1:
            nm = clean(f["owner"]["Name"])
        f["name"] = nm
        used[norm_name(nm)] += 1
    for fid, f in sorted(families.items()):
        if used[norm_name(f["name"])] > 1:
            f["name"] = f"{f['name']} [{fid}]"
            f["flags"].append("DUPLICATE_EPOS_NAME_SUFFIXED")
    assert len({norm_name(f["name"]) for f in families.values()}) == len(families)
    for f in families.values():
        nm = f["name"].replace(":", "-")
        if nm != f["name"]:
            f["flags"].append("COLON_REPLACED")
        if len(nm) > 100:
            f["flags"].append("NAME_TOO_LONG")
        f["name"] = nm
        f["collisions"] = active_names.get(norm_name(nm), [])

    # ---- mapping rows
    mapping, evidence = [], []

    def add_row(prod, tier, reasons, fam=None, mult=None, target_type="Inventory", target_name="", target_sku="", unit="", implied=None, owner_candidates="", suggestion="", role=None):
        s = cov.get(prod["Id"], [0, Decimal(0), Decimal(0), ""])
        hs = hist.get(prod["Id"], [0, Decimal(0), Decimal(0), ""])
        status = {"A": "TIER_A", "B": "TIER_B", "C": "BLOCK"}[tier]
        mstr = fmt_mult(mult) if mult is not None else ""
        mapping.append({
            "Row ID": f"EPOS-{prod['Id']}", "EPOS Product ID": prod["Id"], "EPOS Existing SKU": clean(prod.get("Sku")),
            "EPOS Name": clean(prod["Name"]), "Pipeline Status": status, "Review Status": "Proposed",
            "Target QBO Item Type": target_type, "Target QBO Name": target_name, "Target QBO SKU": target_sku,
            "Target QBO Item Id": "", "Staff Approved Sale Multiplier": mstr if tier != "C" else "",
            "Effective Date": EFFECTIVE_DATE, "Approved By": "",
            "Canonical Family Key": target_sku if fam else "", "Canonical Unit": unit,
            "Staff Approved Purchase Multiplier": mstr if tier != "C" else "",
        })
        evidence.append({
            "EPOS Product ID": prod["Id"], "EPOS Name": clean(prod["Name"]), "Tier": tier,
            "Reasons": "; ".join(reasons), "Role": role or ("STOCK_OWNER" if prod.get("IsStockTracked") else ("CHILD" if fam else "STANDALONE_UNTRACKED")),
            "Family SKU": target_sku if fam else "", "Owner Product ID": fam["owner"]["Id"] if fam else "",
            "Owner Name": clean(fam["owner"]["Name"]) if fam else "", "Owner candidates": owner_candidates,
            "Proposed sale multiplier (canonical units)": mstr,
            "Implied multiplier from cost": q(implied, "0.01") if implied is not None else "",
            "EPOS VolumeOfSale": prod.get("VolumeOfSale") if prod.get("VolumeOfSale") is not None else "",
            "Cost ex tax": prod.get("CostPriceExTax"), "Cost tax group": prod.get("CostPriceTaxGroupName"),
            "Sale price inc tax": prod.get("SalePriceIncTax"), "Sale tax group": prod.get("SalePriceTaxGroupName"),
            "Category": prod.get("CategoryName"), "Sell on till": prod.get("SellOnTill"),
            "Stock tracked": prod.get("IsStockTracked"),
            "Sep coverage lines": s[0], "Sep coverage value": q(s[1]),
            "Jun-15Sep lines": hs[0], "Jun-15Sep value": q(hs[1]),
            "Suggestion (unverified)": suggestion,
        })

    def unit_label(f):
        o = f["owner"]
        return "Each" if f["mult"] == 1 else f"Each (1/{fmt_mult(f['mult'])} of {clean(o['Name'])})"

    # owners
    for fid, f in families.items():
        o = f["owner"]
        reasons, soft, hard = [], [], []
        if any(x.startswith("TRACKED_SIBLING") for x in f["flags"]):
            hard.append("another tracked product shares the base name: two stock owners for one physical family (merge decision needed)")
        if any(x.startswith("VOS_NAME_MISMATCH") for x in f["flags"]):
            soft.append("VolumeOfSale disagrees with name *N")
        if "MISSING_COST" in f["flags"]:
            soft.append("owner cost missing (opening value 0)")
        if "NO_STOCK_ROW" in f["flags"]:
            soft.append("no stock-report row (opening qty 0)")
        if any(x.startswith(("STOCK_VOS_INCONSISTENT", "LOOSE_STOCK_WITHOUT_VOS", "MULTIPLE_STOCK_ROWS")) for x in f["flags"]):
            soft.append("stock row inconsistent with VoS")
        if "DUPLICATE_EPOS_NAME_SUFFIXED" in f["flags"]:
            hard.append("duplicate EPOS name")
        tier = "C" if hard or len(soft) >= 2 else ("B" if soft else "A")
        reasons = (hard + soft) or ["tracked stock owner; multiplier = own VolumeOfSale" if o.get("VolumeOfSale") else "single-row tracked standalone"]
        add_row(o, tier, reasons, fam=f, mult=f["mult"], target_name=f["name"], target_sku=f"AKP-{fid}", unit=unit_label(f))
        f["tier"] = tier

    # suggestions for standalone untracked rows (never used for tiering)
    def alpha_tokens(n):
        return {t for t in re.findall(r"[a-z]{3,}", base_name(n).casefold()) if t not in {"pcs", "pack", "single", "half", "quarter", "big", "small"}}

    weight_groups = defaultdict(list)
    for x in untracked:
        if WEIGHT_RE.search(x["Name"]):
            wk = re.sub(r"\s+", " ", re.sub(r"\(?\b(HALF|QUARTER)\b\)?", "", WEIGHT_RE.sub("", x["Name"]), flags=re.I)).strip().casefold()
            weight_groups[wk].append(x)
    tracked_tokens = [(o, alpha_tokens(o["Name"])) for o in tracked]

    def suggest(x):
        out_s = []
        m = WEIGHT_RE.search(x["Name"])
        if m:
            wk = re.sub(r"\s+", " ", re.sub(r"\(?\b(HALF|QUARTER)\b\)?", "", WEIGHT_RE.sub("", x["Name"]), flags=re.I)).strip().casefold()
            grp = weight_groups.get(wk, [])
            if len(grp) >= 2:
                out_s.append(f"WEIGHT FAMILY '{wk.upper()}' ({len(grp)} untracked sizes, no stock owner): create one tracked EPOS master in kg; this row multiplier {m.group(1)} kg")
        xt = alpha_tokens(x["Name"])
        best = None
        if xt:
            for o, ot in tracked_tokens:
                if not ot or o.get("CategoryName") != x.get("CategoryName"):
                    continue
                jac = len(xt & ot) / len(xt | ot)
                if jac < 0.6:
                    continue
                oc, xc = cost_ex(o), cost_ex(x)
                imp = (xc / (oc / mult_of(o))) if oc > 0 and xc > 0 else None
                score = (jac, -(abs(imp - imp.to_integral()) / max(imp, Decimal(1))) if imp else Decimal(-1))
                if best is None or score > best[0]:
                    best = (score, o, imp)
        if best:
            _, o, imp = best
            out_s.append(f"possible owner {o['Id']} '{clean(o['Name'])}' (VoS {o.get('VolumeOfSale')}); cost implies multiplier {q(imp) if imp else '?'}")
        return " || ".join(out_s)

    # children / standalone
    standalone = 0
    for p in untracked:
        cm = explicit_mult_of(p)
        cands = strict_idx.get(key_strict(p["Name"]), [])
        loose = False
        if not cands:
            cands = loose_idx.get(key_loose(p["Name"]), [])
            loose = bool(cands)

        def check(o):
            om = mult_of(o)
            issues, soft = [], []
            if cm is None:
                return ["child VolumeOfSale is blank/zero; Master Product amount required"], soft, None
            if o.get("VolumeOfSale") is None and p.get("VolumeOfSale") not in (None, 1):
                issues.append(f"owner has no VoS but child VoS {p['VolumeOfSale']}")
            elif cm > om:
                issues.append(f"child multiplier {fmt_mult(cm)} > owner {fmt_mult(om)}")
            oc, ccost = cost_ex(o), cost_ex(p)
            implied = None
            if oc > 0 and ccost > 0:
                expected = oc * cm / om
                implied = ccost / (oc / om)
                dev = abs(ccost - expected) / expected
                if dev > COST_SOFT_TOLERANCE:
                    issues.append(f"cost check failed: child {q(ccost)} vs expected {q(expected)} (implied multiplier {q(implied)})")
                elif dev > COST_TOLERANCE:
                    soft.append(f"cost off {q(dev * 100, '0.1')}% (child {q(ccost)} vs expected {q(expected)}; implied multiplier {q(implied)})")
            else:
                soft.append("cost missing on child or owner; cost check not possible")
            return issues, soft, implied

        if not cands:
            standalone += 1
            reasons = ["no tracked stock owner matches base name"]
            if p.get("VolumeOfSale"):
                reasons.append(f"has VolumeOfSale {p['VolumeOfSale']} (implies a master link that name matching cannot find)")
            reasons.append("propose: make stock-tracked in EPOS if physically stocked, else Non-inventory/Service")
            add_row(p, "C", reasons, target_type="Non-inventory", target_name="", target_sku="", unit="", suggestion=suggest(p))
            continue

        chosen, implied, soft, hard = None, None, [], []
        if len(cands) == 1:
            chosen = cands[0]
            iss, sft, implied = check(chosen)
            hard += iss
            soft += sft
        else:
            passing = [(o,) + check(o) for o in cands]
            ok = [t for t in passing if not t[1]]
            if len(ok) == 1:
                chosen, _, sft, implied = ok[0]
                soft += sft + [f"{len(cands)} tracked owners share the base name; chosen by cost/VoS"]
            else:
                hard.append(f"{len(cands)} tracked owners share base name; cost/VoS cannot pick one")
        cand_str = " | ".join(f"{o['Id']}:{clean(o['Name'])}" for o in cands)
        if chosen is None:
            add_row(p, "C", hard, owner_candidates=cand_str, implied=None, role="CHILD_AMBIGUOUS_OWNER")
            continue
        f = families[chosen["Id"]]
        if loose:
            soft.append("base name matches only after punctuation/space normalisation")
        if cm is None:
            hard.append("child VolumeOfSale is blank/zero; Master Product amount required")
        if any(x.startswith("TRACKED_SIBLING") for x in f["flags"]):
            hard.append("owner family has a tracked sibling (double stock owner)")
        if (p.get("SalePriceTaxGroupName") or "") != (chosen.get("SalePriceTaxGroupName") or ""):
            soft.append(f"sale tax group differs from owner ({p.get('SalePriceTaxGroupName')} vs {chosen.get('SalePriceTaxGroupName')})")
        tier = "C" if hard or len(soft) >= 2 else ("B" if soft else "A")
        reasons = hard + soft or ["explicit VoS, unique owner, cost check passed"]
        for n in staff_note.get(norm_name(p["Name"]), []):
            reasons.append(f"STAFF_NOTE[{n}]")
        add_row(p, tier, reasons, fam=f, mult=cm, target_name=f["name"], target_sku=f"AKP-{chosen['Id']}", unit=unit_label(f), implied=implied, owner_candidates=cand_str)
        f["children"].append((p, tier))

    assert len(mapping) == len(catalogue)

    # ---- families.csv / qbo_create_draft.csv
    fam_rows, draft_rows = [], []
    for fid, f in sorted(families.items(), key=lambda kv: kv[1]["name"].casefold()):
        o = f["owner"]
        value = f["qty"] * f["unit_cost"]
        f["value"] = value
        fam_sales = sum((cov.get(x["Id"], [0, Decimal(0)])[1] for x in [o] + [c for c, _ in f["children"]]), Decimal(0))
        f["sales"] = fam_sales
        inc = cat_income[o.get("CategoryName")].most_common(1)
        exp = cat_expense[o.get("CategoryName")].most_common(1)
        coll = f["collisions"]
        tiers = Counter([f["tier"]] + [t for _, t in f["children"]])
        readiness = "BLOCKED" if f["tier"] == "C" else ("READY_A" if f["tier"] == "A" and not any(t == "C" for _, t in f["children"]) else "REVIEW")
        fam_rows.append({
            "Family SKU": f"AKP-{fid}", "Proposed QBO Name": f["name"], "Owner EPOS Product ID": fid,
            "Owner EPOS Name": clean(o["Name"]), "Category": o.get("CategoryName"),
            "Owner VolumeOfSale": o.get("VolumeOfSale") if o.get("VolumeOfSale") is not None else "",
            "Owner multiplier (canonical units per owner unit)": fmt_mult(f["mult"]),
            "Canonical unit": unit_label(f), "Owner tier": f["tier"], "Create readiness": readiness,
            "Children A/B/C": f"{sum(1 for _, t in f['children'] if t == 'A')}/{sum(1 for _, t in f['children'] if t == 'B')}/{sum(1 for _, t in f['children'] if t == 'C')}",
            "Child Product IDs": " | ".join(f"{c['Id']}x{child_multiplier_label(c)}({t})" for c, t in f["children"]),
            "Owner cost ex tax": o.get("CostPriceExTax"), "Owner cost inc tax": o.get("CostPriceIncTax"),
            "Cost tax group": f["cost_tax"], "Unit cost ex tax (canonical)": q(f["unit_cost"], "0.00001"),
            "Stock report MeasuredCostPrice": "" if f["stock_cost"] is None else str(f["stock_cost"]),
            "Raw canonical qty (stock report)": "" if f["raw_qty"] is None else fmt_mult(f["raw_qty"]),
            "Opening canonical qty (zero floor)": fmt_mult(f["qty"]),
            "Opening value ex tax (catalogue cost)": q(value),
            "Stock report zero-floor value": q(f["stock_floor_val"]),
            "Sep 1-25 family sales value": q(fam_sales),
            "Live active QBO name collision Ids": " | ".join(f"{c['Id']}:{c['Type']}" for c in coll),
            "Flags": "; ".join(f["flags"]),
        })
        draft_rows.append({
            "dry_run": "TRUE", "status": "DRAFT_NOT_APPROVED", "create_readiness": readiness,
            "Name": f["name"], "Sku": f"AKP-{fid}", "Type": "Inventory", "TrackQtyOnHand": "TRUE",
            "QtyOnHand": fmt_mult(f["qty"]), "InvStartDate": EFFECTIVE_DATE,
            "PurchaseCost": q(f["unit_cost"], "0.00001"), "PurchaseCost basis": f"ex-tax (EPOS cost tax group {f['cost_tax']}); VERIFY",
            "UnitPrice": q((dec(o.get("SalePriceIncTax")) or Decimal(0)) / f["mult"], "0.01"),
            "SalesTaxIncluded": "TRUE", "Taxable": "TRUE" if (o.get("SalePriceTaxGroupName") or "") == "VAT" else "FALSE",
            "AssetAccountId": ASSET_ACCOUNT_ID,
            "IncomeAccountId (proposed)": inc[0][0][0] if inc else "", "IncomeAccount (proposed)": inc[0][0][1] if inc else "",
            "ExpenseAccountId (proposed)": exp[0][0][0] if exp else "", "ExpenseAccount (proposed)": exp[0][0][1] if exp else "",
            "Opening value ex tax": q(value),
            "Description": f"EPOS master {fid} {clean(o['Name'])}; canonical unit {unit_label(f)}",
            "Collides with live active QBO Id": " | ".join(c["Id"] for c in coll),
            "Proposed W5 legacy rename": " | ".join(f"{c['Id']} -> LEGACY — {clean(c['Name'])}" + (" (TOO LONG >100)" if len("LEGACY — " + clean(c["Name"])) > 100 else "") for c in coll),
            "Flags": "; ".join(f["flags"]),
        })

    fam_fields = list(fam_rows[0])
    write_csv(out / "families.csv", fam_rows, fam_fields)
    write_csv(out / "qbo_create_draft.csv", draft_rows, list(draft_rows[0]))
    order = {m["EPOS Product ID"]: i for i, m in enumerate(mapping)}
    mapping.sort(key=lambda m: (m["Target QBO SKU"] or "~", m["Pipeline Status"], str(m["EPOS Product ID"])))
    write_csv(out / "mapping_proposal.csv", mapping, CONVERSION_HEADER)
    evidence.sort(key=lambda e: (e["Tier"], -Decimal(e["Sep coverage value"] or 0)))
    write_csv(out / "mapping_evidence.csv", evidence, list(evidence[0]))

    # ---- staff blockers
    ev_by_id = {e["EPOS Product ID"]: e for e in evidence}
    blockers = []
    for e in evidence:
        if e["Tier"] != "C":
            continue
        r = e["Reasons"]
        if e["Role"] == "STANDALONE_UNTRACKED":
            qn = "Is this physically stocked? If yes: which master product (EPOS ID) does it deduct from and how many units per sale, or should it be made stock-tracked? If not stocked (service/fee/made-to-order), confirm Non-inventory."
        elif "cost check failed" in r:
            qn = f"How many units of master {e['Owner Product ID'] or '(candidate)'} does one sale of this button consume? EPOS VoS says {e['EPOS VolumeOfSale'] or 1}, cost implies {e['Implied multiplier from cost'] or '?'}. Fix VoS/cost in EPOS or confirm."
        elif "tracked sibling" in r or "two stock owners" in r:
            qn = "Two stock-tracked rows cover the same physical product. Which one row owns stock, and how many units per sale does the other consume? (Stock must not be counted twice.)"
        elif "cost off" in r and "multiplier 1 implied" in r:
            qn = f"Quick confirm: one sale = 1 unit of master {e['Owner Product ID']} ({e['Owner Name']})? Cost is {e['Implied multiplier from cost']}x the per-unit master cost (bulk discount or stale cost?)."
        elif "share base name" in r:
            qn = f"Which master does this deduct from? Candidates: {e['Owner candidates']}"
        else:
            qn = "Confirm the master product, units consumed per sale and cost basis."
        blockers.append({**{k: e[k] for k in ["EPOS Product ID", "EPOS Name", "Role", "Category", "Sell on till", "Owner Product ID", "Owner Name", "Owner candidates", "EPOS VolumeOfSale", "Implied multiplier from cost", "Cost ex tax", "Sep coverage lines", "Sep coverage value", "Suggestion (unverified)"]}, "Reasons": r, "Question for staff": qn, "Staff answer": "", "Answered by": ""})
    blockers.sort(key=lambda b: (-Decimal(b["Sep coverage value"] or 0), -int(b["Sep coverage lines"] or 0)))
    for i, b in enumerate(blockers, 1):
        b["Priority"] = i
    write_csv(out / "staff_blockers.csv", blockers, ["Priority"] + [k for k in blockers[0] if k != "Priority"])
    write_csv(out / "data_problems.csv", data_problems, ["problem", "detail", "epos_product_id"])

    # ---- summary
    tier_counts = Counter(e["Tier"] for e in evidence)
    def c_reason(e):
        r = e["Reasons"]
        if e["Role"] == "STANDALONE_UNTRACKED":
            return "standalone untracked (no stock owner)"
        if "cost check failed" in r:
            return "cost implies different multiplier (>25%)"
        if "tracked sibling" in r or "two stock owners" in r:
            return "two tracked rows for one family"
        if "cost off" in r:
            return "two soft checks (cost 10-25% off + implicit multiplier/other)"
        return "two soft checks (tax group, missing cost, loose name match) or ambiguous owner"
    c_reasons = defaultdict(lambda: [0, 0, Decimal(0)])
    for e in evidence:
        if e["Tier"] == "C":
            k = c_reason(e)
            c_reasons[k][0] += 1
            c_reasons[k][1] += int(e["Sep coverage lines"])
            c_reasons[k][2] += Decimal(e["Sep coverage value"] or 0)
    tier_by_role = defaultdict(Counter)
    for e in evidence:
        tier_by_role[e["Role"]][e["Tier"]] += 1
    tier_of = {e["EPOS Product ID"]: e["Tier"] for e in evidence}
    tot_lines = sum(v[0] for v in cov.values()) + cov_missing[0]
    tot_val = sum((v[1] for v in cov.values()), Decimal(0)) + cov_missing[1]
    cov_by = defaultdict(lambda: [0, Decimal(0)])
    for p_id, v in cov.items():
        t = tier_of.get(p_id, "NOT_IN_CATALOGUE")
        cov_by[t][0] += v[0]
        cov_by[t][1] += v[1]
    ab_lines = cov_by["A"][0] + cov_by["B"][0]
    ab_val = cov_by["A"][1] + cov_by["B"][1]
    top_c = [{"EPOS Product ID": b["EPOS Product ID"], "EPOS Name": b["EPOS Name"], "Role": b["Role"],
              "Sep lines": b["Sep coverage lines"], "Sep value": b["Sep coverage value"], "Reasons": b["Reasons"]}
             for b in blockers if int(b["Sep coverage lines"] or 0) > 0][:50]
    open_val = sum((f["value"] for f in families.values()), Decimal(0))
    open_val_ab = sum((f["value"] for f in families.values() if f["tier"] != "C"), Decimal(0))
    fam_floor = sum((f["stock_floor_val"] for f in families.values()), Decimal(0))
    fam_flags = Counter(x.split("(")[0].split("[")[0] for f in families.values() for x in f["flags"])
    summary = {
        "status": "PROPOSAL ONLY - offline; no QBO/EPOS calls; nothing approved; Target QBO Item Id blank on every row",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "inputs": {k: {"path": str(getattr(args, k)), "sha256": sha(Path(getattr(args, k)))} for k in DEFAULTS if getattr(args, k) and Path(getattr(args, k)).exists()},
        "catalogue_products": len(catalogue), "tracked": len(tracked), "untracked": len(untracked),
        "families": len(families),
        "families_by_readiness": dict(Counter(r["Create readiness"] for r in fam_rows)),
        "families_with_children": sum(1 for f in families.values() if f["children"]),
        "family_flags": dict(fam_flags.most_common()),
        "mapping_rows": len(mapping),
        "mapping_tiers": dict(sorted(tier_counts.items())),
        "mapping_tiers_by_role": {k: dict(sorted(v.items())) for k, v in tier_by_role.items()},
        "standalone_untracked": standalone,
        "tier_C_by_reason": {k: {"rows": v[0], "sep_lines": v[1], "sep_value": q(v[2])} for k, v in sorted(c_reasons.items(), key=lambda kv: -kv[1][2])},
        "coverage": {
            "sales_file": str(args.sales_coverage), "window": cov_window,
            "lines_total": tot_lines, "value_total": q(tot_val),
            "lines_tier_A_B": ab_lines, "value_tier_A_B": q(ab_val),
            "lines_pct_A_B": round(100 * ab_lines / tot_lines, 2) if tot_lines else None,
            "value_pct_A_B": round(float(100 * ab_val / tot_val), 2) if tot_val else None,
            "by_tier": {k: {"lines": v[0], "value": q(v[1]), "value_pct": round(float(100 * v[1] / tot_val), 2) if tot_val else None} for k, v in sorted(cov_by.items())},
        },
        "opening_valuation": {
            "stock_report": str(args.stock_report),
            "families_opening_value_ex_tax_catalogue_cost": q(open_val),
            "families_opening_value_ex_tax_owner_tier_A_B_only": q(open_val_ab),
            "families_stock_report_zero_floor_value": q(fam_floor),
            "stock_rows_not_assigned_to_a_family_zero_floor_value": q(unmatched_stock_value_floor),
            "stock_report_zero_floor_total_recomputed": q(stock_floor_total),
            "stock_report_raw_total": q(stock_raw_total),
            "reference_zero_floor_25_sep": str(ZERO_FLOOR_REFERENCE),
            "difference_families_catalogue_cost_vs_reference": q(open_val - ZERO_FLOOR_REFERENCE),
            "cost_basis": "ex-tax (catalogue CostPriceExTax / owner multiplier); stock report MeasuredCostPrice is also ex-tax for most rows",
        },
        "name_collisions_live_active": sum(1 for f in families.values() if f["collisions"]),
        "legacy_renames_proposed_W5": sum(len(f["collisions"]) for f in families.values()),
        "data_problems": dict(Counter(d["problem"] for d in data_problems)),
        "top50_till_sold_tier_C": top_c,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT,
                    help="folder holding the EPOS evidence packs (default ../MISC/AKPONORA Investigation/COGS Analysis)")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), dest=k, type=Path, default=None,
                        required=(k == "qbo_items"),
                        help=f"default: {v}" if v is not None else ("required" if k == "qbo_items" else "optional"))
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/build_canonical_<timestamp>/)")
    a = ap.parse_args(argv)
    for k, v in DEFAULTS.items():
        if getattr(a, k) is None and v is not None:
            v = Path(v)
            setattr(a, k, v if v.is_absolute() else a.evidence_root / v)
    a.out = resolve_out(a.out, TOOL)
    s = build(a)
    print(json.dumps({k: s[k] for k in ["families", "families_by_readiness", "mapping_tiers", "mapping_tiers_by_role", "coverage", "opening_valuation", "name_collisions_live_active", "data_problems"]}, indent=2, default=str))
    print("->", a.out)


if __name__ == "__main__":
    main()

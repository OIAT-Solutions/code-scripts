"""Offline accountant review of canonical mapping tiers A/B (files only, no QBO/EPOS calls).

Reads a build_canonical output folder (--base: mapping_proposal.csv, mapping_evidence.csv,
families.csv, staff_blockers.csv), the catalogue it was built from (--catalogue) and the
Sep coverage BookKeeping CSV (--sales). Applies deterministic checks to every tier A/B row,
samples tier B groups with a fixed --seed, approves owners first and then children whose
owner is approved, and writes into --out:
  approved_mapping_staging.csv  approved rows (Review Status Approved, Target QBO Item Id blank)
  still_blocked.csv             everything else, with the question for staff
  families_approved.csv         approved families + Pre-W7 action notes
  review_out.json               stats, samples and per-row check results

Example (reproduces canonical_2026-09-26_1937/approval):
  python -m code_scripts.scripts.akponora_cutover.review_approval \
      --base outputs/nora_gaps_2026-09-25/canonical_2026-09-26_1937 \
      --catalogue outputs/nora_gaps_2026-09-25/epos_catalogue_2026-09-26_1937/catalogue_products.json \
      --out <dir>
"""
import argparse
import csv
import json
import math
import random
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import DEFAULT_EVIDENCE_ROOT, GAPS_DIR, REPO_ROOT, resolve_out

TOOL = "review_approval"
DEFAULT_SEED = 20260926
DEFAULT_APPROVED_BY = "Marvin Mokolo (chat 2026-09-26); reviewed by Claude"
EFFECTIVE = "2026-10-01"
COST_TOL = 0.10
PRICE_LO, PRICE_HI = 0.60, 1.80
SUFFIX_RE = re.compile(r"\s*\*\s*(\d+)\s*$")


def clean(v):
    return " ".join(str(v if v is not None else "").split())


def base_key(n):
    return SUFFIX_RE.sub("", clean(n)).strip().casefold()


def suffix(n):
    m = SUFFIX_RE.search(clean(n))
    return int(m.group(1)) if m else None


def read(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def fnum(v):
    try:
        x = float(clean(v).replace(",", ""))
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def review(base, catalogue, sales, out, seed=DEFAULT_SEED, approved_by=DEFAULT_APPROVED_BY):
    base, out = Path(base), Path(out)
    prop_fields, proposal = read(base / "mapping_proposal.csv")
    _, evidence = read(base / "mapping_evidence.csv")
    fam_fields, families = read(base / "families.csv")
    _, blockers = read(base / "staff_blockers.csv")
    cat = {p["Id"]: p for p in json.load(open(catalogue))}
    ev = {e["EPOS Product ID"]: e for e in evidence}
    fam = {f["Family SKU"]: f for f in families}
    blk = {b["EPOS Product ID"]: b for b in blockers}
    assert len(proposal) == len(evidence) == len(cat)

    # Sep 1-25 sales: net qty and value per product id
    sq, sv = defaultdict(float), defaultdict(float)
    tot_val = 0.0
    for r in read(sales)[1]:
        if clean(r.get("Staff")) == "Total:" or not clean(r.get("Product")):
            continue
        v = fnum(r.get("TOTAL Sales")) or 0.0
        tot_val += v
        pid = clean(r.get("ProductId"))
        if pid:
            pid = str(int(float(pid)))
            sq[pid] += fnum(r.get("Quantity")) or 0.0
            sv[pid] += v


    def mult(p):
        v = p.get("VolumeOfSale")
        return int(v) if v not in (None, 0) else 1


    def ppu_list(p, m):
        s = p.get("SalePriceIncTax") or 0
        return s / m if s and m else None


    def realized_ppu(pid, m):
        q = sq.get(pid, 0.0)
        if q > 0 and sv.get(pid, 0) > 0:
            return sv[pid] / q / m
        return None


    def group_of(reasons):
        r = re.sub(r";?\s*STAFF_NOTE\[.*?\]", "", reasons).strip()
        if r.startswith("multiplier 1 implied"):
            return "B1 implied x1 single (child VoS null)"
        if r.startswith("owner cost missing"):
            return "B2 owner cost missing"
        if r.startswith("VolumeOfSale disagrees"):
            return "B3 owner VoS disagrees with name *N"
        if r.startswith("stock row inconsistent"):
            return "B4 stock row inconsistent with VoS"
        if r.startswith("cost off"):
            return "B5 child cost 10-25% off"
        if r.startswith("sale tax group differs"):
            return "B6 sale tax group differs"
        return "B? " + r


    def check(prow):
        """Deterministic checks. Returns (fail_codes, metrics)."""
        pid = prow["EPOS Product ID"]
        e = ev[pid]
        fails, met = [], {}
        p = cat.get(int(pid))
        if p is None:
            return ["CATALOGUE_MISSING"], met
        try:
            m = Decimal(prow["Staff Approved Sale Multiplier"])
        except Exception:
            return ["MULTIPLIER_BLANK"], met
        if prow["Staff Approved Purchase Multiplier"] != prow["Staff Approved Sale Multiplier"]:
            fails.append("PURCHASE_MULT_DIFFERS")
        sku = prow["Target QBO SKU"]
        owner_id = e["Owner Product ID"]
        f = fam.get(sku)
        if (prow["Target QBO Item Type"] != "Inventory" or sku != f"AKP-{owner_id}" or prow["Canonical Family Key"] != sku
                or f is None or prow["Target QBO Name"] != f["Proposed QBO Name"] or prow["Canonical Unit"] != f["Canonical unit"]
                or prow["Effective Date"] != EFFECTIVE or clean(prow["Target QBO Item Id"])):
            fails.append("PROPOSAL_FIELDS_INCONSISTENT")
        o = cat.get(int(owner_id)) if owner_id else None
        if o is None:
            return fails + ["OWNER_MISSING"], met
        om = mult(o)
        met["om"] = om
        if e["Role"] == "STOCK_OWNER":
            if not p.get("IsStockTracked"):
                fails.append("OWNER_NOT_TRACKED")
            if m != mult(p):
                fails.append("OWNER_MULT_NOT_VOS")
            n = suffix(p["Name"])
            if n and p.get("VolumeOfSale") and n != p["VolumeOfSale"]:
                # Nested VoS (e.g. carton *24 counted in 960 pieces) makes the canonical unit an inner piece that is
                # not sold separately; runbook: canonical unit = smallest separately sold/counted unit -> staff confirm.
                tag = "NESTED" if p["VolumeOfSale"] % n == 0 else "NOT_NESTED"
                fails.append(f"OWNER_NAME_N{n}_VS_VOS{p['VolumeOfSale']}_{tag}")
            if f and any(x in f["Flags"] for x in ("STOCK_VOS_INCONSISTENT", "LOOSE_STOCK_WITHOUT_VOS", "MULTIPLE_STOCK_ROWS", "TRACKED_SIBLING", "DUPLICATE_EPOS_NAME")):
                fails.append("OWNER_STOCK_ROW_OR_SIBLING_ISSUE")
            return fails, met
        # child
        if p.get("IsStockTracked"):
            fails.append("CHILD_IS_TRACKED")
        if m != mult(p):
            fails.append("CHILD_MULT_NOT_VOS_OR_1")
        if m > om:
            fails.append("CHILD_MULT_GT_OWNER")
        if base_key(p["Name"]) != base_key(o["Name"]):
            fails.append("NAME_BASE_NOT_EXACT")
        n = suffix(p["Name"])
        if n is not None and Decimal(n) != m:
            fails.append(f"NAME_SUFFIX_{n}_VS_MULT_{m}")
        oc, cc = o.get("CostPriceExTax") or 0, p.get("CostPriceExTax") or 0
        if oc > 0 and cc > 0:
            exp = oc * float(m) / om
            dev = abs(cc - exp) / exp
            met["cost_dev"] = dev
            if dev > COST_TOL:
                fails.append(f"COST_OFF_{dev*100:.1f}%")
        else:
            fails.append("COST_MISSING")
        if (p.get("SalePriceTaxGroupName") or "") != (o.get("SalePriceTaxGroupName") or ""):
            fails.append("SALE_TAX_GROUP_DIFFERS")
        op = ppu_list(o, om)
        cp = ppu_list(p, float(m))
        if op and cp:
            r = cp / op
            met["price_ratio"] = r
            if not (PRICE_LO <= r <= PRICE_HI):
                fails.append(f"LIST_PRICE_PER_UNIT_RATIO_{r:.2f}")
        rp = realized_ppu(pid, float(m))
        if rp and op:
            r2 = rp / op
            met["sep_ratio"] = r2
            if not (PRICE_LO <= r2 <= PRICE_HI):
                fails.append(f"SEP_PRICE_PER_UNIT_RATIO_{r2:.2f}")
        return fails, met


    results = {}
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        t = ev[pid]["Tier"]
        if t in ("A", "B"):
            results[pid] = check(prow)

    # ---- sampling of B
    rng = random.Random(seed)
    groups = defaultdict(list)
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        if ev[pid]["Tier"] == "B":
            groups[group_of(ev[pid]["Reasons"])].append(pid)
    samples, group_result = {}, {}
    for g in sorted(groups):
        ids = sorted(groups[g], key=int)
        k = min(len(ids), max(30, math.ceil(0.10 * len(ids))))
        s = sorted(rng.sample(ids, k), key=int)
        samples[g] = s
        errs = [i for i in s if results[i][0]]
        group_result[g] = (len(ids), k, errs)

    # ---- decisions (owners first, then children need approved owner)
    decision = {}  # pid -> (approved bool, reason)
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        e = ev[pid]
        if e["Tier"] == "C":
            decision[pid] = (False, "C: " + e["Reasons"])
            continue
        if e["Role"] != "STOCK_OWNER":
            continue
        fails = results[pid][0]
        if fails:
            decision[pid] = (False, f"DEMOTED_FROM_{e['Tier']}: " + ", ".join(fails))
        else:
            decision[pid] = (True, "")
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        e = ev[pid]
        if pid in decision:
            continue
        fails = list(results[pid][0])
        if not decision.get(e["Owner Product ID"], (False,))[0]:
            fails.append("OWNER_NOT_APPROVED")
        decision[pid] = (False, f"DEMOTED_FROM_{e['Tier']}: " + ", ".join(fails)) if fails else (True, "")

    # ---- outputs
    approved_rows = []
    for prow in proposal:
        if decision[prow["EPOS Product ID"]][0]:
            r = dict(prow)
            r["Review Status"] = "Approved"
            r["Approved By"] = approved_by
            r["Effective Date"] = EFFECTIVE
            r["Target QBO Item Id"] = ""
            approved_rows.append(r)
    with open(out / "approved_mapping_staging.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=prop_fields, lineterminator="\n")
        w.writeheader()
        w.writerows(approved_rows)

    blocked = []
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        ok, why = decision[pid]
        if ok:
            continue
        e = ev[pid]
        b = blk.get(pid, {})
        status = "C_ORIGINAL" if e["Tier"] == "C" else f"DEMOTED_FROM_{e['Tier']}"
        q = b.get("Question for staff", "")
        if not q:
            if "OWNER_NOT_APPROVED" in why:
                q = "Owner family is blocked; resolve the owner first, then confirm this row's multiplier."
            elif e["Role"] == "STOCK_OWNER":
                q = "Confirm the pack size (VolumeOfSale) and counting unit of this tracked product before it is created."
            else:
                q = (f"Confirm this product deducts from owner {e['Owner Product ID']} '{e['Owner Name']}' and how many "
                     f"canonical units one sale removes (proposed {prow['Staff Approved Sale Multiplier'] or '?'}).")
        blocked.append({
            "Status": status, "EPOS Product ID": pid, "EPOS Name": e["EPOS Name"], "Role": e["Role"], "Original Tier": e["Tier"],
            "Block reason": why.split(": ", 1)[1] if ": " in why else why,
            "Owner Product ID": e["Owner Product ID"], "Owner Name": e["Owner Name"], "Family SKU": e["Family SKU"],
            "Proposed sale multiplier": e["Proposed sale multiplier (canonical units)"], "EPOS VolumeOfSale": e["EPOS VolumeOfSale"],
            "Implied multiplier from cost": e["Implied multiplier from cost"], "Cost ex tax": e["Cost ex tax"],
            "Sale price inc tax": e["Sale price inc tax"], "Sell on till": e["Sell on till"],
            "Sep 1-25 lines": e["Sep coverage lines"], "Sep 1-25 value": e["Sep coverage value"],
            "Suggestion (unverified)": e["Suggestion (unverified)"], "Question for staff": q, "Staff answer": "", "Answered by": "",
        })
    blocked.sort(key=lambda r: (-(fnum(r["Sep 1-25 value"]) or 0), int(r["EPOS Product ID"])))
    with open(out / "still_blocked.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(blocked[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(blocked)

    # families approved
    child_counts = defaultdict(lambda: [0, 0])
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        e = ev[pid]
        if e["Role"] == "CHILD":
            child_counts[e["Family SKU"]][0 if decision[pid][0] else 1] += 1
    fa = []
    for f in families:
        if decision[f["Owner EPOS Product ID"]][0]:
            r = dict(f)
            r["Children approved"], r["Children blocked"] = child_counts[f["Family SKU"]]
            qty = fnum(f["Opening canonical qty (zero floor)"]) or 0
            notes = []
            if "MISSING_COST" in f["Flags"] and qty > 0:
                notes.append("COST_TO_SUPPLY: positive opening qty at cost 0")
            elif "MISSING_COST" in f["Flags"]:
                notes.append("COST_MISSING (opening qty 0)")
            if "STOCK_REPORT_COST_ZERO" in f["Flags"] and qty > 0:
                notes.append("CONFIRM_QTY: stock-report cost 0 (ghost-looking qty)")
            r["Pre-W7 action"] = "; ".join(notes)
            fa.append(r)
    fa_fields = fam_fields + ["Children approved", "Children blocked", "Pre-W7 action"]
    with open(out / "families_approved.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fa_fields, lineterminator="\n")
        w.writeheader()
        w.writerows(fa)

    # stats for log
    stats = {"tot_val": tot_val, "groups": {g: (n, k, errs) for g, (n, k, errs) in group_result.items()}}
    cnt = Counter()
    val = Counter()
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        t = ev[pid]["Tier"]
        ok = decision[pid][0]
        cnt[(t, ok)] += 1
        val[(t, ok)] += sv.get(pid, 0.0)
    stats["cnt"] = {f"{k[0]}_{'approved' if k[1] else 'blocked'}": v for k, v in cnt.items()}
    stats["val"] = {f"{k[0]}_{'approved' if k[1] else 'blocked'}": v for k, v in val.items()}
    stats["sep_total_value_file"] = tot_val
    stats["families_approved"] = len(fa)
    stats["families_opening_value"] = sum(fnum(f["Opening value ex tax (catalogue cost)"]) or 0 for f in fa)
    stats["families_opening_qty"] = sum(fnum(f["Opening canonical qty (zero floor)"]) or 0 for f in fa)
    dem = [b for b in blocked if b["Status"].startswith("DEMOTED")]
    stats["demoted"] = len(dem)
    codes = Counter()
    for b in dem:
        for c in b["Block reason"].split(", "):
            codes[re.sub(r"_[0-9.]+%?$|_RATIO_.*$|_\d+_VS_MULT_.*$|_N\d+_VS_VOS.*$", "", c)] += 1
    stats["demotion_codes"] = codes
    stats["dem_by_tier_role"] = Counter(b["Original Tier"] + "_" + b["Role"] for b in dem)
    stats["pre_w7"] = Counter(x for r in fa for x in r["Pre-W7 action"].split("; ") if x)
    stats["pre_w7_value"] = {k: sum(fnum(r["Opening value ex tax (catalogue cost)"]) or 0 for r in fa if k in r["Pre-W7 action"]) for k in ("CONFIRM_QTY",)}
    stats["pre_w7_qty_costzero"] = sum(fnum(r["Opening canonical qty (zero floor)"]) or 0 for r in fa if "COST_TO_SUPPLY" in r["Pre-W7 action"])
    json.dump({"stats": stats, "samples": samples, "results": {k: v for k, v in results.items()}}, open(out / "review_out.json", "w"), default=str, indent=1)
    print(json.dumps(stats, default=str, indent=1))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, default=GAPS_DIR / "canonical_2026-09-26_1937",
                    help="build_canonical output folder (default: %(default)s)")
    ap.add_argument("--catalogue", type=Path, default=None,
                    help="catalogue_products.json used for --base (default: the input recorded in <base>/summary.json)")
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    ap.add_argument("--sales", type=Path, default=None,
                    help="Sep coverage BookKeeping CSV (default: <evidence-root>/As of 25th September/BookKeeping_2026_09_25_1245.csv)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="tier-B sampling seed (default %(default)s)")
    ap.add_argument("--approved-by", default=DEFAULT_APPROVED_BY)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/review_approval_<timestamp>/)")
    a = ap.parse_args(argv)
    catalogue = a.catalogue
    if catalogue is None:
        rec = Path(json.load(open(a.base / "summary.json"))["inputs"]["catalogue"]["path"])
        catalogue = rec if rec.is_absolute() else REPO_ROOT / rec
    sales = a.sales or a.evidence_root / "As of 25th September" / "BookKeeping_2026_09_25_1245.csv"
    out = resolve_out(a.out, TOOL)
    review(a.base, catalogue, sales, out, a.seed, a.approved_by)
    print("->", out)


if __name__ == "__main__":
    main()

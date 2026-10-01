"""Final EPOS -> QBO mapping for AKPONORA / NORA MINI MART (company_a).

OFFLINE: files only, no QBO/EPOS calls. Applies the owner/accountant decisions of 26 Sep 2026 (chat):
  (a) rows already approved (approval/approved_mapping_staging.csv) stay approved unchanged;
  (b) blocked CHILD / STOCK_OWNER rows with a tracked stock owner are approved with the multiplier EPOS
      actually uses (EPOS VolumeOfSale, or 1 when null) and listed in pricing_review.csv;
  (c) STANDALONE_UNTRACKED rows are approved to a dedicated NonInventory item per product
      (SKU AKP-NS-{EPOS ProductID}, name = EPOS name normalised, multiplier 1);
  (d) CONFIRM_QTY / COST_MISSING families are approved; create-list cost = current catalogue cost
      (owner CostPriceExTax / owner multiplier); flagged in opening_qty_review.csv;
  (e) any product absent from the latest catalogue is dropped.

Inputs: --canonical (build_canonical output), --approval (review_approval output, default
<canonical>/approval), --catalogue and --sales (must match the sha256 recorded in
<canonical>/summary.json), --items-dump (QBO Item GET dump JSON list) and --w5-plan (W5 plan.csv).
Outputs (in --out): approved_mapping_final.csv, create_list_inventory.csv, create_list_noninventory.csv,
pricing_review.csv, opening_qty_review.csv, account_mapping.csv, w5_additional_collisions.csv, stats.json.

Example (reproduces outputs/nora_gaps_2026-09-25/final_mapping_2026-09-26):
  python -m code_scripts.scripts.akponora_cutover.build_final_mapping --out <dir>
"""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import DEFAULT_EVIDENCE_ROOT, GAPS_DIR, resolve_out

TOOL = "build_final_mapping"
CONTRACT_HEADER = ["Row ID", "EPOS Product ID", "EPOS Existing SKU", "EPOS Name", "Pipeline Status", "Review Status",
                   "Target QBO Item Type", "Target QBO Name", "Target QBO SKU", "Target QBO Item Id",
                   "Staff Approved Sale Multiplier", "Effective Date", "Approved By", "Canonical Family Key",
                   "Canonical Unit", "Staff Approved Purchase Multiplier"]
EFFECTIVE = "2026-10-01"
DEFAULT_APPROVED_BY = "Marvin Mokolo (owner decision chat 2026-09-26, accountant rules b-d); prepared by Claude"
NS_PREFIX = "AKP-NS-"
INVENTORY_ASSET_ACCOUNT = ("77", "Inventory Asset")
TAX_CODE = {"VAT": ("2", "7.5% S"), "NoTax": ("7", "No VAT")}
FALLBACK_CATEGORY = "HOUSEHOLD GOODS & PACKAGING MATERIALS"  # for the 1 EPOS product with no category (non-food)
SUFFIX_RE = re.compile(r"\s*\*\s*(\d+)\s*$")


def clean(v):
    return " ".join(str(v if v is not None else "").split())


def norm(v):
    return clean(v).casefold()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def write(out, name, fields, rows):
    with open(Path(out) / name, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n", extrasaction="raise")
        w.writeheader()
        w.writerows(rows)


def D(v):
    v = clean(v).replace(",", "")
    return Decimal(v) if v else Decimal(0)


def q(v, places="0.00001"):
    return str(Decimal(v).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def mult(p):
    v = p.get("VolumeOfSale")
    return int(v) if v not in (None, 0) else 1


def ns_name(n):
    return clean(str(n).replace(":", "-"))[:100].strip()


def build(canonical, approval, catalogue_path, items_dump, w5_plan, sales, out, approved_by=DEFAULT_APPROVED_BY):
    canonical, approval, catalogue_path, items_dump, w5_plan, sales, out = map(Path, (canonical, approval, catalogue_path, items_dump, w5_plan, sales, out))
    # ---------------------------------------------------------------- inputs
    summary = json.load(open(canonical / "summary.json"))
    assert sha(catalogue_path) == summary["inputs"]["catalogue"]["sha256"], "catalogue changed since canonical run"
    assert sha(sales) == summary["inputs"]["sales_coverage"]["sha256"], "sales file changed since canonical run"
    catalogue = json.load(open(catalogue_path))
    cat = {str(p["Id"]): p for p in catalogue}
    _, proposal = read(canonical / "mapping_proposal.csv")
    _, evidence = read(canonical / "mapping_evidence.csv")
    _, families = read(canonical / "families.csv")
    _, drafts = read(canonical / "qbo_create_draft.csv")
    staging_fields, staging = read(approval / "approved_mapping_staging.csv")
    _, blocked = read(approval / "still_blocked.csv")
    _, fam_approved = read(approval / "families_approved.csv")
    _, w5 = read(w5_plan)
    items = json.load(open(items_dump))
    assert staging_fields == CONTRACT_HEADER
    ev = {r["EPOS Product ID"]: r for r in evidence}
    fam = {r["Family SKU"]: r for r in families}
    draft = {r["Sku"]: r for r in drafts}
    stg = {r["EPOS Product ID"]: r for r in staging}
    blk = {r["EPOS Product ID"]: r for r in blocked}
    fam_pre = {r["Family SKU"]: r for r in fam_approved}
    assert not set(stg) & set(blk) and len(stg) + len(blk) == len(proposal)

    # ---------------------------------------------------------------- Sep 1-25 sales per product id
    sales_val, sales_lines, sales_name = defaultdict(Decimal), Counter(), {}
    sales_total, sales_lines_total = Decimal(0), 0
    for r in read(sales)[1]:
        if clean(r.get("Staff")) == "Total:" or not clean(r.get("Product")):
            continue
        v = D(r.get("TOTAL Sales"))
        sales_total += v
        sales_lines_total += 1
        pid = clean(r.get("ProductId"))
        pid = str(int(float(pid))) if pid else ""
        sales_val[pid] += v
        sales_lines[pid] += 1
        sales_name.setdefault(pid, clean(r.get("Product")))

    # ---------------------------------------------------------------- category -> account mapping (legacy pattern)
    legacy_inv = [i for i in items if i.get("Active") and i.get("Type") == "Inventory"]
    inc_by_cat, exp_by_cat = defaultdict(Counter), defaultdict(Counter)
    for i in legacy_inv:
        c = (i.get("ParentRef") or {}).get("name")
        if not c:
            continue
        ir, er = i.get("IncomeAccountRef") or {}, i.get("ExpenseAccountRef") or {}
        if ir:
            inc_by_cat[c][(ir["value"], ir["name"])] += 1
        if er:
            exp_by_cat[c][(er["value"], er["name"])] += 1
    # cross-check: legacy leaf name -> EPOS catalogue category
    epos_cat_by_name = {}
    for p in catalogue:
        epos_cat_by_name.setdefault(norm(p["Name"]), p.get("CategoryName"))
    xc_inc = defaultdict(Counter)
    for i in legacy_inv:
        c = epos_cat_by_name.get(norm(i["Name"]))
        if c and i.get("IncomeAccountRef"):
            xc_inc[c][(i["IncomeAccountRef"]["value"], i["IncomeAccountRef"]["name"])] += 1
    acct = {}
    acct_rows = []
    epos_cats = sorted({p.get("CategoryName") for p in catalogue if p.get("CategoryName")})
    for c in epos_cats:
        (iid, iname), icnt = inc_by_cat[c].most_common(1)[0]
        (eid, ename), ecnt = exp_by_cat[c].most_common(1)[0]
        assert ename.startswith("200"), (c, ename)
        xi = xc_inc[c].most_common(1)[0][0] if xc_inc[c] else ("", "")
        acct[c] = {"inc": (iid, iname), "exp": (eid, ename)}
        acct_rows.append({
            "EPOS Category": c,
            "Income account Id": iid, "Income account": iname,
            "Income share (legacy active Inventory under this QBO category)": f"{icnt}/{sum(inc_by_cat[c].values())}",
            "Income cross-check via EPOS name match": "agrees" if xi[0] == iid else f"differs: {xi[1]}",
            "Purchase/COGS account Id (200xxx)": eid, "Purchase/COGS account": ename,
            "Expense share (legacy active Inventory under this QBO category)": f"{ecnt}/{sum(exp_by_cat[c].values())}",
            "New Inventory items: AssetAccountRef": INVENTORY_ASSET_ACCOUNT[0],
            "New Inventory items: ExpenseAccountRef (COGS)": eid,
            "New NonInventory items: ExpenseAccountRef (purchase)": eid,
            "New items: IncomeAccountRef": iid,
        })
    acct_rows.append({
        "EPOS Category": "(none in EPOS) -> fallback " + FALLBACK_CATEGORY,
        "Income account Id": acct[FALLBACK_CATEGORY]["inc"][0], "Income account": acct[FALLBACK_CATEGORY]["inc"][1],
        "Income share (legacy active Inventory under this QBO category)": "n/a (1 EPOS product, ERSIN BABY SHAWL; accountant to confirm)",
        "Income cross-check via EPOS name match": "",
        "Purchase/COGS account Id (200xxx)": acct[FALLBACK_CATEGORY]["exp"][0], "Purchase/COGS account": acct[FALLBACK_CATEGORY]["exp"][1],
        "Expense share (legacy active Inventory under this QBO category)": "n/a",
        "New Inventory items: AssetAccountRef": INVENTORY_ASSET_ACCOUNT[0],
        "New Inventory items: ExpenseAccountRef (COGS)": acct[FALLBACK_CATEGORY]["exp"][0],
        "New NonInventory items: ExpenseAccountRef (purchase)": acct[FALLBACK_CATEGORY]["exp"][0],
        "New items: IncomeAccountRef": acct[FALLBACK_CATEGORY]["inc"][0],
    })


    def accounts_for(category):
        return acct[category or FALLBACK_CATEGORY]


    # ---------------------------------------------------------------- mapping rows
    final_rows, pricing_rows, rule_of = [], [], {}
    dropped = []
    for prow in proposal:
        pid = prow["EPOS Product ID"]
        if pid not in cat:  # rule (e)
            dropped.append(pid)
            continue
        p = cat[pid]
        e = ev[pid]
        if pid in stg:  # rule (a)
            r = dict(stg[pid])
            assert r["Review Status"] == "Approved" and r["Target QBO Item Type"] == "Inventory"
            rule_of[pid] = "a"
            final_rows.append(r)
            continue
        b = blk[pid]
        role = e["Role"]
        if role in ("CHILD", "STOCK_OWNER"):  # rule (b)
            owner = e["Owner Product ID"]
            assert owner and owner in cat and cat[owner].get("IsStockTracked"), (pid, owner)
            f = fam["AKP-" + owner]
            m = mult(p)
            if role == "STOCK_OWNER":
                assert owner == pid and str(m) == f["Owner multiplier (canonical units per owner unit)"], pid
            else:
                assert not p.get("IsStockTracked"), pid
            r = {k: prow[k] for k in CONTRACT_HEADER}
            r.update({"Pipeline Status": "OWNER_RULE_B", "Review Status": "Approved", "Target QBO Item Type": "Inventory",
                      "Target QBO Name": f["Proposed QBO Name"], "Target QBO SKU": f["Family SKU"], "Target QBO Item Id": "",
                      "Staff Approved Sale Multiplier": str(m), "Effective Date": EFFECTIVE, "Approved By": approved_by,
                      "Canonical Family Key": f["Family SKU"], "Canonical Unit": f["Canonical unit"],
                      "Staff Approved Purchase Multiplier": str(m)})
            rule_of[pid] = "b"
            final_rows.append(r)
            o = cat[owner]
            om = mult(o)
            n = SUFFIX_RE.search(clean(p["Name"]))
            notes = []
            if n and int(n.group(1)) != m:
                notes.append(f"name suffix *{n.group(1)} but EPOS deducts {m}")
            if role == "CHILD" and m > om:
                notes.append(f"child multiplier {m} > owner multiplier {om}")
            oc, cc = o.get("CostPriceExTax") or 0, p.get("CostPriceExTax") or 0
            exp_cost = Decimal(str(oc)) * m / om if oc else None
            pricing_rows.append({
                "EPOS Product ID": pid, "EPOS Name": p["Name"], "Role": role, "Original status": b["Status"],
                "Block reason (why it was blocked)": b["Block reason"],
                "Owner Product ID": owner, "Owner Name": o["Name"], "Target QBO SKU": f["Family SKU"],
                "Target QBO Name": f["Proposed QBO Name"], "Canonical Unit": f["Canonical unit"],
                "EPOS VolumeOfSale": p.get("VolumeOfSale") if p.get("VolumeOfSale") is not None else "",
                "Approved multiplier (what EPOS deducts)": m, "Owner multiplier": om,
                "Implied multiplier from cost": e["Implied multiplier from cost"],
                "Cost ex tax": cc, "Expected cost at approved multiplier": q(exp_cost, "0.01") if exp_cost is not None else "",
                "Sale price inc tax": p.get("SalePriceIncTax"),
                "Sep 1-25 lines": sales_lines.get(pid, 0), "Sep 1-25 value": q(sales_val.get(pid, 0), "0.01"),
                "Notes": "; ".join(notes),
                "Action": "Not a blocker. Mapping follows EPOS deduction; fix EPOS price/cost later if wrong.",
            })
            continue
        assert role == "STANDALONE_UNTRACKED" and not p.get("IsStockTracked"), pid  # rule (c)
        r = {k: prow[k] for k in CONTRACT_HEADER}
        r.update({"Pipeline Status": "OWNER_RULE_C", "Review Status": "Approved", "Target QBO Item Type": "NonInventory",
                  "Target QBO Name": ns_name(p["Name"]), "Target QBO SKU": NS_PREFIX + pid, "Target QBO Item Id": "",
                  "Staff Approved Sale Multiplier": "1", "Effective Date": EFFECTIVE, "Approved By": approved_by,
                  "Canonical Family Key": NS_PREFIX + pid, "Canonical Unit": "Each",
                  "Staff Approved Purchase Multiplier": "1"})
        rule_of[pid] = "c"
        final_rows.append(r)

    final_rows.sort(key=lambda r: int(r["EPOS Product ID"]))

    # NonInventory names must be unique among all new items (QBO FullyQualifiedName; new items have no parent).
    # EPOS has a few products sharing one name; those NonInventory names get " #{EPOS ProductID}" (W5 convention).
    name_pool = Counter(norm(n) for n in {r["Target QBO Name"] for r in final_rows if r["Target QBO Item Type"] == "Inventory"})
    name_pool.update(norm(r["Target QBO Name"]) for r in final_rows if r["Target QBO Item Type"] == "NonInventory")
    ns_disambiguated = []
    for r in final_rows:
        if r["Target QBO Item Type"] == "NonInventory" and name_pool[norm(r["Target QBO Name"])] > 1:
            tag = " #" + r["EPOS Product ID"]
            r["Target QBO Name"] = r["Target QBO Name"][:100 - len(tag)].rstrip() + tag
            ns_disambiguated.append(r["EPOS Product ID"])
    assert len(final_rows) == len(cat) and all(r["Review Status"] == "Approved" and not r["Target QBO Item Id"] for r in final_rows)

    # ---------------------------------------------------------------- inventory create list
    inv_skus = sorted({r["Target QBO SKU"] for r in final_rows if r["Target QBO Item Type"] == "Inventory"})
    assert set(inv_skus) <= set(fam)
    owner_rows = {r["EPOS Product ID"]: r for r in final_rows}
    inv_create, opening_rows = [], []
    for sku in inv_skus:
        f = fam[sku]
        owner = f["Owner EPOS Product ID"]
        assert owner_rows[owner]["Target QBO SKU"] == sku, sku  # owner itself maps to its family
        o = cat[owner]
        om = Decimal(f["Owner multiplier (canonical units per owner unit)"])
        assert om == mult(o)
        unit_cost = Decimal(str(o.get("CostPriceExTax") or 0)) / om
        unit_price = Decimal(str(o.get("SalePriceIncTax") or 0)) / om
        a = accounts_for(f["Category"])
        stax, ctax = o.get("SalePriceTaxGroupName") or "NoTax", o.get("CostPriceTaxGroupName") or "NoTax"
        flags = f["Flags"]
        prov_qty = D(f["Opening canonical qty (zero floor)"])
        review = []
        if "STOCK_REPORT_COST_ZERO" in flags and prov_qty > 0:
            review.append("CONFIRM_QTY")
        if "MISSING_COST" in flags:
            review.append("COST_MISSING")
        if any(x in flags for x in ("STOCK_VOS_INCONSISTENT", "MULTIPLE_STOCK_ROWS", "NO_STOCK_ROW")) and sku not in fam_pre:
            review.append("STOCK_ROW_ISSUE")
        d = draft[sku]
        inv_create.append({
            "Name": f["Proposed QBO Name"], "Sku": sku, "Type": "Inventory", "TrackQtyOnHand": "TRUE",
            "QtyOnHand": "", "QtyOnHand source": "30 Sep 2026 count (not this file)",
            "Provisional qty (25 Sep stock report, zero floor)": f["Opening canonical qty (zero floor)"],
            "InvStartDate": EFFECTIVE, "PurchaseCost": q(unit_cost),
            "PurchaseCost basis": f"current catalogue CostPriceExTax {o.get('CostPriceExTax')} / owner multiplier {om} (ex-tax, cost tax group {ctax})",
            "PurchaseTaxIncluded": "FALSE", "UnitPrice": q(unit_price, "0.01"), "SalesTaxIncluded": "TRUE",
            "Taxable": "TRUE" if stax == "VAT" else "FALSE",
            "SalesTaxCodeId": TAX_CODE.get(stax, TAX_CODE["NoTax"])[0], "PurchaseTaxCodeId": TAX_CODE.get(ctax, TAX_CODE["NoTax"])[0],
            "AssetAccountId": INVENTORY_ASSET_ACCOUNT[0],
            "IncomeAccountId": a["inc"][0], "IncomeAccount": a["inc"][1],
            "ExpenseAccountId (COGS)": a["exp"][0], "ExpenseAccount (COGS)": a["exp"][1],
            "EPOS Category": f["Category"] or f"(none; fallback {FALLBACK_CATEGORY})",
            "Canonical Unit": f["Canonical unit"], "Owner EPOS Product ID": owner, "Owner EPOS Name": o["Name"],
            "Owner multiplier": str(om),
            "Description": d["Description"], "ParentRef": "", "SubItem": "FALSE",
            "Approval": "rule a (pre-approved family)" if sku in fam_pre else "rule b (family approved 26 Sep owner decision)",
            "Review flags": "; ".join(review), "Canonical flags": flags,
            "Live active collision Ids (W5 plan)": d["Collides with live active QBO Id"],
        })
        if review:
            opening_rows.append({
                "Family SKU": sku, "QBO Name": f["Proposed QBO Name"], "Owner EPOS Product ID": owner,
                "Owner EPOS Name": o["Name"], "Category": f["Category"], "Canonical Unit": f["Canonical unit"],
                "Review reason": "; ".join(review),
                "Provisional qty (25 Sep, zero floor)": f["Opening canonical qty (zero floor)"],
                "Raw qty (25 Sep stock report)": f["Raw canonical qty (stock report)"],
                "Stock report MeasuredCostPrice": f["Stock report MeasuredCostPrice"],
                "Create-list unit cost (current catalogue)": q(unit_cost),
                "Provisional value at create-list cost": q(prov_qty * unit_cost, "0.01"),
                "Sep 1-25 family sales value": f["Sep 1-25 family sales value"], "Canonical flags": flags,
                "Action": ("Opening qty comes from the 30 Sep count; confirm the product physically exists and supply a "
                           "cost if it is 0 before the opening is posted."),
            })

    # ---------------------------------------------------------------- non-inventory create list
    ni_create = []
    for r in final_rows:
        if r["Target QBO Item Type"] != "NonInventory":
            continue
        pid = r["EPOS Product ID"]
        p = cat[pid]
        a = accounts_for(p.get("CategoryName"))
        stax, ctax = p.get("SalePriceTaxGroupName") or "NoTax", p.get("CostPriceTaxGroupName") or "NoTax"
        ni_create.append({
            "Name": r["Target QBO Name"], "Sku": r["Target QBO SKU"], "Type": "NonInventory", "TrackQtyOnHand": "FALSE",
            "UnitPrice": q(p.get("SalePriceIncTax") or 0, "0.01"), "SalesTaxIncluded": "TRUE",
            "Taxable": "TRUE" if stax == "VAT" else "FALSE",
            "SalesTaxCodeId": TAX_CODE.get(stax, TAX_CODE["NoTax"])[0],
            "PurchaseCost": q(p.get("CostPriceExTax") or 0), "PurchaseTaxIncluded": "FALSE",
            "PurchaseTaxCodeId": TAX_CODE.get(ctax, TAX_CODE["NoTax"])[0],
            "IncomeAccountId": a["inc"][0], "IncomeAccount": a["inc"][1],
            "ExpenseAccountId (purchase)": a["exp"][0], "ExpenseAccount (purchase)": a["exp"][1],
            "EPOS Category": p.get("CategoryName") or "", "EPOS Product ID": pid, "EPOS Name": p["Name"],
            "Description": f"EPOS non-stock product {pid} {clean(p['Name'])}", "ParentRef": "", "SubItem": "FALSE",
            "Sep 1-25 lines": sales_lines.get(pid, 0), "Sep 1-25 value": q(sales_val.get(pid, 0), "0.01"),
            "Name changed from EPOS": "yes" if r["Target QBO Name"] != p["Name"] else "",
        })

    # ---------------------------------------------------------------- name checks + collisions
    new_names = [(x["Name"], x["Sku"], "Inventory") for x in inv_create] + [(x["Name"], x["Sku"], "NonInventory") for x in ni_create]
    dup = Counter(norm(n) for n, _, _ in new_names)
    dups = sorted(k for k, v in dup.items() if v > 1)
    bad_names = [n for n, _, _ in new_names if len(n) > 100 or ":" in n or n != clean(n) or not n]
    sku_dup = [k for k, v in Counter(s for _, s, _ in new_names).items() if v > 1]
    w5_ids = {r["Id"] for r in w5}
    live_active = [i for i in items if i.get("Active")]
    by_fqn, by_leaf = defaultdict(list), defaultdict(list)
    for i in live_active:
        by_fqn[norm(i["FullyQualifiedName"])].append(i)
        by_leaf[norm(i["Name"])].append(i)
    extra, covered = [], Counter()
    for n, sku, typ in new_names:
        k = norm(n)
        seen = set()
        for kind, pool in (("FQN_HARD", by_fqn.get(k, [])), ("LEAF_SOFT", by_leaf.get(k, []))):
            for i in pool:
                if i["Id"] in seen:
                    continue
                seen.add(i["Id"])
                if i["Id"] in w5_ids:
                    covered[typ] += 1
                    continue
                extra.append({"New Name": n, "New Sku": sku, "New Type": typ, "Collision kind": kind,
                              "Live Id": i["Id"], "Live Type": i["Type"], "Live Name": i["Name"],
                              "Live FullyQualifiedName": i["FullyQualifiedName"],
                              "Live Sku": i.get("Sku", ""), "Live QtyOnHand": i.get("QtyOnHand", ""),
                              "Why it matters": ("QBO refuses the create (Duplicate Name 6240)" if kind == "FQN_HARD"
                                                 else "create allowed, but name lookups in qbo_upload.py would return 2 items"),
                              "Suggested action": "add to W5 rename plan (rename to 'LEGACY — …') before 30 Sep creates"})

    # ---------------------------------------------------------------- coverage (Sep 1-25 till value)
    final_by_pid = {r["EPOS Product ID"]: r for r in final_rows}
    cov = defaultdict(lambda: [0, Decimal(0)])
    for pid, v in sales_val.items():
        if pid in final_by_pid:
            key = {"a": "rule a (pre-approved Inventory)", "b": "rule b (Inventory, pricing review)",
                   "c": "rule c (NonInventory)"}[rule_of[pid]]
        elif not pid:
            key = "no ProductId on sales line"
        else:
            key = "NOT_IN_LATEST_CATALOGUE (archived/deleted; dropped by rule e)"
        cov[key][0] += sales_lines[pid]
        cov[key][1] += v
    residual = [{"ProductId": pid, "Sales name": sales_name.get(pid, ""), "lines": sales_lines[pid], "value": q(v, "0.01")}
                for pid, v in sorted(sales_val.items(), key=lambda kv: -kv[1]) if pid not in final_by_pid]

    # ---------------------------------------------------------------- write
    write(out, "approved_mapping_final.csv", CONTRACT_HEADER, final_rows)
    write(out, "create_list_inventory.csv", list(inv_create[0]), inv_create)
    write(out, "create_list_noninventory.csv", list(ni_create[0]), ni_create)
    pricing_rows.sort(key=lambda r: (-D(r["Sep 1-25 value"]), int(r["EPOS Product ID"])))
    write(out, "pricing_review.csv", list(pricing_rows[0]), pricing_rows)
    opening_rows.sort(key=lambda r: (r["Review reason"], -D(r["Provisional value at create-list cost"])))
    write(out, "opening_qty_review.csv", list(opening_rows[0]), opening_rows)
    write(out, "account_mapping.csv", list(acct_rows[0]), acct_rows)
    extra_fields = ["New Name", "New Sku", "New Type", "Collision kind", "Live Id", "Live Type", "Live Name",
                    "Live FullyQualifiedName", "Live Sku", "Live QtyOnHand", "Why it matters", "Suggested action"]
    write(out, "w5_additional_collisions.csv", extra_fields, extra)

    stats = {
        "generated_from": {"catalogue_sha256": sha(catalogue_path), "sales_sha256": sha(sales),
                           "staging_sha256": sha(approval / "approved_mapping_staging.csv"),
                           "still_blocked_sha256": sha(approval / "still_blocked.csv"),
                           "items_dump_sha256": sha(items_dump), "w5_plan_sha256": sha(w5_plan)},
        "mapping_rows": len(final_rows), "dropped_rule_e": len(dropped),
        "rows_by_rule": dict(Counter(rule_of.values())),
        "rows_by_type": dict(Counter(r["Target QBO Item Type"] for r in final_rows)),
        "rows_by_rule_role": dict(Counter(f"{rule_of[r['EPOS Product ID']]}_{ev[r['EPOS Product ID']]['Role']}" for r in final_rows)),
        "inventory_items_to_create": len(inv_create),
        "inventory_families_newly_approved_rule_b": sum(1 for x in inv_create if x["Approval"].startswith("rule b")),
        "noninventory_items_to_create": len(ni_create),
        "noninventory_names_changed": sum(1 for x in ni_create if x["Name changed from EPOS"]),
        "noninventory_names_disambiguated": ns_disambiguated,
        "pricing_review_rows": len(pricing_rows),
        "pricing_review_with_notes": sum(1 for x in pricing_rows if x["Notes"]),
        "opening_qty_review": dict(Counter(x for r in opening_rows for x in r["Review reason"].split("; "))),
        "opening_qty_review_confirm_qty_value": q(sum(D(r["Provisional value at create-list cost"]) for r in opening_rows if "CONFIRM_QTY" in r["Review reason"]), "0.01"),
        "new_name_duplicates": dups, "bad_names": bad_names, "sku_duplicates": sku_dup,
        "collisions_covered_by_w5": dict(covered), "w5_additional_collisions": len(extra),
        "coverage": {"sales_lines_total": sales_lines_total, "sales_value_total": q(sales_total, "0.01"),
                     "by_bucket": {k: {"lines": v[0], "value": q(v[1], "0.01"),
                                       "value_pct": q(v[1] / sales_total * 100, "0.01")} for k, v in sorted(cov.items())}},
        "residual_products": residual,
    }
    json.dump(stats, open(out / "stats.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in stats.items() if k != "residual_products"}, indent=1, default=str))
    print("residual:", residual)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--canonical", type=Path, default=GAPS_DIR / "canonical_2026-09-26_1937",
                    help="build_canonical output folder (default: %(default)s)")
    ap.add_argument("--approval", type=Path, default=None, help="review_approval output (default <canonical>/approval)")
    ap.add_argument("--catalogue", type=Path, default=GAPS_DIR / "epos_catalogue_2026-09-26_1937" / "catalogue_products.json")
    ap.add_argument("--items-dump", type=Path, default=GAPS_DIR / "w5_legacy_rename" / "items_dump_2026-09-26.json",
                    help="QBO Item dump (JSON list of Item objects)")
    ap.add_argument("--w5-plan", type=Path, default=GAPS_DIR / "w5_legacy_rename" / "plan.csv")
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    ap.add_argument("--sales", type=Path, default=None,
                    help="Sep coverage BookKeeping CSV (default <evidence-root>/As of 25th September/BookKeeping_2026_09_25_1245.csv)")
    ap.add_argument("--approved-by", default=DEFAULT_APPROVED_BY)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/build_final_mapping_<timestamp>/)")
    a = ap.parse_args(argv)
    sales = a.sales or a.evidence_root / "As of 25th September" / "BookKeeping_2026_09_25_1245.csv"
    out = resolve_out(a.out, TOOL)
    build(a.canonical, a.approval or a.canonical / "approval", a.catalogue, a.items_dump, a.w5_plan, sales, out,
          a.approved_by)
    print("->", out)


if __name__ == "__main__":
    main()

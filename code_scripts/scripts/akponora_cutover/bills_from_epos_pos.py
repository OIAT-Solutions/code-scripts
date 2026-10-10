"""Draft supplier bills from EPOS Purchase Orders for AKPONORA (company_a). Never posts.

Subcommands:
  capture-pos  VIEW-ONLY EPOS: opens the Purchase Orders list for --from..--to and each PO's
               Details view; saves po_list_raw.json (the list JSON the page loads) and
               po_details.jsonl (each Details page's order model). Clicks only login, the date
               filter + Apply and Details. Resumable.
  capture-qbo  READ-ONLY QBO GETs: Bills/Purchases for --from..--to, Vendors, Accounts, TaxCodes
               and per-vendor bill counts since --counts-from -> qbo_snapshot.json.
  parse-pdf    Parse text dumps (pdftotext -layout) of EPOS "Purchase Order" PDF lists into
               pdf_pos.json (cross-check of PO totals). Offline.
  build        OFFLINE: match received POs to QBO bills and draft one bill per unbilled PO
               (account lines on 200100/200201/200202/200300 only, AGENTS.md freeze). Writes
               po_received_<YYYY-MM>.csv, po_bill_match_<YYYY-MM>.csv, bills_to_enter.csv,
               bills_to_enter_totals.csv and summary.json.

Example (reproduces outputs/nora_gaps_2026-09-25/bills_draft_2026-09 from its evidence/):
  python -m code_scripts.scripts.akponora_cutover.bills_from_epos_pos build --out <dir>
Fresh month:
  python -m code_scripts.scripts.akponora_cutover.bills_from_epos_pos capture-pos --from 2026-10-01 --to 2026-10-31 --out <ev>
  python -m code_scripts.scripts.akponora_cutover.bills_from_epos_pos capture-qbo --from 2026-09-15 --to 2026-10-31 --out <ev>
  python -m code_scripts.scripts.akponora_cutover.bills_from_epos_pos build --po-list <ev>/po_list_raw.json \
      --po-details <ev>/po_details.jsonl --qbo-snapshot <ev>/qbo_snapshot.json --catalogue <catalogue_products.json> \
      --window-from 2026-10-01 --window-to 2026-10-31 --open-snap 2026-10-01T00:00 --close-snap 2026-11-01T00:00 \
      --epos-implied 0 --out <dir>
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import (
    GAPS_DIR, ReadOnlyQBO, company_config, dump_json, epos_login, goto_retry, resolve_out,
)

TOOL = "bills_from_epos_pos"
SEP_DIR = GAPS_DIR / "bills_draft_2026-09"
SEP_EV = SEP_DIR / "evidence"

# EPOS category -> purchase account (AGENTS.md freeze: 200100/200201/200202/200300 only)
ACCOUNTS = {
    "200100": ("74", "200000 - Cost of sales:200100 - Purchases - Groceries"),
    "200201": ("1150040033", "200000 - Cost of sales:200200 - Purchases - Drinks:200201 - Alcoholic Drinks"),
    "200202": ("1150040034", "200000 - Cost of sales:200200 - Purchases - Drinks:200202 - Non-Alcoholic Drinks"),
    "200300": ("1150040020", "200000 - Cost of sales:200300 - Purchases - Non - food items"),
}
CATEGORY_ACCOUNT = {
    "ALCOHOLS & SPIRITS": "200201",
    "DRINKS & BEVERAGES": "200202",
    "PROVISIONS AND CEREALS": "200100",
    "CANNED GOOD, COOK OIL, SWALLOW & BAKING": "200100",
    "COOKING SPICES & SEASONINGS": "200100",
    "FROZEN FOODS": "200100",
    "COSMETICS AND TOILETRIES": "200300",
    "HOUSEHOLD GOODS & PACKAGING MATERIALS": "200300",
    "STATIONARY AND BOOKSHOP SUPPLIES": "200300",
}
TAX_CODE_NO_VAT = "7"  # every Jun-Sep 2026 bill: GlobalTaxCalculation TaxExcluded, lines TaxCodeRef 7


def r2(x):
    return round(float(x) + 1e-9, 2)


def norm(s):
    s = (s or "").upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(LIMITED|LTD|NIG|NIGERIA|ENTERPRISES?|ENT|VENTURES?|CO|COMPANY|PVT|STORES?|AND|&)\b", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = s.replace("SAMS", "SAM")
    return " ".join(s.split())


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def parse_note(note):
    note = (note or "").strip()
    m = re.search(r"SUPPLIER\s*:?\s*(.*?)\s*(?:MODE\s*OF\s*PAYMENT\s*:?\s*(.*))?$", note, re.I | re.S)
    if not m:
        return "", "", note
    return m.group(1).strip(" :-"), (m.group(2) or "").strip(), note


def epoch(s):
    m = re.search(r"/Date\((\d+)\)/", s or "")
    return datetime.utcfromtimestamp(int(m.group(1)) / 1000) if m else None




def build(args):
    """Offline draft build; ``args`` carries the input paths and window settings."""
    out = Path(args.out)
    window = (args.window_from, args.window_to)
    open_snap, close_snap, epos_implied = args.open_snap, args.close_snap, args.epos_implied
    tag = window[0][:7]
    od, cd = datetime.fromisoformat(open_snap), datetime.fromisoformat(close_snap)
    open_lbl = f"{od.day}{od.strftime('%b').lower()}_{od:%H%M}"
    close_lbl = f"{cd.day}{cd.strftime('%b').lower()}"
    SPLIT_FROM = od.date().isoformat()
    SPLIT_BEFORE = (od.date() - timedelta(days=1)).isoformat()
    PDF_MONTH = f"{window[0][5:7]}/{window[0][:4]}"
    orders = json.load(open(args.po_list))["body"]["orders"]
    details = {}
    for line in open(args.po_details):
        d = json.loads(line)
        details[d["OrderRef"]] = d
    cats = {p["CategoryId"]: p["CategoryName"] for p in json.load(open(args.catalogue)) if p.get("CategoryId")}
    q = json.load(open(args.qbo_snapshot))
    pdf = json.load(open(args.pdf_pos)) if args.pdf_pos and Path(args.pdf_pos).exists() else {}

    vendors = [v for v in q["vendors"] if v.get("Active", True)]

    billcount = q.get("vendor_bill_counts") or q.get("vendor_bill_counts_2026_06_09", {})

    def vsim(name, v):
        s = sim(name, v["DisplayName"])
        a, b = norm(name).split(), norm(v["DisplayName"]).split()
        # same distinctive first word (e.g. YUWA BREAD ~ YUWA BAKERY) on a vendor that has real bills
        if a and b and a[0] == b[0] and len(a[0]) >= 4 and billcount.get(v["Id"], 0) > 0:
            s = max(s, 0.86)
        return s

    def best_vendor(name):
        scored = sorted(((vsim(name, v), billcount.get(v["Id"], 0), v) for v in vendors),
                        key=lambda t: (-t[0], -t[1]))
        if not scored:
            return None, 0
        top = scored[0][0]
        # among near-ties prefer the vendor already used on 2026 bills
        near = [t for t in scored if t[0] >= top - 0.03]
        near.sort(key=lambda t: (-t[1], -t[0]))
        return near[0][2], near[0][0]

    # ---------------------------------------------------------------- 1. receipts
    po_rows, pos = [], []
    for o in orders:
        recv = (o.get("DateReceived") or "")[:19]
        if o.get("StatusName") != "Received" or not (window[0] <= recv[:10] <= window[1]):
            continue
        d = details.get(o["OrderRef"])
        supplier, payment, note = parse_note(d.get("Note") if d else None)
        lines = []
        for p in (d or {}).get("Products", []):
            qty = float(p.get("QuantityReceived") or 0)
            if qty == 0:
                continue
            ex = float(p.get("ActualProductCostPrice") or p.get("ValueExcTax") or 0)
            inc = float(p.get("ValueIncTax") or 0)
            cat = cats.get(p.get("CategoryID"), f"UNKNOWN({p.get('CategoryID')})")
            lines.append(dict(product_id=p["ProductId"], product=(p["ProductName"] or "").strip(), category=cat,
                              qty=qty, unit_cost_ex=ex, unit_cost_inc=inc, tax_pct=p.get("TaxRatePercentage"),
                              line_ex=qty * ex, line_inc=qty * inc))
        po = dict(ref=o["OrderRef"], grn=";".join(o.get("GoodsReceiptNumbers") or []), received=recv,
                  received_date=recv[:10], supplier=supplier, payment=payment, note=note,
                  total_ex=float(o.get("TotalValueReceivedExTax") or 0), total_inc=float(o.get("TotalValueReceived") or 0),
                  lines=lines, has_detail=d is not None)
        po["lines_inc"] = sum(x["line_inc"] for x in lines)
        po["zero_cost_lines"] = [x for x in lines if x["unit_cost_inc"] == 0]
        pdfrow = pdf.get(str(o["OrderRef"]))
        po["pdf_total_received_ex"] = pdfrow["total_received"] if pdfrow else None
        pos.append(po)
        for x in lines or [dict(product_id="", product="(no detail captured)", category="", qty="", unit_cost_ex="",
                                unit_cost_inc="", tax_pct="", line_ex="", line_inc="")]:
            po_rows.append([po["ref"], po["grn"], po["received"], supplier, payment, x["product_id"], x["product"],
                            x["category"], x["qty"],
                            r2(x["unit_cost_ex"]) if x["unit_cost_ex"] != "" else "",
                            r2(x["unit_cost_inc"]) if x["unit_cost_inc"] != "" else "",
                            r2(x["line_ex"]) if x["line_ex"] != "" else "",
                            r2(x["line_inc"] - x["line_ex"]) if x["line_ex"] != "" else "",
                            r2(x["line_inc"]) if x["line_inc"] != "" else "", x["tax_pct"]])
    with open(out / f"po_received_{tag}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_ref", "grn", "received_at_epos_local", "supplier_from_po_note", "payment_mode",
                    "epos_product_id", "product", "epos_category", "qty_received", "unit_cost_ex_tax",
                    "unit_cost_inc_tax", "line_total_ex_tax", "line_tax", "line_total_inc_tax", "tax_pct"])
        w.writerows(po_rows)

    # ---------------------------------------------------------------- 2. match to QBO bills
    bills = []
    for b in q["bills"]:
        bills.append(dict(id=b["Id"], date=b["TxnDate"], vendor=b["VendorRef"]["name"], amt=float(b["TotalAmt"]),
                          doc=b.get("DocNumber") or "", created=b["MetaData"]["CreateTime"][:10], used=False))

    def ddays(a, b):
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)

    for po in pos:
        po["match"] = None
    # pass 1: one PO == one bill, amount within N1 (or 0.05%), date within 3 days; rank by vendor sim then date
    for tol_days in (0, 1, 3):
        for po in sorted(pos, key=lambda p: -p["total_inc"]):
            if po["match"]:
                continue
            cands = [b for b in bills if not b["used"] and ddays(b["date"], po["received_date"]) <= tol_days
                     and abs(b["amt"] - po["total_inc"]) <= max(1.0, 0.0005 * po["total_inc"])]
            if not cands:
                continue
            cands.sort(key=lambda b: (-sim(po["supplier"], b["vendor"]), ddays(b["date"], po["received_date"])))
            b = cands[0]
            vs = sim(po["supplier"], b["vendor"])
            if vs < 0.5 and po["total_inc"] < 20000 and len(cands) > 0:
                # small round amounts collide easily; demand some vendor agreement
                continue
            b["used"] = True
            po["match"] = dict(kind="1:1", bills=[b["id"]], bill_total=b["amt"], vendor=b["vendor"], vendor_sim=r2(vs),
                               date=b["date"])
    # pass 2: several POs (same supplier, same date +-1) == one bill
    groups = defaultdict(list)
    for po in pos:
        if not po["match"]:
            groups[norm(po["supplier"])].append(po)
    for key, grp in groups.items():
        for b in bills:
            if b["used"] or sim(key, b["vendor"]) < 0.75:
                continue
            near = [p for p in grp if not p["match"] and ddays(b["date"], p["received_date"]) <= 1]
            if len(near) >= 2 and abs(sum(p["total_inc"] for p in near) - b["amt"]) <= 1.0:
                b["used"] = True
                for p in near:
                    p["match"] = dict(kind="n:1", bills=[b["id"]], bill_total=b["amt"], vendor=b["vendor"],
                                      vendor_sim=r2(sim(key, b["vendor"])), date=b["date"])
    # pass 3: same supplier and date, amount differs -> partial / variance (flag, not treated as billed)
    for po in pos:
        if po["match"]:
            continue
        cands = [b for b in bills if not b["used"] and ddays(b["date"], po["received_date"]) <= 1
                 and sim(po["supplier"], b["vendor"]) >= 0.75]
        if cands:
            b = min(cands, key=lambda b: abs(b["amt"] - po["total_inc"]))
            if abs(b["amt"] - po["total_inc"]) <= 0.10 * po["total_inc"]:
                # same supplier, same day, amount within 10%: treat as billed (with variance), not a new bill
                b["used"] = True
                po["match"] = dict(kind="variance", bills=[b["id"]], bill_total=b["amt"], vendor=b["vendor"],
                                   vendor_sim=r2(sim(po["supplier"], b["vendor"])), date=b["date"])
            else:
                po["possible"] = dict(bill=b["id"], amt=b["amt"], vendor=b["vendor"], date=b["date"])

    with open(out / f"po_bill_match_{tag}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_ref", "received_date", "supplier_from_po_note", "epos_total_ex_tax", "epos_total_inc_tax",
                    "status", "qbo_bill_ids", "qbo_bill_date", "qbo_vendor", "qbo_bill_total", "vendor_similarity",
                    "possible_bill_same_vendor_diff_amount"])
        for po in pos:
            m = po["match"]
            ps = po.get("possible")
            w.writerow([po["ref"], po["received_date"], po["supplier"], r2(po["total_ex"]), r2(po["total_inc"]),
                        ("BILLED " + m["kind"]) if m else "UNBILLED",
                        ";".join(m["bills"]) if m else "", m["date"] if m else "", m["vendor"] if m else "",
                        r2(m["bill_total"]) if m else "", m["vendor_sim"] if m else "",
                        f"Bill {ps['bill']} {ps['date']} {ps['vendor']} {ps['amt']:.2f}" if ps else ""])
    unmatched_bills = [b for b in bills if not b["used"] and b["date"] >= window[0]]

    # ---------------------------------------------------------------- 3. draft bills
    # possible duplicate receipts: large PO whose product/qty set overlaps >=50% an EARLIER PO in the month
    def pq(p):
        return {x["product_id"]: x["qty"] for x in p["lines"]}
    by_ref = sorted(pos, key=lambda p: p["received"])
    for i, p in enumerate(by_ref):
        p["dup_of"] = []
        if p["total_inc"] < 50000:
            continue
        a = pq(p)
        for e in by_ref[:i]:
            b = pq(e)
            same = [k for k in a if k in b and a[k] == b[k]]
            if same and len(same) >= 0.5 * max(len(a), len(b)):
                p["dup_of"].append(f"{e['ref']}({e['received_date']},{'billed ' + e['match']['bills'][0] if e['match'] else 'unbilled'})")

    draft_rows, totals, seg_totals = [], defaultdict(float), defaultdict(float)
    vendor_flags = {}
    unbilled = [p for p in pos if not p["match"]]
    for po in unbilled:
        v, s = best_vendor(po["supplier"]) if po["supplier"] else (None, 0)
        if s >= 0.85:
            vname, vid, vflag = v["DisplayName"], v["Id"], "existing" if s == 1 else f"existing (fuzzy {s:.2f}) - confirm"
        elif s >= 0.6:
            vname, vid, vflag = v["DisplayName"], v["Id"], f"CHECK: closest QBO vendor (sim {s:.2f}); may be NEW"
        else:
            vname, vid, vflag = po["supplier"] or "(no supplier on PO)", "", "NEW VENDOR - create before posting"
        vendor_flags[po["supplier"]] = (vname, vid, vflag)
        flags = []
        if not po["lines"]:
            flags.append("no product lines captured")
        if po["zero_cost_lines"]:
            flags.append(f"{len(po['zero_cost_lines'])} line(s) with zero cost")
        if abs(po["lines_inc"] - po["total_inc"]) > 1:
            flags.append(f"line sum {po['lines_inc']:.2f} != PO total {po['total_inc']:.2f}")
        if po.get("possible"):
            ps = po["possible"]
            flags.append(f"possible partial bill {ps['bill']} ({ps['amt']:.2f}) same vendor/date")
        if not po["supplier"]:
            flags.append("supplier missing on PO note")
        if po["dup_of"]:
            flags.append("same products+qty as earlier PO " + ",".join(po["dup_of"]) +
                         " - repeat order or duplicate receipt? verify supplier invoice")
        for i, x in enumerate(po["lines"], 1):
            acct = CATEGORY_ACCOUNT.get(x["category"])
            if not acct:
                acct = "200100"
                flags_line = f"category '{x['category']}' unmapped -> 200100 default, confirm"
            else:
                flags_line = ""
            aid, aname = ACCOUNTS[acct]
            qty_s = f"{x['qty']:g}"
            amt = r2(x["line_inc"])
            seg = "post_reset" if po["received"][:16] >= open_snap else "pre_reset"
            totals[acct] += amt
            seg_totals[(seg, acct)] += amt
            draft_rows.append([f"DRAFT-PO{po['ref']}", po["ref"], po["grn"], vname, vid, vflag, po["received_date"],
                               "TaxExcluded", i, "AccountBasedExpenseLineDetail", acct, aid, aname, amt,
                               TAX_CODE_NO_VAT, f"PO {po['ref']}: {x['product']} x {qty_s}", x["product_id"],
                               x["category"], r2(x["line_ex"]), r2(x["line_inc"] - x["line_ex"]),
                               "; ".join(filter(None, flags + [flags_line])), po["payment"], seg, po["received"]])
    with open(out / "bills_to_enter.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["draft_bill_ref", "po_ref", "grn", "vendor_qbo_name", "vendor_qbo_id", "vendor_flag", "TxnDate",
                    "GlobalTaxCalculation", "line_no", "DetailType", "account_num", "AccountRef_id", "account_name",
                    "Amount", "TaxCodeRef", "Description_memo", "epos_product_id", "epos_category",
                    "epos_line_ex_tax", "epos_line_vat", "flags", "payment_mode_from_po",
                    f"vs_{open_lbl}_reset", "received_at_epos_local"])
        w.writerows(draft_rows)
    with open(out / "bills_to_enter_totals.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["account_num", "AccountRef_id", "account_name", f"pre_reset_received_before_{open_lbl}",
                    f"post_reset_{open_lbl}_to_{close_lbl}", "draft_amount_ngn"])
        for a in ACCOUNTS:
            w.writerow([a, ACCOUNTS[a][0], ACCOUNTS[a][1], r2(seg_totals[("pre_reset", a)]),
                        r2(seg_totals[("post_reset", a)]), r2(totals[a])])
        w.writerow(["TOTAL", "", "", r2(sum(v for k, v in seg_totals.items() if k[0] == "pre_reset")),
                    r2(sum(v for k, v in seg_totals.items() if k[0] == "post_reset")), r2(sum(totals.values()))])

    # ---------------------------------------------------------------- summary
    def agg(ps, key):
        return r2(sum(p[key] for p in ps))

    snap_window = [p for p in pos if open_snap <= p["received"][:16] < close_snap]
    s = dict(
        po_count=len(pos), po_total_ex=agg(pos, "total_ex"), po_total_inc=agg(pos, "total_inc"),
        pos_missing_detail=[p["ref"] for p in pos if not p["has_detail"]],
        po_1_15=dict(n=len([p for p in pos if p["received_date"] <= SPLIT_BEFORE]),
                     ex=agg([p for p in pos if p["received_date"] <= SPLIT_BEFORE], "total_ex"),
                     inc=agg([p for p in pos if p["received_date"] <= SPLIT_BEFORE], "total_inc")),
        po_16_25=dict(n=len([p for p in pos if p["received_date"] >= SPLIT_FROM]),
                      ex=agg([p for p in pos if p["received_date"] >= SPLIT_FROM], "total_ex"),
                      inc=agg([p for p in pos if p["received_date"] >= SPLIT_FROM], "total_inc")),
        po_between_snapshots=dict(n=len(snap_window), ex=agg(snap_window, "total_ex"), inc=agg(snap_window, "total_inc"),
                                  epos_implied_ex=epos_implied,
                                  gap_ex=r2(epos_implied - sum(p["total_ex"] for p in snap_window))),
        billed=dict(n=len([p for p in pos if p["match"]]), inc=agg([p for p in pos if p["match"]], "total_inc")),
        unbilled=dict(n=len(unbilled), ex=agg(unbilled, "total_ex"), inc=agg(unbilled, "total_inc"),
                      n_1_15=len([p for p in unbilled if p["received_date"] <= SPLIT_BEFORE]),
                      inc_1_15=agg([p for p in unbilled if p["received_date"] <= SPLIT_BEFORE], "total_inc"),
                      n_16_25=len([p for p in unbilled if p["received_date"] >= SPLIT_FROM]),
                      inc_16_25=agg([p for p in unbilled if p["received_date"] >= SPLIT_FROM], "total_inc")),
        draft_totals_by_account={a: r2(v) for a, v in totals.items()}, draft_total=r2(sum(totals.values())),
        draft_lines=len(draft_rows),
        draft_by_segment={f"{k[0]}|{k[1]}": r2(v) for k, v in sorted(seg_totals.items())},
        unbilled_pre_reset=dict(n=len([p for p in unbilled if p["received"][:16] < open_snap]),
                                ex=agg([p for p in unbilled if p["received"][:16] < open_snap], "total_ex"),
                                inc=agg([p for p in unbilled if p["received"][:16] < open_snap], "total_inc")),
        unbilled_post_reset=dict(n=len([p for p in unbilled if p["received"][:16] >= open_snap]),
                                 ex=agg([p for p in unbilled if p["received"][:16] >= open_snap], "total_ex"),
                                 inc=agg([p for p in unbilled if p["received"][:16] >= open_snap], "total_inc")),
        qbo_sep_bills_unmatched_to_po=[dict(id=b["id"], date=b["date"], vendor=b["vendor"], amt=b["amt"])
                                       for b in unmatched_bills],
        new_or_check_vendors=sorted({(k, v[0], v[2]) for k, v in vendor_flags.items() if not v[2].startswith("existing")}),
        fuzzy_vendors=sorted({(k, v[0], v[2]) for k, v in vendor_flags.items() if v[2].startswith("existing (fuzzy")}),
        zero_cost_lines=[(p["ref"], x["product_id"], x["product"], x["qty"]) for p in pos for x in p["zero_cost_lines"]],
        line_sum_mismatch=[(p["ref"], r2(p["lines_inc"]), r2(p["total_inc"])) for p in pos
                           if abs(p["lines_inc"] - p["total_inc"]) > 1],
        pdf_crosscheck_mismatch=[(p["ref"], p["pdf_total_received_ex"], r2(p["total_ex"])) for p in pos
                                 if p["pdf_total_received_ex"] is not None and abs(p["pdf_total_received_ex"] - p["total_ex"]) > 1],
        pdf_pos_1_15_not_in_list=sorted(set(k for k, v in pdf.items() if v.get("completed", "") and
                                            v["completed"][3:] == PDF_MONTH) - {str(p["ref"]) for p in pos}),
        missing_supplier=[p["ref"] for p in pos if not p["supplier"]],
        billed_with_variance=[(p["ref"], r2(p["total_inc"]), p["match"]["bills"][0], p["match"]["bill_total"])
                              for p in pos if p["match"] and p["match"]["kind"] == "variance"],
        possible_partials=[(p["ref"], p["total_inc"], p["possible"]) for p in pos if p.get("possible")],
        unmapped_categories=sorted({x["category"] for p in unbilled for x in p["lines"] if x["category"] not in CATEGORY_ACCOUNT}),
        unbilled_possible_duplicates=[(p["ref"], p["received_date"], p["supplier"], r2(p["total_inc"]), p["dup_of"])
                                      for p in unbilled if p["dup_of"]],
        vat_in_unbilled=r2(sum(x["line_inc"] - x["line_ex"] for p in unbilled for x in p["lines"])),
    )
    json.dump(s, open(out / "summary.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in s.items() if not isinstance(v, list) or len(v) < 8}, indent=1, default=str))

    return s


# ---------------------------------------------------------------- capture-pos (EPOS, view only)
PO_LIST_URL = "https://www.eposnowhq.com/Stock/PurchaseOrder/Index"
KEEP_P = ['Id', 'ProductId', 'ProductName', 'CategoryID', 'Barcode', 'TaxRatePercentage', 'ValueIncTax', 'ValueExcTax',
          'ActualProductCostPrice', 'Quantity', 'QuantityReceived', 'VolumeOfSale', 'CostPriceMeasurementUnitVolume',
          'Factor']


def extract_po_model(html: str) -> dict:
    """Read the Details page's own `var jsonData = {...}` order model (scalars + product lines)."""
    i = html.index('var jsonData = ') + len('var jsonData = ')
    d, _ = json.JSONDecoder().raw_decode(html[i:])
    o = {k: v for k, v in d.items() if not isinstance(v, (dict, list)) or k == 'GoodsReceiptNumbers'}
    o['Products'] = [{k: pr.get(k) for k in KEEP_P} for pr in (d.get('Products') or [])]
    o['_items_len'] = len([x for x in (d.get('Items') or []) if x])
    return o


def capture_pos(out: Path, date_from: str, date_to: str, limit: int, company_key: str) -> None:
    from playwright.sync_api import sync_playwright

    cfg = company_config(company_key)
    dfrom = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d/%m/%Y")
    dto = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d/%m/%Y")
    lists = []

    def on_resp(r):
        if 'GetPurchaseOrders' in r.url and r.request.method == 'GET':
            try:
                lists.append({'url': r.url, 'body': json.loads(r.text())})
            except Exception as e:  # noqa: BLE001
                print('list capture failed', e)

    def open_list(page):
        goto_retry(page, PO_LIST_URL)
        page.wait_for_load_state("networkidle")
        for sel, val in (('purchase-order__from-date--input', dfrom), ('purchase-order__to-date--input', dto)):
            inp = page.locator(f'[data-qa-id="{sel}"]')
            inp.click()
            inp.fill(val)
            inp.press('Tab')
        page.keyboard.press('Escape')
        page.locator('[data-qa-id="purchase-order__apply-filter-button"]').click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)

    details_sel = '[data-qa-id="purchase-order__orders-list__details"]'
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        page.on('response', on_resp)
        epos_login(page, cfg)
        open_list(page)
        orders = lists[-1]['body']['orders']
        json.dump(lists[-1], open(out / 'po_list_raw.json', 'w'))
        refs_dom = page.locator(details_sel).count()
        print('orders in list', len(orders), 'DOM rows', refs_dom)
        details_path = out / 'po_details.jsonl'
        done = set()
        if details_path.exists():
            for line in open(details_path):
                done.add(json.loads(line)['_row'])
        for i in range(min(refs_dom, limit)):
            if i in done:
                continue
            for attempt in range(3):
                try:
                    if page.locator(details_sel).count() != refs_dom:
                        open_list(page)
                    page.locator(details_sel).nth(i).click()
                    page.wait_for_url(re.compile(r'/PurchaseOrder/Details'), timeout=60000)
                    page.wait_for_load_state("networkidle")
                    d = extract_po_model(page.content())
                    d['_row'] = i
                    with open(details_path, 'a') as f:
                        f.write(json.dumps(d) + '\n')
                    print(i, d.get('OrderRef'), len(d.get('Products') or []), flush=True)
                    open_list(page)
                    break
                except Exception as e:  # noqa: BLE001
                    print('row', i, 'attempt', attempt, 'error', str(e)[:200], flush=True)
                    open_list(page)
        b.close()


# ---------------------------------------------------------------- capture-qbo (GET only)
def capture_qbo(out: Path, date_from: str, date_to: str, counts_from: str, company_key: str) -> None:
    q = ReadOnlyQBO(company_key)
    snap = {
        "bills": q.query_all(f"select * from Bill where TxnDate >= '{date_from}' and TxnDate <= '{date_to}'", "Bill"),
        "vendors": q.query_all("select * from Vendor", "Vendor"),
        "accounts": q.query_all("select * from Account", "Account"),
        "taxcodes": q.query_all("select * from TaxCode", "TaxCode"),
        "purchases": q.query_all(f"select * from Purchase where TxnDate >= '{date_from}' and TxnDate <= '{date_to}'",
                                 "Purchase"),
    }
    counted = q.query_all(f"select * from Bill where TxnDate >= '{counts_from}' and TxnDate <= '{date_to}'", "Bill")
    snap["vendor_bill_counts"] = dict(Counter((b.get("VendorRef") or {}).get("value") for b in counted))
    snap["vendor_bill_counts_window"] = [counts_from, date_to]
    dump_json(out / "qbo_snapshot.json", snap)
    print({k: len(v) for k, v in snap.items() if isinstance(v, (list, dict))}, "->", out / "qbo_snapshot.json")


# ---------------------------------------------------------------- parse-pdf (offline)
PDF_RX = re.compile(r'^\s*(\d{3,5})\s+(.*?)\s*Plot C,\s+(\d\d/\d\d/\d{4})\s+(\d\d/\d\d/\d{4})\s+(\d\d/\d\d/\d{4})?\s*₦\s*([\d.]*)\s+₦\s*([\d.]+)\s+(\w+)')


def parse_pdf_text(paths: list[Path]) -> dict:
    out = {}
    for f in paths:
        lines = open(f).read().splitlines()
        for i, line in enumerate(lines):
            m = PDF_RX.match(line)
            if m:
                ref, sup, do, de, dc, tc, tr, st = m.groups()
                grn = None
                for j in range(i + 1, min(i + 12, len(lines))):
                    g = re.search(r'GRN list:\s*(.*)', lines[j])
                    if g:
                        grn = g.group(1).strip()
                        break
                out[ref] = dict(ref=ref, supplier=sup.strip(), ordered=do, expected=de, completed=dc,
                                total_cost=float(tc) if tc else None, total_received=float(tr), status=st, grn=grn)
            elif re.match(r'^\s*\d{4}\s', line) and '₦' in line:
                print('UNPARSED', line, file=sys.stderr)
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture-pos", help="VIEW-ONLY EPOS Purchase Orders capture")
    c.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    c.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    c.add_argument("--limit", type=int, default=10 ** 6, help="max PO detail pages to open")
    q = sub.add_parser("capture-qbo", help="READ-ONLY QBO snapshot (GET queries)")
    q.add_argument("--from", dest="date_from", required=True, help="bills/purchases TxnDate from (YYYY-MM-DD)")
    q.add_argument("--to", dest="date_to", required=True)
    q.add_argument("--counts-from", default="2026-06-01", help="per-vendor bill counts window start (default %(default)s)")
    pp = sub.add_parser("parse-pdf", help="parse pdftotext dumps of EPOS PO list PDFs")
    pp.add_argument("text_files", nargs="+", type=Path)
    bd = sub.add_parser("build", help="OFFLINE draft bills build")
    bd.add_argument("--po-list", type=Path, default=SEP_EV / "epos_po_list_2026-09-01_26.json")
    bd.add_argument("--po-details", type=Path, default=SEP_EV / "epos_po_details_2026-09.jsonl")
    bd.add_argument("--qbo-snapshot", type=Path, default=SEP_EV / "qbo_snapshot_2026-09-26.json")
    bd.add_argument("--catalogue", type=Path, default=GAPS_DIR / "epos_catalogue_2026-09-26" / "catalogue_products.json",
                    help="catalogue_products.json (EPOS CategoryId -> CategoryName)")
    bd.add_argument("--pdf-pos", type=Path, default=SEP_EV / "pdf_pos_jun01_sep15.json", help="optional parse-pdf output")
    bd.add_argument("--window-from", default="2026-09-01", help="PO received date from (default %(default)s)")
    bd.add_argument("--window-to", default="2026-09-25", help="PO received date to (default %(default)s)")
    bd.add_argument("--open-snap", default="2026-09-16T21:08", help="IA reset stock snapshot, EPOS local (default %(default)s)")
    bd.add_argument("--close-snap", default="2026-09-25T13:08", help="closing stock snapshot (default %(default)s)")
    bd.add_argument("--epos-implied", type=float, default=40768858.75,
                    help="month_close_draft implied receipts at ex-tax cost (default %(default)s)")
    for s in (c, q, pp, bd):
        s.add_argument("--out", type=Path, default=None, help="output folder (default outputs/bills_from_epos_pos_<timestamp>/)")
        s.add_argument("--company", default="company_a")
    a = ap.parse_args(argv)
    out = resolve_out(a.out, TOOL)
    a.out = out
    if a.cmd == "capture-pos":
        capture_pos(out, a.date_from, a.date_to, a.limit, a.company)
    elif a.cmd == "capture-qbo":
        capture_qbo(out, a.date_from, a.date_to, a.counts_from, a.company)
    elif a.cmd == "parse-pdf":
        res = parse_pdf_text(a.text_files)
        json.dump(res, open(out / "pdf_pos.json", "w"), indent=1)
        print(len(res), "POs ->", out / "pdf_pos.json")
    else:
        build(a)
    print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

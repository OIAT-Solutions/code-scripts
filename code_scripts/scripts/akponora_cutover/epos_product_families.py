"""Staff-facing EPOS product workbooks for AKPONORA / NORA (company_a). OFFLINE, files only.

Subcommands:
  families   AKPONORA_EPOS_Product_Families workbook from a catalogue_products.json and the
             build_canonical mapping_evidence.csv. Sheets:
               "Masters and children"  one MASTER row per stock owner (Owner Product ID ==
                                       EPOS Product ID) followed by its children (grouped by
                                       Owner Product ID, most units first)
               "No master found"       products with no owner (standalone untracked /
                                       ambiguous owner), by Sep till value
               "Read me"
             Units deducted per sale = 'Proposed sale multiplier (canonical units)', else
             EPOS VolumeOfSale, else 1. Link confidence from the tier: A 'Confirmed by EPOS
             pack size', B 'Likely (check)', C 'Needs staff'.
  untracked  The untracked (NonInventory) product list: every untracked product with no
             stock owner, with Sep sales lines/value counted from a BookKeeping CSV.

Examples:
  python -m code_scripts.scripts.akponora_cutover.epos_product_families families \\
      --catalogue outputs/nora_gaps_2026-09-25/epos_catalogue_2026-09-26_1158/catalogue_products.json \\
      --evidence outputs/nora_gaps_2026-09-25/canonical_2026-09-26_1158/mapping_evidence.csv \\
      --as-of "26 Sep 2026 11:58"
  python -m code_scripts.scripts.akponora_cutover.epos_product_families untracked \\
      --catalogue outputs/nora_gaps_2026-09-25/epos_catalogue_2026-09-26_1937/catalogue_products.json \\
      --evidence outputs/nora_gaps_2026-09-25/canonical_2026-09-26_1937/mapping_evidence.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import DEFAULT_EVIDENCE_ROOT, GAPS_DIR, read_csv, resolve_out

TOOL = "epos_product_families"
CONFIDENCE = {"A": "Confirmed by EPOS pack size", "B": "Likely (check)", "C": "Needs staff"}
HEADER_FILL, MASTER_FILL = "1F4E78", "D9EAF7"

FAMILY_COLUMNS = [("Master EPOS ID", 14), ("Master product (holds stock)", 48), ("Row type", 10),
                  ("EPOS Product ID", 14), ("Product name", 48), ("Units deducted per sale", 12),
                  ("EPOS Volume of Sale", 12), ("Stock tracked", 10), ("Sell on till", 10), ("Cost ex tax", 12),
                  ("Sale price inc tax", 12), ("Sep 1-25 till value", 14), ("Link confidence", 24), ("Notes", 40)]
NO_MASTER_COLUMNS = [("EPOS Product ID", 14), ("Product name", 48), ("Category", 32), ("Sell on till", 10),
                     ("Cost ex tax", 12), ("Sale price inc tax", 12), ("Sep 1-25 till value", 14), ("Suggestion", 80)]
UNTRACKED_COLUMNS = [("EPOS Product ID", 14), ("Product name", 52), ("Category", 32), ("Stock tracked", 12),
                     ("Sell on till", 10), ("Cost ex tax", 12), ("Sale price inc tax", 14),
                     ("Sep 1-25 sales lines", 12), ("Sep 1-25 sales value", 14)]


def yes_no(v) -> str:
    return "Yes" if v else "No"


def num(v):
    """CSV text -> int/float (None for blank)."""
    t = str(v if v is not None else "").strip().replace(",", "")
    if not t:
        return None
    f = float(t)
    return int(f) if f.is_integer() else f


def units_deducted(e: dict) -> int | float:
    return num(e.get("Proposed sale multiplier (canonical units)")) or num(e.get("EPOS VolumeOfSale")) or 1


# ---------------------------------------------------------------- families
def build_families(catalogue: list[dict], evidence: list[dict]) -> dict:
    """Return {'masters': [...rows], 'no_master': [...rows]} as lists of cell lists."""
    cat = {int(p["Id"]): p for p in catalogue}
    ev = [e for e in evidence if int(e["EPOS Product ID"]) in cat]
    owners = [e for e in ev if e["Owner Product ID"] and e["Owner Product ID"] == e["EPOS Product ID"]]
    children = defaultdict(list)
    no_master = []
    for e in ev:
        if not e["Owner Product ID"]:
            no_master.append(e)
        elif e["Owner Product ID"] != e["EPOS Product ID"]:
            children[e["Owner Product ID"]].append(e)

    def row(owner, e, kind):
        p = cat[int(e["EPOS Product ID"])]
        return [int(owner["EPOS Product ID"]), owner["EPOS Name"], kind, int(e["EPOS Product ID"]), e["EPOS Name"],
                units_deducted(e), p.get("VolumeOfSale"), yes_no(p.get("IsStockTracked")), yes_no(p.get("SellOnTill")),
                p.get("CostPriceExTax"), p.get("SalePriceIncTax"), num(e["Sep coverage value"]) or 0,
                CONFIDENCE[e["Tier"]], None if e["Tier"] == "A" else (e["Reasons"] or None)]

    masters = []
    owners.sort(key=lambda e: ((cat[int(e["EPOS Product ID"])].get("CategoryName") or ""), e["EPOS Name"],
                               int(e["EPOS Product ID"])))
    for o in owners:
        masters.append(row(o, o, "MASTER"))
        kids = sorted(children.get(o["EPOS Product ID"], []),
                      key=lambda e: -units_deducted(e))  # ties keep evidence order
        masters.extend(row(o, k, "  child") for k in kids)
    orphans = [e for k, v in children.items() if k not in {o["EPOS Product ID"] for o in owners} for e in v]
    no_master_rows = []
    for e in sorted(no_master + orphans, key=lambda e: -(num(e["Sep coverage value"]) or 0)):
        p = cat[int(e["EPOS Product ID"])]
        no_master_rows.append([int(e["EPOS Product ID"]), e["EPOS Name"], p.get("CategoryName"), str(p.get("SellOnTill")),
                               p.get("CostPriceExTax"), p.get("SalePriceIncTax"), num(e["Sep coverage value"]) or 0,
                               e.get("Suggestion (unverified)") or None])
    return {"masters": masters, "no_master": no_master_rows, "owners": len(owners),
            "children": sum(1 for r in masters if r[2] != "MASTER")}


def readme_lines(as_of: str, n_products: int) -> list[str]:
    return [
        f"EPOS product families as of {as_of} (live catalogue pull, {n_products:,} products).",
        "MASTER = the stock-tracked product that holds the stock. Children are till buttons that deduct from it.",
        "Units deducted per sale = how many of the master's base units one sale of that product uses "
        "(from EPOS Volume of Sale; blank Volume = 1).",
        "EPOS does not export the explicit child->master link. Links are inferred from name + Volume of Sale + cost. "
        "'Confirmed' = EPOS pack size and cost agree; 'Likely' = single unit assumed x1; 'Needs staff' = check in EPOS.",
        "Reference verified in EPOS: GOLDEN MORN 45g(3in1)*100 (1754894) master; *10 (1627303) deducts 10; "
        "single (1627302) deducts 1.",
        "'No master found' sheet = products not tracked and not linked to any master (e.g. sold by kg). "
        "They need a tracked master in EPOS or confirmation they are not stock.",
    ]


def _sheet(wb, title, columns, rows, fill_rule=None, first=False):
    from openpyxl.styles import Font, PatternFill

    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append([c for c, _ in columns])
    for i, (_, width) in enumerate(columns, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
        ws.cell(1, i).font = Font(bold=True, color="FFFFFF")
        ws.cell(1, i).fill = PatternFill("solid", fgColor=HEADER_FILL)
    for r in rows:
        ws.append(r)
        if fill_rule and fill_rule(r):
            for c in ws[ws.max_row]:
                c.fill = PatternFill("solid", fgColor=MASTER_FILL)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def write_families_xlsx(path: Path, built: dict, as_of: str, n_products: int) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    _sheet(wb, "Masters and children", FAMILY_COLUMNS, built["masters"], lambda r: r[2] == "MASTER", first=True)
    _sheet(wb, "No master found", NO_MASTER_COLUMNS, built["no_master"])
    ws = wb.create_sheet("Read me")
    for line in readme_lines(as_of, n_products):
        ws.append([line])
    ws.column_dimensions["A"].width = 140
    wb.save(path)


# ---------------------------------------------------------------- untracked list
def sales_by_product(bookkeeping: Path | None) -> dict:
    per = defaultdict(lambda: [0, 0.0])
    if not bookkeeping:
        return per
    for r in read_csv(bookkeeping):
        if " ".join(str(r.get("Staff") or "").split()) == "Total:" or not str(r.get("Product") or "").strip():
            continue
        pid = str(r.get("ProductId") or "").strip()
        if not pid:
            continue
        try:
            key = int(float(pid))
        except ValueError:
            continue
        per[key][0] += 1
        try:
            per[key][1] += float(str(r.get("TOTAL Sales") or "0").replace(",", ""))
        except ValueError:
            pass
    return per


def build_untracked(catalogue: list[dict], evidence: list[dict], sales: dict) -> list[list]:
    cat = {int(p["Id"]): p for p in catalogue}
    ids = [int(e["EPOS Product ID"]) for e in evidence
           if not e["Owner Product ID"] and int(e["EPOS Product ID"]) in cat
           and not cat[int(e["EPOS Product ID"])].get("IsStockTracked")]
    rows = []
    for pid in ids:
        p = cat[pid]
        lines, value = sales.get(pid, [0, 0.0])
        value = int(value) if float(value).is_integer() else value
        rows.append([pid, p.get("Name"), p.get("CategoryName"), yes_no(p.get("IsStockTracked")),
                     yes_no(p.get("SellOnTill")), p.get("CostPriceExTax"), p.get("SalePriceIncTax"), lines, value])
    rows.sort(key=lambda r: (-r[8], r[0]))
    return rows


def write_untracked_xlsx(path: Path, rows: list[list]) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    _sheet(wb, f"{len(rows)} untracked products", UNTRACKED_COLUMNS, rows, first=True)
    wb.save(path)


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("families", "untracked"):
        s = sub.add_parser(name, help=f"build the {name} workbook")
        s.add_argument("--catalogue", type=Path,
                       default=GAPS_DIR / "epos_catalogue_2026-09-26_1937" / "catalogue_products.json")
        s.add_argument("--evidence", type=Path, default=GAPS_DIR / "canonical_2026-09-26_1937" / "mapping_evidence.csv",
                       help="build_canonical mapping_evidence.csv for the same catalogue")
        s.add_argument("--out", type=Path, default=None, help="output folder (default outputs/epos_product_families_<timestamp>/)")
        s.add_argument("--filename", default=None)
    fam = sub.choices["families"]
    fam.add_argument("--as-of", default=None, help="label for the Read me (default: today)")
    unt = sub.choices["untracked"]
    unt.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    unt.add_argument("--bookkeeping", type=Path, default=None,
                     help="BookKeeping CSV for sales lines/value (default <evidence-root>/As of 25th September/BookKeeping_2026_09_25_1245.csv)")
    a = ap.parse_args(argv)
    catalogue = json.loads(a.catalogue.read_text())
    evidence = read_csv(a.evidence)
    out = resolve_out(a.out, TOOL)
    stamp = date.today().isoformat()
    if a.cmd == "families":
        built = build_families(catalogue, evidence)
        path = out / (a.filename or f"AKPONORA_EPOS_Product_Families_{stamp}.xlsx")
        write_families_xlsx(path, built, a.as_of or stamp, len(catalogue))
        print(f"masters {built['owners']}, children {built['children']}, no master {len(built['no_master'])} -> {path}")
    else:
        bk = a.bookkeeping or a.evidence_root / "As of 25th September" / "BookKeeping_2026_09_25_1245.csv"
        rows = build_untracked(catalogue, evidence, sales_by_product(bk))
        path = out / (a.filename or f"AKPONORA_{len(rows)}_untracked_products_{stamp}.xlsx")
        write_untracked_xlsx(path, rows)
        print(f"untracked products {len(rows)} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

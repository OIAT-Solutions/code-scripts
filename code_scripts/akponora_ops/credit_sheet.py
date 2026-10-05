"""Nora Mart Credit Sales sheet (company_a): layout, setup and the server-owned Products list.

Owner design (4 Oct 2026): staff copy each paper credit invoice ("INVOICE/RECEIPT" book, numbered
0006224 ...) into the Google Sheet "Nora Mart Credit Sales", picking the exact EPOS product from a
dropdown; the list price fills itself (an agreed customer price, else the EPOS price); a different
price needs a name and a reason. Repayments go in their own tab. A later nightly step turns Ready rows
into QuickBooks invoices / received payments and writes a status back (not in this module).

This module only builds and maintains the sheet (``setup``), using the service account (Editor):
* creates the tabs (``Read me``, ``Invoices``, ``Repayments``, ``Customers``, ``Customer prices``,
  ``Products``) and their header rows, dropdowns, formulas, highlight rules and warning-only
  protection of the header rows;
* refreshes ``Products`` from the latest EPOS catalogue capture (server-owned; overwritten each time);
* seeds ``Customers`` only when it is empty; ``--examples CSV`` loads transcribed paper invoices into
  an empty ``Invoices`` tab (never ticked Ready).
Safe to run again: it never deletes or overwrites rows people typed, and refuses when a header row
was changed by hand (fix the header, then re-run).

    python -m code_scripts.akponora_ops.credit_sheet setup [--examples outputs/.../paper_invoices_transcribed.csv]
    python -m code_scripts.akponora_ops.credit_sheet setup --dry-run      # print the plan, write nothing
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SHEET_ENV = "OIAT_COMPANY_A_CREDIT_SHEET_ID"
DEFAULT_SHEET_ID = "1x6dB0QX9KWuF6dsqTfBOgBgZ0r4tle1kF847pOXp1nk"
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
MAX_ROWS = 2000  # rows prepared with dropdowns / formulas in Invoices and Repayments

INVOICE_COLS = ["Invoice No", "Date", "Customer", "Location", "Product", "Qty", "List price", "Price charged",
                "Override by", "Override reason", "Line total", "Paid (paper)", "Ready", "Status", "Notes"]
REPAY_COLS = ["Date", "Customer", "Amount", "Received into", "Invoice Nos (optional)", "Paid by", "Reference",
              "Ready", "Status", "Notes"]
CUSTOMER_COLS = ["Customer", "QuickBooks customer", "Locations", "Notes"]
PRICE_COLS = ["Key", "Customer", "Product", "Agreed price", "Set by", "Note"]
PRODUCT_COLS = ["Product", "EPOS ID", "EPOS name", "EPOS price", "Category", "Stock tracked", "Updated"]
TABS = {"Invoices": INVOICE_COLS, "Repayments": REPAY_COLS, "Customers": CUSTOMER_COLS,
        "Customer prices": PRICE_COLS, "Products": PRODUCT_COLS}
ORDER = ["Read me", "Invoices", "Repayments", "Customers", "Customer prices", "Products"]

RECEIVED_INTO = ["Cash", "Moniepoint transfer 5688464974 (acct 4686987227)",
                 "Moniepoint transfer 5397768082 (acct 6397730972)", "Moniepoint POS 1 (4000850527)",
                 "Moniepoint POS 3 (4000850479)", "Moniepoint POS 4 (4000700275)", "Zenith POS (1225575438)",
                 "Other (say in Notes)"]
CUSTOMERS = [
    ["GPFH", "GPFH (Gold Plates Feast House, QBO Id 62)",
     "Chevron HQ (CTH-HQ); Ikorodu / Ayangbure (IKD); 1004 VI; Dream Park",
     "Related company (Goldplates). One customer; write the location on each invoice."],
    ["Jifa Felix", "(to be set up in QuickBooks)", "", "Repays by Moniepoint transfer to 5688464974."],
]
README = [
    ["Nora Mart Credit Sales: how to use this sheet"],
    [""],
    ["1. Invoices tab: one row per line of a paper invoice (same invoice number on every line of that invoice)."],
    ["   Pick the Customer and the exact Product from the dropdowns. List price fills itself."],
    ["   Price charged: what the paper says. If it is different from List price, fill Override by and Override reason"],
    ["   (the row stays red until you do). Tick Ready only when every line of that invoice is entered and checked."],
    ["2. Repayments tab: one row per payment received from a credit customer (amount, date, which account)."],
    ["   Never put credit repayments in the till sheet: they are not that day's sales."],
    ["3. Status column: written by the system (for example 'In QuickBooks'). Do not type in it."],
    ["4. Products and Customer prices: Products is refreshed by the system every night from EPOS (do not edit)."],
    ["   OIAT staff can add an agreed customer price in Customer prices (Customer + Product + Agreed price)."],
    ["5. If a product is not in the list, it is not in EPOS: add it in EPOS first (it appears here the next day)."],
    [""],
    ["Do not rename tabs, move columns or change the header rows: the system reads them by name."],
]


def sheet_id(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get(SHEET_ENV) or "").strip() or DEFAULT_SHEET_ID


def col(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def product_label(epos_id: str, name: str) -> str:
    """Unique dropdown label: EPOS names repeat, IDs don't."""
    return f"{' '.join(str(name).split())} [{epos_id}]"


# ---------------------------------------------------------------- products (server-owned)
def latest_catalogue(state_root: Path) -> Path | None:
    hits = sorted(glob.glob(str(state_root / "ops" / "company_a" / "daily" / "*" / "run_*Z" / "catalogue" /
                                "epos_catalogue" / "catalogue_products.csv")))
    return Path(hits[-1]) if hits else None


def product_rows(catalogue_csv: Path) -> list[list]:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    with open(catalogue_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            pid = str(r.get("Id") or "").split(".")[0].strip()
            name = " ".join(str(r.get("Name") or "").split())
            if not pid or not name:
                continue
            try:
                price = round(float(r.get("SalePriceIncTax") or 0), 2)
            except ValueError:
                price = ""
            rows.append([product_label(pid, name), pid, name, price, r.get("CategoryName") or "",
                         "yes" if str(r.get("IsStockTracked")).lower() == "true" else "no", stamp])
    rows.sort(key=lambda x: x[0].upper())
    return rows


# ---------------------------------------------------------------- formulas / rules
def invoice_formulas() -> dict[str, str]:
    """Header-row ARRAYFORMULAs (header text in row 1, values below)."""
    return {
        "G1": ('=ARRAYFORMULA(IF(ROW(E:E)=1,"List price",IF(E:E="","",'
               "IFERROR(VLOOKUP(C:C&\"|\"&E:E,'Customer prices'!A:D,4,FALSE),"
               'IFERROR(VLOOKUP(E:E,Products!A:D,4,FALSE),"")))))'),
        "K1": '=ARRAYFORMULA(IF(ROW(F:F)=1,"Line total",IF((F:F="")+(H:H=""),"",F:F*H:H)))',
    }


def price_key_formula() -> str:
    return '=ARRAYFORMULA(IF(ROW(B:B)=1,"Key",IF(B:B="","",B:B&"|"&C:C)))'


def override_rule_formula() -> str:
    """Red row: a price that differs from the list price without a name and reason."""
    return '=AND($H2<>"",$G2<>"",$H2<>$G2,OR($I2="",$J2=""))'


# ---------------------------------------------------------------- setup
class Plan:
    def __init__(self):
        self.requests: list[dict] = []
        self.values: list[dict] = []
        self.notes: list[str] = []


def build_plan(meta: dict, read_values, *, products: list[list], examples: list[list] | None = None) -> Plan:
    """Pure: the batchUpdate requests and value writes that bring the sheet to the layout."""
    plan = Plan()
    sheets = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets") or []}
    titles = list(sheets)
    # rename the default first tab instead of leaving it behind
    if "Invoices" not in sheets and titles == ["Sheet1"]:
        plan.requests.append({"updateSheetProperties": {"properties": {"sheetId": sheets["Sheet1"]["sheetId"],
                                                                       "title": "Invoices"},
                                                        "fields": "title"}})
        sheets["Invoices"] = {**sheets.pop("Sheet1"), "title": "Invoices"}
        plan.notes.append("renamed Sheet1 -> Invoices")
    next_id = 1000
    for i, title in enumerate(ORDER):
        if title not in sheets:
            plan.requests.append({"addSheet": {"properties": {"sheetId": next_id, "title": title, "index": i}}})
            sheets[title] = {"sheetId": next_id, "title": title, "_new": True}
            plan.notes.append(f"added tab {title}")
            next_id += 1
    sid = {t: p["sheetId"] for t, p in sheets.items()}

    # header rows: write when blank; refuse when changed by hand. Computed columns carry their
    # ARRAYFORMULA in the header cell (the formula prints the header text itself).
    formulas = {"Invoices": {"List price": invoice_formulas()["G1"], "Line total": invoice_formulas()["K1"]},
                "Customer prices": {"Key": price_key_formula()}}
    for title, cols in TABS.items():
        current = [] if sheets[title].get("_new") else (read_values(f"'{title}'!1:1") or [[]])[0]
        for i, c in enumerate(cols):
            cur = str(current[i]).strip() if i < len(current) else ""
            if cur and cur != c:
                raise SystemExit(f"'{title}' header {col(i)}1 is '{cur}', expected '{c}'. "
                                 "Fix the header row by hand, then re-run setup.")
        header = [formulas.get(title, {}).get(c, c) for c in cols]
        plan.values.append({"range": f"'{title}'!A1", "values": [header]})
        plan.requests.append({"updateSheetProperties": {"properties": {"sheetId": sid[title],
                                                                       "gridProperties": {"frozenRowCount": 1}},
                                                        "fields": "gridProperties.frozenRowCount"}})
        plan.requests.append({"repeatCell": {"range": {"sheetId": sid[title], "startRowIndex": 0, "endRowIndex": 1},
                                             "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                                             "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93}}},
                                             "fields": "userEnteredFormat(textFormat,backgroundColor)"}})

    # dropdowns and checkboxes (Invoices / Repayments), dates, numbers
    def validation(sheet, c, rule, strict=True):
        plan.requests.append({"setDataValidation": {
            "range": {"sheetId": sid[sheet], "startRowIndex": 1, "endRowIndex": MAX_ROWS, "startColumnIndex": c,
                      "endColumnIndex": c + 1},
            "rule": {**rule, "strict": strict, "showCustomUi": True}}})

    def one_of_range(rng):
        return {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"={rng}"}]}}

    def one_of(values):
        return {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]}}

    inv = {c: i for i, c in enumerate(INVOICE_COLS)}
    validation("Invoices", inv["Date"], {"condition": {"type": "DATE_IS_VALID"}})
    validation("Invoices", inv["Customer"], one_of_range("Customers!$A$2:$A"))
    validation("Invoices", inv["Product"], one_of_range("Products!$A$2:$A"))
    validation("Invoices", inv["Paid (paper)"], one_of(["Not paid", "Paid"]))
    validation("Invoices", inv["Ready"], {"condition": {"type": "BOOLEAN"}})
    rep = {c: i for i, c in enumerate(REPAY_COLS)}
    validation("Repayments", rep["Date"], {"condition": {"type": "DATE_IS_VALID"}})
    validation("Repayments", rep["Customer"], one_of_range("Customers!$A$2:$A"))
    validation("Repayments", rep["Received into"], one_of(RECEIVED_INTO))
    validation("Repayments", rep["Ready"], {"condition": {"type": "BOOLEAN"}})
    validation("Customer prices", 1, one_of_range("Customers!$A$2:$A"))
    validation("Customer prices", 2, one_of_range("Products!$A$2:$A"))
    for sheet, cols in (("Invoices", [inv["Date"]]), ("Repayments", [rep["Date"]])):
        for c in cols:
            plan.requests.append({"repeatCell": {
                "range": {"sheetId": sid[sheet], "startRowIndex": 1, "endRowIndex": MAX_ROWS, "startColumnIndex": c,
                          "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                "fields": "userEnteredFormat.numberFormat"}})
    for sheet, cols in (("Invoices", [inv["List price"], inv["Price charged"], inv["Line total"]]),
                        ("Repayments", [rep["Amount"]]), ("Customer prices", [3]), ("Products", [3])):
        for c in cols:
            plan.requests.append({"repeatCell": {
                "range": {"sheetId": sid[sheet], "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}})
    # Invoice No kept as text (0006224 keeps its zeros)
    plan.requests.append({"repeatCell": {
        "range": {"sheetId": sid["Invoices"], "startRowIndex": 1, "endRowIndex": MAX_ROWS, "startColumnIndex": 0,
                  "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}}, "fields": "userEnteredFormat.numberFormat"}})

    # highlight rules (replace ours: delete-all is not available per rule, so add only on a new tab)
    if sheets["Invoices"].get("_new") or not meta_has_rules(meta, sid["Invoices"]):
        plan.requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sid["Invoices"], "startRowIndex": 1, "endRowIndex": MAX_ROWS,
                        "startColumnIndex": 0, "endColumnIndex": len(INVOICE_COLS)}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": override_rule_formula()}]},
                            "format": {"backgroundColor": {"red": 0.99, "green": 0.85, "blue": 0.85}}}}}})

    # warning-only protection: header rows, computed columns, the Status column, the whole Products tab
    protected = [{"sheetId": sid[t], "startRowIndex": 0, "endRowIndex": 1} for t in TABS]
    protected.append({"sheetId": sid["Products"]})
    for c in (inv["List price"], inv["Line total"], inv["Status"]):
        protected.append({"sheetId": sid["Invoices"], "startColumnIndex": c, "endColumnIndex": c + 1})
    protected.append({"sheetId": sid["Repayments"], "startColumnIndex": rep["Status"], "endColumnIndex": rep["Status"] + 1})
    if not meta_has_protection(meta):
        for rng in protected:
            plan.requests.append({"addProtectedRange": {"protectedRange": {
                "range": rng, "warningOnly": True, "description": "Managed by the system: please don't edit"}}})

    # column widths (readability)
    widths = {"Invoices": [95, 95, 90, 140, 360, 60, 95, 105, 110, 200, 105, 90, 60, 220, 220],
              "Repayments": [95, 90, 105, 300, 160, 140, 140, 60, 220, 220],
              "Customers": [120, 300, 380, 380], "Customer prices": [40, 120, 360, 105, 120, 220],
              "Products": [380, 80, 320, 95, 260, 90, 90]}
    for t, ws in widths.items():
        for i, w in enumerate(ws):
            plan.requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid[t], "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    plan.requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sid["Customer prices"], "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})

    # contents: Read me, Products (always), Customers (seed if empty), examples (if Invoices empty)
    plan.values.append({"range": "'Read me'!A1", "values": README})
    plan.values.append({"range": "'Products'!A2", "values": products})
    plan.notes.append(f"Products: {len(products)} rows from the EPOS catalogue")
    customers_now = [] if sheets["Customers"].get("_new") else read_values("'Customers'!A2:A")
    if not any(r and r[0] for r in customers_now or []):
        plan.values.append({"range": "'Customers'!A2", "values": CUSTOMERS})
        plan.notes.append(f"Customers seeded: {[c[0] for c in CUSTOMERS]}")
    if examples:
        invoices_now = [] if sheets["Invoices"].get("_new") else read_values("'Invoices'!A2:A")
        if any(r and r[0] for r in invoices_now or []):
            plan.notes.append("Invoices tab already has rows: examples not loaded")
        else:
            plan.values.append({"range": "'Invoices'!A2", "values": examples})
            plan.notes.append(f"Invoices: {len(examples)} example lines loaded (not Ready)")
    return plan


def meta_has_rules(meta: dict, sheet_id_: int) -> bool:
    for s in meta.get("sheets") or []:
        if s["properties"]["sheetId"] == sheet_id_ and s.get("conditionalFormats"):
            return True
    return False


def meta_has_protection(meta: dict) -> bool:
    return any(s.get("protectedRanges") for s in meta.get("sheets") or [])


def example_rows(transcribed_csv: Path) -> list[list]:
    """Paper invoices not yet in QuickBooks -> Invoices rows (Product left for staff to pick)."""
    out = []
    with open(transcribed_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not str(r.get("In QBO already", "")).startswith("no"):
                continue
            loc = {"CTH-HQ": "Chevron HQ (CTH-HQ)", "IKD": "Ikorodu / Ayangbure (IKD)"}.get(r["Address"], r["Address"])
            paid = {"Paid": "Paid", "Not Paid": "Not paid"}.get(r["Paid mark"], "")
            # None = leave the cell alone (List price / Line total are formulas; Status is the system's)
            out.append([r["Invoice No"].rstrip("?"), r["Date"], "GPFH", loc, None, int(r["Qty"]), None,
                        int(r["Unit price"]), None, None, None, paid, False,
                        None, f"Paper says: {r['Description (as written)']} (pick the exact product)"])
    return out


# ---------------------------------------------------------------- CLI
def service_for(key_path: Path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=[WRITE_SCOPE])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def cmd_setup(a) -> int:
    from code_scripts.akponora_ops import uf_deposits
    from code_scripts.paths import STATE_ROOT

    catalogue = Path(a.catalogue) if a.catalogue else latest_catalogue(Path(STATE_ROOT))
    if not catalogue or not catalogue.exists():
        raise SystemExit("no EPOS catalogue capture found (run the daily run first, or pass --catalogue)")
    products = product_rows(catalogue)
    examples = example_rows(Path(a.examples)) if a.examples else None
    svc = service_for(uf_deposits.key_path(uf_deposits.settings()))
    sid = sheet_id()
    meta = svc.spreadsheets().get(spreadsheetId=sid, fields="sheets(properties,conditionalFormats,protectedRanges)").execute()

    def read_values(rng):
        return svc.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute().get("values") or []

    plan = build_plan(meta, read_values, products=products, examples=examples)
    print(json.dumps({"sheet": sid, "catalogue": str(catalogue), "requests": len(plan.requests),
                      "value_ranges": len(plan.values), "notes": plan.notes}, indent=1))
    if a.dry_run:
        return 0
    if plan.requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": plan.requests}).execute()
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
        "valueInputOption": "USER_ENTERED", "data": plan.values}).execute()
    print("done")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--catalogue", default="", help="EPOS catalogue_products.csv (default: the latest capture)")
    p.add_argument("--examples", default="", help="transcribed paper invoices CSV to load into an empty Invoices tab")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    from code_scripts.scripts.akponora_cutover._common import setup_env

    setup_env()
    return cmd_setup(a)


if __name__ == "__main__":
    sys.exit(main())

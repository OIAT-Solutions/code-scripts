"""EPOS credit sales -> one QBO Invoice per customer per day (company_a / Nora Mart).

Owner decisions (Marvin, chat 5 Oct 2026): the EPOS ``Credit`` tender is a sale on account. The sales path
leaves these rows out of the SalesReceipts (``code_scripts/credit_sales.py`` saves them per business day);
this step, run by ``daily_run`` right after ``sales``, posts them as Invoices so they sit in Accounts
Receivable under the customer. Repayments come later (a separate feature, once the EPOS "pay on account"
flow is known).

Rules:
* Customer: the EPOS customer on the row. Every Goldplates account (``GOLDPLATE…`` in the EPOS name, e.g.
  TALEA MALL / AYANGBUREN / DREAM PARK) is ONE QBO customer, ``GPFH``. Others: the mapping file
  (EPOS customer ID -> QBO customer Id), else one QBO customer with the same name (titles and case
  ignored), else a NEW QBO customer with the EPOS name exactly as EPOS has it (owner: "create any new
  customers"). Two or more possible QBO matches -> held for review.
* Lines: the same product mapping as sales (the sales transform, run on the customer's rows): exact
  approved AKP- item Ids, pack sizes, VAT-inclusive amounts. The invoice total equals the EPOS total.
* One invoice per customer per day, DocNumber ``CR<yymmdd>-<EPOS customer id | GPFH>``; idempotent (an
  existing DocNumber is never posted again) and re-read after posting (total must match).
* Mixed tenders (``Cash/Credit`` …) are not invoiced: the export does not say how much was on credit.
* ``OIAT_COMPANY_A_CREDIT_INVOICES``: ``post`` writes; anything else (default) plans only and the plan
  waits for review. Cap ``OIAT_COMPANY_A_CREDIT_INVOICE_MAX`` per invoice (N5,000,000); larger -> held.
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from code_scripts import credit_sales
from code_scripts.akponora_ops.common import COMPANY, REALM, dump_json
from code_scripts.scripts.akponora_cutover.w7_create_items import sha256_text

FROM_DAY = "2026-10-05"  # the Credit tender was switched on in EPOS on 5 Oct 2026
MODE_ENV = "OIAT_COMPANY_A_CREDIT_INVOICES"
MAX_ENV = "OIAT_COMPANY_A_CREDIT_INVOICE_MAX"
GOLDPLATES_QBO = "GPFH"
TITLES = {"mr", "mrs", "miss", "ms", "dr", "chief", "alhaji", "alhaja", "engr", "barr", "prof", "pastor"}
POSTED, PLANNED, HELD, EXISTS = "posted", "planned", "held", "already in QuickBooks"


def mode(env=None) -> str:
    env = os.environ if env is None else env
    return "post" if str(env.get(MODE_ENV, "")).strip().lower() == "post" else "plan"


def cap(env=None) -> Decimal:
    env = os.environ if env is None else env
    return Decimal(str(env.get(MAX_ENV) or "").replace(",", "").strip() or "5000000")


def mapping_path(state_root: Path | None = None) -> Path:
    return credit_sales.day_dir("x", state_root).parent / "customers.csv"


def money(value) -> Decimal:
    return Decimal(str(value or 0).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm_name(name: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower()).split() if w not in TITLES]
    return " ".join(words)


def is_goldplates(name: str) -> bool:
    return "GOLDPLATE" in re.sub(r"[^A-Z]", "", str(name or "").upper())


def customer_key(row: dict) -> str:
    if is_goldplates(row.get("Customer Full Name")):
        return GOLDPLATES_QBO
    return str(row.get("Customer ID") or "").strip() or "NOID-" + norm_name(row.get("Customer Full Name")).replace(" ", "")[:12]


def doc_number(day: str, key: str) -> str:
    return f"CR{day[2:4]}{day[5:7]}{day[8:10]}-{key}"[:21]


# ---------------------------------------------------------------- customers
def load_mapping(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["epos_customer_id"]: r for r in csv.DictReader(fh) if r.get("epos_customer_id")}


def save_mapping(path: Path, mapping: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["epos_customer_id", "epos_name", "qbo_customer_id", "qbo_name", "how"])
        w.writeheader()
        for key in sorted(mapping):
            w.writerow({k: mapping[key].get(k, "") for k in w.fieldnames})


def resolve_customer(key: str, epos_name: str, customers: list[dict], mapping: dict) -> dict:
    """{'id', 'name', 'how'} for an existing QBO customer, {'create': name} for a new one, or {'hold': reason}."""
    active = [c for c in customers if c.get("Active", True) in (True, "true", "True")]
    if key == GOLDPLATES_QBO:
        hits = [c for c in active if str(c.get("DisplayName", "")).strip().upper() == GOLDPLATES_QBO]
        if len(hits) == 1:
            return {"id": str(hits[0]["Id"]), "name": hits[0]["DisplayName"], "how": "Goldplates account"}
        return {"hold": f"QBO customer {GOLDPLATES_QBO} not found exactly once ({len(hits)})"}
    if key in mapping and mapping[key].get("qbo_customer_id"):
        m = mapping[key]
        return {"id": m["qbo_customer_id"], "name": m.get("qbo_name", ""), "how": "mapping"}
    want = norm_name(epos_name)
    if not want:
        return {"hold": "credit sale without a customer name"}
    hits = [c for c in active if norm_name(c.get("DisplayName")) == want]
    if len(hits) == 1:
        return {"id": str(hits[0]["Id"]), "name": hits[0]["DisplayName"], "how": "same name"}
    if len(hits) > 1:
        return {"hold": f"{len(hits)} QBO customers match {epos_name.strip()!r}; link it in customers.csv"}
    if any(norm_name(c.get("DisplayName")) == want for c in customers):
        return {"hold": f"QBO customer {epos_name.strip()!r} exists but is inactive"}
    return {"create": " ".join(str(epos_name).split())}


# ---------------------------------------------------------------- lines (same mapping as sales)
def invoice_lines(rows: pd.DataFrame, day: str, config, registry) -> tuple[list[dict], Decimal]:
    from code_scripts.transform import transform_dataframe_unified

    out = transform_dataframe_unified(rows.copy(), config, target_date=day)
    lines, gross_total = [], Decimal(0)
    tax_code = str(config.tax_code_id or "2")
    for _, r in out.iterrows():
        proofs = json.loads(r["_Conversion Proof"]) if "_Conversion Proof" in out.columns else []
        rules = [registry.by_row_id.get(str(p.get("row_id") or "").casefold()) for p in proofs]  # index keys are casefolded
        ids = {str(rule.target_qbo_item_id or "") for rule in rules if rule is not None}
        if len(ids) != 1 or not next(iter(ids)):
            raise ValueError(f"no single approved QBO item for {r['Item(Product/Service)']!r}")
        qty = Decimal(str(r["ItemQuantity"]))
        if qty <= 0:
            raise ValueError(f"non-positive quantity for {r['Item(Product/Service)']!r}")
        gross, tax = money(r["*ItemAmount"]), money(r.get("ItemTaxAmount") or 0)
        net = gross - tax
        unit = (net / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append({"DetailType": "SalesItemLineDetail", "Amount": float((unit * qty).quantize(Decimal("0.01"))),
                      "Description": str(r.get("ItemDescription") or ""),
                      "SalesItemLineDetail": {"ItemRef": {"value": next(iter(ids)), "name": r["Item(Product/Service)"]},
                                              "Qty": float(qty), "UnitPrice": float(unit), "ServiceDate": day,
                                              "TaxCodeRef": {"value": tax_code}, "TaxInclusiveAmt": float(gross)}})
        gross_total += gross
    return lines, gross_total


def invoice_payload(day: str, doc: str, customer_id: str, lines: list[dict], gross: Decimal, note: str, config) -> dict:
    net = sum(Decimal(str(l["Amount"])) for l in lines)
    tax = (gross - net).quantize(Decimal("0.01"))
    rate = Decimal(str(config.tax_rate or 0.075))
    rate_id = str(config.get_qbo_config().get("tax_rate_id") or config.tax_code_id or "2")
    return {"CustomerRef": {"value": customer_id}, "TxnDate": day, "DocNumber": doc, "Line": lines,
            "GlobalTaxCalculation": "TaxInclusive", "PrivateNote": note,
            "TxnTaxDetail": {"TotalTax": float(tax), "TaxLine": [{
                "Amount": float(tax), "DetailType": "TaxLineDetail",
                "TaxLineDetail": {"TaxRateRef": {"value": rate_id}, "PercentBased": True,
                                  "TaxPercent": float(rate * 100), "NetAmountTaxable": float(net)}}]}}


def requestid(doc: str) -> str:
    return sha256_text(f"credit_invoice|{REALM}|{doc}")[:36]


# ---------------------------------------------------------------- the step
def days_to_do(business_day: str, state_root: Path | None = None) -> list[str]:
    start, end = date.fromisoformat(FROM_DAY), date.fromisoformat(business_day)
    out = []
    d = start
    while d <= end:
        if (credit_sales.day_dir(d.isoformat(), state_root) / "credit_raw.csv").exists() or \
                (credit_sales.day_dir(d.isoformat(), state_root) / "mixed_raw.csv").exists():
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def run(out: Path, *, business_day: str, client, write_client=None, dry_run: bool = False, env=None,
        config=None, registry=None, state_root: Path | None = None) -> dict:
    """Plan (and with ``post`` mode, post) the credit invoices for every day from FROM_DAY to ``business_day``."""
    env = os.environ if env is None else env
    posting = mode(env) == "post" and not dry_run and write_client is not None
    res = {"mode": "post" if posting else ("dry-run" if dry_run else "plan"), "invoices": [], "mixed": [],
           "customers_created": [], "failed": []}
    days = days_to_do(business_day, state_root)
    if not days:
        dump_json(out / "credit_invoices.json", res)
        return res
    if config is None:
        from code_scripts.company_config import load_company_config
        config = load_company_config(COMPANY)
    if registry is None:
        from code_scripts.product_conversion import ProductConversionRegistry
        registry = ProductConversionRegistry.from_csv(config.product_conversion_file, allow_name_fallback=False)
    customers = client.query_all("select * from Customer", "Customer")
    mpath = mapping_path(state_root)
    mapping = load_mapping(mpath)
    limit = cap(env)
    for day in days:
        folder = credit_sales.day_dir(day, state_root)
        mixed = folder / "mixed_raw.csv"
        if mixed.exists():
            m = pd.read_csv(mixed, dtype=str, keep_default_na=False)
            res["mixed"].append({"day": day, "rows": len(m), "total": str(money(
                pd.to_numeric(m["TOTAL Sales"], errors="coerce").fillna(0).sum())),
                "customers": sorted({n.strip() for n in m.get("Customer Full Name", []) if n.strip()})})
        raw_path = folder / "credit_raw.csv"
        if not raw_path.exists():
            continue
        raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        raw["_key"] = [customer_key(r) for r in raw.to_dict("records")]
        for key, rows in raw.groupby("_key", sort=True):
            names = sorted({n.strip() for n in rows["Customer Full Name"] if n.strip()})
            doc = doc_number(day, key)
            inv = {"day": day, "doc": doc, "customer_key": key, "epos_names": names, "status": PLANNED,
                   "sales": len(set(rows["Date/Time"])), "times": sorted({t[-8:-3] for t in rows["Date/Time"]})}
            try:
                existing = client.query_all(f"select Id, TotalAmt from Invoice where DocNumber = '{doc}'", "Invoice")
                lines, gross = invoice_lines(rows.drop(columns=["_key"]), day, config, registry)
                epos_total = money(pd.to_numeric(rows["TOTAL Sales"], errors="coerce").fillna(0).sum())
                inv.update(total=str(gross), lines=len(lines))
                if abs(gross - epos_total) > Decimal("1"):
                    raise ValueError(f"invoice total N{gross} differs from EPOS N{epos_total}")
                if existing:
                    inv.update(status=EXISTS, invoice_id=str(existing[0]["Id"]))
                    res["invoices"].append(inv)
                    continue
                cust = resolve_customer(key, names[0] if names else "", customers, mapping)
                inv["customer"] = cust.get("name") or cust.get("create") or ""
                if "hold" in cust:
                    inv.update(status=HELD, reason=cust["hold"])
                elif gross > limit:
                    inv.update(status=HELD, reason=f"over the per-invoice cap N{limit:,.0f}")
                if inv["status"] == HELD or not posting:
                    inv["new_customer"] = "create" in cust
                    res["invoices"].append(inv)
                    continue
                if "create" in cust:
                    body = {"DisplayName": cust["create"], "Notes": f"EPOS customer {key} (created by the Nora Mart "
                                                                   f"daily routine from EPOS credit sales)"}
                    resp = write_client.post_json("/customer", body, sha256_text(f"credit_customer|{REALM}|{key}")[:36])
                    made = (resp.json() or {}).get("Customer", {}) if getattr(resp, "status_code", 0) == 200 else {}
                    if not made.get("Id"):
                        raise ValueError(f"customer create failed: HTTP {getattr(resp, 'status_code', '?')} "
                                         f"{str(getattr(resp, 'text', ''))[:200]}")
                    customers.append(made)
                    cust = {"id": str(made["Id"]), "name": made.get("DisplayName", cust["create"]), "how": "created"}
                    res["customers_created"].append({"epos_id": key, "name": cust["name"], "qbo_id": cust["id"]})
                    inv["customer"] = cust["name"]
                if key != GOLDPLATES_QBO:
                    mapping[key] = {"epos_customer_id": key, "epos_name": names[0] if names else "",
                                    "qbo_customer_id": cust["id"], "qbo_name": cust["name"], "how": cust["how"]}
                    save_mapping(mpath, mapping)
                note = (f"EPOS credit sales {day}: {', '.join(names)} ({inv['sales']} sale(s) at {', '.join(inv['times'])}) "
                        f"| {doc} | auto by daily_run credit")
                payload = invoice_payload(day, doc, cust["id"], lines, gross, note, config)
                resp = write_client.post_json("/invoice", payload, requestid(doc))
                live = client.query_all(f"select Id, TotalAmt from Invoice where DocNumber = '{doc}'", "Invoice")
                if len(live) != 1 or abs(money(live[0].get("TotalAmt")) - gross) > Decimal("1"):
                    raise ValueError(f"invoice not confirmed in QuickBooks (HTTP {getattr(resp, 'status_code', '?')}: "
                                     f"{str(getattr(resp, 'text', ''))[:200]})")
                inv.update(status=POSTED, invoice_id=str(live[0]["Id"]))
                res["invoices"].append(inv)
            except Exception as exc:  # noqa: BLE001 - one customer's problem never stops the others
                inv.update(status=HELD, reason=f"{type(exc).__name__}: {exc}"[:300])
                res["invoices"].append(inv)
                res["failed"].append({"doc": doc, "reason": inv["reason"]})
    dump_json(out / "credit_invoices.json", res)
    return res

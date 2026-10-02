"""Rename or create QBO vendors for company_a from a reviewed JSON spec, then sync vendors.csv.

Spec: {"renames": [{"id": "309", "from": "RIE FOODS LIMITED", "to": "RITE FOODS LIMITED"}],
       "creates": [{"name": "WONUOLA SUPER STORE", "epos_names": ["WONUOLA SUPER STORE"]}],
       "approved_by": "..."}

Dry-run by default (GET only): checks each rename's current name and that every new DisplayName is
free across Vendors, Customers and Employees (QBO requires unique names). --execute performs the
writes, re-reads each vendor, and updates STATE_ROOT/mappings/company_a/vendors.csv so the bills
job sees the new names/Ids.

    python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec spec.json
    python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec spec.json --execute
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import requests

from code_scripts.scripts.akponora_cutover._common import setup_env

REALM = "9341455406194328"


def base_url() -> str:
    return f"https://quickbooks.api.intuit.com/v3/company/{REALM}"


def token() -> str:
    setup_env()
    from code_scripts.token_manager import get_access_token

    return get_access_token("company_a", REALM)
from code_scripts.akponora_ops.bills_sync import VENDOR_COLS, vendor_file


def _query(sess, sql: str) -> list:
    r = sess.get(f"{base_url()}/query", params={"query": sql, "minorversion": 75}, timeout=60)
    r.raise_for_status()
    resp = r.json().get("QueryResponse", {})
    return next((v for v in resp.values() if isinstance(v, list)), [])


def _name_taken(sess, name: str) -> list[str]:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    hits = []
    for ent in ("Vendor", "Customer", "Employee"):
        for x in _query(sess, f"select Id, DisplayName from {ent} where DisplayName = '{safe}'"):
            hits.append(f"{ent} {x['Id']}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--execute", action="store_true", help="perform the QBO writes")
    a = ap.parse_args()
    spec = json.loads(a.spec.read_text())
    approved_by = spec.get("approved_by") or ""
    if a.execute and not approved_by:
        sys.exit("spec needs approved_by for --execute")

    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {token()}", "Accept": "application/json",
                         "Content-Type": "application/json"})
    problems, results = [], []

    for rn in spec.get("renames", []):
        v = sess.get(f"{base_url()}/vendor/{rn['id']}", params={"minorversion": 75}, timeout=60).json()["Vendor"]
        if v["DisplayName"] != rn["from"]:
            problems.append(f"rename {rn['id']}: live name {v['DisplayName']!r} != {rn['from']!r}")
            continue
        if taken := _name_taken(sess, rn["to"]):
            problems.append(f"rename {rn['id']}: {rn['to']!r} already used by {taken}")
            continue
        results.append(("rename", rn, v))
    for cr in spec.get("creates", []):
        if taken := _name_taken(sess, cr["name"]):
            problems.append(f"create {cr['name']!r}: already used by {taken}")
            continue
        results.append(("create", cr, None))

    for kind, item, _ in results:
        print(f"{'EXECUTE' if a.execute else 'DRY'} {kind}: {item.get('id', '')} {item.get('from', '')} -> {item.get('to', item.get('name'))}")
    for p in problems:
        print("PROBLEM", p)
    if problems:
        print("refusing: fix the problems above first")
        return 2
    if not a.execute:
        print(f"dry-run only; {len(results)} change(s) ready")
        return 0

    vf = vendor_file()
    rows = list(csv.DictReader(open(vf, encoding="utf-8"))) if vf.exists() else []
    for kind, item, live in results:
        if kind == "rename":
            body = {"sparse": True, "Id": live["Id"], "SyncToken": live["SyncToken"], "DisplayName": item["to"]}
            if live.get("CompanyName") == item["from"]:
                body["CompanyName"] = item["to"]
            r = sess.post(f"{base_url()}/vendor", params={"minorversion": 75}, data=json.dumps(body), timeout=60)
            r.raise_for_status()
            got = r.json()["Vendor"]
            assert got["DisplayName"] == item["to"], got["DisplayName"]
            for row in rows:
                if row["QBO Vendor Id"] == got["Id"]:
                    row["QBO Vendor Name"] = got["DisplayName"]
            print(f"RENAMED vendor {got['Id']} -> {got['DisplayName']}")
        else:
            body = {"DisplayName": item["name"], "CompanyName": item["name"]}
            r = sess.post(f"{base_url()}/vendor", params={"minorversion": 75}, data=json.dumps(body), timeout=60)
            r.raise_for_status()
            got = r.json()["Vendor"]
            for epos_name in item.get("epos_names") or [item["name"]]:
                rows.append({"EPOS Supplier Id": "", "EPOS Supplier Name": epos_name, "QBO Vendor Id": got["Id"],
                             "QBO Vendor Name": got["DisplayName"], "Approved By": approved_by})
            print(f"CREATED vendor {got['Id']} {got['DisplayName']}")
    with open(vf, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, VENDOR_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"vendors.csv updated: {len(rows)} rows ({vf}); realm {REALM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

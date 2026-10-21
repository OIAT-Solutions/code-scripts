"""Rename or create QBO vendors for company_a from a reviewed JSON spec, then sync vendors.csv.

Spec: {"renames": [{"id": "309", "from": "RIE FOODS LIMITED", "to": "RITE FOODS LIMITED"}],
       "creates": [{"name": "WONUOLA SUPER STORE", "epos_names": ["WONUOLA SUPER STORE"]}],
       "approved_by": "..."}

Dry-run by default (GET only): checks each rename's current name and that every new DisplayName is
free across Vendors, Customers and Employees (QBO requires unique names). --execute performs the
writes, re-reads each vendor, and updates STATE_ROOT/mappings/company_a/vendors.csv so the bills
job sees the new names/Ids. Shares the name checks and the create call with the automatic vendor
creation in ``code_scripts/akponora_ops/vendors.py``.

    python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec spec.json
    python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec spec.json --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from code_scripts.akponora_ops import vendors as vendor_ops
from code_scripts.akponora_ops.common import REALM
from code_scripts.scripts.akponora_cutover._common import setup_env
from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient, sha256_text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--execute", action="store_true", help="perform the QBO writes")
    a = ap.parse_args()
    setup_env()
    from code_scripts.akponora_ops.bills_sync import vendor_file

    spec = json.loads(a.spec.read_text())
    approved_by = spec.get("approved_by") or ""
    if a.execute and not approved_by:
        sys.exit("spec needs approved_by for --execute")

    client = QBOClient.for_company_a(allow_writes=a.execute)
    problems, results = [], []

    for rn in spec.get("renames", []):
        v = client.get_json(f"/vendor/{rn['id']}")["Vendor"]
        if v["DisplayName"] != rn["from"]:
            problems.append(f"rename {rn['id']}: live name {v['DisplayName']!r} != {rn['from']!r}")
            continue
        if taken := vendor_ops.name_taken(client, rn["to"]):
            problems.append(f"rename {rn['id']}: {rn['to']!r} already used by {taken}")
            continue
        results.append(("rename", rn, v))
    for cr in spec.get("creates", []):
        if taken := vendor_ops.name_taken(client, cr["name"]):
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
    rows = vendor_ops.read_vendor_rows(vf)
    for kind, item, live in results:
        if kind == "rename":
            body = {"sparse": True, "Id": live["Id"], "SyncToken": live["SyncToken"], "DisplayName": item["to"]}
            if live.get("CompanyName") == item["from"]:
                body["CompanyName"] = item["to"]
            r = client.post_json("/vendor", body, sha256_text(f"vendor_rename|{REALM}|{live['Id']}|{item['to']}")[:36])
            if r.status_code != 200:
                sys.exit(f"rename {live['Id']} failed {r.status_code}: {r.text[:300]}")
            got = r.json()["Vendor"]
            assert got["DisplayName"] == item["to"], got["DisplayName"]
            for row in rows:
                if row["QBO Vendor Id"] == got["Id"]:
                    row["QBO Vendor Name"] = got["DisplayName"]
            print(f"RENAMED vendor {got['Id']} -> {got['DisplayName']}")
        else:
            got = vendor_ops.create_vendor(client, item["name"])
            for epos_name in item.get("epos_names") or [item["name"]]:
                rows.append({"EPOS Supplier Id": "", "EPOS Supplier Name": epos_name, "QBO Vendor Id": got["Id"],
                             "QBO Vendor Name": got["DisplayName"], "Approved By": approved_by})
            print(f"CREATED vendor {got['Id']} {got['DisplayName']}")
    vendor_ops.write_vendor_rows(vf, rows)
    print(f"vendors.csv updated: {len(rows)} rows ({vf}); realm {REALM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

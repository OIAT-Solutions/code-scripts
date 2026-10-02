"""QBO vendor helpers for company_a: fuzzy matching, name checks, gated automatic creation.

Shared by ``bills_sync`` (automatic vendor creation for genuinely new EPOS PO suppliers) and
``scripts/akponora_cutover/vendor_admin.py`` (reviewed manual renames / creates).

Automatic creation (``bills_sync scheduled`` / ``daily_run`` only, never ``plan``):

* every received October PO whose supplier is not in ``vendors.csv`` is scored against ALL live
  QBO vendors (active and inactive) with the ``vendors-suggest`` logic;
* best score < ``NEW_VENDOR_MAX_SCORE`` (0.75) = genuinely new -> created only when
  ``OIAT_COMPANY_A_VENDOR_AUTO_CREATE=1`` and ``OIAT_COMPANY_A_VENDOR_APPROVAL_REF`` is set, at most
  ``OIAT_COMPANY_A_VENDOR_AUTO_MAX`` (default 5) per run, and only when the DisplayName is free
  across Vendors, Customers and Employees (QBO requires unique names);
* best score >= 0.75 = possible typo / duplicate of an existing vendor -> HOLD with the candidates;
* the new vendor is appended to ``vendors.csv`` with ``Approved By = auto:<ref>``.

Never renames, merges, inactivates or edits an existing vendor.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from code_scripts.akponora_ops.common import REALM, read_csv, write_csv
from code_scripts.product_conversion import clean
from code_scripts.scripts.akponora_cutover.bills_from_epos_pos import norm, sim
from code_scripts.scripts.akponora_cutover.w7_create_items import StopRun, qbo_escape, sha256_text

VENDOR_COLS = ["EPOS Supplier Id", "EPOS Supplier Name", "QBO Vendor Id", "QBO Vendor Name", "Approved By"]
AUTO_ENV = "OIAT_COMPANY_A_VENDOR_AUTO_CREATE"
REF_ENV = "OIAT_COMPANY_A_VENDOR_APPROVAL_REF"
MAX_ENV = "OIAT_COMPANY_A_VENDOR_AUTO_MAX"
DEFAULT_MAX = 5
NEW_VENDOR_MAX_SCORE = 0.75
NAME_ENTITIES = ("Vendor", "Customer", "Employee")

# action states
CREATE = "CREATE"                 # genuinely new; would be / will be created
CREATED = "CREATED"
HOLD_NEAR = "HOLD_NEAR_MATCH"     # possible typo / duplicate of an existing vendor
HOLD_GATE = "HOLD_GATE_OFF"       # new, but automatic creation is off
HOLD_CAP = "HOLD_CAP"             # new, but over the per-run cap
HOLD_TAKEN = "HOLD_NAME_TAKEN"    # DisplayName already used by a Customer / Employee / Vendor
FAILED = "FAILED"


def vendor_key(name: str) -> str:
    return norm(name)


def display_name(supplier: str) -> str:
    """QBO DisplayName for an EPOS supplier: whitespace collapsed, edge punctuation and the
    characters QBO refuses in names (``:`` tab newline) removed, at most 100 characters."""
    text = re.sub(r"[:\t\r\n]+", " ", clean(supplier))
    text = " ".join(text.split()).strip(" ,.;-_/")
    return text[:100].rstrip()


def score(name: str, vendor: dict, bill_counts=None) -> float:
    """``vendors-suggest`` score: name similarity, boosted to 0.86 when the first word (>= 4
    letters) matches a vendor that has bills."""
    s = sim(name, vendor.get("DisplayName", ""))
    a, b = vendor_key(name).split(), norm(vendor.get("DisplayName", "")).split()
    if a and b and a[0] == b[0] and len(a[0]) >= 4 and (bill_counts or {}).get(vendor.get("Id"), 0) > 0:
        s = max(s, 0.86)
    return s


def rank(name: str, vendors, bill_counts=None) -> list[tuple[float, dict]]:
    scored = [(score(name, v, bill_counts), v) for v in vendors]
    return sorted(scored, key=lambda t: (-t[0], -(bill_counts or {}).get(t[1].get("Id"), 0), str(t[1].get("Id"))))


def candidate_text(ranked, n: int = 3) -> list[str]:
    out = []
    for s, v in ranked[:n]:
        state = "" if v.get("Active", True) else " (inactive)"
        out.append(f"{v.get('Id')} {v.get('DisplayName')}{state} ({s:.2f})")
    return out


def auto_settings(env=None) -> dict | None:
    """{'ref', 'max'} when automatic vendor creation is on, else None."""
    env = os.environ if env is None else env
    ref = clean(env.get(REF_ENV))
    on = clean(env.get(AUTO_ENV)).lower() in {"1", "true", "yes", "on"}
    if not (on and ref):
        return None
    raw = clean(env.get(MAX_ENV))
    try:
        cap = int(raw) if raw else DEFAULT_MAX
    except ValueError:
        cap = DEFAULT_MAX
    return {"ref": ref, "max": max(0, cap)}


def plan_actions(suppliers: list[dict], vendors: dict, *, history=None, bill_counts=None) -> list[dict]:
    """One action per unmapped supplier. ``suppliers``: [{"key", "name", "supplier_id", "po_refs"}].
    Pure (no HTTP)."""
    pool = list(vendors.values())
    actions = []
    for sup in suppliers:
        name = display_name(sup["name"])
        ranked = rank(name, pool, bill_counts)
        best = ranked[0][0] if ranked else 0.0
        act = {"key": sup["key"], "epos_name": sup["name"], "display_name": name,
               "supplier_id": sup.get("supplier_id", ""), "po_refs": sorted(sup.get("po_refs", [])),
               "best_score": round(best, 3), "candidates": candidate_text(ranked), "vendor_id": "", "detail": ""}
        hist = (history or {}).get(sup["key"])
        if hist:
            vid, n = hist.most_common(1)[0]
            v = vendors.get(vid)
            if v is not None:
                act["candidates"] = [f"{vid} {v.get('DisplayName')} (September bills: {n})"] + act["candidates"]
                act["best_score"] = 1.0
                best = 1.0
        if not name:
            act["state"], act["detail"] = HOLD_NEAR, "supplier name is empty after cleaning; map it by hand"
        elif best >= NEW_VENDOR_MAX_SCORE:
            act["state"] = HOLD_NEAR
            act["detail"] = (f"supplier '{sup['name']}' looks like an existing QBO vendor (best {best:.2f}): "
                             f"{'; '.join(act['candidates'])} - approve the right one in vendors.csv "
                             f"(or create it with vendor_admin if it is really new)")
        else:
            act["state"] = CREATE
            act["detail"] = f"new vendor '{name}' (best existing match {best:.2f})"
        actions.append(act)
    return actions


def name_taken(client, name: str) -> list[str]:
    """Vendors / Customers / Employees already using this DisplayName (GET only)."""
    safe = qbo_escape(name)
    hits = []
    for ent in NAME_ENTITIES:
        rows = client.query(f"select Id, DisplayName from {ent} where DisplayName = '{safe}'").get(ent) or []
        for x in rows if isinstance(rows, list) else [rows]:
            hits.append(f"{ent} {x.get('Id')}")
    return hits


def vendor_requestid(name: str) -> str:
    return sha256_text(f"vendor_create|{REALM}|{name.casefold()}")[:36]


def create_vendor(client, name: str) -> dict:
    """POST one Vendor (DisplayName = CompanyName = ``name``) and re-read it. Raises StopRun."""
    resp = client.post_json("/vendor", {"DisplayName": name, "CompanyName": name[:100]}, vendor_requestid(name))
    if resp.status_code != 200:
        raise StopRun(f"POST /vendor {name!r} failed {resp.status_code}: {resp.text[:300]}")
    got = resp.json().get("Vendor") or {}
    if not got.get("Id"):
        raise StopRun(f"POST /vendor {name!r} returned no Id")
    live = client.get_json(f"/vendor/{got['Id']}").get("Vendor") or {}
    if clean(live.get("DisplayName")) != name:
        raise StopRun(f"vendor {got['Id']} DisplayName {live.get('DisplayName')!r} != {name!r}")
    return live


def read_vendor_rows(path: Path) -> list[dict]:
    return read_csv(path) if Path(path).exists() else []


def write_vendor_rows(path: Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_csv(path, rows, VENDOR_COLS)


def append_vendor_rows(path: Path, new_rows: list[dict]) -> None:
    write_vendor_rows(path, read_vendor_rows(path) + list(new_rows))


def apply_actions(actions: list[dict], *, client, vendors_path: Path, settings: dict | None) -> list[dict]:
    """Create the CREATE actions when ``settings`` (auto gates) allow; mutates and returns ``actions``.

    ``client`` must allow writes when anything is created. Each created vendor is appended to
    ``vendors_path`` immediately (so a crash later never orphans it)."""
    created = 0
    for act in actions:
        if act["state"] != CREATE:
            continue
        if settings is None:
            act["state"] = HOLD_GATE
            act["detail"] += f" - automatic vendor creation is off ({AUTO_ENV}=1 + {REF_ENV}); create/map by hand"
            continue
        if created >= settings["max"]:
            act["state"] = HOLD_CAP
            act["detail"] += f" - over the {settings['max']} new vendor(s) per run cap ({MAX_ENV}); next run or by hand"
            continue
        try:
            taken = name_taken(client, act["display_name"])
            if taken:
                act["state"] = HOLD_TAKEN
                act["detail"] += f" - DisplayName already used by {', '.join(taken)}; map or rename by hand"
                continue
            live = create_vendor(client, act["display_name"])
        except StopRun as exc:
            act["state"], act["detail"] = FAILED, f"vendor create failed: {exc}"
            continue
        created += 1
        act["state"], act["vendor_id"] = CREATED, str(live["Id"])
        act["vendor"] = live
        act["detail"] = f"created QBO vendor {live['Id']} '{live.get('DisplayName')}' (auto:{settings['ref']})"
        append_vendor_rows(vendors_path, [{
            "EPOS Supplier Id": act["supplier_id"], "EPOS Supplier Name": act["epos_name"],
            "QBO Vendor Id": str(live["Id"]), "QBO Vendor Name": live.get("DisplayName", ""),
            "Approved By": f"auto:{settings['ref']}"}])
    return actions

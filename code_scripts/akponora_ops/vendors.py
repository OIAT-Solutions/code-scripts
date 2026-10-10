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

Suppliers excluded in ``review_exclusions`` (kind ``vendor``) are ``EXCLUDED``: never created, and
``bills_sync`` holds their bills with reason ``supplier excluded``.

Manual approval of a held supplier (the portal's "Needs your attention" inbox calls this):

    python -m code_scripts.akponora_ops.vendors approve --epos-name N \
        (--link-to <QBO vendor id> | --create [--display-name D]) --approval-ref R \
        [--epos-supplier-id X] (--dry-run | --expect-sha S) [--vendors PATH] [--out DIR]

``--dry-run`` (GET only) runs the preflight (supplier not already approved in vendors.csv to another
vendor, not excluded in review_exclusions, linked vendor exists and is active, new DisplayName free
across Vendors / Customers / Employees) and prints the deterministic payload and its
``payload_sha256``. The write needs ``--expect-sha`` equal to that sha (recomputed live), then
creates (POST /vendor, deterministic requestid) or links, re-reads the vendor, appends the
vendors.csv row with ``Approved By = <approval ref>``, verifies the row resolves, and writes a
receipt JSON plus one audit line (``STATE_ROOT/ops/company_a/vendors/``). Exit 0 done / dry-run
clean / already approved, 2 preflight problem or stop, 4 refused (missing / wrong sha or ref).

Never renames, merges, inactivates or edits an existing vendor.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from code_scripts.akponora_ops.common import REALM, read_csv, write_csv
from code_scripts.product_conversion import clean
from code_scripts.scripts.akponora_cutover.bills_from_epos_pos import norm, sim
from code_scripts.scripts.akponora_cutover.w7_create_items import StopRun, canonical_json, qbo_escape, sha256_text

VENDOR_COLS = ["EPOS Supplier Id", "EPOS Supplier Name", "QBO Vendor Id", "QBO Vendor Name", "Approved By"]
AUTO_ENV = "OIAT_COMPANY_A_VENDOR_AUTO_CREATE"
REF_ENV = "OIAT_COMPANY_A_VENDOR_APPROVAL_REF"
MAX_ENV = "OIAT_COMPANY_A_VENDOR_AUTO_MAX"
DEFAULT_MAX = 5
NEW_VENDOR_MAX_SCORE = 0.75
NAME_ENTITIES = ("Vendor", "Customer", "Employee")

# action states
CREATE = "CREATE"                 # genuinely new; would be / will be created
LINK = "LINK"                     # same name as one existing QBO vendor bar capitals / spacing: link it
LINKED = "LINKED"
CREATED = "CREATED"
HOLD_NEAR = "HOLD_NEAR_MATCH"     # possible typo / duplicate of an existing vendor
HOLD_GATE = "HOLD_GATE_OFF"       # new, but automatic creation is off
HOLD_CAP = "HOLD_CAP"             # new, but over the per-run cap
HOLD_TAKEN = "HOLD_NAME_TAKEN"    # DisplayName already used by a Customer / Employee / Vendor
EXCLUDED = "EXCLUDED"             # review_exclusions kind=vendor: never created, never asked again
FAILED = "FAILED"


def vendor_key(name: str) -> str:
    return norm(name)


def display_name(supplier: str) -> str:
    """QBO DisplayName for an EPOS supplier: whitespace collapsed, edge punctuation and the
    characters QBO refuses in names (``:`` tab newline) removed, at most 100 characters."""
    text = re.sub(r"[:\t\r\n]+", " ", clean(supplier))
    text = " ".join(text.split()).strip(" ,.;-_/")
    return text[:100].rstrip()


def same_name(a: str, b: str) -> bool:
    """Equal apart from capital letters, spacing and edge punctuation (owner, 5 Oct 2026:
    'MEGA FROZEN FOODS' = QBO 'Mega frozen Foods')."""
    def k(x):
        return " ".join(clean(x).split()).strip(" ,.;:-_/").casefold()
    return bool(k(a)) and k(a) == k(b)


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


def plan_actions(suppliers: list[dict], vendors: dict, *, history=None, bill_counts=None,
                 exclusions=None) -> list[dict]:
    """One action per unmapped supplier. ``suppliers``: [{"key", "name", "supplier_id", "po_refs"}].
    ``exclusions``: a ``review_exclusions.Exclusions``; an excluded supplier is ``EXCLUDED`` (never
    created). Pure (no HTTP)."""
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
        why = exclusions.describe("vendor", sup["name"]) if exclusions is not None else ""
        twins = [v for v in pool if v.get("Active", True) and same_name(name, v.get("DisplayName", ""))]
        if why:
            act["state"], act["detail"] = EXCLUDED, f"supplier excluded: {why}"
        elif len(twins) == 1:
            act["state"], act["vendor_id"] = LINK, str(twins[0].get("Id"))
            act["detail"] = (f"same name as QBO vendor {twins[0].get('Id')} '{twins[0].get('DisplayName')}' "
                             "except capital letters / spacing: linked automatically")
        elif not name:
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
        if act["state"] == LINK:
            # mapping only (no QBO write): the vendor already exists with the same name
            act["state"] = LINKED
            append_vendor_rows(vendors_path, [{
                "EPOS Supplier Id": act["supplier_id"], "EPOS Supplier Name": act["epos_name"],
                "QBO Vendor Id": act["vendor_id"], "QBO Vendor Name": act["detail"].split("'")[1],
                "Approved By": "auto-link: same name bar capitals/spacing (owner yes 2026-10-05)"}])
            continue
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


# ---------------------------------------------------------------- manual approval (approve CLI)
class ApproveRefused(StopRun):
    """approve refused before any write (exit 4)."""


def approved_rows_for(rows: list[dict], key: str, supplier_id: str = "") -> list[dict]:
    """Approved vendors.csv rows that already resolve this supplier (by name key or EPOS supplier id)."""
    out = []
    for r in rows:
        if not clean(r.get("Approved By")) or not clean(r.get("QBO Vendor Id")):
            continue
        sid = clean(r.get("EPOS Supplier Id"))
        if vendor_key(r.get("EPOS Supplier Name", "")) == key or (supplier_id and sid and sid == supplier_id):
            out.append(r)
    return out


def approval_preflight(client, *, epos_name: str, link_to: str = "", create: bool = False,
                       display: str = "", supplier_id: str = "", vendors_path: Path, exclusions=None) -> dict:
    """GET-only. Returns {"payload", "payload_sha256", "problems", "warnings", "already", "vendor"}.

    The payload (and so its sha) is deterministic for the same inputs and the same live QBO state;
    it carries no approval ref and no timestamp."""
    epos_name, link_to, supplier_id = clean(epos_name), clean(link_to), clean(supplier_id)
    if bool(link_to) == bool(create):
        raise ApproveRefused("give exactly one of --link-to <QBO vendor id> or --create")
    key = vendor_key(epos_name)
    problems, warnings = [], []
    if not key:
        problems.append("EPOS supplier name is empty after normalizing")
    why = exclusions.describe("vendor", epos_name) if exclusions is not None and key else ""
    if why:
        problems.append(f"supplier is excluded in review_exclusions ({why}); remove the exclusion first")
    payload = {"action": "link" if link_to else "create", "realm": REALM, "epos_supplier_name": epos_name,
               "epos_key": key, "epos_supplier_id": supplier_id}
    live = None
    if link_to:
        if not link_to.isdigit():
            problems.append(f"--link-to {link_to!r} is not a QBO vendor Id")
        else:
            try:
                live = client.get_json(f"/vendor/{link_to}").get("Vendor") or None
            except (StopRun, KeyError) as exc:
                problems.append(f"QBO vendor {link_to} not readable: {exc}")
            else:
                if live is None:
                    problems.append(f"QBO vendor {link_to} not found")
                elif live.get("Active") is False:
                    problems.append(f"QBO vendor {link_to} {live.get('DisplayName')!r} is inactive")
        payload.update(qbo_vendor_id=link_to, qbo_vendor_name=clean((live or {}).get("DisplayName")))
    else:
        name = display_name(display or epos_name)
        if display and name != clean(display):
            warnings.append(f"display name normalized to {name!r}")
        if not name:
            problems.append("DisplayName is empty after cleaning")
        else:
            taken = name_taken(client, name)
            if taken:
                problems.append(f"DisplayName {name!r} already used by {', '.join(taken)}; use --link-to or "
                                "--display-name")
        payload["qbo_payload"] = {"DisplayName": name, "CompanyName": name[:100]}
    existing = approved_rows_for(read_vendor_rows(vendors_path), key, supplier_id) if key else []
    ids = sorted({clean(r["QBO Vendor Id"]) for r in existing})
    already = None
    if existing and (create or ids == [link_to]) and len(ids) == 1 and not why:
        already = existing[0]  # idempotent retry: this supplier is already approved -> no write
        problems = [p for p in problems if "already used by" not in p]
    elif ids:
        problems.append(f"vendors.csv already maps supplier '{epos_name}' to QBO vendor(s) {', '.join(ids)}; "
                        "fix vendors.csv by hand first")
    return {"payload": payload, "payload_sha256": sha256_text(canonical_json(payload)), "problems": problems,
            "warnings": warnings, "already": already, "vendor": live}


def _write_evidence(state: Path, out: Path | None, receipt: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%fZ")
    rdir = Path(state) / "receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"approve_{stamp}.json"
    text = json.dumps(receipt, indent=1, default=str) + "\n"
    path.write_text(text, encoding="utf-8")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "vendor_approve_receipt.json").write_text(text, encoding="utf-8")
    keys = ("ts", "result", "action", "epos_supplier_name", "qbo_vendor_id", "qbo_vendor_name", "approval_ref",
            "payload_sha256")
    with open(Path(state) / "audit.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: receipt.get(k) for k in keys}, default=str) + "\n")
    return path


def approve(client, *, epos_name: str, approval_ref: str, vendors_path: Path, state: Path, link_to: str = "",
            create: bool = False, display: str = "", supplier_id: str = "", dry_run: bool = False,
            expect_sha: str = "", exclusions=None, out: Path | None = None) -> dict:
    """Manual approval of one EPOS supplier. Raises ApproveRefused (exit 4) / StopRun (exit 2).

    ``result``: ``dry_run`` | ``preflight_failed`` | ``already_approved`` | ``created`` | ``linked``."""
    approval_ref = clean(approval_ref)
    if not approval_ref:
        raise ApproveRefused("--approval-ref is required (the chat-yes / portal approval reference)")
    pf = approval_preflight(client, epos_name=epos_name, link_to=link_to, create=create, display=display,
                            supplier_id=supplier_id, vendors_path=vendors_path, exclusions=exclusions)
    result = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dry_run": dry_run,
              "action": pf["payload"]["action"], "epos_supplier_name": clean(epos_name), "approval_ref": approval_ref,
              "payload": pf["payload"], "payload_sha256": pf["payload_sha256"], "problems": pf["problems"],
              "warnings": pf["warnings"], "qbo_vendor_id": "", "qbo_vendor_name": "", "vendors_csv": str(vendors_path)}
    if pf["problems"]:
        result["result"] = "preflight_failed"
        return result
    if pf["already"] is not None:
        row = pf["already"]
        result.update(result="already_approved", qbo_vendor_id=clean(row["QBO Vendor Id"]),
                      qbo_vendor_name=clean(row.get("QBO Vendor Name")))
        return result
    if dry_run:
        result["result"] = "dry_run"
        return result
    if not clean(expect_sha) or clean(expect_sha) != pf["payload_sha256"]:
        raise ApproveRefused(f"--expect-sha mismatch: live payload sha is {pf['payload_sha256']}, expected "
                             f"{clean(expect_sha) or '(none)'}; re-run --dry-run and re-approve")
    payload = pf["payload"]
    if payload["action"] == "create":
        live = create_vendor(client, payload["qbo_payload"]["DisplayName"])
    else:
        live = client.get_json(f"/vendor/{payload['qbo_vendor_id']}").get("Vendor") or {}
        if clean(live.get("DisplayName")) != payload["qbo_vendor_name"]:
            raise StopRun(f"vendor {payload['qbo_vendor_id']} changed during approval ({live.get('DisplayName')!r})")
    if live.get("Active") is False:
        raise StopRun(f"vendor {live.get('Id')} is inactive after the write")
    vid, vname = str(live["Id"]), clean(live.get("DisplayName"))
    append_vendor_rows(vendors_path, [{"EPOS Supplier Id": clean(supplier_id), "EPOS Supplier Name": clean(epos_name),
                                       "QBO Vendor Id": vid, "QBO Vendor Name": vname, "Approved By": approval_ref}])
    check = approved_rows_for(read_vendor_rows(vendors_path), payload["epos_key"], clean(supplier_id))
    if {clean(r["QBO Vendor Id"]) for r in check} != {vid}:
        raise StopRun(f"vendors.csv does not resolve '{epos_name}' to {vid} after the update")
    result.update(result="created" if payload["action"] == "create" else "linked", qbo_vendor_id=vid,
                  qbo_vendor_name=vname)
    result["receipt"] = str(_write_evidence(state, out, result))
    return result


def main(argv=None) -> int:
    from code_scripts.akponora_ops import review_exclusions
    from code_scripts.akponora_ops.common import mapping_file, state_dir
    from code_scripts.scripts.akponora_cutover._common import setup_env
    from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("approve", help="link or create the QBO vendor for one held EPOS supplier")
    p.add_argument("--epos-name", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--link-to", default="", help="existing QBO vendor Id")
    g.add_argument("--create", action="store_true", help="create a new QBO vendor")
    p.add_argument("--display-name", default="", help="DisplayName for --create (default: cleaned EPOS name)")
    p.add_argument("--epos-supplier-id", default="")
    p.add_argument("--approval-ref", default="")
    p.add_argument("--dry-run", action="store_true", help="GET only: preflight + payload sha")
    p.add_argument("--expect-sha", default="", help="payload_sha256 from the dry-run (required to write)")
    p.add_argument("--vendors", default=None, help="override STATE_ROOT/mappings/company_a/vendors.csv")
    p.add_argument("--out", default=None, help="also write vendor_approve_receipt.json here")
    a = ap.parse_args(argv)
    if a.display_name and not a.create:
        ap.error("--display-name only goes with --create")
    setup_env()
    vendors_path = Path(a.vendors) if a.vendors else mapping_file().parent / "vendors.csv"
    try:
        client = QBOClient.for_company_a(allow_writes=not a.dry_run)
        res = approve(client, epos_name=a.epos_name, approval_ref=a.approval_ref, vendors_path=vendors_path,
                      state=state_dir("vendors"), link_to=a.link_to, create=a.create, display=a.display_name,
                      supplier_id=a.epos_supplier_id, dry_run=a.dry_run, expect_sha=a.expect_sha,
                      exclusions=review_exclusions.load(review_exclusions.path_near(vendors_path)),
                      out=Path(a.out) if a.out else None)
    except ApproveRefused as exc:
        print(json.dumps({"result": "refused", "error": str(exc)}))
        return 4
    except StopRun as exc:
        print(json.dumps({"result": "stopped", "error": str(exc)}))
        return 2
    print(json.dumps(res, indent=1, default=str))
    return 2 if res["result"] == "preflight_failed" else 0


if __name__ == "__main__":
    sys.exit(main())

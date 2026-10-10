#!/usr/bin/env python3
"""W5 legacy item rename for AKPONORA VENTURES LTD (company_a).

Renames colliding legacy items to ``LEGACY — {name}`` using a sparse Item update
that sends ONLY {sparse, Id, SyncToken, Name}. ``--rollback`` renames them back
from rollback.csv (NewName -> OldName) with the same checks.

DEFAULT IS DRY-RUN (GET requests only). Nothing is written to QBO unless
``--execute`` is passed. W5 needs the owner's chat yes (AGENTS.md).

Modes (one required):
  --test      the 10 plan rows with TestRank 1..10 (active Inventory, qty 0, no Sep sales)
  --all       every plan row not flagged INACTIVE_SKIP_NO_REACTIVATE
  --rollback  every rollback.csv row: rename NewName back to OldName

Safety:
  * fresh GET before each update; the current name must still match the plan
    (rows already at the target name are skipped)
  * target name must not already exist (read-only query) before the update
  * re-read after update; Name must equal the target and Type/Active/QtyOnHand/
    PurchaseCost/UnitPrice/Income/Expense/Asset accounts/Sku/InvStartDate/ParentRef/
    TrackQtyOnHand/SubItem must be unchanged
  * Inventory asset account balances (77 + all 120000 children) read before and after
    (and every --balance-every items); any change stops the run
  * <=5 requests/s, retries on 429/5xx/network, re-reads on stale SyncToken
  * resumable: executed rows are appended to the results CSV and skipped on the next run
  * stops on the first verification failure (exit code 2)

Examples:
  python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --test              # dry-run, GETs only
  python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --test --execute    # owner only
  python -m code_scripts.scripts.akponora_cutover.w5_legacy_rename --rollback --limit 5  # dry-run rollback
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import COMPANY_KEY, GAPS_DIR, company_config

W5_DIR = GAPS_DIR / "w5_legacy_rename"
MINOR = "75"
PREFIX = "LEGACY — "
CATCH_ALL_ID = "15030"
# 77 Inventory Asset; 120000 Inventory and its children (120100, 120200, 120201, 120202, 120300)
BALANCE_ACCOUNTS = ["77", "1150040008", "1150040011", "1150040012", "1150040029", "1150040030", "1150040013"]
VERIFY_FIELDS = ["Type", "Active", "QtyOnHand", "PurchaseCost", "UnitPrice", "IncomeAccountRef",
                 "ExpenseAccountRef", "AssetAccountRef", "Sku", "InvStartDate", "ParentRef",
                 "TrackQtyOnHand", "SubItem"]
RESULT_COLS = ["ts", "mode", "Id", "status", "OldName", "NewName", "SyncTokenBefore", "SyncTokenAfter", "detail"]
DONE_STATUSES = {"RENAMED", "ALREADY_RENAMED"}
ROLLBACK_DONE_STATUSES = {"ROLLED_BACK", "ALREADY_ROLLED_BACK"}


class StopRun(Exception):
    pass


class QBO:
    def __init__(self, execute: bool, max_rps: float, company_key: str = COMPANY_KEY):
        import requests

        cfg = company_config(company_key)  # sets OIAT_COMPANIES_DIR before code_scripts.paths is imported
        from code_scripts.company_config import get_qbo_api_base_url
        from code_scripts.token_manager import get_access_token

        self._requests = requests
        self._get_token = lambda: get_access_token(cfg.company_key, cfg.realm_id)
        self.base = f"{get_qbo_api_base_url(cfg.qbo_environment)}/v3/company/{cfg.realm_id}"
        self.execute = execute
        self.min_interval = 1.0 / max_rps
        self._last = 0.0
        self._token = self._get_token()
        self.requests = 0

    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _call(self, method: str, path: str, *, params=None, body=None, retries=6):
        if method != "GET" and not self.execute:
            raise RuntimeError("refusing non-GET request in dry-run mode")
        params = dict(params or {}, minorversion=MINOR)
        delay = 2.0
        requests = self._requests
        for attempt in range(retries + 1):
            self._throttle()
            self.requests += 1
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            try:
                if method == "GET":
                    r = requests.get(self.base + path, params=params, headers=headers, timeout=60)
                else:
                    headers["Content-Type"] = "application/json"
                    r = requests.post(self.base + path, params=params, headers=headers, data=json.dumps(body), timeout=60)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise StopRun(f"network error after retries: {exc}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 401 and attempt < retries:
                self._token = self._get_token()
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            return r
        return r

    def get_json(self, path, params=None):
        r = self._call("GET", path, params=params)
        if r.status_code != 200:
            raise StopRun(f"GET {path} failed {r.status_code}: {r.text[:300]}")
        return r.json()

    def query(self, q):
        return self.get_json("/query", {"query": q}).get("QueryResponse", {})

    def item(self, item_id):
        return self.get_json(f"/item/{item_id}")["Item"]

    def sparse_rename(self, item_id, sync_token, name):
        body = {"sparse": True, "Id": str(item_id), "SyncToken": str(sync_token), "Name": name}
        return self._call("POST", "/item", body=body, retries=3)


def balances(qbo: QBO) -> dict:
    ids = ",".join(f"'{a}'" for a in BALANCE_ACCOUNTS)
    rows = qbo.query(f"select Id, Name, CurrentBalance from Account where Id in ({ids})").get("Account", [])
    got = {a["Id"]: a.get("CurrentBalance") for a in rows}
    missing = set(BALANCE_ACCOUNTS) - set(got)
    if missing:
        raise StopRun(f"balance accounts missing from query: {sorted(missing)}")
    return got


def snapshot(item: dict) -> dict:
    return {f: item.get(f) for f in VERIFY_FIELDS}


def name_exists(qbo: QBO, name: str) -> list:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    rows = qbo.query(f"select Id, Name, Active from Item where Name = '{safe}' maxresults 10").get("Item", [])
    return [r["Id"] for r in rows]


def is_stale(resp) -> bool:
    try:
        errs = resp.json()["Fault"]["Error"]
    except Exception:
        return False
    return any(str(e.get("code")) == "5010" or "stale" in (e.get("Message", "") + e.get("Detail", "")).lower()
               for e in errs)


def valid_new_name(name: str) -> bool:
    return name.startswith(PREFIX) and len(name) <= 100 and ":" not in name


def load_plan(path: Path, mode: str) -> list:
    """Rows with 'from' (current live name) and 'to' (target name)."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    out = []
    for r in rows:
        if r["Id"] == CATCH_ALL_ID or "INACTIVE_SKIP_NO_REACTIVATE" in r["Flags"] or r["Active"] != "True":
            continue
        if not valid_new_name(r["NewName"]):
            raise SystemExit(f"plan row {r['Id']} has an invalid NewName")
        if mode == "test":
            if r["TestRank"] and int(r["TestRank"]) <= 10:
                out.append(r)
        else:
            out.append(r)
    if mode == "test":
        out.sort(key=lambda r: int(r["TestRank"]))
    for r in out:
        r["from"], r["to"] = r["OldName"], r["NewName"]
    return out


def load_rollback(path: Path) -> list:
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r["Id"] == CATCH_ALL_ID:
            continue
        if not valid_new_name(r["NewName"]) or not r["OldName"].strip() or len(r["OldName"]) > 100:
            raise SystemExit(f"rollback row {r['Id']} is not a valid 'LEGACY — ' rename")
        r["from"], r["to"] = r["NewName"], r["OldName"]
        out.append(r)
    return out


def done_ids(results: Path, statuses: set) -> set:
    if not results.exists():
        return set()
    return {r["Id"] for r in csv.DictReader(open(results, encoding="utf-8")) if r["status"] in statuses}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true")
    g.add_argument("--all", action="store_true")
    g.add_argument("--rollback", action="store_true", help="rename NewName back to OldName from --rollback-csv")
    ap.add_argument("--execute", action="store_true", help="actually POST sparse updates (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (the default)")
    ap.add_argument("--w5-dir", type=Path, default=W5_DIR, help="folder holding plan/rollback/results (default %(default)s)")
    ap.add_argument("--plan", type=Path, default=None, help="default <w5-dir>/plan.csv")
    ap.add_argument("--rollback-csv", type=Path, default=None, help="default <w5-dir>/rollback.csv")
    ap.add_argument("--results", type=Path, default=None,
                    help="CSV the run appends to (default <w5-dir>/results.csv | results_dryrun.csv | "
                         "rollback_results.csv | rollback_results_dryrun.csv)")
    ap.add_argument("--done-results", type=Path, default=None,
                    help="executed results used to skip finished rows (default <w5-dir>/results.csv, "
                         "or rollback_results.csv with --rollback)")
    ap.add_argument("--ids", default="", help="comma-separated item Ids to restrict to")
    ap.add_argument("--limit", type=int, default=0, help="process at most N pending rows")
    ap.add_argument("--max-rps", type=float, default=5.0)
    ap.add_argument("--balance-every", type=int, default=250)
    ap.add_argument("--company", default=COMPANY_KEY)
    a = ap.parse_args(argv)
    if a.execute and a.dry_run:
        ap.error("--execute and --dry-run are mutually exclusive")
    if a.max_rps > 5:
        ap.error("--max-rps must be <= 5")
    mode = "test" if a.test else "all" if a.all else "rollback"
    execute = a.execute
    rb = mode == "rollback"
    stem = ("rollback_results" if rb else "results") + ("" if execute else "_dryrun")
    results = a.results or a.w5_dir / f"{stem}.csv"
    done_file = a.done_results or a.w5_dir / ("rollback_results.csv" if rb else "results.csv")
    done_ok, already_status, ok_status = ((ROLLBACK_DONE_STATUSES, "ALREADY_ROLLED_BACK", "ROLLED_BACK") if rb
                                          else (DONE_STATUSES, "ALREADY_RENAMED", "RENAMED"))

    plan = load_rollback(a.rollback_csv or a.w5_dir / "rollback.csv") if rb else load_plan(a.plan or a.w5_dir / "plan.csv", mode)
    if a.ids:
        wanted = {x.strip() for x in a.ids.split(",") if x.strip()}
        plan = [r for r in plan if r["Id"] in wanted]
    skip = done_ids(done_file, done_ok)
    pending = [r for r in plan if r["Id"] not in skip]
    if a.limit:
        pending = pending[: a.limit]
    print(f"mode={mode} {'EXECUTE' if execute else 'DRY-RUN'} plan_rows={len(plan)} "
          f"already_done={len([r for r in plan if r['Id'] in skip])} pending={len(pending)} results={results}")
    if not pending:
        return 0

    qbo = QBO(execute=execute, max_rps=a.max_rps, company_key=a.company)
    results.parent.mkdir(parents=True, exist_ok=True)
    new_file = not results.exists()
    fh = open(results, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(fh, RESULT_COLS)
    if new_file:
        w.writeheader()

    def log(row, status, before="", after="", detail=""):
        w.writerow({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": mode + ("" if execute else "-dry"),
                    "Id": row["Id"], "status": status, "OldName": row["OldName"], "NewName": row["NewName"],
                    "SyncTokenBefore": before, "SyncTokenAfter": after, "detail": detail})
        fh.flush()

    bal0 = balances(qbo)
    print("balances before:", json.dumps(bal0))
    counts = {}
    exit_code = 0
    try:
        for n, row in enumerate(pending, 1):
            item = qbo.item(row["Id"])
            if item["Name"] == row["to"]:
                log(row, already_status, item["SyncToken"], detail="name already equals target")
                counts[already_status] = counts.get(already_status, 0) + 1
                continue
            if item["Name"] != row["from"]:
                log(row, "FAIL_PRECHECK", item["SyncToken"], detail=f"live Name differs from plan: {item['Name']!r}")
                raise StopRun(f"Id {row['Id']}: live Name differs from plan")
            if not item.get("Active") or (not rb and item.get("Type") != row["Type"]):
                log(row, "FAIL_PRECHECK", item["SyncToken"], detail=f"Active={item.get('Active')} Type={item.get('Type')}")
                raise StopRun(f"Id {row['Id']}: not active or type changed")
            clash = name_exists(qbo, row["to"])
            if clash:
                log(row, "FAIL_PRECHECK", item["SyncToken"], detail=f"target name already used by {clash}")
                raise StopRun(f"Id {row['Id']}: target name already used by {clash}")
            before = snapshot(item)
            if not rb and str(item["SyncToken"]) != row["SyncToken"]:
                print(f"  note Id {row['Id']}: SyncToken moved {row['SyncToken']} -> {item['SyncToken']} since plan")

            if not execute:
                payload = {"sparse": True, "Id": row["Id"], "SyncToken": item["SyncToken"], "Name": row["to"]}
                log(row, "DRY_RUN_OK", item["SyncToken"], detail=json.dumps(payload, ensure_ascii=False))
                counts["DRY_RUN_OK"] = counts.get("DRY_RUN_OK", 0) + 1
                print(f"  [{n}/{len(pending)}] DRY {row['Id']}: {row['from']!r} -> {row['to']!r}")
                continue

            token = item["SyncToken"]
            for attempt in range(4):
                resp = qbo.sparse_rename(row["Id"], token, row["to"])
                if resp.status_code == 200:
                    break
                if is_stale(resp) and attempt < 3:
                    item = qbo.item(row["Id"])
                    if item["Name"] != row["from"]:
                        log(row, "FAIL_STALE", token, item["SyncToken"], f"name changed during retry: {item['Name']!r}")
                        raise StopRun(f"Id {row['Id']}: name changed during stale retry")
                    before = snapshot(item)
                    token = item["SyncToken"]
                    continue
                log(row, "FAIL_UPDATE", token, detail=f"{resp.status_code}: {resp.text[:400]}")
                raise StopRun(f"Id {row['Id']}: update failed {resp.status_code}")

            after_item = qbo.item(row["Id"])
            after = snapshot(after_item)
            diffs = {f: (before[f], after[f]) for f in VERIFY_FIELDS if before[f] != after[f]}
            if after_item["Name"] != row["to"]:
                diffs["Name"] = (row["to"], after_item["Name"])
            if diffs:
                log(row, "FAIL_VERIFY", token, after_item["SyncToken"], json.dumps(diffs, default=str, ensure_ascii=False))
                raise StopRun(f"Id {row['Id']}: verification failed {diffs}")
            log(row, ok_status, token, after_item["SyncToken"], "verified")
            counts[ok_status] = counts.get(ok_status, 0) + 1
            print(f"  [{n}/{len(pending)}] OK {row['Id']}: {row['from']!r} -> {row['to']!r}")

            if a.balance_every and n % a.balance_every == 0:
                mid = balances(qbo)
                if mid != bal0:
                    raise StopRun(f"inventory balances changed mid-run: before={bal0} now={mid}")
    except StopRun as exc:
        print(f"STOPPED: {exc}")
        exit_code = 2
    finally:
        try:
            bal1 = balances(qbo)
            print("balances after: ", json.dumps(bal1))
            if bal1 != bal0:
                print("WARNING: inventory balances changed between before and after reads")
                exit_code = 2
        except StopRun as exc:
            print(f"could not read balances after: {exc}")
            exit_code = 2
        fh.close()
    print(f"summary {json.dumps(counts)} requests={qbo.requests} results={results}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

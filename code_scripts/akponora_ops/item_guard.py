"""Read-only QBO guard for the company_a October item/account contract.

Scans production QBO (GET only) for anything changed since the stored cursor and
flags items, transaction lines and balances that break the October contract
(AGENTS.md): new stock items must be ``AKP-`` / ``AKP-NS-`` items listed in the
approved mapping, legacy (``LEGACY —``) items and the catch-all ``15030`` are
frozen, and Inventory Asset ``77`` is the only stock asset (120xxx stays at 0).

Usage::

    .venv/bin/python -m code_scripts.akponora_ops.item_guard [--since ISO] [--out DIR]
        [--no-slack] [--fail-on-alert]

Severities: ALERT (contract broken), WARN (needs a look), INFO (expected activity,
aggregated as counts). Writes ``report.json`` and ``alerts.csv`` (ALERT + WARN) to
the run folder; the cursor and legacy snapshot advance only after a successful scan.
Exit 0, or 4 with ``--fail-on-alert`` when any ALERT exists.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from code_scripts.akponora_ops.common import (
    AKP_NS_SKU_PREFIX,
    AKP_SKU_PREFIX,
    ASSET_ID,
    CATCH_ALL_ITEM_ID,
    COMPANY,
    INV_START,
    LEGACY_PREFIX,
    REALM,
    dump_json,
    mapping_file,
    read_csv,
    run_dir,
    send_slack,
    state_dir,
    write_csv,
)
from code_scripts.product_conversion import MappingValidationError, ProductConversionRegistry, clean
from code_scripts.scripts.akponora_cutover._common import ReadOnlyQBO

TOOL = "item_guard"
DEFAULT_SINCE = "2026-10-01T00:00:00+01:00"
# Naive --since values are read as business time (Africa/Lagos, no DST).
BUSINESS_TZ = timezone(timedelta(hours=1))
# Re-scan a few minutes behind the previous scan start to absorb QBO/local clock skew.
CURSOR_OVERLAP = timedelta(minutes=5)
FUZZY_RATIO = 0.92
INFO_SAMPLE = 25
TOP_NEGATIVE = 20
SLACK_DETAIL_LINES = 6
EXIT_ALERT = 4

ALERT, WARN, INFO = "ALERT", "WARN", "INFO"
STOCK_TYPES = frozenset({"Inventory", "NonInventory"})
FAMILY_120 = re.compile(r"^120\d{3}\b")
BILLS_SYNC_MARK = "created by bills_sync"
ITEM_TXN_ENTITIES = ("Bill", "Purchase", "VendorCredit", "Invoice", "SalesReceipt", "CreditMemo", "RefundReceipt")
# A September-dated document created after the cutover breaks the bookkeeper freeze;
# September sales backfills (SalesReceipt etc.) are expected and only counted.
BACKDATE_WARN_ENTITIES = frozenset({"Bill", "Purchase", "VendorCredit", "Invoice"})
PARTY_REFS = ("VendorRef", "CustomerRef", "EntityRef")

ALERT_COLUMNS = [
    "severity", "check", "entity", "id", "doc_number", "txn_date", "party", "line_item_id", "line_item",
    "account", "amount", "sku", "name", "created_by", "suggestion", "detail",
]


# ---------------------------------------------------------------- small helpers
def parse_ts(value: str) -> datetime:
    """ISO timestamp -> aware datetime (naive values are business time)."""
    dt = datetime.fromisoformat(clean(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=BUSINESS_TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(BUSINESS_TZ).isoformat(timespec="seconds")


def norm_name(value: str) -> str:
    return " ".join(clean(value).casefold().split())


def ref_value(obj: dict | None, key: str) -> str:
    return clean(((obj or {}).get(key) or {}).get("value"))


def ref_name(obj: dict | None, key: str) -> str:
    return clean(((obj or {}).get(key) or {}).get("name"))


def is_akp(item: dict) -> bool:
    return clean(item.get("Sku")).startswith(AKP_SKU_PREFIX)


def is_legacy_named(item: dict) -> bool:
    return clean(item.get("Name")).startswith(LEGACY_PREFIX)


def is_legacy(item: dict) -> bool:
    """Frozen pre-October catalogue: any stock item that is not an AKP item, or is named LEGACY."""
    return item.get("Type") in STOCK_TYPES and (not is_akp(item) or is_legacy_named(item))


def item_snapshot(item: dict) -> dict:
    return {"name": clean(item.get("Name")), "active": bool(item.get("Active", True)), "qty": item.get("QtyOnHand")}


def created_by(meta: dict | None) -> str:
    return ref_name(meta, "LastModifiedByRef") or ref_value(meta, "LastModifiedByRef")


def iter_lines(lines: Iterable[dict]) -> Iterable[dict]:
    for line in lines or []:
        yield line
        nested = (line.get("GroupLineDetail") or {}).get("Line")
        if nested:
            yield from iter_lines(nested)


def money(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{'-' if amount < 0 else ''}\u20a6{abs(amount):,.2f}"


# ---------------------------------------------------------------- mapping
@dataclass
class MappingTargets:
    by_id: dict[str, dict]
    error: str = ""

    @classmethod
    def from_rows(cls, rows: Iterable[dict], error: str = "") -> "MappingTargets":
        by_id = {}
        for row in rows:
            item_id = clean(row.get("Target QBO Item Id"))
            if item_id:
                by_id[item_id] = {
                    "sku": clean(row.get("Target QBO SKU")),
                    "type": clean(row.get("Target QBO Item Type")).replace("Non-inventory", "NonInventory"),
                    "name": clean(row.get("Target QBO Name")),
                }
        return cls(by_id, error)


def load_mapping(path: str | Path) -> MappingTargets:
    """Approved targets from the installed mapping; a missing file fails the scan."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"approved mapping not found: {path}")
    try:
        registry = ProductConversionRegistry.from_csv(path)
    except MappingValidationError as exc:
        return MappingTargets.from_rows(read_csv(path), error=str(exc))
    by_id = {}
    for rule in registry.rules:
        if rule.target_qbo_item_id:
            by_id[rule.target_qbo_item_id] = {
                "sku": rule.target_qbo_sku, "type": rule.target_qbo_type, "name": rule.target_qbo_name,
            }
    return MappingTargets(by_id)


def catalogue_receipt_ids(directory: Path) -> set[str]:
    """Best-effort QBO Item Ids mentioned in catalogue_sync JSON receipts (keys containing 'id')."""
    found: set[str] = set()

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif "id" in key.casefold() and clean(node).isdigit():
            found.add(clean(node))

    if directory.is_dir():
        for path in directory.rglob("*.json"):
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return found


# ---------------------------------------------------------------- scan
@dataclass
class ItemGuard:
    client: object
    targets: MappingTargets
    since: str
    legacy_snapshot: dict | None = None
    catalogue_ids: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.since_dt = parse_ts(self.since)
        self.since = iso(self.since_dt)
        self.findings: list[dict] = []
        self.info: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.stats: dict = {}

    # ---- recording
    def add(self, severity: str, check: str, detail: str, **fields) -> None:
        row = {"severity": severity, "check": check, "detail": detail}
        row.update({k: v for k, v in fields.items() if v not in (None, "")})
        self.findings.append(row)

    def note(self, check: str, sample=None) -> None:
        bucket = self.info.setdefault(check, {"count": 0, "sample": []})
        bucket["count"] += 1
        if sample is not None and len(bucket["sample"]) < INFO_SAMPLE:
            bucket["sample"].append(sample)

    def changed(self, meta: dict | None, key: str = "LastUpdatedTime") -> bool:
        stamp = clean((meta or {}).get(key))
        return bool(stamp) and parse_ts(stamp) >= self.since_dt

    def item_fields(self, item: dict) -> dict:
        return {
            "entity": "Item", "id": clean(item.get("Id")), "name": clean(item.get("Name")),
            "sku": clean(item.get("Sku")), "created_by": created_by(item.get("MetaData")),
        }

    # ---- data loading (cached for the run)
    def load(self) -> None:
        active = self.client.query_all("select * from Item", "Item")
        inactive = self.client.query_all(
            f"select * from Item where Active = false and Metadata.LastUpdatedTime >= '{self.since}'", "Item"
        )
        self.items = {clean(i.get("Id")): i for i in [*active, *inactive]}
        accounts = self.client.query_all("select * from Account where Active in (true, false)", "Account")
        self.accounts = {clean(a.get("Id")): a for a in accounts}
        self.inventory_accounts = {ASSET_ID} | self.family_120
        self.akp_names: dict[str, dict] = {}
        self.by_norm_name: dict[str, list[str]] = defaultdict(list)
        for item_id, item in self.items.items():
            self.by_norm_name[norm_name(item.get("Name"))].append(item_id)
            if is_akp(item) and not is_legacy_named(item):
                self.akp_names.setdefault(norm_name(item.get("Name")), item)
        self.stats["items_loaded"] = {"active": len(active), "inactive_changed": len(inactive)}

    @property
    def family_120(self) -> set[str]:
        return {i for i, a in self.accounts.items() if FAMILY_120.match(clean(a.get("Name"))) and i != ASSET_ID}

    def account_label(self, account_id: str) -> str:
        account = self.accounts.get(account_id) or {}
        return clean(account.get("FullyQualifiedName") or account.get("Name")) or account_id

    # ---- check 1: items
    def check_items(self) -> None:
        changed = [i for i in self.items.values() if self.changed(i.get("MetaData"))]
        created = 0
        dup_keys_reported: set[str] = set()
        for item in sorted(changed, key=lambda i: int(clean(i.get("Id")) or 0)):
            is_new = self.changed(item.get("MetaData"), "CreateTime")
            created += is_new
            if item.get("Type") not in STOCK_TYPES:
                if is_new:
                    self.note(f"{item.get('Type') or 'Other'}_item_created", self.item_fields(item))
                continue
            if is_akp(item) and not is_legacy_named(item):
                self.check_akp_item(item, is_new)
            else:
                self.check_non_akp_item(item, is_new)
            key = norm_name(item.get("Name"))
            others = [i for i in self.by_norm_name.get(key, []) if i != clean(item.get("Id"))]
            if others and key not in dup_keys_reported:
                dup_keys_reported.add(key)
                all_legacy = all(is_legacy(self.items[i]) for i in [clean(item.get("Id")), *others])
                self.add(WARN if all_legacy else ALERT, "duplicate_name",
                         "Name differs only by case/whitespace from Id(s) " + ", ".join(others)
                         + (" (all frozen legacy)" if all_legacy else ""), **self.item_fields(item))
        self.stats["items_changed"] = len(changed)
        self.stats["items_created"] = created
        self.check_legacy_renamed_everywhere()

    def check_akp_item(self, item: dict, is_new: bool) -> None:
        fields = self.item_fields(item)
        before = len(self.findings)
        sku, item_type, item_id = fields["sku"], item.get("Type"), fields["id"]
        expected_type = "NonInventory" if sku.startswith(AKP_NS_SKU_PREFIX) else "Inventory"
        if item_type != expected_type:
            self.add(ALERT, "akp_type_mismatch", f"Sku {sku} requires Type {expected_type}, item is {item_type}", **fields)
        target = self.targets.by_id.get(item_id)
        if target is None:
            self.add(ALERT, "akp_item_not_in_mapping", "AKP item Id is not an approved mapping target", **fields)
        else:
            if target["sku"] != sku or target["type"] != item_type:
                self.add(ALERT, "akp_mapping_mismatch",
                         f"Mapping expects {target['type']} {target['sku']}, item is {item_type} {sku}", **fields)
            elif norm_name(target["name"]) != norm_name(item.get("Name")):
                self.add(WARN, "akp_name_drift", f"Mapping name is '{target['name']}'", **fields)
            if not item.get("Active", True):
                self.add(ALERT, "akp_mapped_item_inactive", "Approved mapping target is inactive; sales will fail",
                         **fields)
        if item_type == "Inventory":
            asset = ref_value(item, "AssetAccountRef")
            if asset != ASSET_ID:
                self.add(ALERT, "akp_wrong_asset_account",
                         f"AssetAccountRef {self.account_label(asset)} (must be Inventory Asset {ASSET_ID})", **fields)
            start = clean(item.get("InvStartDate"))
            if start and start < INV_START:
                self.add(ALERT, "akp_inv_start_date", f"InvStartDate {start} is before {INV_START}", **fields)
        if len(self.findings) == before:
            if is_new:
                fields["catalogue_sync_receipt"] = item_id in self.catalogue_ids
                self.note("akp_item_created", fields)
            else:
                self.note("akp_item_updated")

    def check_non_akp_item(self, item: dict, is_new: bool) -> None:
        fields = self.item_fields(item)
        item_id = fields["id"]
        before = len(self.findings)
        if is_new:
            self.add(ALERT, "non_akp_item_created",
                     f"New {item.get('Type')} item without an approved AKP-/AKP-NS- Sku", **fields)
        elif not is_legacy_named(item):
            self.add(ALERT, "legacy_not_frozen",
                     f"Legacy item changed and is not named '{LEGACY_PREFIX.strip()} …' (renamed back?)", **fields)
        if not is_legacy_named(item):
            self.check_fuzzy_duplicate(item)
        if self.legacy_snapshot is not None and not is_new:
            prev = self.legacy_snapshot.get(item_id)
            now = item_snapshot(item)
            if prev is None:
                if now["active"]:
                    self.add(ALERT, "legacy_reactivated", "Legacy item active again (absent from last snapshot)",
                             **fields)
            else:
                if prev["name"] != now["name"]:
                    self.add(ALERT, "legacy_renamed", f"Name changed from '{prev['name']}'", **fields)
                if not prev["active"] and now["active"]:
                    self.add(ALERT, "legacy_reactivated", "Legacy item reactivated", **fields)
                if prev["qty"] != now["qty"] and now["active"]:
                    self.add(ALERT, "legacy_qty_changed", f"QtyOnHand {prev['qty']} -> {now['qty']}", **fields)
        if len(self.findings) == before:
            self.note("legacy_item_updated")

    def check_fuzzy_duplicate(self, item: dict) -> None:
        key = norm_name(item.get("Name"))
        if not key or not self.akp_names:
            return
        match = difflib.get_close_matches(key, list(self.akp_names), n=1, cutoff=FUZZY_RATIO)
        if match:
            akp = self.akp_names[match[0]]
            ratio = difflib.SequenceMatcher(None, key, match[0]).ratio()
            self.add(ALERT, "possible_duplicate", f"Name matches AKP item {akp.get('Id')} (ratio {ratio:.2f})",
                     suggestion=f"possible duplicate of {akp.get('Sku')} ({akp.get('Name')}, Id {akp.get('Id')})",
                     **self.item_fields(item))

    def check_legacy_renamed_everywhere(self) -> None:
        """One aggregated WARN: W5 renamed only legacy names that collide with AKP names."""
        flagged = {f.get("id") for f in self.findings if f.get("entity") == "Item"}
        unrenamed = sorted(
            (item for item_id, item in self.items.items()
             if item.get("Active", True) and item.get("Type") in STOCK_TYPES and not is_akp(item)
             and not is_legacy_named(item) and item_id not in flagged and item_id != CATCH_ALL_ITEM_ID),
            key=lambda i: int(clean(i.get("Id")) or 0),
        )
        self.stats["legacy_not_renamed"] = {
            "count": len(unrenamed),
            "qty_nonzero": sum(1 for i in unrenamed if float(i.get("QtyOnHand") or 0) != 0),
            "items": [{"id": clean(i.get("Id")), "name": clean(i.get("Name")), "type": i.get("Type"),
                       "qty": i.get("QtyOnHand")} for i in unrenamed],
        }
        if unrenamed:
            self.add(WARN, "legacy_not_renamed",
                     f"{len(unrenamed)} active non-AKP stock items lack the LEGACY prefix (still selectable by name); "
                     "see report.json legacy_not_renamed", entity="Item")

    # ---- check 2: transactions
    def check_transactions(self) -> None:
        scanned = {}
        for entity in ITEM_TXN_ENTITIES:
            rows = self.client.query_all(
                f"select * from {entity} where Metadata.LastUpdatedTime >= '{self.since}'", entity
            )
            scanned[entity] = len(rows)
            for txn in rows:
                self.check_txn(entity, txn)
        rows = self.client.query_all(
            f"select * from JournalEntry where Metadata.LastUpdatedTime >= '{self.since}'", "JournalEntry"
        )
        scanned["JournalEntry"] = len(rows)
        for txn in rows:
            self.check_journal(txn)
        self.stats["transactions_scanned"] = scanned

    def txn_fields(self, entity: str, txn: dict) -> dict:
        party = next((ref_name(txn, k) for k in PARTY_REFS if ref_name(txn, k)), "")
        return {
            "entity": entity, "id": clean(txn.get("Id")), "doc_number": clean(txn.get("DocNumber")),
            "txn_date": clean(txn.get("TxnDate")), "party": party, "created_by": created_by(txn.get("MetaData")),
        }

    def check_txn(self, entity: str, txn: dict) -> None:
        base = self.txn_fields(entity, txn)
        if entity == "Bill" and BILLS_SYNC_MARK in clean(txn.get("PrivateNote")).casefold():
            self.note("bills_sync_bills", {"id": base["id"], "doc_number": base["doc_number"]})
        if base["txn_date"] < INV_START:
            if entity in BACKDATE_WARN_ENTITIES and self.changed(txn.get("MetaData"), "CreateTime"):
                self.add(WARN, "backdated_txn", f"{entity} dated before {INV_START} created after the cutover",
                         amount=txn.get("TotalAmt"), **base)
            else:
                self.note(f"pre_october_{entity}_changed")
            return
        for line in iter_lines(txn.get("Line")):
            amount = line.get("Amount")
            detail = (line.get("SalesItemLineDetail") or line.get("ItemBasedExpenseLineDetail"))
            if detail and ref_value(detail, "ItemRef"):
                self.check_item_line(base, detail, amount)
            account_detail = line.get("AccountBasedExpenseLineDetail")
            if account_detail:
                account = ref_value(account_detail, "AccountRef")
                if account in self.inventory_accounts:
                    self.add(ALERT, "expense_line_to_inventory_account",
                             "Account line posts straight to an inventory asset account",
                             account=self.account_label(account), amount=amount, **base)

    def check_item_line(self, base: dict, detail: dict, amount) -> None:
        item_id = ref_value(detail, "ItemRef")
        item = self.items.get(item_id) or {}
        name = clean(item.get("Name")) or ref_name(detail, "ItemRef")
        line = {"line_item_id": item_id, "line_item": name, "amount": amount, **base}
        if item_id == CATCH_ALL_ITEM_ID:
            self.add(ALERT, "catch_all_item_in_october", f"Catch-all item {CATCH_ALL_ITEM_ID} used on/after {INV_START}",
                     **line)
        elif item and is_legacy(item):
            self.add(ALERT, "legacy_item_in_october", "Frozen legacy item used on/after the cutover", **line)
        elif item_id not in self.targets.by_id:
            kind = item.get("Type") or "unknown type"
            self.add(ALERT, "unmapped_item_in_october", f"Item ({kind}) is not an approved mapping target", **line)

    def check_journal(self, txn: dict) -> None:
        base = self.txn_fields("JournalEntry", txn)
        for line in iter_lines(txn.get("Line")):
            detail = line.get("JournalEntryLineDetail") or {}
            account = ref_value(detail, "AccountRef")
            if account in self.inventory_accounts:
                sign = -1 if clean(detail.get("PostingType")) == "Credit" else 1
                self.add(WARN, "journal_to_inventory_account",
                         f"{detail.get('PostingType')} to inventory account; valid only for an approved variance/close",
                         account=self.account_label(account), amount=sign * float(line.get("Amount") or 0), **base)

    # ---- check 3: balances
    def check_balances(self) -> None:
        balances = {}
        for account_id in sorted(self.family_120):
            account = self.accounts[account_id]
            balance = float(account.get("CurrentBalance") or 0)
            balances[self.account_label(account_id)] = balance
            if abs(balance) >= 0.005:
                self.add(ALERT, "inventory_120_nonzero", "120xxx inventory account must stay at 0",
                         entity="Account", id=account_id, account=self.account_label(account_id), amount=balance)
        for account_id in (ASSET_ID, "87"):
            if account_id in self.accounts:
                balances[self.account_label(account_id)] = float(self.accounts[account_id].get("CurrentBalance") or 0)
        self.stats["balances"] = balances

        negative = []
        for item in self.items.values():
            qty = item.get("QtyOnHand")
            if item.get("Type") == "Inventory" and is_akp(item) and item.get("Active", True) and qty is not None \
                    and float(qty) < 0:
                cost = float(item.get("PurchaseCost") or item.get("UnitPrice") or 0)
                negative.append({**self.item_fields(item), "qty": float(qty), "value": round(float(qty) * cost, 2)})
        negative.sort(key=lambda r: r["value"])
        self.stats["negative_stock"] = {
            "count": len(negative), "total_value": round(sum(r["value"] for r in negative), 2),
            "top": negative[:TOP_NEGATIVE],
        }
        for row in negative[:TOP_NEGATIVE]:
            self.add(WARN, "akp_negative_stock", f"QtyOnHand {row['qty']:g} (stock awaiting bills?)",
                     amount=row["value"], **{k: row[k] for k in ("entity", "id", "name", "sku")})

    # ---- run
    def run(self) -> dict:
        if self.targets.error:
            self.add(ALERT, "mapping_invalid", self.targets.error)
        self.load()
        self.check_items()
        self.check_transactions()
        self.check_balances()
        return self.report()

    def report(self) -> dict:
        counts = Counter(f["severity"] for f in self.findings)
        counts[INFO] = sum(b["count"] for b in self.info.values())
        by_check = Counter((f["severity"], f["check"]) for f in self.findings)
        return {
            "tool": TOOL, "company": COMPANY, "realm": REALM, "since": self.since,
            "counts": {s: counts.get(s, 0) for s in (ALERT, WARN, INFO)},
            "by_check": [{"severity": s, "check": c, "count": n} for (s, c), n in sorted(by_check.items())],
            "info": self.info,
            "mapping_targets": len(self.targets.by_id),
            "legacy_snapshot_compared": self.legacy_snapshot is not None,
            **self.stats,
            "findings": self.findings,
        }

    def legacy_state(self) -> dict:
        prev = dict(self.legacy_snapshot or {})
        prev.update({i: item_snapshot(it) for i, it in self.items.items() if is_legacy(it)})
        return prev


# ---------------------------------------------------------------- output
def slack_text(report: dict, report_path: str = "") -> str:
    counts = report["counts"]
    lines = [f"item_guard {report['company']}: {counts[ALERT]} ALERT, {counts[WARN]} WARN, "
             f"{counts[INFO]:,} INFO since {report['since']}"]
    for severity in (ALERT, WARN):
        parts = [f"{r['check']} x{r['count']}" for r in report["by_check"] if r["severity"] == severity]
        if parts:
            lines.append(f"{severity}: " + ", ".join(parts))
    neg = report.get("negative_stock") or {}
    if neg.get("count"):
        lines.append(f"Negative AKP stock: {neg['count']} items, {money(neg['total_value'])}")
    shown = [f for f in report["findings"] if f["severity"] == ALERT][:SLACK_DETAIL_LINES]
    for f in shown:
        what = " ".join(x for x in (f.get("entity"), f.get("doc_number") or f.get("id"), f.get("txn_date")) if x)
        target = f.get("line_item") or f.get("name") or f.get("account") or ""
        amount = f" {money(f['amount'])}" if f.get("amount") not in (None, "") else ""
        lines.append(f"- {what}: {f['check']} {target}{amount}".rstrip())
    extra = counts[ALERT] - len(shown)
    if extra > 0:
        lines.append(f"- ... {extra} more ALERT(s)")
    if report["info"]:
        lines.append("INFO: " + ", ".join(f"{k} x{v['count']:,}" for k, v in sorted(report["info"].items())))
    if report_path:
        lines.append(f"Report: {report_path}")
    return "\n".join(lines)


def should_send(report: dict) -> bool:
    return report["counts"][ALERT] > 0 or report["counts"][WARN] > 0


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only QBO guard for the company_a October item contract.")
    parser.add_argument("--since", help="ISO timestamp (default: stored cursor, else 2026-10-01 business time)")
    parser.add_argument("--out", help="run folder (default outputs/item_guard_<UTC timestamp>/)")
    parser.add_argument("--no-slack", action="store_true", help="never post to Slack")
    parser.add_argument("--fail-on-alert", action="store_true", help=f"exit {EXIT_ALERT} when any ALERT exists")
    parser.add_argument("--no-state", action="store_true",
                        help="dry-run: do not advance the stored cursor / legacy snapshot")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    client=None,
    state: Path | None = None,
    mapping_path: Path | None = None,
    catalogue_dir: Path | None = None,
    slack: Callable[[str], None] = send_slack,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    args = parse_args(argv)
    state = state or state_dir(TOOL)
    cursor_path, snapshot_path = state / "cursor.json", state / "legacy_snapshot.json"
    since = args.since or (read_json(cursor_path) or {}).get("since") or DEFAULT_SINCE
    started = now()
    out = run_dir(TOOL, args.out)

    targets = load_mapping(mapping_path or mapping_file())
    guard = ItemGuard(
        client=client or ReadOnlyQBO(COMPANY),
        targets=targets,
        since=since,
        legacy_snapshot=read_json(snapshot_path),
        catalogue_ids=catalogue_receipt_ids(catalogue_dir or state.parent / "catalogue_sync"),
    )
    report = guard.run()
    report["scan_started_at"] = iso(started)
    report["next_cursor"] = iso(started - CURSOR_OVERLAP)
    report["qbo_requests"] = getattr(guard.client, "requests", None)

    report_path = out / "report.json"
    dump_json(report_path, report)
    write_csv(out / "alerts.csv", [f for f in report["findings"] if f["severity"] in (ALERT, WARN)], ALERT_COLUMNS)
    text = slack_text(report, str(report_path))
    if not args.no_slack and should_send(report):
        slack(text)

    if not args.no_state:
        dump_json(snapshot_path, guard.legacy_state())
        dump_json(cursor_path, {"since": report["next_cursor"], "previous_since": report["since"],
                                "advanced_at": iso(now()), "report": str(report_path)})
    print(text)
    return EXIT_ALERT if args.fail_on_alert and report["counts"][ALERT] else 0


if __name__ == "__main__":
    sys.exit(main())

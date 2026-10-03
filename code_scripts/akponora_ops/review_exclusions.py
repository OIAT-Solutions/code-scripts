"""Permanent review exclusions ("don't ask again") for company_a.

State file ``STATE_ROOT/mappings/company_a/review_exclusions.csv`` (next to ``approved.csv`` and
``vendors.csv``), columns ``kind, key, reason, added_by, added_at, expires_at``:

* ``product`` - key = EPOS Product ID. ``catalogue_sync`` never plans a create / mapping row for it
  and reports it as ``excluded`` (its till sales still fail the day while it is unmapped).
* ``vendor``  - key = EPOS supplier name (stored normalized, as ``vendors.vendor_key``). Never
  auto-created; ``bills_sync`` HOLDs its bills with reason ``supplier excluded``.
* ``routine_repeat`` - key = EPOS supplier name (normalized like ``vendor``). A supplier whose repeat
  orders are routine (daily bread, water): ``bills_sync`` posts its below-threshold "possible duplicate"
  POs automatically instead of waiting for a person. Repeats at or above the threshold still HOLD.
* ``bill``    - key = EPOS PO OrderRef (``EPOS-PO-`` prefix accepted). The PO was resolved outside
  the tool: it is planned ``EXCLUDED`` (never posted, counts as done for the cursor) and a post
  skips it as ``RESOLVED`` even when it was approved in an older review.csv.

``expires_at`` (optional, ``YYYY-MM-DD``): the exclusion stops applying ON that date (active while
today < expires_at, Africa/Lagos). Every add / remove is appended to the append-only
``review_exclusions_history.csv`` (who, when, why, file sha256 after the change).

    python -m code_scripts.akponora_ops.review_exclusions list [--kind K] [--all] [--json]
    python -m code_scripts.akponora_ops.review_exclusions add --kind K --key X --reason "..." --added-by R \
        [--expires-at YYYY-MM-DD] [--replace]
    python -m code_scripts.akponora_ops.review_exclusions remove --kind K --key X --removed-by R --reason "..."

Exit codes: 0 done (or unchanged), 2 refused (bad input, already excluded differently, not found).
Every command prints one JSON object on stdout.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import sha256_file
from code_scripts.product_conversion import canonical_product_id, clean
from code_scripts.scripts.akponora_cutover.bills_from_epos_pos import norm

FILE_NAME = "review_exclusions.csv"
HISTORY_NAME = "review_exclusions_history.csv"
COLUMNS = ["kind", "key", "reason", "added_by", "added_at", "expires_at"]
HISTORY_COLUMNS = ["ts", "action", "kind", "key", "reason", "actor", "expires_at", "previous", "file_sha256_after"]
KINDS = ("product", "vendor", "bill", "routine_repeat")
PO_PREFIX = "EPOS-PO-"
TZ = ZoneInfo("Africa/Lagos")


class ExclusionError(ValueError):
    """Refused change (exit 2)."""


def default_path() -> Path:
    from code_scripts.akponora_ops.common import mapping_file

    return mapping_file().parent / FILE_NAME


def path_near(path_or_dir) -> Path:
    """The exclusions file that sits next to ``path_or_dir`` (a mapping/vendors file or a folder)."""
    p = Path(path_or_dir)
    return (p if p.is_dir() else p.parent) / FILE_NAME


def history_path(path: Path) -> Path:
    return Path(path).parent / HISTORY_NAME


def normalize_key(kind: str, key) -> str:
    kind = clean(kind).lower()
    text = clean(key)
    if kind not in KINDS:
        raise ExclusionError(f"kind must be one of {', '.join(KINDS)}")
    if not text:
        raise ExclusionError("key is empty")
    if kind == "product":
        return canonical_product_id(text)
    if kind in ("vendor", "routine_repeat"):
        out = norm(text)
        if kind == "routine_repeat":
            # Apostrophe variants are the same supplier: UNCLE SAM'S / UNCLE'S SAM -> UNCLE SAM
            out = " ".join(w for w in out.split() if len(w) > 1)
        if not out:
            raise ExclusionError(f"supplier name {text!r} is empty after normalizing")
        return out
    if text.upper().startswith(PO_PREFIX):
        text = text[len(PO_PREFIX):]
    return text


def today_lagos() -> date:
    return datetime.now(TZ).date()


def is_active(row: dict, today: date | None = None) -> bool:
    exp = clean(row.get("expires_at"))
    if not exp:
        return True
    try:
        return (today or today_lagos()) < date.fromisoformat(exp[:10])
    except ValueError:
        return True  # unreadable expiry: fail safe (still excluded)


def read_rows(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{c: clean(r.get(c)) for c in COLUMNS} for r in csv.DictReader(fh)]


class Exclusions:
    """Active exclusions, looked up by (kind, normalized key)."""

    def __init__(self, rows=(), *, path: Path | None = None, today: date | None = None):
        self.path = Path(path) if path else None
        self.rows = [r for r in rows if is_active(r, today)]
        self._index = {(r["kind"], r["key"]): r for r in self.rows}

    def get(self, kind: str, key) -> dict | None:
        try:
            return self._index.get((kind, normalize_key(kind, key)))
        except ExclusionError:
            return None

    def keys(self, kind: str) -> set[str]:
        return {k for (kd, k) in self._index if kd == kind}

    def sha256(self) -> str:
        return sha256_file(self.path) if self.path and self.path.exists() else ""

    def describe(self, kind: str, key) -> str:
        row = self.get(kind, key)
        if row is None:
            return ""
        extra = f", until {row['expires_at']}" if row.get("expires_at") else ""
        return f"{row['reason']} (excluded by {row['added_by']} {row['added_at'][:10]}{extra})"


def load(path: Path | None = None, today: date | None = None) -> Exclusions:
    path = Path(path) if path else default_path()
    return Exclusions(read_rows(path), path=path, today=today)


def empty() -> Exclusions:
    return Exclusions()


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.parent / (path.name + ".lock")
    with open(lock, "a+") as fh:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        yield


def _write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["kind"], r["key"])))
    os.replace(tmp, path)


def _append_history(path: Path, row: dict) -> None:
    hp = history_path(path)
    new = not hp.exists()
    with open(hp, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HISTORY_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(kind: str, key, *, reason: str, added_by: str, expires_at: str = "", replace: bool = False,
        path: Path | None = None) -> dict:
    """Add (or, with ``replace``, change) one exclusion. Idempotent for an identical row."""
    path = Path(path) if path else default_path()
    kind = clean(kind).lower()
    nkey = normalize_key(kind, key)
    reason, added_by, expires_at = clean(reason), clean(added_by), clean(expires_at)
    if not reason:
        raise ExclusionError("--reason is required")
    if not added_by:
        raise ExclusionError("--added-by is required (who approved this, e.g. the approval ref)")
    if expires_at:
        try:
            date.fromisoformat(expires_at)
        except ValueError as exc:
            raise ExclusionError(f"--expires-at must be YYYY-MM-DD: {exc}") from exc
    with _locked(path):
        rows = read_rows(path)
        old = next((r for r in rows if r["kind"] == kind and r["key"] == nkey), None)
        if old and old["reason"] == reason and old["expires_at"] == expires_at:
            return {"result": "unchanged", "row": old, "file": str(path)}
        if old and not replace:
            raise ExclusionError(f"{kind} {nkey} is already excluded ({old['reason']!r} by {old['added_by']}); "
                                 "pass --replace to change it")
        row = {"kind": kind, "key": nkey, "reason": reason, "added_by": added_by, "added_at": now_iso(),
               "expires_at": expires_at}
        rows = [r for r in rows if r is not old] + [row]
        _write_rows(path, rows)
        sha = sha256_file(path)
        _append_history(path, {"ts": row["added_at"], "action": "replace" if old else "add", "kind": kind,
                               "key": nkey, "reason": reason, "actor": added_by, "expires_at": expires_at,
                               "previous": json.dumps(old, sort_keys=True) if old else "", "file_sha256_after": sha})
    return {"result": "replaced" if old else "added", "row": row, "file": str(path), "file_sha256": sha}


def remove(kind: str, key, *, removed_by: str, reason: str, path: Path | None = None) -> dict:
    path = Path(path) if path else default_path()
    kind = clean(kind).lower()
    nkey = normalize_key(kind, key)
    if not clean(removed_by):
        raise ExclusionError("--removed-by is required")
    if not clean(reason):
        raise ExclusionError("--reason is required")
    with _locked(path):
        rows = read_rows(path)
        old = next((r for r in rows if r["kind"] == kind and r["key"] == nkey), None)
        if old is None:
            raise ExclusionError(f"{kind} {nkey} is not excluded")
        _write_rows(path, [r for r in rows if r is not old])
        sha = sha256_file(path)
        _append_history(path, {"ts": now_iso(), "action": "remove", "kind": kind, "key": nkey,
                               "reason": clean(reason), "actor": clean(removed_by),
                               "expires_at": old.get("expires_at", ""),
                               "previous": json.dumps(old, sort_keys=True), "file_sha256_after": sha})
    return {"result": "removed", "row": old, "file": str(path), "file_sha256": sha}


def list_rows(*, kind: str = "", include_expired: bool = False, path: Path | None = None) -> list[dict]:
    path = Path(path) if path else default_path()
    rows = read_rows(path)
    out = []
    for r in rows:
        if kind and r["kind"] != kind:
            continue
        active = is_active(r)
        if active or include_expired:
            out.append({**r, "active": active})
    return out


def main(argv=None) -> int:
    from code_scripts.scripts.akponora_cutover._common import setup_env

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None, help=f"override STATE_ROOT/mappings/company_a/{FILE_NAME}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--key", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--added-by", required=True)
    p.add_argument("--expires-at", default="")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("remove")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--key", required=True)
    p.add_argument("--removed-by", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("list")
    p.add_argument("--kind", default="", choices=("", *KINDS))
    p.add_argument("--all", action="store_true", help="include expired rows")
    p.add_argument("--json", action="store_true", help="(output is always JSON; kept for symmetry)")
    a = ap.parse_args(argv)
    setup_env()
    path = Path(a.file) if a.file else default_path()
    try:
        if a.cmd == "add":
            res = add(a.kind, a.key, reason=a.reason, added_by=a.added_by, expires_at=a.expires_at,
                      replace=a.replace, path=path)
        elif a.cmd == "remove":
            res = remove(a.kind, a.key, removed_by=a.removed_by, reason=a.reason, path=path)
        else:
            res = {"file": str(path), "rows": list_rows(kind=a.kind, include_expired=a.all, path=path)}
    except ExclusionError as exc:
        print(json.dumps({"result": "refused", "error": str(exc)}))
        return 2
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

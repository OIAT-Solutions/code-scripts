"""Daily clean-up of old working files under STATE_ROOT (Marvin, 5 Oct 2026).

Working files are kept for a month; Akponora daily-run evidence (the record behind bills, payments and
banking) for three months. Nothing that guards against double posting depends on these files: every
receipt is checked against QuickBooks before posting, and each day's totals live in the portal database.

Never touched: company configs, QBO tokens, mappings, approvals, the uploaded-DocNumber ledger, the
Akponora tool state (catalogue snapshot, vendor/exclusion files, stock latest.json) and dot-files (locks).

The schedule worker runs :func:`prune` once per business day; ``manage.py prune_old_files`` runs it by hand
(``--dry-run`` lists what would go).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from code_scripts import paths as _paths

WORKING_DAYS = 31
EVIDENCE_DAYS = 90
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
MARKER = "housekeeping_last.json"


@dataclass
class Result:
    removed_files: int = 0
    removed_dirs: int = 0
    freed_bytes: int = 0
    removed: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"removed_files": self.removed_files, "removed_dirs": self.removed_dirs,
                "freed_mb": round(self.freed_bytes / 1e6, 1)}


def _targets(state_root: Path) -> list[tuple[Path, int, str]]:
    """(folder, keep days, how): ``day_dirs`` = children named by date; ``files`` = files by age."""
    ops = state_root / "code_scripts"
    return [
        (ops / "Uploaded", WORKING_DAYS, "day_dirs"),        # EPOS download + transformed CSV per day
        (ops / "Uploaded", WORKING_DAYS, "files"),           # Uploaded/ranges etc.
        (ops / "uploads", WORKING_DAYS, "files"),            # range_raw / spill_raw staging
        (ops / "logs", WORKING_DAYS, "files"),               # pipeline + run logs
        (ops / "reports", WORKING_DAYS, "files"),
        (ops / "exports", WORKING_DAYS, "files"),
        (ops / "outputs", WORKING_DAYS, "files"),
        (state_root / "ops" / "company_a" / "daily", EVIDENCE_DAYS, "day_dirs"),  # daily-run evidence
    ]


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _remove(p: Path, res: Result, dry_run: bool) -> None:
    is_dir = p.is_dir()
    res.freed_bytes += _size(p)
    res.removed.append(str(p))
    if is_dir:
        res.removed_dirs += 1
    else:
        res.removed_files += 1
    if dry_run:
        return
    if is_dir:
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def prune(*, today: date | None = None, dry_run: bool = False, state_root: Path | None = None) -> Result:
    state_root = Path(state_root or _paths.STATE_ROOT)
    today = today or date.today()
    res = Result()
    for folder, keep_days, how in _targets(state_root):
        if not folder.is_dir():
            continue
        cutoff = today - timedelta(days=keep_days)
        if how == "day_dirs":
            for child in folder.iterdir():
                m = DAY_RE.match(child.name)
                if child.is_dir() and m:
                    try:
                        day = date.fromisoformat(m.group(1))
                    except ValueError:
                        continue
                    if day < cutoff:
                        _remove(child, res, dry_run)
            continue
        cutoff_ts = datetime.combine(cutoff, datetime.min.time()).timestamp()
        for f in sorted(folder.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            if how == "files" and DAY_RE.match(f.relative_to(folder).parts[0]) and folder.name == "Uploaded":
                continue  # day folders are handled (whole) above
            if f.stat().st_mtime < cutoff_ts:
                _remove(f, res, dry_run)
        if not dry_run:  # drop folders left empty
            for d in sorted((d for d in folder.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
    return res


def due(today: date, state_root: Path | None = None) -> bool:
    marker = Path(state_root or _paths.STATE_ROOT) / MARKER
    try:
        return json.loads(marker.read_text()).get("day") != today.isoformat()
    except Exception:  # noqa: BLE001 - missing / unreadable marker: run
        return True


def mark_done(today: date, result: Result, state_root: Path | None = None) -> None:
    marker = Path(state_root or _paths.STATE_ROOT) / MARKER
    marker.write_text(json.dumps({"day": today.isoformat(), **result.as_dict()}))

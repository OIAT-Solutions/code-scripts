"""Read-only view over the Company A (AKPONORA) daily-run evidence.

The daily routine (``python -m code_scripts.akponora_ops.daily_run``), started by the portal schedule
worker as a ``company_a_daily`` job, writes per run::

    STATE_ROOT/ops/company_a/daily/<business_date>/run_<HHMMSSZ>[_dry]/summary.json
    STATE_ROOT/ops/company_a/daily/<business_date>/run_<HHMMSSZ>[_dry]/<step>/{log.txt,*.csv,*.json}

This module only READS those files (plus the posting-hold file and env settings) for the
portal. It never writes, never calls QBO / EPOS, and must never crash a page on a missing,
partial or malformed file: every field is read defensively and unknown fields / steps are
shown generically.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from code_scripts import paths as _paths

COMPANY_KEY = "company_a"
UF_ENV = "OIAT_COMPANY_A_UF_DEPOSIT_ENABLED"
DEFAULT_TZ = "Africa/Lagos"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_RE = re.compile(r"^run_[0-9A-Za-z_-]{1,64}$")
VIEWABLE_SUFFIXES = {".csv", ".json", ".txt", ".log"}
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 500
LOG_TAIL_LINES = 60
MAX_RUNS = 200

# What a person can run from the portal ("What to run"), in the routine's order. One name per step everywhere.
DAILY_STEP_CHOICES = [("", "Whole day"), ("catalogue", "Products"), ("bills", "Bills"), ("sales", "Sales"),
                      ("credit", "Credit sales (invoices)"), ("guard", "Health check"), ("stock", "Stock check"),
                      ("uf", "Banking (funds allocation)")]

STEP_LABELS = {
    "catalogue": "Products",
    "bills": "Bills",
    "sales": "Sales",
    "credit": "Credit sales",
    "guard": "Health check",
    "uf": "Banking",
    "deposits": "Banking",
    "uf_deposits": "Banking",
}
STATUS_ICONS = {"ok": "✅", "review": "⚠️", "failed": "❌", "skipped": "⏭", "disabled": "💤", "unknown": "❔"}
OVERALL_STATUS = {"clean": "ok", "ok": "ok", "review": "review", "failed": "failed"}
EXIT_STATUS = {0: "ok", 3: "review", 2: "failed"}


# --------------------------------------------------------------------------- helpers
def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def state_root() -> Path:
    return Path(_paths.STATE_ROOT)


def ops_root() -> Path:
    """Everything the evidence viewer may ever read lives under here."""
    return state_root() / "ops" / COMPANY_KEY


def daily_root() -> Path:
    return ops_root() / "daily"


def posting_hold_path() -> Path:
    try:
        from code_scripts.operations_controls import posting_hold_path as _hold_path

        return Path(_hold_path())
    except Exception:  # noqa: BLE001
        return state_root() / "company_a_posting_hold.json"


def _read_json(path: Path) -> tuple[Any, str]:
    """(data, error). Never raises."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, f"unreadable: {type(exc).__name__}"


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def format_naira(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "—"
    return f"₦{number:,.2f}"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def _tz() -> ZoneInfo:
    """Times on the pages are shown in the store's local time (the business timezone)."""
    from ..business_date import get_business_timezone

    try:
        return get_business_timezone()
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TZ)


def _local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(_tz()) if dt else None


# --------------------------------------------------------------------------- schedule
def _cron_valid(expr: str) -> bool:
    try:
        from croniter import croniter

        return bool(expr) and croniter.is_valid(expr)
    except Exception:  # noqa: BLE001
        return False


def schedule_info(now: datetime | None = None) -> dict:
    """Nora's Daily routine schedule (the portal schedule row; there is no other scheduler)."""
    from . import workflows

    s = workflows.daily_routine_schedule()
    info = {"enabled": bool(s and s.enabled), "cron": s.cron_expr if s else "", "timezone": s.timezone_name if s else "",
            "next_run": (s.next_fire_at.astimezone(ZoneInfo(s.timezone_name)) if s and s.enabled and s.next_fire_at else None),
            "cron_valid": _cron_valid(s.cron_expr if s else ""), "time_label": ""}
    parts = info["cron"].split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:] == ["*", "*", "*"]:
        info["time_label"] = f"Daily at {int(parts[1]):02d}:{int(parts[0]):02d}"
    return info


# --------------------------------------------------------------------------- runs
@dataclass
class Step:
    name: str
    label: str
    status: str
    detail: str = ""
    counts: dict = field(default_factory=dict)
    review: list = field(default_factory=list)
    exit_code: Any = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    key_facts: list = field(default_factory=list)  # [(label, value)]
    extra_counts: list = field(default_factory=list)  # [(key, value)] not covered by key_facts

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self.status, STATUS_ICONS["unknown"])


@dataclass
class Run:
    business_date: str
    run_id: str
    path: Path
    dry_run: bool
    status: str  # ok / review / failed / incomplete / unreadable
    error: str = ""
    finished_at: datetime | None = None
    steps: list = field(default_factory=list)
    waiting: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self.status, STATUS_ICONS["unknown"])

    @property
    def status_label(self) -> str:
        return {"ok": "OK", "review": "Review", "failed": "Failed", "incomplete": "No summary",
                "unreadable": "Unreadable"}.get(self.status, self.status.title())

    @property
    def mode_label(self) -> str:
        return "Dry run" if self.dry_run else "Real"

    def step(self, name: str) -> Step | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None


def _step_facts(name: str, status: str, c: dict) -> list[tuple[str, str]]:
    if status in ("skipped", "disabled") or not c:
        return []
    if name == "catalogue":
        return [("New EPOS products", str(c.get("new_products", 0))),
                ("Items created", str(c.get("items_created", 0))),
                ("Mapping only", str(c.get("mapping_only", 0))),
                ("Review / hold", f"{c.get('review', 0)} / {c.get('hold', 0)}"),
                ("Mapping installed", "yes" if c.get("mapping_installed") else "no")]
    if name == "bills":
        window = c.get("window")
        window_label = "..".join(str(x) for x in window) if isinstance(window, list) else str(window or "")
        return [("Window", window_label or "—"), ("POs", str(c.get("pos", 0))),
                ("Bills posted", str(c.get("posted", 0))), ("Ready total", format_naira(c.get("ready_total"))),
                ("Held", f"{c.get('hold', 0)} ({format_naira(c.get('hold_total_inc'))})"),
                ("Capped", str(c.get("capped", 0))), ("Already posted", str(c.get("already_posted", 0))),
                ("Vendors created / held", f"{c.get('vendors_created', 0)} / {c.get('vendors_held', 0)}")]
    if name == "sales":
        facts = [("Mode", str(c.get("mode") or "—"))]
        if c.get("uploaded") is not None:
            facts.append(("Receipts uploaded / skipped / failed",
                          f"{c.get('uploaded')} / {c.get('skipped', 0)} / {c.get('failed', 0)}"))
        if c.get("reconcile_status"):
            facts += [("Reconcile", str(c.get("reconcile_status"))), ("EPOS total", format_naira(c.get("epos_total"))),
                      ("QBO total", format_naira(c.get("qbo_total")))]
        return facts
    if name == "guard":
        return [("ALERT", str(c.get("alert", 0))), ("WARN", str(c.get("warn", 0)))]
    if name in ("uf", "deposits", "uf_deposits"):
        facts = []
        if "balance" in c:
            facts.append(("Undeposited Funds balance", format_naira(c.get("balance"))))
        if "last_deposit_date" in c:
            facts.append(("Last deposit", str(c.get("last_deposit_date") or "none")))
        if "days_since_last_deposit" in c:
            facts.append(("Days since last deposit", str(c.get("days_since_last_deposit"))))
        return facts
    return []


_FACT_KEYS = {
    "catalogue": {"new_products", "items_created", "mapping_only", "review", "hold", "mapping_installed"},
    "bills": {"window", "pos", "posted", "ready_total", "hold", "hold_total_inc", "capped", "already_posted",
              "vendors_created", "vendors_held"},
    "sales": {"mode", "uploaded", "skipped", "failed", "reconcile_status", "epos_total", "qbo_total"},
    "guard": {"alert", "warn"},
    "uf": {"balance", "last_deposit_date", "days_since_last_deposit"},
}
_FACT_KEYS["deposits"] = _FACT_KEYS["uf_deposits"] = _FACT_KEYS["uf"]


def _parse_step(raw: Any) -> Step | None:
    data = _as_dict(raw)
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    status = str(data.get("status") or "unknown").strip().lower() or "unknown"
    counts = _as_dict(data.get("counts"))
    covered = _FACT_KEYS.get(name, set()) if status not in ("skipped", "disabled") else set()
    extra = []
    for key, value in counts.items():
        if key in covered:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)[:500]
        extra.append((str(key), str(value)))
    return Step(
        name=name,
        label=STEP_LABELS.get(name, name.replace("_", " ").title()),
        status=status,
        detail=str(data.get("detail") or ""),
        counts=counts,
        review=[str(x) for x in _as_list(data.get("review"))],
        exit_code=data.get("exit_code"),
        started_at=_parse_dt(data.get("started_at")),
        finished_at=_parse_dt(data.get("finished_at")),
        key_facts=_step_facts(name, status, counts),
        extra_counts=extra,
    )


def _run_totals(steps: list[Step], *, dry_run: bool = False) -> dict:
    by = {s.name: s for s in steps}
    totals: dict[str, Any] = {}
    sales = by.get("sales")
    if sales and sales.counts:
        c = sales.counts
        posted = not dry_run and c.get("mode") == "post" and sales.status == "ok"
        totals["sales_posted"] = format_naira(c.get("qbo_total")) if posted else "—"
        totals["sales_mode"] = c.get("mode") or ""
    bills = by.get("bills")
    if bills and bills.counts:
        totals["bills_posted"] = _as_int(bills.counts.get("posted"))
        totals["bills_ready_total"] = format_naira(bills.counts.get("ready_total"))
        totals["vendors_created"] = _as_int(bills.counts.get("vendors_created"))
    cat = by.get("catalogue")
    if cat and cat.counts:
        totals["items_created"] = _as_int(cat.counts.get("items_created"))
    return totals


def _run_from_dir(run_dir: Path) -> Run:
    day = run_dir.parent.name
    run_id = run_dir.name
    dry = run_id.endswith("_dry")
    data, error = _read_json(run_dir / "summary.json")
    if data is None:
        return Run(business_date=day, run_id=run_id, path=run_dir, dry_run=dry,
                   status="incomplete" if error == "missing" else "unreadable", error=error)
    if not isinstance(data, dict):
        return Run(business_date=day, run_id=run_id, path=run_dir, dry_run=dry, status="unreadable",
                   error="summary.json is not an object")
    if "dry_run" in data:
        dry = bool(data.get("dry_run"))
    status = OVERALL_STATUS.get(str(data.get("status") or "").lower())
    if status is None:
        status = EXIT_STATUS.get(_as_int(data.get("exit_code"), -1), "unknown")
    steps = [s for s in (_parse_step(x) for x in _as_list(data.get("steps"))) if s is not None]
    waiting = [str(x) for x in _as_list(data.get("waiting_for_review"))]
    finished = _parse_dt(data.get("finished_at"))
    if finished is None:
        try:
            finished = datetime.fromtimestamp((run_dir / "summary.json").stat().st_mtime, ZoneInfo("UTC"))
        except OSError:
            finished = None
    return Run(business_date=str(data.get("business_date") or day), run_id=run_id, path=run_dir, dry_run=dry,
               status=status, finished_at=_local(finished), steps=steps, waiting=waiting,
               totals=_run_totals(steps, dry_run=dry), raw=data)


def _run_dirs() -> list[Path]:
    root = daily_root()
    try:
        days = [d for d in root.iterdir() if d.is_dir() and DATE_RE.match(d.name)]
    except OSError:
        return []
    out: list[Path] = []
    for day in days:
        try:
            out += [r for r in day.iterdir() if r.is_dir() and RUN_RE.match(r.name)]
        except OSError:
            continue
    # newest first: business date, then the UTC stamp in the folder name
    out.sort(key=lambda p: (p.parent.name, p.name.replace("_dry", "")), reverse=True)
    return out[:MAX_RUNS]


def list_runs(limit: int | None = None, *, include_dry: bool = True) -> list[Run]:
    runs = []
    for run_dir in _run_dirs():
        run = _run_from_dir(run_dir)
        if not include_dry and run.dry_run:
            continue
        runs.append(run)
        if limit and len(runs) >= limit:
            break
    return runs


def latest_run(*, real_only: bool = False) -> Run | None:
    runs = list_runs(limit=1, include_dry=not real_only)
    return runs[0] if runs else None


def get_run(business_date: str, run_id: str) -> Run | None:
    if not DATE_RE.match(business_date or "") or not RUN_RE.match(run_id or ""):
        return None
    run_dir = daily_root() / business_date / run_id
    try:
        if not run_dir.is_dir() or not _within(run_dir.resolve(), daily_root().resolve()):
            return None
    except OSError:
        return None
    return _run_from_dir(run_dir)


# --------------------------------------------------------------------------- evidence
def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def evidence_files(run: Run, *, max_files: int = 200) -> list[dict]:
    """Viewable files inside one run folder (relative paths only)."""
    files: list[dict] = []
    try:
        root = run.path.resolve()
        for path in sorted(run.path.rglob("*")):
            if len(files) >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in VIEWABLE_SUFFIXES:
                continue
            resolved = path.resolve()
            if not _within(resolved, root):
                continue
            rel = path.relative_to(run.path).as_posix()
            files.append({"path": rel, "size": path.stat().st_size, "step": rel.split("/")[0] if "/" in rel else ""})
    except OSError:
        pass
    return files


class EvidenceError(ValueError):
    pass


def resolve_evidence(run: Run, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside the run folder, refusing anything outside STATE_ROOT/ops/company_a."""
    rel = str(rel_path or "").strip()
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ":" in rel:
        raise EvidenceError("invalid path")
    candidate = run.path / rel
    try:
        resolved = candidate.resolve(strict=True)
        allowed_root = ops_root().resolve(strict=True)
        run_root = run.path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EvidenceError("not found") from None
    if not _within(resolved, allowed_root) or not _within(resolved, run_root):
        raise EvidenceError("outside the Company A evidence folder")
    if not resolved.is_file():
        raise EvidenceError("not a file")
    if resolved.suffix.lower() not in VIEWABLE_SUFFIXES:
        raise EvidenceError("file type not viewable")
    return resolved


def read_evidence(path: Path) -> dict:
    """Structured, size-capped content for safe in-page rendering (templates autoescape)."""
    out: dict[str, Any] = {"kind": "text", "truncated": False, "size": 0}
    try:
        size = path.stat().st_size
        out["size"] = size
        with open(path, "rb") as fh:
            raw = fh.read(MAX_VIEW_BYTES + 1)
    except OSError as exc:
        return {"kind": "error", "error": f"unreadable: {type(exc).__name__}"}
    if len(raw) > MAX_VIEW_BYTES:
        raw, out["truncated"] = raw[:MAX_VIEW_BYTES], True
    text = raw.decode("utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0] if rows else []
        body = rows[1:]
        if len(body) > MAX_CSV_ROWS:
            body, out["truncated"] = body[:MAX_CSV_ROWS], True
        out.update(kind="csv", header=header, rows=body, row_count=len(rows) - 1 if rows else 0)
    elif suffix == ".json":
        try:
            out.update(kind="json", text=json.dumps(json.loads(text), indent=2, ensure_ascii=False))
        except ValueError:
            out.update(kind="text", text=text)
    else:
        out.update(kind="text", text=text)
    return out


def log_tail(run: Run, step_name: str, lines: int = LOG_TAIL_LINES) -> str:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", step_name or ""):
        return ""
    try:
        path = resolve_evidence(run, f"{step_name}/log.txt")
    except EvidenceError:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# --------------------------------------------------------------------------- holds & alerts
def posting_hold() -> dict:
    """Current posting hold (if any) and the most recently cleared one, read-only."""
    path = posting_hold_path()
    out: dict[str, Any] = {"active": False, "path": str(path), "last_cleared": None}
    try:
        exists = path.exists()
    except OSError:
        exists = False
    if exists:
        data, error = _read_json(path)
        data = _as_dict(data)
        result = data.get("result")
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("status") or result.get("reason") or result.get("error") or "")
            detail = result.get("detail") or result.get("message")
            if detail:
                reason = f"{reason}: {detail}" if reason else str(detail)
        elif result:
            reason = str(result)
        out.update(active=True, error=error, at=_local(_parse_dt(data.get("at"))), at_raw=str(data.get("at") or ""),
                   business_date=str(data.get("business_date") or ""), source=str(data.get("source") or ""),
                   reason=reason[:500], action=str(data.get("action") or ""),
                   result_json=json.dumps(result, indent=2, ensure_ascii=False, default=str)[:4000] if result else "")
    try:
        archives = sorted(path.parent.glob("company_a_posting_hold.cleared-*.json"), reverse=True)
    except OSError:
        archives = []
    if archives:
        data, _ = _read_json(archives[0])
        cleared = _as_dict(_as_dict(data).get("cleared"))
        out["last_cleared"] = {"at": _local(_parse_dt(cleared.get("at"))), "approved_by": str(cleared.get("approved_by") or ""),
                               "reason": str(cleared.get("reason") or "")[:500],
                               "business_date": str(_as_dict(data).get("business_date") or "")}
    return out


def guard_alerts(runs: list[Run] | None = None) -> dict | None:
    """item_guard alerts from the newest run that has a guard step folder."""
    for run in runs if runs is not None else list_runs():
        guard_dir = run.path / "guard"
        if not guard_dir.is_dir():
            continue
        step = run.step("guard")
        info: dict[str, Any] = {"run": run, "status": step.status if step else "unknown",
                                "alert": _as_int((step.counts if step else {}).get("alert")),
                                "warn": _as_int((step.counts if step else {}).get("warn")),
                                "by_check": [], "csv_path": "", "report_path": ""}
        if (guard_dir / "report.json").is_file():
            info["report_path"] = "guard/report.json"
        alerts_csv = guard_dir / "alerts.csv"
        if alerts_csv.is_file():
            info["csv_path"] = "guard/alerts.csv"
            counter: Counter = Counter()
            try:
                with open(alerts_csv, encoding="utf-8-sig", errors="replace", newline="") as fh:
                    for i, row in enumerate(csv.DictReader(fh)):
                        if i >= 20000:
                            break
                        counter[(str(row.get("severity") or "?"), str(row.get("check") or "?"))] += 1
            except (OSError, csv.Error):
                pass
            info["by_check"] = [{"severity": sev, "check": check, "count": n}
                                for (sev, check), n in sorted(counter.items(), key=lambda kv: (kv[0][0] != "ALERT", -kv[1], kv[0][1]))]
        return info
    return None


def uf_info(run: Run | None) -> dict | None:
    """Undeposited Funds / deposits step data from a run, if the step exists."""
    if run is None:
        return None
    for name in ("deposits", "uf_deposits", "uf"):
        step = run.step(name)
        if step is not None:
            return {"step": step, "enabled_env": _truthy(os.getenv(UF_ENV)), "run": run}
    return None


# --------------------------------------------------------------------------- page bundles
def overview_card() -> dict:
    runs = list_runs(limit=40)
    real = next((r for r in runs if not r.dry_run), None)
    return {
        "schedule": schedule_info(),
        "last_real": real,
        "last_any": runs[0] if runs else None,
        "hold": posting_hold(),
        "uf": uf_info(real or (runs[0] if runs else None)),
        "has_evidence": bool(runs),
    }


def schedule_row() -> dict:
    sched = schedule_info()
    last = latest_run(real_only=True)
    return {"schedule": sched, "last_run": last, "hold": posting_hold()}

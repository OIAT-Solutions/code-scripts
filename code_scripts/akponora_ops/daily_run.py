"""One unattended daily routine for AKPONORA / NORA (company_a).

    python -m code_scripts.akponora_ops.daily_run [--date YYYY-MM-DD] [--dry-run] [--only step,...]

Default business date = the last CLOSED Lagos business day (05:00 cutoff): run at 06:00 on
3 Oct it does 2 Oct. Steps, in this order, each in its own evidence folder under
``STATE_ROOT/ops/company_a/daily/<date>/run_<UTC stamp>/<step>/``:

1. ``catalogue``  catalogue_sync ``scheduled`` (creates qty-0 items + mapping only under its own
   env gates / cap; ambiguous -> review). A failure here never stops bills or sales: the sales
   transform still fails closed on any unmapped product.
2. ``bills``      bills_sync ``scheduled`` for POs received up to that business day (from its
   cursor, so earlier days still pending are retried). Creates genuinely new vendors under the
   vendor gates, posts UNPAID bills only under the bills gates / caps; holds wait for review.
3. ``sales``      ``run_pipeline --company company_a --target-date <date>``. Posting only through
   the standing auto-approval (full mapping + EPOS totals); without it, a dry-run is built and
   the day waits for review. Runs after bills so stock arrives before it is sold. Skipped when
   the posting hold is already in place.
3b. ``credit``    EPOS credit sales -> one QBO Invoice per customer per day (``credit_invoices``). The
   sales path already left the Credit-tender rows out of the SalesReceipts (``code_scripts/credit_sales``).
   Posts only with ``OIAT_COMPANY_A_CREDIT_INVOICES=post``; otherwise the plan waits for review.
4. ``guard``      item_guard read-only scan (report only).
5. ``stock``      stock_snapshot ``run`` (READ-ONLY): EPOS stock report vs QBO QtyOnHand per family,
   written to ``STATE_ROOT/ops/company_a/stock_snapshot/latest.json`` for the portal's Products &
   Stock page. Report only: differences never make the run wait for review; a failure is reported
   and never affects the other steps.
6. ``uf``         Undeposited Funds deposits from the till sheet (``uf_deposits``), off unless
   ``OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1``. For each business day since 25 Sep not yet deposited:
   the day's SalesReceipts still in Undeposited Funds are deposited (LinkedTxn) into the banks the
   "Nora Mart Daily Sales Account Breakdown" sheet names, plus Bank->Bank true-up transfers so each
   bank matches the sheet mix. Posts only with ``OIAT_COMPANY_A_UF_AUTO_POST=1`` and
   ``OIAT_COMPANY_A_UF_APPROVAL_REF`` (cap ``OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL``); otherwise the
   plan waits for review. Days are independent: a held day (blank sheet, totals off, unmapped till
   line ...) waits on its own and later complete days still post. The Slack line ends with the
   till-sheet status (last day entered, missing days, days waiting to deposit). Runs in-process
   after sales and guard; a failure here never affects the other steps.

A failed / held step never makes a later step write something inconsistent: each later step has
its own fail-closed gates, and the sales step never runs while the posting hold is in place.
``--dry-run`` writes nothing to QBO (catalogue/bills ``plan``, ``run_pipeline --dry-run``,
item_guard without advancing its cursor, uf_deposits plan only) and sends Slack only with ``--slack``.

Exit 0 = all clean, 3 = something waits for review, 2 = a step failed (or the run lock stayed
busy). Writes ``summary.json`` and sends ONE Slack summary (``OIAT_AKPONORA_OPS_SLACK_WEBHOOK_URL``,
else the company_a pipeline webhook, else ``SLACK_WEBHOOK_URL``). Holds the global run lock for
the whole run so it never overlaps a manual ``run_pipeline`` (waits up to
``OIAT_COMPANY_A_DAILY_RUN_LOCK_WAIT_MINUTES``, default 30).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import COMPANY, SLACK_ENV, dump_json
from code_scripts.scripts.akponora_cutover._common import REPO_ROOT, business_date, setup_env

TOOL = "daily_run"
TZ = ZoneInfo("Africa/Lagos")
STEPS = ("catalogue", "bills", "sales", "credit", "guard", "stock", "uf")
DEFAULT_CRON = "0 6 * * *"
LOCK_WAIT_ENV = "OIAT_COMPANY_A_DAILY_RUN_LOCK_WAIT_MINUTES"
STEP_SLACK_ENV = "OIAT_COMPANY_A_DAILY_RUN_STEP_SLACK"
UF_ENV = "OIAT_COMPANY_A_UF_DEPOSIT_ENABLED"
AGED_ENV = "OIAT_COMPANY_A_UF_AGED_DAYS"  # a day not banked for this many days is escalated (default 7)

OK, REVIEW, FAILED, SKIPPED, DISABLED = "ok", "review", "failed", "skipped", "disabled"
EXIT_OK, EXIT_FAILED, EXIT_REVIEW = 0, 2, 3


@dataclass
class StepResult:
    name: str
    status: str = OK
    exit_code: int | None = None
    detail: str = ""
    counts: dict = field(default_factory=dict)
    review: list = field(default_factory=list)
    out: str = ""
    started_at: str = ""
    finished_at: str = ""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def last_closed_business_date(now: datetime | None = None) -> date:
    """The last Lagos business day (05:00 cutoff) that has fully ended at ``now``."""
    now = now or datetime.now(TZ)
    local = now.astimezone(TZ).replace(tzinfo=None) if now.tzinfo else now
    return business_date(local) - timedelta(days=1)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run_command(cmd: list[str], *, log_path: Path, env: dict, cwd: Path = REPO_ROOT) -> int:
    """Run one step subprocess, its stdout+stderr going to ``log_path``. Returns the exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    return proc.returncode


def slack_webhook() -> str | None:
    explicit = os.getenv(SLACK_ENV, "").strip()
    if explicit:
        return explicit
    try:
        from code_scripts.scripts.akponora_cutover._common import company_config

        url = company_config(COMPANY).slack_webhook_url
    except Exception:  # noqa: BLE001 - Slack must never break the run
        url = None
    return url or os.getenv("SLACK_WEBHOOK_URL") or None


def send_summary_slack(text: str) -> None:
    from code_scripts.slack_notify import send_slack_success

    send_slack_success(text, webhook_url=slack_webhook())


class DailyRun:
    """The ordered routine. ``runner`` / ``slack`` / ``uf_client`` / ``uf_write_client`` / ``uf_sheet``
    are injectable for tests."""

    def __init__(self, business_day: str, *, dry_run: bool = False, only: list[str] | None = None,
                 root: Path | None = None, runner: Callable = run_command, slack: Callable | None = None,
                 send_slack: bool = True, env: dict | None = None, uf_client=None, uf_write_client=None,
                 uf_sheet=None, python: str = sys.executable, credit_client=None, credit_write_client=None):
        self.date = business_day
        self.dry_run = dry_run
        self.only = list(only) if only else list(STEPS)
        self.runner = runner
        self.slack = slack or send_summary_slack
        self.send_slack = send_slack
        self.base_env = dict(os.environ if env is None else env)
        self.uf_client = uf_client
        self.uf_write_client = uf_write_client
        self.uf_sheet = uf_sheet
        self.credit_client = credit_client
        self.credit_write_client = credit_write_client
        self.python = python
        stamp = datetime.now(timezone.utc).strftime("%H%M%SZ")
        self.day_dir = Path(root) if root else _daily_root() / business_day
        self.run_dir = self.day_dir / (f"run_{stamp}" + ("_dry" if dry_run else ""))
        self.results: list[StepResult] = []
        self.started_at = ""

    # ------------------------------------------------------------ helpers
    def step_dir(self, name: str) -> Path:
        path = self.run_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def child_env(self) -> dict:
        from code_scripts.run_lock import LOCK_HELD_ENV

        env = dict(self.base_env)
        env[LOCK_HELD_ENV] = "1"  # this routine holds the global run lock for its children
        if not truthy(env.get(STEP_SLACK_ENV)):
            # one Slack summary: silence the per-step / pipeline messages of the children
            for key in (SLACK_ENV, "SLACK_WEBHOOK_URL", _company_slack_env_key()):
                if key:
                    env[key] = ""
        return env

    def run_step(self, name: str, cmd: list[str], out: Path) -> int:
        return self.runner(cmd, log_path=out / "log.txt", env=self.child_env())

    # ------------------------------------------------------------ steps
    def step_catalogue(self, res: StepResult) -> None:
        out = Path(res.out)
        mode = "plan" if self.dry_run else "scheduled"
        rc = self.run_step("catalogue", [self.python, "-m", "code_scripts.akponora_ops.catalogue_sync", mode,
                                         "--out", str(out), "--no-slack"], out)
        res.exit_code = rc
        summary = read_json(out / "summary.json", {}) or {}
        receipt = read_json(out / "apply_receipt.json", {}) or {}
        counts = summary.get("counts") or {}
        res.counts = {"new_products": counts.get("new", 0), "auto": counts.get("auto", 0),
                      "review": counts.get("review", 0), "hold": counts.get("hold", 0),
                      "items_created": len(receipt.get("created") or []),
                      "items_adopted": len(receipt.get("adopted") or []),
                      "mapping_only": len(receipt.get("mapping_only") or []),
                      "mapping_installed": bool(receipt.get("installed"))}
        if receipt.get("stopped"):
            res.detail = f"apply stopped: {receipt['stopped']}"
        if rc == 0:
            res.status = OK
        elif rc in (3, 4):
            res.status = REVIEW
            res.review.append(f"catalogue: {counts.get('review', 0)} review / {counts.get('hold', 0)} hold "
                              f"product(s){' (auto cap exceeded)' if rc == 4 else ''} -> {out / 'review.csv'}")
        else:
            res.status = FAILED
            res.detail = res.detail or f"catalogue_sync exited {rc}; see {out / 'log.txt'}"

    def bills_from(self) -> str:
        try:
            from code_scripts.akponora_ops import bills_sync

            start = bills_sync.default_from()
        except Exception:  # noqa: BLE001 - fall back to the business date itself
            start = self.date
        return min(start, self.date)

    def step_bills(self, res: StepResult) -> None:
        out = Path(res.out)
        mode = "plan" if self.dry_run else "scheduled"
        rc = self.run_step("bills", [self.python, "-m", "code_scripts.akponora_ops.bills_sync", mode,
                                     "--from", self.bills_from(), "--to", self.date, "--out", str(out),
                                     "--no-slack"], out)
        res.exit_code = rc
        summary = read_json(out / "summary.json", {}) or {}
        sched = read_json(out / "scheduled.json", {}) or {}
        c = summary.get("counts") or {}
        posted = sched.get("posted") or {}
        vendors = summary.get("vendor_actions") or []
        res.counts = {"window": summary.get("window"), "pos": summary.get("pos_in_window", 0),
                      "ready": c.get("READY", 0), "ready_total": summary.get("ready_total", "0.00"),
                      "hold": c.get("HOLD", 0), "hold_total_inc": summary.get("hold_total_inc", "0.00"),
                      "already_posted": c.get("SKIP", 0), "posted": posted.get("POSTED", 0),
                      "adopted": posted.get("ADOPTED", 0), "capped": posted.get("CAPPED", 0),
                      "held_live": posted.get("HELD_LIVE", 0),
                      "vendors_created": sum(v.get("state") == "CREATED" for v in vendors),
                      "vendors_linked": sum(v.get("state") == "LINKED" for v in vendors),
                      "vendors_held": sum(str(v.get("state", "")).startswith("HOLD") for v in vendors)}
        res.counts["holds"] = [{"po": h.get("po"), "supplier": h.get("supplier") or "", "total": h.get("total_inc"),
                                "reason": (h.get("reasons") or [""])[0]} for h in (summary.get("holds") or [])[:10]]
        res.counts["vendor_holds"] = [{"name": v.get("display_name") or "", "state": v.get("state"),
                                       "detail": v.get("detail") or ""} for v in vendors
                                      if str(v.get("state", "")).startswith("HOLD") or v.get("state") == "FAILED"]
        res.counts.update(bills_waiting(out, summary, vendors))
        res.counts["routine_repeats"] = len(summary.get("routine_repeats") or [])
        res.counts["cash_paid"] = sched.get("cash_paid", 0)
        res.counts["cash_paid_total"] = sched.get("cash_paid_total", "0.00")
        res.counts["cash_pay_failed"] = sched.get("cash_pay_failed") or []
        res.counts["vendors"] = [f"{v.get('state')}: {v.get('display_name')}"
                                 + (f" -> QBO {v['vendor_id']}" if v.get("vendor_id") else "") for v in vendors]
        if sched.get("stopped"):
            res.detail = f"post stopped: {sched['stopped']}"
        for h in (summary.get("holds") or [])[:10]:
            res.review.append(f"bill HOLD PO {h.get('po')} {h.get('supplier') or '(no supplier)'} "
                              f"N{h.get('total_inc')}: {(h.get('reasons') or [''])[0][:160]}")
        for v in vendors:
            if str(v.get("state", "")).startswith("HOLD") or v.get("state") == "FAILED":
                res.review.append(f"vendor {v.get('state')}: {v.get('detail', '')[:200]}")
        if rc == 0 and self.dry_run and c.get("HOLD", 0):
            res.status = REVIEW  # plan exits 0 even with holds; a dry run still shows them as waiting
        elif rc == 0:
            res.status = OK
        elif rc == 3:
            res.status = REVIEW
            waiting = sched.get("waiting", c.get("READY", 0) + c.get("HOLD", 0))
            res.review.append(f"bills: {waiting} bill(s) wait for review -> {out / 'review.csv'} "
                              f"(Approve=yes, then bills_sync post --expect-sha {str(summary.get('payloads_sha256', ''))[:12]}...)")
        else:
            res.status = FAILED
            res.detail = res.detail or f"bills_sync exited {rc}; see {out / 'log.txt'}"

    def step_sales(self, res: StepResult) -> None:
        out = Path(res.out)
        from code_scripts.operations_controls import HOLD_CLEAR_HINT, posting_hold_path

        hold = posting_hold_path()
        if hold.exists() and not self.dry_run:
            res.status = REVIEW
            info = read_json(hold, {}) or {}
            res.detail = (f"Company A posting hold in place since {info.get('at', '?')} (business date "
                          f"{info.get('business_date', '?')}); sales not run")
            res.review.append(f"sales: posting hold {hold} - {HOLD_CLEAR_HINT}, then re-run "
                              f"daily_run --date {self.date} --only sales")
            return
        dry = self.dry_run
        standing_note = ""
        if not dry:
            try:
                from code_scripts.standing_approval import standing_approval_settings

                standing = standing_approval_settings(self.base_env)
            except ValueError as exc:  # StandingApprovalConfigError
                standing, standing_note = None, f"standing approval misconfigured: {exc}"
            if standing is None:
                dry = True
                standing_note = standing_note or ("standing auto-approval is off "
                                                  "(OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED + "
                                                  "OIAT_COMPANY_A_STANDING_APPROVAL_REF)")
        cmd = [self.python, "run_pipeline.py", "--company", COMPANY, "--target-date", self.date]
        if dry:
            cmd.append("--dry-run")
        started = time.time()
        rc = self.run_step("sales", cmd, out)
        res.exit_code = rc
        res.counts = {"mode": "dry-run" if dry else "post", **sales_stats(self.date, since=started)}
        if rc == 0 and not dry:
            res.status = OK
        elif rc == 0 and self.dry_run:
            res.status = OK
            res.detail = "dry-run built (nothing posted)"
        elif rc == 0:
            res.status = REVIEW
            res.detail = f"{standing_note}; dry-run built, nothing posted"
            res.review.append(f"sales {self.date}: review the dry-run evidence, then post by hand with a chat yes "
                              f"(log {out / 'log.txt'})")
        elif hold.exists():
            res.status = REVIEW
            info = read_json(hold, {}) or {}
            res.detail = f"held: posting hold written ({info.get('source', '?')}); nothing more posts"
            res.review.append(f"sales {self.date}: posting hold {hold} - fix the cause (log {out / 'log.txt'}), "
                              f"{HOLD_CLEAR_HINT}, then re-run daily_run --date {self.date} --only sales")
        else:
            res.status = FAILED
            res.detail = (f"{standing_note + '; ' if standing_note else ''}run_pipeline exited {rc}; "
                          f"see {out / 'log.txt'}")

    def step_credit(self, res: StepResult) -> None:
        """EPOS credit sales -> one QBO Invoice per customer per day (credit_invoices, in-process)."""
        from code_scripts.akponora_ops import credit_invoices as ci
        from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

        if not ci.days_to_do(self.date):
            res.status, res.detail = OK, "no credit sales"
            res.counts = {"mode": "", "invoices": [], "mixed": [], "customers_created": [], "posted_total": "0"}
            return
        client = self.credit_client or QBOClient.for_company_a(allow_writes=False)
        write = None
        if ci.mode(self.base_env) == "post" and not self.dry_run:
            write = self.credit_write_client or QBOClient.for_company_a(allow_writes=True)
        r = ci.run(Path(res.out), business_day=self.date, client=client, write_client=write,
                   dry_run=self.dry_run, env=self.base_env)
        invoices = r["invoices"]
        res.counts = {"mode": r["mode"], "invoices": invoices, "mixed": r["mixed"],
                      "customers_created": r["customers_created"],
                      "posted_total": str(sum(ci.money(i.get("total")) for i in invoices if i["status"] == ci.POSTED))}
        for i in invoices:
            if i["status"] == ci.HELD:
                res.review.append(f"credit {i['day']} {i['doc']} {', '.join(i['epos_names'])}: {i.get('reason', '')}")
            elif i["status"] == ci.PLANNED and not self.dry_run:
                res.review.append(f"credit {i['day']} {i['doc']} {', '.join(i['epos_names'])} N{i.get('total')}: "
                                  f"planned, not posted ({ci.MODE_ENV}=post to post)")
        for m in r["mixed"]:
            res.review.append(f"credit {m['day']}: {m['rows']} mixed-tender row(s) N{m['total']} "
                              f"({', '.join(m['customers'])}) not invoiced")
        if r["failed"]:
            res.status = REVIEW
        elif res.review:
            res.status = REVIEW
        else:
            res.status = OK
        if not invoices and not r["mixed"]:
            res.detail = "no credit sales"

    def step_guard(self, res: StepResult) -> None:
        out = Path(res.out)
        cmd = [self.python, "-m", "code_scripts.akponora_ops.item_guard", "--out", str(out), "--no-slack",
               "--fail-on-alert"]
        if self.dry_run:
            cmd.append("--no-state")
        rc = self.run_step("guard", cmd, out)
        res.exit_code = rc
        report = read_json(out / "report.json", {}) or {}
        counts = report.get("counts") or {}
        res.counts = {"alert": counts.get("ALERT", 0), "warn": counts.get("WARN", 0)}
        if rc == 0:
            res.status = OK
        elif rc == 4:
            res.status = REVIEW
            res.review.append(f"item guard: {counts.get('ALERT', 0)} ALERT(s) -> {out / 'alerts.csv'}")
        else:
            res.status = FAILED
            res.detail = f"item_guard exited {rc}; see {out / 'log.txt'}"

    def step_stock(self, res: StepResult) -> None:
        """Read-only stock snapshot (EPOS vs QBO); report only."""
        out = Path(res.out)
        rc = self.run_step("stock", [self.python, "-m", "code_scripts.akponora_ops.stock_snapshot", "run",
                                     "--out", str(out)], out)
        res.exit_code = rc
        summary = read_json(out / "summary.json", {}) or {}
        s = summary.get("summary") or {}
        res.counts = {"by_status": s.get("by_status") or {}, "likely_timing": s.get("different_likely_timing", 0),
                      "unmapped_tracked": s.get("unmapped_tracked", 0), "text": summary.get("summary_text", ""),
                      "latest": summary.get("latest", "")}
        if rc == 0:
            res.status = OK
            if truthy(self.base_env.get("OIAT_COMPANY_A_STOCK_MOVEMENTS_ENABLED")):
                self.stock_movement_review(res, summary)
        else:
            res.status = FAILED
            res.detail = (f"stock_snapshot exited {rc}"
                          + (f" ({summary['error'][:200]})" if summary.get("error") else "")
                          + f"; see {out / 'log.txt'}")

    def stock_movement_review(self, res: StepResult, snapshot_summary: dict) -> None:
        """Opt-in view-only detection. Activation needs owner approval; default is off."""
        sources = snapshot_summary.get("sources") or {}
        catalogue = (sources.get("catalogue") or {}).get("path")
        mapping = (sources.get("mapping") or {}).get("path")
        if not catalogue or not mapping:
            res.status, res.detail = FAILED, "Stock movement check needs the stock snapshot's catalogue and mapping sources."
            res.exit_code = 2
            return
        out = self.step_dir("movements")
        start = max(date(2026, 10, 1), date.fromisoformat(self.date) - timedelta(days=2)).isoformat()
        rc = self.run_step("movements", [self.python, "-m", "code_scripts.akponora_ops.stock_movements",
            "--capture-live", "--captures", str(out / "captures"), "--catalogue", str(catalogue),
            "--mapping", str(mapping), "--from-date", start, "--through-date", self.date, "--out", str(out)], out)
        report = read_json(out / "summary.json", {}) or {}
        events = report.get("events") or []
        res.counts["movement_events"] = len(events)
        res.counts["movement_capture_errors"] = len(report.get("errors") or [])
        if rc != 0 or report.get("errors") or not report:
            res.status, res.detail = FAILED, f"EPOS stock-movement capture incomplete; see {out / 'summary.json'}"
            res.exit_code = rc or 2
        elif events:
            res.status = REVIEW
            res.review.append(f"{len(events)} EPOS stock adjustment(s) need classification; no stock corrections posted.")

    def step_uf(self, res: StepResult) -> None:
        """Undeposited Funds deposits from the till sheet (uf_deposits ``scheduled``, in-process)."""
        from code_scripts.akponora_ops import uf_deposits as ufd

        s = ufd.settings(self.base_env)
        if not s["enabled"]:
            res.status = DISABLED
            res.detail = f"off ({UF_ENV}=1 to deposit Undeposited Funds from the till sheet)"
            return
        client = self.uf_client
        if client is None:
            from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient

            client = QBOClient.for_company_a(allow_writes=False)
        source = self.uf_sheet or ufd.sheet_source(s)
        accounts = ufd.load_accounts(ufd.accounts_path(s))
        r = ufd.run_scheduled(Path(res.out), business_day=self.date, client=client, source=source, s=s,
                              accounts=accounts, write_client=self.uf_write_client, dry_run=self.dry_run)
        mode = "dry-run (plan only)" if self.dry_run else ("auto-post" if r["auto_post"] else "plan only")
        sheet = r.get("till_sheet") or {}
        res.counts = {
            "mode": mode, "window": r["window"], "uf_balance": r["uf_balance"],
            "deposited": [{"day": d["day"], "total": d["receipts_total"], "by_bank": d["final_by_bank"],
                           "overage": d.get("overage", "0.00")}
                          for d in r["days"] if d["status"] == ufd.DEPOSITED and d.get("posted_now")],
            "held": [{"day": d["day"], "status": d["status"], "reason": (d["reasons"] or [""])[0]}
                     for d in r["days"] if d["status"] in (ufd.HELD, ufd.WAITING_SHEET, ufd.NO_SALES)],
            "ready": [{"day": d["day"], "total": d["receipts_total"], "by_bank": d["final_by_bank"]}
                      for d in r["days"] if d["status"] == ufd.READY],
            "already_done": sum(1 for d in r["days"] if d["status"] == ufd.DEPOSITED and not d.get("posted_now")),
            "till_sheet": sheet.get("text", ""),
            "till_sheet_last_day": sheet.get("last_complete_day"),
            "till_sheet_missing": list(sheet.get("missing") or []) + [x["day"] for x in sheet.get("incomplete") or []],
            "till_sheet_waiting_to_deposit": [x["day"] for x in sheet.get("complete_not_deposited") or []],
            "sheet_marks_written": len((r.get("sheet_marks") or {}).get("written") or []),
            "sheet_marks_error": (r.get("sheet_marks") or {}).get("error", ""),
        }
        for d in r["days"]:
            if d["status"] in (ufd.HELD, ufd.WAITING_SHEET, ufd.NO_SALES):
                res.review.append(f"uf {d['day']} {d['status']}: {(d['reasons'] or [''])[0][:200]} "
                                  f"-> {d['dir']}/review.csv")
            elif d["status"] == ufd.READY and not self.dry_run:
                res.review.append(f"uf {d['day']} READY N{d['receipts_total']} (plan only): {d['post_command']}")
        if r["stopped"]:
            res.status = FAILED
            res.detail = f"post stopped: {r['stopped']}"
        elif r["waiting"]:
            res.status = REVIEW
        else:
            res.status = OK

    # ------------------------------------------------------------ orchestration
    def execute(self) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = _now_utc()
        if self.send_slack:
            try:
                self.slack(start_text(self.date, dry_run=self.dry_run, only=self.only,
                                      banking_on=truthy(self.base_env.get(UF_ENV)), links=self.links()))
            except Exception:  # noqa: BLE001 - Slack must never break the run
                pass
        for name in STEPS:
            res = StepResult(name=name, started_at=_now_utc())
            if name not in self.only:
                res.status, res.detail = SKIPPED, "not selected (--only)"
                self.results.append(res)
                continue
            res.out = str(self.step_dir(name))
            try:
                getattr(self, f"step_{name}")(res)
            except Exception as exc:  # noqa: BLE001 - a crashed step never stops the later ones
                res.status, res.detail = FAILED, f"{type(exc).__name__}: {exc}"
            res.finished_at = _now_utc()
            self.results.append(res)
        return self.finish()

    def links(self) -> dict:
        return portal_links(self.base_env, self.date, self.run_dir.name)

    def overall_exit(self) -> int:
        statuses = {r.status for r in self.results}
        if FAILED in statuses:
            return EXIT_FAILED
        if REVIEW in statuses:
            return EXIT_REVIEW
        return EXIT_OK

    def finish(self) -> dict:
        code = self.overall_exit()
        summary = {"tool": TOOL, "company": COMPANY, "business_date": self.date, "dry_run": self.dry_run,
                   "exit_code": code, "status": {0: "clean", 2: "failed", 3: "review"}[code],
                   "run_dir": str(self.run_dir), "started_at": self.started_at, "finished_at": _now_utc(),
                   "previous_guard_alert": previous_guard_alert(self.day_dir.parent, self.date),
                   "previous_stock_negative": previous_stock_negative(self.day_dir.parent, self.date),
                   "uf_aged_days": aged_days(self.base_env),
                   "links": self.links(),
                   "steps": [asdict(r) for r in self.results],
                   "waiting_for_review": [line for r in self.results for line in r.review]}
        dump_json(self.run_dir / "summary.json", summary)
        dump_json(self.day_dir / "summary.json", summary)
        if self.send_slack:
            try:
                self.slack(slack_text(summary))
            except Exception:  # noqa: BLE001
                pass
        return summary


# ---------------------------------------------------------------- reporting
ICON = {OK: ":white_check_mark:", REVIEW: ":warning:", FAILED: ":x:", SKIPPED: ":fast_forward:",
        DISABLED: ":zzz:"}


def step_line(r: dict) -> str:
    c = r.get("counts") or {}
    name = r["name"]
    if r["status"] in (SKIPPED, DISABLED):
        body = r.get("detail", "")
    elif name == "catalogue":
        body = (f"{c.get('new_products', 0)} new EPOS product(s); items created {c.get('items_created', 0)}, "
                f"mapping-only {c.get('mapping_only', 0)}, review {c.get('review', 0)}, hold {c.get('hold', 0)}"
                + ("; mapping installed" if c.get("mapping_installed") else ""))
    elif name == "bills":
        body = (f"{c.get('pos', 0)} PO(s) {'..'.join(c.get('window') or [])}; posted {c.get('posted', 0)} "
                f"(READY N{c.get('ready_total', '0.00')}), hold {c.get('hold', 0)} (N{c.get('hold_total_inc', '0.00')}), "
                f"capped {c.get('capped', 0)}, already posted {c.get('already_posted', 0)}; "
                f"vendors created {c.get('vendors_created', 0)}, held {c.get('vendors_held', 0)}. Bills left UNPAID")
        if c.get("vendors"):
            body += " | " + "; ".join(c["vendors"][:5])
    elif name == "sales":
        bits = [c.get("mode", "")]
        if c.get("uploaded") is not None:
            bits.append(f"receipts uploaded {c.get('uploaded')}, skipped {c.get('skipped', 0)}, failed {c.get('failed', 0)}")
        if c.get("reconcile_status"):
            bits.append(f"reconcile {c['reconcile_status']} EPOS N{c.get('epos_total')} / QBO N{c.get('qbo_total')}")
        body = "; ".join(b for b in bits if b)
    elif name == "guard":
        body = f"ALERT {c.get('alert', 0)}, WARN {c.get('warn', 0)} (read-only)"
    elif name == "stock":
        body = c.get("text") or ""
    elif name == "uf":
        bits = [c.get("mode", "")]
        dep = c.get("deposited") or []
        bits.append(f"deposited {len(dep)} day(s)" + (": " + "; ".join(
            f"{d['day']} N{d['total']} (" + ", ".join(f"{k} N{v}" for k, v in (d.get("by_bank") or {}).items()) + ")"
            for d in dep) if dep else ""))
        if c.get("ready"):
            bits.append("ready, not posted: " + ", ".join(f"{d['day']} N{d['total']}" for d in c["ready"]))
        held = c.get("held") or []
        if held:
            first = held[0]
            bits.append(f"not deposited {len(held)} day(s) ({', '.join(d['day'] for d in held[:8])}"
                        f"{', ...' if len(held) > 8 else ''}); first {first['day']}: {first['reason'][:160]}")
        bits.append(f"Undeposited Funds N{c.get('uf_balance')}")
        body = "; ".join(b for b in bits if b)
        if c.get("till_sheet"):
            body += ". " + c["till_sheet"]
    else:
        body = ""
    detail = r.get("detail") or ""
    if detail and r["status"] not in (SKIPPED, DISABLED) and detail not in body:
        body = f"{body} - {detail}" if body else detail
    return f"{ICON.get(r['status'], '')} *{name}* [{r['status']}] {body}".rstrip()


def bills_waiting(out: Path, summary: dict, vendors: list) -> dict:
    """{posted_total, waiting_items}: what posted (results.csv) and every PO a person still has to act on."""
    import csv
    from decimal import Decimal, InvalidOperation

    def rows(name):
        try:
            with open(out / name, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except OSError:
            return []

    def dec(v):
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return Decimal(0)

    posted = {r.get("PO") for r in rows("results.csv") if r.get("status") in ("POSTED", "ADOPTED")}
    posted_total = sum((dec(r.get("Total")) for r in rows("results.csv") if r.get("status") == "POSTED"), Decimal(0))
    held_vendors = {v.get("display_name"): v for v in vendors
                    if str(v.get("state", "")).startswith("HOLD") or v.get("state") == "FAILED"}
    waiting = []
    for r in rows("review.csv"):
        if r.get("Status") not in ("READY", "HOLD") or r.get("PO") in posted:
            continue
        supplier = r.get("EPOS Supplier") or ""
        item = {"po": r.get("PO"), "supplier": supplier, "total": r.get("EPOS Total Inc") or r.get("Bill Total"),
                "status": r.get("Status"), "why": r.get("Reasons") or r.get("Warnings") or ""}
        if supplier in held_vendors:
            item["vendor_hint"] = _vendor_hint(held_vendors[supplier].get("detail", ""))
        waiting.append(item)
    return {"posted_total": str(posted_total) if posted else "", "waiting_items": waiting}


def technical_text(summary: dict) -> str:
    """The full technical summary (counts, paths, commands): printed to the log / container output."""
    head = {0: ":white_check_mark: all clean", 3: ":warning: waiting for review", 2: ":x: a step FAILED"}[
        summary["exit_code"]]
    lines = [f"Akponora daily run {summary['business_date']}{' (DRY RUN - nothing written)' if summary['dry_run'] else ''}: {head}"]
    lines += [step_line(r) for r in summary["steps"]]
    if summary["waiting_for_review"]:
        lines.append("Waiting for review:")
        lines += [f"- {line}" for line in summary["waiting_for_review"][:20]]
    lines.append(f"Evidence: {summary['run_dir']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the Slack messages (plain English)
STEP_LABEL = {"catalogue": "Products", "bills": "Bills", "sales": "Sales", "credit": "Credit sales", "guard": "Item check",
              "stock": "Stock", "uf": "Banking"}
SHEET_URL = "https://docs.google.com/spreadsheets/d/{}"


def human_day(day: str) -> str:
    """'2026-10-02' -> 'Fri 2 Oct 2026'."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    return f"{d:%a} {d.day} {d:%b %Y}"


def day_list(days) -> str:
    """['2026-09-25', '2026-09-26', '2026-10-01'] -> '25, 26 Sep and 1 Oct'."""
    groups: list[tuple[str, list[str]]] = []
    for day in sorted(set(days)):
        try:
            d = date.fromisoformat(str(day)[:10])
        except ValueError:
            continue
        month = f"{d:%b}"
        if groups and groups[-1][0] == month:
            groups[-1][1].append(str(d.day))
        else:
            groups.append((month, [str(d.day)]))
    parts = [f"{', '.join(n)} {m}" for m, n in groups]
    return " and ".join([", ".join(parts[:-1]), parts[-1]]) if len(parts) > 1 else (parts[0] if parts else "")


def naira_text(value) -> str:
    """₦4,857,550 (kobo shown only when there are any). Unknown -> 'unknown'."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(value).replace(",", "").replace("N", "").replace("₦", ""))
    except (InvalidOperation, ValueError):
        return "unknown"
    return f"₦{amount:,.0f}" if amount == amount.to_integral_value() else f"₦{amount:,.2f}"


def portal_links(env: dict, day: str, run_id: str) -> dict:
    """Links for the Slack message: the run page, the daily-runs list, the inbox, the till sheet."""
    base = (env.get("OIAT_PORTAL_BASE_URL") or "").strip().rstrip("/")
    if not base and (env.get("PORTAL_DOMAIN") or "").strip():
        base = f"https://{env['PORTAL_DOMAIN'].strip().rstrip('/')}"
    sheet_id = (env.get("OIAT_COMPANY_A_TILL_SHEET_ID") or "").strip()
    if not sheet_id:
        try:
            from code_scripts.akponora_ops.till_sheet import DEFAULT_SHEET_ID as sheet_id
        except Exception:  # noqa: BLE001
            sheet_id = ""
    links = {"till_sheet": SHEET_URL.format(sheet_id) if sheet_id else ""}
    if base:
        links.update(run=f"{base}/epos-qbo/company-a/daily-runs/{day}/{run_id}/",
                     runs=f"{base}/epos-qbo/company-a/daily-runs/", inbox=f"{base}/epos-qbo/attention/")
    return links


def link(url: str, label: str) -> str:
    return f"<{url}|{label}>" if url else label


def previous_guard_alert(daily_root: Path, day: str) -> int | None:
    """Item-check ALERT count of the last real (not dry) run before ``day``; None when there is none
    (or when anything about the old evidence can't be read - this must never break a run)."""
    try:
        return _previous_count(daily_root, day, "guard", lambda c: c["alert"] if "alert" in c else None)
    except Exception:  # noqa: BLE001
        return None


def previous_stock_negative(daily_root: Path, day: str) -> int | None:
    """Items negative in QuickBooks at the last real stock check before ``day`` (None when unknown)."""
    try:
        return _previous_count(daily_root, day, "stock",
                               lambda c: (c.get("by_status") or {}).get("NEGATIVE_QBO") if c.get("by_status") else None)
    except Exception:  # noqa: BLE001
        return None


def _previous_count(daily_root: Path, day: str, step: str, pick: Callable) -> int | None:
    try:
        days = sorted((p for p in Path(daily_root).iterdir() if p.is_dir() and p.name < day), reverse=True)
    except OSError:
        return None
    for folder in days:
        runs = sorted((p for p in folder.glob("run_*") if not p.name.endswith("_dry")), reverse=True)
        for run in runs:
            summary = read_json(run / "summary.json", {}) or {}
            found = next((s for s in summary.get("steps") or [] if s.get("name") == step), None)
            if found and found.get("status") in (OK, REVIEW):
                value = pick(found.get("counts") or {})
                if value is not None:
                    return int(value)
    return None


def day_title(day: str) -> str:
    """'*Nora Mart · business day Fri 2 Oct*' - the bold title every Slack message starts with."""
    return f"*Nora Mart · business day {short_day(day)}*"


def start_text(day: str, *, dry_run: bool = False, only=None, banking_on: bool = False, links=None) -> str:
    links = links or {}
    parts = [("sales", "sales"), ("bills", "bills"), ("uf", "banking" if banking_on else "")]
    names = [label for step, label in parts if label and (not only or step in only)]
    if not only or any(s in only for s in ("catalogue", "guard", "stock")):
        names.append("checks")
    return (f":arrow_forward: {day_title(day)} · daily run started ({', '.join(names)})"
            + (" · _practice run, nothing will be posted_" if dry_run else "")
            + " · summary to follow · " + link(links.get("runs", ""), "Daily runs"))


def short_day(day: str) -> str:
    """'2026-10-02' -> 'Fri 2 Oct'."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    return f"{d:%a} {d.day} {d:%b}"


def _vendor_hint(detail: str) -> str:
    """'... looks like an existing QBO vendor (best 0.90): 64 FLOURISH (0.90); ...' -> 'FLOURISH'."""
    import re

    m = re.search(r"\d+ ([^();:]+?) \((?:0|1)\.\d+\)", detail or "")
    return m.group(1).strip() if m else ""


def _bank_reason(reason: str) -> str:
    """A held deposit day's reason in a few plain words."""
    text = (reason or "").lower()
    if "box is blank" in text or "sheet day is blank" in text:
        return "till sales breakdown incomplete"
    if "differ" in text or "tolerance" in text or "outside" in text and "sheet total" in text:
        return "till sheet and sales don't agree"
    if "outside this tool" in text or "deposited" in text and "by hand" in text:
        return "some of the day's sales were banked by hand"
    if "closing date" in text or "closed" in text:
        return "the day is in a closed period in QuickBooks"
    if "limit" in text or "cap" in text:
        return "larger than the automatic limit"
    if "not in till_accounts" in text or "unknown till line" in text:
        return "the till sheet has a line we don't recognise"
    return "see the run"


def aged_days(env: dict) -> int:
    try:
        return max(1, int(str(env.get(AGED_ENV) or "").strip() or 7))
    except ValueError:
        return 7


def days_waiting(day: str, business_date: str) -> int:
    try:
        return (date.fromisoformat(business_date) - date.fromisoformat(day)).days
    except ValueError:
        return 0


def _lagos_time(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def _trend(now: int, before) -> str:
    """'(3 more than last run)' / '(2 fewer than last run)' / '(same as last run)'; '' when unknown."""
    if before is None:
        return ""
    if now == before:
        return " (same as last run)"
    return f" ({abs(now - before):,} {'more' if now > before else 'fewer'} than last run)"


def slack_text(summary: dict) -> str:
    """The daily Slack summary, read on a phone: a one-line headline (status, business day, sales posted,
    to-do count), one *bold* line per area with any extra facts as bullets under it, then who needs to do
    what (who · what → where) and a short footer. No error text or file paths (they are behind 'Open run');
    the technical version is ``technical_text``."""
    from decimal import Decimal

    steps = {s["name"]: s for s in summary.get("steps") or []}
    links = summary.get("links") or {}
    dry = bool(summary.get("dry_run"))
    inbox = link(links.get("inbox", ""), "Inbox")
    run = link(links.get("run", ""), "Open run")
    sheet = link(links.get("till_sheet", ""), "Till sheet")
    lines: list[str] = []
    you: list[str] = []
    store: list[str] = []
    oiat: list[str] = []
    stopped: list[str] = []   # headline words for anything that failed
    sales_posted = ""         # the headline number

    def counts(name):
        return (steps.get(name) or {}).get("counts") or {}

    def status(name):
        return (steps.get(name) or {}).get("status")

    def section(label, text, bullets=()):
        lines.append(f"*{label}:* {text}")
        lines.extend(f"• {b}" for b in bullets if b)

    # Sales
    if status("sales") not in (None, SKIPPED, DISABLED):
        c = counts("sales")
        if status("sales") == FAILED:
            text = "didn't post"
            stopped.append("sales didn't post")
            oiat.append(f"sales didn't post → {run}")
        elif dry:
            text = "practice run built, nothing posted"
        elif status("sales") == REVIEW:
            text = "paused, nothing posted"
            oiat.append(f"sales are paused → {run}")
        else:
            uploaded, skipped = c.get("uploaded") or 0, c.get("skipped") or 0
            amount = naira_text(c.get("qbo_total")) if c.get("qbo_total") is not None else ""
            if uploaded:
                text = f"{amount} posted ({_plural(uploaded, 'receipt')})" if amount else \
                    f"{_plural(uploaded, 'receipt')} posted"
                sales_posted = amount
            elif skipped:
                text = f"already in QuickBooks ({_plural(skipped, 'receipt')})"
            else:
                text = "nothing to post"
            rec = c.get("reconcile_status") or ""
            if rec == "MATCH":
                text += " · matches EPOS"
            elif rec:
                text += f" · *doesn't match EPOS* (EPOS {naira_text(c.get('epos_total'))})"
                oiat.append(f"sales don't match EPOS → {run}")
        section("Sales", text)

    # Credit sales: invoices to customers (Accounts Receivable), kept out of the sales receipts
    if status("credit") not in (None, SKIPPED, DISABLED):
        c = counts("credit")
        invoices, mixed = c.get("invoices") or [], c.get("mixed") or []
        if status("credit") == FAILED:
            stopped.append("credit sales didn't run")
            oiat.append(f"credit sales didn't run → {run}")
            section("Credit", "didn't run")
        elif invoices or mixed:
            done = [i for i in invoices if i.get("status") in ("posted", "already in QuickBooks")]
            new = [i for i in invoices if i.get("status") == "posted"]
            planned = [i for i in invoices if i.get("status") == "planned"]
            held = [i for i in invoices if i.get("status") == "held"]
            total = sum(Decimal(str(i.get("total") or 0)) for i in new)
            if new:
                text = f"{naira_text(total)} invoiced ({_plural(len(new), 'customer')})"
            elif planned:
                ptotal = sum(Decimal(str(i.get("total") or 0)) for i in planned)
                text = f"{naira_text(ptotal)} ready to invoice ({_plural(len(planned), 'customer')}), nothing posted"
            elif done:
                text = "already invoiced"
            else:
                text = "nothing invoiced"
            bullets = [f"{i.get('customer') or ', '.join(i.get('epos_names') or [])} · {naira_text(i.get('total'))}"
                       f"{' (new customer)' if i.get('new_customer') or any(x.get('epos_id') == i.get('customer_key') for x in c.get('customers_created') or []) else ''}"
                       for i in new + planned]
            section("Credit", text, bullets)
            if planned and not dry:
                you.append(f"credit sales are planned, not posted: say yes to switch on invoicing → {run}")
            for i in held:
                oiat.append(f"credit sale for {', '.join(i.get('epos_names') or ['?'])} ({naira_text(i.get('total'))}) "
                            f"is on hold → {run}")
            for m in mixed:
                you.append(f"{_plural(m.get('rows') or 0, 'sale line')} on {human_day(m['day'])} mixed credit with another "
                           f"tender ({', '.join(m.get('customers') or ['no name'])}): book by hand → {run}")

    # Bills: what posted, then cash paid, then what waits / was noted
    if status("bills") not in (None, SKIPPED, DISABLED):
        c = counts("bills")
        if status("bills") == FAILED:
            stopped.append("bills didn't run")
            oiat.append(f"bills didn't run → {run}")
            section("Bills", "didn't run")
        else:
            waiting = c.get("waiting_items") or []
            posted = c.get("posted") or 0
            if dry:
                ready = c.get("ready") or 0
                text = (f"{naira_text(c.get('ready_total'))} ready ({_plural(ready, 'bill')}), nothing posted"
                        if ready else "no new purchase orders")
            elif posted:
                total = c.get("posted_total")
                text = (f"{naira_text(total)} posted ({_plural(posted, 'bill')})" if total not in (None, "")
                        else f"{_plural(posted, 'bill')} posted")
            else:
                text = "nothing new to post" if not waiting else "nothing posted"
            bullets = []
            if c.get("cash_paid"):
                bullets.append(f"{naira_text(c.get('cash_paid_total'))} paid in cash ({_plural(c['cash_paid'], 'bill')})")
            if c.get("cash_pay_failed"):
                oiat.append(f"a bill's cash payment didn't go through; retried next run → {run}")
            notes = []
            if waiting:
                notes.append(f"{len(waiting)} waiting for you")
            if c.get("routine_repeats"):
                notes.append(f"{_plural(c['routine_repeats'], 'routine repeat order')} let through")
            if c.get("vendors_created"):
                notes.append(f"{_plural(c['vendors_created'], 'new supplier')} added")
            if c.get("vendors_linked"):
                notes.append(f"{_plural(c['vendors_linked'], 'supplier')} linked by name")
            bullets.append(" · ".join(notes))
            section("Bills", text, bullets)
            for w in waiting:
                po = f"PO {w.get('po')} ({w.get('supplier') or 'no supplier'}, {naira_text(w.get('total'))})"
                if w.get("vendor_hint") is not None:
                    hint = w["vendor_hint"]
                    you.append(f"{po}: link supplier to {hint} or create it → {inbox}" if hint
                               else f"{po}: link or create the supplier → {inbox}")
                elif "duplicate" in (w.get("why") or "").lower():
                    m = __import__("re").search(r"PO (\d+)", w.get("why") or "")
                    earlier = f"PO {m.group(1)}" if m else "an earlier order"
                    you.append(f"{po} looks like {earlier}: approve if real → {inbox}")
                else:
                    you.append(f"approve {po} → {inbox}")

    # Banking (and what is still in Undeposited Funds)
    uf_balance = counts("uf").get("uf_balance")
    uf_line = (f"{naira_text(Decimal(str(uf_balance)).quantize(Decimal('1')))} still in Undeposited Funds"
               if uf_balance not in (None, "") else "")
    if status("uf") not in (None, SKIPPED):
        c = counts("uf")
        if status("uf") == DISABLED:
            section("Banking", "off")
        elif status("uf") == FAILED and not c:
            stopped.append("banking didn't run")
            oiat.append(f"banking didn't run → {run}")
            section("Banking", "didn't run")
        else:
            def total(items):  # money banked: the receipts plus any till-sheet overage
                return sum((Decimal(str(d.get("total") or 0)) + Decimal(str(d.get("overage") or 0)) for d in items),
                           Decimal(0))

            deposited, ready, held = c.get("deposited") or [], c.get("ready") or [], c.get("held") or []
            over_line = ""
            stop_days = [d["day"] for d in held if str(d.get("reason", "")).startswith("post stopped")]
            bits = []
            if deposited:
                bits.append(f"{naira_text(total(deposited))} banked for {day_list(d['day'] for d in deposited)}")
                over = [d for d in deposited if Decimal(str(d.get("overage") or 0)) > 0]
                if over:
                    over_line = (f"{naira_text(sum(Decimal(str(d['overage'])) for d in over))} more on the till sheet "
                                 f"than sales ({day_list(d['day'] for d in over)}), booked to Till Overage")
            if stop_days:
                bits.append(f"stopped part-way on {day_list(stop_days)}")
                stopped.append("banking stopped")
                oiat.append(f"banking stopped part-way on {day_list(stop_days)}; nothing posts twice → {run}")
            if ready and dry:
                bits.append(f"would bank {naira_text(total(ready))} for {day_list(d['day'] for d in ready)}")
            elif ready and stop_days:
                bits.append(f"{day_list(d['day'] for d in ready)} go on the next run")
            elif ready:
                bits.append(f"{naira_text(total(ready))} for {day_list(d['day'] for d in ready)} waiting for you")
                you.append(f"approve banking {naira_text(total(ready))} for {day_list(d['day'] for d in ready)} → {inbox}")
            if not bits:
                bits.append("nothing new to bank")
            section("Banking", " · ".join(bits), [over_line, uf_line])
            sheet_days = defaultdict(list)
            for d in held:
                if d.get("status") == "WAITING_SHEET":
                    sheet_days[_bank_reason(d.get("reason"))].append(d["day"])
                elif d.get("status") == "HELD" and d["day"] not in stop_days:
                    oiat.append(f"banking for {day_list([d['day']])} on hold: {_bank_reason(d.get('reason'))} → {run}")
            for reason, days in sheet_days.items():
                if reason == "till sales breakdown incomplete":
                    store.append(f"complete the till sales breakdown for {day_list(days)} → {sheet}")
                else:
                    store.append(f"check the till sheet for {day_list(days)} ({reason}) → {sheet}")
            if c.get("sheet_marks_error"):
                oiat.append(f"couldn't write the Banked notes on the till sheet (banking itself is fine) → {run}")
            # Escalate money that has sat in Undeposited Funds too long (normal waits stay quiet).
            limit = summary.get("uf_aged_days") or 7
            old = sorted((d for d in held if days_waiting(d["day"], summary["business_date"]) > limit),
                         key=lambda d: d["day"])
            if len(old) == 1:
                oiat.append(f"{day_list([old[0]['day']])} not banked after "
                            f"{days_waiting(old[0]['day'], summary['business_date'])} days: follow up with the store → {sheet}")
            elif old:
                oiat.append(f"{len(old)} days not banked after more than {limit} days ({day_list(d['day'] for d in old)}; "
                            f"oldest {days_waiting(old[0]['day'], summary['business_date'])} days): "
                            f"follow up with the store → {sheet}")

    # Checks (products, item check, stock): did they run, and what changed
    check_bits, broken = [], []
    for name, label in (("catalogue", "products"), ("guard", "item check"), ("stock", "stock check")):
        if status(name) == FAILED:
            broken.append(label)
    c = counts("catalogue")
    if status("catalogue") not in (None, SKIPPED, DISABLED, FAILED):
        created = (c.get("items_created") or 0) + (c.get("mapping_only") or 0)
        waiting = (c.get("review") or 0) + (c.get("hold") or 0)
        if created:
            check_bits.append(f"{_plural(created, 'new product')} added")
        if waiting:
            you.append(f"decide on {_plural(waiting, 'new EPOS product')} → {inbox}")
    if status("guard") not in (None, SKIPPED, DISABLED, FAILED):
        alert, prev = counts("guard").get("alert") or 0, summary.get("previous_guard_alert")
        if prev is not None and alert > prev:
            check_bits.append(f"{_plural(alert - prev, 'new item alert')}")
            oiat.append(f"look at {_plural(alert - prev, 'new item alert')} → {run}")
    if status("stock") not in (None, SKIPPED, DISABLED, FAILED):
        neg = (counts("stock").get("by_status") or {}).get("NEGATIVE_QBO") or 0
        prev = summary.get("previous_stock_negative")
        if neg:
            check_bits.append(f"{_plural(neg, 'item')} with negative stock in QuickBooks{_trend(neg, prev)}")
        elif prev:
            check_bits.append("no items with negative stock in QuickBooks (was {:,})".format(prev))
    if broken:
        stopped.append(f"{' and '.join(broken)} didn't run")
        oiat.append(f"{' and '.join(broken)} didn't run → {run}")
        section("Checks", f"{' and '.join(broken)} didn't run", check_bits)
    elif any(status(n) not in (None, SKIPPED, DISABLED) for n in ("catalogue", "guard", "stock")):
        section("Checks", "all ran" + ("" if check_bits else ", nothing new"), check_bits)

    todo = [f":bust_in_silhouette: *You* · {x}" for x in you] + [f":convenience_store: *Store* · {x}" for x in store] \
        + [f":hammer_and_wrench: *OIAT* · {x}" for x in oiat]
    n = len(todo)
    head_bits = []
    if stopped:
        icon = ":red_circle:"
        head_bits.append(stopped[0])
    elif todo:
        icon = ":large_yellow_circle:"
    else:
        icon = ":large_green_circle:"
    if sales_posted and not dry:
        head_bits.append(f"{sales_posted} sales posted")
    head_bits.append(f"{n} to-do{'s' if n != 1 else ''}" if todo else "all done")
    if dry:
        head_bits.append("_practice run, nothing posted_")
    out = [f"{icon} {day_title(summary['business_date'])} · " + " · ".join(head_bits), ""] + lines
    if todo:
        out += ["", "*To do*"] + todo
    foot = []
    finished = _lagos_time(summary.get("finished_at"))
    if finished:
        foot.append(f"Finished {finished}")
    if links.get("run"):
        foot.append(run)
    if foot:
        out += ["", " · ".join(foot)]
    return "\n".join(out)


def _minutes(start: str | None, end: str | None) -> str:
    try:
        secs = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return ""
    return f"{max(1, round(secs / 60))} min"


def sales_stats(day: str, *, since: float | None = None) -> dict:
    """Upload / reconcile figures from the pipeline metadata for ``day`` (archived or in place).
    Only metadata written after ``since`` (epoch seconds) counts as this run's."""
    try:
        from code_scripts.scripts.akponora_cutover._common import company_config

        name = company_config(COMPANY).metadata_file
    except Exception:  # noqa: BLE001
        name = "last_epos_transform.json"
    candidates = [REPO_ROOT / "code_scripts" / "Uploaded" / day / name, REPO_ROOT / "code_scripts" / name]
    try:
        from code_scripts.paths import OPS_UPLOADED_DIR

        candidates.insert(0, Path(OPS_UPLOADED_DIR) / day / name)
    except Exception:  # noqa: BLE001
        pass
    for path in candidates:
        if not path.exists() or (since is not None and path.stat().st_mtime < since - 5):
            continue
        meta = read_json(path, {}) or {}
        target = str(meta.get("target_date") or meta.get("business_date") or day)
        if target[:10] != day:
            continue
        stats = meta.get("upload_stats") or {}
        rec = meta.get("reconcile") or {}
        return {"uploaded": stats.get("uploaded"), "skipped": stats.get("skipped", 0),
                "failed": stats.get("failed", 0), "reconcile_status": rec.get("status", ""),
                "epos_total": rec.get("epos_total"), "qbo_total": rec.get("qbo_total"), "metadata": str(path)}
    return {}


def _daily_root() -> Path:
    setup_env()
    from code_scripts.paths import STATE_ROOT

    return Path(STATE_ROOT) / "ops" / COMPANY / "daily"


def _company_slack_env_key() -> str:
    try:
        from code_scripts.scripts.akponora_cutover._common import company_config

        key = (company_config(COMPANY)._data.get("slack") or {}).get("webhook_url_env_key") or ""
    except Exception:  # noqa: BLE001
        return "SLACK_WEBHOOK_URL_A"
    return "" if key.startswith("http") else key


# ---------------------------------------------------------------- lock + CLI
def acquire_lock(holder: str, *, wait_minutes: float, sleep=time.sleep):
    """Return an acquired GlobalRunLock, waiting up to ``wait_minutes``; None when still busy."""
    from code_scripts.run_lock import GlobalRunLock

    deadline = time.monotonic() + wait_minutes * 60
    while True:
        lock = GlobalRunLock(holder=holder)
        if lock.acquire().acquired:
            return lock
        if time.monotonic() >= deadline:
            return None
        sleep(min(60, max(1, deadline - time.monotonic())))


def parse_only(text: str | None) -> list[str] | None:
    if not text:
        return None
    names = [x.strip() for x in text.split(",") if x.strip()]
    bad = [n for n in names if n not in STEPS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown step(s) {bad}; choose from {','.join(STEPS)}")
    return names


def main(argv=None, *, runner: Callable = run_command, slack: Callable | None = None, sleep=time.sleep,
         uf_client=None, uf_write_client=None, uf_sheet=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="business date YYYY-MM-DD (default: last closed Lagos business day)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing to QBO; no Slack unless --slack")
    ap.add_argument("--only", type=parse_only, help=f"comma-separated subset of {','.join(STEPS)}")
    ap.add_argument("--slack", action="store_true", help="send the Slack summary on a dry run too")
    ap.add_argument("--no-slack", action="store_true", help="never send the Slack summary")
    a = ap.parse_args(argv)
    setup_env()
    day = a.date or last_closed_business_date().isoformat()
    date.fromisoformat(day)
    send = not a.no_slack and (a.slack or not a.dry_run)
    wait = float(os.getenv(LOCK_WAIT_ENV, "").strip() or 30)
    lock = acquire_lock(f"akponora_daily_run:{day}{':dry' if a.dry_run else ''}", wait_minutes=wait, sleep=sleep)
    if lock is None:
        text = (f":red_circle: {day_title(day)} · didn't start\n\n"
                f":hammer_and_wrench: *OIAT* · another pipeline job was still running after {wait:g} minutes, so "
                "nothing ran. Re-run the daily run from the portal once it has finished.")
        print(text)
        if send:
            try:
                (slack or send_summary_slack)(text)
            except Exception:  # noqa: BLE001
                pass
        return EXIT_FAILED
    try:
        run = DailyRun(day, dry_run=a.dry_run, only=a.only, runner=runner, slack=slack, send_slack=send,
                       uf_client=uf_client, uf_write_client=uf_write_client, uf_sheet=uf_sheet)
        summary = run.execute()
    finally:
        lock.release()
    print(technical_text(summary))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())

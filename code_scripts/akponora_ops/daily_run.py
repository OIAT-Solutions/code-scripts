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
4. ``guard``      item_guard read-only scan (report only).
5. ``uf``         Undeposited Funds deposits from the till sheet (``uf_deposits``), off unless
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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops.common import COMPANY, SLACK_ENV, dump_json
from code_scripts.scripts.akponora_cutover._common import REPO_ROOT, business_date, setup_env

TOOL = "daily_run"
TZ = ZoneInfo("Africa/Lagos")
STEPS = ("catalogue", "bills", "sales", "guard", "uf")
ENABLED_ENV = "OIAT_COMPANY_A_DAILY_RUN_ENABLED"
CRON_ENV = "OIAT_COMPANY_A_DAILY_RUN_CRON"
DEFAULT_CRON = "0 6 * * *"
LOCK_WAIT_ENV = "OIAT_COMPANY_A_DAILY_RUN_LOCK_WAIT_MINUTES"
STEP_SLACK_ENV = "OIAT_COMPANY_A_DAILY_RUN_STEP_SLACK"
UF_ENV = "OIAT_COMPANY_A_UF_DEPOSIT_ENABLED"

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
                 uf_sheet=None, python: str = sys.executable):
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
        self.python = python
        stamp = datetime.now(timezone.utc).strftime("%H%M%SZ")
        self.day_dir = Path(root) if root else _daily_root() / business_day
        self.run_dir = self.day_dir / (f"run_{stamp}" + ("_dry" if dry_run else ""))
        self.results: list[StepResult] = []

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
                      "vendors_held": sum(str(v.get("state", "")).startswith("HOLD") for v in vendors)}
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
            "deposited": [{"day": d["day"], "total": d["receipts_total"], "by_bank": d["final_by_bank"]}
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
                   "run_dir": str(self.run_dir), "finished_at": _now_utc(),
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


def slack_text(summary: dict) -> str:
    head = {0: ":white_check_mark: all clean", 3: ":warning: waiting for review", 2: ":x: a step FAILED"}[
        summary["exit_code"]]
    lines = [f"Akponora daily run {summary['business_date']}{' (DRY RUN - nothing written)' if summary['dry_run'] else ''}: {head}"]
    lines += [step_line(r) for r in summary["steps"]]
    if summary["waiting_for_review"]:
        lines.append("Waiting for review:")
        lines += [f"- {line}" for line in summary["waiting_for_review"][:20]]
    lines.append(f"Evidence: {summary['run_dir']}")
    return "\n".join(lines)


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
        text = (f":x: Akponora daily run {day}: the global run lock stayed busy for {wait:g} min "
                "(another run_pipeline / ops job); nothing ran. Re-run when it finishes.")
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
    print(slack_text(summary))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())

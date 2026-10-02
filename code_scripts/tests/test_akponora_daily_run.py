"""daily_run orchestration, automatic vendor creation, payment hint, no double scheduling.

Everything is faked: no subprocess, no EPOS, no QBO, no Slack."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import bills_sync as bs
from code_scripts.akponora_ops import daily_run as dr
from code_scripts.akponora_ops import ops_scheduler
from code_scripts.akponora_ops import vendors as vendor_ops
from code_scripts.tests.test_bills_sync import COKE, WATER, FakeQBO, Fixture, Resp, client, order

STANDING = {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1",
            "OIAT_COMPANY_A_STANDING_APPROVAL_REF": "owner standing yes (test)"}


def _out(cmd):
    return Path(cmd[cmd.index("--out") + 1]) if "--out" in cmd else None


def step_of(cmd) -> str:
    joined = " ".join(cmd)
    for needle, name in (("catalogue_sync", "catalogue"), ("bills_sync", "bills"), ("run_pipeline.py", "sales"),
                         ("item_guard", "guard")):
        if needle in joined:
            return name
    raise AssertionError(cmd)


class FakeRunner:
    """Stands in for ``run_command``: records calls, writes the evidence each job would write."""

    def __init__(self, codes=None, hold_path=None, raise_on=()):
        self.codes = codes or {}
        self.calls = []
        self.envs = []
        self.hold_path = hold_path
        self.raise_on = set(raise_on)

    def __call__(self, cmd, *, log_path, env):
        name = step_of(cmd)
        self.calls.append((name, list(cmd)))
        self.envs.append(env)
        if name in self.raise_on:
            raise RuntimeError(f"{name} exploded")
        out = _out(cmd)
        rc = self.codes.get(name, 0)
        if name == "catalogue":
            (out / "summary.json").write_text(json.dumps({"counts": {"new": 3, "auto": 2, "review": 1, "hold": 0}}))
            (out / "apply_receipt.json").write_text(json.dumps({
                "created": [{"sku": "AKP-9001"}, {"sku": "AKP-NS-9002"}], "adopted": [], "mapping_only": [],
                "installed": {"sha256": "abc", "rows": 6152}, "stopped": None}))
        elif name == "bills":
            (out / "summary.json").write_text(json.dumps({
                "window": ["2026-10-01", "2026-10-02"], "pos_in_window": 4, "counts": {"READY": 2, "HOLD": 1, "SKIP": 1},
                "ready_total": "125000.00", "hold_total_inc": "40000.00", "payloads_sha256": "f" * 64,
                "holds": [{"po": "3999", "supplier": "NIGERIAN BOTLING CO", "total_inc": "40000.00",
                           "reasons": ["supplier 'NIGERIAN BOTLING CO' not approved in vendors.csv"]}],
                "vendor_actions": [
                    {"state": "CREATED", "display_name": "WONUOLA SUPER STORE", "vendor_id": "901", "detail": "created"},
                    {"state": "HOLD_NEAR_MATCH", "display_name": "NIGERIAN BOTLING CO", "vendor_id": "",
                     "detail": "looks like 10 NIGERIAN BOTTLING COMPANY (0.95)"}]}))
            (out / "scheduled.json").write_text(json.dumps({"posted": {"POSTED": 2}, "stopped": None,
                                                            "waiting": 1 if rc == 3 else 0}))
        elif name == "sales":
            if rc not in (0,) and self.hold_path is not None:
                self.hold_path.write_text(json.dumps({"at": "now", "source": "standing_auto_approval",
                                                      "business_date": "2026-10-02"}))
        elif name == "guard":
            (out / "report.json").write_text(json.dumps({"counts": {"ALERT": 1 if rc == 4 else 0, "WARN": 2}}))
        log_path.write_text("fake\n")
        return rc


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hold = self.root / "company_a_posting_hold.json"
        for target, value in (("code_scripts.operations_controls.posting_hold_path", lambda: self.hold),
                              ("code_scripts.akponora_ops.bills_sync.default_from", lambda: "2026-10-01"),
                              ("code_scripts.akponora_ops.daily_run.sales_stats",
                               lambda day, since=None: {"uploaded": 6, "skipped": 0, "failed": 0,
                                                        "reconcile_status": "MATCH", "epos_total": 3211950.0,
                                                        "qbo_total": 3211950.0}),
                              ("code_scripts.akponora_ops.daily_run._company_slack_env_key", lambda: "SLACK_WEBHOOK_URL_A")):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.slack = []

    def make(self, runner, *, env=None, dry=False, only=None, uf_client=None):
        return dr.DailyRun("2026-10-02", dry_run=dry, only=only, root=self.root / "daily" / "2026-10-02",
                           runner=runner, slack=self.slack.append, env=env if env is not None else dict(STANDING),
                           uf_client=uf_client, python="py")

    def test_steps_run_in_order_with_their_own_folders(self):
        runner = FakeRunner()
        summary = self.make(runner).execute()
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard"])
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual([s["name"] for s in summary["steps"]], list(dr.STEPS))
        self.assertEqual(summary["steps"][-1]["status"], dr.DISABLED)  # UF placeholder off by default
        for name, cmd in runner.calls:
            if name != "sales":
                self.assertEqual(_out(cmd).name, name)
        bills_cmd = runner.calls[1][1]
        self.assertEqual(bills_cmd[bills_cmd.index("--from") + 1:bills_cmd.index("--from") + 4],
                         ["2026-10-01", "--to", "2026-10-02"])
        sales_cmd = runner.calls[2][1]
        self.assertEqual(sales_cmd[1:], ["run_pipeline.py", "--company", "company_a", "--target-date", "2026-10-02"])
        self.assertTrue((self.root / "daily" / "2026-10-02" / "summary.json").exists())
        # children inherit the run lock and are silenced (one Slack summary)
        for env in runner.envs:
            self.assertEqual(env["OIAT_RUN_LOCK_HELD"], "1")
            self.assertEqual(env["SLACK_WEBHOOK_URL_A"], "")
        self.assertEqual(len(self.slack), 1)

    def test_catalogue_crash_still_runs_bills_and_sales(self):
        runner = FakeRunner(raise_on={"catalogue"})
        summary = self.make(runner).execute()
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard"])
        self.assertEqual(summary["steps"][0]["status"], dr.FAILED)
        self.assertIn("exploded", summary["steps"][0]["detail"])
        self.assertEqual(summary["exit_code"], 2)

    def test_held_sales_still_runs_guard_and_exits_3(self):
        runner = FakeRunner(codes={"sales": 1}, hold_path=self.hold)
        summary = self.make(runner).execute()
        steps = {s["name"]: s for s in summary["steps"]}
        self.assertEqual(steps["sales"]["status"], dr.REVIEW)
        self.assertEqual(steps["guard"]["status"], dr.OK)
        self.assertIn("guard", [c[0] for c in runner.calls])
        self.assertEqual(summary["exit_code"], 3)
        self.assertTrue(any("posting hold" in line for line in summary["waiting_for_review"]))

    def test_existing_posting_hold_skips_sales(self):
        self.hold.write_text(json.dumps({"at": "yesterday", "business_date": "2026-10-01"}))
        runner = FakeRunner()
        summary = self.make(runner).execute()
        self.assertNotIn("sales", [c[0] for c in runner.calls])
        self.assertEqual({s["name"]: s["status"] for s in summary["steps"]}["sales"], dr.REVIEW)
        self.assertEqual(summary["exit_code"], 3)

    def test_sales_failure_without_hold_is_failed(self):
        summary = self.make(FakeRunner(codes={"sales": 1})).execute()
        self.assertEqual({s["name"]: s["status"] for s in summary["steps"]}["sales"], dr.FAILED)
        self.assertEqual(summary["exit_code"], 2)

    def test_exit_codes(self):
        self.assertEqual(self.make(FakeRunner()).execute()["exit_code"], 0)
        self.assertEqual(self.make(FakeRunner(codes={"bills": 3})).execute()["exit_code"], 3)
        self.assertEqual(self.make(FakeRunner(codes={"catalogue": 4})).execute()["exit_code"], 3)
        self.assertEqual(self.make(FakeRunner(codes={"guard": 4})).execute()["exit_code"], 3)
        self.assertEqual(self.make(FakeRunner(codes={"guard": 1, "bills": 3})).execute()["exit_code"], 2)
        self.assertEqual(self.make(FakeRunner(codes={"bills": 2})).execute()["exit_code"], 2)

    def test_without_standing_approval_sales_is_a_dry_run_waiting_for_review(self):
        runner = FakeRunner()
        summary = self.make(runner, env={}).execute()
        self.assertIn("--dry-run", runner.calls[2][1])
        self.assertEqual({s["name"]: s["status"] for s in summary["steps"]}["sales"], dr.REVIEW)
        self.assertEqual(summary["exit_code"], 3)

    def test_dry_run_writes_nothing(self):
        runner = FakeRunner()
        summary = self.make(runner, dry=True).execute()
        cmds = {name: cmd for name, cmd in runner.calls}
        self.assertEqual(cmds["catalogue"][3], "plan")
        self.assertEqual(cmds["bills"][3], "plan")
        self.assertNotIn("scheduled", " ".join(" ".join(c) for c in cmds.values()))
        self.assertIn("--dry-run", cmds["sales"])
        self.assertIn("--no-state", cmds["guard"])
        self.assertTrue(summary["dry_run"])
        self.assertTrue(dr.slack_text(summary).splitlines()[0].count("DRY RUN"))

    def test_dry_run_main_sends_no_slack_by_default(self):
        runner = FakeRunner()
        slack = []
        with mock.patch.object(dr, "_daily_root", lambda: self.root / "daily"), \
                mock.patch.object(dr, "acquire_lock", return_value=SimpleNamespace(release=lambda: None)):
            rc = dr.main(["--date", "2026-10-02", "--dry-run"], runner=runner, slack=slack.append)
        self.assertEqual(rc, 3)  # the fake bills plan has a HOLD: a dry run shows it as waiting
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard"])
        self.assertEqual(slack, [])

    def test_only_runs_selected_steps(self):
        runner = FakeRunner()
        summary = self.make(runner, only=["sales", "guard"]).execute()
        self.assertEqual([c[0] for c in runner.calls], ["sales", "guard"])
        self.assertEqual({s["name"]: s["status"] for s in summary["steps"]}["catalogue"], dr.SKIPPED)
        with self.assertRaises(argparse.ArgumentTypeError):
            dr.parse_only("sales,deposits")

    def test_slack_summary_lists_counts_vendors_and_review_paths(self):
        summary = self.make(FakeRunner(codes={"bills": 3})).execute()
        text = self.slack[0]
        self.assertIn("Akponora daily run 2026-10-02", text)
        self.assertIn("items created 2", text)
        self.assertIn("mapping installed", text)
        self.assertIn("posted 2", text)
        self.assertIn("vendors created 1", text)
        self.assertIn("CREATED: WONUOLA SUPER STORE -> QBO 901", text)
        self.assertIn("UNPAID", text)
        self.assertIn("receipts uploaded 6", text)
        self.assertIn("reconcile MATCH", text)
        self.assertIn("Waiting for review:", text)
        self.assertIn(str(Path(summary["run_dir"]) / "bills" / "review.csv"), text)
        self.assertIn("HOLD_NEAR_MATCH", text)

    def test_uf_placeholder_reports_only(self):
        class UF:
            def __init__(self):
                self.queries = []

            def query(self, sql):
                self.queries.append(sql)
                if "from Account" in sql:
                    return {"Account": [{"Id": "1150040005", "CurrentBalance": 29233799.99}]}
                return {"Deposit": [{"TxnDate": "2026-09-24"}]}

        uf = UF()
        env = {**STANDING, dr.UF_ENV: "1"}
        summary = self.make(FakeRunner(), env=env, uf_client=uf, only=["uf"]).execute()
        step = {s["name"]: s for s in summary["steps"]}["uf"]
        self.assertEqual(step["status"], dr.OK)
        self.assertEqual(step["counts"]["days_since_last_deposit"], 8)
        self.assertTrue(all(q.lower().startswith("select") for q in uf.queries))

    def test_business_date_is_last_closed_lagos_day(self):
        tz = ZoneInfo("Africa/Lagos")
        self.assertEqual(dr.last_closed_business_date(datetime(2026, 10, 3, 6, 0, tzinfo=tz)).isoformat(), "2026-10-02")
        self.assertEqual(dr.last_closed_business_date(datetime(2026, 10, 3, 4, 59, tzinfo=tz)).isoformat(), "2026-10-01")


class LockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock_path = Path(tmp.name) / "lock"
        p = mock.patch("code_scripts.run_lock.lock_file_path", lambda: self.lock_path)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OIAT_RUN_LOCK_HELD", None)
        os.environ[dr.LOCK_WAIT_ENV] = "0"

    def test_busy_lock_runs_nothing_and_fails(self):
        from code_scripts.run_lock import GlobalRunLock

        manual = GlobalRunLock("run_pipeline:company_a:2026-10-02")
        self.assertTrue(manual.acquire().acquired)
        self.addCleanup(manual.release)
        runner, slack = FakeRunner(), []
        rc = dr.main(["--date", "2026-10-02"], runner=runner, slack=slack.append, sleep=lambda s: None)
        self.assertEqual(rc, 2)
        self.assertEqual(runner.calls, [])
        self.assertIn("lock", slack[0])

    def test_lock_is_held_during_the_run_and_released_after(self):
        from code_scripts.run_lock import GlobalRunLock

        seen = []

        def runner(cmd, *, log_path, env):
            probe = GlobalRunLock("probe")
            seen.append(probe.acquire().acquired)
            probe.release()
            return 0

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(dr, "_daily_root", lambda: Path(root)), \
                mock.patch("code_scripts.operations_controls.posting_hold_path", lambda: Path(root) / "hold.json"), \
                mock.patch("code_scripts.akponora_ops.bills_sync.default_from", lambda: "2026-10-01"), \
                mock.patch.object(dr, "sales_stats", lambda day, since=None: {}):
            dr.main(["--date", "2026-10-02", "--dry-run", "--only", "guard"], runner=runner, slack=lambda t: None)
        self.assertEqual(seen, [False])
        after = GlobalRunLock("after")
        self.assertTrue(after.acquire().acquired)
        after.release()


class NoDoubleSchedulingTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.startswith(("OIAT_AKPONORA_", "OIAT_COMPANY_A_DAILY_RUN"))]:
            os.environ.pop(key)

    def test_daily_run_off_by_default(self):
        self.assertEqual(ops_scheduler.JOBS["daily_run"].cron(), "")
        self.assertNotIn("daily_run", [j.name for j in ops_scheduler.configured_jobs()])

    def test_daily_run_is_the_only_job_when_enabled(self):
        os.environ.update({"OIAT_COMPANY_A_DAILY_RUN_ENABLED": "1", "OIAT_AKPONORA_BILLS_SYNC_CRON": "0 9 * * *",
                           "OIAT_AKPONORA_ITEM_GUARD_CRON": "0 19 * * *"})
        jobs = ops_scheduler.configured_jobs()
        self.assertEqual([j.name for j in jobs], ["daily_run"])
        self.assertEqual(jobs[0].cron(), "0 6 * * *")
        self.assertEqual(jobs[0].command()[1:], ["-m", "code_scripts.akponora_ops.daily_run"])
        self.assertFalse(jobs[0].takes_lock)
        os.environ["OIAT_COMPANY_A_DAILY_RUN_CRON"] = "15 6 * * *"
        self.assertEqual(ops_scheduler.JOBS["daily_run"].cron(), "15 6 * * *")
        os.environ["OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS"] = "1"
        self.assertEqual(sorted(j.name for j in ops_scheduler.configured_jobs()),
                         ["bills_sync", "daily_run", "item_guard"])

    def test_daily_run_failure_does_not_double_alert(self):
        with mock.patch.object(ops_scheduler.subprocess, "run", return_value=SimpleNamespace(returncode=2)), \
                mock.patch.object(ops_scheduler, "send_slack") as slack:
            self.assertEqual(ops_scheduler.run_job(ops_scheduler.JOBS["daily_run"]), 2)
        slack.assert_not_called()


# ---------------------------------------------------------------- vendors inside bills_sync
class VendorFakeQBO(FakeQBO):
    def __init__(self, *a, customers=(), employees=(), **k):
        super().__init__(*a, **k)
        self.customers = list(customers)
        self.employees = list(employees)

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        params = params or {}
        path = url.split("/v3/company/x", 1)[-1]
        if method == "POST" and path == "/vendor":
            self.calls.append((method, path, dict(params)))
            body = json.loads(data)
            self.next_id += 1
            v = {"Id": str(self.next_id), "DisplayName": body["DisplayName"], "CompanyName": body["CompanyName"],
                 "Active": True}
            self.vendors[v["Id"]] = v
            return Resp(200, {"Vendor": v})
        if method == "GET" and path == "/query" and "where DisplayName =" in params["query"]:
            self.calls.append((method, path, dict(params)))
            sql = params["query"]
            name = sql.split("DisplayName = '", 1)[1][:-1].replace("\\'", "'")
            ent = sql.split(" from ", 1)[1].split()[0]
            pool = {"Vendor": list(self.vendors.values()), "Customer": self.customers, "Employee": self.employees}[ent]
            return Resp(200, {"QueryResponse": {ent: [x for x in pool if x["DisplayName"] == name]}})
        return super().request(method, url, params=params, headers=headers, data=data, timeout=timeout)

    def vendor_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/vendor"]


VENDOR_ENV = {vendor_ops.AUTO_ENV: "1", vendor_ops.REF_ENV: "owner vendor yes (test)"}
BILLS_ENV = {bs.AUTO_ENV: "1", bs.AUTO_REF_ENV: "owner bills yes (test)"}


class VendorAutoCreateTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (*VENDOR_ENV, vendor_ops.MAX_ENV, *BILLS_ENV):
            os.environ.pop(key, None)

    def scheduled_plan(self, orders, fake):
        self.f.n += 1
        out = self.f.tmp / f"sched{self.f.n}"
        out.mkdir()
        (out / "po_list.json").write_text(json.dumps({"body": {"orders": [o for o, _ in orders]}}))
        (out / "po_details.jsonl").write_text("".join(json.dumps(d) + "\n" for _, d in orders))
        a = argparse.Namespace(out=str(out), mapping=str(self.f.tmp / "mapping.csv"),
                               vendors=str(self.f.tmp / "vendors.csv"), po_list=str(out / "po_list.json"),
                               po_details=str(out / "po_details.jsonl"), date_from="2026-10-01",
                               date_to="2026-10-05", tax_mode="gross", dup_days=14, dup_min_value="50000",
                               no_slack=True, create_vendors=True)
        with mock.patch.object(bs, "cursor_path", lambda: self.f.cursor), \
                mock.patch.object(bs, "today_lagos", lambda: datetime(2026, 10, 6, 9, 0)):
            out, summary, entries = bs.run_plan(a, client=client(fake), write_client=client(fake, writes=True))
        return out, summary, {e["po"]["ref"]: e for e in entries}

    def vendor_rows(self):
        return {r["EPOS Supplier Name"]: r for r in bs.read_csv(self.f.tmp / "vendors.csv")}

    def test_new_vendor_is_created_and_its_bill_posts(self):
        os.environ.update({**VENDOR_ENV, **BILLS_ENV})
        fake = VendorFakeQBO()
        out, summary, e = self.scheduled_plan([order(3980, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(len(fake.vendor_posts()), 1)
        new_id = [v for v in fake.vendors.values() if v["DisplayName"] == "WONUOLA SUPER STORE"][0]["Id"]
        self.assertEqual(e["3980"]["status"], "READY", e["3980"]["reasons"])
        self.assertEqual(e["3980"]["payload"]["VendorRef"]["value"], new_id)
        row = self.vendor_rows()["WONUOLA SUPER STORE"]
        self.assertEqual((row["QBO Vendor Id"], row["Approved By"]), (new_id, "auto:owner vendor yes (test)"))
        self.assertEqual(summary["vendor_actions"][0]["state"], vendor_ops.CREATED)
        res = self.f.post(out, fake, auto=True, review=False)
        self.assertEqual(res["counts"], {"POSTED": 1})
        bill = [b for b in fake.bills.values() if b["DocNumber"] == "EPOS-PO-3980"][0]
        self.assertEqual(bill["VendorRef"]["value"], new_id)
        self.assertEqual(bill["Balance"], bill["TotalAmt"])  # unpaid
        self.assertFalse([c for c in fake.calls if c[1].startswith("/billpayment")])

    def test_near_match_is_held_with_candidates(self):
        os.environ.update(VENDOR_ENV)
        fake = VendorFakeQBO()
        _, summary, e = self.scheduled_plan([order(3981, [WATER], supplier="NIGERIAN BOTLING COMPANY")], fake)
        self.assertEqual(fake.vendor_posts(), [])
        self.assertEqual(e["3981"]["status"], "HOLD")
        act = summary["vendor_actions"][0]
        self.assertEqual(act["state"], vendor_ops.HOLD_NEAR)
        self.assertGreaterEqual(act["best_score"], vendor_ops.NEW_VENDOR_MAX_SCORE)
        self.assertIn("NIGERIAN BOTTLING COMPANY", act["candidates"][0])
        self.assertTrue(any("HOLD_NEAR_MATCH" in r for r in e["3981"]["reasons"]))
        self.assertNotIn("NIGERIAN BOTLING COMPANY", self.vendor_rows())

    def test_cap_limits_new_vendors_per_run(self):
        os.environ.update({**VENDOR_ENV, vendor_ops.MAX_ENV: "1"})
        fake = VendorFakeQBO()
        _, summary, e = self.scheduled_plan([order(3982, [WATER], supplier="WONUOLA SUPER STORE"),
                                             order(3983, [WATER], supplier="ZENITH PLASTICS ABUJA")], fake)
        states = sorted(a["state"] for a in summary["vendor_actions"])
        self.assertEqual(states, [vendor_ops.CREATED, vendor_ops.HOLD_CAP])
        self.assertEqual(len(fake.vendor_posts()), 1)
        self.assertEqual(sorted(x["status"] for x in e.values()), ["HOLD", "READY"])

    def test_gate_off_holds_and_creates_nothing(self):
        fake = VendorFakeQBO()
        _, summary, e = self.scheduled_plan([order(3984, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(fake.vendor_posts(), [])
        self.assertEqual(e["3984"]["status"], "HOLD")
        self.assertEqual(summary["vendor_actions"][0]["state"], vendor_ops.HOLD_GATE)
        self.assertEqual(fake.methods(), {"GET"})

    def test_plan_mode_never_creates_even_with_gates_on(self):
        os.environ.update(VENDOR_ENV)
        fake = VendorFakeQBO()
        _, summary, e, _ = self.f.plan([order(3985, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(fake.vendor_posts(), [])
        self.assertEqual(summary["vendor_actions"][0]["state"], vendor_ops.CREATE)
        self.assertIn("plan only", summary["vendor_actions"][0]["detail"])
        self.assertEqual(e["3985"]["status"], "HOLD")

    def test_name_used_by_a_customer_is_held(self):
        os.environ.update(VENDOR_ENV)
        fake = VendorFakeQBO(customers=[{"Id": "77", "DisplayName": "WONUOLA SUPER STORE"}])
        _, summary, _ = self.scheduled_plan([order(3986, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(fake.vendor_posts(), [])
        self.assertEqual(summary["vendor_actions"][0]["state"], vendor_ops.HOLD_TAKEN)
        self.assertIn("Customer 77", summary["vendor_actions"][0]["detail"])

    def test_display_name_is_cleaned(self):
        self.assertEqual(vendor_ops.display_name("  BUNARICH  BREAD, "), "BUNARICH BREAD")
        self.assertEqual(vendor_ops.display_name("A:B\tC"), "A B C")


class PaymentHintTests(unittest.TestCase):
    def test_payment_mode_from_po_note_goes_into_private_note(self):
        f = Fixture()
        lst, det = order(3968, [WATER])
        det["Note"] = "SUPPLIER: NIGERIAN BOTTLING COMPANY,                      MODE OF PAYMENY: CASH"
        _, _, e, _ = f.plan([(lst, det)])
        self.assertEqual(e["3968"]["status"], "READY", e["3968"]["reasons"])
        self.assertEqual(e["3968"]["po"]["payment"], "CASH")
        self.assertTrue(e["3968"]["payload"]["PrivateNote"].startswith("Payment hint: CASH"))
        self.assertIn("UNPAID", e["3968"]["payload"]["PrivateNote"])

    def test_note_variants(self):
        cases = {
            "SUPPLIER:UNCLE SAMS BAKERY AND CAFE   MODE OF PAYMENT:TRANSFER": ("UNCLE SAMS BAKERY AND CAFE", "TRANSFER"),
            "SUUPPLIER: UNCLE SAM'S BAKERY AND CAFE,     MODE OF PAY MENT:TRANSFER": ("UNCLE SAM'S BAKERY AND CAFE", "TRANSFER"),
            "SUPPLIER: BUNARICH  BREAD,   MODE OF PAYMENY: CASH": ("BUNARICH BREAD", "CASH"),
            "SUPPLIER: XYZ": ("XYZ", ""),
            "": ("", ""),
        }
        for note, want in cases.items():
            self.assertEqual(bs.parse_po_note(note)[:2], want, note)

    def test_payment_mode_column_used_when_note_has_none(self):
        lst, det = order(3990, [WATER])
        det["Note"] = "SUPPLIER: NIGERIAN BOTTLING COMPANY"
        det["PaymentMode"] = "Transfer"
        self.assertEqual(bs.build_po(lst, det)["payment"], "TRANSFER")


if __name__ == "__main__":
    unittest.main()

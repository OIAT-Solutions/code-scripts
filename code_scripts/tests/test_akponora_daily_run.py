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
from code_scripts.akponora_ops import vendors as vendor_ops
from code_scripts.tests.test_bills_sync import WATER, FakeQBO, Fixture, Resp, client, order

STANDING = {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1",
            "OIAT_COMPANY_A_STANDING_APPROVAL_REF": "owner standing yes (test)"}


def _out(cmd):
    return Path(cmd[cmd.index("--out") + 1]) if "--out" in cmd else None


def step_of(cmd) -> str:
    joined = " ".join(cmd)
    for needle, name in (("catalogue_sync", "catalogue"), ("bills_sync", "bills"), ("run_pipeline.py", "sales"),
                         ("item_guard", "guard"), ("stock_snapshot", "stock")):
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
        elif name == "stock":
            if rc == 0:
                (out / "summary.json").write_text(json.dumps({
                    "summary": {"by_status": {"MATCH": 3812, "DIFFERENT": 41, "NEGATIVE_QBO": 11},
                                "different_likely_timing": 30, "unmapped_tracked": 0},
                    "summary_text": "Stock check: 3,812 match, 41 different, 11 negative in QuickBooks",
                    "latest": "/state/ops/company_a/stock_snapshot/latest.json"}))
            else:
                (out / "summary.json").write_text(json.dumps({"error": "SnapshotError: no catalogue"}))
        log_path.write_text("fake\n")
        return rc


class DailyRunTests(unittest.TestCase):
    def test_stock_movement_capture_opt_in_and_incomplete_is_review(self):
        with tempfile.TemporaryDirectory() as folder:
            for enabled, incomplete in ((False, False), (True, False), (True, True)):
                with self.subTest(enabled=enabled, incomplete=incomplete):
                    calls = []
                    def runner(cmd, *, log_path, env):
                        calls.append(cmd)
                        out = _out(cmd)
                        if "stock_snapshot" in " ".join(cmd):
                            report = {"sources": {"catalogue": {"path": "/fixture/catalogue"},
                                                   "mapping": {"path": "/fixture/mapping"}}}
                        else:
                            report = {"events": [{"event_id": "test"}],
                                      "errors": ["missing details"] if incomplete else []}
                            self.assertIn("--capture-live", cmd)
                            self.assertEqual(cmd[cmd.index("--from-date") + 1], "2026-10-04")
                            self.assertEqual(cmd[cmd.index("--through-date") + 1], "2026-10-06")
                        (out / "summary.json").write_text(json.dumps(report))
                        return 0
                    class Clock(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return datetime(2026, 10, 6, 18, tzinfo=ZoneInfo("Africa/Lagos"))
                    with mock.patch.object(dr, "_company_slack_env_key", return_value="SLACK_TEST"), mock.patch.object(dr, "datetime", Clock):
                        run = dr.DailyRun("2026-10-05", root=Path(folder), runner=runner,
                            env={"OIAT_COMPANY_A_STOCK_MOVEMENTS_ENABLED": "1" if enabled else "0"})
                        result = dr.StepResult(name="stock", out=str(run.step_dir("stock")))
                        run.step_stock(result)
                    self.assertEqual(len(calls), 2 if enabled else 1)
                    # an unreadable adjustment is a review item, not a failed run (7-8 Oct 2026)
                    self.assertEqual(result.status, dr.REVIEW if enabled else dr.OK)
                    self.assertEqual(result.exit_code, 0)
                    if incomplete:
                        self.assertTrue(any("could not read 1 adjustment" in r for r in result.review))

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
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard", "stock"])
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
        self.assertEqual(len(self.slack), 2)  # a start message, then one summary
        self.assertIn("daily run started", self.slack[0])
        self.assertTrue(self.slack[1].startswith(":large_yellow_circle: *Nora Mart · business day Fri 2 Oct* · ₦3,211,950 sales posted · "))

    def test_catalogue_crash_still_runs_bills_and_sales(self):
        runner = FakeRunner(raise_on={"catalogue"})
        summary = self.make(runner).execute()
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard", "stock"])
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
        self.assertTrue(dr.technical_text(summary).splitlines()[0].count("DRY RUN"))
        self.assertIn("practice run, nothing posted", dr.slack_text(summary).splitlines()[0])

    def test_dry_run_main_sends_no_slack_by_default(self):
        runner = FakeRunner()
        slack = []
        with mock.patch.object(dr, "_daily_root", lambda: self.root / "daily"), \
                mock.patch.object(dr, "acquire_lock", return_value=SimpleNamespace(release=lambda: None)):
            rc = dr.main(["--date", "2026-10-02", "--dry-run"], runner=runner, slack=slack.append)
        self.assertEqual(rc, 3)  # the fake bills plan has a HOLD: a dry run shows it as waiting
        self.assertEqual([c[0] for c in runner.calls], ["catalogue", "bills", "sales", "guard", "stock"])
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
        text = dr.technical_text(summary)
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

    def test_uf_step_runs_in_process_after_guard(self):
        # the step itself is covered in test_uf_deposits.DailyRunUFTests; here: ordering + isolation
        seen = []

        def fake_uf(_self, res):
            seen.append([r.name for r in _self.results])
            raise RuntimeError("sheet unreachable")

        with mock.patch.object(dr.DailyRun, "step_uf", fake_uf):
            summary = self.make(FakeRunner(), env={**STANDING, dr.UF_ENV: "1"}).execute()
        self.assertEqual(seen, [["catalogue", "bills", "sales", "credit", "guard", "stock"]])
        step = {s["name"]: s for s in summary["steps"]}["uf"]
        self.assertEqual(step["status"], dr.FAILED)
        self.assertIn("sheet unreachable", step["detail"])
        self.assertEqual({s["name"]: s["status"] for s in summary["steps"]}["sales"], dr.OK)

    def test_stock_step_runs_after_guard_and_reports_in_slack(self):
        runner = FakeRunner()
        summary = self.make(runner).execute()
        cmds = {name: cmd for name, cmd in runner.calls}
        self.assertEqual(cmds["stock"][1:5], ["-m", "code_scripts.akponora_ops.stock_snapshot", "run", "--out"])
        self.assertEqual(_out(cmds["stock"]).name, "stock")
        step = {s["name"]: s for s in summary["steps"]}["stock"]
        self.assertEqual(step["status"], dr.OK)
        self.assertEqual(step["counts"]["by_status"]["DIFFERENT"], 41)
        self.assertEqual(summary["exit_code"], 0)
        self.assertIn("*stock* [ok] Stock check: 3,812 match, 41 different, 11 negative in QuickBooks",
                      dr.technical_text(summary))
        self.assertIn("*Checks:* all ran\n• 2 new products added\n• 11 items with negative stock in QuickBooks\n",
                      self.slack[-1])

    def test_stock_step_is_read_only_in_dry_run_too(self):
        runner = FakeRunner()
        self.make(runner, dry=True).execute()
        cmds = {name: cmd for name, cmd in runner.calls}
        self.assertEqual(cmds["stock"][3:5], ["run", "--out"])

    def test_stock_failure_is_isolated(self):
        runner = FakeRunner(codes={"stock": 2})
        summary = self.make(runner, env={**STANDING, dr.UF_ENV: "1"})
        with mock.patch.object(dr.DailyRun, "step_uf", lambda _self, res: setattr(res, "status", dr.OK)):
            summary = summary.execute()
        steps = {s["name"]: s for s in summary["steps"]}
        self.assertEqual(steps["stock"]["status"], dr.FAILED)
        self.assertIn("no catalogue", steps["stock"]["detail"])
        self.assertEqual(steps["uf"]["status"], dr.OK)  # later step still ran
        self.assertEqual(steps["sales"]["status"], dr.OK)
        crashed = self.make(FakeRunner(raise_on={"stock"})).execute()
        self.assertEqual({s["name"]: s["status"] for s in crashed["steps"]}["stock"], dr.FAILED)

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
        self.assertIn("didn't start", slack[0])

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

    def test_same_name_bar_capitals_and_spacing_links_automatically(self):
        """5 Oct 2026: EPOS 'MEGA FROZEN FOODS' = QBO 'Mega frozen Foods' (score 1.00) was held."""
        os.environ.update(VENDOR_ENV)
        from code_scripts.tests.test_bills_sync import VENDORS

        fake = VendorFakeQBO(vendors={**VENDORS, "12": {"Id": "12", "DisplayName": "Mega frozen Foods", "Active": True}})
        _, summary, e = self.scheduled_plan([order(3984, [WATER], supplier="MEGA FROZEN  FOODS.")], fake)
        self.assertEqual(fake.vendor_posts(), [])  # nothing created in QBO
        act = summary["vendor_actions"][0]
        self.assertEqual(act["state"], vendor_ops.LINKED)
        self.assertEqual(e["3984"]["status"], "READY", e["3984"]["reasons"])
        row = next(r for r in self.vendor_rows().values() if r["QBO Vendor Id"] == "12")
        self.assertEqual((row["QBO Vendor Id"], act["vendor_id"]), ("12", "12"))
        self.assertEqual(e["3984"]["payload"]["VendorRef"]["value"], "12")
        self.assertTrue(row["Approved By"].startswith("auto-link"))
        self.assertTrue(vendor_ops.same_name("MEGA FROZEN FOODS", "Mega frozen  Foods"))
        self.assertFalse(vendor_ops.same_name("MEGA FROZEN FOODS", "MEGA FROZEN FOOD"))  # a letter differs: still held

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


class SlackMessageTests(unittest.TestCase):
    """The plain-English Slack messages (start + summary), shaped on the first live run (3 Oct 2026)."""

    def summary(self, **over):
        sheet_wait = [{"day": d, "status": "WAITING_SHEET",
                       "reason": "CASH (System 1) box is blank (type 0 if there was no cash)"}
                      for d in ("2026-09-25", "2026-09-26", "2026-09-29", "2026-10-01")]
        steps = [
            {"name": "catalogue", "status": dr.OK, "counts": {"new_products": 0, "items_created": 0, "mapping_only": 0,
                                                              "review": 0, "hold": 0}},
            {"name": "bills", "status": dr.REVIEW, "counts": {
                "pos": 9, "ready": 8, "ready_total": "217350.00", "posted": 7, "posted_total": "190350.00", "hold": 1,
                "hold_total_inc": "20000.00", "capped": 0, "vendors_created": 0,
                "waiting_items": [
                    {"po": "3969", "supplier": "UNCLE'S SAM BAKERY AND CAFE", "total": "27000.00", "status": "READY",
                     "why": "possible duplicate receipt of EPOS PO 3967 (2026-10-01, ...): same products/qty"},
                    {"po": "3970", "supplier": "FLOURISH COOL WATER, ALPINE FRESH WATER", "total": "20000.00",
                     "status": "HOLD", "why": "supplier not approved in vendors.csv", "vendor_hint": "FLOURISH"}]}},
            {"name": "sales", "status": dr.OK, "counts": {"mode": "post", "uploaded": 6, "skipped": 0, "failed": 0,
                                                          "reconcile_status": "MATCH", "epos_total": 4857550.0,
                                                          "qbo_total": 4857550.0}},
            {"name": "guard", "status": dr.REVIEW, "counts": {"alert": 437, "warn": 26}},
            {"name": "stock", "status": dr.OK, "counts": {"by_status": {"MATCH": 3458, "DIFFERENT": 449, "NEGATIVE_QBO": 19},
                                                          "likely_timing": 202}},
            {"name": "uf", "status": dr.FAILED, "detail": "post stopped: 2026-09-27: deposit 80514 failed verification",
             "counts": {"mode": "auto-post", "uf_balance": "33912599.99", "deposited": [],
                        "ready": [{"day": "2026-09-28", "total": "3113300.00"}, {"day": "2026-09-30", "total": "3965950.00"}],
                        "held": sheet_wait + [{"day": "2026-09-27", "status": "HELD",
                                               "reason": "post stopped: deposit 80514 failed verification"},
                                              {"day": "2026-10-02", "status": "WAITING_SHEET",
                                               "reason": "CASH (System 1) box is blank (type 0 if there was no cash)"}]}},
        ]
        base = {"business_date": "2026-10-02", "dry_run": False, "exit_code": 2, "steps": steps,
                "started_at": "2026-10-03T17:00:00+00:00", "finished_at": "2026-10-03T17:25:59+00:00",
                "previous_guard_alert": 437,
                "links": dr.portal_links({"PORTAL_DOMAIN": "portal.example.com"}, "2026-10-02", "run_170000Z")}
        base.update(over)
        return base

    INBOX = "<https://portal.example.com/epos-qbo/attention/|Inbox>"
    RUN = "<https://portal.example.com/epos-qbo/company-a/daily-runs/2026-10-02/run_170000Z/|Open run>"

    def test_first_live_run_reads_clearly(self):
        lines = dr.slack_text(self.summary()).splitlines()
        self.assertEqual(lines, [
            ":red_circle: *Nora Mart · business day Fri 2 Oct* · banking stopped · ₦4,857,550 sales posted · 4 to-dos",
            "",
            "*Sales:* ₦4,857,550 posted (6 receipts) · matches EPOS",
            "*Bills:* ₦190,350 posted (7 bills)",
            "• 2 waiting for you",
            "*Banking:* stopped part-way on 27 Sep · 28, 30 Sep go on the next run",
            "• ₦33,912,600 still in Undeposited Funds",
            "*Checks:* all ran",
            "• 19 items with negative stock in QuickBooks",
            "",
            "*To do*",
            f":bust_in_silhouette: *You* · PO 3969 (UNCLE'S SAM BAKERY AND CAFE, ₦27,000) looks like PO 3967: approve if real → {self.INBOX}",
            f":bust_in_silhouette: *You* · PO 3970 (FLOURISH COOL WATER, ALPINE FRESH WATER, ₦20,000): link supplier to FLOURISH or create it → {self.INBOX}",
            ":convenience_store: *Store* · complete the till sales breakdown for 25, 26, 29 Sep and 1, 2 Oct → "
            "<https://docs.google.com/spreadsheets/d/15lvfx6q-g7JYgzY4kQZC87JKK2za8SRvuUXjqhovd3A|Till sheet>",
            f":hammer_and_wrench: *OIAT* · banking stopped part-way on 27 Sep; nothing posts twice → {self.RUN}",
            "",
            f"Finished 18:25 · {self.RUN}",
        ])

    def test_a_clean_day_is_short(self):
        s = self.summary()
        s["steps"][1] = {"name": "bills", "status": dr.OK, "counts": {"posted": 3, "posted_total": "61000.00",
                                                                        "waiting_items": []}}
        s["steps"][5] = {"name": "uf", "status": dr.OK, "counts": {
            "uf_balance": "29055050.00", "deposited": [{"day": "2026-10-02", "total": "4857550.00"}], "ready": [], "held": []}}
        s["steps"][4]["counts"]["by_status"]["NEGATIVE_QBO"] = 0
        text = dr.slack_text(s)
        self.assertEqual(text.splitlines(), [
            ":large_green_circle: *Nora Mart · business day Fri 2 Oct* · ₦4,857,550 sales posted · all done", "",
            "*Sales:* ₦4,857,550 posted (6 receipts) · matches EPOS",
            "*Bills:* ₦61,000 posted (3 bills)",
            "*Banking:* ₦4,857,550 banked for 2 Oct",
            "• ₦29,055,050 still in Undeposited Funds",
            "*Checks:* all ran, nothing new", "",
            f"Finished 18:25 · {self.RUN}"])

    def test_4_oct_bills_are_grouped_and_stock_shows_its_trend(self):
        """4 Oct 2026: the Bills line was a run-on of counts and naira, 'supplier(s)', and the negative-stock
        number had no direction."""
        s = self.summary(business_date="2026-10-04", previous_stock_negative=26)
        s["steps"][1] = {"name": "bills", "status": dr.REVIEW, "counts": {
            "posted": 5, "posted_total": "2190750.00", "cash_paid": 10, "cash_paid_total": "167250.00",
            "routine_repeats": 2, "vendors_linked": 1,
            "waiting_items": [{"po": "3976", "supplier": "UNCLE SAMS BAKERY AND CAFE", "total": "72000.00",
                               "status": "HOLD", "why": "possible duplicate receipt of EPOS PO 3862 (...)"}]}}
        s["steps"][4]["counts"]["by_status"]["NEGATIVE_QBO"] = 29
        s["steps"][5] = {"name": "uf", "status": dr.REVIEW, "counts": {
            "uf_balance": "8834975.00", "ready": [],
            "deposited": [{"day": "2026-10-01", "total": "4000000"}, {"day": "2026-10-02", "total": "5000000"},
                          {"day": "2026-10-03", "total": "5479325"}],
            "held": [{"day": "2026-10-04", "status": "WAITING_SHEET", "reason": "CASH (System 1) box is blank"},
                     {"day": "2026-09-29", "status": "HELD", "reason": "sheet total differs from receipts (tolerance)"}]}}
        lines = dr.slack_text(s).splitlines()
        self.assertEqual(lines[0], ":large_yellow_circle: *Nora Mart · business day Sun 4 Oct* · ₦4,857,550 sales posted · 3 to-dos")
        self.assertEqual(lines[3:10], [
            "*Bills:* ₦2,190,750 posted (5 bills)",
            "• ₦167,250 paid in cash (10 bills)",
            "• 1 waiting for you · 2 routine repeat orders let through · 1 supplier linked by name",
            "*Banking:* ₦14,479,325 banked for 1, 2, 3 Oct",
            "• ₦8,834,975 still in Undeposited Funds",
            "*Checks:* all ran",
            "• 29 items with negative stock in QuickBooks (3 more than last run)"])
        self.assertIn(f":bust_in_silhouette: *You* · PO 3976 (UNCLE SAMS BAKERY AND CAFE, ₦72,000) looks like PO 3862: approve if real → {self.INBOX}", lines)
        self.assertIn(f":hammer_and_wrench: *OIAT* · banking for 29 Sep on hold: till sheet and sales don't agree → {self.RUN}", lines)
        self.assertNotIn("(s)", "\n".join(lines))
        s["previous_stock_negative"] = 29
        self.assertIn("• 29 items with negative stock in QuickBooks (same as last run)", dr.slack_text(s))
        s["previous_stock_negative"] = 31
        self.assertIn("(2 fewer than last run)", dr.slack_text(s))

    def test_new_item_alerts_and_approval_days(self):
        s = self.summary(previous_guard_alert=430)
        s["steps"][5]["status"] = dr.REVIEW
        s["steps"][5]["counts"]["held"] = []
        text = dr.slack_text(s)
        self.assertIn("*Checks:* all ran\n• 7 new item alerts\n• 19 items with negative stock in QuickBooks", text)
        self.assertIn(f":hammer_and_wrench: *OIAT* · look at 7 new item alerts → {self.RUN}", text)
        self.assertIn("*Banking:* ₦7,079,250 for 28, 30 Sep waiting for you", text)
        self.assertIn(f"*You* · approve banking ₦7,079,250 for 28, 30 Sep → {self.INBOX}", text)
        self.assertTrue(text.startswith(":large_yellow_circle: *Nora Mart · business day Fri 2 Oct* · ₦4,857,550 sales posted · 4 to-dos"))

    def test_failed_sales_is_red_and_has_no_error_text(self):
        s = self.summary()
        s["steps"][2] = {"name": "sales", "status": dr.FAILED, "detail": "run_pipeline exited 1; see /data/x/log.txt",
                         "counts": {}}
        text = dr.slack_text(s)
        self.assertTrue(text.startswith(":red_circle: *Nora Mart · business day Fri 2 Oct* · sales didn't post · 5 to-dos"))
        self.assertIn(":hammer_and_wrench: *OIAT* · sales didn't post", text)
        self.assertIn("*Sales:* didn't post", text)
        self.assertNotIn("/data/", text)
        self.assertNotIn("exited", text)
        self.assertNotIn("DocNumber", dr.slack_text(self.summary()))

    def test_days_not_banked_for_a_week_are_escalated(self):
        s = self.summary()
        text = dr.slack_text(s)
        self.assertNotIn("not banked after", text)  # 25 Sep is exactly 7 days before 2 Oct: still quiet
        text = dr.slack_text(self.summary(business_date="2026-10-03"))
        self.assertIn(":hammer_and_wrench: *OIAT* · 25 Sep not banked after 8 days: follow up with the store", text)
        text = dr.slack_text(self.summary(business_date="2026-10-05", uf_aged_days=7))
        self.assertIn("*OIAT* · 3 days not banked after more than 7 days (25, 26, 27 Sep; oldest 10 days): follow up", text)

    def test_start_message(self):
        text = dr.start_text("2026-10-02", banking_on=True,
                             links=dr.portal_links({"PORTAL_DOMAIN": "portal.example.com"}, "2026-10-02", "run_x"))
        self.assertEqual(text, ":arrow_forward: *Nora Mart · business day Fri 2 Oct* · daily run started (sales, bills, banking, "
                               "checks) · summary to follow · <https://portal.example.com/epos-qbo/company-a/daily-runs/|Daily runs>")

    def test_bills_waiting_reads_the_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "review.csv").write_text(
                "PO,EPOS Supplier,EPOS Total Inc,Bill Total,Status,Reasons,Warnings\n"
                "3967,UNCLE SAM'S BAKERY AND CAFE,27000.00,27000.00,READY,,\n"
                "3969,UNCLE'S SAM BAKERY AND CAFE,27000.00,27000.00,READY,,possible duplicate receipt of EPOS PO 3967\n"
                "3970,FLOURISH COOL WATER,20000.00,20000.00,HOLD,supplier not approved,\n")
            (out / "results.csv").write_text("PO,status,Total\n3967,POSTED,27000.0\n")
            got = dr.bills_waiting(out, {}, [{"display_name": "FLOURISH COOL WATER", "state": "HOLD_NEAR_MATCH",
                                             "detail": "looks like an existing QBO vendor (best 0.90): 64 FLOURISH (0.90)"}])
        self.assertEqual(got["posted_total"], "27000.0")
        self.assertEqual([w["po"] for w in got["waiting_items"]], ["3969", "3970"])
        self.assertEqual(got["waiting_items"][1]["vendor_hint"], "FLOURISH")

    def test_previous_guard_alert_reads_the_last_real_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, run, alert in (("2026-09-30", "run_170000Z", 420), ("2026-10-01", "run_170000Z", 430),
                                    ("2026-10-01", "run_180000Z_dry", 999)):
                (root / day / run).mkdir(parents=True)
                (root / day / run / "summary.json").write_text(json.dumps(
                    {"steps": [{"name": "guard", "status": dr.REVIEW, "counts": {"alert": alert}}]}))
            self.assertEqual(dr.previous_guard_alert(root, "2026-10-02"), 430)
            self.assertIsNone(dr.previous_guard_alert(root, "2026-09-30"))

    def test_previous_stock_negative_reads_the_last_real_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, run, neg in (("2026-10-01", "run_170000Z", 26), ("2026-10-02", "run_180000Z_dry", 99)):
                (root / day / run).mkdir(parents=True)
                (root / day / run / "summary.json").write_text(json.dumps(
                    {"steps": [{"name": "stock", "status": dr.OK, "counts": {"by_status": {"NEGATIVE_QBO": neg}}}]}))
            self.assertEqual(dr.previous_stock_negative(root, "2026-10-03"), 26)
            self.assertIsNone(dr.previous_stock_negative(root, "2026-10-01"))
            self.assertIsNone(dr.previous_stock_negative(root / "missing", "2026-10-03"))

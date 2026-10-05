"""Company A daily-run visibility (phase 1, read-only): service parsing + portal pages."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord
from apps.epos_qbo.services import company_a_ops as ops
from apps.epos_qbo.tests.utils import suppress_expected_request_logs


def _step(name, status="ok", counts=None, review=None, detail="", exit_code=0):
    return {"name": name, "status": status, "exit_code": exit_code, "detail": detail, "counts": counts or {},
            "review": review or [], "out": f"/data/ops/company_a/daily/x/{name}",
            "started_at": "2026-10-02T17:00:00+00:00", "finished_at": "2026-10-02T17:05:00+00:00"}


def _summary(day, *, status="clean", exit_code=0, dry=False, steps=None, waiting=None):
    return {"tool": "daily_run", "company": "company_a", "business_date": day, "dry_run": dry,
            "exit_code": exit_code, "status": status, "run_dir": f"/data/ops/company_a/daily/{day}/run_x",
            "finished_at": "2026-10-02T17:06:00+00:00", "steps": steps if steps is not None else [],
            "waiting_for_review": waiting or []}


OK_STEPS = [
    _step("catalogue", counts={"new_products": 2, "auto": 2, "review": 0, "hold": 0, "items_created": 2,
                               "items_adopted": 0, "mapping_only": 0, "mapping_installed": True}),
    _step("bills", counts={"window": ["2026-10-01", "2026-10-02"], "pos": 3, "ready": 3, "ready_total": "150000.00",
                           "hold": 0, "hold_total_inc": "0.00", "already_posted": 0, "posted": 3, "adopted": 0,
                           "capped": 0, "held_live": 0, "vendors_created": 1, "vendors_held": 0,
                           "vendors": ["CREATED: New Supplier -> QBO 77"]}),
    _step("sales", counts={"mode": "post", "uploaded": 40, "skipped": 0, "failed": 0, "reconcile_status": "MATCH",
                           "epos_total": 812345.5, "qbo_total": 812345.5, "metadata": "/x"}),
    _step("guard", counts={"alert": 0, "warn": 1}),
    _step("uf", status="disabled", detail="off (OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1 to report)"),
]


class CompanyAOpsFixtureMixin:
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="oiat_company_a_ops_", dir=os.getenv("TMPDIR") or None)).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch("code_scripts.paths.STATE_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (ops.UF_ENV,):
            os.environ.pop(key, None)

    def make_run(self, day, run_id, summary=None, *, raw=None, files=None):
        run_dir = self.tmp / "ops" / "company_a" / "daily" / day / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (run_dir / "summary.json").write_text(raw, encoding="utf-8")
        elif summary is not None:
            (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        for rel, text in (files or {}).items():
            path = run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return run_dir

    def make_hold(self, **extra):
        doc = {"at": "2026-10-02T18:10:00+00:00", "business_date": "2026-10-02", "source": "reconciliation",
               "result": {"status": "MISMATCH", "detail": "EPOS 100 vs QBO 90"},
               "action": "PAUSE; reconcile QBO and source evidence; human approval before clearing hold"}
        doc.update(extra)
        (self.tmp / "company_a_posting_hold.json").write_text(json.dumps(doc), encoding="utf-8")


ALERTS_CSV = (
    "severity,check,entity,id,detail\n"
    "ALERT,akp_mapping_mismatch,Item,1,x\n"
    "ALERT,akp_mapping_mismatch,Item,2,y\n"
    "WARN,duplicate_name,Item,3,<script>alert(1)</script>\n"
)


class CompanyAOpsServiceTests(CompanyAOpsFixtureMixin, TestCase):
    def test_parses_ok_review_failed_dry_runs_newest_first(self):
        self.make_run("2026-10-01", "run_170000Z", _summary("2026-10-01", steps=OK_STEPS))
        self.make_run("2026-10-02", "run_090000Z_dry", _summary("2026-10-02", dry=True, steps=OK_STEPS))
        self.make_run("2026-10-02", "run_170000Z", _summary(
            "2026-10-02", status="review", exit_code=3,
            steps=[_step("bills", "review", {"posted": 0, "hold": 2, "hold_total_inc": "5000"}, ["bill HOLD PO 9"])],
            waiting=["bill HOLD PO 9", "bills: 2 bill(s) wait"]))
        self.make_run("2026-09-30", "run_170000Z", _summary("2026-09-30", status="failed", exit_code=2,
                                                            steps=[_step("sales", "failed", detail="boom", exit_code=1)]))
        runs = ops.list_runs()
        self.assertEqual([(r.business_date, r.run_id) for r in runs], [
            ("2026-10-02", "run_170000Z"), ("2026-10-02", "run_090000Z_dry"),
            ("2026-10-01", "run_170000Z"), ("2026-09-30", "run_170000Z")])
        self.assertEqual([r.status for r in runs], ["review", "ok", "ok", "failed"])
        self.assertEqual([r.dry_run for r in runs], [False, True, False, False])
        self.assertEqual(len(runs[0].waiting), 2)
        ok = runs[2]
        self.assertEqual(ok.totals["sales_posted"], "₦812,345.50")
        self.assertEqual(ok.totals["bills_posted"], 3)
        self.assertEqual(ok.totals["bills_ready_total"], "₦150,000.00")
        self.assertEqual(ok.totals["items_created"], 2)
        self.assertEqual(ok.totals["vendors_created"], 1)
        self.assertEqual(ok.step("uf").status, "disabled")
        self.assertEqual(ok.step("guard").label, "Health check")
        self.assertEqual(ops.latest_run(real_only=True).run_id, "run_170000Z")
        # dry sales never count as posted
        self.assertEqual(runs[1].totals["sales_posted"], "—")
        self.assertEqual(ops.list_runs(include_dry=False)[1].business_date, "2026-10-01")

    def test_malformed_and_missing_summaries_never_raise(self):
        self.make_run("2026-10-02", "run_100000Z", raw="{not json")
        self.make_run("2026-10-02", "run_110000Z", raw="[1, 2]")
        self.make_run("2026-10-02", "run_120000Z")  # no summary.json (in progress / crashed)
        self.make_run("2026-10-02", "run_130000Z", {"status": "weird", "exit_code": "x", "steps": [
            "not a dict", {"no": "name"}, {"name": "new_step", "status": "ok", "counts": {"a": [1, 2], "b": {"c": 1}}},
            {"name": "bills", "status": "review", "counts": "nope", "review": "nope"},
            {"name": "sales", "status": "ok", "counts": {"mode": "post", "qbo_total": "not-a-number"}},
        ], "waiting_for_review": "nope", "finished_at": "garbage"})
        (self.tmp / "ops" / "company_a" / "daily" / "not-a-date").mkdir(parents=True)
        (self.tmp / "ops" / "company_a" / "daily" / "2026-10-02" / "evil..dir").mkdir()
        runs = {r.run_id: r for r in ops.list_runs()}
        self.assertEqual(set(runs), {"run_100000Z", "run_110000Z", "run_120000Z", "run_130000Z"})
        self.assertEqual(runs["run_100000Z"].status, "unreadable")
        self.assertEqual(runs["run_110000Z"].status, "unreadable")
        self.assertEqual(runs["run_120000Z"].status, "incomplete")
        weird = runs["run_130000Z"]
        self.assertEqual(weird.status, "unknown")
        self.assertEqual([s.name for s in weird.steps], ["new_step", "bills", "sales"])
        self.assertEqual(weird.steps[0].label, "New Step")
        self.assertIn(("a", "[1, 2]"), weird.steps[0].extra_counts)
        self.assertEqual(weird.waiting, [])
        self.assertEqual(weird.totals["sales_posted"], "—")

    def test_no_evidence_root(self):
        self.assertEqual(ops.list_runs(), [])
        self.assertIsNone(ops.latest_run())
        self.assertIsNone(ops.guard_alerts())
        self.assertFalse(ops.posting_hold()["active"])

    def test_schedule_info_comes_from_the_daily_routine_schedule_row(self):
        from apps.epos_qbo.services import workflows

        info = ops.schedule_info()
        self.assertFalse(info["enabled"])
        self.assertIsNone(info["next_run"])
        sched, _ = workflows.ensure_daily_routine_schedule(now=datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("Africa/Lagos")))
        sched.enabled = True
        sched.save()
        info = ops.schedule_info()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["cron"], "0 18 * * *")
        self.assertEqual(info["time_label"], "Daily at 18:00")
        self.assertEqual(info["timezone"], "Africa/Lagos")
        self.assertEqual(info["next_run"], datetime(2026, 10, 3, 18, 0, tzinfo=ZoneInfo("Africa/Lagos")))
        sched.cron_expr = "nonsense"
        sched.save()
        self.assertFalse(ops.schedule_info()["cron_valid"])

    def test_posting_hold_and_last_cleared(self):
        self.assertFalse(ops.posting_hold()["active"])
        self.make_hold()
        (self.tmp / "company_a_posting_hold.cleared-20261001T000000000000Z.json").write_text(json.dumps(
            {"cleared": {"at": "2026-10-01T09:00:00+00:00", "approved_by": "Marvin", "reason": "reconciled"}}))
        hold = ops.posting_hold()
        self.assertTrue(hold["active"])
        self.assertEqual(hold["business_date"], "2026-10-02")
        self.assertIn("MISMATCH", hold["reason"])
        self.assertEqual(hold["last_cleared"]["approved_by"], "Marvin")
        (self.tmp / "company_a_posting_hold.json").write_text("garbage")
        self.assertTrue(ops.posting_hold()["active"])

    def test_guard_alerts_summary_by_check(self):
        self.make_run("2026-10-01", "run_170000Z", _summary("2026-10-01", steps=OK_STEPS),
                      files={"guard/alerts.csv": "severity,check\n"})
        self.make_run("2026-10-02", "run_170000Z", _summary(
            "2026-10-02", status="review", exit_code=3,
            steps=[_step("guard", "review", {"alert": 2, "warn": 1}, exit_code=4)]),
            files={"guard/alerts.csv": ALERTS_CSV, "guard/report.json": "{}"})
        self.make_run("2026-10-03", "run_170000Z", _summary("2026-10-03", steps=[]))  # no guard folder
        guard = ops.guard_alerts()
        self.assertEqual(guard["run"].business_date, "2026-10-02")
        self.assertEqual(guard["alert"], 2)
        self.assertEqual(guard["by_check"][0], {"severity": "ALERT", "check": "akp_mapping_mismatch", "count": 2})
        self.assertEqual(guard["csv_path"], "guard/alerts.csv")

    def test_uf_info_prefers_deposits_step(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=[
            _step("uf", counts={"balance": 12345.5, "last_deposit_date": "2026-09-30", "days_since_last_deposit": 2}),
        ]))
        uf = ops.uf_info(ops.latest_run())
        self.assertEqual(uf["step"].name, "uf")
        self.assertIn(("Undeposited Funds balance", "₦12,345.50"), uf["step"].key_facts)


class CompanyAEvidenceSafetyTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS), files={
            "bills/review.csv": "PO,Approve\n1,\n", "bills/log.txt": "\n".join(f"line {i}" for i in range(200)),
            "bills/payload.bin": "x"})
        self.make_run("2026-10-01", "run_170000Z", _summary("2026-10-01"), files={"bills/review.csv": "other\n"})
        (self.tmp / "secret.txt").write_text("TOP SECRET")
        (self.tmp / "ops" / "company_a" / "outside.json").write_text("{}")
        self.run = ops.get_run("2026-10-02", "run_170000Z")

    def test_resolves_files_inside_run(self):
        path = ops.resolve_evidence(self.run, "bills/review.csv")
        self.assertEqual(path, (self.run_dir / "bills" / "review.csv").resolve())
        content = ops.read_evidence(path)
        self.assertEqual(content["kind"], "csv")
        self.assertEqual(content["header"], ["PO", "Approve"])
        tail = ops.log_tail(self.run, "bills")
        self.assertTrue(tail.endswith("line 199"))
        self.assertNotIn("line 100\n", tail)
        self.assertEqual(ops.log_tail(self.run, "../.."), "")
        self.assertIn("bills/review.csv", [f["path"] for f in ops.evidence_files(self.run)])
        self.assertNotIn("bills/payload.bin", [f["path"] for f in ops.evidence_files(self.run)])

    def test_refuses_traversal_absolute_symlink_and_types(self):
        (self.run_dir / "link.txt").symlink_to(self.tmp / "secret.txt")
        (self.run_dir / "linkdir").symlink_to(self.tmp)
        bad = ["../../2026-10-01/run_170000Z/bills/review.csv", "../../../../../secret.txt", "/etc/passwd",
               str(self.tmp / "secret.txt"), "bills/../../../outside.json", "link.txt", "linkdir/secret.txt",
               "bills/payload.bin", "bills", "", "nope.csv", "C:\\x.csv", "bills/review.csv\x00.txt"]
        for rel in bad:
            with self.subTest(rel=rel), self.assertRaises(ops.EvidenceError):
                ops.resolve_evidence(self.run, rel)
        self.assertNotIn("link.txt", [f["path"] for f in ops.evidence_files(self.run)])

    def test_get_run_rejects_bad_ids(self):
        for day, run_id in [("../x", "run_170000Z"), ("2026-10-02", "../run_170000Z"), ("2026-10-02", "run_.."),
                            ("2026-10-02", "summary.json"), ("2026-10-09", "run_170000Z")]:
            with self.subTest(day=day, run_id=run_id):
                self.assertIsNone(ops.get_run(day, run_id))


class CompanyAPortalPageTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="viewer", password="pw12345")
        self.client.login(username="viewer", password="pw12345")
        self.make_run("2026-10-02", "run_170000Z", _summary(
            "2026-10-02", status="review", exit_code=3, steps=OK_STEPS[:2] + [
                _step("sales", "review", {"mode": "dry-run"}, ["sales 2026-10-02: review the dry-run evidence"],
                      detail="standing auto-approval is off"),
                _step("guard", "review", {"alert": 2, "warn": 1}, exit_code=4)] + OK_STEPS[4:],
            waiting=["sales 2026-10-02: review the dry-run evidence", "item guard: 2 ALERT(s)"]),
            files={"guard/alerts.csv": ALERTS_CSV, "sales/log.txt": "$ python run_pipeline.py\n<b>done</b>\n"})
        self.make_run("2026-10-01", "run_170000Z", _summary("2026-10-01", steps=OK_STEPS))
        self.make_run("2026-10-02", "run_080000Z_dry", _summary("2026-10-02", dry=True, steps=OK_STEPS))

    def test_requires_login(self):
        self.client.logout()
        for url in (reverse("epos_qbo:company-a-runs"),
                    reverse("epos_qbo:company-a-run-detail", args=["2026-10-02", "run_170000Z"]),
                    reverse("epos_qbo:company-a-evidence", args=["2026-10-02", "run_170000Z"]) + "?path=guard/alerts.csv"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("login", response["Location"])

    def test_runs_list_page(self):
        response = self.client.get(reverse("epos_qbo:company-a-runs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company A daily runs")
        self.assertContains(response, "run_080000Z_dry")
        self.assertContains(response, "Dry run")
        self.assertContains(response, "₦812,345.50")
        self.assertContains(response, "Holds &amp; alerts")
        self.assertContains(response, "akp_mapping_mismatch")
        self.assertContains(response, "No posting hold")
        response = self.client.get(reverse("epos_qbo:company-a-runs") + "?dry=0")
        self.assertNotContains(response, "run_080000Z_dry")

    def test_run_detail_page(self):
        response = self.client.get(reverse("epos_qbo:company-a-run-detail", args=["2026-10-02", "run_170000Z"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recorded follow-ups · 2")
        self.assertContains(response, "standing auto-approval is off")
        self.assertContains(response, "Bills posted")
        self.assertContains(response, "&lt;b&gt;done&lt;/b&gt;")  # log tail escaped
        self.assertNotContains(response, "<b>done</b>")
        self.assertContains(response, "guard/alerts.csv")
        with suppress_expected_request_logs():
            self.assertEqual(self.client.get(reverse("epos_qbo:company-a-run-detail",
                                                     args=["2026-10-09", "run_170000Z"])).status_code, 404)
            self.assertEqual(self.client.get(reverse("epos_qbo:company-a-run-detail",
                                                     args=["2026-10-02", "run_..x"])).status_code, 404)

    def test_evidence_viewer_renders_csv_escaped(self):
        url = reverse("epos_qbo:company-a-evidence", args=["2026-10-02", "run_170000Z"])
        response = self.client.get(url, {"path": "guard/alerts.csv"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "akp_mapping_mismatch")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_evidence_viewer_refuses_traversal(self):
        (self.tmp / "secret.txt").write_text("TOP SECRET")
        url = reverse("epos_qbo:company-a-evidence", args=["2026-10-02", "run_170000Z"])
        with suppress_expected_request_logs(extra_loggers=["apps.epos_qbo.views_company_a"]):
            for rel in ("../../../../secret.txt", "/etc/passwd", "../run_080000Z_dry/summary.json", ""):
                with self.subTest(rel=rel):
                    response = self.client.get(url, {"path": rel})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotContains(response, "TOP SECRET", status_code=404)

    def test_views_are_get_only(self):
        with suppress_expected_request_logs():
            response = self.client.post(reverse("epos_qbo:company-a-runs"))
        self.assertEqual(response.status_code, 405)


class CompanyAPortalIntegrationTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="operator", password="pw12345")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_schedules"))
        self.client.login(username="operator", password="pw12345")
        CompanyConfigRecord.objects.create(
            company_key="company_a", display_name="AKPONORA VENTURES LTD.",
            config_json={"company_key": "company_a", "display_name": "AKPONORA VENTURES LTD.",
                         "qbo": {"realm_id": "123"},
                         "epos": {"username_env_key": "EPOS_USERNAME_A", "password_env_key": "EPOS_PASSWORD_A"}})

    def _daily_routine(self, enabled):
        from apps.epos_qbo.services import workflows

        sched, _ = workflows.ensure_daily_routine_schedule()
        sched.enabled = enabled
        sched.save()
        return sched

    def test_schedules_row_enabled(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS))
        self._daily_routine(True)
        response = self.client.get(reverse("epos_qbo:schedules"))
        self.assertEqual(response.status_code, 200)
        rows = response.context["schedule_display_rows"]
        routine = next(r for r in rows if r["name"] == "Daily routine")
        self.assertEqual(routine["state"], "active")
        self.assertEqual(routine["business_date"], "2026-10-02")
        self.assertIsNotNone(routine["last_run"])
        self.assertEqual(sum(1 for r in rows if r["name"] == "Daily routine"), 1)
        self.assertEqual(response.content.decode().count('<h1'), 1)

    def test_schedules_row_paused(self):
        self._daily_routine(False)
        response = self.client.get(reverse("epos_qbo:schedules"), {"state": "paused"})
        routine = next(r for r in response.context["schedule_display_rows"] if r["name"] == "Daily routine")
        self.assertEqual(routine["state"], "paused")
        self.assertIsNone(routine["next_run"])
        self.assertIsNone(routine["last_run"])
        response = self.client.get(reverse("epos_qbo:schedules"), {"state": "history"})
        self.assertFalse(any(r["name"] == "Daily routine" for r in response.context["schedule_display_rows"]))

    def test_overview_card_without_hold(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS))
        # Pin "now" to 3 Oct: the card shows the amount of the expected day (2 Oct), not of today's date.
        from datetime import datetime as _dt, timezone as _tz
        with mock.patch("django.utils.timezone.now", return_value=_dt(2026, 10, 3, 12, tzinfo=_tz.utc)):
            response = self.client.get(reverse("epos_qbo:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-company-a-row="true"')
        self.assertContains(response, "Last confirmed sales:")
        self.assertContains(response, "₦812,345.50")
        self.assertNotContains(response, "Sales are paused")

    def test_overview_card_with_hold(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS))
        self.make_hold()
        response = self.client.get(reverse("epos_qbo:overview"))
        self.assertContains(response, "Sales are paused")
        self.assertContains(response, "Review the reason")
        self.assertNotContains(response, "MISMATCH")

    def test_overview_without_any_company_a_evidence_hides_card(self):
        response = self.client.get(reverse("epos_qbo:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="company-a-card"')

    def test_company_detail_has_holds_and_alerts(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS),
                      files={"guard/alerts.csv": ALERTS_CSV})
        response = self.client.get(reverse("epos_qbo:company-detail", args=["company_a"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company sections")
        self.assertContains(response, "Review stock checks")
        response = self.client.get(reverse("epos_qbo:company-detail", args=["company_a"]), {"tab": "products"})
        self.assertContains(response, 'id="company-a-holds-alerts"')
        self.assertContains(response, "akp_mapping_mismatch")

    def test_runs_page_lists_company_a_runs(self):
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=OK_STEPS))
        response = self.client.get(reverse("epos_qbo:runs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="daily-history"')
        self.assertContains(response, "Sales confirmed")
        self.assertContains(response, reverse("epos_qbo:company-a-run-detail", args=["2026-10-02", "run_170000Z"]))

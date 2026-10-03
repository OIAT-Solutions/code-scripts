"""Inbox safety, exact approvals and operational failure visibility."""
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User, Permission
from django.core import signing
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.epos_qbo.models import CompanyConfigRecord, PortalReviewAction, RunJob
from apps.epos_qbo.services import attention, attention_actions
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _summary, _step
from django.test import TestCase


class AttentionTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("reviewer", "", "test-password")
        self.client.force_login(self.user)
        self.folder = self.make_run("2026-10-02", "run_180000Z", _summary("2026-10-02"), files={
            "bills/summary.json": json.dumps({"payloads_sha256": "abc", "vendor_actions": []}),
            "bills/review.csv": "PO,EPOS Supplier,Status,Reasons,Warnings,Approve\n123,Test Supplier,READY,,,\n124,Other Supplier,HOLD,Possible duplicate,,\n",
            "bills/review_lines.csv": "PO,Amount\n123,100\n",
            "bills/payloads.jsonl": "{}\n",
        })
        self.url = reverse("epos_qbo:attention-confirm")

    def token(self, item, action="approve"):
        response = self.client.get(self.url, {"key": item["key"], "action": action})
        self.assertEqual(response.status_code, 200)
        return response.context["token"]

    def post(self, token):
        return self.client.post(self.url, {"token": token, "reason": "Checked evidence", "approval_ref": "owner yes for PO123", "confirmed": "yes"})

    def test_inbox_ready_and_held_and_escaping(self):
        items, errors = attention.inbox()
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["approve"])
        self.assertFalse(items[1]["approve"])
        self.assertTrue(items[1]["skip"])
        response = self.client.get(reverse("epos_qbo:attention"))
        self.assertContains(response, "Needs your attention")
        self.assertContains(response, "Possible duplicate")
        self.assertEqual(errors, [])

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_confirmation_queues_once_no_tool_or_qbo_call(self, dispatch):
        token = self.token(attention.inbox()[0][0])
        with mock.patch("apps.epos_qbo.services.attention_actions.execute") as execute:
            self.assertEqual(self.post(token).status_code, 302)
            self.assertEqual(self.post(token).status_code, 302)
            execute.assert_not_called()
        self.assertEqual(PortalReviewAction.objects.count(), 1)
        record = PortalReviewAction.objects.get()
        self.assertEqual(record.actor, "reviewer")
        self.assertEqual(record.job.status, "queued")
        self.assertEqual(record.job.scope, RunJob.SCOPE_PORTAL_REVIEW)
        dispatch.assert_called_once()

    def test_changed_line_evidence_refuses_before_enqueue(self):
        token = self.token(attention.inbox()[0][0])
        (self.folder / "bills/review_lines.csv").write_text("changed")
        self.assertEqual(self.post(token).status_code, 400)
        self.assertFalse(PortalReviewAction.objects.exists())

    def test_permissions_csrf_and_other_user_token(self):
        token = self.token(attention.inbox()[0][0])
        viewer = User.objects.create_user("viewer", password="test")
        self.client.force_login(viewer)
        self.assertEqual(self.post(token).status_code, 403)
        self.assertEqual(self.client.get(self.url, {"action": "approve", "key": attention.inbox()[0][0]["key"]}).status_code, 403)
        self.assertEqual(self.client.get(reverse("epos_qbo:attention")).status_code, 200)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, {"token": token}).status_code, 403)

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_tampered_token_and_automatic_signed_in_approval_ref(self, dispatch):
        token = self.token(attention.inbox()[0][0])
        self.assertEqual(self.post(token + "tampered").status_code, 400)
        self.assertEqual(self.client.post(self.url, {"token": token, "reason": "checked", "confirmed": "yes", "approval_ref":"spoofed user"}).status_code, 302)
        record = PortalReviewAction.objects.latest("created_at")
        self.assertIn(f"Approved by {self.user.get_username()} in the portal,", record.payload["approval_ref"])
        self.assertNotIn("spoofed", record.payload["approval_ref"])

    def test_ready_bill_adapter_passes_hash_and_only_selected_row(self):
        item = attention.inbox()[0][0]
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW, company_key="company_a")
        record = PortalReviewAction.objects.create(job=job, actor="reviewer", action="approve", reason="checked", confirmation_id="unique", payload={"key": item["key"], "snapshot": item["snapshot"], "approval_ref": "chat yes"})
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            self.assertEqual(attention_actions.execute(record), 0)
        cmd = call.call_args.args[0]
        self.assertIn("code_scripts.akponora_ops.bills_sync", cmd)
        self.assertEqual(cmd[cmd.index("--expect-sha") + 1], "abc")
        review = attention.rows(self.folder / "bills/review.csv")
        self.assertEqual([r["Approve"] for r in review], ["yes", ""])

    def test_background_refuses_stale_review(self):
        item = attention.inbox()[0][0]
        (self.folder / "bills/payloads.jsonl").write_text("changed")
        with self.assertRaises(attention_actions.ReviewChanged):
            attention_actions.current_item(item["key"], item["snapshot"])

    def test_hold_clear_uses_existing_archive_function(self):
        self.make_hold()
        item = attention.inbox()[0][0]
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job, actor="reviewer", action="approve", reason="reconciled", confirmation_id="hold", payload={"key": item["key"], "snapshot": item["snapshot"], "approval_ref": "chat yes"})
        self.assertEqual(attention_actions.execute(record), 0)
        self.assertFalse((self.tmp / "company_a_posting_hold.json").exists())
        self.assertEqual(len(list(self.tmp.glob("company_a_posting_hold.cleared-*.json"))), 1)

    def test_symlink_and_malformed_evidence_show_error(self):
        external = self.tmp.parent / (self.tmp.name + "-outside")
        external.write_text("private")
        self.addCleanup(external.unlink)
        path = self.folder / "bills/review.csv"
        path.unlink()
        path.symlink_to(external)
        items, errors = attention.inbox()
        self.assertTrue(errors)
        self.assertFalse(items)

    def test_stuck_and_failed_company_b_are_red_read_only(self):
        CompanyConfigRecord.objects.create(company_key="company_b", display_name="Goldplates", is_active=True)
        job = RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key="company_b", status="running", started_at=timezone.now()-timedelta(days=40))
        self.assertIn("stuck", attention.blockers()[0]["reason"])
        self.client.get(reverse("epos_qbo:attention"))
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        job.status = "failed"
        job.save()
        self.assertIn("did not post", attention.blockers()[0]["reason"])

    def test_dry_run_does_not_count_as_posted_sales(self):
        CompanyConfigRecord.objects.create(company_key="company_a", display_name="Akponora", is_active=True)
        self.make_run("2026-10-02", "run_190000Z_dry", _summary("2026-10-02", dry=True, steps=[_step("sales", counts={"mode":"post", "reconcile_status":"MATCH"})]))
        blockers = attention.blockers(now=datetime(2026,10,3,12,tzinfo=ZoneInfo("UTC")))
        self.assertIn("behind", blockers[0]["reason"])

    def test_daily_defaults_preview_and_rejects_future_or_pre_cutover(self):
        response = self.client.get(self.url, {"action": "daily", "date": "2026-10-01"})
        self.assertEqual(response.status_code, 200)
        data = signing.loads(response.context["token"], salt="company-a-review-confirmation")
        self.assertTrue(data["dry_run"])
        for day in ("2099-01-01", "2026-09-30", "bad"):
            self.assertEqual(self.client.get(self.url, {"action":"daily", "date":day}).status_code, 400)

    def test_catalogue_batch_and_deposit_use_existing_hash_gates(self):
        folder = self.folder / "catalogue"
        folder.mkdir()
        plan = {"plan_sha256": "planhash", "decisions": [{"pid":"1","review":"REVIEW"},{"pid":"2","review":"HOLD"}]}
        (folder/"summary.json").write_text("{}")
        (folder/"plan.json").write_text(json.dumps(plan))
        day = self.folder / "uf/2026-10-02"
        day.mkdir(parents=True)
        (day/"summary.json").write_text(json.dumps({"status":"READY", "payloads_sha256":"deposithash"}))
        items, _ = attention.inbox()
        catalogue = next(i for i in items if i["kind"] == "catalogue")
        deposit = next(i for i in items if i["kind"] == "deposit")
        self.assertTrue(catalogue["approve"])
        self.assertIn("--expect-plan-sha", attention_actions.tool_command(catalogue,"approve","yes"))
        self.assertIn("deposithash", attention_actions.tool_command(deposit,"approve","yes"))
        (day/"post_123.json").write_text(json.dumps({"complete":True}))
        self.assertFalse(any(i["kind"] == "deposit" for i in attention.inbox()[0]))

    def test_partial_catalogue_apply_is_not_hidden(self):
        folder = self.folder / "catalogue"
        folder.mkdir()
        (folder/"summary.json").write_text("{}")
        (folder/"plan.json").write_text(json.dumps({"plan_sha256":"planhash","decisions":[{"pid":"1","review":"REVIEW"}]}))
        (folder/"apply_receipt.json").write_text(json.dumps({"installed":None,"stopped":"failed"}))
        self.assertTrue(any(i["kind"] == "catalogue" for i in attention.inbox()[0]))

    def test_vendor_mapping_uses_reviewed_candidate_and_live_read_only_check(self):
        summary = {"payloads_sha256":"abc","vendor_actions":[{"supplier_id":"7","epos_name":"Supplier typo","state":"HOLD_NEAR_MATCH","candidates":["10 Supplier (0.95)"]}]}
        (self.folder/"bills/summary.json").write_text(json.dumps(summary))
        item = next(i for i in attention.inbox()[0] if i["kind"] == "vendor")
        self.assertTrue(item["approve"])
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job,actor="reviewer",action="approve",reason="checked",confirmation_id="vendor",payload={"key":item["key"],"snapshot":item["snapshot"],"approval_ref":"yes","vendor_id":"10"})
        with mock.patch("code_scripts.scripts.akponora_cutover.w7_create_items.QBOClient.for_company_a") as client:
            client.return_value.get_json.return_value = {"Vendor":{"Id":"10","DisplayName":"Supplier","Active":True}}
            self.assertEqual(attention_actions.execute(record),0)
            client.assert_called_once_with(allow_writes=False)
            client.return_value.post_json.assert_not_called()
        self.assertFalse(any(i["kind"] == "vendor" for i in attention.inbox()[0]))

    def test_skip_deposit_defers_only_same_snapshot_and_never_runs_tool(self):
        day = self.folder / "uf/2026-10-02"
        day.mkdir(parents=True)
        (day/"summary.json").write_text(json.dumps({"status":"HOLD","reasons":["missing sheet"]}))
        item = next(i for i in attention.inbox()[0] if i["kind"] == "deposit")
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW, status=RunJob.STATUS_SUCCEEDED)
        record = PortalReviewAction.objects.create(job=job,actor="reviewer",action="skip",reason="waiting",confirmation_id="defer",payload={"key":item["key"],"snapshot":item["snapshot"]})
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call") as call:
            self.assertEqual(attention_actions.execute(record),0)
            call.assert_not_called()
        record.finished_at = timezone.now()
        record.save()
        self.assertFalse(any(i["kind"] == "deposit" for i in attention.inbox()[0]))
        (day/"summary.json").write_text(json.dumps({"status":"READY","payloads_sha256":"new"}))
        self.assertTrue(any(i["kind"] == "deposit" for i in attention.inbox()[0]))

    def test_views_do_not_import_or_call_quickbooks(self):
        import ast
        from apps.epos_qbo import views_attention
        tree = ast.parse(Path(views_attention.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("qbo", (node.module or "").lower())
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"post_json", "get_json", "query", "QBOClient"})

    def test_missing_earlier_day_stays_red_even_after_later_sales_posted(self):
        CompanyConfigRecord.objects.create(company_key="company_a",display_name="Akponora",is_active=True)
        (self.folder/"summary.json").write_text(json.dumps(_summary("2026-10-02",steps=[_step("sales",counts={"mode":"post","reconcile_status":"MATCH"})])))
        self.assertTrue(attention.blockers(now=datetime(2026,10,3,12,tzinfo=ZoneInfo("UTC"))))

    def test_background_command_records_result_and_respects_global_lock(self):
        from django.core.management import call_command, CommandError
        from code_scripts.run_lock import LockResult
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job,actor="reviewer",action="skip",reason="waiting",confirmation_id="worker",payload={})
        with mock.patch("code_scripts.run_lock.hold_global_lock") as lock, mock.patch("apps.epos_qbo.management.commands.execute_portal_review.execute", return_value=0) as execute:
            lock.return_value.__enter__.return_value = LockResult(acquired=False)
            with self.assertRaises(CommandError):
                call_command("execute_portal_review", str(job.id))
            execute.assert_not_called()
        record.refresh_from_db()
        self.assertIn("Another pipeline run", record.result)
        self.assertIsNotNone(record.finished_at)

    def test_background_daily_review_exit_is_a_completed_job_with_review_result(self):
        from django.core.management import call_command
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job,actor="reviewer",action="daily",reason="preview",confirmation_id="dailyworker",payload={})
        with mock.patch("apps.epos_qbo.management.commands.execute_portal_review.execute", return_value=3):
            call_command("execute_portal_review",str(job.id))
        record.refresh_from_db()
        self.assertIn("need review",record.result)
        self.assertIsNotNone(record.finished_at)

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_local_deferral_needs_reason_but_no_financial_approval_reference(self, dispatch):
        day = self.folder / "uf/2026-10-02"
        day.mkdir(parents=True)
        (day/"summary.json").write_text(json.dumps({"status":"HOLD"}))
        item = next(i for i in attention.inbox()[0] if i["kind"] == "deposit")
        token = self.token(item,"skip")
        response = self.client.post(self.url,{"token":token,"reason":"Waiting for sheet","confirmed":"yes"})
        self.assertEqual(response.status_code,302)

    def test_state_root_with_symlinked_parent_is_supported(self):
        alias = self.tmp.parent / (self.tmp.name + "-alias")
        alias.symlink_to(self.tmp, target_is_directory=True)
        self.addCleanup(alias.unlink)
        self.make_hold()
        with mock.patch("code_scripts.paths.STATE_ROOT",alias):
            items, errors = attention.inbox()
        self.assertEqual(errors,[])
        self.assertTrue(any(i["kind"] == "hold" for i in items))

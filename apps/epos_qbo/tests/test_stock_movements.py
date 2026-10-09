import json
import os
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.epos_qbo.models import PortalReviewAction, RunJob
from apps.epos_qbo.services import attention, attention_actions
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _summary


class StockMovementPortalTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("reviewer", "", "password")
        self.client.force_login(self.user)
        self.event = {"event_id": "event1", "transfer_id": "42", "evidence_sha256": "sha1",
            "name": "Test product", "review_reason": "Stock added outside PO import", "staff": "Counter",
            "occurred_at_epos": "02/10/2026 09:00:00", "canonical_qty_change": "21", "epos_reason": "Stock Take"}
        self.report = {"company": "company_a", "events": [self.event], "errors": []}
        self.folder = self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02"),
            files={"movements/summary.json": json.dumps(self.report)})
        self.url = reverse("epos_qbo:attention-confirm")

    def item(self):
        return next(i for i in attention.inbox()[0] if i["kind"] == "stock_movement")

    def token(self):
        response = self.client.get(self.url, {"key": self.item()["key"], "action": "delivery"})
        self.assertEqual(response.status_code, 200)
        return response.context["token"]

    def test_card_is_classification_only_not_stock_approval(self):
        item = self.item()
        self.assertFalse(item["approve"])
        self.assertTrue(item["delivery"])
        self.assertEqual(self.client.get(self.url, {"key": item["key"], "action": "approve"}).status_code, 400)
        response = self.client.get(reverse("epos_qbo:attention"))
        self.assertContains(response, "Record as delivery")
        self.assertContains(response, "Record as count correction")

    def test_newest_capture_deduplicates_event_and_incomplete_is_visible(self):
        self.report["errors"] = ["missing movement page"]
        self.make_run("2026-10-03", "run_170000Z", _summary("2026-10-03"),
            files={"movements/summary.json": json.dumps(self.report)})
        folder = self.tmp / "ops/company_a/daily/2026-10-03/run_170000Z/movements/summary.json"
        later = folder.stat().st_mtime + 60
        os.utime(folder, (later, later))
        items, errors = attention.inbox()
        self.assertEqual(sum(i["kind"] == "stock_movement" for i in items), 1)
        self.assertEqual([e for e in errors if "could not read 1 adjustment" in e], [errors[0]])

    def test_an_older_incomplete_capture_raises_no_warning(self):
        # 9 Oct 2026: four stale red banners came from captures older than the latest clean one
        self.write_report("2026-10-01", ["Transfer 1: unexpected detail columns"], age=-120)
        self.assertFalse(any("stock-movement" in e for e in attention.inbox()[1]))

    def write_report(self, day, errors, age):
        self.make_run(day, "run_170000Z", _summary(day), files={"movements/summary.json": json.dumps(dict(self.report, errors=errors))})
        path = self.tmp / f"ops/company_a/daily/{day}/run_170000Z/movements/summary.json"
        when = (self.tmp / "ops/company_a/daily/2026-10-02/run_170000Z/movements/summary.json").stat().st_mtime + age
        os.utime(path, (when, when))

    def test_changed_evidence_rejected_before_enqueue(self):
        token = self.token()
        self.event["canonical_qty_change"] = "22"
        (self.folder / "movements/summary.json").write_text(json.dumps(self.report))
        self.assertEqual(self.client.post(self.url, {"token": token, "reason": "invoice 123", "confirmed": "yes"}).status_code, 400)
        self.assertFalse(PortalReviewAction.objects.exists())

    def test_signed_confirmation_and_worker_never_call_financial_tool(self):
        token = self.token()
        self.assertEqual(self.client.post(self.url, {"token": token, "reason": "invoice 123", "confirmed": "yes"}).status_code, 302)
        record = PortalReviewAction.objects.get()
        self.assertEqual(record.payload["movement_sha"], "sha1")
        with mock.patch.object(attention_actions.subprocess, "call") as call:
            self.assertEqual(attention_actions.execute(record), 0)
            call.assert_not_called()
        record.job.status = RunJob.STATUS_SUCCEEDED
        record.job.save()
        record.finished_at = timezone.now()
        record.save()
        # A delivery explanation does not silently clear the missing bill.
        item = self.item()
        self.assertEqual(item["extra"]["classification"], "delivery")
        self.assertIn("bill", item["reason"])

    def test_viewer_cannot_classify(self):
        key = self.item()["key"]
        self.client.force_login(User.objects.create_user("viewer"))
        self.assertEqual(self.client.get(self.url, {"key": key, "action": "delivery"}).status_code, 403)

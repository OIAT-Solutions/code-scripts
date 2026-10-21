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

    def kinds(self):
        return [i for i in attention.inbox()[0] if i["kind"] == "stock_movement"]

    def test_events_quickbooks_follows_on_its_own_get_no_card(self):
        # 12 Oct 2026: cards for Stock Take events already posted by stock_follow were still showing
        self.assertIn("Not posted automatically", self.kinds()[0]["reason"])  # product not certain: stays
        auto = dict(self.event, event_id="event2", transfer_id="43", qbo_item_id="19001", source_value_change="-500",
                    canonical_qty_change="-1", occurred_at_epos="10/10/2026 10:16:11", holds=[])
        posted = dict(auto, event_id="event3", transfer_id="44", qbo_item_id="")
        self.report["events"] = [self.event, auto, posted]
        (self.folder / "movements/summary.json").write_text(json.dumps(self.report))
        ledger = self.tmp / "ops/company_a/stock_follow/posted.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"event3": {"adjustment": "1", "doc": "ST261010-x"}}))
        self.assertEqual([i["extra"]["event"]["event_id"] for i in self.kinds()], ["event1"])

    def test_products_quickbooks_does_not_count_get_no_card(self):
        self.event["epos_product_id"] = "777"
        (self.folder / "movements/summary.json").write_text(json.dumps(self.report))
        mapping = self.tmp / "mappings/company_a/approved.csv"
        mapping.parent.mkdir(parents=True, exist_ok=True)
        old = mapping.read_text() if mapping.is_file() else ""
        mapping.write_text("EPOS Product ID,Target QBO Item Type\n777,NonInventory\n")
        try:
            self.assertEqual(self.kinds(), [])
        finally:
            mapping.write_text(old) if old else mapping.unlink()

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

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, QboWebhookEvent
from apps.epos_qbo.services.qbo_webhook_notifications import send_pending_notifications


def _signature(body: bytes, token: str) -> str:
    digest = hmac.new(token.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class QuickBooksWebhookViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("epos_qbo:quickbooks-webhook")
        self.token = "test-verifier-token"

    def _post(self, payload: object, *, token: str | None = None, url: str | None = None):
        body = json.dumps(payload).encode("utf-8")
        verifier = token if token is not None else self.token
        return self.client.post(
            url or self.url,
            data=body,
            content_type="application/json",
            HTTP_INTUIT_SIGNATURE=_signature(body, verifier),
        )

    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_VERIFIER_TOKEN": "test-verifier-token"})
    @mock.patch("apps.epos_qbo.webhooks._after_response")
    @mock.patch("apps.epos_qbo.webhooks.accept_qbo_webhook_body", return_value=["a", "b"])
    def test_valid_signature_processes_payload_without_login(self, process_payload, after_response):
        response = self._post({"eventNotifications": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notifications_queued"], 2)
        process_payload.assert_called_once_with({"eventNotifications": []})
        after_response.assert_called_once_with(send_pending_notifications, ["a", "b"])

    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_VERIFIER_TOKEN": "test-verifier-token"})
    @mock.patch("apps.epos_qbo.webhooks._after_response")
    @mock.patch("apps.epos_qbo.webhooks.accept_qbo_webhook_body", return_value=["a"])
    def test_no_slash_url_processes_payload_without_login(self, process_payload, _after_response):
        response = self._post({"eventNotifications": []}, url="/epos-qbo/webhooks/quickbooks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notifications_queued"], 1)
        process_payload.assert_called_once_with({"eventNotifications": []})

    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_VERIFIER_TOKEN": "test-verifier-token"})
    @mock.patch("apps.epos_qbo.webhooks.accept_qbo_webhook_body")
    def test_invalid_signature_rejected(self, process_payload):
        response = self._post({"eventNotifications": []}, token="wrong-token")
        self.assertEqual(response.status_code, 401)
        process_payload.assert_not_called()
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.status, QboWebhookEvent.STATUS_REJECTED)
        self.assertFalse(event.signature_valid)
        self.assertIn("Invalid Intuit signature", event.skip_reason)

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_missing_verifier_token_is_service_unavailable(self):
        response = self.client.post(self.url, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)

    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_VERIFIER_TOKEN": "test-verifier-token"})
    @mock.patch("apps.epos_qbo.webhooks._after_response")
    @mock.patch("apps.epos_qbo.webhooks.accept_qbo_webhook_body", return_value=["a"])
    def test_cloudevents_array_payload_is_accepted(self, process_payload, _after_response):
        payload = [
            {
                "specversion": "1.0",
                "id": "event-1",
                "type": "qbo.item.created.v1",
                "time": "2026-06-12T15:52:10Z",
                "intuitentityid": "14895",
                "intuitaccountid": "9341455406194328",
                "data": {},
            }
        ]
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        process_payload.assert_called_once_with(payload)


class QuickBooksWebhookNotificationTests(TestCase):
    def setUp(self):
        now = mock.patch(
            "apps.epos_qbo.services.qbo_webhook_notifications._now",
            return_value=datetime(2026, 6, 12, 18, 0, tzinfo=timezone.utc),
        )
        now.start()
        self.addCleanup(now.stop)
        self.company = CompanyConfigRecord.objects.create(
            company_key="company_a",
            display_name="Oreva Innovations & Tech",
            config_json={
                "company_key": "company_a",
                "display_name": "Oreva Innovations & Tech",
                "qbo": {"realm_id": "9341455406194328", "environment": "production"},
            },
        )
        self.payload = {
            "eventNotifications": [
                {
                    "realmId": "9341455406194328",
                    "dataChangeEvent": {
                        "entities": [
                            {
                                "name": "Item",
                                "id": "14895",
                                "operation": "Create",
                                "lastUpdated": "2026-06-12T14:55:00.000Z",
                            }
                        ]
                    },
                }
            ]
        }

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.requests.get")
    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.get_access_token", return_value="access")
    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict(
        "os.environ",
        {"QBO_WEBHOOK_SLACK_URL_COMPANY_A": "https://hooks.slack.test/qbo-company-a"},
    )
    def test_item_event_sends_enriched_slack(self, send_slack, _get_token, requests_get):
        requests_get.return_value = mock.Mock(
            status_code=200,
            json=lambda: {
                "Item": {
                    "Name": "Coke 50CL",
                    "Type": "Inventory",
                    "Active": True,
                    "TrackQtyOnHand": True,
                    "FullyQualifiedName": "Drinks:Coke 50CL",
                    "IncomeAccountRef": {"name": "Product Sales", "value": "91"},
                    "AssetAccountRef": {"name": "Inventory Asset", "value": "92"},
                }
            },
        )

        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        sent_count = process_qbo_webhook_payload(self.payload)

        self.assertEqual(sent_count, 1)
        send_slack.assert_called_once()
        message, webhook = send_slack.call_args.args
        self.assertEqual(webhook, "https://hooks.slack.test/qbo-company-a")
        self.assertIn("QuickBooks Item Create", message)
        self.assertIn("Oreva Innovations & Tech", message)
        self.assertIn("Coke 50CL", message)
        self.assertIn("Inventory", message)
        self.assertIn("Product Sales", message)
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.status, QboWebhookEvent.STATUS_SENT)
        self.assertTrue(event.signature_valid)
        self.assertTrue(event.slack_sent)
        self.assertEqual(event.realm_id, "9341455406194328")
        self.assertEqual(event.company_key, "company_a")
        self.assertEqual(event.entity_name, "Item")

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.requests.get")
    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.get_access_token", return_value="access")
    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict(
        "os.environ",
        {"QBO_WEBHOOK_SLACK_URL_COMPANY_A": "https://hooks.slack.test/qbo-company-a"},
    )
    def test_cloudevents_item_event_sends_enriched_slack(self, send_slack, _get_token, requests_get):
        requests_get.return_value = mock.Mock(
            status_code=200,
            json=lambda: {
                "Item": {
                    "Name": "Test Product",
                    "Type": "Inventory",
                    "Active": True,
                    "TrackQtyOnHand": True,
                }
            },
        )
        payload = [
            {
                "specversion": "1.0",
                "id": "event-1",
                "type": "qbo.item.created.v1",
                "time": "2026-06-12T15:52:10Z",
                "intuitentityid": "14895",
                "intuitaccountid": "9341455406194328",
                "data": {},
            }
        ]

        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_body

        self.assertEqual(process_qbo_webhook_body(payload), 1)
        message = send_slack.call_args.args[0]
        self.assertIn("QuickBooks Item Create", message)
        self.assertIn("Test Product", message)
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.realm_id, "9341455406194328")
        self.assertEqual(event.entity_name, "Item")
        self.assertEqual(event.operation, "Create")
        self.assertEqual(event.entity_id, "14895")

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_non_item_event_sends_generic_slack(self, send_slack):
        payload = {
            "eventNotifications": [
                {
                    "realmId": "9341455406194328",
                    "dataChangeEvent": {
                        "entities": [
                            {
                                "name": "Customer",
                                "id": "42",
                                "operation": "Update",
                                "lastUpdated": "2026-06-12T15:00:00.000Z",
                            }
                        ]
                    },
                }
            ]
        }

        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        sent_count = process_qbo_webhook_payload(payload)

        self.assertEqual(sent_count, 1)
        message = send_slack.call_args.args[0]
        self.assertIn("QuickBooks Customer Update", message)
        self.assertIn("Entity ID: `42`", message)

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_unknown_realm_sends_no_slack(self, send_slack):
        payload = {
            "eventNotifications": [
                {
                    "realmId": "unknown",
                    "dataChangeEvent": {"entities": [{"name": "Item", "id": "1", "operation": "Create"}]},
                }
            ]
        }

        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        self.assertEqual(process_qbo_webhook_payload(payload), 0)
        send_slack.assert_not_called()
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.status, QboWebhookEvent.STATUS_SKIPPED)
        self.assertEqual(event.realm_id, "unknown")
        self.assertIn("Unknown realmId", event.skip_reason)

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"SLACK_WEBHOOK_URL_A": "https://hooks.slack.test/pipeline"}, clear=True)
    def test_pipeline_slack_env_is_not_used_for_qbo_webhooks(self, send_slack):
        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        self.assertEqual(process_qbo_webhook_payload(self.payload), 0)
        send_slack.assert_not_called()
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.status, QboWebhookEvent.STATUS_SKIPPED)
        self.assertIn("No QBO webhook Slack URL", event.skip_reason)

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict(
        "os.environ",
        {"QBO_WEBHOOK_SLACK_URL_COMPANY_A": "https://hooks.slack.test/qbo-company-a"},
    )
    def test_intuit_sample_realm_is_logged_and_sent_as_test_event(self, send_slack):
        payload = {
            "eventNotifications": [
                {
                    "realmId": "310687",
                    "dataChangeEvent": {
                        "entities": [
                            {
                                "name": "Item",
                                "id": "1234",
                                "operation": "Create",
                                "lastUpdated": "2026-06-12T15:08:10.491Z",
                            }
                        ]
                    },
                }
            ]
        }

        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        self.assertEqual(process_qbo_webhook_payload(payload), 1)
        message, webhook = send_slack.call_args.args
        self.assertEqual(webhook, "https://hooks.slack.test/qbo-company-a")
        self.assertIn("QuickBooks Test Event", message)
        self.assertIn("TEST", message.upper())
        event = QboWebhookEvent.objects.get()
        self.assertTrue(event.is_test_event)
        self.assertEqual(event.status, QboWebhookEvent.STATUS_SENT)
        self.assertEqual(event.realm_id, "310687")

    def _customer_payload(self, last_updated: str) -> dict:
        return {
            "eventNotifications": [
                {
                    "realmId": "9341455406194328",
                    "dataChangeEvent": {
                        "entities": [
                            {"name": "Customer", "id": "42", "operation": "Update", "lastUpdated": last_updated}
                        ]
                    },
                }
            ]
        }

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_october_resent_change_is_recorded_but_not_posted_again(self, send_slack):
        # 22-28 Oct 2026: Intuit resent one batch 56 times and each copy was posted to Slack again.
        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        payload = self._customer_payload("2026-06-12T15:00:00.000Z")
        self.assertEqual(process_qbo_webhook_payload(payload), 1)
        self.assertEqual(process_qbo_webhook_payload(payload), 0)

        send_slack.assert_called_once()
        first, second = QboWebhookEvent.objects.order_by("id")
        self.assertEqual(first.status, QboWebhookEvent.STATUS_SENT)
        self.assertEqual(second.status, QboWebhookEvent.STATUS_SKIPPED)
        self.assertIn("resent", second.skip_reason)

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_resent_change_is_posted_when_the_earlier_post_failed(self, send_slack):
        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        payload = self._customer_payload("2026-06-12T15:00:00.000Z")
        process_qbo_webhook_payload(payload)
        QboWebhookEvent.objects.update(status=QboWebhookEvent.STATUS_FAILED)

        self.assertEqual(process_qbo_webhook_payload(payload), 1)
        self.assertEqual(send_slack.call_count, 2)

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_change_older_than_six_hours_is_recorded_without_slack(self, send_slack):
        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        self.assertEqual(process_qbo_webhook_payload(self._customer_payload("2026-06-12T11:59:00.000Z")), 0)

        send_slack.assert_not_called()
        event = QboWebhookEvent.objects.get()
        self.assertEqual(event.status, QboWebhookEvent.STATUS_SKIPPED)
        self.assertIn("over 6 hours old", event.skip_reason)
        self.assertEqual(event.last_updated, "2026-06-12T11:59:00.000Z")

    @mock.patch("apps.epos_qbo.services.qbo_webhook_notifications.send_slack_success")
    @mock.patch.dict("os.environ", {"QBO_WEBHOOK_SLACK_URL": "https://hooks.slack.test/qbo-default"})
    def test_change_time_that_cannot_be_read_is_still_posted(self, send_slack):
        from apps.epos_qbo.services.qbo_webhook_notifications import process_qbo_webhook_payload

        self.assertEqual(process_qbo_webhook_payload(self._customer_payload("")), 1)
        self.assertEqual(process_qbo_webhook_payload(self._customer_payload("")), 1)


class QuickBooksWebhookAfterResponseTests(TestCase):
    def test_slack_work_runs_on_its_own_thread(self):
        from apps.epos_qbo.webhooks import _after_response

        seen = []
        done = threading.Event()

        def work(pending):
            seen.append((threading.current_thread().name, pending))
            done.set()

        _after_response(work, ["event"])

        self.assertTrue(done.wait(5))
        self.assertEqual(seen, [("qbo-webhook-slack", ["event"])])

    def test_a_failure_after_the_response_is_logged_not_raised(self):
        from apps.epos_qbo.webhooks import _after_response

        done = threading.Event()

        def work(_pending):
            done.set()
            raise RuntimeError("Slack down")

        with self.assertLogs("apps.epos_qbo.webhooks", level="ERROR") as logs:
            _after_response(work, ["event"])
            self.assertTrue(done.wait(5))
            for thread in threading.enumerate():
                if thread.name == "qbo-webhook-slack":
                    thread.join(5)
        self.assertIn("QBO webhook Slack notifications failed", logs.output[0])

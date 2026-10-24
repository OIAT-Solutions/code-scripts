"""Efficiency changes must preserve complete evidence and financial fail-closed checks."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from code_scripts import qbo_upload as upload
from code_scripts.akponora_ops import bills_sync as bills


def live_item(i, **changes):
    return {"Id": str(i), "Name": f"Product {i}", "Type": "Inventory", "Sku": f"AKP-{i}",
            "Active": True, "TrackQtyOnHand": True, "InvStartDate": "2026-10-01",
            "AssetAccountRef": {"value": "77"}, **changes}


def response(items, status=200):
    result = mock.Mock(status_code=status)
    result.json.return_value = {"QueryResponse": {"Item": items}}
    return result


class GroupedExactItemsTests(unittest.TestCase):
    def test_563_targets_need_29_reads_and_remain_fresh_on_second_pass(self):
        items = [live_item(i) for i in range(1, 564)]
        batches = [response(items[i:i + 20]) for i in range(0, 563, 20)]
        expected = {x["Name"]: x for x in items}
        with mock.patch.object(upload, "_make_qbo_request", side_effect=batches + batches) as request, \
             mock.patch.object(upload, "get_or_create_item_id") as create:
            for _ in range(2):
                resolved = {}
                upload.resolve_conversion_items(list(expected), SimpleNamespace(company_key="company_a"),
                                                object(), "realm", resolved, exact_targets=expected)
                self.assertEqual(len(resolved), 563)
            self.assertEqual(request.call_count, 58)
            self.assertTrue(all("select * from Item where Id in" in parse_qs(urlparse(c.args[1]).query)["query"][0]
                                for c in request.call_args_list))
            create.assert_not_called()

    def test_missing_duplicate_unexpected_http_and_malformed_results_refuse(self):
        for result in [response([]), response([live_item(1), live_item(1)]), response([live_item(2)]),
                       response([live_item(1)], 500), response([None]), response("bad")]:
            with self.subTest(body=result.json.return_value), \
                 mock.patch.object(upload, "_make_qbo_request", return_value=result):
                with self.assertRaises(RuntimeError):
                    upload.query_exact_conversion_items(object(), "realm", ["1"])

    def test_partial_second_batch_cannot_publish_partial_resolution(self):
        items = [live_item(i) for i in range(1, 22)]
        result = {}
        with mock.patch.object(upload, "_make_qbo_request", side_effect=[response(items[:20]), response([])]):
            with self.assertRaises(RuntimeError):
                upload.resolve_conversion_items([x["Name"] for x in items], SimpleNamespace(company_key="company_a"),
                                                object(), "realm", result, exact_targets={x["Name"]: x for x in items})
        self.assertEqual(result, {})

    def test_each_required_field_is_still_validated(self):
        expected = live_item(1)
        mutations = {"Sku": "LEGACY", "Name": "Other", "Type": "NonInventory", "Active": False,
                     "TrackQtyOnHand": False, "InvStartDate": "2026-09-01",
                     "AssetAccountRef": {"value": "120000"}}
        for field, value in mutations.items():
            result = {}
            with self.subTest(field=field), mock.patch.object(upload, "_make_qbo_request",
                                                            return_value=response([{**expected, field: value}])):
                with self.assertRaises(ValueError):
                    upload.resolve_conversion_items([expected["Name"]], SimpleNamespace(company_key="company_a"),
                                                    object(), "realm", result, exact_targets={expected["Name"]: expected})
                self.assertEqual(result, {})


class ReceivedPOEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.order = {"OrderRef": 42, "DateReceived": "2026-10-22T12:00:00", "TotalValueReceived": 100}
        self.detail = {"OrderRef": 42, "Products": [{"ProductId": 1, "QuantityReceived": 2}]}

    def row(self, **changes):
        return {"ref": "42", "fingerprint": bills.po_fingerprint(self.order), "detail": self.detail, **changes}

    def test_active_window_received_order_reused_but_changed_or_incomplete_not_reused(self):
        got = bills.cached_details([self.order], {"42": self.row()}, bills.received_order_cacheable)
        self.assertEqual(got, {"42": self.detail})
        variants = [self.row(fingerprint="changed"), self.row(detail={"OrderRef": 999, "Products": []}),
                    self.row(detail={"OrderRef": 42}), self.row(detail={"OrderRef": 42, "Products": None})]
        for row in variants:
            self.assertEqual(bills.cached_details([self.order], {"42": row}, bills.received_order_cacheable), {})
        self.assertFalse(bills.received_order_cacheable({**self.order, "DateReceived": None}))
        self.assertFalse(bills.received_order_cacheable({**self.order, "DateReceived": "invalid"}))

    def test_shared_cache_is_materialized_for_offline_replay_without_detail_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evidence = root / "evidence"; evidence.mkdir()
            primary, other = root / "bills.jsonl", root / "catalogue.jsonl"
            other.write_text(json.dumps(self.row()) + "\n")
            page = mock.MagicMock()
            resp = mock.Mock(url="https://example/GetPurchaseOrders")
            resp.request.method = "GET"; resp.text.return_value = json.dumps({"orders": [self.order]})
            page.on.side_effect = lambda event, handler: handler(resp)
            pw = mock.MagicMock(); pw.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
            context = mock.MagicMock(); context.__enter__.return_value = pw
            with mock.patch.object(bills, "sync_playwright", return_value=context), \
                 mock.patch.object(bills, "company_config"), mock.patch.object(bills, "epos_login"), \
                 mock.patch.object(bills, "goto_retry"), mock.patch.object(bills, "extract_po_model") as extract:
                orders, details = bills.capture_epos(evidence, order_from="2026-10-01", order_to="2026-10-23",
                                                    want=lambda o: True, cacheable=bills.received_order_cacheable,
                                                    cache_path=primary, fallback_cache_paths=(other,))
            extract.assert_not_called()
            self.assertEqual(details["42"]["Products"], self.detail["Products"])
            _, replay = bills.load_pos(evidence / "po_list_raw.json", evidence / "po_details.jsonl")
            self.assertEqual(replay, details)
            self.assertEqual(bills.load_po_cache(primary)["42"]["detail"], details["42"])
            metrics = json.loads((evidence / "capture_metrics.json").read_text())
            self.assertEqual((metrics["cache_hits"], metrics["detail_pages_requested"], metrics["missing_details"]), (1, 0, 0))

    def test_collection_keeps_unresolved_window_and_uses_received_cache_policy(self):
        args = SimpleNamespace(dup_days=14, dup_min_value="50000", po_list=None, po_details=None)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bills, "state_dir", return_value=Path(tmp)), \
             mock.patch.object(bills, "capture_epos", return_value=([], {})) as capture:
            bills.collect_pos(args, Path(tmp), ("2026-10-06", "2026-10-23"))
        self.assertTrue(capture.call_args.kwargs["cacheable"](self.order))
        self.assertEqual(capture.call_args.kwargs["order_from"], "2026-09-15")


"""Regression tests for the Company A cutover review fixes (H1, H3, M1-M3).

Every QBO call is faked; any real HTTP attempt fails the test.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

from code_scripts import operations_controls, qbo_upload
from code_scripts.company_config import CompanyConfig
from code_scripts.operations_controls import (
    ProposalQueue,
    build_posting_manifest,
    clear_posting_hold,
    is_controlled_business_date,
    payload_digest,
    posting_hold_path,
    require_posting_approval,
    require_reconciliation_match,
)
from code_scripts.conversion_contract import validate_upload_frame
from code_scripts.tests.test_product_conversion import approved_row, sales_frame, write_mapping
from code_scripts.transform import transform_dataframe_unified

COMPANY_A_JSON = Path(__file__).resolve().parents[1] / "companies" / "company_a.json"
REALM = "9341455406194328"
CATCH_ALL_ITEM = {"Id": "15030", "Name": "AKP-UNMAPPED-EPOS-SALES", "Type": "NonInventory", "Active": True}
OCT_ITEM = {
    "Id": "90001", "Name": "Wine bottle", "Type": "Inventory", "Sku": "AKP-WINE", "InvStartDate": "2026-10-01",
    "Active": True, "TrackQtyOnHand": True, "AssetAccountRef": {"value": "77"},
}
LEGACY_ITEM_IDS = {"1", "15031"}  # default item + a legacy Inventory example (REDBULL WATERMELON)
TEMPLATE_HEADER = (Path(__file__).resolve().parents[2] / "templates" / "product_conversion_empty.csv")


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


def _raw(product, qty, total, when, tender="Cash", product_id=""):
    net = round(total / 1.075, 2)
    return sales_frame(
        Product=product, Quantity=qty, **{
            "TOTAL Sales": total, "NET Sales": net, "Tax": round(total - net, 2), "Cost Price": 0,
            "Date/Time": when, "Tender": tender, "ProductId": product_id,
        }
    )


class CompanyAUploadHarness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.mapping = self.root / "approved.csv"
        self.mapping.write_text(TEMPLATE_HEADER.read_text())
        env = {"COMPANY_A_PRODUCT_CONVERSION_FILE": str(self.mapping)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COMPANY_A_POSTING_APPROVAL_FILE", None)
        state_patch = mock.patch("code_scripts.paths.STATE_ROOT", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        # Belt and braces: no real HTTP from anywhere in this test.
        http_patch = mock.patch("requests.Session.request", side_effect=AssertionError("real HTTP attempted"))
        http_patch.start()
        self.addCleanup(http_patch.stop)
        self.config = CompanyConfig(COMPANY_A_JSON)

    def install_october_mapping(self):
        row = approved_row(**{
            "Target QBO Item Type": "Inventory", "Target QBO Item Id": "90001", "Target QBO Name": "Wine bottle",
            "Target QBO SKU": "AKP-WINE", "Effective Date": "2026-10-01", "Staff Approved Sale Multiplier": "1",
            "EPOS Name": "Wine crate", "EPOS Product ID": "4242", "EPOS Existing SKU": "WINE-CRATE",
        })
        write_mapping(self.root, [row])  # writes root/mapping.csv
        self.mapping = self.root / "mapping.csv"
        os.environ["COMPANY_A_PRODUCT_CONVERSION_FILE"] = str(self.mapping)

    def transform_to_csv(self, raw: pd.DataFrame, target: str) -> Path:
        frame = transform_dataframe_unified(raw, self.config, target_date=target)
        path = self.work / f"sales_{target}.csv"
        frame.to_csv(path, index=False)
        return path

    def run_upload(self, csv_path, target, *, existing=(), date_mismatches=None, dry_run=False, post_status=None):
        posts, calls = [], []

        def fake(method, url, token_mgr, **kwargs):
            calls.append((method, url))
            if method == "POST":
                if "/salesreceipt" not in url:
                    raise AssertionError("unexpected POST " + url)
                payload = kwargs["json"]
                status = post_status(payload) if post_status else 200
                if status >= 300:
                    return FakeResponse({"Fault": {"Error": [{"code": "6000", "message": "rejected"}]}}, status)
                posts.append({"url": url, "payload": payload})
                return FakeResponse({"SalesReceipt": {"Id": str(5000 + len(posts)), "DocNumber": payload["DocNumber"]}})
            if "/item/15030" in url:
                return FakeResponse({"Item": CATCH_ALL_ITEM})
            if "/item/90001" in url:
                return FakeResponse({"Item": OCT_ITEM})
            if "/item/" in url:
                raise AssertionError("unexpected item lookup " + url)
            return FakeResponse({"QueryResponse": {}})

        argv = ["qbo_upload.py", "--company", "company_a", "--target-date", target]
        if dry_run:
            argv.append("--dry-run")
        code = 0
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(qbo_upload, "get_available_companies", return_value=["company_a"]), \
             mock.patch.object(qbo_upload, "load_company_config", return_value=self.config), \
             mock.patch.object(qbo_upload, "ensure_company_runtime_compatible", return_value=None), \
             mock.patch.object(qbo_upload, "verify_realm_match", return_value=None), \
             mock.patch.object(qbo_upload, "TokenManager", return_value=mock.MagicMock(request_stats={})), \
             mock.patch.object(qbo_upload, "find_latest_single_csv", return_value=str(csv_path)), \
             mock.patch.object(qbo_upload, "get_repo_root", return_value=str(self.work)), \
             mock.patch.object(qbo_upload, "check_qbo_existing_docnumbers",
                               return_value=(set(existing), dict(date_mismatches or {}))), \
             mock.patch.object(qbo_upload, "load_uploaded_docnumbers", return_value=set()), \
             mock.patch.object(qbo_upload, "save_uploaded_docnumber", return_value=None), \
             mock.patch.object(qbo_upload, "get_or_create_item_id",
                               side_effect=AssertionError("name-based item resolution/create on sales path")), \
             mock.patch.object(qbo_upload, "create_inventory_item",
                               side_effect=AssertionError("Inventory create on sales path")), \
             mock.patch.object(qbo_upload, "_make_qbo_request", side_effect=fake):
            try:
                qbo_upload.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, posts, calls

    def september_raw(self):
        when = "2026-09-23 12:00:00"
        return pd.concat([
            _raw("SACHET WATER 50cl*20", 3, 1500, when, "Cash"),
            _raw("FRESH YO YOGHURT", 2, 600, when, "Cash"),
            _raw("REFUNDED WINE", -1, -9000, when, "Cash"),      # refund line
            _raw("ZERO LINE", 0, 0, when, "Transfer"),           # 0/0 line
            _raw("BREAD", 1, 1200, when, "Transfer"),
        ], ignore_index=True)

    def october_raw(self, qty=2, total=240000):
        return _raw("Wine crate", qty, total, "2026-10-01 12:00:00", "Cash", product_id="4242")


class SeptemberCatchAllTests(CompanyAUploadHarness):
    def assert_all_catch_all(self, posts):
        lines = [l for p in posts for l in p["payload"]["Line"] if l.get("DetailType") == "SalesItemLineDetail"]
        self.assertTrue(lines)
        self.assertEqual({l["SalesItemLineDetail"]["ItemRef"]["value"] for l in lines}, {"15030"})
        return lines

    def test_september_day_with_refund_and_zero_lines_posts_to_catch_all_without_manifest(self):
        raw = self.september_raw()
        csv_path = self.transform_to_csv(raw, "2026-09-23")
        self.assertIn("_Conversion Proof", pd.read_csv(csv_path).columns)
        code, posts, calls = self.run_upload(csv_path, "2026-09-23")
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 2)  # Cash + Transfer receipts
        lines = self.assert_all_catch_all(posts)
        gross = sum(float(l["SalesItemLineDetail"]["TaxInclusiveAmt"]) for l in lines)
        self.assertAlmostEqual(gross, float(raw["TOTAL Sales"].sum()), places=2)
        # Legacy coercion: refund/zero lines post with Qty 1, refund keeps its negative gross.
        refund = [l for l in lines if float(l["SalesItemLineDetail"]["TaxInclusiveAmt"]) < 0]
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund[0]["SalesItemLineDetail"]["Qty"], 1.0)
        self.assertTrue(all("requestid=" not in p["url"] for p in posts))
        self.assertFalse(posting_hold_path().exists())
        self.assertFalse((self.state / "company_a_proposals.sqlite").exists())

    def test_legacy_csv_without_proof_column_still_uploads_history(self):
        csv_path = self.transform_to_csv(self.september_raw(), "2026-09-23")
        frame = pd.read_csv(csv_path).drop(columns=["_Conversion Proof"])
        frame.to_csv(csv_path, index=False)
        code, posts, _ = self.run_upload(csv_path, "2026-09-23")
        self.assertEqual(code, 0)
        self.assert_all_catch_all(posts)

    def test_rerun_of_posted_day_skips_existing_docnumbers_harmlessly(self):
        csv_path = self.transform_to_csv(self.september_raw(), "2026-09-23")
        docs = set(pd.read_csv(csv_path)["*SalesReceiptNo"])
        code, posts, calls = self.run_upload(csv_path, "2026-09-23", existing=docs)
        self.assertEqual(code, 0)
        self.assertEqual(posts, [])
        # No exact-content verification query is needed for pre-October history.
        self.assertFalse([c for c in calls if "SalesReceipt%20where%20DocNumber" in c[1]])

    def test_one_failed_september_receipt_does_not_block_the_others_or_write_hold(self):
        csv_path = self.transform_to_csv(self.september_raw(), "2026-09-23")
        code, posts, _ = self.run_upload(
            csv_path, "2026-09-23", post_status=lambda p: 400 if p["PrivateNote"] == "Cash" else 200
        )
        self.assertEqual(code, 1)  # a failed receipt still fails the run, as before
        self.assertEqual(len(posts), 1)
        self.assertFalse(posting_hold_path().exists())
        # Retry of the failed receipt works on the next run (the posted one is skipped).
        posted = {p["payload"]["DocNumber"] for p in posts}
        code, retry_posts, _ = self.run_upload(csv_path, "2026-09-23", existing=posted)
        self.assertEqual(code, 0)
        self.assertEqual(len(retry_posts), 1)

    def test_september_date_mismatch_is_per_receipt_as_before(self):
        csv_path = self.transform_to_csv(self.september_raw(), "2026-09-23")
        doc = sorted(set(pd.read_csv(csv_path)["*SalesReceiptNo"]))[0]
        code, posts, _ = self.run_upload(csv_path, "2026-09-23", date_mismatches={doc: "2026-09-22"})
        self.assertEqual(code, 0)  # attempted anyway (QBO would reject a true duplicate)
        self.assertEqual(len(posts), 2)

    def test_existing_hold_does_not_block_september_history(self):
        posting_hold_path().write_text("{}")
        csv_path = self.transform_to_csv(self.september_raw(), "2026-09-23")
        code, posts, _ = self.run_upload(csv_path, "2026-09-23")
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 2)


class OctoberControlTests(CompanyAUploadHarness):
    def setUp(self):
        super().setUp()
        self.install_october_mapping()

    def evidence_path(self, target="2026-10-01"):
        return self.state / "conversion_preflight" / f"company_a_sales_batch_{target}.json"

    def approve(self, evidence):
        manifest = build_posting_manifest(
            json.loads(evidence.read_text()), realm=REALM, approved_by="Marvin", chat_approval_ref="chat yes",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        os.environ["COMPANY_A_POSTING_APPROVAL_FILE"] = str(path)
        return path

    def test_october_requires_manifest_and_posts_nothing_without_it(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        code, posts, _ = self.run_upload(csv_path, "2026-10-01")
        self.assertEqual(code, 1)
        self.assertEqual(posts, [])

    def test_dry_run_evidence_then_manifest_allows_exact_post_to_approved_inventory_id(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        code, posts, calls = self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.assertEqual((code, posts), (0, []))
        self.assertFalse([c for c in calls if c[0] == "POST"])
        evidence = json.loads(self.evidence_path().read_text())
        self.assertTrue(evidence["complete"])
        self.assertEqual(len(evidence["payloads"]), 1)
        self.approve(self.evidence_path())
        code, posts, _ = self.run_upload(csv_path, "2026-10-01")
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 1)
        self.assertIn("requestid=", posts[0]["url"])
        item_ids = {l["SalesItemLineDetail"]["ItemRef"]["value"] for l in posts[0]["payload"]["Line"]
                    if l.get("DetailType") == "SalesItemLineDetail"}
        self.assertEqual(item_ids, {"90001"})
        self.assertFalse(item_ids & ({"15030"} | LEGACY_ITEM_IDS))

    def test_manifest_for_different_payload_is_refused(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.approve(self.evidence_path())
        changed = self.transform_to_csv(self.october_raw(qty=3, total=360000), "2026-10-01")
        code, posts, _ = self.run_upload(changed, "2026-10-01")
        self.assertEqual((code, posts), (1, []))

    def test_october_refund_or_zero_line_fails_day_before_any_post(self):
        raw = pd.concat([self.october_raw(), self.october_raw(qty=-1, total=-120000)], ignore_index=True)
        raw.loc[1, "Tender"] = "Transfer"
        csv_path = self.transform_to_csv(raw, "2026-10-01")
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(map(str, a)))):
            code, posts, _ = self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.assertEqual((code, posts), (1, []))
        text = "\n".join(buf)
        self.assertIn("refused before any POST", text)
        self.assertIn("Non-positive sale quantity", text)
        self.assertFalse(json.loads(self.evidence_path().read_text())["complete"])
        with self.assertRaises(ValueError):
            build_posting_manifest(json.loads(self.evidence_path().read_text()), realm=REALM,
                                   approved_by="x", chat_approval_ref="y",
                                   expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

    def test_october_failure_writes_hold_and_blocks_until_cleared(self):
        raw = pd.concat([self.october_raw(), self.october_raw()], ignore_index=True)
        raw.loc[1, "Tender"] = "Transfer"
        csv_path = self.transform_to_csv(raw, "2026-10-01")
        self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.approve(self.evidence_path())
        code, posts, calls = self.run_upload(csv_path, "2026-10-01", post_status=lambda p: 400)
        self.assertEqual(code, 1)
        self.assertEqual(posts, [])
        self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)  # stopped after the first failure
        self.assertTrue(posting_hold_path().exists())
        code, posts, _ = self.run_upload(csv_path, "2026-10-01")
        self.assertEqual((code, posts), (1, []))  # hold blocks October
        archived = clear_posting_hold(approved_by="Marvin", reason="reconciled; QBO rejected, nothing posted")
        self.assertTrue(archived.exists())
        self.assertFalse(posting_hold_path().exists())
        # Retry after a confirmed 4xx gets a fresh requestid and posts.
        code, posts, _ = self.run_upload(csv_path, "2026-10-01")
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 2)

    def test_october_existing_receipt_must_match_exactly(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        doc = pd.read_csv(csv_path)["*SalesReceiptNo"].iloc[0]
        # Fake QBO returns no receipt for the verification query -> day refused.
        code, posts, _ = self.run_upload(csv_path, "2026-10-01", existing={doc})
        self.assertEqual((code, posts), (1, []))

    def test_october_never_uses_catch_all_even_if_csv_is_tampered(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        frame = pd.read_csv(csv_path)
        frame.loc[:, "Item(Product/Service)"] = "AKP-UNMAPPED-EPOS-SALES"
        with self.assertRaises(ValueError):
            validate_upload_frame(frame, self.config)
        legacy = frame.drop(columns=["_Conversion Proof"])
        with self.assertRaisesRegex(ValueError, "October"):
            validate_upload_frame(legacy, self.config)

    def test_october_legacy_inventory_name_not_in_approved_list_is_refused(self):
        csv_path = self.transform_to_csv(self.october_raw(), "2026-10-01")
        frame = pd.read_csv(csv_path)
        frame.loc[:, "Item(Product/Service)"] = "REDBULL WATERMELON"
        with self.assertRaises(ValueError):
            validate_upload_frame(frame, self.config)


class ControlPrimitivesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch("code_scripts.paths.STATE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_controlled_dates(self):
        self.assertFalse(is_controlled_business_date("2026-09-30"))
        self.assertTrue(is_controlled_business_date("2026-10-01"))
        self.assertTrue(is_controlled_business_date("garbage"))
        self.assertTrue(is_controlled_business_date(None))
        # fail_closed_from can move earlier, never later.
        self.assertTrue(is_controlled_business_date("2026-09-30", "2026-09-15"))
        self.assertTrue(is_controlled_business_date("2026-10-01", "2027-01-01"))

    def test_hold_only_for_october_and_clear_archives(self):
        require_reconciliation_match("company_a", {"status": "MISMATCH"}, business_date="2026-09-30")
        require_reconciliation_match("company_a", {"status": "NOT RUN"}, business_date="2026-09-01")
        self.assertFalse(posting_hold_path().exists())
        with self.assertRaises(RuntimeError):
            require_reconciliation_match("company_a", {"status": "MISMATCH"}, business_date="2026-10-02")
        self.assertTrue(posting_hold_path().exists())
        with self.assertRaises(ValueError):
            operations_controls.assert_no_posting_hold()
        with self.assertRaises(ValueError):
            clear_posting_hold(approved_by="", reason="x")
        archived = clear_posting_hold(approved_by="Marvin", reason="reconciled")
        self.assertIn("cleared", json.loads(archived.read_text()))
        operations_controls.assert_no_posting_hold()

    def test_clear_hold_cli(self):
        posting_hold_path().write_text("{}")
        self.assertEqual(operations_controls._cli(["clear-hold", "--approved-by", "M", "--reason", "ok"]), 0)
        self.assertFalse(posting_hold_path().exists())

    def test_failed_receipt_is_retryable_with_new_request_id(self):
        q = ProposalQueue(self.root / "q.sqlite")
        payload = {"DocNumber": "SR-1", "TxnDate": "2026-10-01", "Line": [1]}
        rid = q.propose(REALM, "SalesReceipt", "SR-1:2026-10-01", payload, {"e": 1})
        first = q.http_request_id(rid)
        q.record_result(rid, "UNKNOWN", detail="timeout")
        # Same payload after an uncertain outcome: same requestid (QBO de-duplicates).
        self.assertEqual(q.propose(REALM, "SalesReceipt", "SR-1:2026-10-01", payload, {"e": 1}), rid)
        self.assertEqual(q.http_request_id(rid), first)
        # Different payload while outcome is unresolved: refused.
        with self.assertRaisesRegex(ValueError, "mark-failed"):
            q.propose(REALM, "SalesReceipt", "SR-1:2026-10-01", {**payload, "Line": [2]}, {"e": 1})
        q.mark_failed(rid, reason="confirmed absent in QBO", approved_by="Marvin")
        q.propose(REALM, "SalesReceipt", "SR-1:2026-10-01", {**payload, "Line": [2]}, {"e": 1})
        self.assertNotEqual(q.http_request_id(rid), first)
        q.record_result(rid, "POSTED", qbo_id="99")
        with self.assertRaises(ValueError):
            q.propose(REALM, "SalesReceipt", "SR-1:2026-10-01", {**payload, "Line": [3]}, {"e": 1})
        with self.assertRaises(ValueError):
            q.mark_failed(rid, reason="x", approved_by="y")

    def test_manifest_builder_round_trips_with_require_posting_approval(self):
        payload = {"DocNumber": "SR-1", "TxnDate": "2026-10-01"}
        evidence = {"realm": REALM, "complete": True, "payloads": [
            {"payload": payload, "sha256": payload_digest(payload), "requires_approval": True},
            {"payload": {"DocNumber": "SR-0", "TxnDate": "2026-09-30"}, "requires_approval": False},
        ]}
        manifest = build_posting_manifest(evidence, realm=REALM, approved_by="M", chat_approval_ref="yes",
                                          expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
        self.assertEqual(manifest["payload_sha256"], [payload_digest(payload)])
        path = self.root / "m.json"
        path.write_text(json.dumps(manifest))
        require_posting_approval(path, payload, REALM, "SalesReceipt")
        tampered = {**evidence, "payloads": [{"payload": payload, "sha256": "0" * 64}]}
        with self.assertRaises(ValueError):
            build_posting_manifest(tampered, realm=REALM, approved_by="M", chat_approval_ref="yes",
                                   expires_at=manifest["expires_at"])
        with self.assertRaises(ValueError):
            build_posting_manifest(evidence, realm=REALM, approved_by="M", chat_approval_ref="yes",
                                   expires_at="2026-10-01T00:00:00")  # no timezone

    def test_send_sales_receipt_pre_october_needs_no_manifest(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(qbo_upload, "_make_qbo_request",
                               return_value=FakeResponse({"SalesReceipt": {"Id": "1"}})) as request:
            qbo_upload.send_sales_receipt({"DocNumber": "SR-1", "TxnDate": "2026-09-30"}, object(), REALM)
        self.assertNotIn("requestid", request.call_args.args[1])
        self.assertFalse((self.root / "company_a_proposals.sqlite").exists())

    def test_send_sales_receipt_october_without_manifest_never_posts(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(qbo_upload, "_make_qbo_request") as request:
            for txn_date in ("2026-10-01", "", None):
                with self.subTest(txn_date=txn_date), self.assertRaises(ValueError):
                    qbo_upload.send_sales_receipt({"DocNumber": "SR-1", "TxnDate": txn_date}, object(), REALM)
        request.assert_not_called()


class SalesPathInvariantTests(unittest.TestCase):
    def test_company_a_create_inventory_item_stays_refused(self):
        config = CompanyConfig(COMPANY_A_JSON)
        with self.assertRaises(RuntimeError):
            qbo_upload.create_inventory_item("X", "GROCERY", 1.0, 1.0, config, object(), REALM, {}, {})
        with self.assertRaises(RuntimeError):
            qbo_upload.get_or_create_item_id("X", object(), REALM, config, {})

    def test_sales_upload_source_has_no_inventory_adjustment_or_quantity_apply(self):
        source = Path(qbo_upload.__file__).read_text()
        self.assertNotIn("inventoryadjustment", source.lower())


if __name__ == "__main__":
    unittest.main()

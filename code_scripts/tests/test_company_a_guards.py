"""Company A (AKPONORA) hard refusals on legacy / destructive tooling.

Each guard is proven both ways: company_a (or its realm) is refused, another company is not.
All QBO HTTP is mocked; ``_make_qbo_request`` is patched to fail loudly if reached unexpectedly.
"""
from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_scripts import company_a_guard as guard
from code_scripts import qbo_upload
from code_scripts.company_a_guard import COMPANY_A_REALM_ID, CompanyAProtectedError
from code_scripts.scripts import qbo_delete_sales_receipts as delete_sr
from code_scripts.scripts import qbo_inv_manager as inv_mgr
from code_scripts.scripts.bills import qbo_import_bills as import_bills
from code_scripts.scripts.invoice import qbo_import_invoices as import_invoices

COMPANIES = ["company_a", "company_b"]


def _resp(status: int = 200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = ""
    return r


def _no_live_qbo(*_a, **_k):
    raise AssertionError("live QBO call attempted in a unit test")


def _run(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


class GuardHelperTests(unittest.TestCase):
    def test_detects_company_a_by_key_or_realm(self):
        self.assertTrue(guard.is_company_a("company_a"))
        self.assertTrue(guard.is_company_a("COMPANY_A"))
        self.assertTrue(guard.is_company_a(None, COMPANY_A_REALM_ID))
        self.assertTrue(guard.is_company_a("company_b", COMPANY_A_REALM_ID))
        self.assertFalse(guard.is_company_a("company_b", "123"))
        self.assertFalse(guard.is_company_a(None, None))

    def test_assert_not_company_a_message_cites_agents_md(self):
        with self.assertRaisesRegex(CompanyAProtectedError, "AGENTS.md"):
            guard.assert_not_company_a("x", company_key="company_a")
        guard.assert_not_company_a("x", company_key="company_b", realm_id="123")


# ---------------------------------------------------------------------------
# S1 qbo_delete_sales_receipts
# ---------------------------------------------------------------------------


class DeleteSalesReceiptsGuardTests(unittest.TestCase):
    def _patches(self, cfg):
        return [
            mock.patch.object(delete_sr, "get_available_companies", return_value=COMPANIES),
            mock.patch.object(delete_sr, "load_company_config", return_value=cfg),
            mock.patch.object(delete_sr, "verify_realm_match"),
            mock.patch.object(delete_sr, "TokenManager"),
        ]

    def _main(self, argv, cfg):
        patches = self._patches(cfg)
        mocks = [p.start() for p in patches]
        try:
            return (*_run(delete_sr.main, argv), mocks)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_company_a_refused_in_every_mode(self):
        cfg = SimpleNamespace(company_key="company_a", realm_id=COMPANY_A_REALM_ID)
        for extra in (["--dry-run"], []):
            with mock.patch.object(delete_sr, "_make_qbo_request", side_effect=_no_live_qbo):
                rc, _out, err, mocks = self._main(
                    ["--company", "company_a", "--target-date", "2026-01-15", *extra], cfg
                )
            self.assertEqual(rc, 2)
            self.assertIn("AGENTS.md", err)
            mocks[1].assert_not_called()  # load_company_config
            mocks[3].assert_not_called()  # TokenManager

    def test_company_a_realm_under_other_key_refused(self):
        cfg = SimpleNamespace(company_key="company_b", realm_id=COMPANY_A_REALM_ID)
        with mock.patch.object(delete_sr, "_make_qbo_request", side_effect=_no_live_qbo):
            rc, _out, err, mocks = self._main(["--company", "company_b", "--target-date", "2026-01-15"], cfg)
        self.assertEqual(rc, 2)
        mocks[3].assert_not_called()

    def test_delete_helper_refuses_company_a_realm(self):
        with mock.patch.object(delete_sr, "_make_qbo_request", side_effect=_no_live_qbo):
            with self.assertRaises(CompanyAProtectedError):
                delete_sr._delete_sales_receipt(mock.Mock(), COMPANY_A_REALM_ID, "1", "0")

    def test_other_company_still_deletes(self):
        cfg = SimpleNamespace(company_key="company_b", realm_id="realm-b")
        receipts = [{"Id": "9", "DocNumber": "SR-1", "TxnDate": "2026-01-15", "SyncToken": "0"}]
        with mock.patch.object(delete_sr, "_query_sales_receipts", return_value=receipts), \
             mock.patch.object(delete_sr, "_make_qbo_request", return_value=_resp(200)) as req:
            rc, out, _err, _m = self._main(["--company", "company_b", "--target-date", "2026-01-15"], cfg)
        self.assertEqual(rc, 0)
        self.assertIn("Deleted 1", out)
        self.assertIn("operation=delete", req.call_args.args[1])


# ---------------------------------------------------------------------------
# S2 qbo_upload legacy item resolution + Category create
# ---------------------------------------------------------------------------


class _Cfg(SimpleNamespace):
    def get_qbo_config(self):
        return {"default_item_id": "1"}


def _resolve_all(config, realm_id):
    return qbo_upload.resolve_all_unique_items(
        [], {}, {}, config, mock.Mock(), realm_id, None, {}, {}, None, {}, set(), False, False, None, None, None
    )


class QboUploadLegacyResolutionGuardTests(unittest.TestCase):
    def test_resolve_all_unique_items_refuses_company_a(self):
        with mock.patch.object(qbo_upload, "_make_qbo_request", side_effect=_no_live_qbo):
            with self.assertRaises(CompanyAProtectedError):
                _resolve_all(_Cfg(company_key="company_a", product_conversion_enabled=True), COMPANY_A_REALM_ID)
            with self.assertRaises(CompanyAProtectedError):
                _resolve_all(_Cfg(company_key="company_b", product_conversion_enabled=False), COMPANY_A_REALM_ID)

    def test_resolve_all_unique_items_refuses_conversion_mode(self):
        with self.assertRaisesRegex(RuntimeError, "product conversion mode"):
            _resolve_all(_Cfg(company_key="company_b", product_conversion_enabled=True), "realm-b")

    def test_resolve_all_unique_items_allowed_for_other_company(self):
        stats = _resolve_all(_Cfg(company_key="company_b", product_conversion_enabled=False), "realm-b")
        self.assertEqual(stats["items_created"], 0)

    def test_category_create_refused_for_company_a_realm(self):
        calls = []

        def fake(method, url, _tm, **_k):
            calls.append(method)
            if method == "GET":
                return _resp(200, {"QueryResponse": {}})
            raise AssertionError("Category POST attempted for Company A")

        with mock.patch.object(qbo_upload, "_make_qbo_request", side_effect=fake):
            with self.assertRaises(CompanyAProtectedError):
                qbo_upload.get_or_create_item_category_id(mock.Mock(), COMPANY_A_REALM_ID, "DRINKS")
        self.assertEqual(calls, ["GET"])

    def test_category_lookup_still_reuses_existing_for_company_a(self):
        found = _resp(200, {"QueryResponse": {"Item": [{"Id": "77", "Name": "DRINKS", "Active": True}]}})
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=found):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    qbo_upload.get_or_create_item_category_id(mock.Mock(), COMPANY_A_REALM_ID, "DRINKS"), "77"
                )

    def test_category_create_allowed_for_other_realm(self):
        def fake(method, url, _tm, **_k):
            if method == "GET":
                return _resp(200, {"QueryResponse": {}})
            return _resp(200, {"Item": {"Id": "88"}})

        with mock.patch.object(qbo_upload, "_make_qbo_request", side_effect=fake):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(qbo_upload.get_or_create_item_category_id(mock.Mock(), "realm-b", "DRINKS"), "88")


# ---------------------------------------------------------------------------
# S3 qbo_inv_manager set-invstart*
# ---------------------------------------------------------------------------


class InvManagerSetInvStartGuardTests(unittest.TestCase):
    def _args(self, company, **kw):
        base = dict(
            company=company,
            item_id="7220",
            date="2026-01-01",
            cutoff_date="2026-01-01",
            new_date="2026-01-01",
            maxresults=1000,
            no_include_inactive=False,
            csv="missing.csv",
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_company_a_refused_for_all_set_invstart_commands(self):
        cmds = (inv_mgr.cmd_set_invstart, inv_mgr.cmd_set_invstart_bulk, inv_mgr.cmd_set_invstart_from_csv)
        with mock.patch.object(inv_mgr, "patch_item_inv_start_date") as patch_mock, \
             mock.patch.object(inv_mgr, "_query_inventory_items_paginated") as query_mock, \
             mock.patch.object(inv_mgr, "_make_qbo_request", side_effect=_no_live_qbo):
            for cmd in cmds:
                rc, _out, err = _run(cmd, self._args("company_a"), mock.Mock(), COMPANY_A_REALM_ID)
                self.assertEqual(rc, 1, cmd.__name__)
                self.assertIn("AGENTS.md", err)
                # realm-only detection too
                rc, _out, _err = _run(cmd, self._args("company_b"), mock.Mock(), COMPANY_A_REALM_ID)
                self.assertEqual(rc, 1, cmd.__name__)
        patch_mock.assert_not_called()
        query_mock.assert_not_called()

    def test_other_company_set_invstart_still_works(self):
        with mock.patch.object(inv_mgr, "patch_item_inv_start_date", return_value=(True, "2026-02-01", "")) as patch_mock:
            rc, _out, _err = _run(inv_mgr.cmd_set_invstart, self._args("company_b"), mock.Mock(), "realm-b")
        self.assertEqual(rc, 0)
        patch_mock.assert_called_once()

    def test_other_company_set_invstart_bulk_still_works(self):
        with mock.patch.object(inv_mgr, "_query_inventory_items_paginated", return_value=[]) as query_mock:
            rc, out, _err = _run(inv_mgr.cmd_set_invstart_bulk, self._args("company_b"), mock.Mock(), "realm-b")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to patch", out)
        query_mock.assert_called_once()

    def test_other_company_set_invstart_from_csv_reaches_csv_check(self):
        rc, _out, err = _run(
            inv_mgr.cmd_set_invstart_from_csv, self._args("company_b", csv="/nonexistent/x.csv"), mock.Mock(), "realm-b"
        )
        self.assertEqual(rc, 1)
        self.assertIn("CSV not found", err)


# ---------------------------------------------------------------------------
# S6 qbo_import_invoices
# ---------------------------------------------------------------------------


class InvoiceImportGuardTests(unittest.TestCase):
    def test_refusal_function(self):
        f = import_invoices.company_a_live_invoice_refusal
        self.assertIsNotNone(f("company_a", COMPANY_A_REALM_ID, ["2026-10-01"], live=True))
        self.assertIsNotNone(f("company_b", COMPANY_A_REALM_ID, ["2026-11-05"], live=True))
        self.assertIsNone(f("company_a", COMPANY_A_REALM_ID, ["2026-09-30"], live=True))
        self.assertIsNone(f("company_a", COMPANY_A_REALM_ID, ["2026-10-01"], live=False))
        self.assertIsNone(f("company_b", "realm-b", ["2026-10-01"], live=True))
        msg = f("company_a", COMPANY_A_REALM_ID, ["2026-10-02"], live=True)
        self.assertIn("AGENTS.md", msg)
        self.assertIn("AKP-", msg)

    def _write_csv(self, td, date):
        path = Path(td) / "inv.csv"
        path.write_text(
            "Customer,InvoiceDate,ItemName,Qty,Rate,Amount\n" f"ACME,{date},WIDGET,1,100,100\n",
            encoding="utf-8",
        )
        return path

    def test_company_a_live_october_refused_before_qbo(self):
        cfg = SimpleNamespace(company_key="company_a", realm_id=COMPANY_A_REALM_ID, slack_webhook_url=None)
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, "2026-10-02")
            with mock.patch.object(import_invoices, "load_company_config", return_value=cfg), \
                 mock.patch.object(import_invoices, "verify_realm_match") as vrm, \
                 mock.patch.object(import_invoices, "TokenManager") as tm, \
                 mock.patch.object(import_invoices, "_make_qbo_request", side_effect=_no_live_qbo):
                rc, _out, err = _run(import_invoices.main, ["--company", "company_a", "--csv", str(path)])
        self.assertEqual(rc, 2)
        self.assertIn("2026-10-01", err)
        vrm.assert_not_called()
        tm.assert_not_called()

    def test_company_a_dry_run_and_pre_october_pass_the_guard(self):
        cfg = SimpleNamespace(company_key="company_a", realm_id=COMPANY_A_REALM_ID, slack_webhook_url=None)
        cases = [("2026-10-02", ["--dry-run"]), ("2026-09-15", [])]
        for date, extra in cases:
            with tempfile.TemporaryDirectory() as td:
                path = self._write_csv(td, date)
                with mock.patch.object(import_invoices, "load_company_config", return_value=cfg), \
                     mock.patch.object(import_invoices, "verify_realm_match", side_effect=SystemExit(99)):
                    with self.assertRaises(SystemExit) as ctx:
                        _run(import_invoices.main, ["--company", "company_a", "--csv", str(path), *extra])
            self.assertEqual(ctx.exception.code, 99, (date, extra))


# ---------------------------------------------------------------------------
# S7 qbo_import_bills
# ---------------------------------------------------------------------------


class BillImportGuardTests(unittest.TestCase):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(import_bills, "get_available_companies", return_value=COMPANIES), \
             mock.patch.object(import_bills, "load_company_config") as cfg_mock, \
             mock.patch.object(import_bills, "TokenManager") as tm_mock, \
             mock.patch.object(import_bills, "_make_qbo_request", side_effect=_no_live_qbo), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                import_bills.main(argv)
        return ctx.exception.code, err.getvalue(), cfg_mock, tm_mock

    def test_refusal_function(self):
        f = import_bills.company_a_create_refusal
        self.assertIsNotNone(f("company_a", None, True))
        self.assertIsNotNone(f("company_b", COMPANY_A_REALM_ID, True))
        self.assertIsNone(f("company_a", COMPANY_A_REALM_ID, False))
        self.assertIsNone(f("company_b", "realm-b", True))
        self.assertIn("bills_sync.py", f("company_a", None, True))

    def test_company_a_create_refused(self):
        code, err, cfg_mock, tm_mock = self._main(
            ["--company", "company_a", "--all", "--create", "--headers", "/nonexistent/h.csv"]
        )
        self.assertEqual(code, 2)
        self.assertIn("bills_sync.py", err)
        cfg_mock.assert_not_called()
        tm_mock.assert_not_called()

    def test_company_a_dry_run_and_other_company_create_pass_the_guard(self):
        for company, mode in (("company_a", "--dry-run"), ("company_b", "--create")):
            code, err, _cfg, _tm = self._main(
                ["--company", company, "--all", mode, "--headers", "/nonexistent/h.csv"]
            )
            self.assertEqual(code, 1, (company, mode))
            self.assertIn("headers file not found", err)


if __name__ == "__main__":
    unittest.main()

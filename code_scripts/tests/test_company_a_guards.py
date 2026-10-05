"""Company A (AKPONORA) hard refusals on legacy / destructive tooling.

Each guard is proven both ways: company_a (or its realm) is refused, another company is not.
All QBO HTTP is mocked; ``_make_qbo_request`` is patched to fail loudly if reached unexpectedly.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from code_scripts import company_a_guard as guard
from code_scripts import qbo_upload
from code_scripts.company_a_guard import COMPANY_A_REALM_ID, CompanyAProtectedError


def _resp(status: int = 200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = ""
    return r


def _no_live_qbo(*_a, **_k):
    raise AssertionError("live QBO call attempted in a unit test")


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


if __name__ == "__main__":
    unittest.main()

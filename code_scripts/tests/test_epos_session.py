import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_scripts import epos_session as es
from code_scripts.scripts.akponora_cutover import _common


class Context:
    def __init__(self, cookies=()):
        self.saved, self.added = list(cookies), []

    def cookies(self):
        return self.saved

    def add_cookies(self, cookies):
        self.added += cookies


class Page:
    """Back-office page (``report=True``) or EPOS login form."""

    def __init__(self, report, cookies=()):
        self.context, self.report, self.filled = Context(cookies), report, []
        self.url = "https://www.eposnowhq.com/Pages/Reporting/SageReport.aspx" if report else \
            "https://login.eposnowhq.com/login?login_challenge=x"

    def locator(self, sel):
        page = self
        return mock.Mock(count=lambda: 1 if page.report else 0,
                         or_=lambda other: mock.Mock(first=mock.Mock(wait_for=lambda timeout: None)))

    def get_by_role(self, role, name):
        page = self
        return mock.Mock(count=lambda: 0 if page.report else 1,
                         fill=lambda v: page.filled.append(name), click=lambda: page.filled.append(name))

    def wait_for_load_state(self, *a, **k):
        pass


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"STATE_ROOT": str(self.tmp), es.ENV: "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_saved_cookies_are_owner_only_and_reused_within_the_run(self):
        es.save(Context([{"name": "ASP.NET_SessionId", "value": "v", "domain": "www.eposnowhq.com", "path": "/"}]),
                "company_a")
        p = es.path("company_a")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        ctx = Context()
        self.assertTrue(es.restore(ctx, "company_a"))
        self.assertEqual(ctx.added[0]["name"], "ASP.NET_SessionId")
        self.assertNotIn("password", p.read_text().lower())

    def test_old_missing_or_switched_off_is_not_reused(self):
        es.save(Context([{"name": "a", "value": "b", "domain": "x", "path": "/"}]), "company_a")
        old = time.time() - es.MAX_AGE - 5
        os.utime(es.path("company_a"), (old, old))
        self.assertFalse(es.restore(Context(), "company_a"))
        self.assertFalse(es.restore(Context(), "company_b"))
        es.save(Context([{"name": "a", "value": "b", "domain": "x", "path": "/"}]), "company_a")
        with mock.patch.dict(os.environ, {es.ENV: ""}):
            self.assertFalse(es.restore(Context(), "company_a"))
            es.clear("company_a")
            es.save(Context([{"name": "a"}]), "company_a")
            self.assertFalse(es.path("company_a").exists())

    def test_login_page_is_detected(self):
        self.assertFalse(es.on_login_page(Page(report=True)))
        self.assertTrue(es.on_login_page(Page(report=False)))


class EposLoginTests(SessionTests):
    cfg = SimpleNamespace(company_key="company_a", epos_username="u", epos_password="p")

    def login(self, page):
        with mock.patch.object(_common, "goto_retry"):
            _common.epos_login(page, self.cfg)
        return page

    def test_first_step_logs_in_and_later_steps_reuse_it(self):
        first = self.login(Page(report=False, cookies=[{"name": "s", "value": "1", "domain": "w", "path": "/"}]))
        self.assertIn("Password", first.filled)
        later = self.login(Page(report=True))
        self.assertEqual(later.filled, [])
        self.assertEqual(later.context.added[0]["name"], "s")

    def test_an_expired_saved_login_logs_in_again(self):
        es.save(Context([{"name": "s", "value": "1", "domain": "w", "path": "/"}]), "company_a")
        page = self.login(Page(report=False))
        self.assertIn("Password", page.filled)

    def test_without_the_daily_run_flag_nothing_is_saved(self):
        with mock.patch.dict(os.environ, {es.ENV: ""}):
            page = self.login(Page(report=False, cookies=[{"name": "s"}]))
        self.assertIn("Password", page.filled)
        self.assertFalse(es.path("company_a").exists())


if __name__ == "__main__":
    unittest.main()

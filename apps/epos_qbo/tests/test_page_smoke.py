"""Every portal page opens (no server error) for a superuser, on empty state and with both companies.

Guards the 5 Oct 2026 clean-up: removed tools must not leave a page that crashes."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from apps.epos_qbo.models import CompanyConfigRecord, RunArtifact, RunJob

CONFIG = {"epos": {"username_env_key": "EPOS_USERNAME_A", "password_env_key": "EPOS_PASSWORD_A"}, "qbo": {"realm_id": "1"}}


def _get_patterns(resolver, prefix=""):
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from _get_patterns(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


class PageSmokeTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oiat_smoke_", dir=os.getenv("TMPDIR") or None)).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch("code_scripts.paths.STATE_ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        for key, name in (("company_a", "AKPONORA VENTURES LTD."), ("company_b", "GOLDPLATES FEASTHOUSE LTD.")):
            CompanyConfigRecord.objects.create(company_key=key, display_name=name, is_active=True,
                                               config_json={"company_key": key, "display_name": name, **CONFIG})
        self.client.force_login(User.objects.create_superuser("smoke", "", "pw"))

    def test_every_get_page_opens(self):
        checked = []
        for route, pattern in _get_patterns(get_resolver()):
            if route.startswith(("admin/", "static/", "media/", "logout/")) or "<" in route.replace("<slug:company_key>", ""):
                continue
            for key in (("company_a", "company_b") if "<slug:company_key>" in route else (None,)):
                url = "/" + route.replace("<slug:company_key>", key or "")
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertLess(response.status_code, 500, url)
                    checked.append((url, response.status_code))
        self.assertGreater(len(checked), 20, checked)
        for tab in ("sales", "purchases", "products", "suppliers", "deposits", "settings"):
            for key in ("company_a", "company_b"):
                with self.subTest(tab=tab, key=key):
                    self.assertLess(self.client.get(f"/epos-qbo/companies/{key}/", {"tab": tab}).status_code, 500)

    def test_old_inventory_runs_in_history_still_open(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_INVENTORY_PIPELINE, company_key="company_a",
                                    status=RunJob.STATUS_SUCCEEDED, inventory_options_json={"mode": "audit_only"})
        RunArtifact.objects.create(company_key="company_a", run_job=job, source_path="/x/inventory_pipeline_1.json",
                                   upload_stats_json={"report_type": "inventory_pipeline"})
        for url in (f"/epos-qbo/runs/{job.id}/", "/epos-qbo/runs/", "/epos-qbo/", "/epos-qbo/companies/company_a/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)


class RunEntryPointTests(TestCase):
    """Marvin, 6 Oct: 'Run…' must be easy to find, prefilled from context, and say why when not allowed."""

    def setUp(self):
        for key, name in (("company_a", "AKPONORA VENTURES LTD."), ("company_b", "GOLDPLATES FEASTHOUSE LTD.")):
            CompanyConfigRecord.objects.create(company_key=key, display_name=name, is_active=True,
                                               config_json={"company_key": key, "display_name": name, **CONFIG})

    def test_run_panel_is_visible_and_prefilled(self):
        self.client.force_login(User.objects.create_superuser("boss", "", "pw"))
        html = self.client.get("/epos-qbo/runs/", {"run_step": "uf", "run_date": "2026-10-04"}).content.decode()
        self.assertIn("Run something now", html)
        self.assertIn('<option value="uf" selected>Banking (funds allocation)</option>', html)
        self.assertIn('value="2026-10-04"', html)
        self.assertIn("Other companies · Sales sync", html)
        company = self.client.get("/epos-qbo/companies/company_a/", {"tab": "deposits"}).content.decode()
        self.assertIn("?run_step=uf#run-a-day", company)

    def test_without_permission_the_page_says_why(self):
        self.client.force_login(User.objects.create_user("viewer", "", "pw"))
        html = self.client.get("/epos-qbo/runs/").content.decode()
        self.assertIn("Can trigger runs", html)
        self.assertNotIn("Review run", html)

    def test_confirm_title_names_the_step(self):
        self.client.force_login(User.objects.create_superuser("boss", "", "pw"))
        from unittest import mock as _m
        from datetime import date as _d
        with _m.patch("code_scripts.akponora_ops.daily_run.last_closed_business_date", return_value=_d(2026, 10, 5)):
            html = self.client.get("/epos-qbo/attention/confirm/", {"action": "daily", "date": "2026-10-04",
                                                                     "only": "uf", "mode": "dry"}).content.decode()
        self.assertIn("Preview banking (funds allocation) for Akponora, 4 October 2026", html)

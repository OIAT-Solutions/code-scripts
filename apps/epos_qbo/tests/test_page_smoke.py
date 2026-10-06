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
    """Marvin, 6 Oct: one Run button and one dialog for every company; prefilled from context."""

    def setUp(self):
        for key, name in (("company_a", "AKPONORA VENTURES LTD."), ("company_b", "GOLDPLATES FEASTHOUSE LTD.")):
            CompanyConfigRecord.objects.create(company_key=key, display_name=name, is_active=True,
                                               config_json={"company_key": key, "display_name": name, **CONFIG})
        from datetime import date as _d
        p = mock.patch("code_scripts.akponora_ops.daily_run.last_closed_business_date", return_value=_d(2026, 10, 5))
        p.start()
        self.addCleanup(p.stop)

    def boss(self):
        self.client.force_login(User.objects.create_superuser("boss", "", "pw"))

    def test_one_dialog_every_company_equal(self):
        self.boss()
        r = self.client.get("/epos-qbo/runs/")
        html = r.content.decode()
        self.assertEqual(html.count("data-open-run>"), 1)
        self.assertIn('id="run-dialog-title" class="text-lg font-semibold">Start a run</h2>', html)
        self.assertIn("Choose a company", html)
        self.assertNotIn("Other companies", html)
        opts = r.context["run_options"]
        self.assertEqual(opts["company_a"]["preview"], True)
        self.assertEqual(opts["company_b"]["steps"], [["sales", "Sales"]])
        self.assertFalse(r.context["run_prefill"]["open"])

    def test_context_link_opens_the_dialog_prefilled(self):
        self.boss()
        r = self.client.get("/epos-qbo/runs/", {"run_company": "company_a", "run_step": "uf", "run_date": "2026-10-04"})
        self.assertEqual(r.context["run_prefill"], {"company": "company_a", "step": "uf", "day": "2026-10-04", "open": True})
        company = self.client.get("/epos-qbo/companies/company_a/", {"tab": "deposits"}).content.decode()
        self.assertIn("?run_company=company_a&amp;run_step=uf", company)

    def test_without_permission_the_button_says_why(self):
        self.client.force_login(User.objects.create_user("viewer", "", "pw"))
        html = self.client.get("/epos-qbo/runs/").content.decode()
        self.assertIn("Can trigger runs", html)
        self.assertNotIn('id="run-dialog"', html)

    def test_review_routes_per_company(self):
        self.boss()
        r = self.client.get("/epos-qbo/runs/review/", {"company": "company_a", "date": "2026-10-04", "only": "uf", "mode": "dry"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/epos-qbo/attention/confirm/?action=daily&date=2026-10-04&only=uf&mode=dry", r["Location"])
        html = self.client.get(r["Location"]).content.decode()
        self.assertIn("Preview banking (funds allocation) for Akponora, 4 October 2026", html)
        r = self.client.get("/epos-qbo/runs/review/", {"company": "company_b", "date": "2026-10-04", "only": "sales", "mode": "post"})
        html = r.content.decode()
        self.assertIn("Run sales for GOLDPLATES FEASTHOUSE LTD., 4 October 2026", html)
        self.assertIn('name="target_date" value="2026-10-04"', html)

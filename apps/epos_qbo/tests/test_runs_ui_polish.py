from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, RunArtifact, RunJob


class RunJobFriendlyIdTests(TestCase):
    def test_friendly_id_sales_all_companies(self):
        started = datetime(2026, 4, 28, 18, 0, tzinfo=dt_timezone.utc)
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, started_at=started)
        self.assertTrue(job.friendly_id.startswith("SAL-0428-1800-"))
        self.assertEqual(len(job.friendly_id.split("-")[-1]), 4)

    def test_friendly_title_and_internal_id_separation(self):
        started = datetime(2026, 4, 28, 18, 0, tzinfo=dt_timezone.utc)
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, started_at=started)
        self.assertIn("Sales Run SAL-0428-1800-", job.friendly_title)
        self.assertIn(str(job.id).split("-", 1)[0][:4].upper(), job.friendly_title)


class RunsAndRunDetailRenderingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="operator", password="pw12345")
        self.client.login(username="operator", password="pw12345")

    def test_daily_history_uses_business_outcome_and_preserves_detail_link(self):
        started = datetime(2026, 4, 28, 18, 0, tzinfo=dt_timezone.utc)
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, started_at=started, status=RunJob.STATUS_SUCCEEDED)
        response = self.client.get(reverse("epos_qbo:runs"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertNotIn(job.friendly_id, html)
        self.assertIn("Date not recorded", html)
        self.assertIn("Finished · check outcome", html)
        self.assertIn(reverse('epos_qbo:run-detail', args=[job.id]), html)

    def test_run_detail_shows_friendly_title_and_internal_id(self):
        started = datetime(2026, 4, 29, 14, 52, tzinfo=dt_timezone.utc)
        job = RunJob.objects.create(
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_a",
            started_at=started,
            status=RunJob.STATUS_SUCCEEDED,
        )
        response = self.client.get(reverse("epos_qbo:run-detail", kwargs={"job_id": job.id}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn(job.friendly_title, html)
        self.assertIn("Internal reference:", html)
        self.assertIn(str(job.id), html)
        self.assertIn("Target", html)

    def test_run_detail_uses_operator_friendly_artifact_labels(self):
        started = datetime(2026, 4, 28, 18, 0, tzinfo=dt_timezone.utc)
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, started_at=started, status=RunJob.STATUS_SUCCEEDED)
        RunArtifact.objects.create(
            run_job=job,
            company_key="company_a",
            kind=RunArtifact.KIND_SALES_UPLOAD,
            processed_at=started,
            source_path="/tmp/last_company_a_transform.json",
            source_hash="hash-1",
            reliability_status=RunArtifact.RELIABILITY_WARNING,
            upload_stats_json={},
        )
        response = self.client.get(reverse("epos_qbo:run-detail", kwargs={"job_id": job.id}))
        html = response.content.decode("utf-8")
        self.assertIn("Sales metadata", html)
        self.assertNotIn("Rolling (last_*)", html)
        self.assertNotIn("last_", html)

    def test_unknown_artifact_does_not_render_sales_metadata(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_INVENTORY_PIPELINE, company_key="company_a", status=RunJob.STATUS_SUCCEEDED)
        RunArtifact.objects.create(
            run_job=job,
            company_key="company_a",
            kind="other",
            processed_at=datetime(2026, 4, 29, 1, 39, tzinfo=dt_timezone.utc),
            source_path="/tmp/unknown_artifact.json",
            source_hash="e" * 64,
            reliability_status=RunArtifact.RELIABILITY_HIGH,
            upload_stats_json={"report_type": "unknown"},
        )
        html = self.client.get(reverse("epos_qbo:run-detail", kwargs={"job_id": job.id})).content.decode("utf-8")
        self.assertIn("Artifact", html)
        self.assertNotIn("Sales metadata", html)

    def test_sales_all_companies_run_detail_shows_target_all_companies(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, status=RunJob.STATUS_SUCCEEDED)
        html = self.client.get(reverse("epos_qbo:run-detail", kwargs={"job_id": job.id})).content.decode("utf-8")
        self.assertIn("Workflow", html)
        self.assertIn("Sales", html)
        self.assertIn("Target", html)
        self.assertIn("All companies", html)

    def test_sales_single_company_run_detail_prefers_company_display_name(self):
        CompanyConfigRecord.objects.create(company_key="company_a", display_name="ACME LTD.", config_json={})
        job = RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key="company_a", status=RunJob.STATUS_SUCCEEDED)
        html = self.client.get(reverse("epos_qbo:run-detail", kwargs={"job_id": job.id})).content.decode("utf-8")
        self.assertIn("Company: ACME LTD.", html)

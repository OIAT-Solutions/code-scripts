from datetime import date, datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, RunArtifact, RunJob, RunSchedule
from apps.epos_qbo.services import experience
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _summary, _step


class ExperienceTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 4, 12, tzinfo=ZoneInfo('UTC'))
        self.a = CompanyConfigRecord.objects.create(company_key='company_a', display_name='Akponora', config_json={})
        self.b = CompanyConfigRecord.objects.create(company_key='company_b', display_name='Goldplates', config_json={})
        self.user = User.objects.create_user(username='reader', password='test-only')
        self.client.force_login(self.user)

    def sales(self, day, stamp='run_170000Z', amount='100.00', dry=False, status='clean', sales_status='ok', mode='post', match='MATCH'):
        self.make_run(day, stamp, _summary(day, dry=dry, status=status, steps=[_step('sales', status=sales_status, counts={'mode':mode, 'reconcile_status':match, 'qbo_total':amount})]))

    def artifact(self, day, amount='200.00', job=None, match='MATCH', dry=False, company='company_b'):
        return RunArtifact.objects.create(company_key=company, target_date=day, run_job=job, source_path='fixture.json', source_hash=str(RunArtifact.objects.count()), reconcile_status=match, reconcile_qbo_total=amount, upload_stats_json={'dry_run':dry})

    def home(self, key=''):
        return experience.home_context(key, self.now)

    def test_preview_does_not_confirm_sales_or_hide_missing_day(self):
        self.sales('2026-10-03', dry=True)
        context = self.home('company_a')
        self.assertIsNone(context['home_rows'][0]['latest'])
        self.assertEqual(context['home_sales'], 'Not fully confirmed')
        self.assertEqual(context['home_missing'], 3)

    def test_latest_sales_day_does_not_hide_earlier_gap(self):
        self.sales('2026-10-01')
        self.sales('2026-10-03')
        row = self.home('company_a')['home_rows'][0]
        self.assertEqual(row['missing'], [date(2026,10,2)])
        self.assertEqual(row['tone'], 'danger')
        self.assertEqual(row['latest'], date(2026,10,3))

    def test_duplicate_attempts_do_not_double_count_money(self):
        for day in ('2026-10-01','2026-10-02','2026-10-03'):
            self.sales(day)
        self.sales('2026-10-03', stamp='run_180000Z')
        self.assertEqual(self.home('company_a')['home_sales'], '₦100.00')
        row = experience.daily_rows('company_a', date(2026,10,3))[0]
        self.assertEqual(len(row['attempts']), 2)
        self.assertEqual(row['amount'], Decimal('100.00'))

    def test_missing_amount_is_not_displayed_as_zero(self):
        self.sales('2026-10-03', amount=None)
        self.assertEqual(self.home('company_a')['home_sales'], 'Not fully confirmed')
        self.assertEqual(experience.daily_rows('company_a')[0]['amount_label'], 'Not recorded')

    def test_failed_retry_keeps_confirmed_day_and_explains_latest_issue(self):
        old = RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b', target_date=date(2026,10,3), status=RunJob.STATUS_SUCCEEDED)
        self.artifact(date(2026,10,3), job=old)
        RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b', target_date=date(2026,10,3), status=RunJob.STATUS_FAILED)
        rows = experience.daily_rows('company_b')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['confirmed'])
        self.assertTrue(rows[0]['latest_issue'])
        self.assertEqual(rows[0]['amount'], Decimal('200.00'))

    def test_successful_job_without_artifact_is_not_posted_sales(self):
        RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b', target_date=date(2026,10,3), status=RunJob.STATUS_SUCCEEDED)
        row = experience.daily_rows('company_b')[0]
        self.assertFalse(row['confirmed'])
        self.assertEqual(row['label'], 'Finished · check outcome')

    def test_undated_job_does_not_acquire_created_business_date(self):
        RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b', status=RunJob.STATUS_SUCCEEDED)
        self.assertIsNone(experience.daily_rows('company_b')[0]['day'])

    def test_all_company_job_is_split_by_actual_artifact_days(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_ALL, status=RunJob.STATUS_SUCCEEDED)
        self.artifact(date(2026,10,2), job=job)
        self.artifact(date(2026,10,3), job=job)
        self.assertEqual([r['day'] for r in experience.daily_rows('company_b')], [date(2026,10,3),date(2026,10,2)])

    def test_mismatch_and_preview_artifact_never_confirm_day(self):
        self.artifact(date(2026,10,3), match='MISMATCH')
        self.artifact(date(2026,10,2), dry=True)
        self.assertTrue(all(not r['confirmed'] for r in experience.daily_rows('company_b')))
        self.assertIsNone(self.home('company_b')['home_rows'][0]['latest'])

    def test_latest_preview_does_not_change_real_day_amount(self):
        self.sales('2026-10-03')
        self.sales('2026-10-03', stamp='run_190000Z_dry', dry=True, amount='999.00')
        row = experience.daily_rows('company_a')[0]
        self.assertTrue(row['confirmed'])
        self.assertEqual(row['amount'], Decimal('100.00'))
        self.assertEqual(len(experience.daily_rows('company_a',include_previews=False)[0]['attempts']),1)

    def test_home_never_says_up_to_date_with_bad_connection(self):
        for day in ('2026-10-01','2026-10-02','2026-10-03'):
            self.sales(day)
        context = experience.home_context('company_a',self.now,{'company_a':{'severity':'critical'}})
        self.assertEqual(context['home_rows'][0]['tone'],'danger')
        self.assertIn('connection needs attention',context['home_rows'][0]['issue'])
        self.assertEqual(context['home_rows'][0]['action_url'],reverse('epos_qbo:api-tokens'))

    def test_schedule_all_company_applies_to_goldplates_not_akponora(self):
        next_run = datetime(2026,10,4,18,tzinfo=ZoneInfo('UTC'))
        RunSchedule.objects.create(name='Daily', scope=RunJob.SCOPE_ALL, enabled=True, next_fire_at=next_run)
        self.assertEqual(self.home('company_b')['home_rows'][0]['next_run'], next_run)
        self.assertIsNone(self.home('company_a')['home_rows'][0]['next_run'])

    def test_filters_apply_to_history_and_home(self):
        self.sales('2026-10-02')
        self.sales('2026-10-03')
        self.artifact(date(2026,10,3))
        rows = experience.daily_rows('company_a',date(2026,10,3),date(2026,10,3))
        self.assertEqual(len(rows),1)
        self.assertEqual([r['company_key'] for r in self.home('company_a')['home_rows']], ['company_a'])

    def test_daily_page_filters_and_invalid_dates(self):
        response = self.client.get(reverse('epos_qbo:runs'), {'from':'wrong'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Choose valid dates')
        self.assertEqual(len(response.context['daily_page']),0)

    def test_shared_navigation_has_no_company_specific_or_logs_entries(self):
        response = self.client.get(reverse('epos_qbo:overview'))
        self.assertContains(response,'Main navigation')
        self.assertNotContains(response,'Company A daily')
        self.assertNotContains(response,'>Logs<')
        self.assertNotContains(response,'>API Tokens<')
        self.assertNotContains(response,'>Quick Sync<')

    def test_logs_bookmark_redirect_preserves_dates_and_company(self):
        response = self.client.get(reverse('epos_qbo:logs'), {'company_key':'company_b','date_from':'2026-10-01','date_to':'2026-10-03'})
        self.assertEqual(response.status_code,302)
        self.assertIn('company=company_b',response.url)
        self.assertIn('from=2026-10-01',response.url)
        self.assertIn('to=2026-10-03',response.url)

    def test_admin_permission_and_read_only_methods(self):
        response = self.client.get(reverse('epos_qbo:admin-home'))
        self.assertEqual(response.status_code,403)
        self.user.is_staff=True
        self.user.save(update_fields=['is_staff'])
        self.assertEqual(self.client.get(reverse('epos_qbo:admin-home')).status_code,200)
        self.assertEqual(self.client.post(reverse('epos_qbo:admin-home')).status_code,405)

    def test_clear_all_company_filter_overrides_saved_preference(self):
        from apps.epos_qbo.models import DashboardUserPreference
        DashboardUserPreference.objects.create(user=self.user, default_overview_company_key='company_a')
        response = self.client.get(reverse('epos_qbo:overview'), {'company':''})
        self.assertEqual(len(response.context['home_rows']),2)

    def test_old_company_a_sales_remain_available_without_becoming_october_evidence(self):
        self.artifact(date(2026,9,30), company='company_a')
        self.assertTrue(experience.daily_rows('company_a')[0]['confirmed'])
        self.assertIsNone(self.home('company_a')['home_rows'][0]['latest'])

    def test_day_before_cutoff_is_not_expected_to_be_closed(self):
        before_cutoff = datetime(2026,10,4,2,tzinfo=ZoneInfo('UTC'))
        context = experience.home_context('company_a',before_cutoff)
        self.assertEqual(context['home_expected'],date(2026,10,2))

    def test_other_activity_keeps_review_and_stock_jobs_out_of_sales_history(self):
        RunJob.objects.create(scope=RunJob.SCOPE_INVENTORY_PIPELINE,company_key='company_b',status=RunJob.STATUS_SUCCEEDED)
        self.assertEqual(experience.daily_rows('company_b'),[])
        activity = experience.other_activity('company_b')
        self.assertEqual(activity[0]['title'],'Stock check')
        self.assertEqual(activity[0]['label'],'Finished')

    def imported_sales(self, day, company='company_b', stats=None):
        import json
        from apps.epos_qbo.services.artifact_ingestion import ingest_metadata_file
        path = self.tmp / f'{company}_{day}_transform.json'
        path.write_text(json.dumps({'target_date': day.isoformat(), 'company_key': company,
            'source_mode': 'raw_split', 'processed_at': '2026-10-03T12:02:55.462129+00:00',
            'upload_stats': stats if stats is not None else {'attempted':32,'uploaded':32,'skipped':0,'failed':0},
            'reconcile': {'status':'MATCH','epos_total':9665900.0,'epos_count':32,
                          'qbo_total':9665900.0,'qbo_count':32,'difference':0.0}}))
        record, _ = ingest_metadata_file(path)
        self.assertIsNone(record.run_job_id)
        self.assertEqual(record.kind, RunArtifact.KIND_SALES_UPLOAD)
        return record

    def test_imported_goldplates_without_dry_run_key_and_old_failure_are_up_to_date(self):
        from datetime import timedelta
        for offset in range(31):
            self.imported_sales(date(2026,9,1) + timedelta(days=offset))
        RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b',
            target_date=date(2026,8,20), status=RunJob.STATUS_FAILED)
        now = datetime(2026,10,2,12,tzinfo=ZoneInfo('UTC'))
        context = experience.home_context('company_b', now)
        self.assertEqual(context['home_missing'], 0)
        self.assertEqual(context['home_rows'][0]['label'], 'Up to date')
        self.assertEqual(context['home_rows'][0]['latest'], date(2026,10,1))
        self.assertEqual(context['home_banners'], [])
        self.assertEqual(context['home_sales'], '₦9,665,900.00')
        # At the time in the brief, 2 October still needs a confirmed record.
        context = experience.home_context('company_b', datetime(2026,10,3,15,tzinfo=ZoneInfo('UTC')))
        self.assertEqual(context['home_rows'][0]['missing'], [date(2026,10,2)])
        self.assertNotIn('failed', context['home_rows'][0]['issue'])
        self.assertIn('1 day has', context['home_rows'][0]['issue'])

    def test_imported_company_a_go_live_confirms_without_daily_evidence(self):
        self.sales('2026-10-01', dry=True)
        self.imported_sales(date(2026,10,1), company='company_a')
        context = experience.home_context('company_a', datetime(2026,10,3,15,tzinfo=ZoneInfo('UTC')))
        self.assertEqual(context['home_rows'][0]['missing'], [date(2026,10,2)])
        self.assertEqual(context['home_rows'][0]['latest'], date(2026,10,1))
        row = experience.daily_rows('company_a')[0]
        self.assertTrue(row['confirmed'])
        self.assertEqual(row['amount'], Decimal('9665900.00'))

    def test_company_a_without_go_live_evidence_remains_explicitly_unconfirmed(self):
        self.sales('2026-10-01', dry=True)
        context = experience.home_context('company_a', datetime(2026,10,3,15,tzinfo=ZoneInfo('UTC')))
        self.assertEqual(context['home_missing'], 2)
        self.assertIn('1 October has no automatic sales check on record', context['home_rows'][0]['issue'])
        self.assertEqual(context['home_confirmed_count'], 0)

    def test_company_a_artifact_with_job_is_visible_in_daily_history(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_a',
            target_date=date(2026,10,1), status=RunJob.STATUS_SUCCEEDED)
        self.artifact(date(2026,10,1), company='company_a', job=job)
        self.assertTrue(experience.daily_rows('company_a')[0]['confirmed'])

    def test_new_failure_after_latest_confirmed_day_still_warns(self):
        self.imported_sales(date(2026,10,1))
        RunJob.objects.create(scope=RunJob.SCOPE_SINGLE, company_key='company_b',
            target_date=date(2026,10,2), status=RunJob.STATUS_FAILED)
        row = self.home('company_b')['home_rows'][0]
        self.assertIn('latest sales attempt failed', row['issue'])

    def test_latest_confirmed_date_ignores_preview_and_malformed_stats(self):
        self.imported_sales(date(2026,10,1))
        self.imported_sales(date(2026,10,2), stats={'dry_run':True})
        bad = self.artifact(date(2026,10,3))
        bad.upload_stats_json = ['not a stats object']
        bad.save()
        row = self.home('company_b')['home_rows'][0]
        self.assertEqual(row['latest'], date(2026,10,1))
        self.assertEqual(row['missing'][-2:], [date(2026,10,2),date(2026,10,3)])

    def test_home_missing_day_link_uses_singular(self):
        self.sales('2026-10-01')
        self.sales('2026-10-03')
        with mock.patch('apps.epos_qbo.services.experience.timezone.now', return_value=self.now):
            response = self.client.get(reverse('epos_qbo:overview'), {'company':'company_a'})
        self.assertContains(response, 'View 1 missing day</summary>')
        self.assertNotContains(response, '1 days have')

    def test_imported_and_daily_evidence_for_same_day_do_not_double_count(self):
        self.imported_sales(date(2026,10,3), company='company_a')
        self.sales('2026-10-03', amount='100.00')
        self.assertEqual(self.home('company_a')['home_sales'], '₦100.00')
        rows = experience.daily_rows('company_a')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['confirmed'])

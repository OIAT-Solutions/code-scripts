import json
from datetime import date
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, RunArtifact, RunJob, RunSchedule, PortalReviewAction
from apps.epos_qbo.services import company_workspace, experience, messages, company_a_ops
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _summary, _step


class CompanyWorkspaceTests(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.a = CompanyConfigRecord.objects.create(company_key='company_a', display_name='Akponora', config_json={})
        self.b = CompanyConfigRecord.objects.create(company_key='company_b', display_name='Goldplates', config_json={})
        self.user = User.objects.create_user('viewer', password='test-only')
        self.client.force_login(self.user)
        self.url = reverse('epos_qbo:company-detail', args=['company_a'])

    def make_sales(self, day='2026-10-02', stamp='run_170000Z', dry=False, amount='123.00'):
        return self.make_run(day, stamp, _summary(day, dry=dry, steps=[_step('sales', counts={'mode':'post','reconcile_status':'MATCH','qbo_total':amount})]))

    def test_tabs_follow_company_capabilities_and_settings_permission(self):
        self.assertEqual(company_workspace.available_tabs(self.a, self.user), ['overview','attention','sales','purchases','products','deposits','runs','schedules','settings'])
        self.assertEqual(company_workspace.available_tabs(self.b, self.user), ['sales'])
        self.assertEqual(company_workspace.available_tabs(self.b, self.user, True), ['sales'])  # products: Akponora only
        self.user.user_permissions.add(Permission.objects.get(codename='can_edit_companies'))
        self.user = User.objects.get(pk=self.user.pk)
        self.assertIn('settings', company_workspace.available_tabs(self.a,self.user))

    def test_unsupported_tab_falls_back_to_first_supported_page_without_config(self):
        self.a.config_json={'secret_config_marker':'do-not-display'}
        self.a.save()
        for key, tab in [('company_b','purchases'),('company_a','wrong')]:
            response=self.client.get(reverse('epos_qbo:company-detail',args=[key]), {'tab':tab})
            self.assertEqual(response.context['company_tab'], 'overview' if key == 'company_a' else 'sales')
            self.assertNotContains(response,'do-not-display')
        response=self.client.get(self.url, {'tab':'settings'})
        self.assertEqual(response.context['company_tab'],'settings')
        self.assertNotContains(response,'do-not-display')
        self.assertNotContains(response,'Manage company settings')

    def test_switching_company_defaults_to_own_first_supported_page(self):
        response=self.client.get(self.url,{'tab':'purchases'})
        choices={c['key']:c['url'] for c in response.context['company_choices']}
        self.assertIn('tab=overview',choices['company_a'])
        self.assertIn('tab=sales',choices['company_b'])
        self.assertEqual(response.context['company_key'], 'company_a')
        self.assertContains(response, 'Nora Mart')
        self.assertContains(response, 'Akponora Ventures Ltd')
        self.assertContains(response, 'Goldplates')
        self.assertIn('tab=sales', choices['company_b'])

    def test_switcher_exposes_only_allowed_restore_tabs_on_workspace_and_queue(self):
        for url in [self.url, reverse('epos_qbo:attention')]:
            response = self.client.get(url)
            choices = {c['key']: c for c in response.context['company_choices']}
            self.assertEqual(choices['company_b']['tabs'], ['sales'])
            self.assertIn('tab=overview', choices['company_a']['url'])
            self.assertContains(response, 'data-navigation-user="%s"' % self.user.pk)
            self.assertContains(response, 'data-company-tabs="sales"')

    def test_company_sales_agree_with_home_and_keep_confirmed_amount(self):
        self.make_sales()
        self.make_run('2026-10-02','run_180000Z',_summary('2026-10-02',status='failed',steps=[_step('sales',status='failed')]))
        response=self.client.get(self.url)
        row=response.context['company_sales_page'][0]
        self.assertTrue(row['confirmed'])
        self.assertTrue(row['latest_issue'])
        self.assertEqual(row['amount_label'],'₦123.00')
        self.assertEqual(response.context['company_position']['latest'], experience.home_context('company_a')['home_rows'][0]['latest'])
        self.assertContains(response,'Sales confirmed · needs attention')

    def test_preview_does_not_appear_as_confirmed_sales(self):
        self.make_sales(dry=True)
        response=self.client.get(self.url)
        self.assertEqual(len(response.context['company_sales_page']),0)
        self.assertNotContains(response,'₦123.00')

    def test_missing_money_stays_unknown(self):
        self.make_sales(amount=None)
        self.assertContains(self.client.get(self.url),'Not recorded')
        self.assertNotContains(self.client.get(self.url),'₦0.00')

    def test_purchases_link_to_exact_inbox_card_and_not_a_post_action(self):
        self.make_run('2026-10-02','run_170000Z',_summary('2026-10-02',steps=[_step('bills',status='review',counts={'hold':1})]), files={
            'bills/summary.json':json.dumps({'payloads_sha256':'fixture-sha'}),
            'bills/review.csv':'PO,EPOS Supplier,Status,Reasons\n42,Sample supplier,HOLD,Possible duplicate\n'})
        response=self.client.get(self.url,{'tab':'purchases'})
        decisions=response.context['company_decisions']
        self.assertEqual(len(decisions),1)
        self.assertContains(response,'#item-'+decisions[0]['key'])
        self.assertNotContains(response,reverse('epos_qbo:attention-confirm'))
        self.assertNotContains(response,'Bills posted: <strong>0')
        self.assertContains(self.client.get(reverse('epos_qbo:attention')),'id="item-'+decisions[0]['key']+'"')

    def test_all_workspace_reads_are_get_only_and_do_not_dispatch_tools(self):
        with mock.patch('apps.epos_qbo.services.job_runner.dispatch_next_queued_job') as dispatch, mock.patch('apps.epos_qbo.services.attention_actions.execute') as execute:
            for tab in ('overview','sales','purchases','products','deposits','runs','schedules','settings'):
                self.assertEqual(self.client.get(self.url,{'tab':tab}).status_code,200)
            # 13 Oct 2026: Nora Mart's Review queue is the table + side-panel page
            self.assertRedirects(self.client.get(self.url,{'tab':'attention','kind':'stock'}),
                                 reverse('epos_qbo:attention')+'?kind=stock', fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse('epos_qbo:attention')).status_code,200)
            self.assertEqual(self.client.post(self.url).status_code,405)
            self.assertEqual(self.client.post(reverse('epos_qbo:companies-list')).status_code,405)
            dispatch.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(RunJob.objects.count(),0)
        self.assertEqual(PortalReviewAction.objects.count(),0)

    def test_nora_dashboard_uses_company_a_records_and_keeps_company_b_workspace(self):
        self.make_sales(day='2026-10-02', amount='123.00')
        b_job = RunJob.objects.create(company_key='company_b', target_date='2026-10-02', scope=RunJob.SCOPE_SINGLE,
                                      status=RunJob.STATUS_SUCCEEDED)
        RunArtifact.objects.create(company_key='company_b', target_date='2026-10-02', run_job=b_job,
            kind=RunArtifact.KIND_SALES_UPLOAD, reconcile_status='MATCH', reconcile_qbo_total='999.00',
            upload_stats_json={'dry_run': False})
        nora = self.client.get(self.url, {'tab':'overview'})
        self.assertEqual(nora.status_code, 200)
        self.assertTrue(nora.context['nora_workspace'])
        self.assertContains(nora, 'Nora Mart Operations')
        self.assertContains(nora, '₦123.00')
        self.assertNotContains(nora, '₦999.00')
        self.assertNotContains(nora, 'GOLDPLATES FEASTHOUSE')
        goldplates = self.client.get(reverse('epos_qbo:company-detail', args=['company_b']), {'tab':'sales'})
        self.assertEqual(goldplates.status_code, 200)
        # 13 Oct 2026: Goldplates sales also use the new layout, so the company switcher keeps you in it
        self.assertEqual(goldplates.templates[0].name, 'epos_qbo/nora_workspace.html')
        self.assertContains(goldplates, 'Goldplates Feasthouse Ltd.')
        self.assertContains(goldplates, '₦999.00')

    def test_nora_sales_filter_searches_real_dates_and_does_not_replace_confirmed_value(self):
        self.make_sales(day='2026-10-02', stamp='run_170000Z', amount='123.00')
        self.make_run('2026-10-02','run_180000Z',_summary('2026-10-02',status='failed',steps=[_step('sales',status='failed')]))
        self.make_sales(day='2026-10-03', stamp='run_190000Z', amount='456.00')
        response=self.client.get(self.url, {'tab':'sales', 'q':'2026-10-02'})
        self.assertEqual(response.context['company_sales_page'].paginator.count, 1)
        row=response.context['company_sales_page'][0]
        self.assertEqual(row['amount_label'], '₦123.00')
        self.assertTrue(row['latest_issue'])

    def test_overview_chart_uses_real_confirmed_amounts_and_zero_has_no_bar(self):
        self.make_sales(day='2026-10-02', amount='0.00')
        self.make_sales(day='2026-10-03', amount='100.00')
        response = self.client.get(self.url, {'tab':'overview', 'q':'not-used'})
        points = {point['day'].isoformat(): point for point in response.context['nora_sales_trend']}
        self.assertEqual(points['2026-10-02']['amount'], '₦0.00')
        self.assertEqual(points['2026-10-02']['height'], 0)
        self.assertEqual(points['2026-10-03']['amount'], '₦100.00')
        self.assertGreater(points['2026-10-03']['height'], 0)
        self.assertContains(response, 'style="height:0%"')

    def test_schedule_details_and_new_schedule_preserve_permission_and_records(self):
        schedule = RunSchedule.objects.create(name='Nora daily routine', company_key='company_a',
            scope=RunJob.SCOPE_COMPANY_A_DAILY, cron_expr='0 18 * * *', timezone_name='Africa/Lagos', enabled=True)
        before_jobs = RunJob.objects.count()
        response = self.client.get(self.url, {'tab':'schedules'})
        self.assertContains(response, f'data-dialog="schedule-{schedule.id}"')
        self.assertContains(response, 'Africa/Lagos')
        self.assertNotContains(response, 'New schedule')
        self.assertNotContains(response, 'Manage schedules')
        self.user.user_permissions.add(Permission.objects.get(codename='can_manage_schedules'))
        response = self.client.get(self.url, {'tab':'schedules'})
        self.assertContains(response, 'New schedule')
        self.assertContains(response, '?create=1&amp;company=company_a#schedule-management')
        self.assertEqual(RunSchedule.objects.count(), 1)
        self.assertEqual(RunJob.objects.count(), before_jobs)
        schedule.refresh_from_db()
        self.assertTrue(schedule.enabled)

    def test_chart_uses_readable_scale_without_losing_full_amounts(self):
        self.make_sales(day='2026-10-02', amount='5280950.00')
        response = self.client.get(self.url, {'tab':'overview'})
        self.assertEqual(response.context['nora_trend_total'], '₦5,280,950.00')
        self.assertEqual(response.context['nora_trend_ticks'][0], '₦6m')
        point = next(p for p in response.context['nora_sales_trend'] if p['confirmed'])
        self.assertEqual(point['compact'], '₦5.28m')
        self.assertAlmostEqual(point['height'], 5280950 / 6000000 * 100)
        self.assertContains(response, '2 October 2026: ₦5,280,950.00')

    def test_review_queue_groups_stock_kinds_and_preserves_filters_in_pagination(self):
        decisions = [dict(kind='stock_adjust' if n % 2 else 'stock_follow', date='2026-10-02',
                          key=f'stock-{n}', title=f'Stock record {n}', reason='Review stock count',
                          identity=f'item-{n}', company_key='company_a') for n in range(17)]
        with mock.patch('apps.epos_qbo.services.attention.inbox', return_value=(decisions, [])), \
             mock.patch('apps.epos_qbo.services.experience.inbox', return_value=(decisions, [])):
            response = self.client.get(reverse('epos_qbo:attention'))
        # every record is a table row with a side panel; the workflow filter groups the stock kinds
        self.assertEqual(response.content.decode().count('data-group="stock"'), 17)
        self.assertContains(response, 'class="nora-drawer" id="item-stock-16"')
        self.assertEqual(company_workspace.QUEUE_KIND_GROUPS['stock'], {'stock_movement', 'stock_adjust', 'stock_follow'})


    def test_sales_pagination_keeps_company_and_tab(self):
        for day in range(1,18):
            self.make_sales(day=f'2026-10-{day:02d}')
        response=self.client.get(self.url,{'tab':'sales','page':2})
        self.assertEqual(response.context['company_sales_page'].number,2)
        self.assertEqual(len(response.context['company_sales_page']),2)
        self.assertContains(response,'tab=sales&amp;q=&amp;status=all&amp;page=1')

    def test_banking_tab_has_no_recent_work_list(self):
        self.make_run('2026-10-02', 'run_170000Z', _summary('2026-10-02', steps=[
            _step('uf', counts={'deposited': [{'day': '2026-09-25', 'by_bank': {'77': '100'}}],
                                'held': [{'day': '2026-09-26'}, {'day': '2026-09-27'}]})]))
        response = self.client.get(self.url, {'tab': 'deposits'})
        self.assertNotContains(response, 'Recent work')
        self.assertNotContains(response, "'by_bank'")

    def test_unknown_step_does_not_look_finished(self):
        outcome=messages.step_outcome(company_a_ops.Step(name='bills', label='Bills', status='unknown'))
        self.assertEqual(outcome['label'],'Not confirmed')
        self.assertNotEqual(outcome['tone'],'success')

    def test_run_detail_uses_recorded_amount_and_keeps_raw_data_in_details(self):
        self.make_sales(amount=None)
        response=self.client.get(reverse('epos_qbo:company-a-run-detail',args=['2026-10-02','run_170000Z']))
        self.assertContains(response,'Not recorded')
        self.assertContains(response,'Diagnostics')
        self.assertContains(response,'Downloads and technical details')
        self.assertNotContains(response,'₦0.00')

    def test_run_detail_keeps_other_attempt_confirmation_separate(self):
        self.make_sales()
        self.make_run('2026-10-02','run_180000Z',_summary('2026-10-02',status='failed',steps=[_step('sales',status='failed')]))
        response=self.client.get(reverse('epos_qbo:company-a-run-detail',args=['2026-10-02','run_180000Z']))
        self.assertContains(response,'confirmed in another attempt')
        self.assertContains(response,'₦123.00')
        self.assertEqual(response.context['business_outcome']['label'],'Could not finish')

    def test_non_sales_job_success_does_not_claim_sales(self):
        job=RunJob.objects.create(company_key='company_b',scope=RunJob.SCOPE_INVENTORY_PIPELINE,status='succeeded')
        response=self.client.get(reverse('epos_qbo:run-detail',args=[job.id]))
        self.assertEqual(response.context['business_outcome']['label'],'Finished')
        self.assertNotContains(response,'Sales confirmed')

    def test_sales_evidence_is_visible_before_diagnostics_and_excludes_preview_confirmation(self):
        job = RunJob.objects.create(company_key='company_b', target_date='2026-10-02', scope=RunJob.SCOPE_SINGLE, status='succeeded')
        RunArtifact.objects.create(company_key='company_b', target_date='2026-10-02', run_job=job,
            source_path='fixture.json', source_hash='preview', reconcile_status='MATCH',
            reconcile_qbo_total='100', upload_stats_json={'dry_run': True})
        response = self.client.get(reverse('epos_qbo:run-detail', args=[job.id]))
        record = response.context['business_evidence'][0]
        self.assertTrue(record['preview'])
        self.assertFalse(record['confirmed'])
        html = response.content.decode()
        self.assertLess(html.index('Sales evidence'), html.index('Downloads and diagnostics'))

    def test_company_pages_require_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code,302)

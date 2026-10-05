import json
from datetime import date
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, RunArtifact, RunJob, PortalReviewAction
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
        self.assertEqual(company_workspace.available_tabs(self.a, self.user), ['sales','purchases','products','suppliers','deposits'])
        self.assertEqual(company_workspace.available_tabs(self.b, self.user), ['sales'])
        self.assertEqual(company_workspace.available_tabs(self.b, self.user, True), ['sales','products'])
        self.user.user_permissions.add(Permission.objects.get(codename='can_edit_companies'))
        self.user = User.objects.get(pk=self.user.pk)
        self.assertIn('settings', company_workspace.available_tabs(self.a,self.user))

    def test_unsupported_or_unpermitted_tab_falls_back_to_sales_without_config(self):
        self.a.config_json={'secret_config_marker':'do-not-display'}
        self.a.save()
        for key, tab in [('company_a','settings'),('company_b','purchases'),('company_a','wrong')]:
            response=self.client.get(reverse('epos_qbo:company-detail',args=[key]), {'tab':tab})
            self.assertEqual(response.context['company_tab'],'sales')
            self.assertNotContains(response,'do-not-display')

    def test_switching_company_retains_only_supported_tab(self):
        response=self.client.get(self.url,{'tab':'purchases'})
        choices={c['key']:c['url'] for c in response.context['company_choices']}
        self.assertIn('tab=purchases',choices['company_a'])
        self.assertIn('tab=sales',choices['company_b'])

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

    def test_bill_hold_does_not_create_a_supplier_outcome(self):
        self.make_run('2026-10-02','run_170000Z',_summary('2026-10-02',steps=[_step('bills',status='review',counts={'hold':1})]))
        response=self.client.get(self.url,{'tab':'suppliers'})
        self.assertEqual(response.context['company_step_activity'],[])

    def test_bill_review_does_not_mark_completed_supplier_results_as_failed(self):
        self.make_run('2026-10-02','run_170000Z',_summary('2026-10-02',steps=[_step('bills',status='review',counts={'hold':1,'vendors_created':1,'vendors_held':0})]))
        response=self.client.get(self.url,{'tab':'suppliers'})
        self.assertEqual(response.context['company_step_activity'][0]['label'],'Supplier results recorded')

    def test_preview_step_never_displays_posted_bill_or_supplier_counts(self):
        self.make_run('2026-10-02','run_170000Z_dry',_summary('2026-10-02',dry=True,steps=[_step('bills',counts={'posted':7,'vendors_created':8})]))
        for tab in ('purchases','suppliers'):
            response=self.client.get(self.url,{'tab':tab})
            self.assertEqual(response.context['company_step_activity'][0]['facts'],[])
            self.assertContains(response,'Nothing was posted')

    def test_all_workspace_reads_are_get_only_and_do_not_dispatch_tools(self):
        with mock.patch('apps.epos_qbo.services.job_runner.dispatch_next_queued_job') as dispatch, mock.patch('apps.epos_qbo.services.attention_actions.execute') as execute:
            for tab in ('sales','purchases','products','suppliers','deposits'):
                self.assertEqual(self.client.get(self.url,{'tab':tab}).status_code,200)
            self.assertEqual(self.client.post(self.url).status_code,405)
            self.assertEqual(self.client.post(reverse('epos_qbo:companies-list')).status_code,405)
            dispatch.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(RunJob.objects.count(),0)
        self.assertEqual(PortalReviewAction.objects.count(),0)

    def test_company_a_never_uses_generic_inventory_trigger(self):
        self.user.is_superuser=True
        self.user.save()
        for tab in ('sales','products'):
            response=self.client.get(self.url,{'tab':tab})
            self.assertNotContains(response,reverse('epos_qbo:run-trigger-inventory'))
            self.assertNotContains(response,reverse('epos_qbo:run-trigger'))

    def test_directory_business_filters_search_and_home_agree(self):
        self.make_sales()
        response=self.client.get(reverse('epos_qbo:companies-list'),{'search':'Akponora','state':'attention'})
        self.assertEqual([r['company_key'] for r in response.context['directory_rows']],['company_a'])
        self.assertEqual(response.context['directory_rows'][0]['latest'],date(2026,10,2))
        self.assertContains(response,'Open company')
        self.assertNotContains(response,'Trigger sync')
        response=self.client.get(reverse('epos_qbo:companies-list'),{'state':'current'})
        self.assertEqual(response.context['directory_rows'],[])

    def test_sales_pagination_keeps_company_and_tab(self):
        for day in range(1,18):
            self.make_sales(day=f'2026-10-{day:02d}')
        response=self.client.get(self.url,{'tab':'sales','page':2})
        self.assertEqual(response.context['company_sales_page'].number,2)
        self.assertEqual(len(response.context['company_sales_page']),2)
        self.assertContains(response,'tab=sales&amp;page=1')

    def test_deposit_activity_shows_counts_instead_of_record_lists(self):
        self.make_run('2026-10-02', 'run_170000Z', _summary('2026-10-02', steps=[
            _step('uf', counts={'deposited': [{'day': '2026-09-25', 'by_bank': {'77': '100'}}],
                                'held': [{'day': '2026-09-26'}, {'day': '2026-09-27'}]})]))
        response = self.client.get(self.url, {'tab': 'deposits'})
        facts = dict(response.context['company_step_activity'][0]['facts'])
        self.assertEqual(facts['Days deposited'], 1)
        self.assertEqual(facts['Days needing review'], 2)
        self.assertNotContains(response, "'by_bank'")

    def test_unknown_step_does_not_look_finished(self):
        outcome=messages.step_outcome(company_a_ops.Step(name='bills', label='Bills', status='unknown'))
        self.assertEqual(outcome['label'],'Not confirmed')
        self.assertNotEqual(outcome['tone'],'success')

    def test_run_detail_uses_recorded_amount_and_keeps_raw_data_in_details(self):
        self.make_sales(amount=None)
        response=self.client.get(reverse('epos_qbo:company-a-run-detail',args=['2026-10-02','run_170000Z']))
        self.assertContains(response,'Not recorded')
        self.assertContains(response,'Supporting details')
        self.assertContains(response,'Supporting records and technical details')
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

    def test_company_pages_require_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code,302)

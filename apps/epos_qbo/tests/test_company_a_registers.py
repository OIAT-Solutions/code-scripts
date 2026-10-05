"""Products & Stock, Suppliers, Deposits and the new approval contracts (Company A).

Every tool call is mocked; nothing here reaches QuickBooks, EPOS or Google.
"""
import ast
import csv
import json
import re
from datetime import date
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.management import call_command, CommandError
from django.test import Client, TestCase
from django.urls import reverse

from apps.epos_qbo.models import CompanyConfigRecord, PortalReviewAction, PortalSettingChange, RunJob
from apps.epos_qbo.services import attention, attention_actions, deposits, workspace_jobs
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _step, _summary

MAPPING_COLUMNS = ["Row ID", "EPOS Product ID", "EPOS Existing SKU", "EPOS Name", "Pipeline Status", "Review Status",
                   "Target QBO Item Type", "Target QBO Name", "Target QBO SKU", "Target QBO Item Id",
                   "Staff Approved Sale Multiplier", "Effective Date", "Approved By", "Canonical Family Key",
                   "Canonical Unit", "Staff Approved Purchase Multiplier"]
LAST_CLOSED = "code_scripts.akponora_ops.daily_run.last_closed_business_date"


def snapshot_row(sku, status, *, name=None, qid="1", typ="Inventory", epos=10, qbo=10, diff=0, timing=None, flags=(), active=True, pids=("1",)):
    return {"family_sku": sku, "qbo_item_id": qid, "qbo_name": name or sku, "type": typ, "qbo_active": active,
            "canonical_unit": "bottles", "epos_master_id": pids[0], "epos_master_name": (name or sku).upper(),
            "epos_product_ids": list(pids), "epos_volume_of_sale": 1, "epos_qty_canonical": epos, "qbo_qty_on_hand": qbo,
            "difference": diff, "status": status, "likely_timing": timing, "timing_reasons": ["Sold 3 today."] if timing else [],
            "flags": list(flags), "tolerance": 0.001}


def snapshot_doc():
    rows = [
        snapshot_row("AKP-1", "MATCH", name="Coke 50cl", qid="11", pids=("1", "2")),
        snapshot_row("AKP-3", "NEGATIVE_QBO", name="Bread", qid="13", epos=4, qbo=-2, diff=6, pids=("3",)),
        snapshot_row("AKP-4", "DIFFERENT", name="Milk", qid="14", epos=7, qbo=10, diff=-3, timing=True, pids=("4",)),
        snapshot_row("AKP-5", "NO_QBO_ITEM", name="Sugar", qid="", qbo=None, diff=None, pids=("5",)),
        snapshot_row("AKP-NS-6", "NOT_TRACKED_IN_EPOS", name="Gift wrap", qid="16", typ="NonInventory", epos=None, qbo=None, diff=None, pids=("6",)),
        snapshot_row("AKP-7", "MATCH", name="Old soap", qid="17", active=False, flags=("QBO_ITEM_INACTIVE",), pids=("7",)),
    ]
    return {"schema_version": 1, "company": "company_a", "generated_at": "2026-10-03T08:00:00+00:00", "tolerance": 0.001,
            "summary_text": "Stock check: 2 match, 1 different, 1 negative in QuickBooks",
            "sources": {"epos_stock_report": {"path": "x", "at": "2026-10-03T07:55:00+00:00", "refreshed_this_run": True},
                        "catalogue": {"path": "y", "at": "2026-10-02T18:00:00+00:00"},
                        "qbo": {"read_at": "2026-10-03T07:58:00+00:00", "refreshed_this_run": True}, "mapping": {"path": "z", "at": "2026-10-02T18:00:00+00:00"}},
            "business_context": {"current_business_date": "2026-10-03", "last_posted_sales_date": "2026-10-02", "unposted_sales_days": ["2026-10-03"],
                                 "note": "EPOS is live; QuickBooks has sales up to 2 Oct and only posted bills."},
            "timing_evidence": {}, "summary": {"rows": 6, "by_status": {}},
            "rows": rows,
            "unmapped_epos_products": [
                {"epos_product_id": "101", "name": "Fanta 50cl", "tracked": True, "category": "Drinks", "epos_qty": 24},
                {"epos_product_id": "105", "name": "New biscuit", "tracked": False, "category": "Snacks", "epos_qty": None},
                {"epos_product_id": "106", "name": "Skipped thing", "tracked": False, "category": "", "epos_qty": None}],
            "unassigned_stock_rows": [], "qbo_akp_items_not_in_mapping": []}


def decision(pid, name, action, review, *, master="", target_sku="AKP-101", sha=None):
    return {"pid": pid, "name": name, "action": action, "review": review, "adopt_qbo_id": "", "master_pid": master,
            "target": {"type": "Inventory", "sku": target_sku, "name": "Fanta 50cl", "qbo_id": ""}, "multiplier": "12" if master else "1",
            "flags": ["UNEXPLAINED_STOCK"] if review == "REVIEW" else [], "reasons": ["EPOS category missing"] if review == "HOLD" else [],
            "notes": [], "stock": {"units": "24"}, "decision_sha256": sha or f"sha{pid}", "canonical_unit": "bottles"}


class RegisterFixtures(CompanyAOpsFixtureMixin):
    def setUp(self):
        super().setUp()
        self.company = CompanyConfigRecord.objects.create(company_key="company_a", display_name="Akponora", is_active=True, config_json={})
        self.user = User.objects.create_user("ada", password="test-only", first_name="Ada", last_name="Obi")
        for code in ("can_trigger_runs", "can_approve_company_a_reviews"):
            self.user.user_permissions.add(Permission.objects.get(codename=code))
        self.client.force_login(self.user)
        self.maps = self.tmp / "mappings" / "company_a"
        self.maps.mkdir(parents=True)
        self.page = reverse("epos_qbo:company-detail", args=["company_a"])
        self.confirm = reverse("epos_qbo:attention-confirm")
        patcher = mock.patch(LAST_CLOSED, return_value=date(2026, 10, 2))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_csv(self, path, columns, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)

    def write_snapshot(self, doc=None):
        path = self.tmp / "ops/company_a/stock_snapshot/latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc or snapshot_doc()))

    def write_mapping(self, pending=()):
        rows = [{c: "" for c in MAPPING_COLUMNS} | {"EPOS Product ID": pid, "Review Status": "Pending Approval" if pid in pending else "Approved",
                                                   "Approved By": "Approved by Ada in the portal, 2026-10-01 10:00 Lagos"} for pid in ("1", "2", "3", "4", "5", "6", "7")]
        self.write_csv(self.maps / "approved.csv", MAPPING_COLUMNS, rows)

    def write_exclusion(self, kind, key, reason="not sold here"):
        self.write_csv(self.maps / "review_exclusions.csv", ["kind", "key", "reason", "added_by", "added_at", "expires_at"],
                       [{"kind": kind, "key": key, "reason": reason, "added_by": "Approved by Ada in the portal, 2026-10-01 10:00 Lagos",
                         "added_at": "2026-10-01T09:00:00+00:00", "expires_at": ""}])

    def write_plan(self, decisions, applied=()):
        run = self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02"))
        folder = run / "catalogue"
        folder.mkdir()
        (folder / "plan.json").write_text(json.dumps({"plan_sha256": "plansha", "decisions": decisions}))
        (folder / "summary.json").write_text(json.dumps({"plan_sha256": "plansha", "decision_shas": {d["pid"]: d["decision_sha256"] for d in decisions if d["review"] != "HOLD"}}))
        if applied:
            (folder / "applied_state.json").write_text(json.dumps({"mapping_sha256": "m", "applied": {pid: {} for pid in applied}}))
        return folder

    def record(self, item, action, **payload):
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW, company_key="company_a")
        data = {"key": item["key"], "snapshot": item["snapshot"], "approval_ref": "Approved by Ada Obi in the portal, 2026-10-03 14:05 Lagos"}
        data.update(payload)
        return PortalReviewAction.objects.create(job=job, actor="ada", action=action, reason="checked the PO", confirmation_id=f"{item['key']}{action}{len(payload)}", payload=data)

    def item(self, kind, identity=None):
        return next(i for i in attention.inbox()[0] if i["kind"] == kind and (identity is None or i["identity"] == identity))


# --------------------------------------------------------------------------- products & stock
class ProductsAndStockTests(RegisterFixtures, TestCase):
    def test_no_snapshot_renders_calm_empty_state_with_update_button(self):
        response = self.client.get(self.page, {"tab": "products"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No stock check yet")
        self.assertContains(response, "Update products &amp; stock")
        self.assertEqual(response.context["stock_errors"], [])

    def test_unreadable_snapshot_is_reported_not_crashed(self):
        path = self.tmp / "ops/company_a/stock_snapshot/latest.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        response = self.client.get(self.page, {"tab": "products"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["stock_errors"])

    def test_statuses_links_units_and_order(self):
        self.write_snapshot()
        self.write_mapping(pending=("4",))
        self.write_exclusion("product", "106")
        response = self.client.get(self.page, {"tab": "products"})
        rows = {r["sku"] or r["pid"]: r for r in response.context["stock_page"].object_list}
        self.assertEqual(response.context["stock_page"].object_list[0]["status"], "NEGATIVE_QBO")
        self.assertEqual(rows["AKP-3"]["qbo"], "-2 bottles")
        self.assertEqual(rows["AKP-3"]["epos"], "4 bottles")
        self.assertEqual(rows["AKP-1"]["link_label"], "Linked in QuickBooks")
        self.assertEqual(rows["AKP-4"]["link_label"], "Link needs checking")  # mapping row not approved
        self.assertEqual(rows["AKP-5"]["link_label"], "Link needs checking")  # QuickBooks item missing
        self.assertEqual(rows["AKP-7"]["link_label"], "Link needs checking")  # inactive item
        self.assertEqual(rows["101"]["link_label"], "Not mapped")
        self.assertEqual(rows["106"]["link"], "excluded")
        self.assertTrue(rows["AKP-4"]["likely_timing"])
        self.assertEqual(rows["AKP-NS-6"]["qbo"], "Not a stock item")
        self.assertContains(response, "Likely timing")
        self.assertContains(response, "Last updated")
        self.assertContains(response, "EPOS is live; QuickBooks has sales up to 2 Oct")
        self.assertEqual(response.context["stock_counts"]["negative_qbo"], 1)

    def test_search_and_filters(self):
        self.write_snapshot()
        response = self.client.get(self.page, {"tab": "products", "q": "milk"})
        self.assertEqual([r["sku"] for r in response.context["stock_page"].object_list], ["AKP-4"])
        response = self.client.get(self.page, {"tab": "products", "q": "snacks"})
        self.assertEqual([r["pid"] for r in response.context["stock_page"].object_list], ["105"])
        response = self.client.get(self.page, {"tab": "products", "status": "unmapped"})
        self.assertEqual(len(response.context["stock_page"].object_list), 3)
        response = self.client.get(self.page, {"tab": "products", "status": "negative_qbo"})
        self.assertEqual([r["sku"] for r in response.context["stock_page"].object_list], ["AKP-3"])
        response = self.client.get(self.page, {"tab": "products", "status": "<script>"})
        self.assertEqual(response.context["stock_filter"], "all")

    def test_unmapped_products_link_to_per_product_approval_or_dont_ask_again(self):
        self.write_snapshot()
        self.write_plan([decision("101", "Fanta 50cl", "CREATE_INVENTORY", "AUTO")])
        response = self.client.get(self.page, {"tab": "products"})
        item = self.item("product", "101")
        self.assertContains(response, f"key={item['key']}&amp;action=approve")
        self.assertContains(response, "action=exclude&amp;kind=product&amp;pid=105")

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_update_button_queues_read_only_snapshot_job_once(self, dispatch):
        url = reverse("epos_qbo:workspace-update", args=["company_a"])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url, {"action": "stock"}).status_code, 302)
            self.client.post(url, {"action": "stock"})
        job = RunJob.objects.get(scope=RunJob.SCOPE_WORKSPACE_READ)
        self.assertEqual(job.inventory_options_json["action"], "stock")
        self.assertEqual(workspace_jobs.command("stock", job.id)[-2:], ["code_scripts.akponora_ops.stock_snapshot", "run"])
        self.assertEqual(workspace_jobs.command("stock", job.id, mode="qbo")[-1], "--no-epos")
        dispatch.assert_not_called()  # queued; the schedule worker starts it
        self.assertEqual(self.client.post(url, {"action": "stock", "mode": "rm -rf"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"action": "shell"}).status_code, 400)

    def test_update_requires_permission_and_csrf(self):
        url = reverse("epos_qbo:workspace-update", args=["company_a"])
        viewer = User.objects.create_user("viewer", password="x")
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(url, {"action": "stock"}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, {"action": "stock"}).status_code, 403)
        self.assertEqual(csrf.get(url).status_code, 405)

    def test_background_job_runs_exact_command_and_explains_busy(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_WORKSPACE_READ, company_key="company_a", requested_by=self.user,
                                    inventory_options_json={"action": "stock", "mode": "qbo"})
        with mock.patch("apps.epos_qbo.management.commands.update_company_records.subprocess.call", return_value=0) as call:
            call_command("update_company_records", str(job.id))
        self.assertEqual(call.call_args.args[0][1:], ["-m", "code_scripts.akponora_ops.stock_snapshot", "run", "--no-epos"])
        with mock.patch("apps.epos_qbo.management.commands.update_company_records.subprocess.call", return_value=5):
            with self.assertRaisesMessage(CommandError, "already running"):
                call_command("update_company_records", str(job.id))


# --------------------------------------------------------------------------- per-product approval
class ProductApprovalTests(RegisterFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.write_plan([
            decision("101", "Fanta 50cl", "CREATE_INVENTORY", "AUTO"),
            decision("102", "Fanta 50cl x12", "MAPPING_ONLY", "REVIEW", master="101"),
            decision("103", "Mystery", "CREATE_NONINVENTORY", "HOLD"),
            decision("104", "Already done", "CREATE_INVENTORY", "AUTO"),
        ], applied=("104",))

    def test_one_item_per_product_hold_cannot_be_approved_and_applied_hidden(self):
        items = {i["identity"]: i for i in attention.inbox()[0] if i["kind"] == "product"}
        self.assertEqual(set(items), {"101", "102", "103"})
        self.assertTrue(items["101"]["approve"])
        self.assertFalse(items["103"]["approve"])
        self.assertTrue(items["103"]["exclude"])
        self.assertIn("starts at quantity zero", items["101"]["reason"])

    def test_approve_command_selects_only_this_product_with_decision_sha(self):
        item = self.item("product", "101")
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            self.assertEqual(attention_actions.execute(self.record(item, "approve")), 0)
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[1:4], ["-m", "code_scripts.akponora_ops.catalogue_sync", "apply"])
        self.assertEqual(cmd[cmd.index("--plan-dir") + 1], str(self.folder.resolve()))
        self.assertEqual(cmd[cmd.index("--expect-sha") + 1], "plansha")
        self.assertEqual(cmd[cmd.index("--only") + 1], "101")
        self.assertEqual(cmd[cmd.index("--expect-decision-shas") + 1], "101=sha101")
        self.assertEqual(cmd[cmd.index("--approval-ref") + 1], "Approved by Ada Obi in the portal, 2026-10-03 14:05 Lagos")
        self.assertIn("--json", cmd)
        self.assertIn("--no-slack", cmd)

    def test_pack_child_brings_its_master_from_the_same_plan(self):
        item = self.item("product", "102")
        self.assertIn("main product", item["reason"])
        cmd = attention_actions.tool_command(item, "approve", "ref")
        self.assertEqual(cmd[cmd.index("--only") + 1], "101,102")
        self.assertEqual(cmd[cmd.index("--expect-decision-shas") + 1], "101=sha101,102=sha102")

    def test_mapped_or_excluded_products_leave_the_inbox(self):
        self.write_exclusion("product", "101")
        self.assertNotIn("101", {i["identity"] for i in attention.inbox()[0] if i["kind"] == "product"})
        self.write_csv(self.maps / "approved.csv", MAPPING_COLUMNS, [{c: "" for c in MAPPING_COLUMNS} | {"EPOS Product ID": "102"}])
        self.assertNotIn("102", {i["identity"] for i in attention.inbox()[0] if i["kind"] == "product"})

    def test_approving_one_product_does_not_invalidate_the_others(self):
        before = self.item("product", "102")["snapshot"]
        (self.folder / "applied_state.json").write_text(json.dumps({"applied": {"104": {}, "101": {}}}))
        (self.folder / "apply_receipt.json").write_text(json.dumps({"applied": [{"pid": "101"}]}))
        self.assertEqual(self.item("product", "102")["snapshot"], before)

    def test_dont_ask_again_runs_review_exclusions_with_automatic_reference(self):
        item = self.item("product", "103")
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(self.record(item, "exclude"))
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[1:4], ["-m", "code_scripts.akponora_ops.review_exclusions", "add"])
        self.assertEqual(cmd[cmd.index("--kind") + 1], "product")
        self.assertEqual(cmd[cmd.index("--key") + 1], "103")
        self.assertEqual(cmd[cmd.index("--reason") + 1], "checked the PO")
        self.assertTrue(cmd[cmd.index("--added-by") + 1].startswith("Approved by Ada Obi in the portal"))

    def test_exclude_from_stock_list_without_a_plan(self):
        self.write_snapshot()
        response = self.client.get(self.confirm, {"action": "exclude", "kind": "product", "pid": "105"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(self.confirm, {"action": "exclude", "kind": "product", "pid": "999"}).status_code, 400)
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job, actor="ada", action="exclude", reason="not ours", confirmation_id="x105",
                                                   payload={"exclusion_kind": "product", "exclusion_key": "105", "approval_ref": "Approved by Ada"})
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(record)
        self.assertEqual(call.call_args.args[0][call.call_args.args[0].index("--key") + 1], "105")

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_confirmation_fills_reference_needs_reason_and_has_no_chat_field(self, dispatch):
        item = self.item("product", "101")
        response = self.client.get(self.confirm, {"key": item["key"], "action": "approve"})
        self.assertContains(response, "Approve product")
        self.assertContains(response, "Approved by Ada Obi in the portal,")
        self.assertNotContains(response, "chat")
        self.assertNotContains(response, 'name="approval_ref"')
        token = response.context["token"]
        self.assertEqual(self.client.post(self.confirm, {"token": token, "reason": "", "confirmed": "yes"}).status_code, 400)
        self.assertEqual(self.client.post(self.confirm, {"token": token, "reason": "x" * 400, "confirmed": "yes"}).status_code, 400)
        self.assertEqual(self.client.post(self.confirm, {"token": token, "reason": "EPOS row checked", "confirmed": "yes", "approval_ref": "spoof"}).status_code, 302)
        ref = PortalReviewAction.objects.get().payload["approval_ref"]
        self.assertRegex(ref, r"^Approved by Ada Obi in the portal, \d{4}-\d{2}-\d{2} \d{2}:\d{2} Lagos$")

    def test_approval_reference_falls_back_to_username(self):
        user = User.objects.create_user("plainuser")
        self.assertTrue(attention_actions.approval_reference(user).startswith("Approved by plainuser in the portal, "))


# --------------------------------------------------------------------------- suppliers
class SupplierTests(RegisterFixtures, TestCase):
    def setUp(self):
        super().setUp()
        vendor = {"state": "HOLD_NEAR_MATCH", "epos_name": "Coca Cola Nig", "display_name": "Coca Cola Nig", "vendor_id": "",
                  "best_score": 0.95, "candidates": ["10 Coca-Cola Nigeria (0.95)"], "po_refs": ["PO-9"], "detail": "looks like"}
        self.run = self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02"), files={
            "bills/summary.json": json.dumps({"payloads_sha256": "billsha", "vendor_actions": [vendor]}),
            "bills/review.csv": "PO,EPOS Supplier,Status,Reasons,Warnings,Approve\nPO-9,Coca Cola Nig,HOLD,supplier not approved,,\n"})
        self.write_csv(self.maps / "vendors.csv", ["EPOS Supplier Id", "EPOS Supplier Name", "QBO Vendor Id", "QBO Vendor Name", "Approved By"],
                       [{"EPOS Supplier Id": "3", "EPOS Supplier Name": "Nestle", "QBO Vendor Id": "20", "QBO Vendor Name": "Nestle Nigeria", "Approved By": "Approved by Ada"}])

    def check_result(self, **over):
        result = {"result": "dry_run", "problems": [], "warnings": [], "payload_sha256": "vsha",
                  "payload": {"action": "link", "qbo_vendor_id": "10", "qbo_vendor_name": "Coca-Cola Nigeria"}}
        result.update(over)
        return result

    def run_check(self, choice, result):
        item = self.item("vendor")
        proc = mock.Mock(returncode=0 if result["result"] == "dry_run" else 2, stdout=json.dumps(result), stderr="")
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.run", return_value=proc) as run:
            self.assertEqual(attention_actions.execute(self.record(item, "preview", choice=choice)), 0)
        return run.call_args.args[0]

    def test_check_then_approve_link_uses_dry_run_sha(self):
        item = self.item("vendor")
        self.assertFalse(item["approve"])
        self.assertTrue(item["preview"])
        cmd = self.run_check({"mode": "link", "link_to": "10"}, self.check_result())
        self.assertEqual(cmd[1:4], ["-m", "code_scripts.akponora_ops.vendors", "approve"])
        self.assertEqual(cmd[cmd.index("--link-to") + 1], "10")
        self.assertEqual(cmd[cmd.index("--epos-name") + 1], "Coca Cola Nig")
        self.assertIn("--dry-run", cmd)
        item = self.item("vendor")
        self.assertTrue(item["approve"])
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(self.record(item, "approve"))
        cmd = call.call_args.args[0]
        self.assertNotIn("--dry-run", cmd)
        self.assertEqual(cmd[cmd.index("--expect-sha") + 1], "vsha")
        self.assertEqual(cmd[cmd.index("--link-to") + 1], "10")
        self.assertTrue(cmd[cmd.index("--approval-ref") + 1].startswith("Approved by Ada Obi"))

    def test_create_choice_and_preflight_problem_blocks_approval(self):
        cmd = self.run_check({"mode": "create", "display_name": "Coca Cola Nig Ltd"},
                             self.check_result(result="preflight_failed", problems=["DisplayName already used by Customer 4"]))
        self.assertIn("--create", cmd)
        self.assertEqual(cmd[cmd.index("--display-name") + 1], "Coca Cola Nig Ltd")
        item = self.item("vendor")
        self.assertFalse(item["approve"])
        response = self.client.get(reverse("epos_qbo:attention"))
        self.assertContains(response, "DisplayName already used by Customer 4")

    @mock.patch("apps.epos_qbo.services.job_runner.dispatch_next_queued_job")
    def test_check_form_only_accepts_listed_suppliers(self, dispatch):
        item = self.item("vendor")
        response = self.client.get(self.confirm, {"key": item["key"], "action": "preview"})
        self.assertContains(response, "Coca-Cola Nigeria")
        token = response.context["token"]
        self.assertEqual(self.client.post(self.confirm, {"token": token, "reason": "r", "confirmed": "yes", "choice": "link:99"}).status_code, 400)
        self.assertEqual(self.client.post(self.confirm, {"token": token, "reason": "r", "confirmed": "yes", "choice": "link:10"}).status_code, 302)
        self.assertEqual(PortalReviewAction.objects.get().payload["choice"], {"mode": "link", "link_to": "10"})

    def test_supplier_dont_ask_again_and_tab(self):
        item = self.item("vendor")
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(self.record(item, "exclude"))
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--kind") + 1], "vendor")
        self.assertEqual(cmd[cmd.index("--key") + 1], "Coca Cola Nig")
        self.write_exclusion("vendor", "COCA COLA")  # stored normalized, as the tool does
        self.assertFalse(any(i["kind"] == "vendor" for i in attention.inbox()[0]))
        response = self.client.get(self.page, {"tab": "suppliers"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nestle Nigeria")
        self.assertContains(response, "Supplier · COCA COLA")
        self.assertContains(response, "action=unexclude&amp;kind=vendor&amp;ex=COCA%20COLA")

    def test_suppliers_tab_lists_held_supplier_actions(self):
        response = self.client.get(self.page, {"tab": "suppliers"})
        self.assertContains(response, "Check with QuickBooks")
        self.assertContains(response, "Coca Cola Nig")
        response = self.client.get(self.page, {"tab": "suppliers", "q": "nestle"})
        self.assertEqual(len(response.context["supplier_page"].object_list), 1)

    def test_ask_again_removes_through_the_tool(self):
        self.write_exclusion("bill", "PO-9")
        response = self.client.get(self.confirm, {"action": "unexclude", "kind": "bill", "ex": "PO-9"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(self.confirm, {"action": "unexclude", "kind": "bill", "ex": "PO-1"}).status_code, 400)
        job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW)
        record = PortalReviewAction.objects.create(job=job, actor="ada", action="unexclude", reason="came back", confirmation_id="un",
                                                   payload={"exclusion_kind": "bill", "exclusion_key": "PO-9", "approval_ref": "Approved by Ada"})
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(record)
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[3:], ["remove", "--kind", "bill", "--key", "PO-9", "--removed-by", "Approved by Ada", "--reason", "came back"])

    def test_bill_dont_ask_again_hides_the_po(self):
        item = self.item("bill", "PO-9")
        self.assertTrue(item["exclude"])
        with mock.patch("apps.epos_qbo.services.attention_actions.subprocess.call", return_value=0) as call:
            attention_actions.execute(self.record(item, "exclude"))
        self.assertEqual(call.call_args.args[0][call.call_args.args[0].index("--kind") + 1:][:3], ["bill", "--key", "PO-9"])
        self.write_exclusion("bill", "PO-9")
        self.assertFalse(any(i["kind"] == "bill" for i in attention.inbox()[0]))


# --------------------------------------------------------------------------- deposits
DAY_SUMMARY = {"day": "2026-10-01", "status": "READY", "state": "READY", "reasons": [], "warnings": ["sheet total N100.00 vs receipts N99.00"],
               "sheet_total": "100000.00", "receipts_total": "100000.00", "receipts": {"cash": 3, "card": 5},
               "sheet_by_bank": {}, "target_by_bank": {}, "final_by_bank": {}, "deposits_new": 2, "transfers_new": 1,
               "existing": [], "payload_count": 3, "payloads_sha256": "depsha", "post_command": "x"}
REVIEW = ("Day,Bank No,QBO Account Id,Kind,Sheet Lines,Sheet Amount,Target,Deposited,Receipts,Deposit DocNumbers,Transfer Out,Transfer In,Final,Final - Target,Status\n"
          "2026-10-01,4000850527,77,card,1,60000.00,60000.00,0,5,,1500.00,0,0,0,NEW\n"
          "2026-10-01,1000,78,cash,1,40000.00,40000.00,0,3,,0,1500.00,0,0,NEW\n")


class DepositTests(RegisterFixtures, TestCase):
    def write_days(self, days):
        path = self.tmp / "ops/company_a/uf_deposits/days.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"days": days}))

    def full_fixture(self):
        self.write_days({
            "2026-09-25": {"status": "DEPOSITED", "reason": "", "updated_at": "2026-09-27T18:00:00+00:00"},
            "2026-09-26": {"status": "WAITING_SHEET", "reason": "CASH (System 1) box is blank (type 0 if there was no cash)", "updated_at": "2026-10-02T18:00:00+00:00"},
            "2026-09-27": {"status": "NO_SALES", "reason": "no SalesReceipts in QBO for 2026-09-27 yet (sales not posted?)", "updated_at": "2026-10-02T18:00:00+00:00"},
            "2026-09-28": {"status": "HELD", "reason": "sheet total N100,000.00 vs receipts N90,000.00: difference N10,000.00 is over the tolerance N1,000.00", "updated_at": "2026-10-02T18:00:00+00:00"},
            "2026-10-01": {"status": "READY", "reason": "", "updated_at": "2026-10-02T18:00:00+00:00"}})
        files = {"uf/2026-10-01/summary.json": json.dumps(DAY_SUMMARY), "uf/2026-10-01/review.csv": REVIEW,
                 "uf/scheduled.json": json.dumps({"uf_balance": "1234567.50", "till_sheet": {
                     "as_of": "2026-10-02", "last_complete_day": "2026-10-01", "missing": ["2026-10-02"], "incomplete": [],
                     "complete_not_deposited": [], "deposited": ["2026-09-25"], "text": "Till sheet: last day entered 1 Oct."}})}
        self.make_run("2026-10-02", "run_170000Z", _summary("2026-10-02", steps=[_step("uf", "review", {"uf_balance": "1234567.50"})]), files=files)
        self.write_csv(self.maps / "till_accounts.csv", ["Till sheet line", "Terminal / TID", "QBO account number", "QBO account Id", "Kind", "Active", "Note"],
                       [{"Till sheet line": "Moniepoint POS 1", "Terminal / TID": "2KUD", "QBO account number": "4000850527", "QBO account Id": "77", "Kind": "card", "Active": "yes", "Note": ""},
                        {"Till sheet line": "Petty cash", "Terminal / TID": "", "QBO account number": "1000", "QBO account Id": "78", "Kind": "cash", "Active": "yes", "Note": ""}])

    def test_page_renders_without_any_deposit_files(self):
        response = self.client.get(self.page, {"tab": "deposits"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Banking isn't switched on yet")
        self.assertContains(response, "No deposit records yet")
        self.assertEqual(response.context["deposit_errors"], [])
        self.assertContains(response, "Not checked yet")

    def test_incomplete_sheet_does_not_display_zero_or_a_false_difference(self):
        rows = deposits.day_rows(
            {"2026-09-25": {"status": "WAITING_SHEET"}},
            {"2026-09-25": ({"sheet_total": "0", "receipts_total": "100000"}, None)},
            {}, deposits.settings(), date(2026, 9, 25))
        self.assertEqual(rows[0]["sheet"], "Not entered")
        self.assertEqual(rows[0]["difference"], "")
        self.assertFalse(rows[0]["outside"])

    def test_entered_zero_is_still_a_real_comparison(self):
        rows = deposits.day_rows(
            {"2026-09-25": {"status": "HELD"}},
            {"2026-09-25": ({"sheet_total": "0", "receipts_total": "100000"}, None)},
            {}, deposits.settings(), date(2026, 9, 25))
        self.assertEqual(rows[0]["sheet"], "₦0.00")
        self.assertTrue(rows[0]["outside"])

    def test_every_state_has_a_plain_label_and_cards(self):
        self.full_fixture()
        response = self.client.get(self.page, {"tab": "deposits"})
        rows = {r["date"]: r for r in response.context["deposit_page"].object_list}
        self.assertEqual(rows["2026-09-25"]["label"], "Banked")
        self.assertIn("Moved to the banks on 27 Sep 2026", rows["2026-09-25"]["message"])
        self.assertEqual(rows["2026-09-26"]["label"], "Waiting for till sheet")
        self.assertIn("The till sales breakdown for this day isn't complete yet.", rows["2026-09-26"]["message"])
        self.assertEqual(rows["2026-09-27"]["label"], "No sales posted yet")
        self.assertEqual(rows["2026-09-28"]["label"], "Needs attention")
        self.assertIn("more than the allowed ₦1,000.00", rows["2026-09-28"]["message"])
        self.assertEqual(rows["2026-10-01"]["label"], "Ready to bank")
        self.assertEqual(rows["2026-10-02"]["label"], "Waiting for till sheet")  # missing on the sheet
        self.assertEqual(rows["2026-10-02"]["sheet"], "Not entered")
        self.assertEqual(list(rows)[0], "2026-10-02")  # newest first
        self.assertEqual(response.context["deposit_balance"], "₦1,234,567.50")
        self.assertEqual(response.context["deposit_last_day"], date(2026, 10, 1))
        self.assertEqual(response.context["deposit_waiting"], 2)
        self.assertContains(response, "Approve deposit")

    def test_day_detail_uses_bank_names_and_true_up_sentence(self):
        self.full_fixture()
        response = self.client.get(self.page, {"tab": "deposits", "day": "2026-10-01"})
        banks = response.context["deposit_banks"]
        self.assertEqual(banks[0]["name"], "Moniepoint POS 1 (2KUD)")
        self.assertEqual(banks[1]["name"], "Petty cash")
        self.assertContains(response, "move ₦1,500.00 between banks")
        self.assertContains(response, "depsha")
        self.assertContains(response, "Plan this day again")

    def test_reason_catalogue_matches_tool_strings(self):
        cases = {
            "sheet total N100,000.00 vs receipts N90,000.00: difference N10,000.00 is over the tolerance N1,000.00": "differ by ₦10,000.00",
            "sheet total N90,000.00 vs receipts N100,000.00: difference N-10,000.00 is over the tolerance N1,000.00": "differ by ₦10,000.00",
            "till line 'POS 9' = N5,000.00: TID 9X not in till_accounts.csv": "line we don't recognise (POS 9)",
            "till line 'Misc' = N5,000.00: unknown till line (no terminal number) - add it to till_accounts.csv / fix the sheet": "line we don't recognise (Misc)",
            "till line 'Cash' is negative (N-5.00)": "negative amount for Cash",
            "2026-09-28 is inside the QBO closed period (BookCloseDate 2026-09-30)": "closed period",
            "QBO account Id 77 (4000850527) not found": "isn't set up correctly in QuickBooks (4000850527)",
            "QBO account 4000850527 (Id 77) is inactive": "isn't set up correctly in QuickBooks",
            "2 receipt(s) already deposited outside this tool - resolve by hand: 12 N5.00 x": "already banked by hand",
            "receipts total N20,000,000.00 is over the automatic cap N15,000,000.00 (OIAT_COMPANY_A_UF_AUTO_CAP) - post by hand with a chat yes": "automatic limit (₦15,000,000.00)",
            "post stopped: POST /deposit failed 400: bad": "stopped part-way",
            "no SalesReceipts in QBO for 2026-09-27 yet (sales not posted?)": "aren't in QuickBooks yet",
            "CASH (System 1) box is blank (type 0 if there was no cash)": "The till sales breakdown for this day isn't complete yet.",
            "SYSTEM (EPOS total) box is blank - the day is not finished on the sheet": "SYSTEM total is blank",
            "sheet day is blank (all boxes empty or zero)": "is empty",
            "till sheet tab 'OCT 2026' has no block for 2026-10-02": "isn't filled in yet",
            "till sheet tab 'OCT 2026' has 2 blocks for 2026-10-02": "appears twice",
            "something brand new": "Open details",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIn(expected, deposits.reason(raw))

    def test_approve_deposit_command_and_refresh_jobs(self):
        self.full_fixture()
        item = self.item("deposit", "2026-10-01")
        self.assertTrue(item["approve"])
        cmd = attention_actions.tool_command(item, "approve", "Approved by Ada Obi in the portal, 2026-10-03 14:05 Lagos")
        self.assertEqual(cmd[1:4], ["-m", "code_scripts.akponora_ops.uf_deposits", "post"])
        self.assertEqual(cmd[cmd.index("--expect-sha") + 1], "depsha")
        self.assertTrue(cmd[cmd.index("--plan-dir") + 1].endswith("uf/2026-10-01"))
        self.assertEqual(workspace_jobs.command("deposit_status", "j")[-3:], ["code_scripts.akponora_ops.uf_deposits", "status", "--json"])
        plan = workspace_jobs.command("deposit_plan", "j", "2026-10-01")
        self.assertEqual(plan[plan.index("--from") + 1], "2026-10-01")
        self.assertEqual(plan[plan.index("--to") + 1], "2026-10-01")
        with self.assertRaises(ValueError):
            workspace_jobs.command("deposit_plan", "j", "2026-09-01")
        with self.assertRaises(ValueError):
            workspace_jobs.command("deposit_plan", "j", "2026-10-05")

    def test_portal_replan_overrides_older_state(self):
        self.full_fixture()
        replan = self.tmp / "ops/company_a/portal_reads/job1/deposits/2026-09-28"
        replan.mkdir(parents=True)
        (replan / "summary.json").write_text(json.dumps(DAY_SUMMARY | {"day": "2026-09-28", "payloads_sha256": "newsha"}))
        response = self.client.get(self.page, {"tab": "deposits"})
        rows = {r["date"]: r for r in response.context["deposit_page"].object_list}
        self.assertEqual(rows["2026-09-28"]["status"], "READY")
        self.assertTrue(self.item("deposit", "2026-09-28")["approve"])

    def test_tolerance_edit_is_admin_only_and_audited(self):
        url = reverse("epos_qbo:deposit-settings", args=["company_a"])
        self.assertEqual(self.client.post(url, {"tolerance": "2000", "tolerance_pct": "1", "reason": "store asked"}).status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_portal_settings"))
        self.assertEqual(self.client.post(url, {"tolerance": "-1", "tolerance_pct": "1", "reason": "x"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"tolerance": "2000", "tolerance_pct": "1"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"tolerance": "2000", "tolerance_pct": "1", "reason": "store asked"}).status_code, 302)
        text = (self.tmp / "ops/company_a/uf_deposits/settings.env").read_text()
        self.assertIn("OIAT_COMPANY_A_UF_TOLERANCE=2000", text)
        self.assertIn("OIAT_COMPANY_A_UF_TOLERANCE_PCT=1", text)
        change = PortalSettingChange.objects.get(setting="OIAT_COMPANY_A_UF_TOLERANCE")
        self.assertEqual((change.old_value, change.new_value, change.reason), ("1000", "2000", "store asked"))
        self.assertEqual(deposits.settings()["OIAT_COMPANY_A_UF_TOLERANCE"], 2000)


# --------------------------------------------------------------------------- no QuickBooks from views
class NoQuickBooksFromViewsTests(RegisterFixtures, TestCase):
    MODULES = ("views_attention.py", "views_workspace.py", "services/products.py", "services/deposits.py",
               "services/suppliers.py", "services/exclusions.py", "services/workspace_jobs.py")

    def test_view_and_register_modules_never_reference_quickbooks_clients(self):
        root = Path(attention.__file__).resolve().parents[1]
        for name in self.MODULES:
            tree = ast.parse((root / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
                    self.assertFalse(any(re.search(r"qbo|token_manager|w7_create_items|requests", n or "", re.I) for n in names), (name, names))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"post_json", "get_json", "QBOClient", "for_company_a"}, name)

    def test_pages_render_with_quickbooks_and_subprocess_blocked(self):
        self.write_snapshot()
        with mock.patch("code_scripts.scripts.akponora_cutover.w7_create_items.QBOClient.for_company_a", side_effect=AssertionError("QBO")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("tool")), mock.patch("subprocess.run", side_effect=AssertionError("tool")):
            for tab in ("products", "suppliers", "deposits"):
                self.assertEqual(self.client.get(self.page, {"tab": tab}).status_code, 200)
            self.assertEqual(self.client.get(reverse("epos_qbo:attention")).status_code, 200)

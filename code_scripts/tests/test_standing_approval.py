"""Company A standing auto-approval (unattended October+ daily posting).

Every QBO/EPOS call is faked; any real HTTP attempt fails the test. The real
qbo_upload preflight/posting code runs against a fake QBO.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pandas as pd

from code_scripts import qbo_upload, run_all_companies, run_pipeline, standing_approval
from code_scripts.operations_controls import payload_digest, posting_hold_path
from code_scripts.tests.test_cutover_review_fixes import (
    CATCH_ALL_ITEM,
    OCT_ITEM,
    CompanyAUploadHarness,
    FakeResponse,
)

REF = "owner standing approval 2026-10-01 (chat #42)"
ON = {
    "OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1",
    "OIAT_COMPANY_A_STANDING_APPROVAL_REF": REF,
}
DAY = "2026-10-01"


class SettingsTests(unittest.TestCase):
    def test_requires_both_env_vars(self):
        self.assertIsNone(standing_approval.standing_approval_settings({}))
        self.assertIsNone(standing_approval.standing_approval_settings(
            {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1"}))
        self.assertIsNone(standing_approval.standing_approval_settings(
            {"OIAT_COMPANY_A_STANDING_APPROVAL_REF": REF}))
        self.assertIsNone(standing_approval.standing_approval_settings(
            {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "0", "OIAT_COMPANY_A_STANDING_APPROVAL_REF": REF}))
        self.assertIsNone(standing_approval.standing_approval_settings(
            {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1", "OIAT_COMPANY_A_STANDING_APPROVAL_REF": "   "}))
        self.assertEqual(standing_approval.standing_approval_settings(ON), {"ref": REF, "max_gross": None})

    def test_max_gross_cap_parsing_fails_closed(self):
        env = dict(ON, OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS="5,000,000")
        self.assertEqual(standing_approval.standing_approval_settings(env)["max_gross"], Decimal("5000000"))
        for bad in ("lots", "-1", "0", "NaN"):
            with self.assertRaises(standing_approval.StandingApprovalConfigError):
                standing_approval.standing_approval_settings(dict(ON, OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS=bad))


class StandingApprovalHarness(CompanyAUploadHarness):
    """Fake QBO keeps posted receipts, so a rerun sees them as existing."""

    def setUp(self):
        super().setUp()
        self.install_october_mapping()
        self.qbo_receipts: dict[str, dict] = {}
        self.all_posts: list[dict] = []
        self.step_log: list[dict] = []
        self.after_dry_run = None  # optional hook(evidence_path)
        for key in ("OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED", "OIAT_COMPANY_A_STANDING_APPROVAL_REF",
                    "OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS"):
            os.environ.pop(key, None)

    def enable(self, **extra):
        os.environ.update(ON)
        os.environ.update(extra)

    def fake_qbo(self, posts, calls):
        def fake(method, url, token_mgr, **kwargs):
            calls.append((method, url))
            if method == "POST":
                if "/salesreceipt" not in url:
                    raise AssertionError("unexpected POST " + url)
                payload = kwargs["json"]
                posts.append({"url": url, "payload": payload})
                self.qbo_receipts[payload["DocNumber"]] = json.loads(json.dumps(payload))
                return FakeResponse({"SalesReceipt": {"Id": str(7000 + len(self.qbo_receipts)),
                                                      "DocNumber": payload["DocNumber"]}})
            if "/item/15030" in url:
                return FakeResponse({"Item": CATCH_ALL_ITEM})
            if "/item/90001" in url:
                return FakeResponse({"Item": OCT_ITEM})
            if "/item/" in url:
                raise AssertionError("unexpected item lookup " + url)
            if "SalesReceipt%20where%20DocNumber" in url:
                for doc, receipt in self.qbo_receipts.items():
                    if f"%27{doc}%27" in url:
                        return FakeResponse({"QueryResponse": {"SalesReceipt": [receipt]}})
            return FakeResponse({"QueryResponse": {}})
        return fake

    def upload(self, csv_path, target, *, dry_run):
        posts, calls = [], []
        argv = ["qbo_upload.py", "--company", "company_a", "--target-date", target]
        if dry_run:
            argv.append("--dry-run")
        code = 0
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(qbo_upload, "get_available_companies", return_value=["company_a"]), \
             mock.patch.object(qbo_upload, "load_company_config", return_value=self.config), \
             mock.patch.object(qbo_upload, "ensure_company_runtime_compatible", return_value=None), \
             mock.patch.object(qbo_upload, "verify_realm_match", return_value=None), \
             mock.patch.object(qbo_upload, "TokenManager", return_value=mock.MagicMock(request_stats={})), \
             mock.patch.object(qbo_upload, "find_latest_single_csv", return_value=str(csv_path)), \
             mock.patch.object(qbo_upload, "get_repo_root", return_value=str(self.work)), \
             mock.patch.object(qbo_upload, "check_qbo_existing_docnumbers",
                               side_effect=lambda docs, *a, **k: ({d for d in docs if d in self.qbo_receipts}, {})), \
             mock.patch.object(qbo_upload, "load_uploaded_docnumbers", return_value=set()), \
             mock.patch.object(qbo_upload, "save_uploaded_docnumber", return_value=None), \
             mock.patch.object(qbo_upload, "get_or_create_item_id", side_effect=AssertionError("name resolution")), \
             mock.patch.object(qbo_upload, "create_inventory_item", side_effect=AssertionError("Inventory create")), \
             mock.patch.object(qbo_upload, "_make_qbo_request", side_effect=self.fake_qbo(posts, calls)), \
             mock.patch("builtins.print"):
            try:
                qbo_upload.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, posts

    # run_pipeline.run_step replacement: runs the real qbo_upload with the step's env.
    def fake_run_step(self, label, script_name, args=None, env_overrides=None, env_remove=None):
        args = list(args or [])
        if script_name == "epos_playwright.py":
            out = Path(args[args.index("--output-dir") + 1]) / args[args.index("--output-filename") + 1]
            out.parent.mkdir(parents=True, exist_ok=True)
            self.raw.to_csv(out, index=False)
            self.step_log.append({"script": script_name})
            return
        if script_name == "transform.py":
            target = args[args.index("--target-date") + 1]
            self.csv_path = self.transform_to_csv(self.raw, target)
            self.step_log.append({"script": script_name})
            return
        self.assertEqual(script_name, "qbo_upload.py")
        target = args[args.index("--target-date") + 1]
        saved = dict(os.environ)
        try:
            for name in env_remove or []:
                os.environ.pop(name, None)
            os.environ.update(env_overrides or {})
            dry = "--dry-run" in args
            self.step_log.append({"script": script_name, "dry_run": dry,
                                  "approval_file": os.environ.get("COMPANY_A_POSTING_APPROVAL_FILE"),
                                  "env_overrides": env_overrides, "env_remove": env_remove})
            code, posts = self.upload(self.csv_path, target, dry_run=dry)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.all_posts.extend(posts)
        if dry and self.after_dry_run:
            self.after_dry_run(standing_approval.evidence_path(target))
        if code:
            raise SystemExit(f"[ERROR] {label} failed with exit code {code}")

    def prepare(self, raw=None, target=DAY):
        self.raw = raw if raw is not None else self.october_raw()
        self.csv_path = self.transform_to_csv(self.raw, target)
        self.raw_file = self.work / f"BookKeeping_split_{target}.csv"
        self.raw.to_csv(self.raw_file, index=False)

    def auto_upload(self, target=DAY):
        settings = run_pipeline.standing_approval_for_day("company_a", target, self.config)
        self.assertIsNotNone(settings)
        with mock.patch.object(run_pipeline, "run_step", side_effect=self.fake_run_step):
            return run_pipeline.run_standing_approval_upload(
                "Phase 3", target, self.config, str(self.raw_file),
                ["--company", "company_a", "--target-date", target], settings)

    def hold(self):
        return json.loads(posting_hold_path().read_text())

    def assert_refused(self, gate, *, posts=0):
        with self.assertRaises(SystemExit) as ctx:
            self.auto_upload()
        message = str(ctx.exception)
        self.assertIn(gate, message)
        self.assertIn("nothing posted", message)
        self.assertEqual(len(self.all_posts), posts)
        self.assertTrue(posting_hold_path().exists())
        self.assertFalse((self.state / "approvals").exists() and any((self.state / "approvals").iterdir()))
        # Only the dry-run ran: no posting subprocess was started.
        self.assertFalse([s for s in self.step_log if s.get("script") == "qbo_upload.py" and not s["dry_run"]])
        return message


class StandingApprovalFlowTests(StandingApprovalHarness):
    def test_gates_pass_manifest_created_and_posted(self):
        self.enable()
        os.environ["COMPANY_A_POSTING_APPROVAL_FILE"] = str(self.root / "stale-manual.json")
        self.prepare()
        result = self.auto_upload()
        self.assertTrue(result["passed"])
        self.assertEqual(len(self.all_posts), 1)
        item_ids = {l["SalesItemLineDetail"]["ItemRef"]["value"] for l in self.all_posts[0]["payload"]["Line"]
                    if l.get("DetailType") == "SalesItemLineDetail"}
        self.assertEqual(item_ids, {"90001"})
        manifest_path = Path(result["manifest"])
        self.assertEqual(manifest_path.parent, self.state / "approvals")
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["approved_by"], "auto:scheduler")
        self.assertEqual(manifest["chat_approval_ref"], REF)
        self.assertEqual(manifest["payload_sha256"], [payload_digest(self.all_posts[0]["payload"])])
        ttl = datetime.fromisoformat(manifest["expires_at"]) - datetime.now(timezone.utc)
        self.assertTrue(timedelta(hours=5, minutes=55) < ttl <= timedelta(hours=6))
        # The dry-run never sees an approval file; the post gets only the auto manifest.
        dry, post = [s for s in self.step_log if s["script"] == "qbo_upload.py"]
        self.assertTrue(dry["dry_run"])
        self.assertIsNone(dry["approval_file"])
        self.assertFalse(post["dry_run"])
        self.assertEqual(post["approval_file"], str(manifest_path))
        # Parent environment untouched (manifest is for that subprocess only).
        self.assertEqual(os.environ["COMPANY_A_POSTING_APPROVAL_FILE"], str(self.root / "stale-manual.json"))
        self.assertFalse(posting_hold_path().exists())

    def test_rerun_of_posted_day_posts_nothing_and_does_not_error(self):
        self.enable()
        self.prepare()
        self.auto_upload()
        self.assertEqual(len(self.all_posts), 1)
        self.step_log.clear()
        result = self.auto_upload()
        self.assertTrue(result["passed"])
        self.assertIsNone(result["manifest"])
        self.assertEqual(result["summary"]["receipts_to_post"], 0)
        self.assertEqual(result["summary"]["receipts_already_in_qbo"], 1)
        self.assertEqual(len(self.all_posts), 1)  # no duplicate
        post = [s for s in self.step_log if s["script"] == "qbo_upload.py" and not s["dry_run"]][0]
        self.assertIsNone(post["approval_file"])
        self.assertFalse(posting_hold_path().exists())

    def test_gate_evidence_incomplete_preflight_failure(self):
        self.enable()
        raw = pd.concat([self.october_raw(), self.october_raw(qty=-1, total=-120000)], ignore_index=True)
        raw.loc[1, "Tender"] = "Transfer"
        self.prepare(raw)
        self.assert_refused("evidence_complete")
        self.assertEqual(self.hold()["source"], "standing_auto_approval")
        self.assertEqual(self.hold()["result"]["failed_gates"][0]["gate"], "evidence_complete")

    def test_gate_evidence_unapproved_or_catch_all_target(self):
        self.enable()
        self.prepare()

        def tamper(path):
            doc = json.loads(path.read_text())
            for entry in doc["payloads"]:
                for line in entry["payload"]["Line"]:
                    if line.get("DetailType") == "SalesItemLineDetail":
                        line["SalesItemLineDetail"]["ItemRef"]["value"] = "15030"
                entry["sha256"] = payload_digest(entry["payload"])
            path.write_text(json.dumps(doc))
        self.after_dry_run = tamper
        message = self.assert_refused("evidence_complete")
        self.assertIn("not an approved mapping target", message)

    def test_gate_totals_payload_gross_differs_from_epos_raw(self):
        self.enable()
        self.prepare()
        extra = self.october_raw(qty=1, total=5000)
        pd.concat([self.raw, extra], ignore_index=True).to_csv(self.raw_file, index=False)
        message = self.assert_refused("totals_match_epos")
        self.assertIn("EPOS raw gross", message)

    def test_gate_totals_tolerates_one_naira(self):
        self.enable()
        self.prepare()
        skewed = self.raw.copy()
        skewed["TOTAL Sales"] = skewed["TOTAL Sales"].astype(float) + 0.6
        skewed.to_csv(self.raw_file, index=False)
        self.assertTrue(self.auto_upload()["passed"])
        self.assertEqual(len(self.all_posts), 1)

    def test_gate_hold_present_refuses_and_keeps_original_hold(self):
        self.enable()
        self.prepare()
        posting_hold_path().write_text(json.dumps({"source": "reconciliation", "why": "earlier mismatch"}))
        self.assert_refused("no_posting_hold")
        self.assertEqual(self.hold(), {"source": "reconciliation", "why": "earlier mismatch"})

    def test_gate_mapping_sha_changed_after_dry_run(self):
        self.enable()
        self.prepare()
        self.after_dry_run = lambda _path: self.mapping.write_text(self.mapping.read_text() + "\n")
        self.assert_refused("mapping_sha_matches")

    def test_gate_max_gross_cap(self):
        self.enable(OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS="100000")
        self.prepare()  # day gross 240,000
        message = self.assert_refused("max_gross_cap")
        self.assertIn("exceeds cap", message)

    def test_invalid_cap_fails_closed_with_hold(self):
        self.enable(OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS="lots")
        with self.assertRaises(SystemExit) as ctx:
            run_pipeline.standing_approval_for_day("company_a", DAY, self.config)
        self.assertIn("max_gross_cap", str(ctx.exception))
        self.assertTrue(posting_hold_path().exists())

    def test_stale_evidence_is_refused(self):
        self.enable()
        self.prepare()

        def backdate(path):
            doc = json.loads(path.read_text())
            doc["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            path.write_text(json.dumps(doc))
        self.after_dry_run = backdate
        message = self.assert_refused("evidence_complete")
        self.assertIn("stale", message)

    def test_scope_only_company_a_on_or_after_cutover_with_both_env_vars(self):
        self.assertIsNone(run_pipeline.standing_approval_for_day("company_a", DAY, self.config))  # env off
        os.environ["OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED"] = "1"
        self.assertIsNone(run_pipeline.standing_approval_for_day("company_a", DAY, self.config))  # no ref
        self.enable()
        self.assertIsNotNone(run_pipeline.standing_approval_for_day("company_a", DAY, self.config))
        self.assertIsNone(run_pipeline.standing_approval_for_day("company_a", "2026-09-30", self.config))
        self.assertIsNone(run_pipeline.standing_approval_for_day("company_b", DAY, self.config))


class PipelineMainTests(StandingApprovalHarness):
    """run_pipeline.main end to end with EPOS, split, reconcile and archive faked."""

    def run_main(self, target=DAY):
        self.prepare(target=target)
        notifications = {"failure": [], "success": []}

        def split(downloaded, from_date, to_date, company_dir, repo_root, config, clear_existing=True):
            return {from_date: str(self.raw_file)}, {}, {}

        def reconcile(company_key, day, config, repo_root):
            qbo = sum(float(l["SalesItemLineDetail"]["TaxInclusiveAmt"]) for r in self.qbo_receipts.values()
                      for l in r["Line"] if l.get("DetailType") == "SalesItemLineDetail")
            epos = float(self.raw["TOTAL Sales"].sum())
            return {"status": "MATCH" if abs(qbo - epos) <= 1 else "MISMATCH", "epos_total": epos,
                    "qbo_total": qbo, "epos_count": 1, "qbo_count": len(self.qbo_receipts),
                    "difference": abs(qbo - epos)}

        with mock.patch.object(run_pipeline, "load_company_config", return_value=self.config), \
             mock.patch.object(run_pipeline, "ensure_company_runtime_compatible", return_value=None), \
             mock.patch.object(run_pipeline, "verify_realm_match", return_value=None), \
             mock.patch.object(run_pipeline, "repo_root", self.work), \
             mock.patch.object(run_pipeline, "run_step", side_effect=self.fake_run_step), \
             mock.patch.object(run_pipeline, "split_csv_by_date", side_effect=split), \
             mock.patch.object(run_pipeline, "_log_raw_vs_processed_totals", return_value=None), \
             mock.patch.object(run_pipeline, "reconcile_company", side_effect=reconcile), \
             mock.patch.object(run_pipeline, "archive_files", return_value=None), \
             mock.patch.object(run_pipeline, "notify_pipeline_start"), \
             mock.patch.object(run_pipeline, "notify_pipeline_update"), \
             mock.patch.object(run_pipeline, "notify_pipeline_success",
                               side_effect=lambda *a, **k: notifications["success"].append(a)), \
             mock.patch.object(run_pipeline, "notify_pipeline_failure",
                               side_effect=lambda *a, **k: notifications["failure"].append(a)):
            code = run_pipeline.main("company_a", target)
        return code, notifications

    def test_main_posts_unattended_when_gates_pass(self):
        self.enable()
        code, notes = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.all_posts), 1)
        self.assertEqual(len(notes["success"]), 1)
        self.assertFalse(notes["failure"])
        self.assertFalse(posting_hold_path().exists())
        # Rerun: nothing new posted, still success.
        code, notes = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.all_posts), 1)

    def test_main_gate_failure_notifies_with_reason_and_exits_non_zero(self):
        self.enable(OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS="1000")
        code, notes = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.all_posts, [])
        self.assertEqual(len(notes["failure"]), 1)
        error_text = notes["failure"][0][2]
        self.assertIn("standing auto-approval refused", error_text)
        self.assertIn("max_gross_cap", error_text)
        self.assertIn("clear-hold", error_text)
        self.assertTrue(posting_hold_path().exists())

    def test_main_hold_present_refuses(self):
        self.enable()
        posting_hold_path().write_text("{}")
        code, notes = self.run_main()
        self.assertEqual((code, self.all_posts), (1, []))
        self.assertIn("no_posting_hold", notes["failure"][0][2])

    def test_main_without_env_vars_keeps_manual_manifest_behaviour(self):
        code, notes = self.run_main()
        self.assertEqual(code, 1)  # no manual manifest -> qbo_upload refuses, as today
        self.assertEqual(self.all_posts, [])
        uploads = [s for s in self.step_log if s["script"] == "qbo_upload.py"]
        self.assertEqual(len(uploads), 1)
        self.assertFalse(uploads[0]["dry_run"])
        self.assertIsNone(uploads[0]["env_overrides"])
        self.assertIsNone(uploads[0]["env_remove"])
        self.assertFalse(posting_hold_path().exists())
        self.assertFalse((self.state / "approvals").exists())

    def test_main_with_only_automation_flag_is_manual(self):
        os.environ["OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED"] = "1"
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        uploads = [s for s in self.step_log if s["script"] == "qbo_upload.py"]
        self.assertEqual([u["dry_run"] for u in uploads], [False])
        self.assertFalse((self.state / "approvals").exists())

    def test_main_explicit_dry_run_never_auto_posts(self):
        self.enable()
        self.prepare()
        with mock.patch.object(run_pipeline, "load_company_config", return_value=self.config), \
             mock.patch.object(run_pipeline, "ensure_company_runtime_compatible", return_value=None), \
             mock.patch.object(run_pipeline, "verify_realm_match", return_value=None), \
             mock.patch.object(run_pipeline, "repo_root", self.work), \
             mock.patch.object(run_pipeline, "run_step", side_effect=self.fake_run_step), \
             mock.patch.object(run_pipeline, "split_csv_by_date",
                               return_value=({DAY: str(self.raw_file)}, {}, {})), \
             mock.patch.object(run_pipeline, "_log_raw_vs_processed_totals", return_value=None), \
             mock.patch.object(run_pipeline, "notify_pipeline_start"), \
             mock.patch.object(run_pipeline, "notify_pipeline_failure"):
            code = run_pipeline.main("company_a", DAY, upload_dry_run=True)
        self.assertEqual((code, self.all_posts), (0, []))
        self.assertFalse((self.state / "approvals").exists())


class RunAllCompaniesOrderTests(unittest.TestCase):
    def run_order(self, env):
        seen = []

        def fake_run(cmd, *a, **k):
            seen.append(cmd[cmd.index("--company") + 1])
            return mock.MagicMock(returncode=0)

        args = run_all_companies._build_parser().parse_args(["--target-date", DAY])
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("code_scripts.load_env.load_env_file"), \
             mock.patch.object(run_all_companies, "get_available_companies",
                               return_value=["company_a", "company_b"]), \
             mock.patch.object(run_all_companies.subprocess, "run", side_effect=fake_run), \
             mock.patch("builtins.print"):
            self.assertEqual(run_all_companies._run_companies(args), 0)
        return seen

    def test_order_unchanged_without_standing_approval(self):
        self.assertEqual(self.run_order({}), ["company_a", "company_b"])

    def test_company_a_runs_last_with_standing_approval(self):
        self.assertEqual(self.run_order(ON), ["company_b", "company_a"])


if __name__ == "__main__":
    unittest.main()

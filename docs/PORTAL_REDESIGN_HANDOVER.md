# Portal redesign: handover

Branch `codex/portal-redesign-phase1`, 3 Oct 2026. Codex built most of it. Claude finished it after Codex's WIP checkpoint (`07a6e82`). Nothing is pushed or deployed.

## What the branch contains

- **Needs your attention inbox** (Codex phase 1, extended). It holds held bills, suppliers, new products, deposit days and the sales posting hold. Each action is confirmed, runs as a background `RunJob` and is audited in `PortalReviewAction`.
  - **Products.** Each product has its own card. Approve runs `catalogue_sync apply --plan-dir … --approval-ref … --expect-sha <plan_sha256> --only <id> --expect-decision-shas <id>=<sha> --json --no-slack`. A pack whose main product is created in the same plan adds the master to `--only`. HOLD products can't be approved. Products that are already applied, mapped or excluded are hidden. A card is bound to `plan.json` and `summary.json` only, so approving one product doesn't invalidate the others.
  - **Suppliers.** "Check with QuickBooks" lets the user choose one of the listed candidates or "create new" with an optional name. It runs `vendors approve … --dry-run` in a background job and saves the result to `ops/company_a/portal_reads/vendor_checks/<key>.json`. Any problems are shown on the card. "Approve supplier" is offered only after a clean check. It runs the same arguments with `--expect-sha <payload_sha256>`. The live QuickBooks read from the old link flow has been removed.
  - **Don't ask again** (products, suppliers, bills) runs `review_exclusions add --kind … --key … --reason <reason> --added-by <ref>`. "Ask again" on the Suppliers tab runs `review_exclusions remove`. Excluded items leave the inbox.
  - **Approval reference.** It is filled automatically as `Approved by <full name or username> in the portal, YYYY-MM-DD HH:MM Lagos`. There is no field for a chat reference. A short reason (at most 300 characters) is required and stored in the audit record.
- **Home, Daily runs, company tabs, plain-English messages, navigation.** These are Codex's phase 2 work and are unchanged. See `docs/CODEX_PORTAL_*_REPORT.md`.
- **Products & Stock** (Company A). This tab reads `ops/company_a/stock_snapshot/latest.json`, `mappings/company_a/approved.csv` and `ops/company_a/catalogue_sync/catalogue_snapshot.json`.
  - Search, filters and count cards.
  - EPOS and QuickBooks quantities in the canonical unit.
  - Link state: Linked / Link needs checking / Not mapped.
  - Stock state, with the "likely timing" note.
  - Last updated and source times.
  - "Update products & stock" and "QuickBooks only" queue `stock_snapshot run [--no-epos]` (`update_company_records` command).
  - New EPOS products show "Approve product" when a plan has them, otherwise "Don't ask again".
  - If there is no snapshot file, the tab shows a calm empty state.
- **Deposits** (implements `docs/CODEX_SPEC_DEPOSITS_PAGE.md`):
  - Cards for Undeposited Funds, the till sheet (last day entered, missing-day chips, sheet link) and items waiting for a person.
  - A day list from 25 Sep with the five plain labels.
  - A reason catalogue matched to the `uf_deposits` and `till_sheet` strings, with the raw text under Details.
  - A day detail view with bank names from `till_accounts.csv` and the true-up sentence.
  - Actions: Approve deposit, Plan this day again, Refresh status, Skip for now.
  - A banner saying deposits aren't switched on yet.
  - Settings tab: tolerance edit for portal admins (writes `uf_deposits/settings.env`, audited in `PortalSettingChange`) and a read-only till accounts table.
- **Suppliers.** This tab shows the `vendors.csv` mapping with who approved each row, suppliers waiting for a decision, and the don't-ask-again list.

## To deploy

1. Apply the migrations:
   - `0018_portal_review_actions`: review audit and the `can_approve_company_a_reviews` permission.
   - `0019_alter_runjob_scope`: `workspace_read` job scope.
   - `0020_portal_setting_change`: tolerance audit.
2. Grant permissions:
   - `can_approve_company_a_reviews`: approve, skip, don't ask again and supplier checks.
   - `can_trigger_runs`: Update products & stock, Refresh status, Plan again and daily runs.
   - `can_manage_portal_settings`: deposit tolerance.
   - `can_edit_companies`: shows the Settings tab.
3. Rebuild Tailwind (the built CSS is committed). No new environment variables are needed.

## Checks run

- `python manage.py test apps.epos_qbo apps.dashboard apps.core`: 535 passed.
- `python -m unittest discover -s code_scripts/tests`: 652 passed.
- `makemigrations --check`: clean.
- Template guard: passed.
- Screenshots use synthetic fixtures only and are in `outputs/portal_phase1_review/claude-*.png` (desktop at 1440 px, phone at 390 px, no horizontal overflow).

## Known gaps

- **Not verified against live server files.** The stock snapshot, deposit day folders and vendor checks were tested only with fixtures that follow the documented contracts.
- **Exclusion keys** are shown as the tool stores them. Suppliers are normalized, for example `COCA COLA` for "Coca Cola Nig".
- **Supplier checks are not refreshed automatically.** A check older than a day stays valid in the portal, but the tool re-checks the live SHA when approving.
- **Supplier choices are limited.** A supplier can only be linked to a candidate the bills step listed, or created new. There is no free-text vendor Id.
- **Till accounts editing** is still read-only.
- **The deposit "receipts by tender" line** assumes `summary.receipts` is `{tender: count}` as the spec says. Other shapes are ignored.
- **Global run lock.** Review jobs, including supplier checks and exclusions, take the global pipeline lock, so they wait or fail while a daily run is active.
- **Pipeline changes:** none were needed. No `code_scripts/akponora_ops/` files were edited.

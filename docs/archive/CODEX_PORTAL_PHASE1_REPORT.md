# Portal redesign phase 1

Implemented on `codex/portal-redesign-phase1`, based directly on `ac4f15f`.

## What changed

- Added **Needs your attention** to desktop and mobile navigation. The inbox reads Company A bill, supplier, catalogue, deposit and sales-hold evidence, and shows Company B posting problems without modifying its run data.
- Added red Home banners for a sales hold, missing sales days, failed sales runs and runs stuck for more than two hours. Banners also update when the overview refreshes. Company A freshness comes from real, reconciled daily-run sales evidence; previews do not count as posted sales.
- Added explicit confirmation and background jobs for READY bill approval, bill resolution outside the tool, linking a reviewed supplier match, catalogue-plan approval, READY deposit-day approval, clearing a sales hold, and whole-day or single-step runs for a closed October business date. Runs default to preview.
- Added `can_approve_company_a_reviews`, CSRF protection, user-bound expiring confirmations, duplicate-submission protection, reviewed-evidence fingerprints and execution-time revalidation. Production write confirmations require the specific chat approval reference and a reason. Actions retain who, when, the reviewed identity/run/hash, reason and outcome.
- Reused the existing tools and hash gates. Views never call QuickBooks. Review jobs use the shared pipeline lock; daily runs retain their own global lock. Portal review jobs do not ingest unrelated sales artifacts.

## Existing tool boundaries / requests for Claude

These limits are visible in the UI and were preserved without editing pipeline modules:

1. `bills_sync` cannot approve HOLD bills, and `uf_deposits` cannot post HOLD days. Fix the source and make a new plan. Bills remain unpaid.
2. `catalogue_sync apply` applies all eligible AUTO/REVIEW decisions in one plan. The confirmation states that scope. Individual product approval or permanent skip needs a selection contract in that tool, with dependency and digest checks.
3. Supplier approval links a reviewed candidate to an existing active QuickBooks supplier, using a read-only live check and the existing vendor-mapping helper. Manual creation of a brand-new supplier is not exposed here. A manual vendor approval/creation entry point with payload SHA, preflight and audit receipt would support that follow-up.
4. **Skip for now** on suppliers, catalogue plans and deposit days is an audited inbox deferral for that exact evidence snapshot. It does not change mapping, stock, QuickBooks or any financial cursor. Changed evidence reopens the item. Only bill skips use the existing `Approve=skip` tool behavior. Permanent pipeline exclusions need explicit tool support.

## Validation

On the requested `ac4f15f` base:

- `python -m unittest discover -s code_scripts/tests -q`: **591 passed**.
- `python manage.py test apps.epos_qbo apps.dashboard apps.core`: **449 passed**, including 22 new inbox/action tests.
- Tailwind rebuilt; Django system check, migration drift check, template variable guard and `git diff --check` passed.
- Local Chromium checks at 1440 px and 390 px: inbox, Home, approval confirmation and review-item sections rendered; mobile inbox and confirmation had no horizontal overflow.
- A real background job cleared only the synthetic local hold, archived it through the existing function, and recorded a successful audit outcome. Financial tool execution was mocked in tests; no production financial action was executed.

Screenshots use synthetic fixtures only:

- `output/playwright/phase1-home-desktop.png`
- `output/playwright/phase1-inbox-desktop.png`
- `output/playwright/phase1-inbox-mobile.png`
- `output/playwright/phase1-inbox-items-desktop.png`
- `output/playwright/phase1-inbox-items-mobile.png`
- `output/playwright/phase1-confirm-desktop.png`
- `output/playwright/phase1-confirm-mobile.png`

Copies are also in the original checkout's gitignored `outputs/portal_phase1_review/` folder.

## Handoff

Migration `0018_portal_review_actions` must be applied when this branch is deployed; grant `can_approve_company_a_reviews` to the intended operators. Deployment and enabling automated production modes remain separate owner-approved actions.

The shared checkout received concurrent pipeline commits while implementation was underway. Portal changes were moved to a separate worktree at `/private/tmp/oiat-portal-redesign-phase1`; its branch starts exactly at `ac4f15f`, and the original checkout's source changes were restored. No pipeline files, production data, environment settings or Company B run data were changed.

Live validation was unavailable: `oiat-srv-01` did not resolve, and automatic approval review rejected opening the authenticated production portal because it treated the request as local implementation only. Screenshots and end-to-end checks therefore use isolated local fixtures. No push or deployment was performed.

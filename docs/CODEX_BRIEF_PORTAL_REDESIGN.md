# Brief for Codex: OIAT Portal redesign (phase 2 + 3)

You are improving the **OIAT Portal**, the Django admin and monitoring UI for our EPOS → QuickBooks Online pipelines. **OIAT staff** use it to monitor and operate the pipelines for two client companies. Company staff do not use it; a client-facing dashboard is a separate future project.

## Read first
1. `AGENTS.md`: governing rules for Company A (AKPONORA / NORA MINI MART, `company_a`). Non-negotiable.
2. `CLAUDE.md`: repo layout, commands, template rules. Keep every Django `{{ ... }}` tag on one line; a pre-commit hook enforces it.
3. `docs/SERVER_SETUP.md`, `docs/AKPONORA_DAILY_OPERATIONS.md`, `docs/AKPONORA_ROADMAP.md` (sections 4 and 5 list the portal phases).
4. `apps/epos_qbo/services/company_a_ops.py` and its templates: phase 1, already built. It is a read-only view of Company A daily runs.

## State of things (3 Oct 2026)
- **Company A** went live on new QuickBooks items on 1 Oct. Its whole day runs unattended on the server as one routine, `python -m code_scripts.akponora_ops.daily_run`, at **18:00 Africa/Lagos** in the Docker container `akponora-ops`. The steps are: new products → supplier bills from EPOS purchase orders → sales → health check → Undeposited Funds deposits (deposits are still switched off).
  - Each run writes evidence to `STATE_ROOT/ops/company_a/daily/<business_date>/run_<stamp>[_dry]/` (`summary.json` plus one folder per step: `review.csv`, `alerts.csv`, `log.txt`) and sends one Slack summary.
  - Anything uncertain is **held for a person**: bills (`code_scripts/akponora_ops/bills_sync.py`), new vendors (`vendors.py`), new products (`catalogue_sync.py`), deposit days (`uf_deposits.py`), and the sales posting hold (`code_scripts/operations_controls.py`: `show-hold` / `clear-hold`).
  - Today the only way to approve them is editing CSVs or running commands. **The portal should replace that.**
- **Company B** (GOLDPLATES FEASTHOUSE, `company_b`) runs the old way through the portal's own schedule worker (`apps/epos_qbo/services/schedule_worker.py`, 19:00 Lagos). Claude is fixing a run stuck since 21 Aug and backfilling its days. **Do not touch Company B's run data while that happens.**
- Branch: `cursor/post-akponora-qbo-writes-51f3` (PR #62). Local tip `bc1196a` includes phase 1 and the deposit step. **Work on your own branch from `bc1196a`:** `git checkout -b codex/portal-redesign bc1196a`. Do not push without Marvin's yes.

## What Claude is doing at the same time (do not edit these)
- `code_scripts/akponora_ops/*` (deposit-day changes and pipeline logic), `code_scripts/*` pipeline modules, `docker/`, `docker-compose.yml`, `.env*`.
- Server operations: Company B fix and backfill, deploys.
- If you need a pipeline change, write it down in your report instead of making it.

## Access
- **Repo on the server:** `ssh -i ~/.ssh/oiat_server oiatadmin@oiat-srv-01` (Windows, cmd shell). The repo is at `C:\Users\oiatadmin\Documents\prod\epos_to_qbo_automation\code-scripts` and runs on Docker (`docker compose ps`; services `web`, `scheduler`, `akponora-ops`). **Read-only use only:** look at real evidence files and logs (`docker compose exec -T web ls /data/ops/company_a/daily`). Never edit files on the server, never restart or rebuild containers, never post to QuickBooks. Docker builds do not work over SSH on Windows anyway.
- **The live portal:** `https://portal.oiatsolutions.com` (Tailscale only). Marvin is signed in on **Chrome on this Mac mini**, so use it to see the current pages. **Look only:** do not click Run, Sync, Save, Approve or any other action on the live portal.
- **Local testing:** run the portal locally with a scratch `STATE_ROOT` and fixture evidence (copy a few real `summary.json` / `review.csv` files from the server into your fixtures, with no tokens and no `.env`).

## The review (what is wrong today)
1. **The portal does not know what happened.** Overview shows Akponora's last sync as 12 Jun, and the headline metrics come from 23 Jul. The Company A daily run is not reflected except in the new phase 1 pages.
2. **Real problems look minor.** Goldplates was skipped every day for six weeks and the portal showed only a yellow "Warning". Anything that means *money is not reaching QuickBooks* must be a **red banner at the top**, with what's wrong and what to do.
3. **Technical language everywhere,** e.g. "Subprocess exited with code 1", "Reconciled", "Access token expired (will refresh on next sync)", run IDs like `SAL-0821-1200-E3A3`, and raw command lines and file paths on Tools. Users should see **plain English** ("Sales for 22 Jul didn't post. The sales file couldn't be downloaded. Try again."). Keep technical details behind a "Details" toggle.
4. **Few useful actions.** Users need buttons like "Review 3 items", "Approve bill", "Retry this day", "Fix QuickBooks connection".
5. **Tools is a developer page** (SQL, CLI snippets). Move it under Admin.
6. **Settings mixes** portal defaults, personal preferences and webhook diagnostics. Split it.

## Target structure (approved by Marvin)
| Page | Purpose | Main actions |
| --- | --- | --- |
| **Home** (Overview) | "Is everything OK today?" One row per company: up to date / needs you / not posting, with a plain reason. Red banner if any company is behind | Review items · Retry |
| **Needs your attention** (new inbox, top priority) | Everything waiting for a person: held bills (e.g. a possible duplicate PO), near-duplicate new vendors, unclear new products, deposit days held or waiting for the till sheet, sales posting hold, Company B stuck or failed runs | Approve · Skip · Open details |
| **Company page** (one per company, tabs) | Sales · Purchases (bills) · Products & Stock · Suppliers · Deposits · Settings (tabs only where they apply; Company B has fewer) | Context-specific |
| **Daily runs** (merge Runs and Logs) | One line per day per company in sentences ("Posted ₦4.9M of sales; 8 bills; 1 bill waiting"), step detail on click. Reuse phase 1 for Company A | Run again · Run a past date |
| **Schedules** | When each company runs, including Company A's 18:00 routine (phase 1 row) | Run now · Pause |
| **Admin** | Tools (query, verify), QuickBooks connection status, webhooks, portal defaults | Admin only |

## Rules for actions (phase 2)
- **Every button calls the existing tool or function** with the same gates (approval reference, payload SHA, caps). Never reimplement posting logic in views. For example, an approve button marks the row `Approve=yes` and calls `bills_sync post --review … --approval-ref "<user> via portal <timestamp>" --expect-sha …`; clearing the hold calls `operations_controls clear-hold --approved-by <user> --reason <text>`.
- Write actions need the existing Django permissions (`can_trigger_runs`, `can_manage_schedules`, etc.; add `can_approve_company_a_reviews` if needed), CSRF, a confirmation step that says in plain words what will happen, and an **audit record** (who, when, what, result).
- Run actions as background jobs (the existing `RunJob` / `job_runner` pattern), never inside the request.
- Nothing may post to QuickBooks without going through those tools. The tests must prove no view calls QuickBooks directly.

## Phases
1. **Needs your attention inbox** with Approve/Skip for held bills, vendors, products and deposit days, plus clearing the sales hold (with reason) and Run now / Run a past date for the Company A daily run. Include the red-banner logic on Home.
2. **Home redesign + company pages with tabs + Daily runs merge + plain-English messages** (a single message catalogue: technical code → sentence → what to do).
3. **Products & Stock** (EPOS product ↔ mapping ↔ QuickBooks item, EPOS vs QuickBooks quantity, negative stock; repurposes the old inventory review page), **Suppliers** (`vendors.csv` view and edit with audit), **Deposits** (per-day till sheet vs sales, per-bank split, status; till-sheet "last day entered / missing days"), and the **settings/mapping editors** (till accounts, deposit tolerance toggle) with audit.
4. Move Tools under Admin; split Settings.

## Stock snapshot contract (for Products & Stock)

Claude owns the producer (`code_scripts/akponora_ops/stock_snapshot.py`); the portal only reads the file and starts the job.

- **File:** `STATE_ROOT/ops/company_a/stock_snapshot/latest.json` (on the server `/data/ops/company_a/stock_snapshot/latest.json`). Replaced atomically; a failed run leaves the previous file. Dated copies: `history/stock_snapshot_<YYYY-MM-DD>.json`. Ignore `epos_side.json` / `qbo_side.json` / `work/` (internal caches).
- **Written by:** the daily run (`stock` step, after `guard`) and by hand. **"Update products/stock" button:** run `python -m code_scripts.akponora_ops.stock_snapshot run` as a background `RunJob` (never in the request); optional `--no-epos` ("QuickBooks only", fast) / `--no-qbo` ("EPOS only"). Exit 0 = written, 2 = failed (show the job log), 5 = another snapshot is already running. It is read-only on EPOS and QBO, so `can_trigger_runs` is enough; it does not take the global run lock.
- **Top level:** `schema_version` (1), `company`, `generated_at` (UTC ISO), `tolerance`, `summary_text` (one sentence, e.g. "Stock check: 3,812 match, 41 different, 11 negative in QuickBooks"), `sources`, `business_context`, `timing_evidence`, `summary`, `rows`, `unmapped_epos_products`, `unassigned_stock_rows`, `qbo_akp_items_not_in_mapping`.
- **`sources`:** `epos_stock_report {path, at, refreshed_this_run}`, `catalogue {path, at}`, `qbo {read_at, refreshed_this_run}`, `mapping {path, at}`. Show "EPOS stock as of … · QuickBooks as of …"; the two can differ after a partial refresh.
- **`business_context`:** `current_business_date`, `last_posted_sales_date` (may be null), `unposted_sales_days` (list), `note` (plain-English explanation of why EPOS and QuickBooks differ: EPOS is live, QuickBooks has sales up to the last posted day and only posted bills). Show the note above the table.
- **`summary`:** `rows`, `by_status` (every status below as a key), `inventory_rows`, `noninventory_rows`, `different_likely_timing`, `unmapped_epos_products`, `unmapped_tracked`, `unassigned_stock_rows`, `qbo_akp_items_not_in_mapping`.
- **`rows[]`** (one per QBO item / canonical family in the installed mapping, sorted by status then SKU):
  `family_sku` (`AKP-<master id>` or `AKP-NS-<id>`), `qbo_item_id`, `qbo_name`, `type` (`Inventory` / `NonInventory`), `qbo_active`, `canonical_unit`, `epos_master_id`, `epos_master_name`, `epos_product_ids` (all EPOS products mapped to it, master and pack children), `epos_volume_of_sale`, `epos_qty_canonical` (number or null when unknown), `qbo_qty_on_hand` (number; null for NonInventory or a missing item), `difference` (= EPOS − QuickBooks; null when either side is unknown), `status`, `likely_timing` (true/false on `DIFFERENT` rows, null otherwise), `timing_reasons` (sentences), `flags` (technical, for a Details toggle: e.g. `AMBIGUOUS_EPOS_NAME`, `QBO_ITEM_INACTIVE`, `MULTIPLE_STOCK_ROWS(2)`, `STOCK_VOS_INCONSISTENT(...)`, `NONINVENTORY_BUT_EPOS_TRACKED`, `MASTER_NOT_IN_CATALOGUE`), `tolerance`.
- **`status`** (plain words for the UI):
  - `MATCH`: same quantity (within `tolerance`, default 0.001 units).
  - `DIFFERENT`: quantities differ; if `likely_timing`, say "probably today's sales / a delivery not yet billed".
  - `NEGATIVE_QBO`: QuickBooks below zero (usually a delivery not billed yet). Show first, in red.
  - `NEGATIVE_EPOS`: EPOS below zero (a count/receiving problem in EPOS).
  - `NOT_IN_EPOS_REPORT`: tracked in EPOS but no row in the stock report could be matched by name.
  - `NOT_TRACKED_IN_EPOS`: NonInventory item, or EPOS no longer stock-tracks the master; no quantity comparison.
  - `NO_QBO_ITEM`: the mapping points to a QuickBooks item that does not exist.
- **`unmapped_epos_products[]`:** `epos_product_id`, `name`, `tracked`, `category`, `epos_qty` — EPOS catalogue products with no mapping row (catalogue_sync handles them; show as "new in EPOS, not yet set up").
- **`unassigned_stock_rows[]`:** stock-report rows not matched to one tracked product (`Name`, `reason` = `AMBIGUOUS_TRACKED_NAME` / `UNTRACKED_PRODUCT` / `NOT_IN_CATALOGUE`, `TotalStock`, `TotalCost`).
- **`qbo_akp_items_not_in_mapping[]`:** `AKP-` items in QuickBooks the mapping does not use (`qbo_item_id`, `sku`, `name`, `type`, `active`, `qbo_qty_on_hand`).
- The page never offers to change QuickBooks quantities (no "make QuickBooks match EPOS"): AGENTS.md forbids patching QtyOnHand or posting InventoryAdjustments.

## Done means
- Full suites green: `python -m unittest discover -s code_scripts/tests -q` and `python manage.py test apps.epos_qbo apps.dashboard apps.core`.
- Tailwind rebuilt if you add classes (`npm run build:css`).
- Desktop and mobile layouts checked with screenshots from a local run.
- Each phase is its own commit with a plain summary. Report back to Marvin with what changed, screenshots, and anything that needs a pipeline change from Claude.

## Tool contracts for the portal

Pipeline-side entry points for the "Needs your attention" inbox (branch `claude/approval-contracts`). Run each as a background `RunJob` from the repo root with the venv Python; pass the approving user as the approval ref (e.g. `"<user> via portal <timestamp>"`). Never call QuickBooks from a view. All of them print JSON (or write a JSON receipt) you can store in the audit record.

**Products (catalogue_sync)**
- Inputs: a plan folder (`plan.json`, `summary.json`, `review.csv`). `summary.json` has `plan_sha256`, `decision_shas` (`{EPOS id: sha}` for every non-HOLD decision), `excluded`, `counts`. review.csv has a `Decision SHA` column and `EXCLUDED` rows.
- Approve some products: `python -m code_scripts.akponora_ops.catalogue_sync apply --plan-dir <dir> --approval-ref "<ref>" --expect-sha <plan_sha256> --only <id,id> [--exclude <id,…>] [--expect-decision-shas id=sha,id=sha] [--json] [--no-slack]`.
- Refusals exit 4 with `REFUSED: …` on stderr: unknown id, HOLD id, id in both lists, child selected without the master this plan creates (message names the master to add), a decision whose digest changed, an expected decision sha that does not match, `--only` with `--auto`, installed mapping changed by anything other than an earlier apply of this same plan. Stop during writes = exit 2.
- Receipt: `<plan>/apply_receipt.json` (latest) and `<plan>/apply_receipt_<stamp>.json`; keys `applied` (exactly what went in: pid, action, decision_sha256, target_sku, qbo_id, multiplier), `created`, `adopted`, `mapping_only`, `not_selected` (with why), `excluded`, `already_applied`, `installed` (new mapping sha). Re-applying an id from the same plan is a no-op (`already_applied`), so retries are safe. Several approvals against one plan are fine (`applied_state.json`).
- Skip a product ("don't ask again"): `review_exclusions add --kind product --key <EPOS id> …` (below). Note: an excluded product sold on the till still fails the sales day.

**Suppliers (vendors approve)**
- Preview: `python -m code_scripts.akponora_ops.vendors approve --epos-name "<EPOS supplier>" (--link-to <QBO vendor Id> | --create [--display-name "<name>"]) --approval-ref "<ref>" [--epos-supplier-id <id>] --dry-run` → JSON with `result` (`dry_run` | `preflight_failed` | `already_approved`), `problems`, `warnings`, `payload`, `payload_sha256`. Show the problems in plain words.
- Write: same arguments without `--dry-run`, plus `--expect-sha <payload_sha256>` → `result` `created` | `linked` | `already_approved`, `qbo_vendor_id`, `qbo_vendor_name`, `receipt` (path). Exit 0 ok, 2 preflight problem / stop, 4 refused (missing or changed sha, no approval ref).
- Skip a supplier: `review_exclusions add --kind vendor --key "<EPOS supplier name>" …` → never auto-created, its bills HOLD with `supplier excluded`.

**Bills (bills_sync post)**
- Approve: set `Approve=yes` on the READY row in the plan's review.csv, then `bills_sync post --review <plan>/review.csv --approval-ref "<ref>" --expect-sha <payloads_sha256>`. Results per PO in `<plan>/results.csv` (`POSTED`, `ADOPTED`, `HELD_LIVE`, `RESOLVED`, `CAPPED`, `FAILED`, `STOPPED`) and `<plan>/post_<stamp>.json`.
- Skip for this plan: `Approve=skip` (never posted; the day counts as done for the cursor).
- Skip permanently ("resolved outside the tool"): `review_exclusions add --kind bill --key <PO OrderRef> …`. The next plan shows the PO as `EXCLUDED`; a post of an older plan records it `RESOLVED` without posting.

**Exclusions (review_exclusions)**
- File: `STATE_ROOT/mappings/company_a/review_exclusions.csv` (`kind, key, reason, added_by, added_at, expires_at`), history `review_exclusions_history.csv` (append-only: ts, action, kind, key, reason, actor, expires_at, previous, file_sha256_after).
- `python -m code_scripts.akponora_ops.review_exclusions add --kind product|vendor|bill --key <k> --reason "<why>" --added-by "<ref>" [--expires-at YYYY-MM-DD] [--replace]`
- `… remove --kind <k> --key <k> --removed-by "<ref>" --reason "<why>"` (an "undo" button)
- `… list [--kind <k>] [--all]` → `{"file", "rows": [… "active"]}`
- Output is one JSON object; exit 0 (`added` / `replaced` / `unchanged` / `removed`), 2 (`refused` + `error`). Adding the same row twice is `unchanged` (safe to retry).

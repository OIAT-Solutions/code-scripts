# Akponora (Company A) — daily operations from October 2026

**Read [`AGENTS.md`](../AGENTS.md) first.** Cutover is done (1 Oct live). This file is how the store runs day to day. Evidence of what has already been posted: [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md).

**Company:** `company_a`, QBO realm `9341455406194328` (production).  
**Identity:** EPOS Product ID → mapping → QBO item `AKP-{master}` (Inventory, asset 77) or `AKP-NS-{id}` (NonInventory). Never barcodes, never pack size from a trailing `*N`, never hand-made QBO items.

---

## What runs every day: `daily_run`

One routine runs everything, in order, for the last closed business day. On the server the portal schedule worker (`scheduler` container) runs it at 18:00 Lagos, from the portal schedule row "Nora daily routine" (setup, env, holds and approvals: [`SERVER_SETUP.md`](SERVER_SETUP.md)):

```bash
.venv/bin/python -m code_scripts.akponora_ops.daily_run [--date YYYY-MM-DD] [--dry-run] [--only catalogue,bills,sales,guard,stock,uf]
```

| Order | Step | What |
| --- | --- | --- |
| 1 | **catalogue** | `catalogue_sync scheduled`: new EPOS products → mapping rows; new masters → QBO items at **qty 0** (auto only under its gates/cap). A failure here does not stop bills or sales; sales still fail closed on unmapped products |
| 2 | **bills** | `bills_sync scheduled`: received POs (that day + earlier pending days) → **unpaid** Bills. New suppliers → QBO vendor (gated); near matches HOLD. PO payment mode → Bill memo hint |
| 3 | **sales** | `run_pipeline --target-date <day>` via the standing auto-approval; without it, a dry-run and exit 3. Skipped while the posting hold is in place |
| 4 | **guard** | `item_guard`: read-only, report only |
| 5 | **stock** | `stock_snapshot run`: read-only EPOS stock vs QBO QtyOnHand per item → `STATE_ROOT/ops/company_a/stock_snapshot/latest.json` (portal Products & Stock). Report only; a failure never affects the other steps |
| 6 | **uf** | `uf_deposits scheduled`: each day's receipts in Undeposited Funds → Bank Deposits by the till sheet + true-up transfers (below). Off unless `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1`; posts only with `OIAT_COMPANY_A_UF_AUTO_POST=1` + ref |

Exit 0 = clean, 3 = waits for review, 2 = a step failed. One Slack summary; evidence under `STATE_ROOT/ops/company_a/daily/<day>/`. It holds the global run lock. The portal schedule worker is the only scheduler; Company A sales run only inside this routine, never in a sales schedule. Each job below can still be run on its own.

Until the server is on, sales are still posted by hand from this repo (dry-run, then chat yes + post).

---

## Portal (read-only view of the daily run)

The OIAT Portal shows Company A without any new write action (phase 1). It reads the same `STATE_ROOT` as the `scheduler` container:

- **Schedules**: the row "Nora daily routine" (workflow "Daily routine", scope `company_a_daily`): enabled/paused, cron and timezone (default 18:00 Africa/Lagos), next run, and the last run's result. Turn it on or off and change its time here. `python manage.py ensure_workflow_schedules` creates it paused if it is missing. Only one Daily routine per company; inventory sync cannot be scheduled.
- **Company A daily** (sidebar; `/epos-qbo/company-a/daily-runs/`): every run newest first, real vs dry, overall status, a chip per step, sales posted ₦, bills posted (count / ₦), items / vendors created, items waiting. A run's page shows each step's summary, its review items, the last lines of each step's `log.txt`, and links to view its CSV / JSON evidence in the page (only files inside that run's folder under `STATE_ROOT/ops/company_a`).
- **Overview** and the **Company A company page**: a Company A card (last real run, what posted, what is waiting, Undeposited Funds step) and a red banner when the posting hold is in place. The company page also has a Holds & alerts panel (posting hold details, latest item-guard alerts by check).

Approvals and running a day ("Run daily for date") are in the portal Inbox. Clearing a posting hold is not in the portal yet; do it as described below.

**Stuck portal runs.** A portal run left "running" by a dead process (e.g. a container restart; found 3 Oct 2026: a Company B run stuck since 21 Aug blocked every Company B schedule) is now closed automatically by the scheduler every cycle and before a manual "Run now". It is marked failed with "Marked failed: the run stopped without reporting back (detected …)" plus the reason, and a "Run failed" event, when: the global run lock (`STATE_ROOT/logs/.oiat_global_run.lock`) is free for a sales run older than 5 minutes; or its PID is gone / now belongs to a newer process and its log has been quiet 15 minutes; or it has run longer than `OIAT_RUNJOB_MAX_HOURS` (default 6). A skip now names the blocking run and how long it has run. Manual check: `python manage.py reconcile_run_jobs`. The lock is shared with the daily run, so the daily run holding it never makes a portal run look stale.

---

## Catalogue sync

```bash
.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync plan --out outputs/catalogue_sync_<day>
# after review, chat yes:
.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync apply \
  --plan-dir outputs/catalogue_sync_<day> \
  --approval-ref "<chat yes>" \
  --expect-sha <plan_sha256 from summary.json>          # --expect-plan-sha still works
# apply only some decisions (the portal does this per approved product):
.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync apply --plan-dir outputs/catalogue_sync_<day> \
  --approval-ref "<chat yes>" --expect-sha <plan_sha256> --only 500,501 \
  [--exclude 502] [--expect-decision-shas 500=<sha>,501=<sha>] [--json]
```

- **Plan** is read-only (EPOS view-only + QBO GET). Exit 3 means HOLD or REVIEW rows need a human.
- **Selective apply**: `--only` / `--exclude` take EPOS Product IDs. Each decision has a `decision_sha256` (summary.json `decision_shas`, review.csv `Decision SHA`); a decision that changed is refused, and `--expect-decision-shas` pins the ones a human approved. A child (mapping-only row) cannot be applied without its master when this plan creates the master: select both. Unknown IDs, HOLD rows, and `--only`/`--exclude` with `--auto` are refused (exit 4). A plan can be applied in parts: `applied_state.json` in the plan folder records what went in, a retry of an applied ID is a no-op (`already_applied`), and `apply_receipt.json` (`applied`, `not_selected`, `excluded`) lists exactly what this apply did (one timestamped copy per apply).
- **Excluded products** (`review_exclusions`, below) are never planned; they show as `EXCLUDED` in review.csv and `excluded` in summary.json. Excluding a product does not make its till sales post: if it is sold, the sales day still fails until it is mapped.
- **Apply** creates items (qty 0, InvStartDate 2026-10-01, asset 77) and installs a new mapping version. Never posts InventoryAdjustment.
- Unexplained EPOS stock on a new tracked product is flagged; the item is still created at 0. A stock adjustment needs a separate chat yes.
- **Before-sales hook** (off by default): set `OIAT_COMPANY_A_CATALOGUE_SYNC_BEFORE_SALES=1`. The Company A pipeline then plans (and, if automated creates are on, applies) only the Product IDs sold that day that are missing from the mapping. If anything stays unmapped, the transform still fails the day closed.
- **Automated creates** (off by default, chat yes to enable): `OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE=1`, `OIAT_COMPANY_A_CATALOGUE_APPROVAL_REF=…`, optional `OIAT_COMPANY_A_CATALOGUE_AUTO_MAX_CREATES` (default 25).

---

## Bills from EPOS purchase orders

```bash
.venv/bin/python -m code_scripts.akponora_ops.bills_sync plan --from 2026-10-01 --to 2026-10-02 \
  --out outputs/bills_sync_<day>
.venv/bin/python -m code_scripts.akponora_ops.bills_sync vendors-suggest --out outputs/bills_sync_<day>
# human: fill Approve=yes on READY rows in review.csv; copy approved vendors into
#   runtime/mappings/company_a/vendors.csv
# then chat yes:
.venv/bin/python -m code_scripts.akponora_ops.bills_sync post \
  --review outputs/bills_sync_<day>/review.csv \
  --approval-ref "<chat yes>" \
  --expect-sha <payloads sha from summary.json>
```

- Only POs **received on or after 1 Oct**. September receipts stay against GRNI `210200` (Id 87).
- Lines are in the QBO item's unit: `QuantityReceived × Staff Approved Purchase Multiplier`. Unmapped products or vendors HOLD the whole PO.
- Bills are left **unpaid**. Never creates BillPayments, items or accounts. The EPOS PO "MODE OF PAYMENT" (CASH / TRANSFER; typos like `PAYMENY` are tolerated) is written into the Bill PrivateNote as a payment hint.
- Vendor map: `STATE_ROOT/mappings/company_a/vendors.csv` (human-approved from `vendors-suggest`, or `auto:<ref>` rows).
- **Automatic vendors** (`scheduled` only, never `plan`; off by default): when a PO supplier is not in vendors.csv, it is scored against all live QBO vendors. Best score < 0.75 (genuinely new) → created with DisplayName = the cleaned EPOS supplier name, provided `OIAT_COMPANY_A_VENDOR_AUTO_CREATE=1`, `OIAT_COMPANY_A_VENDOR_APPROVAL_REF` is set, the name is free across Vendors/Customers/Employees, and the per-run cap is not reached (`OIAT_COMPANY_A_VENDOR_AUTO_MAX`, default 5). Score ≥ 0.75 (possible typo/duplicate) → HOLD with the candidates. Evidence: `vendor_actions.json`. Shared code with `vendor_admin`: `code_scripts/akponora_ops/vendors.py`.
- **Automated post** (off by default): `OIAT_COMPANY_A_BILLS_AUTO_POST=1` + approval ref + per-bill / per-run caps.
- **`Approve` column**: `yes` posts (READY rows only); `skip` / `resolved` means a human dealt with this PO outside the tool for this plan: never posted, and its day counts as done for the cursor. Anything else (blank) waits.
- **Excluded POs / suppliers** (`review_exclusions`): an excluded PO is planned `EXCLUDED` ("resolved outside the tool", permanently) and counts as done for the cursor; an excluded supplier's bills HOLD with `supplier excluded` and it is never auto-created. Exclusions added after a plan still win at `post`: the PO is recorded `RESOLVED`, the supplier's bill `HELD_LIVE`; neither posts.
- **Approving a held supplier by hand** (one supplier, chat yes / portal approval):

```bash
# preview (GET only): preflight + payload sha
.venv/bin/python -m code_scripts.akponora_ops.vendors approve --epos-name "WONUOLA SUPER STORE" \
  --create [--display-name "WONUOLA SUPER STORE"] --approval-ref "<ref>" --dry-run
#   or --link-to <existing QBO vendor Id> instead of --create
# write (same arguments, sha from the dry run):
.venv/bin/python -m code_scripts.akponora_ops.vendors approve --epos-name "WONUOLA SUPER STORE" \
  --create --approval-ref "<ref>" --expect-sha <payload_sha256>
```

  Preflight: the supplier is not mapped to another vendor in vendors.csv, not excluded, the linked vendor exists and is active, a new DisplayName is free across Vendors/Customers/Employees. The write re-reads the vendor, appends the vendors.csv row with `Approved By = <ref>`, and writes a receipt + audit line under `STATE_ROOT/ops/company_a/vendors/`. A retry after success returns `already_approved`. The next bills plan then resolves the supplier. Exit 0 ok, 2 preflight problem, 4 refused (sha / ref).

---

## Review exclusions ("don't ask again")

`STATE_ROOT/mappings/company_a/review_exclusions.csv` (`kind, key, reason, added_by, added_at, expires_at`); every change is appended to `review_exclusions_history.csv` next to it.

```bash
.venv/bin/python -m code_scripts.akponora_ops.review_exclusions add --kind product|vendor|bill --key <id or name> \
  --reason "<why>" --added-by "<who / approval ref>" [--expires-at YYYY-MM-DD] [--replace]
.venv/bin/python -m code_scripts.akponora_ops.review_exclusions remove --kind … --key … --removed-by "<who>" --reason "<why>"
.venv/bin/python -m code_scripts.akponora_ops.review_exclusions list [--kind …] [--all]
```

- `product` = EPOS Product ID; `vendor` = EPOS supplier name (matched normalized); `bill` = EPOS PO OrderRef (`EPOS-PO-` prefix accepted).
- `expires_at` (optional): the exclusion stops applying on that date (Lagos).
- Output is JSON; exit 0 done/unchanged, 2 refused (missing reason/by, already excluded differently without `--replace`, not found).

---

## Undeposited Funds deposits (till sheet)

```bash
.venv/bin/python -m code_scripts.akponora_ops.uf_deposits plan --date 2026-10-01 --out outputs/uf_deposits_<day>
#   offline: add --sheet-xlsx <download of the sheet>.xlsx
# after review, chat yes:
.venv/bin/python -m code_scripts.akponora_ops.uf_deposits post --plan-dir outputs/uf_deposits_<day>/2026-10-01 \
  --approval-ref "<chat yes>" --expect-sha <payloads_sha256 from 2026-10-01/summary.json>
```

- Source: the Google Sheet "Nora Mart Daily Sales Account Breakdown", read live by a read-only service account (`OIAT_COMPANY_A_TILL_SHEET_SA_KEY`, default `STATE_ROOT/secrets/google_service_account.json`; setup in [`SERVER_SETUP.md`](SERVER_SETUP.md) §12). Box → bank map: `STATE_ROOT/mappings/company_a/till_accounts.csv` (seeded from `templates/till_accounts_company_a.csv`).
- Method (same as 26 Sep): whole receipts are deposited by tender (Cash → 100100, Card → card banks, Transfer → transfer banks, mixed → both) with LinkedTxn (minorversion 65, `TxnLineId 0`); then Bank→Bank true-up transfers make each bank's day total = sheet amount × receipts total / sheet total. Deposits follow the receipts; the mix follows the sheet. DocNumber `UF<yymmdd><bank no>`; transfer memo tag `UFTU <day> <from>><to>`; every memo `UF deposit <day> from till sheet; approval <ref>`.
- Days are independent (owner, 3 Oct 2026): every run re-evaluates each day since 2026-09-25 that is not yet deposited and posts every day whose gates pass; a held or missing day never blocks a later one.
- Holds (that day only): blank or unfinished sheet day (a CASH box or SYSTEM empty), unmapped / inactive till line, sheet vs receipts beyond max(₦1,000, 0.5 %), receipts deposited by hand, closed period, missing bank, or above `OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL` (₦15M) in automatic mode.
- Tolerance toggle: `OIAT_COMPANY_A_UF_TOLERANCE` (₦) and `OIAT_COMPANY_A_UF_TOLERANCE_PCT` (%), read every run. In `.env` (then `docker compose up -d scheduler web`), or without a restart in `STATE_ROOT/ops/company_a/uf_deposits/settings.env` (wins over `.env`; only these two keys).
- Per-day state `STATE_ROOT/ops/company_a/uf_deposits/days.json` (floor 2026-09-25): `DEPOSITED` (final) / `READY` / `HELD` (reason) / `WAITING_SHEET` / `NO_SALES`. An old `cursor.json` is migrated on first read (days up to it = `DEPOSITED`).
- Till-sheet status every run (step `summary.json` and the daily Slack): "Till sheet: last day entered 1 Oct. Missing: 25, 26, 29 Sep. Waiting to deposit: none. Deposited: …". Same on demand, read-only: `.venv/bin/python -m code_scripts.akponora_ops.uf_deposits status`.
- Automatic (chat yes to enable): `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1` + `OIAT_COMPANY_A_UF_AUTO_POST=1` + `OIAT_COMPANY_A_UF_APPROVAL_REF`. Enabled alone = plan every day, nothing posted.
- The old cutover scripts (`akponora_cutover/uf_*`) are history (26 Sep); `uf_allocation_draft` now uses the same sheet parser (`akponora_ops/till_sheet.py`).

---

## Item guard

```bash
.venv/bin/python -m code_scripts.akponora_ops.item_guard --fail-on-alert
# first / catch-up run:
.venv/bin/python -m code_scripts.akponora_ops.item_guard --since 2026-10-01T00:00:00 \
  --out outputs/item_guard_<day> --no-slack
```

Read-only. Alerts on non-`AKP-` items, October lines on `LEGACY —` / `15030` / unmapped items, 120xxx activity, wrong asset account, near-duplicate names. Negative stock and approved journals are WARN. Slack when there is an ALERT or WARN (unless `--no-slack`).

First live run (2 Oct): October sales clean; 11 negative items pending bills; **438 legacy items never renamed** (owner decision — see AGENTS.md open items).

---

## Stock snapshot (EPOS vs QuickBooks quantities)

```bash
.venv/bin/python -m code_scripts.akponora_ops.stock_snapshot run            # EPOS stock report + QBO
.venv/bin/python -m code_scripts.akponora_ops.stock_snapshot run --no-epos  # refresh QBO only
.venv/bin/python -m code_scripts.akponora_ops.stock_snapshot run --no-qbo   # refresh EPOS only
#   --stock-report <StockReport.csv> (no download), --catalogue <file>, --tolerance 0.001, --out <dir>, --slack
```

Read-only on both sides (EPOS StockReport download; QBO `select * from Item`). Writes
`STATE_ROOT/ops/company_a/stock_snapshot/latest.json` (atomic) and `history/stock_snapshot_<Lagos day>.json`.
One row per QBO item in the installed mapping:

- **EPOS quantity** = the family's EPOS **master** count in canonical units: full × VolumeOfSale + loose (or
  TotalStock when the master has no VolumeOfSale), as for the 30 Sep opening counts. Pack children are
  never added (their EPOS stock is the master's). The StockReport has no ProductID, so rows are matched to
  the stock-tracked catalogue product by exact name; a name shared by two tracked products is not used
  (`AMBIGUOUS_EPOS_NAME`). Catalogue = catalogue_sync's `catalogue_snapshot.json`.
- **Status**: `MATCH` (within 0.001 units, `OIAT_COMPANY_A_STOCK_TOLERANCE`), `DIFFERENT`, `NEGATIVE_QBO`,
  `NEGATIVE_EPOS`, `NOT_IN_EPOS_REPORT`, `NOT_TRACKED_IN_EPOS` (NonInventory or an untracked master),
  `NO_QBO_ITEM`. Also lists EPOS products not in the mapping.
- **Timing.** EPOS is live; QuickBooks has sales only up to the last posted day and only posted bills. The file
  records `last_posted_sales_date` and the unposted days. A `DIFFERENT` row gets `likely_timing` when one of
  its products sold on the last posted day (archived BookKeeping CSV) or is on an EPOS PO received since
  yesterday / whose bill is held (latest daily-run bills review). This is a hint, not proof.
- **Never** "fix" a difference by patching QtyOnHand or posting an InventoryAdjustment (AGENTS.md). Differences
  that remain after the day's sales and bills post go to the month-end count variance.

Exit 0 written, 2 failed (previous `latest.json` kept), 5 another snapshot running.

---

## Scheduling

One scheduler: the portal schedule worker (`python manage.py run_schedule_worker`, the `scheduler` container). It starts every job; pages and Inbox actions only queue (start within one poll, 15 s). What can be scheduled is the workflow catalogue (`apps/epos_qbo/services/workflows.py`): "Daily routine" for Company A only (one per company), "Sales sync" for every other company. The individual jobs (catalogue, bills, item guard) have no schedule of their own; they run inside `daily_run`, or by hand as above. Details: [`SCHEDULING_AUTHORITY.md`](SCHEDULING_AUTHORITY.md).

Run one day by hand on the server:

```bash
docker compose exec scheduler python -m code_scripts.akponora_ops.daily_run --date <day> [--only …]
```

---

## Sales (until / after W9)

From W9 the server's `daily_run` posts sales ([`SERVER_SETUP.md`](SERVER_SETUP.md)).

Until the server is deployed and W9 is on, each day from this laptop:

```bash
OIAT_COMPANIES_DIR=code_scripts/companies python run_pipeline.py --company company_a --target-date YYYY-MM-DD --dry-run
# chat yes, then post (manifest or standing approval once env is set)
```

W9 env (server `.env`, chat yes): `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1`, `OIAT_COMPANY_A_STANDING_APPROVAL_REF=…`, optional gross cap. Details: [`AKPONORA_OCT1_GOLIVE_RUNBOOK.md`](AKPONORA_OCT1_GOLIVE_RUNBOOK.md) §7.

---

## Bookkeeper rules (from October)

- Bills and invoices: **new `AKP-` / `AKP-NS-` items only**, in the item's unit. Prefer `bills_sync` over typing.
- September deliveries billed late: against GRNI `210200`, not stock items.
- Do **not** create new QBO items by hand. Do **not** use `LEGACY —` items or catch-all `15030`.
- Customer invoices: new items + matching EPOS stock-out. No `SR-` invoice numbers.
- Undeposited Funds (`100900`): do not deposit sales receipts by hand. `uf_deposits` (daily run step 5) deposits them from the till sheet; staff must fill every day's sheet block (both CASH boxes and SYSTEM, 0 where nothing).

---

## Write policy (reminder)

| Allowed without asking | Needs chat yes | Forbidden |
| --- | --- | --- |
| `plan` (incl. `uf_deposits plan`), `vendors-suggest`, `item_guard`, dry-runs, mapping files on disk | `catalogue_sync apply`, `bills_sync post`, enabling any `*_AUTO_*` (incl. `OIAT_COMPANY_A_UF_AUTO_POST`) / before-sales hook / W9, InventoryAdjustment, `uf_deposits post`, Bill Payments | Delete/inactivate products; patch legacy qty; create items with default qty; October sales to catch-all or legacy |

---

## Evidence (gitignored — never commit)

- Live firstruns: `outputs/catalogue_sync_firstrun/`, `outputs/bills_sync_firstrun/`, `outputs/item_guard_firstrun/`
- Persistent cursors / ledgers: `runtime/ops/company_a/<job>/`
- Mapping: `runtime/mappings/company_a/approved.csv` (+ `versions/`, `vendors.csv`)

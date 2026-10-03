# Akponora (Company A) — daily operations from October 2026

**Read [`AGENTS.md`](../AGENTS.md) first.** Cutover is done (1 Oct live). This file is how the store runs day to day. Evidence of what has already been posted: [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md).

**Company:** `company_a`, QBO realm `9341455406194328` (production).  
**Identity:** EPOS Product ID → mapping → QBO item `AKP-{master}` (Inventory, asset 77) or `AKP-NS-{id}` (NonInventory). Never barcodes, never pack size from a trailing `*N`, never hand-made QBO items.

---

## What runs every day: `daily_run`

One routine runs everything, in order, for the last closed business day. On the server it runs at 06:00 Lagos (setup, env, holds and approvals: [`SERVER_SETUP.md`](SERVER_SETUP.md)):

```bash
.venv/bin/python -m code_scripts.akponora_ops.daily_run [--date YYYY-MM-DD] [--dry-run] [--only catalogue,bills,sales,guard,uf]
```

| Order | Step | What |
| --- | --- | --- |
| 1 | **catalogue** | `catalogue_sync scheduled`: new EPOS products → mapping rows; new masters → QBO items at **qty 0** (auto only under its gates/cap). A failure here does not stop bills or sales; sales still fail closed on unmapped products |
| 2 | **bills** | `bills_sync scheduled`: received POs (that day + earlier pending days) → **unpaid** Bills. New suppliers → QBO vendor (gated); near matches HOLD. PO payment mode → Bill memo hint |
| 3 | **sales** | `run_pipeline --target-date <day>` via the standing auto-approval; without it, a dry-run and exit 3. Skipped while the posting hold is in place |
| 4 | **guard** | `item_guard`: read-only, report only |
| 5 | **uf** | `uf_deposits scheduled`: each day's receipts in Undeposited Funds → Bank Deposits by the till sheet + true-up transfers (below). Off unless `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1`; posts only with `OIAT_COMPANY_A_UF_AUTO_POST=1` + ref |

Exit 0 = clean, 3 = waits for review, 2 = a step failed. One Slack summary; evidence under `STATE_ROOT/ops/company_a/daily/<day>/`. It holds the global run lock. While `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1`, the portal scheduler never schedules Company A, and the individual job crons below are ignored. Each job below can still be run on its own.

Until the server is on, sales are still posted by hand from this repo (dry-run, then chat yes + post).

---

## Portal (read-only view of the daily run)

The OIAT Portal shows Company A without any new write action (phase 1). It reads the same `STATE_ROOT` as the `akponora-ops` container:

- **Schedules**: a system-managed row "Company A daily run (products → bills → sales → health check → deposits)" with enabled/disabled (`OIAT_COMPANY_A_DAILY_RUN_ENABLED`), cron and timezone (`OIAT_COMPANY_A_DAILY_RUN_CRON`, `SCHEDULE_TZ`), next run, and the last run's result. It is not editable in the portal.
- **Company A daily** (sidebar; `/epos-qbo/company-a/daily-runs/`): every run newest first, real vs dry, overall status, a chip per step, sales posted ₦, bills posted (count / ₦), items / vendors created, items waiting. A run's page shows each step's summary, its review items, the last lines of each step's `log.txt`, and links to view its CSV / JSON evidence in the page (only files inside that run's folder under `STATE_ROOT/ops/company_a`).
- **Overview** and the **Company A company page**: a Company A card (last real run, what posted, what is waiting, Undeposited Funds step) and a red banner when the posting hold is in place. The company page also has a Holds & alerts panel (posting hold details, latest item-guard alerts by check).

Approvals, clearing the hold and "Run now" are not in the portal yet (see the roadmap §4/§5). Do them as described below.

---

## Catalogue sync

```bash
.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync plan --out outputs/catalogue_sync_<day>
# after review, chat yes:
.venv/bin/python -m code_scripts.akponora_ops.catalogue_sync apply \
  --plan-dir outputs/catalogue_sync_<day> \
  --approval-ref "<chat yes>" \
  --expect-plan-sha <plan_sha256 from summary.json>
```

- **Plan** is read-only (EPOS view-only + QBO GET). Exit 3 means HOLD or REVIEW rows need a human.
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
- Tolerance toggle: `OIAT_COMPANY_A_UF_TOLERANCE` (₦) and `OIAT_COMPANY_A_UF_TOLERANCE_PCT` (%), read every run. In `.env` (then `docker compose up -d akponora-ops`), or without a restart in `STATE_ROOT/ops/company_a/uf_deposits/settings.env` (wins over `.env`; only these two keys).
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

## Ops scheduler (container)

Separate from the portal sales scheduler. With `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1` it runs only `daily_run` (`OIAT_COMPANY_A_DAILY_RUN_CRON`, default `0 6 * * *`), and the individual crons below are ignored unless `OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS=1`. **Keep them unset while the daily run is on.** Opt-in:

```bash
docker compose --profile akponora-ops up -d akponora-ops
```

A job runs only when its cron is set in `.env`:

| Env | Example | Job |
| --- | --- | --- |
| `OIAT_AKPONORA_CATALOGUE_SYNC_CRON` | `30 6 * * *` | catalogue sync (`scheduled`) |
| `OIAT_AKPONORA_BILLS_SYNC_CRON` | `0 9,15 * * *` | bills sync (`scheduled`) |
| `OIAT_AKPONORA_ITEM_GUARD_CRON` | `0 19 * * *` | item guard (`--fail-on-alert`) |
| `SCHEDULE_TZ` | `Africa/Lagos` | timezone for all three |

Optional: `OIAT_AKPONORA_<JOB>_CMD` override, `OIAT_AKPONORA_OPS_SLACK_WEBHOOK_URL` (else the pipeline webhook). Evidence under `STATE_ROOT/ops/company_a/` (and `…/runs/` when `STATE_ROOT` is set). Jobs that can write take the global run lock and skip while a sales run holds it.

List / one-shot:

```bash
.venv/bin/python -m code_scripts.akponora_ops.ops_scheduler --list
.venv/bin/python -m code_scripts.akponora_ops.ops_scheduler --run-now item_guard
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

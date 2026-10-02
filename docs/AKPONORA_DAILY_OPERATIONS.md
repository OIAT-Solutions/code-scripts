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
| 5 | **uf** | Undeposited Funds placeholder (`OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1`): balance + days since last deposit. Never deposits |

Exit 0 = clean, 3 = waits for review, 2 = a step failed. One Slack summary; evidence under `STATE_ROOT/ops/company_a/daily/<day>/`. It holds the global run lock. While `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1`, the portal scheduler never schedules Company A, and the individual job crons below are ignored. Each job below can still be run on its own.

Until the server is on, sales are still posted by hand from this repo (dry-run, then chat yes + post).

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
- Undeposited Funds (`100900`): deposit from the till sheet (`akponora_cutover/uf_*`). A teammate is connecting the Daily Sales Account Breakdown sheet → server → QBO (review later).

---

## Write policy (reminder)

| Allowed without asking | Needs chat yes | Forbidden |
| --- | --- | --- |
| `plan`, `vendors-suggest`, `item_guard`, dry-runs, mapping files on disk | `catalogue_sync apply`, `bills_sync post`, enabling any `*_AUTO_*` / before-sales hook / W9, InventoryAdjustment, UF deposits, Bill Payments | Delete/inactivate products; patch legacy qty; create items with default qty; October sales to catch-all or legacy |

---

## Evidence (gitignored — never commit)

- Live firstruns: `outputs/catalogue_sync_firstrun/`, `outputs/bills_sync_firstrun/`, `outputs/item_guard_firstrun/`
- Persistent cursors / ledgers: `runtime/ops/company_a/<job>/`
- Mapping: `runtime/mappings/company_a/approved.csv` (+ `versions/`, `vendors.csv`)

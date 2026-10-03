# OIAT server setup: Akponora (Company A) unattended daily run

**Read [`AGENTS.md`](../AGENTS.md) first.** This page is the exact procedure to move Company A from "run by hand on the ops Mac" to one routine on the OIAT server (`oiat-srv-01`, Docker). Turning on any automated write is a **chat-yes** action for the owner.

## What runs

One job: `python -m code_scripts.akponora_ops.daily_run`, in the `akponora-ops` container, at **06:00 Lagos** every day. It works on the **last closed business day** (05:00 cutoff), so the 06:00 run on 3 Oct does 2 Oct. The steps run in this order:

| # | Step | What it does | Writes to QBO only when |
| --- | --- | --- | --- |
| 1 | `catalogue` | New EPOS products → new `AKP-`/`AKP-NS-` items at **qty 0** + mapping install. Ambiguous products wait for review | `OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE=1` + ref (cap 25) |
| 2 | `bills` | Received EPOS POs (that day, plus earlier days still pending) → **unpaid** Bills. A genuinely new supplier becomes a QBO vendor; a near match waits for review. The PO "MODE OF PAYMENT" (CASH / TRANSFER) goes into the Bill memo as a hint | Vendors: `OIAT_COMPANY_A_VENDOR_AUTO_CREATE=1` + ref (cap 5). Bills: `OIAT_COMPANY_A_BILLS_AUTO_POST=1` + ref (caps) |
| 3 | `sales` | `run_pipeline --company company_a --target-date <day>`. Posts only through the standing auto-approval gates: 100% mapped, totals = EPOS, no posting hold, mapping SHA. Runs after bills, so stock arrives before it is sold | `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1` + standing ref. Without them it builds a dry-run and the day waits for review |
| 4 | `guard` | `item_guard`: read-only QBO scan | Never |
| 5 | `uf` | Undeposited Funds deposits (`uf_deposits`): each business day's SalesReceipts still in `100900` → Bank Deposits into the banks the till sheet names, then Bank→Bank true-up transfers so each bank matches the sheet mix. A day whose sheet is blank / unfinished, whose totals disagree with QBO, or that has an unmapped till line is held, and later days wait (section 12) | `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1` + `OIAT_COMPANY_A_UF_AUTO_POST=1` + ref (cap ₦15M per day). Enabled without auto post = plan only, days wait for review |

Rules:

- A failed or held step never makes a later step post something inconsistent. If catalogue fails, bills and sales still run, and sales still fails closed on any unmapped product. If sales is held, the guard still runs.
- It never creates Bill Payments, InventoryAdjustments or journals, and never edits or deletes existing QBO records. Deposits and bank transfers are created only by the `uf` step, under its own switches.
- It holds the global run lock for the whole run, so it never overlaps a manual `run_pipeline` or portal run. If the lock is busy, it waits up to 30 minutes, then reports a failure.
- **No double runs.** While `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1`, the portal scheduler never schedules Company A (all-company runs exclude it, and Company A-only schedules are skipped). The individual `OIAT_AKPONORA_<JOB>_CRON` values are also ignored. Company B is unchanged.
- **Exit codes:** `0` all clean, `3` something waits for review, `2` a step failed.
- **Evidence:** `/data/ops/company_a/daily/<day>/run_<UTC time>/<step>/`, with `summary.json` (and the latest copy in `/data/ops/company_a/daily/<day>/summary.json`).

---

## 1. Get the code on the server

On `oiat-srv-01`, in the repo checkout (the branch must be pushed from the ops Mac first):

```bash
cd ~/code-scripts            # the server checkout
git fetch origin
git checkout claude/akponora-daily-run
git pull --ff-only
git log --oneline -3         # tip must be the daily-run commit
```

## 2. `.env` for Company A

Add this block to the server `.env` (the file compose reads through `env_file`). Leave the daily-run switch **off** until the smoke test (step 5) has passed. Replace each `<…>` approval reference with the owner's chat-yes reference (date + message).

```bash
# --- Company A: unattended daily run (docs/SERVER_SETUP.md) ---
COMPOSE_PROFILES=akponora-ops                 # `docker compose up -d` also starts akponora-ops
OIAT_COMPANY_A_DAILY_RUN_ENABLED=0            # set to 1 in step 6, after the smoke test
OIAT_COMPANY_A_DAILY_RUN_CRON=0 6 * * *       # Africa/Lagos (SCHEDULE_TZ)
OIAT_COMPANY_A_DAILY_RUN_LOCK_WAIT_MINUTES=30
OIAT_AKPONORA_OPS_SLACK_WEBHOOK_URL=          # optional ops channel; empty = SLACK_WEBHOOK_URL_A

# Sales (W9): standing auto-approval
OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1
OIAT_COMPANY_A_STANDING_APPROVAL_REF="owner standing approval, <date>, <chat ref>"
OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS=15000000

# Catalogue: new EPOS products -> qty-0 items + mapping
OIAT_COMPANY_A_CATALOGUE_AUTO_CREATE=1
OIAT_COMPANY_A_CATALOGUE_APPROVAL_REF="owner catalogue approval, <date>, <chat ref>"
OIAT_COMPANY_A_CATALOGUE_AUTO_MAX_CREATES=25
OIAT_COMPANY_A_CATALOGUE_SYNC_BEFORE_SALES=0  # the daily run already syncs first

# Vendors: create genuinely new EPOS suppliers (near matches always wait)
OIAT_COMPANY_A_VENDOR_AUTO_CREATE=1
OIAT_COMPANY_A_VENDOR_APPROVAL_REF="owner vendor approval, <date>, <chat ref>"
OIAT_COMPANY_A_VENDOR_AUTO_MAX=5

# Bills: unpaid bills from received EPOS POs
OIAT_COMPANY_A_BILLS_AUTO_POST=1
OIAT_COMPANY_A_BILLS_APPROVAL_REF="owner bills approval, <date>, <chat ref>"
OIAT_COMPANY_A_BILLS_AUTO_MAX_BILL=2000000    # larger bills wait for review
OIAT_COMPANY_A_BILLS_AUTO_MAX_COUNT=20        # per run

# Undeposited Funds deposits from the till sheet (section 12). Off until the key is installed.
OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=0           # 1 = plan every day (needs the Google key)
OIAT_COMPANY_A_UF_AUTO_POST=0                 # 1 = post READY days (chat yes)
OIAT_COMPANY_A_UF_APPROVAL_REF=               # "owner UF approval, <date>, <chat ref>"
OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL=15000000 # a day above this waits for a manual post
OIAT_COMPANY_A_UF_TOLERANCE=1000              # sheet vs receipts: max(N1,000, 0.5% of receipts)
OIAT_COMPANY_A_UF_TOLERANCE_PCT=0.5
OIAT_COMPANY_A_TILL_SHEET_ID=15lvfx6q-g7JYgzY4kQZC87JKK2za8SRvuUXjqhovd3A
OIAT_COMPANY_A_TILL_SHEET_SA_KEY=/data/secrets/google_service_account.json

# Leave these UNSET while the daily run is on (they would be ignored anyway):
# OIAT_AKPONORA_CATALOGUE_SYNC_CRON / OIAT_AKPONORA_BILLS_SYNC_CRON / OIAT_AKPONORA_ITEM_GUARD_CRON
# OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS
```

Also check that the existing keys are there: `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` (the **same Intuit app as the ops Mac**, or the copied tokens are refused), `EPOS_USERNAME_A` / `EPOS_PASSWORD_A`, `SLACK_WEBHOOK_URL_A`.

A step's automation can stay off by setting its switch to `0`. That step then plans only, and its work waits for review (exit 3).

## 3. Build and start

```bash
docker compose build
docker compose up -d web scheduler akponora-ops      # caddy / cloudflared as before
docker compose ps
docker compose logs --tail=50 akponora-ops            # "No ops job configured" is expected while the switch is 0
```

## 4. Copy Company A state into `/data`

**First stop all Company A work on the ops Mac** (no `run_pipeline`, `bills_sync post`, `catalogue_sync apply` or QBO scripts for Company A). From now on, the server is the only machine that writes Company A.

Copy these files from the Mac to the server (for example `scp` over Tailscale into `~/akp_transfer/` on the server), keeping the folder layout:

| From the Mac (repo root) | To (inside the volume) | Notes |
| --- | --- | --- |
| `runtime/mappings/company_a/approved.csv` | `/data/mappings/company_a/approved.csv` | The installed mapping. Required |
| `runtime/mappings/company_a/versions/` | `/data/mappings/company_a/versions/` | Mapping history + receipts |
| `runtime/mappings/company_a/vendors.csv` | `/data/mappings/company_a/vendors.csv` | Approved vendor map. Required for bills |
| `runtime/ops/company_a/` (if present) | `/data/ops/company_a/` | Cursors: `bills_sync/cursor.json`, `catalogue_sync/` snapshot + ledger, `item_guard/` cursor + legacy snapshot |
| `runtime/company_a_posting_hold.json` (only if present) | `/data/company_a_posting_hold.json` | An open hold must move with the work. Clear it only on purpose (section 8) |
| `runtime/code_scripts/qbo_tokens.sqlite` | merged, see below | Company A row only |

```bash
cd ~/code-scripts
T=~/akp_transfer
docker compose exec web mkdir -p /data/mappings/company_a /data/ops
docker compose cp $T/runtime/mappings/company_a/approved.csv web:/data/mappings/company_a/approved.csv
docker compose cp $T/runtime/mappings/company_a/vendors.csv  web:/data/mappings/company_a/vendors.csv
docker compose cp $T/runtime/mappings/company_a/versions     web:/data/mappings/company_a/
docker compose cp $T/runtime/ops/company_a                   web:/data/ops/          # if present
# check: the sha must equal the Mac's `shasum -a 256 runtime/mappings/company_a/approved.csv`
docker compose exec web sha256sum /data/mappings/company_a/approved.csv /data/mappings/company_a/vendors.csv
```

**QBO tokens.** Intuit rotates the refresh token on every refresh, so **only one machine may refresh Company A tokens**. After this copy, the Mac's Company A token is dead weight; never use it again for Company A. Merge only the Company A row, so the server's Company B tokens are kept:

```bash
docker compose cp $T/runtime/code_scripts/qbo_tokens.sqlite web:/data/code_scripts/qbo_tokens_from_mac.sqlite
docker compose exec web python - <<'EOF'
import sqlite3
cols = "company_key, realm_id, access_token, refresh_token, access_expires_at, refresh_expires_at, updated_at, environment, client_fingerprint"
db = sqlite3.connect("/data/code_scripts/qbo_tokens.sqlite")
db.execute("attach '/data/code_scripts/qbo_tokens_from_mac.sqlite' as mac")
db.execute(f"insert or replace into qbo_tokens ({cols}) select {cols} from mac.qbo_tokens where company_key = 'company_a'")
db.commit()
print(db.execute("select company_key, realm_id, environment, updated_at from qbo_tokens").fetchall())
EOF
docker compose exec web rm /data/code_scripts/qbo_tokens_from_mac.sqlite
docker compose exec web python store_tokens.py --list
```

If the server already has a newer working Company A token (check `updated_at` in `store_tokens.py --list`), skip the merge. Delete `~/akp_transfer` when you are done; it holds a token.

**Company config.** The volume's `company_a.json` must have the October conversion block (`transform.product_conversion` with `fail_closed_from 2026-10-01`, `aggregate_products true`, `auto_fix_wrong_type_items false`):

```bash
docker compose exec web python manage.py check_company_config_drift
docker compose exec web grep -A6 product_conversion /data/code_scripts/companies/company_a.json
# if it is missing or old:
docker compose cp code_scripts/companies/company_a.json web:/data/code_scripts/companies/company_a.json
docker compose exec web python manage.py sync_companies_from_json
```

**Days already posted from the Mac** (1 Oct onwards) are safe. The pipeline's QBO duplicate check skips receipts that already exist, and bills skip any `EPOS-PO-<ref>` DocNumber already in QBO.

## 5. Smoke test (dry run, writes nothing)

```bash
Y=$(TZ=Africa/Lagos date -v-1d +%F 2>/dev/null || TZ=Africa/Lagos date -d yesterday +%F)
docker compose exec akponora-ops python -m code_scripts.akponora_ops.daily_run --dry-run --date $Y --slack
echo "exit $?"
docker compose exec akponora-ops cat /data/ops/company_a/daily/$Y/summary.json | head -60
```

- `--dry-run` runs catalogue and bills as `plan` and sales as `run_pipeline --dry-run`, and the guard does not advance its cursor. It reads EPOS and QBO but writes nothing to QBO. Slack is sent only with `--slack`.
- Expect exit `0` or `3`. Exit `3` lists what would wait for review. Exit `2` means a step failed: open that step's `log.txt`. Typical first-run causes are tokens, EPOS login, a missing mapping, or a missing company config.
- To run one step: `--only sales` or `--only catalogue,bills`.

## 6. Turn it on (chat yes)

After a clean smoke test and the owner's chat yes:

```bash
sed -i 's/^OIAT_COMPANY_A_DAILY_RUN_ENABLED=0/OIAT_COMPANY_A_DAILY_RUN_ENABLED=1/' .env
docker compose up -d akponora-ops scheduler web       # recreate so they read .env
docker compose exec akponora-ops python -m code_scripts.akponora_ops.ops_scheduler --list
#   daily_run  cron=0 6 * * *   (any individual cron shows "ignored: daily_run on")
docker compose logs --tail=20 akponora-ops            # "Scheduled daily_run at '0 6 * * *' (Africa/Lagos)"
```

Do not rebuild or restart `akponora-ops` between 06:00 and about 07:30 Lagos while a run is in progress. Every step can be resumed, but a killed run leaves that day's summary incomplete. To run a day by hand: `docker compose exec akponora-ops python -m code_scripts.akponora_ops.daily_run --date <day>`.

## 7. Reading the Slack summary

There is one message per run:

```
Akponora daily run 2026-10-02: :warning: waiting for review
:white_check_mark: catalogue [ok] 2 new EPOS product(s); items created 2, mapping-only 0, review 0, hold 0; mapping installed
:warning: bills [review] 5 PO(s) 2026-10-02..2026-10-02; posted 4 (READY N312000.00), hold 1 (N40000.00), capped 0, already posted 0; vendors created 1, held 1. Bills left UNPAID | CREATED: WONUOLA SUPER STORE -> QBO 912; HOLD_NEAR_MATCH: NIGERIAN BOTLING CO
:white_check_mark: sales [ok] post; receipts uploaded 6, skipped 0, failed 0; reconcile MATCH EPOS N3211950.00 / QBO N3211950.00
:white_check_mark: guard [ok] ALERT 0, WARN 3 (read-only)
:warning: uf [review] auto-post; deposited 1 day(s): 2026-10-01 N3211950.00 (100100 N401200.00, 100202 N1500300.00, ...); held 1 day(s) from 2026-10-02: CASH (System 1) box is blank (type 0 if there was no cash); Undeposited Funds N3456000.00
Waiting for review:
- bill HOLD PO 3999 NIGERIAN BOTLING CO N40000.00: supplier ... not approved in vendors.csv
- vendor HOLD_NEAR_MATCH: supplier 'NIGERIAN BOTLING CO' looks like an existing QBO vendor (best 0.95): 10 NIGERIAN BOTTLING COMPANY (0.95) ...
- bills: 1 bill(s) wait for review -> /data/ops/company_a/daily/2026-10-02/run_050012Z/bills/review.csv (...)
Evidence: /data/ops/company_a/daily/2026-10-02/run_050012Z
```

- `[ok]` means the step is done, `[review]` means a person must decide (the listed file says what), `[failed]` means it crashed or stopped (read `<step>/log.txt`), and `[skipped]` / `[disabled]` mean the step was not run.
- Head line: "all clean" = exit 0, "waiting for review" = exit 3, "a step FAILED" = exit 2.
- WARNs from the guard (for example negative stock until bills post) are informational. ALERTs need a look (`guard/alerts.csv`).

## 8. Clearing a sales hold

A held sales day writes `/data/company_a_posting_hold.json`. Nothing more posts for Company A until a person clears it.

```bash
docker compose exec akponora-ops python -m code_scripts.operations_controls show-hold
# read the failed gate(s) and the sales log: /data/ops/company_a/daily/<day>/run_*/sales/log.txt
# fix the cause (unmapped product -> catalogue review below; EPOS late data -> wait; totals -> investigate)
# confirm in QBO what already exists for the day, then:
docker compose exec akponora-ops python -m code_scripts.operations_controls clear-hold --approved-by "<name>" --reason "<what was fixed>"
docker compose exec akponora-ops python -m code_scripts.akponora_ops.daily_run --date <held day> --only sales
```

Re-run every missed day explicitly, oldest first. The 06:00 run only does yesterday.

## 9. Approving held bills, vendors and products

**Near-match vendor (`HOLD_NEAR_MATCH`).** If the supplier is the existing QBO vendor, add one row to `/data/mappings/company_a/vendors.csv` (`EPOS Supplier Id, EPOS Supplier Name, QBO Vendor Id, QBO Vendor Name, Approved By`). The EPOS name must be exactly as on the PO, with `Approved By` = your name. If it really is a new vendor, create it with `vendor_admin` (`--spec`, dry-run, then `--execute`). The next run, or `daily_run --date <day> --only bills`, posts the bill.

```bash
docker compose exec akponora-ops python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec /data/ops/vendor_spec.json
docker compose exec akponora-ops python -m code_scripts.scripts.akponora_cutover.vendor_admin --spec /data/ops/vendor_spec.json --execute
```

**Held or capped bills.** Open `<run>/bills/review.csv` and `review_lines.csv` (the `Reasons` column says why). For a bill that is correct but above the auto cap, or READY in plan-only mode, set `Approve=yes` on its row and post (chat yes):

```bash
docker compose exec akponora-ops python -m code_scripts.akponora_ops.bills_sync post \
  --review /data/ops/company_a/daily/<day>/run_<…>/bills/review.csv \
  --approval-ref "<chat yes>" --expect-sha <payloads_sha256 from bills/summary.json>
```

Duplicate-PO holds, unit-cost holds and unmapped-product holds are fixed at the source (EPOS PO, mapping or catalogue), then the next run picks the PO up again. The bills cursor only moves past days whose POs are all done. `Approve=skip` marks a PO resolved outside the tool. Bills are always left **unpaid**; the owner pays them in QBO.

**Catalogue review / hold.** Open `<run>/catalogue/review.csv`. REVIEW rows are applied with a manual `catalogue_sync apply --plan-dir <run>/catalogue --approval-ref "<chat yes>" --expect-plan-sha <plan_sha256>`. HOLD rows need an EPOS fix (master link, amount) first.

## 10. Recommended schedule

| Lagos time | Job | Container | Notes |
| --- | --- | --- | --- |
| 06:00 daily | `daily_run` (catalogue → vendors+bills → sales → guard → uf) | `akponora-ops` | The only Company A schedule |
| 18:00 daily | Portal all-company sales (`SCHEDULE_CRON`) | `scheduler` | Company B etc.; Company A automatically excluded |
| — | `OIAT_AKPONORA_*_CRON` individual jobs | `akponora-ops` | Leave unset while `daily_run` is on (ignored unless `OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS=1`) |
| as needed | `daily_run --date <day> [--only …]` | `akponora-ops` | Catch-up / re-run after a hold |

## 11. Turning it off

- **Everything:** `OIAT_COMPANY_A_DAILY_RUN_ENABLED=0`, then `docker compose up -d akponora-ops scheduler`. Company A then has no schedule at all, unless `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1`, which puts it back into the 18:00 portal run.
- **One kind of write:** set that step's switch to `0` (for example `OIAT_COMPANY_A_BILLS_AUTO_POST=0`). The step keeps planning, and its work waits for review.
- **Emergency:** `docker compose stop akponora-ops`.

---

## 12. Undeposited Funds deposits (`uf` step) and till sheet access

### What it does

Sales receipts land in Undeposited Funds (`100900`, QBO Id 72). The `uf` step moves each business day's receipts into the banks where the money really went, as written by the store on the Google Sheet **"Nora Mart Daily Sales Account Breakdown"** (owned by OIAT Admin). It uses the same method as the 26 Sep 2026 clean-up of January–24 September: *deposits follow the receipts, the mix follows the sheet*.

For each day from its cursor (`/data/ops/company_a/uf_deposits/cursor.json`, first day 25 Sep 2026) up to the run's business date:

1. It reads the day's block on the sheet (SYSTEM 1 / SYSTEM 2 boxes) and maps every box to a QBO bank with `till_accounts.csv` (below).
2. It reads the day's SalesReceipts still in Undeposited Funds.
3. **Gates.** The day is **held** if: the sheet has no block for that day, the block is blank, a CASH box or SYSTEM is empty, a box is not a number, a filled box is not in `till_accounts.csv` (or is `Active=no`), the sheet total and the receipts total differ by more than max(₦1,000, 0.5 % of the receipts), a bank account is missing / inactive / renumbered in QBO, the day is inside the QBO closing date, a receipt was already deposited by hand, or (automatic mode) the day is above ₦15M. A held day stops the later days, so days are always deposited in order.
4. **Deposits.** Whole receipts go to banks by tender: Cash receipts → the cash bank, Card → the card banks, Transfer → the transfer banks, mixed tenders (`Card/Cash` …) → the union of those banks, each time to the bank with the most of its sheet share still unfilled. One QBO Bank Deposit per bank, `DocNumber UF<yymmdd><bank no>` (e.g. `UF261001100100`), each line linked to its SalesReceipt.
5. **True-up transfers.** Because whole receipts rarely split exactly like the sheet, Bank→Bank transfers then move the difference so each bank's total for the day equals the sheet amount scaled to the receipts total (for example sheet ₦3,200,000 vs receipts ₦3,199,500: every bank gets its sheet share × 3,199,500 / 3,200,000). No transfer when the receipts already fit. The transfer memo carries `UFTU <day> <from>><to>`.
6. Every deposit and transfer memo starts `UF deposit <day> from till sheet; approval <ref>`. Each one is re-read and checked after posting. Evidence: `/data/ops/company_a/daily/<day>/run_*/uf/<deposit day>/` (`review.csv` per bank, `receipts.csv`, `payloads.jsonl`, `summary.json`, `results.csv`).

Re-running is safe: receipts already in a `UF…` deposit and transfers with the `UFTU` tag are recognised and never posted twice.

### `till_accounts.csv`

`/data/mappings/company_a/till_accounts.csv` maps each till-sheet line to a QBO bank. The container creates it from `templates/till_accounts_company_a.csv` the first time only; after that the copy in `/data` is the one used (edit it there).

| Column | Meaning |
| --- | --- |
| Till sheet line | The line's name on the sheet (for people) |
| Terminal / TID | The terminal number in brackets on the sheet line, e.g. `[5024249823]`. `-` for CASH. This is what the tool matches on |
| QBO account number / QBO account Id | The bank in QBO. Both are checked against QBO every run |
| Kind | `cash`, `card` or `transfer`: which receipts (by tender) may be deposited there |
| Active | `yes` / `no`. A filled box on an `Active=no` line holds the day |
| Note | Free text (wallet number etc.) |

Owner-confirmed rows (3 Oct 2026):

| Till sheet line | TID | QBO bank | Kind |
| --- | --- | --- | --- |
| CASH (System 1 + System 2) | - | 100100 (Id 29) | cash |
| ZENITH POS | 1284573680 | 100301 Zenith 1225575438 (Id 1150040044) | card |
| MONIE POINT POS 1 | 5024249823 | 100207 Moniepoint 4000850527 (Id 1150040041) | card |
| MONIE POINT POS 2 / TRANSFER | 5397768082 | 100205 Moniepoint 6397730972 (Id 1150040005) | transfer |
| MONIE POINT POS 3 | 5024245533 | 100206 Moniepoint 4000850479 (Id 1150040040) | card |
| MONIE POINT POS 4 | 5015892841 | 100201 Moniepoint 4000700275 (Id 1150040001) | card (unused so far) |
| MONIE POINT POS 5 / TRANSFER | 5688464974 | 100202 Moniepoint 4686987227 (Id 1150040002) | transfer (sales + expenses wallet) |

A new terminal on the sheet holds that day until a row is added here.

### Till sheet access (Google service account)

The server reads the sheet with a Google **service account** that can only **view** it (scope `spreadsheets.readonly`). Do these steps once, signed in as the **OIAT Admin** Google account (the sheet's owner).

**A. Create a Google Cloud project**

1. Open <https://console.cloud.google.com/> and sign in as OIAT Admin. Accept the terms if asked.
2. Click the project picker at the top left (next to "Google Cloud") → **New project**.
3. Project name: `oiat-till-sheet`. Leave Location as it is → **Create**. Wait for the notification, then select the new project in the project picker.

**B. Turn on the Google Sheets API**

4. Left menu (☰) → **APIs & Services** → **Library**.
5. Search `Google Sheets API` → open it → **Enable**. (The Drive API is not needed.)

**C. Create the service account (no roles)**

6. Left menu → **IAM & Admin** → **Service accounts** → **+ Create service account**.
7. Service account name: `oiat-till-sheet-reader` (the ID fills in). Description: `Reads the Nora Mart till sheet (read-only)`. → **Create and continue**.
8. "Grant this service account access to project": leave **empty** → **Continue**. "Grant users access": leave empty → **Done**.
9. Copy the service account's **email** from the list (it looks like `oiat-till-sheet-reader@oiat-till-sheet.iam.gserviceaccount.com`).

**D. Create the JSON key**

10. Click the service account → **Keys** tab → **Add key** → **Create new key** → **JSON** → **Create**. A `.json` file downloads. It is a password: never email it, never commit it, delete the download after step F.
    - If Google says *"Service account key creation is disabled"*, an organisation policy blocks keys (new Google Workspace organisations have this on by default). A Workspace super-admin opens **IAM & Admin → Organization policies**, finds **"Disable service account key creation"** (`iam.disableServiceAccountKeyCreation`), **Manage policy** → *Override parent's policy* → Enforcement **Off** for the `oiat-till-sheet` project only → **Set policy**, then repeats step 10.

**E. Share the sheet with the service account (Viewer)**

11. Open the sheet "Nora Mart Daily Sales Account Breakdown" (`https://docs.google.com/spreadsheets/d/15lvfx6q-g7JYgzY4kQZC87JKK2za8SRvuUXjqhovd3A`).
12. **Share** → paste the service-account email → role **Viewer** → untick **Notify people** → **Share** (or **Share anyway** if Google warns it is outside the organisation).
    - If sharing is blocked ("can't share outside your organisation"), the Workspace admin opens <https://admin.google.com> → **Apps → Google Workspace → Drive and Docs → Sharing settings → Sharing options** and allows sharing outside the domain (or adds an allowlist rule that permits it) for the OIAT Admin account's organisational unit, then repeat step 12. Viewer access is all that is needed; never give Editor.

**F. Put the key on the server**

13. Copy the downloaded file to the server (for example over Tailscale), then from the repo folder on the server (PowerShell on Windows):

```powershell
cd C:\oiat\code-scripts                     # the server checkout (where docker-compose.yml is)
docker compose cp "$env:USERPROFILE\Downloads\oiat-till-sheet-1234abcd.json" web:/data/secrets/google_service_account.json
docker compose exec web chmod 600 /data/secrets/google_service_account.json
docker compose exec web ls -l /data/secrets
Remove-Item "$env:USERPROFILE\Downloads\oiat-till-sheet-1234abcd.json"   # and empty the Recycle Bin
```

(Linux/macOS: the same `docker compose cp …` with a normal path.) `/data` is shared by `web` and `akponora-ops`, so copying through `web` is enough.

**G. Env vars** (server `.env`, then `docker compose up -d akponora-ops`; the image must be rebuilt once for the new Python packages: `docker compose build`):

```bash
OIAT_COMPANY_A_TILL_SHEET_ID=15lvfx6q-g7JYgzY4kQZC87JKK2za8SRvuUXjqhovd3A
OIAT_COMPANY_A_TILL_SHEET_SA_KEY=/data/secrets/google_service_account.json
OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1      # plan every day; nothing is posted yet
OIAT_COMPANY_A_UF_AUTO_POST=0            # 1 only after a clean plan and the owner's chat yes
OIAT_COMPANY_A_UF_APPROVAL_REF=          # "owner UF approval, <date>, <chat ref>"
OIAT_COMPANY_A_UF_AUTO_MAX_DAY_TOTAL=15000000
OIAT_COMPANY_A_UF_TOLERANCE=1000
OIAT_COMPANY_A_UF_TOLERANCE_PCT=0.5
```

**H. Smoke test (read-only: sheet + QBO GET, writes nothing to QBO)**

```bash
docker compose exec akponora-ops python -m code_scripts.akponora_ops.uf_deposits plan --date 2026-10-01 --no-slack
#   2026-10-01 READY    receipts 3211950.00 sheet 3212000.00 ...      (or HOLD + the reason)
docker compose exec akponora-ops python -m code_scripts.akponora_ops.uf_deposits plan --no-slack
#   every day from the cursor (25 Sep) to yesterday
```

- A Google error `403 The caller does not have permission` means step 12 (sharing) is missing; `404` means the sheet id is wrong; "key not found" means step 13.
- Without Google access you can still check a day from a download of the sheet: `… uf_deposits plan --date <day> --sheet-xlsx /data/ops/nora_sales.xlsx`.
- Open `<out>/<day>/review.csv`: per bank the sheet amount, the scaled target, the deposited receipts, the true-up in/out and the final (= target).

### Posting

- **By hand (chat yes per day):** `python -m code_scripts.akponora_ops.uf_deposits post --plan-dir <out>/<day> --approval-ref "<chat yes>" --expect-sha <payloads_sha256 from <day>/summary.json>`. It re-checks every receipt and DocNumber live first, posts the deposits, then the transfers, verifies each, and can be re-run after a failure (`results.csv`).
- **Automatic:** with `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1`, `OIAT_COMPANY_A_UF_AUTO_POST=1` and `OIAT_COMPANY_A_UF_APPROVAL_REF` set (chat yes), the daily run posts every READY day in order, up to ₦15M per day.
- **Held day:** fix the cause (staff fill the sheet, type 0 in an empty CASH box, add a terminal to `till_accounts.csv`, or investigate a total difference). The next run retries from the cursor. The cursor only moves past days that are fully deposited.
- `--dry-run` on the daily run plans only and never moves the cursor.

# Handover tracker: OIAT EPOS → QuickBooks (read this first)

**Last updated:** 2026-10-04 ~19:15 New York by Claude.
**Rule:** any agent that picks this up must **update this file when it finishes something** (tick the item, add the date and the commit).
Governing rules: [`AGENTS.md`](../AGENTS.md). Full checklist: [`AKPONORA_ROADMAP.md`](AKPONORA_ROADMAP.md). History of production writes: [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md).

---

## 1. Where everything is

| Thing | Location |
| --- | --- |
| Main repo (this Mac) | `/Users/marvinmokolo/Developer Projects/OIAT/code-scripts` |
| Main working branch | `cursor/post-akponora-qbo-writes-51f3` (PR **#62** → `master`). **Everything is pushed** (as of `1a960a5` with this tracker). The server runs `0ad7a1f` until the next deploy |
| Portal redesign branch | `codex/portal-redesign-phase1`, in worktree **`/private/tmp/oiat-portal-redesign-phase1`**. ⚠️ It lives under `/private/tmp`, which can be wiped on reboot. **Push it or copy it before any restart.** It contains all main-branch work up to `d98255b` plus Codex's portal pages. Checkpoint `07a6e82` = Codex's unfinished WIP (Codex hit its usage limit and will **not** resume) |
| Evidence/data (never commit) | `code-scripts/outputs/…`, `code-scripts/runtime/…`, `../MISC/AKPONORA Investigation/COGS Analysis/As of 30th September 2026/` |
| Mac QBO token | **Retired** (`runtime/code_scripts/qbo_tokens.sqlite.retired`). **Never call QuickBooks from the Mac.** The server owns the tokens |
| Production server | `oiat-srv-01` (Windows, Docker Desktop). Repo `C:\Users\oiatadmin\Documents\prod\epos_to_qbo_automation\code-scripts`. Containers: `web`, `scheduler`, `akponora-ops`, `caddy`, `cloudflared`. Data volume `/data` (STATE_ROOT) |
| SSH to server | `ssh -i ~/.ssh/oiat_server oiatadmin@oiat-srv-01` (Tailscale; key in macOS keychain agent). The shell is `cmd`. Helper: `/private/tmp/claude-501/srv.sh "<cmd>"` (volatile; recreate it with the snippet below) |
| Portal | `https://portal.oiatsolutions.com` (Tailscale only; Marvin signed in on Chrome on this Mac) |
| Server docs | [`SERVER_SETUP.md`](SERVER_SETUP.md), [`AKPONORA_DAILY_OPERATIONS.md`](AKPONORA_DAILY_OPERATIONS.md) |

`srv.sh` snippet:

```bash
#!/bin/bash
ssh -i ~/.ssh/oiat_server -o BatchMode=yes -o ConnectTimeout=20 oiatadmin@oiat-srv-01 "cd /d C:\\Users\\oiatadmin\\Documents\\prod\\epos_to_qbo_automation\\code-scripts && $*" 2>&1 | grep -v "post-quantum\|store now\|openssh.com/pq"
```

**Server gotchas:**
- `docker compose build` does **not** work over SSH (Windows credential store). Marvin runs the build in a desktop PowerShell; `up -d`, `exec`, `cp` and `logs` work over SSH.
- `docker compose up -d` recreates `web`, which kills anything running inside it. Never do it during a run (Company A 18:00 Lagos, Goldplates 19:00 Lagos).
- For non-trivial commands, `scp` a script to the repo folder, `docker compose cp` it into `web:/tmp`, run it, then delete it.

---

## 2. Live state (production), as of 4 Oct 2026

**Company A: AKPONORA / NORA MINI MART (`company_a`, realm 9341455406194328)**
- **New items:** live since 1 Oct on 3,938 Inventory `AKP-` and 492 NonInventory `AKP-NS-` items; all legacy items are `LEGACY —`. September is closed. FIFO COGS works.
- **Item categories check (4 Oct):** 0 of 4,431 items misfiled (`outputs/category_check_20261004/`).
- **Unattended `daily_run`** at **18:00 Lagos** in the `akponora-ops` container: products → bills (+ cash payments) → sales → item check → stock → deposits.
  - Server code: `5ed4b36`; it needs a rebuild to take effect (see §4 A1).
  - The 4 Oct run (business date 3 Oct) took 12.8 min, down from 26.
- **Slack:** a one-line start message, then a summary grouped by You / Store / OIAT.
- **Deposits:** on, auto, ₦8M/day cap.
  - Banked: 25, 26, 27, 28 and 30 Sep.
  - Undeposited Funds **₦19,576,650** (4 Oct).
  - Held: 29 Sep (till sheet ₦704,425 short of sales); 1–3 Oct (cash box blank).
- **Bills:** posted nightly, left unpaid.
  - From the next build, **cash-on-delivery POs are paid from Petty Cash** on the bill date (`bill_payments`, owner yes 4 Oct).
  - Routine repeat suppliers: FLOURISH COOL WATER, ALPINE FRESH TABLE WATER.
  - QBO vendor 64 was renamed `FLOURISH` → `FLOURISH COOL WATER` (4 Oct).
- **Portal:** Home, Inbox (with "Approve · routine supplier" and "Approve repeat order"), Daily runs, Deposits, Products & Stock, Suppliers.

**Company B: GOLDPLATES FEASTHOUSE (`company_b`)**
- **Pipeline:** status quo, portal schedule at **19:00 Lagos**.
- **Posted and confirmed:** 30 Jun – 3 Oct, all MATCH.
- **Open:**
  - ₦2.65M of late-synced July sales (manual correction; **do not re-run those days**);
  - 2 missing 22 Jun receipts (₦22,600);
  - the 2 Oct job record says "failed" (cosmetic; the day is confirmed).

---

## 3. Current focus (Marvin's order, one at a time)

1. **Goldplates invoicing**, now (§4 B).
2. **Bank reconciliation**, next (§4 C), which includes the ₦200.6M unpaid-bills backlog.
3. **Credit sales**, after that (§4 D).

---

## 4. Open items

### A. Daily operations
1. [ ] **Rebuild** for `5ed4b36` (cash payments for cash POs, Approve repeat order). Marvin: `docker compose build web scheduler akponora-ops`, then `docker compose up -d web scheduler akponora-ops`. Do it outside 17:45–19:30 Lagos.
2. [ ] **After the next run, check:**
   - October cash bills paid from Petty Cash (`bills/cash_payments.json`); expected include 80546 (PO 3978) and the water POs;
   - POs 3970 and 3979 posted (routine / linked);
   - the bills step at about 2–4 min.
3. [ ] **Inbox (Marvin):**
   - PO 3982 MEGA FROZEN FOODS ₦1.928M: link to QBO "Mega frozen Foods";
   - PO 3976 Uncle Sam's ₦72,000: "Approve repeat order" if real (after the rebuild).
4. [ ] **Store:**
   - 1–3 Oct breakdowns are now complete (banked tonight);
   - **check 29 Sep** on the till sheet (₦4,392,900 vs EPOS sales ₦5,097,325);
   - one supplier per PO (PO 3970 named two).
5. [ ] **Watch negative stock in QBO** (11 → 19 → 28 items). If it keeps rising after the held bills post, deliveries are not being recorded as EPOS POs.

### A2. Till sheet feedback (Marvin, 4 Oct)
- [x] **"Banked" notes in the till sheet** (`till_sheet_marks`). Column B of each day's title row shows:
  - `✅ Banked · ₦… · <when>`;
  - `⏸ Not banked: <reason>`, only for a real problem on a completed day.

  Days still being filled in get no note. The marks never overwrite typed text and re-sync after every banking run (they backfill earlier days). The service account is now **Editor** on the sheet. Takes effect from the next rebuild.
- [ ] **Parked (Marvin: too frequent):** the "Funds Allocation" menu plus a server poller every 10 min. Brief kept at `BRIEF_TILL_SHEET_FUNDS_ALLOCATION_MENU.md`; don't hand it to Claude in Chrome for now.
- [ ] **Later (Marvin):** a similar staff sheet for credit-sales invoices.

### B. Credit sales (PAUSED 5 Oct: the team is sorting out the EPOS till / credit-sale process)
- **Direction (5 Oct):** use **EPOS credit sales on the existing customer accounts** instead of a separate sheet.
  - EPOS already has accounts for GOLDPLATE RESTAURANT – TALEA MALL / AYANGBUREN / DREAM PARK, BASK LOUNGE, JIFA FELIX, VERA AKPOREHA and OIAT. TALEA MALL has Max Credit ₦9M and balance ₦0 (never used).
  - The credit tender is currently **off**; the team will turn it on and define the till process.
  - The BookKeeping export already carries `Customer Full Name`, `Customer ID` and `Tender`.
- **Pipeline change needed before credit sales go live:** credit-tender rows → one QBO Invoice per customer per day (not a SalesReceipt into Undeposited Funds), via a customer mapping file (EPOS customer → QBO customer; unknown = hold + Inbox); "pay on account" → ReceivePayment if EPOS exports it. Get **one test credit sale and one test repayment** first and read that day's export.
- **Backlog** (paper invoices 0006232–0006242, ₦7.13M, outside EPOS): post once from the transcription; 25–30 Sep dated 30 Sep (non-stock line).
- **The "Nora Mart Credit Sales" sheet + `credit_sheet.py`: parked.** Keep it only if it helps finish the backlog products; otherwise remove both.

- [x] **Sheet "Nora Mart Credit Sales"** (`1x6dB0QX9KWuF6dsqTfBOgBgZ0r4tle1kF847pOXp1nk`; service account Editor), built 5 Oct by `credit_sheet setup` (`50f8a00`+).
  - Tabs: Read me, Invoices, Repayments, Customers, Customer prices, Products (6,143 EPOS products, server-owned).
  - Dropdowns; auto list price (agreed customer price, else EPOS); a red row when a price differs without name + reason.
  - 18 example lines loaded = the 9 paper invoices not in QBO (0006232 … 0006242, ₦7,126,000), not Ready, products to be picked.
  - Transcription: `outputs/credit_sales_2026-10/paper_invoices_transcribed.csv`.
- **Facts from the paper invoices (4 Oct):**
  - Pre-numbered book (0006224 …). 0006224–0006231 are already in QBO as SR-20260408-0023…0028; 0006224's vegetable oil was posted as "Services".
  - GPFH locations: CTH-HQ = Chevron HQ, IKD = Ikorodu / Ayangbure (confirmed): one customer, location on the invoice.
  - Goldplates prices often differ from EPOS (palm oil ₦52k vs ₦75k; Titus ₦201k vs ₦130k), so use Customer prices.
  - Some bulk items (fish cartons) may not be EPOS products.
  - Jifa Felix: credit customer repaying by transfer into 5688464974 (₦300k 17 Sep, ₦750k 22 Sep, ₦800k 24 Sep, ₦500k 29 Sep, 2 × ₦500k 2 Oct). Repayments must never go in the till sheet.
- [ ] **Waiting on Marvin's team:**
  - every invoice-book page from 0006224 onwards (all customers, voided pages too) and the repayment records since 1 Sep; later, everything back to Dec 2025 for the GPFH A/R tie-out;
  - gaps 0006225 / 0006226 / 0006233 / 0006234;
  - how the "Paid" invoices were paid;
  - Jifa Felix's invoices.
- [ ] **Next (Claude):** the nightly reader. Ready rows → QBO invoices `DocNumber = paper number` (Inbox approval first); Repayments → ReceivePayment; status written back; refuse invoice numbers already in QBO; the 25–30 Sep backlog dated 30 Sep on a non-stock line (Sep COGS is already in the close). Also: a polish brief for Claude in Chrome (look only; keep tabs, headers and columns).

### B-old. Goldplates invoicing (superseded by B)
Facts and design discussed 4 Oct.
- **Today's practice:** Nora Mart supplies GPFH (QBO customer Id 62) on paper invoices; copies go in a WhatsApp group.
  - Prices are the EPOS selling prices.
  - GPFH pays by transfer to a Moniepoint account (Marvin is confirming which).
  - Staff adjust EPOS stock on the delivery day.
  - Nothing has been invoiced in QBO since 22 Sep; GPFH open A/R is ₦36.6M.
  - "Services" lines must never be used.
- **Design (proposed, Marvin agreed in principle):**
  - a Google Sheet "Goldplates Invoices" (a tab per month, a row per paper-invoice line, a "Done" tick), shared read-only with the service account;
  - a nightly step after sales: exact EPOS-product match, pack sizes to base units, a price check against EPOS, one QBO invoice per paper invoice `GPFH-<paper no>`;
  - Inbox approval first, auto later; a Slack line; payments matched in bank reconciliation.
- [ ] **Waiting on Marvin:**
  1. Do the paper invoices have printed numbers?
  2. 22–30 Sep backlog: post dated 30 Sep on a non-stock line (recommended; September COGS is already in the close)?
  3. Should Claude make the sheet template (.xlsx to upload)?
  4. Should Goldplates' QBO record matching bills from Nora Mart (recommended, later)?
  5. Which Moniepoint account does GPFH pay into?

### C. Bank reconciliation (next; Marvin: do as its own project)
- [ ] **Unpaid-bills backlog:** no BillPayment has been recorded since **18 May 2026**.
  - 677 bills since June are open, **₦200.6M**; nearly all are really paid.
  - Before May, payments came from **Moniepoint 4686987227 (100202)** and **Petty Cash (100100)**.
  - Needs the Moniepoint statements (Jun → now) and the petty-cash records; match each payment to its bill and pay it on the real date from the real account. Do **not** bulk-mark as paid without evidence.
  - Water suppliers: Flourish ₦190,400 and Alpine ₦311,600 open, though EPOS says cash.
- [ ] **Old cash POs (b, Marvin yes 4 Oct):** inside the bank reconciliation project, pay June–September bills that match an EPOS PO marked CASH from Petty Cash on their dates, after Marvin's yes on the list. Capture the June–August POs from EPOS first (only Sep–Oct are captured: 273 POs, 93 CASH, 179 TRANSFER, 1 blank; no transfer PO names its account). Transfer bills: match to the statements.
- [ ] **Staff PO-note convention** (a, built 4 Oct): `MODE OF PAYMENT: CASH (PAID)` / `TRANSFER (PAID, MONIEPOINT 4686)` / `CREDIT` or `NOT PAID`. The bills step pays CASH (unless NOT PAID) from Petty Cash, and TRANSFER PAID + account from that bank (unique match only). Brief the store.
- [ ] **Bank statements vs QBO** for every till account (Moniepoint ×6, Zenith, Petty Cash): reconcile monthly.
- [ ] **29 Sep deposit hold:** ₦704,425 gap between the till sheet and sales.
- [ ] **Till sheet higher than sales** by ₦700–₦3,150 a day (27, 28, 30 Sep). Investigate per tender; then the **Cash Over/Short** design: banks equal the sheet exactly and the difference goes to Over/Short (agreed 3 Oct), plus a clean-up journal for the days banked under option 1.
- [ ] **GPFH payments** (Moniepoint transfers) applied to GPFH invoices.

### D. Credit sales (after C)
- [ ] Not yet discussed. EPOS has a per-customer credit limit of about ₦2M.

### E. Smaller follow-ups
- [ ] **Clean-up decisions:** GitHub merged-branch deletes, zip the May evidence on the server, website-log retention (`/data/db.sqlite3` 1.8 GB), `main` vs `master`, the `tender-bell` / `d618` drafts, root `Uploaded/` (696 MB). See `outputs/cleanup_audit/REPORT.md`.
- [ ] **Goldplates corrections:** ₦2.65M July late syncs; 2 receipts for 22 Jun.
- [ ] **Goldplates 2 Oct job label:** "failed", but it actually succeeded (optional fix).
- [ ] **Master product setup:** **parked by Marvin.** Do not start without his yes.
- [ ] **Nightly read-only checker:** on hold (Marvin: avoid bloat).
- [ ] **Later:** the client dashboard; W10 legacy retirement (see the roadmap).

**Done (3–4 Oct):**
- deploy of the portal redesign; deposits on; the 3 Oct fixes (deposit memo tag, Slack layout, PO cache, Inbox banners, routine suppliers, aged-day alert, SQLite timeout, stale-lock fix);
- the 1 Oct record imported; the clean-up (dead code, docs archived, backups out of the image, Mac clutter, 21 GB build cache);
- vendor 64 renamed; PO 3970 linked; the category check.

---

## 5. Rules for any agent continuing

- Read `AGENTS.md` first. Every production QuickBooks write needs Marvin's explicit yes for that action. The permission system may also block writes; if it does, give Marvin the exact command instead of working around it.
- QuickBooks calls happen **only on the server** (tokens live there). EPOS can be read from the Mac (pipeline credentials in `.env`).
- Don't edit `code_scripts/akponora_ops/` and `apps/` in parallel branches without coordinating. Use isolated worktrees, then merge.
- After finishing anything, **update this file** (sections 2–4), the cutover log (for production writes) and the roadmap ticks. Then commit.

### Small portal follow-ups (not blocking)
- ✅ Pluralisation fixed 3 Oct in `58d6f1a`: "1 day has…" and "View 1 missing day".
- The company page shows "Next scheduled run: No active schedule recorded" unless the `web` container has the Company A daily-run env. Check it after deploy.
- Supplier linking is limited to the bills step's suggestion or creating a new supplier; till accounts are view-only.
- Inbox actions take the global pipeline lock, so they wait while a daily run is active.

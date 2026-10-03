# Handover tracker: OIAT EPOS → QuickBooks (read this first)

**Last updated:** 2026-10-03 ~10:00 New York (15:00 Lagos) by Claude (session "Repository onboarding").
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

## 2. Live state (production)

**Company A: AKPONORA / NORA MINI MART (`company_a`, realm 9341455406194328)**
- Live on the new QBO items since 1 Oct: 3,938 Inventory `AKP-` + 492 NonInventory `AKP-NS-`. All 4,316 legacy items are renamed `LEGACY —`. September is closed (journals 76552/76553/76554, offset 80493, reclass 80500). FIFO COGS is working.
- **Unattended `daily_run` is ON** on the server (`akponora-ops` container) at **18:00 Africa/Lagos** (13:00 New York): products → bills → sales → guard (→ stock and deposits once deployed). Server `.env` has the switches and caps (backup `.env.bak_20261003`). The server build is **`0ad7a1f`**; the newer main-branch code is not deployed yet.
- **First automatic run: 3 Oct 2026 18:00 Lagos.** It should post 2 Oct sales and the 1–2 Oct bills (PO 3969 is expected to HOLD as a possible duplicate). **Check the Slack summary and the evidence in `/data/ops/company_a/daily/2026-10-02/`.**
- Vendors: `vendors.csv` maps all 123 EPOS suppliers (15 created, 5 renamed on 2 Oct).
- Undeposited Funds: ~₦29M+ waiting from 25 Sep onwards. The deposit step is built but **off**; it needs the Google service account (Marvin is setting it up now).

**Company B: GOLDPLATES FEASTHOUSE (`company_b`)**
- Status-quo pipeline via the portal schedule, **19:00 Lagos**.
- The run stuck since 21 Aug was cleared on 3 Oct.
- **Backfill done and verified on 3 Oct:** 78 days (30 Jun – 1 Oct), ₦819,227,851, all matching EPOS, 0 duplicates. 2 Oct posts at tonight's 19:00 run.
- Open: ₦2.65M of late-synced July sales on days already posted (needs a manual correction, **do not re-run those days**), and 2 missing 22 Jun receipts (₦22,600).

---

## 3. In progress right now

| Item | Who | Where | Status |
| --- | --- | --- | --- |
| Portal redesign (inbox, Home, Daily runs, company workspaces, Products & Stock, Deposits, Suppliers) | Codex → Claude subagent | **Merged into the main branch at `a2322b3`** (652 + 535 tests pass). Handover: `docs/PORTAL_REDESIGN_HANDOVER.md`. Not deployed yet | ✅ Ready to deploy |
| Google service account for the till sheet | Marvin | Google Cloud (OIAT Admin) | Steps in `SERVER_SETUP.md` ("Till sheet access"). The key goes to `/data/secrets/google_service_account.json` |

---

## 4. Next steps (in order)

1. [ ] **After 18:00 Lagos:** check Company A's first automatic run (Slack + `/data/ops/company_a/daily/2026-10-02/run_*/summary.json`). Approve PO 3969 if it is genuine (`bills_sync post` with `Approve=yes`), or let the inbox do it after deploy.
2. [ ] **After 19:00 Lagos:** check that Goldplates 2 Oct posted (portal Daily runs, or a QBO read on the server).
3. [ ] **Combine and deploy** (after both runs, about 14:30 New York or later):
   1. ✅ Portal branch merged (`a2322b3`); screenshots in `outputs/portal_phase1_review/claude-*.png`.
   2. Run both test suites: `python -m unittest discover -s code_scripts/tests -q` and `python manage.py test apps.epos_qbo apps.dashboard apps.core`, with dummy env and a scratch `STATE_ROOT`.
   3. Push.
   4. Server: `git pull`. Marvin runs `docker compose build web scheduler akponora-ops` (desktop PowerShell). Then `docker compose up -d`.
   5. Run `python manage.py migrate` in `web` (**0018, 0019, 0020**). Grant `can_approve_company_a_reviews`, `can_trigger_runs` and `can_manage_portal_settings` to the operators (Django admin).
   6. Smoke test: `daily_run --dry-run --date <yesterday>`. Open the portal pages.
4. [ ] **Deposits:** once the service account key is on the server, run `uf_deposits status` and `plan` (read-only), show Marvin, then with his yes set `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1`, `_UF_AUTO_POST=1`, `_UF_APPROVAL_REF`, and the sheet id/key env.
5. [ ] **Goldplates corrections:** the ₦2.65M July late syncs and the 2 receipts for 22 Jun (prepare, then Marvin approves).
6. [ ] **Master product setup** (team's Master Product Review; `docs/AKPONORA_STAFF_CHECKLIST.md`): **parked by Marvin.** Do not start without his yes.
7. [ ] **Later:** repo clean-up phase 2, the client dashboard, and the W10 legacy retirement (see the roadmap).

---

## 5. Rules for any agent continuing

- Read `AGENTS.md` first. Every production QuickBooks write needs Marvin's explicit yes for that action. The permission system may also block writes; if it does, give Marvin the exact command instead of working around it.
- QuickBooks calls happen **only on the server** (tokens live there). EPOS can be read from the Mac (pipeline credentials in `.env`).
- Don't edit `code_scripts/akponora_ops/` and `apps/` in parallel branches without coordinating. Use isolated worktrees, then merge.
- After finishing anything, **update this file** (sections 2–4), the cutover log (for production writes) and the roadmap ticks. Then commit.

### Small portal follow-ups (not blocking)
- "1 days have no confirmed…" should read "1 day has…" (pluralisation).
- The company page shows "Next scheduled run: No active schedule recorded" unless the `web` container has the Company A daily-run env. Check it after deploy.
- Supplier linking is limited to the bills step's suggestion or creating a new supplier; till accounts are view-only.
- Inbox actions take the global pipeline lock, so they wait while a daily run is active.

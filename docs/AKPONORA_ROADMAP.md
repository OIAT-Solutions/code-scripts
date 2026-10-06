# Akponora (Company A) roadmap and checklist

One place for everything outstanding. Tick items off here (`[x]`) and add the date. Rules live in [`AGENTS.md`](../AGENTS.md); cutover history (removed 5 Oct; in git history).

Owner column: **Marvin**, **Team**, **Agent** (Claude / Codex / Cursor), **Staff** (store).

---

## 1. Before the first unattended daily run (blocking)

| Done | Item | Owner |
| --- | --- | --- |
| [x] 2026-10-02 | Finish renaming the remaining 437 legacy items `LEGACY — …` (`outputs/w5b_legacy_rename_2026-10-02/`): all 4,316 legacy Inventory items now `LEGACY —`, IA unchanged | Agent |
| [x] 2026-10-02 | Merge `daily_run` and the safety/clean-up branches; full tests green | Agent |
| [x] 2026-10-03 | Push to PR #62 | Agent (needs Marvin's yes) |
| [x] 2026-10-03 | Server: pull the branch, `docker compose build && up` | Marvin / Team |
| [x] 2026-10-03 | Server: copy `runtime/mappings/company_a/approved.csv`, `vendors.csv` and ops cursors into the server's `/data` (see `SERVER_SETUP.md`) | Marvin |
| [x] 2026-10-03 | Server: copy `runtime/code_scripts/qbo_tokens.sqlite`, run one read-only check on the server, then retire the Mac's copy (`.retired`). From then on **only the server refreshes tokens** | Marvin |
| [x] 2026-10-03 | Server `.env`: Company A switches (`OIAT_COMPANY_A_DAILY_RUN_ENABLED`, `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED`, `OIAT_COMPANY_A_STANDING_APPROVAL_REF`, catalogue/vendor/bills auto switches + caps) | Marvin |
| [x] 2026-10-03 | Server smoke test: `daily_run --dry-run --date <yesterday>` | Marvin / Agent on server |
| [ ] | First real `daily_run`: expect 8 of the 9 October POs (1–2 Oct, ₦237,350) to post as unpaid bills, and PO 3969 (possible duplicate) to be held for review | Server |

## 2. First week of October

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | Check each morning's Slack summary; clear holds the same day | Marvin / Team |
| [ ] | Pay posted bills in QBO (Pay Bills, choosing the account the money left from; the bill memo shows the EPOS payment mode) | Staff / bookkeeper |
| [ ] | Confirm the 11 items that went negative on 1 Oct come back up as the 1–2 Oct bills post | Agent |
| [ ] | Enter the real **September** supplier invoices against `210200 Goods Received Not Invoiced` (not stock items) as they arrive; GRNI balance should fall towards 0 | Bookkeeper |
| [ ] | Resume customer invoices (Gold Plates etc.) on the new items only, with EPOS stock-out; no `SR-` numbers | Bookkeeper (after the invoice tool is repurposed, §4) |
| [x] 2026-10-03 | Undeposited Funds automation built: `uf_deposits` = daily run step 5 (till sheet → Bank Deposits + true-up transfers, gated, off by default). Branch `claude/akponora-uf-deposits` | Agent |
| [ ] | Till sheet access: Google service account (read-only) + share the sheet as Viewer + key in `/data/secrets/` (`SERVER_SETUP.md` §12) | Marvin (OIAT Admin) |
| [x] 2026-10-03 | UF days made independent (owner decision): a held / missing till-sheet day no longer blocks later days; per-day state `days.json` replaces the cursor (old cursor migrated); till-sheet status in every run's Slack + `uf_deposits status`; tolerance read every run (`settings.env` toggle, no restart). Branch `claude/uf-independent-days` | Agent |
| [ ] | Staff: fill the till sheet for 25, 26 and 29 Sep (they now wait on their own; the daily Slack lists missing days) | Staff |
| [ ] | `OIAT_COMPANY_A_UF_DEPOSIT_ENABLED=1` (plan only); review the 25 Sep → today plans; then post (₦29.2M+ for 25 Sep–1 Oct) by hand or set `OIAT_COMPANY_A_UF_AUTO_POST=1` + ref (chat yes) | Marvin + Agent |
| [ ] | Send the updated freeze note / new rules to the bookkeeper | Marvin |
| [ ] | QBO closing date → 30/09/2026 (optional but recommended) | Marvin |

- [ ] **Deposits: banks equal the till sheet exactly** (agreed with Marvin 3 Oct). The sheet − sales difference goes to a Cash Over/Short line, plus one clean-up journal for the days posted with receipts-scaled targets. Details: `HANDOVER_TRACKER.md` §4 item 4a.
- [ ] **Investigate the daily till sheet vs EPOS sales difference** (§4 item 4b).

- [ ] **Deposits: aged-hold alert** after about 7 days (Slack + portal inbox). See tracker §4 item 4c.
- [ ] **1 Oct read-only re-check** so the portal Home stops flagging it (tracker §4 item 4d).

- [ ] **Goldplates invoicing** from a shared Google Sheet (design in `HANDOVER_TRACKER.md` §4 B).
- [ ] **Bank reconciliation project:** the ₦200.6M unpaid-bills backlog (no BillPayment since 18 May), monthly statement reconciliation, Cash Over/Short (`HANDOVER_TRACKER.md` §4 C).
- [ ] **Credit sales** (`HANDOVER_TRACKER.md` §4 D).
- [x] Cash-on-delivery bills paid from Petty Cash automatically (`bill_payments`, 4 Oct).

## 3. Staff / EPOS data fixes (not blocking)

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | 51 products with no master link in EPOS (e.g. Coke/Fanta/Sprite 60CL singles, CWAY water singles) → link them, then rebuild the mapping | Staff |
| [ ] | Frozen food by kg, eggs, loose rice: make one stock-tracked master per family (unit g / Each), then rebuild the mapping so they become Inventory | Staff + Agent |
| [ ] | 78 "ghost stock" families opened at qty 0: count them; any real stock → one approved stock adjustment on the new item | Staff + Agent |
| [ ] | Pricing review: `outputs/final_mapping_2026-10-01/pricing_review.csv` | Staff |
| [ ] | Answer: Ernest's 19 Sep stock adds (deliveries or recounts?); POs 3828/3829/3855/3861 duplicates? If deliveries/non-duplicates → adjust GRNI | Staff, then Agent |

## 4. Repurpose old tools (instead of only guarding them)

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | **Products & Stock page** (portal, replaces the old inventory review page): EPOS product ↔ mapping ↔ new QBO item, EPOS vs QBO qty, negative stock, catalogue-sync queue | Agent |
| [ ] | **Legacy retirement tool (W10)** (old `qbo_pack_variant_cleanup` / `qbo_inventory_remediation`: removed 5 Oct; in git history): retire `LEGACY —` items in small approved batches (check qty/value → zero to `300150` → inactivate) | Agent (each batch needs Marvin's yes) |
| [ ] | **Correct-a-posted-day tool** (old `qbo_delete_sales_receipts`: removed 5 Oct; in git history): void + re-post a day through the pipeline, with hold/approval and an audit record | Agent |
| [ ] | **Customer invoices on new items** (old invoice importer: removed 5 Oct; in git history; EPOS ID/SKU match, stock-out check, proper numbering) | Agent |
| [ ] | **Manual bill import** inside `bills_sync` for purchases outside EPOS POs (old bill importer: removed 5 Oct; in git history) | Agent |
| [x] 2026-10-05 | `qbo_inv_manager`: removed with the rest of the legacy inventory stack (in git history) | Agent |
| [ ] | Remove each temporary safety guard once its tool is refactored | Agent |
| [x] | **Portal phase 1 (read-only)**: Company A daily run on Schedules, Company A daily-runs list + run detail (steps, review items, log tails, evidence viewer), Overview card, Holds & alerts panel | Agent |
| [ ] | **Portal phase 2 — Review & Approvals inbox**: approve / skip held bills, new vendors, new products (catalogue review) and deposits, each with who/why audit; clear the posting hold with approver + reason (archives the hold file as `clear-hold` does); "Run now" for the daily run (whole run or `--only` steps, dry-run first) | Agent |
| [ ] | **Portal phase 3 — Products & Stock / Suppliers / Deposits pages**: Products & Stock (see above), Suppliers (vendor mapping, created / held vendors, PO supplier names), Deposits (Undeposited Funds balance, days since last deposit, proposed deposits once the deposits step is real) | Agent |
| [ ] | **Portal phase 3 — settings and mapping editors with audit**: daily-run env toggles / caps, product and vendor mappings, each change recorded (who, when, before/after) and synced to the files the ops jobs read | Agent |

## 5. Platform / team

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | Each team member: QBO **sandbox** profile for development (`./build/run-sandbox.sh`); no production tokens on laptops | Team |
| [ ] | Production work happens on the server (server Claude session, SSH over Tailscale, or portal "Run now") | Team |
| [ ] | Portal as the single UI for Company A: phase 2 (approvals inbox, hold clear, Run now) and phase 3 (Products & Stock, Suppliers, Deposits, settings/mapping editors) from §4; until then approvals stay on the CLI | Agent / Marvin |
| [ ] | Optional: read-only token endpoint on the server (1-hour access tokens, never the refresh token) if laptops need production lookups | Agent |
| [ ] | Public ingress for QBO webhooks (portal is tailnet-only) so webhook item lookups work | Marvin / Team |
| [x] | Repo clean-up phase 2 (3 Oct, Claude): root shims, old scheduler pair and `akponora_canonical_catalog.py` deleted; `uf_reverse_and_allocate` archived; finished docs archived. Journal templates kept (tested, used by `post_journal`). `.env.example` completeness still open | Claude |
| [x] 2026-10-05 | Repo clean-up phase 3: legacy inventory stack and Inventory Review pages, `credit_sheet`, `ops_scheduler` / `akponora-ops`, env fallback scheduler, one-off cutover scripts, `docs/archive/`, finished briefs/runbooks/checkpoints removed (in git history). Posting contract kept as `docs/AKPONORA_POSTING_CONTROLS.md` | Claude |
| [ ] | Merge PR #62 to `master` after the first clean week | Marvin |

| [ ] | Portal redesign phases 2–3 (brief: `docs/CODEX_BRIEF_PORTAL_REDESIGN.md`) | Codex |
| [ ] | **Client dashboard (future):** a separate, simple dashboard for Company A staff: fill in the till breakdown as a form, mark bills paid, see the product catalogue and items needing fixes. The OIAT Portal stays for OIAT staff. Agency/internal client-workspace delivery is tracked separately in `OIAT_PORTAL_DELIVERY_PLAN.md`; first UX fixes are local only | Team |

## 6. Accounting follow-ups (year-end or accountant)

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | Large equity balances `300100` (−₦531.2M) / `300150` (₦428.0M) from the historical corrections: decide a year-end clean-up | Accountant |
| [ ] | 1–16 Sep cost of sales sits in equity via the 16 Sep reset; optional reclass to P&L | Accountant |
| [ ] | July/August GPFH invoice COGS (~₦26.6M possibly overstated); review | Accountant |
| [ ] | VAT on purchases: GRNI was accrued ex-tax; check the treatment when real bills arrive | Accountant |
| [ ] | Count corrections from October: evidence investigation, movement capture and portal classification built locally 6 Oct (off; 106 tests). Count drafts cannot post. Remaining: PO matching, follow-up closure, sandbox-tested FIFO posting, signed count approval and deployment. See `AKPONORA_STOCK_RECONCILIATION.md` | Agent + Accountant |

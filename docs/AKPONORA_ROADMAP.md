# Akponora (Company A) roadmap and checklist

One place for everything outstanding. Tick items off here (`[x]`) and add the date. Rules live in [`AGENTS.md`](../AGENTS.md); history in [`AKPONORA_CUTOVER_LOG.md`](AKPONORA_CUTOVER_LOG.md).

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
| [ ] | Undeposited Funds: fill the till sheet for 25, 26 and 29 Sep; deposit 25 Sep onwards (₦29.2M+) by the till-sheet method | Staff, then Agent |
| [ ] | Send the updated freeze note / new rules to the bookkeeper | Marvin |
| [ ] | QBO closing date → 30/09/2026 (optional but recommended) | Marvin |

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
| [ ] | **Legacy retirement tool (W10)** from `qbo_pack_variant_cleanup` + `qbo_inventory_remediation`: retire `LEGACY —` items in small approved batches (check qty/value → zero to `300150` → inactivate) | Agent (each batch needs Marvin's yes) |
| [ ] | **Correct-a-posted-day tool** from `qbo_delete_sales_receipts`: void + re-post a day through the pipeline, with hold/approval and an audit record | Agent |
| [ ] | **Customer invoices on new items** from the old invoice importer (EPOS ID/SKU match, stock-out check, proper numbering) | Agent |
| [ ] | **Manual bill import** inside `bills_sync` for purchases outside EPOS POs (from the old bill importer) | Agent |
| [ ] | `qbo_inv_manager`: keep only read-only item checks; drop start-date editing for Company A | Agent |
| [ ] | Remove each temporary safety guard once its tool is refactored | Agent |

## 5. Platform / team

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | Each team member: QBO **sandbox** profile for development (`./build/run-sandbox.sh`); no production tokens on laptops | Team |
| [ ] | Production work happens on the server (server Claude session, SSH over Tailscale, or portal "Run now") | Team |
| [ ] | Optional: read-only token endpoint on the server (1-hour access tokens, never the refresh token) if laptops need production lookups | Agent |
| [ ] | Public ingress for QBO webhooks (portal is tailnet-only) so webhook item lookups work | Marvin / Team |
| [ ] | Repo clean-up phase 2 (Codex): root shims, old scheduler pair, `akponora_canonical_catalog.py`, journal templates, `.env.example` completeness | Codex |
| [ ] | Merge PR #62 to `master` after the first clean week | Marvin |

## 6. Accounting follow-ups (year-end or accountant)

| Done | Item | Owner |
| --- | --- | --- |
| [ ] | Large equity balances `300100` (−₦531.2M) / `300150` (₦428.0M) from the historical corrections: decide a year-end clean-up | Accountant |
| [ ] | 1–16 Sep cost of sales sits in equity via the 16 Sep reset; optional reclass to P&L | Accountant |
| [ ] | July/August GPFH invoice COGS (~₦26.6M possibly overstated); review | Accountant |
| [ ] | VAT on purchases: GRNI was accrued ex-tax; check the treatment when real bills arrive | Accountant |
| [ ] | Month-end from October: QBO FIFO is primary; post only the verified variance between QBO item quantities/value and the EPOS count | Agent + Accountant |

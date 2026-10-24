# Nora whole-process hardening — active handover

Started 24 October 2026 at 16:50 UTC. Owner explicitly requests implementing the all-round review in one branch, running production from that branch, and reviewing today's run before any merge to master. Do NOT merge this branch without that later review.

Branch: `codex/nora-process-hardening`, base master `df046f4`. Prior runtime efficiency release PR #64 is already deployed. Company A only; no EPOS API; existing financial caps/approval identities remain unless a documented hardening holds uncertain work. No test financial writes or historical backfills.

## Clock and deployment gate

Next daily schedule is 17:00 UTC. At start only nine minutes remain. Owner answered: KEEP the 17:00 UTC run and deploy AFTER it finishes. Do not change the schedule or interrupt that job. Owner is UNSURE whether completed stock adjustments are editable: continue fresh edit-safe detail checks; do not use immutable movement-detail caching.

## Scope from all-round review

- [ ] Durable/atomic per-operation state and recovery after uncertain financial responses.
- [ ] Completed credit-invoice work records; unique existing invoice with exact total/customer/item checks.
- [ ] Separate PO discovery evidence from unresolved accounting work without losing late old orders.
- [ ] Shared appropriately fresh source evidence; keep live validation before financial writes.
- [ ] Incremental movement discovery with edit-safe detail handling and periodic wide completeness checks.
- [ ] Delivery-versus-stock-addition matching/hold; aggregate exposure visibility; retain approved caps.
- [ ] Permanent payment identities with backward-compatible sheet layout, immediate durable save, shared outcomes and uncertain-response reconciliation.
- [ ] Repayment/till overlap checks, physical cash evidence input and bank reconciliation evidence (never invent balances/statements).
- [ ] Focused final stock verification after writes with cutoffs/freshness recorded.
- [ ] Stall handling/progress/review ownership; safe recovery and maintenance documentation.
- [ ] Backup consistency, isolated restore validation and protected retention; encrypted offsite acceptance depends on configured destination/key.
- [ ] November practice fixtures/instructions and initial outstanding-balance evidence. Human training/live opening-balance signoff cannot be fabricated by code.
- [ ] Local/fake-service tests, production-host read-only shadow checks, isolated candidate build/tests, idle activation to branch.
- [ ] Review today's real run; leave branch deployed and PR unmerged pending owner review.

## Evidence and recovery

Evidence under gitignored `outputs/run_performance_20261024/process_hardening/`. Preserve existing `.agents/`, `.playwright-mcp/`, `skills-lock.json`, original tracker stash and server .env backups.

Current known-good image: `oiat-portal:latest` = `sha256:6b8c6ecfdf2bc3bc00e1317faa27d311fb593355df20a9d8be9806df25cac1f2`; save a new rollback tag before activation. Prior older rollback also exists as `oiat-portal:nora-before-efficiency-20261024`.

17:00 UTC checkpoint: implementation underway, no new deployment. Atomic JSON writes, SQLite per-operation work state, grouped complete credit-invoice checks/prepared-work reuse, immediate payment persistence/recovery and permanent IDs in optional column K, stronger stock eligibility and final post-adjustment snapshot are drafted. Credit tests 12 pass; payments 11 pass. Full suite currently has only stock tests needing changes for the explicitly stronger delivery/aggregate-cap policy; do not deploy until all old/new tests pass and live read-only shadows validate actual QBO invoice fields. Stock details remain freshly checked.

Today's 17:00 run must finish using the prior PR #64 image. Do not replay a financial routine merely to benchmark this branch. After it finishes, use its saved evidence for shadow comparisons, deploy the fully validated branch idle, and leave PR unmerged. Update this file with each completed area, tests, SHA, schedule outcome and actual running image.

17:09 UTC: core drafted changes pass all 475 existing code_scripts tests after updating complete-entity fake responses and stock-policy expectations. Still add dedicated interruption/identity/edit/cap regression tests, money cross-checks, discovery work state, backup/restore and production read-only validation before deployment. The 17:00 job is running on the old PR #64 image and remains untouched.

21:19 UTC: resumed after usage interruption. Today's master/PR #64 run completed 17:19:53 UTC in 19m52s, exit 3 (review), no failed steps: catalogue 454s, bills 48s, sales 70s, credit 8s, guard 21s, stock 557s, stock_follow 10s, banking 12s, payments 8s. Billing capture: 0 detail pages, 209 selected, no missing. All three missing catalogue details were Cancelled with no received date/value; branch now excludes these purely cancelled records from receipt capture.

Read-only candidate invoice shadow validated all 5 historical invoices against source/customer/lines/total with 2 QBO reads in 6.666s, temporary state only. Additional drafted changes: batched discovery work records with first-seen aging, repayment/till overlap guards and fresh source checks before automatic banking, physical-cash/bank-evidence validators, encrypted incremental backup/isolated-restore CLI (cryptography 50.0.2 installed locally). None of this branch has been deployed. Offsite destination/key-custodian question is pending. Still implement/add regression tests, finish operational acceptance/retention, validate live browser HTML reader equivalence and real backup/restore, then rebuild/test and activate idle production on this branch. Master must stay unchanged.

21:28 UTC implementation checkpoint: 489 code_scripts tests pass. Added strict existing Bill supplier/date/AP/item/quantity/tax/price checks (paid bills may still match); full creation-history PO list discovery, retaining the active receipt/detail window and accounting cursor. Added same-item/quantity/near-date delivery overlap holds, weekly full open-period fresh stock recheck, final stock-check exception handling, money-evidence/aged-review reports, backup retention and isolated restore hook. Backup Mac path regression fixed. Live stock HTML/full-browser comparison and full-history PO/bill shadow validation are in progress; production remains untouched on master and idle, next run 25 Oct 17:00 UTC. No branch deployment or merge yet.

21:35 UTC validation update: full-history filtered-response wait now proven live: 4,168 orders, active receipt/duplicate window 209, 262 cache hits, ZERO detail pages/missing, 24.370s collection; complete bill shadow 39.286s, 127 exact existing matches, 22 ready, 17 held, no financial writes. Earlier 67-order shadow was the default-list response race and is superseded. Ten fresh stock detail pages match full-browser accounting data (date/status/location/staff/note and all product/quantity/cost/reason values); barcode display decoration excluded, stable TransferID remains URL-based. HTML reads ~0.22–0.36s each versus ~1.53–1.93s browser on these samples. Empty grids/pagers still use full browser fallback. Permanent sheet IDs now read back before use; fresh staff rows checked before payment, and status updates follow IDs if sorted during the run. Historical stock postings without old source hashes establish a labeled observation baseline; this cannot prove there were no edits before the baseline. No new branch deployment yet.

21:38 UTC release preparation: all 494 code_scripts tests pass locally (3.273s), including lost accepted payment/stock responses, duplicate/copied IDs, sorted-row status targeting, changed banking source, aggregate stock cap, delivery overlap, edited movements, exact invoice/bill matches, atomic-file interruption and encrypted incremental SQLite restore/retention/tampering. Upstream master remains df046f4 after fetch. Production idle and still on master; next schedule tomorrow 17:00 UTC. Server recovery preflight: required DB/token/config/mapping files present, 2,274 Company A operational files (~832MB), cryptography 50.0.2 already installed; no backup key/config yet. The candidate backup dependency is therefore already satisfied in the base image. Staff guide and read-only outstanding-balance snapshot tool are implemented; actual November practice, opening-balance acceptance and offsite/key custody remain human actions. Final sheet compatibility/invoice shadows, candidate image validation, real encrypted restore and branch activation remain before completion.

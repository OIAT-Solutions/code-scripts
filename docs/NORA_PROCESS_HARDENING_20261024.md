# Nora whole-process hardening — active handover

Started 24 October 2026 at 16:50 UTC. Owner explicitly requests implementing the all-round review in one branch, running production from that branch, and reviewing today's run before any merge to master. Do NOT merge this branch without that later review.

CURRENT: deployed to web/scheduler at 21:48 UTC, 24 Oct. First full branch run is 25 Oct 17:00 UTC; PR #65 stays unmerged. See Deployed result below.

Branch: `codex/nora-process-hardening`, base master `df046f4`. Prior runtime efficiency release PR #64 is already deployed. Company A only; no EPOS API; existing financial caps/approval identities remain unless a documented hardening holds uncertain work. No test financial writes or historical backfills.

## Clock and deployment gate

Next daily schedule is 17:00 UTC. At start only nine minutes remain. Owner answered: KEEP the 17:00 UTC run and deploy AFTER it finishes. Do not change the schedule or interrupt that job. Owner is UNSURE whether completed stock adjustments are editable: continue fresh edit-safe detail checks; do not use immutable movement-detail caching.

## Scope from all-round review

- [x] Durable/atomic per-operation state and recovery after uncertain financial responses.
- [x] Completed credit-invoice work records; unique existing invoice with exact total/customer/item checks.
- [x] Separate PO discovery evidence from unresolved accounting work without losing late old orders.
- [x] Shared appropriately fresh source evidence; keep live validation before financial writes.
- [x] Incremental movement discovery with edit-safe detail handling and periodic wide completeness checks.
- [x] Delivery-versus-stock-addition matching/hold; aggregate exposure visibility; retain approved caps.
- [x] Permanent payment identities with backward-compatible sheet layout, immediate durable save, shared outcomes and uncertain-response reconciliation.
- [x] Repayment/till overlap checks, physical cash evidence input and bank reconciliation evidence (never invent balances/statements).
- [x] Focused final stock verification after writes with cutoffs/freshness recorded.
- [x] Stall handling/progress/review ownership; safe recovery and maintenance documentation.
- [x] Backup consistency, isolated restore validation and protected retention; encrypted offsite acceptance depends on configured destination/key.
- [x] November practice fixtures/instructions and initial outstanding-balance evidence. Human training/live opening-balance signoff cannot be fabricated by code.
- [x] Local/fake-service tests, production-host read-only shadow checks, isolated candidate build/tests, idle activation to branch.
- [x] Review today's real run; leave branch deployed and PR unmerged pending owner review.

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

21:42 UTC server validation checkpoint: draft PR #65 created (https://github.com/OIAT-Solutions/code-scripts/pull/65), branch pushed through 2769d32. Production checkout is on this branch; running services still use prior master image. Rollback tag preserved as oiat-portal:nora-before-hardening-20261024. Real backup test exposed cross-filesystem os.replace from container /tmp to /data; fixed with encrypted destination-filesystem temporary publication and added regression. All 495 local tests pass. Final candidate rebuild is underway; do NOT activate before isolated tests, real backup restore and idle check pass. The protected key and backup config were created on /data; no key values were displayed and no financial writes occurred. Offsite and separate key custody are still unconfigured.

21:45 UTC: server candidate passed 495 tests in 33.738s and Django system checks; all 15 changed runtime module hashes matched. Encrypted local set and full isolated restore passed for 2,433 files in 62.254s (including DB/token/mapping/config). Coverage found Oct 1 manual-cutover raw/processed references were not beside archive metadata; exact matching originals DO exist elsewhere under /data/code_scripts. Backup collector now includes those unique exact files and refuses missing/ambiguous declared archive sources; add regression and repeat validation before activation. Subsequent audit-only boundary brings local suite to 496, plus archive-location regression pending. Sunday wide stock audit detects older edits but newly discovered old unposted movements require explicit review, preserving the ordinary three-day auto window. No financial replay or activation has occurred.

21:46 UTC: manual Oct 1 archive originals found elsewhere under the server state root; collector now resolves the unique exact source filenames without moving originals and refuses missing/ambiguous evidence. All 497 local tests pass (3.042s). Final server rebuild/test and repeated encrypted recovery coverage/restore remain before activation. The earlier successful backup/restore is retained; new set must include all 23 raw/processed day references. No production service restart yet.


## Deployed result — 24 Oct 21:50 UTC

Runtime code through `606302c` is deployed on **codex/nora-process-hardening**, draft PR [#65](https://github.com/OIAT-Solutions/code-scripts/pull/65). Master remains `df046f4`. Mac and production checkouts stay on the hardening branch. Image `sha256:77cdac0d8fd2faec5593766269664deae516a47afd3fb536756ffae89567ab24` is running in web and scheduler, each with zero restarts at acceptance. All 15 changed runtime modules match the tested Mac sources in BOTH containers. Portal HTTP 200, no active jobs, one schedule worker, Nora enabled for **25 Oct 17:00 UTC / 18:00 Lagos**. Ingress containers were not recreated.

Validation: **497** code_scripts tests pass locally (3.042s) and in the final isolated network-disabled server image (27.948s). Django system check reports no issues. GitHub Gitleaks checks pass. No real QBO transaction was posted as a test and no full financial routine was replayed. Live read-only bill, invoice, stock HTML and existing-adjustment signature checks are above. Payment IDs are initialized/read back in column K only; A:J remains compatible.

Final encrypted local set restored **2,436 files**, with consistent SQLite integrity and every saved checksum verified. All **23 raw and 23 processed archive references** are included, including the uniquely located Oct 1 recovery originals. Incremental repeat: **6 changed / 2,430 reused**, backup plus full isolated restore **16.211s**. Key/config reside on the protected server paths in the staff/recovery guide; key values were never printed. Routine now runs configured backup/restore after financial work under its global lock. Current QBO outstanding balances are captured server-side for owner review; they are not signed November opening balances.

Operational boundaries: unresolved New Stock/no-reason changes and possible delivery/count duplicates HOLD. The existing stock value cap is checked over the whole transfer. Sunday wider source rechecks detect older edits, but a newly discovered old unposted movement is review-only outside the normal three-day automatic window. Historical postings with no original source hash get an explicitly labeled observation baseline; edits before that baseline cannot be ruled out by the new checksum. Missing cash/statement inputs remain "needs evidence". No bank-statement authentication or human count is claimed.

Rollback while idle: tag `oiat-portal:nora-before-hardening-20261024` as `oiat-portal:latest`, recreate only web/scheduler, verify HTTP/schedule/worker. This rollback image is `sha256:6b8c6ecfdf2bc3bc00e1317faa27d311fb593355df20a9d8be9806df25cac1f2`. Do not revert persistent financial ledgers/work databases or restore a backup over production to roll back code. Reconcile any uncertain QBO write before retrying.

### Required acceptance still open

- [ ] Observe the first COMPLETE scheduled run on this hardening branch (25 Oct), review duration and exceptions, then obtain the owner's merge/return-to-master decision. Today's 19m52s run only validates prior PR #64.
- [ ] Owner selects an offsite destination and separate recovery-key custodian; prove restore from that destination before claiming offsite recovery. The async question is pending. Local encrypted recovery is already validated.
- [ ] Staff practise in a copy, provide real cash/statement evidence and sign genuine outstanding opening balances at the November cutoff. No staff practice/live signoff has been fabricated.

21:52 UTC final evidence refresh: the outstanding-balance snapshot records 51 open customer invoices and 857 open supplier bills (includes older/manual QBO records; owner review required). Final protected set includes that evidence: 2,437 files restored, all 23 raw/processed archive references included, 5 changed / 2,432 reused, 14.194s backup plus complete isolated restore. Retention removed one superseded same-day index and one unreferenced blob while retaining two independently restorable sets. Both payment tabs have Payment ID headers installed/read back, zero staff payment rows, and no financial test writes.

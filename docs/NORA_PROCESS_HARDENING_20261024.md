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

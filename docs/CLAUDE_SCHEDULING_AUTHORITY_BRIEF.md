# Prompt for Claude: one EPOS/QBO scheduling authority

We are separating OIAT's agency portal from client-specific OIAT workspaces. All of these dashboards are for OIAT staff. Client business applications remain separate.

Please assess and implement a local, tested consolidation of EPOS/QBO scheduling. The preferred outcome is one scheduling authority, with company-specific workflows executed in background workers. This is not authorization to deploy, change production automation flags, or write to QuickBooks.

## Read first

Read `AGENTS.md`, `docs/HANDOVER_TRACKER.md`, `docs/SERVER_SETUP.md`, `docs/AKPONORA_OPERATIONS_CONTROLS.md` and `docs/OIAT_PORTAL_DELIVERY_PLAN.md`. These production safeguards remain mandatory.

## Confirmed starting point

Read-only inspection on 5 October found both `scheduler` and `akponora-ops` running in Docker on OIAT-SRV-01. `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1` was set in both containers. The compose definitions run:

- `scheduler`: `python manage.py run_schedule_worker`.
- `akponora-ops`: `python -m code_scripts.akponora_ops.ops_scheduler`.

Docker hosts the workers. The two Python scheduling implementations currently decide when work starts. The portal worker excludes Company A while the dedicated daily-run mode owns it. Recheck the deployed image, settings, schedules and execution history before relying on this snapshot.

## Decision and implementation

1. Trace both scheduling implementations, RunSchedule/RunJob dispatch, daily_run, locks, business-date selection, run records and recovery. Explain which guarantees each provides.
2. Determine whether the portal schedule worker can safely schedule Nora's full daily routine and Goldplates' sales workflow. Prefer this as the single authority if it can preserve the required guarantees. If another choice is materially safer or simpler, present the evidence and proposed design before implementing that alternative.
3. Separate scheduling from execution. The authority decides when and what to run; workers execute existing business tools. Never run financial workflows in a web request or duplicate daily_run's accounting logic in the scheduler or operations console.
4. Implement the chosen scheduling design locally in an isolated branch/worktree. Preserve ordered daily steps, approval references, payload/plan SHA gates, global locking, preview exclusion, immutable evidence and audit identity. Confirm nested-lock behaviour; do not introduce a scheduler lock that deadlocks the existing daily_run lock.
5. Maintain exactly one owner per workflow during and after migration. Both worker processes may coexist during a controlled transition, but cannot independently schedule the same routine. Do not leave two unrelated authorities as the permanent design.

## Required operational behaviour

- Explicit enabled/paused state, timezone, next due time and execution timestamps.
- A closed trading date determined through the existing business-date contract.
- Clear handling of restart, duplicate dispatch, overlap, partial completion, cancellation and stale jobs.
- Define missed-run handling deliberately. Do not silently backfill financial days or replay a completed day merely because a worker was offline.
- Retain confirmed posting evidence even when a later attempt fails. Completed subprocess status alone is not accounting confirmation.
- Preserve all scheduled Company A steps and the existing Company B workflow. Do not change either company's business rules as part of scheduling consolidation.
- The agency console consumes operational summaries; it must not schedule the same EPOS/QBO work a second time.

## Coordination with Codex's interface work

Codex is working on `codex/oiat-portal-ux` in `/Users/marvinmokolo/.codex/worktrees/oiat-portal-ux/code-scripts`. Checkpoint `8a377ed` follows `403565b`; inspect newer commits before integrating.

Do not reset that worktree or edit its files concurrently. Keep your changes in a separate branch. Focus on scheduler, dispatch and execution contracts. Avoid template/frontend changes. If models, views.py or shared presentation adapters must change, identify the overlap explicitly and integrate after the interface checkpoint is available.

The interface needs a company-neutral schedule summary: company identity, workflow identity/name, enabled/completed state, configured timing/timezone, next due timestamp, last execution timestamp, last business date, outcome, freshness and permitted actions. Keep internal scheduler ownership in diagnostics, not normal operator copy. Do not claim that one worker's heartbeat proves every workflow is healthy.

No separate frontend or API framework is required merely to consolidate scheduling. Reuse existing queue, permissions and audit mechanisms where they satisfy the requirements.

## Validation and release handoff

Test with synthetic local state and mocked external writes. Cover duplicate dispatch, restart, lock contention, business-date boundaries, previews, stale execution records and missed runs. Run the relevant portal and operational suites. Update the handover tracker and provide an exact changed-file/commit list.

Prepare a reviewable production cutover plan: verified current image/commit, schedule ownership before/after, exact required flag changes, affected containers, active-job checks, rollback and post-deploy verification. Preserve server untracked files and persistent state. Existing notes say Docker builds over Windows SSH may fail due to credential-store access; confirm the supported build method rather than assuming remote build works.

Stop before production activation. Marvin will approve the concrete release and any production automation changes separately. Do not run QBO writes to prove this design. Report local validation separately from deployment and live acceptance.

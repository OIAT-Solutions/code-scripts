# One scheduling authority for EPOS/QBO workflows

Status: implemented and tested locally on branch `claude/scheduling-authority` (5 Oct 2026). **Not deployed.**
Production activation needs Marvin's approval of the cutover below (AGENTS.md). It was written against
`docs/CLAUDE_SCHEDULING_AUTHORITY_BRIEF.md` and `docs/OIAT_PORTAL_DELIVERY_PLAN.md`.

## Before (5 Oct 2026)

| | `akponora-ops` (`ops_scheduler`) | `scheduler` (`run_schedule_worker`) |
| --- | --- | --- |
| Owns | Nora's daily routine (`daily_run`), cron from `.env` | Goldplates sales and other portal schedules |
| State | in memory / env | `RunSchedule` rows: enabled, timezone, next due, last fired, events |
| Run record | evidence folders only | `RunJob` plus evidence |
| Worker offline at the fire time | **silently skipped** | **one catch-up run** of the latest closed day on restart |
| Executes in | its own container | wherever the queue is dispatched, **including the web container** when a page dispatches |

Two authorities: the portal worker excluded Company A while `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1`.

## After

The **portal schedule worker** is the one authority. It decides when and what runs. The worker process
executes each job as a subprocess of the existing business tool.

- **Nora's routine:** a `RunJob` scope `company_a_daily` runs
  `python -m code_scripts.akponora_ops.daily_run --date <D>`, unchanged.
  - `D` is the closed trading date bound when the job is queued (`get_target_trading_date` at the due time; same 05:00 Lagos contract as `daily_run`, tested).
  - Steps, approval references, SHA gates, the global file lock, preview exclusion and evidence all stay inside `daily_run`.
  - Exit 0 and 3 are recorded as succeeded (3 = finished with items waiting for review); 2 is failed.
- **Ownership:** one switch, `OIAT_COMPANY_A_DAILY_RUN_OWNER`.
  - `ops_scheduler` (the default, today's behaviour): the portal row records `skipped_not_owner`.
  - `portal`: the akponora-ops cron refuses to schedule `daily_run`.
  - Only the oldest "Nora daily routine" row can ever queue; duplicates are skipped.
- **Missed runs (Nora):**
  - A fire more than `OIAT_SCHEDULE_MISSED_GRACE_MINUTES` (180) late is **not run**. It records `skipped_missed`, posts a Slack OIAT line, and leaves the day unconfirmed on Home.
  - A person runs it from the Inbox (Run daily for date).
  - A day with a completed real run is never replayed (`skipped_done`).
  - Dry runs and failed runs don't count as done.
- **Goldplates:** unchanged (business rules and missed-run behaviour).
- **Execution placement:** with `OIAT_JOBS_DISPATCH_IN_WORKER_ONLY=1`, only the worker process starts queued jobs. Pages and Inbox actions only queue (start within one poll, 15 s), so a web restart can't kill a financial run. The worker starts queued jobs every cycle.
- **Locks:**
  - The portal `RunLock` (database) wraps the job.
  - `daily_run` takes the global file lock itself; `job_runner` never takes it (tested), so there is no nested acquisition and no deadlock.
  - Overlap: a fire while the routine is running records `skipped_overlap`.
  - The stale-lock rule (`clear_if_stale`) and the stuck-run reconciler apply as before.
- **Restart mid-run:** the child dies with the worker container. The reconciler marks the job failed. Posted evidence is kept (daily_run evidence, RunArtifacts). Re-running the day is safe: bills and sales dedupe, deposits resume. It's done deliberately from the Inbox.
- **Expected confirmed date** (`workflows.expected_confirmed_date`): Home and the Inbox banners expect the latest day whose scheduled run plus a 90-minute allowance (`OIAT_SCHEDULE_RUN_ALLOWANCE_MINUTES`) has passed. Before 18:00 / 19:00 Lagos the previous day is expected, so a day that simply hasn't run yet isn't "missing" (Marvin, 5 Oct).
- **Interface summary:** `workflows.workflow_summaries()` returns one row per workflow:
  - company, workflow id/name, enabled, completed, cron, timezone, next due;
  - last run, last business date, outcome, expected date, freshness, permitted actions;
  - `owner` (diagnostic only).

  No heartbeat is used as proof of health. For Codex's Schedules / Home presentation.

## Changed files (branch `claude/scheduling-authority`)

- `apps/epos_qbo/models.py`: `RunJob.SCOPE_COMPANY_A_DAILY`; event types `skipped_missed`, `skipped_done`, `skipped_not_owner`.
- `apps/epos_qbo/migrations/0021_workflow_scheduling.py`: choices only; no data change.
- `apps/epos_qbo/services/workflows.py` (new): ownership, missed policy, done check, expected date, summaries.
- `apps/epos_qbo/services/schedule_worker.py`: the Nora routine path; dispatch every cycle.
- `apps/epos_qbo/services/job_runner.py`: the `daily_run` command, success exit codes, worker-only dispatch.
- `apps/epos_qbo/services/company_a_ops.py`: `schedule_info` reads the owner.
- `apps/epos_qbo/services/experience.py`, `attention.py`: expected date per company. **Overlaps Codex's branch**: small, same functions.
- `apps/epos_qbo/management/commands/run_schedule_worker.py`: marks the worker process.
- `apps/epos_qbo/management/commands/ensure_workflow_schedules.py` (new): creates the paused Nora row.
- `code_scripts/akponora_ops/ops_scheduler.py`: steps aside when the portal owns the routine.
- `apps/epos_qbo/tests/test_workflow_scheduling.py` (new, 17 tests).

No template or frontend changes. `views.py` is unchanged: its `dispatch_next_queued_job()` calls become "queue only" under the worker-only flag. Codex should show "Queued" rather than "Started" for those actions.

## Production cutover (for approval; outside 17:45–19:30 Lagos)

1. **Release.**
   - Merge this branch and Codex's interface checkpoint into the main branch. Run both suites.
   - Record the current running image: `docker images oiat-portal` and `docker tag oiat-portal:latest oiat-portal:rollback-<date>`.
2. **Checks.**
   - No running or queued `RunJob`; `RunLock` inactive; global file lock free.
   - Note the server's untracked files (keep them).
3. **Deploy (behaviour unchanged).**
   - `git pull`. Marvin builds in a desktop PowerShell (Windows SSH builds fail on the credential store).
   - `docker compose up -d web scheduler akponora-ops`.
   - `python manage.py migrate` (0021).
   - `python manage.py ensure_workflow_schedules` (paused row).
   - Owner is still `ops_scheduler`: that night's run is still the akponora-ops cron. Verify Home and Schedules.
4. **Cutover (a separate yes).**
   - Add to `.env`: `OIAT_COMPANY_A_DAILY_RUN_OWNER=portal` and `OIAT_JOBS_DISPATCH_IN_WORKER_ONLY=1`.
   - `docker compose up -d web scheduler akponora-ops`.
   - Check the akponora-ops log says the portal owns `daily_run`.
   - Enable "Nora daily routine" on the Schedules page; check its next due is 18:00 Lagos.
5. **Verify after the first portal-owned run.**
   - A `queued` event at 18:00 and a `company_a_daily` job running in the `scheduler` container.
   - The `daily_run` evidence folder and Slack summary.
   - Job `succeeded` (exit 0 or 3); Home confirms the day.
6. **Rollback** (any time):
   - remove `OIAT_COMPANY_A_DAILY_RUN_OWNER` (or set `ops_scheduler`) and pause the row;
   - `docker compose up -d scheduler akponora-ops`;
   - image rollback if needed: `docker tag oiat-portal:rollback-<date> oiat-portal:latest`, then `up -d`.
7. **Later:** after a clean week, retire the akponora-ops cron (compose profile) in its own reviewed change.

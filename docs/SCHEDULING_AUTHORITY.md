# One scheduling authority for EPOS/QBO workflows

Status (5 Oct 2026): done. The cutover below was completed on 5 Oct 2026, and the legacy scheduling stack
was removed the same day (see "Legacy removal"). The portal schedule worker is the only scheduler. It was
written against `docs/CLAUDE_SCHEDULING_AUTHORITY_BRIEF.md` and `docs/OIAT_PORTAL_DELIVERY_PLAN.md`.

## Before (5 Oct 2026)

| | `akponora-ops` (`ops_scheduler`) | `scheduler` (`run_schedule_worker`) |
| --- | --- | --- |
| Owns | Nora's daily routine (`daily_run`), cron from `.env` | Goldplates sales and other portal schedules |
| State | in memory / env | `RunSchedule` rows: enabled, timezone, next due, last fired, events |
| Run record | evidence folders only | `RunJob` plus evidence |
| Worker offline at the fire time | **silently skipped** | **one catch-up run** of the latest closed day on restart |
| Executes in | its own container | wherever the queue is dispatched, **including the web container** when a page dispatches |

Two authorities: the portal worker excluded Company A while `OIAT_COMPANY_A_DAILY_RUN_ENABLED=1`.

## How it works now

The **portal schedule worker** (`python manage.py run_schedule_worker`, the `scheduler` container) is the one
authority. It decides when and what runs, and it is the only process that starts jobs. It executes each job
as a subprocess of the existing business tool. There is no owner switch and no env fallback schedule.

- **Catalogue** (`apps/epos_qbo/services/workflows.py`, `WORKFLOWS`): only these can be scheduled.
  - "Daily routine" (`company_a_daily`): Company A only, one per company. Row "Nora daily routine", default 18:00 Africa/Lagos, created paused by `python manage.py ensure_workflow_schedules`. Turned on/off and timed on the Schedules page.
  - "Sales sync" (`single_company`): every company except Company A. Goldplates (company_b) runs at 19:00 Lagos.
  - The worker refuses any other scope (inventory sync can no longer be scheduled). Company A sales are never in a sales schedule: all-company runs always exclude Company A, and a Company A Sales sync schedule is refused. No env switch changes that.
- **Nora's routine:** a `RunJob` scope `company_a_daily` runs
  `python -m code_scripts.akponora_ops.daily_run --date <D>`, unchanged.
  - `D` is the closed trading date bound when the job is queued (`get_target_trading_date` at the due time; same 05:00 Lagos contract as `daily_run`, tested).
  - Steps, approval references, SHA gates, the global file lock, preview exclusion and evidence all stay inside `daily_run`.
  - Exit 0 and 3 are recorded as succeeded (3 = finished with items waiting for review); 2 is failed.
  - It runs inside the `scheduler` container, which gets every `OIAT_COMPANY_A_*` switch from `env_file: .env`. The sales step still posts only with `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1` and `OIAT_COMPANY_A_STANDING_APPROVAL_REF`.
  - Only the oldest "Nora daily routine" row can ever queue; duplicates are skipped.
- **Missed runs (Nora):**
  - A fire more than `OIAT_SCHEDULE_MISSED_GRACE_MINUTES` (180) late is **not run**. It records `skipped_missed`, posts a Slack OIAT line, and leaves the day unconfirmed on Home.
  - A person runs it from the Inbox (Run daily for date).
  - A day with a completed real run is never replayed (`skipped_done`).
  - Dry runs and failed runs don't count as done.
- **Goldplates:** unchanged (business rules and missed-run behaviour).
- **Execution placement:** only the worker process starts queued jobs, always. Pages and Inbox actions only queue (start within one poll, `OIAT_SCHEDULER_POLL_SECONDS`, 15 s), so a web restart can't kill a financial run. The worker starts queued jobs every cycle.
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

  No heartbeat is used as proof of health. For Codex's Schedules / Home presentation.

## Changed files (branch `claude/scheduling-authority`, history)

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

## Production cutover (done 5 Oct 2026; history)

Steps 1 to 5 were carried out on 5 Oct 2026. The owner switch and worker-only flag they mention were removed the same day (see "Legacy removal").

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
6. **Rollback** (no longer applies after the legacy removal):
   - remove `OIAT_COMPANY_A_DAILY_RUN_OWNER` (or set `ops_scheduler`) and pause the row;
   - `docker compose up -d scheduler akponora-ops`;
   - image rollback if needed: `docker tag oiat-portal:rollback-<date> oiat-portal:latest`, then `up -d`.

## Legacy removal (5 Oct 2026)

Removed:

- The `akponora-ops` container / compose service and `code_scripts/akponora_ops/ops_scheduler.py` (its cron loop, `--list`, `--run-now`, the `OIAT_AKPONORA_<JOB>_CRON` / `_CMD` env vars, `OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS`). `COMPOSE_PROFILES=akponora-ops` no longer means anything.
- The owner switch `OIAT_COMPANY_A_DAILY_RUN_OWNER`, and `OIAT_COMPANY_A_DAILY_RUN_ENABLED` / `OIAT_COMPANY_A_DAILY_RUN_CRON`. The Daily routine's on/off and time are the portal row "Nora daily routine".
- `OIAT_JOBS_DISPATCH_IN_WORKER_ONLY`: pages and Inbox actions always only queue; only the worker starts jobs.
- The env fallback schedule (`OIAT_SCHEDULER_ENABLE_ENV_FALLBACK`, `SCHEDULE_CRON`, `SCHEDULE_TZ`, the "Legacy Env Fallback" / "System Fallback Schedule" row, `is_system_managed`). Migration `0022_remove_legacy_schedules` deletes that row and the legacy inventory schedules and drops the field.
- Scheduled inventory sync: the worker refuses any scope not in the catalogue.

Still in use: `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED` + `OIAT_COMPANY_A_STANDING_APPROVAL_REF`, the other `OIAT_COMPANY_A_*` feature switches, `OIAT_COMPANY_A_DAILY_RUN_LOCK_WAIT_MINUTES`, `OIAT_COMPANY_A_DAILY_RUN_STEP_SLACK`, `OIAT_SCHEDULER_POLL_SECONDS`, `OIAT_SCHEDULE_MISSED_GRACE_MINUTES`, `OIAT_SCHEDULE_RUN_ALLOWANCE_MINUTES`.

Deploy:

1. After the evening runs (not between 17:45 and 19:30 Lagos), build the image.
2. `docker compose up -d --remove-orphans` (removes the old `akponora-ops` container).
3. `docker compose exec web python manage.py migrate` (0022).
4. Delete these obsolete `.env` lines: `OIAT_COMPANY_A_DAILY_RUN_ENABLED`, `OIAT_COMPANY_A_DAILY_RUN_CRON`, `OIAT_COMPANY_A_DAILY_RUN_OWNER`, `OIAT_JOBS_DISPATCH_IN_WORKER_ONLY`, `COMPOSE_PROFILES=akponora-ops`, `SCHEDULE_CRON`, `SCHEDULE_TZ`, `OIAT_SCHEDULER_ENABLE_ENV_FALLBACK`.

## Independent review (5 Oct 2026) and outcome

Review of `06bbe6d`; fixed in the follow-up commit. Tests were added for each fix (24 scheduling tests in total).

| # | Finding | Outcome |
| --- | --- | --- |
| 1, 2 | The Nora row was created system-managed, so the env-fallback logic disabled it, and the Schedules page couldn't enable or run it | Fixed: user-managed row; fallback never touches it (removed 5 Oct: no fallback) |
| 3 | A web-side reconcile could mark a live, silent daily job failed (PID in another container; step output not in the job log) | Fixed: a held global flock means "running" for `company_a_daily`; scope added to the flock rules |
| 4 | A partial Inbox run (`--only`) counted as the day done | Fixed: only a full routine with no skipped steps counts |
| 5 | A stale due time could raise a false "missed" alert | Fixed: days that already ran are filtered out first; enabling recomputes the due time |
| 6 | The owner switch was read once at ops_scheduler start | Fixed: re-checked before each run; the loop no longer crashes after a flip (found by the test). **Both containers must still be recreated together after the `.env` edit** (step 4). (Removed 5 Oct: no owner switch) |
| 7 | Slack HTTP call inside the DB transaction | Fixed: `transaction.on_commit` |
| 8 | Goldplates Home and Inbox banners now use the expected date (no "missing" before 19:00 + 90 min Lagos) | Kept: Marvin asked for this for both companies (5 Oct). It's a presentation rule, not a Goldplates pipeline change |
| 9 | A duplicate daily job could queue for the same date (Inbox plus schedule) | Fixed: re-checked at dispatch; cancelled if the day finished meanwhile |
| 10 | A multi-day outage reported one day | Fixed: every missed day listed once (minus days that ran) |
| 11 | Cron defaults differed | Fixed: both use the ops default; production sets `0 18 * * *` |
| 12 | A Nora row with the wrong or blank company | Fixed: refused |
| 13 | Worker-only mode needs the scheduler up; SQLite has no row locks | Noted: page actions show "Queued" (Codex). The worker is the only dispatcher in worker-only mode, so races disappear once it's on. (5 Oct: worker-only is now always on) |
| 14 | The summary's "confirmed" rule differed from Home's | Fixed: same verified-sales rule |

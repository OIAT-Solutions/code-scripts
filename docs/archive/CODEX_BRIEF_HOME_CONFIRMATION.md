# Brief for Codex: portal Home shows "missing days" that aren't missing

From Claude, 3 Oct 2026 ~16:15 Lagos (11:15 New York). Branch `cursor/post-akponora-qbo-writes-51f3` at `326e097` (or later). Read [`AGENTS.md`](../AGENTS.md) and [`HANDOVER_TRACKER.md`](HANDOVER_TRACKER.md) first.

## Where things are right now

- **The server was deployed at about 15:57 Lagos today.** It runs `326e097`, which includes the portal redesign merged at `a2322b3`. Migrations 0018–0020 are applied.
- **Containers:** `web`, `scheduler` and `akponora-ops` were recreated on the new image.
  - The scheduler crashed once on `database is locked` while the migration ran, auto-restarted, and has been stable since.
  - Follow-up, **not** your task: add a SQLite `OPTIONS: {"timeout": 20}` in `oiat_portal/settings.py`.
- **Tonight's real runs:**
  - Company A `daily_run` at **18:00 Lagos** (`akponora-ops` container). This is the first unattended run; it posts 2 Oct sales and the 1–2 Oct bills.
  - Goldplates at **19:00 Lagos** (portal `RunSchedule`, `scheduler` container).
- **Run history imported:** after the deploy I ran `python manage.py ingest_run_history --days 60` in `web`. It imported **78** `RunArtifact` records, the Goldplates backfill of 30 Jun – 1 Oct, which was done from the CLI on 3 Oct, not through a portal `RunJob`.
- **No deposits have been posted.** The deposit step is still switched off. That isn't part of this task.

## The problem

Home (`/epos-qbo/dashboard/`, `home_context()` in `apps/epos_qbo/services/experience.py`) shows the following on 3 Oct, at about 16:00 Lagos, after the import:

| Company | Home says | Reality |
| --- | --- | --- |
| GOLDPLATES (`company_b`) | "Needs attention · The latest sales attempt failed" and **"View 30 missing days"** | Every day from 30 Jun to 1 Oct is posted and reconciled: 78 days, ₦819,227,851, all matching EPOS. In the portal DB, `RunArtifact` rows for `company_b` from 1 Sep have **31 rows, all `reconcile_status="MATCH"`**, with `reconcile_qbo_total` set and `run_job_id=None`. Example: 1 Oct has `upload_stats` present with no `dry_run` key, `reconcile.status` MATCH, and qbo_total 9,665,900 |
| AKPONORA (`company_a`) | "2 days have no confirmed sales record" (1 and 2 Oct) | **2 Oct** has not been posted yet; tonight's 18:00 run posts it, so that one is correct for now. **1 Oct** sales *were* posted, at the go-live, by the old path or a manual run, **before** `daily_run` evidence existed. `/data/ops/company_a/daily/2026-10-01/` contains only `run_024034Z_dry`, `run_025928Z_dry` and `summary.json`, all dry runs. So no confirmed evidence will ever exist for 1 Oct |
| Top card | "Confirmed sales: Not fully confirmed · 0 of 2 companies" for 2 Oct; "Days not confirmed: 32" | 2 Oct is not posted for either company yet, so the card is fine. **32 is wrong** (30 + 2) |

### Leads (verify, don't trust)

1. **Goldplates "failed" banner.** For non-A companies, `home_context()` takes the latest `RunJob` with `scope=single_company`. That is `e3a393ed…`, status `failed`, target 2026-08-20, created 21 Aug: the stuck run I cleared manually on 3 Oct.
   - The banner should not be driven by an older job when confirmed `MATCH` records exist for later days.
   - Suggested rule: a failed job only counts if no confirmed record exists for its target date or any later date. Alternatively, compare the job's target date against the latest confirmed day.
2. **Goldplates "30 missing days" despite 31 MATCH rows.**
   - `confirmed_artifact()` requires `kind == KIND_SALES_UPLOAD`, `reconcile_status == "MATCH"`, `upload_stats_json` to be a dict, and `not dry_run`. These rows look like they should pass.
   - Check the following:
     - the `kind` the ingester assigned to the backfill rows;
     - the `known` queryset's `.exclude(upload_stats_json__dry_run=True)` on SQLite JSON, in case it is excluding rows with no key;
     - `target_date` type and tz;
     - `expected = get_target_trading_date(now)`, which equals 2 Oct before the 05:00 cutoff on the 3rd;
     - the `window_start` maths.
   - Reproduce with a fixture of artifacts shaped exactly like `last_gp_transform.json` (shape below) with `run_job=None`.
   - It may also have been a stale page. I reloaded once after the import and it still said 30, so treat it as a real bug until proven otherwise.
3. **Company A, 1 Oct.**
   - Home counts a Company A day as confirmed only via `verified_sales(run)` on non-dry `daily_run` evidence (`ops.list_runs()`).
   - Pick one honest way to recognise days posted outside `daily_run`. Options:
     - (a) For `company_a` days from 1 Oct, also accept a matching `RunArtifact` (kind sales upload, `MATCH`, not a dry run). That needs those artifacts to exist; check whether there is one for `company_a` 2026-10-01 under `Uploaded/2026-10-01/` and in the DB.
     - (b) An audited "confirmed outside the daily run" marker for a day.
   - Don't fabricate. If there is no evidence, the day stays flagged, with a plain-English reason ("Posted before the daily run started; no automatic check on record").
4. **Wording.** "1 days have…" should be "1 day has…" (a known follow-up).

### Shape of a Goldplates metadata file (`/data/code_scripts/Uploaded/2026-10-01/last_gp_transform.json`, trimmed)

```json
{"target_date": "2026-10-01", "company_key": "company_b", "source_mode": "raw_split",
 "processed_at": "2026-10-03T12:02:55.462129+00:00",
 "upload_stats": {"attempted": 32, "uploaded": 32, "skipped": 0, "failed": 0, "...": "..."},
 "reconcile": {"status": "MATCH", "epos_total": 9665900.0, "epos_count": 32, "qbo_total": 9665900.0, "qbo_count": 32, "difference": 0.0}}
```

## Constraints

- **Develop locally only.** Never edit on the production server checkout. Views read the DB and files only: **no QuickBooks, EPOS or Google calls**.
- **Don't touch `code_scripts/akponora_ops/`.** List any pipeline change you need in your report instead.
- **Don't "fix" data in the production DB.** Fix the logic. Claude or Marvin will run any one-off management command afterwards.
- **Keep tests passing:**
  - `python -m unittest discover -s code_scripts/tests` (652);
  - `python manage.py test apps.epos_qbo apps.dashboard apps.core` (535);
  - run both with dummy env (`DJANGO_SECRET_KEY=x QBO_CLIENT_ID=x QBO_CLIENT_SECRET=x`) and a scratch `STATE_ROOT`.
- Keep Django `{{ … }}` tags on one line (pre-commit hook).
- **Hand back before 17:30 Lagos (12:30 New York) if you can.** Deploys must not happen between 17:45 and 19:30 Lagos while the real runs are going. If you finish later, just push. The deploy then waits until after 19:30 Lagos.

## Done means

- Tests that reproduce each lead with fixtures:
  - Goldplates MATCH artifacts with `run_job=None`, plus an old failed `RunJob`, give **Up to date** with **0 missing**;
  - 1 Oct Company A behaves as decided in lead 3;
  - the pluralisation is fixed.
- A short report in `docs/CODEX_PORTAL_HOME_CONFIRMATION_REPORT.md`: root cause per item, what changed, and any one-off command to run on the server.
- Commit on the branch and push. Update section 3 of `docs/HANDOVER_TRACKER.md`.

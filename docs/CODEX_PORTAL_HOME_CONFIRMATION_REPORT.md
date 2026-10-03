# Home sales confirmation fix — 3 October 2026

## What changed

Home and Daily runs now recognise confirmed sales imported from the CLI, including Company A sales posted before its daily run existed. No production data was changed. Views still read local files and the portal database only.

### Goldplates missing days

The ingester assigns the correct `sales_upload` kind. The fault was Home's `.exclude(upload_stats_json__dry_run=True)`: on SQLite, absent JSON keys evaluate as NULL, so that exclusion also discards the imported metadata where `dry_run` is absent. The fixtures reproduce the exact metadata shape in the brief through the real ingester, with no linked `RunJob`.

Home now applies `confirmed_artifact()` in Python to the candidate MATCH records. Both the latest confirmed date and the coverage count use that same rule. Explicit previews and malformed stats remain unconfirmed. The business-date cutoff and 30-day window were correct and are unchanged.

### Goldplates old failed warning

Home previously used the latest portal job's status without considering later confirmed business dates. A failed job now drives the warning only when its target date is after the latest confirmed date (or when either date is unknown). The August failure therefore remains in history but does not override September/October confirmations. A failed attempt for a newer unconfirmed day still warns.

### Company A 1 October

Chosen approach: accept matching non-preview sales-upload artifacts as well as verified daily-run evidence. Daily-run evidence takes precedence for Home's amount when both sources confirm the same day; dates are counted once. Daily runs also displays the imported October evidence, so the link from Home leads to the record that confirmed the day.

If no matching artifact exists, 1 October remains unconfirmed. Home explicitly says: "1 October has no automatic sales check on record; check earlier posting evidence before retrying." Dry-run evidence, a successful job without reconciliation, or the cutover log alone does not fabricate confirmation.

The production existence of Company A's 1 October metadata/DB artifact was not checked during this local-only task. The fix does not require a marker or a migration.

### Counts and wording

Both "1 day has" and "View 1 missing day" use singular wording. Aggregate counts now reflect the accepted evidence.

At the brief's 3 October afternoon timestamp, the expected trading day is **2 October**. Given Goldplates records only through 1 October, **one** Goldplates day remains unconfirmed until tonight's run provides evidence. Company A contributes **one** if its 1 October artifact exists, or **two** if it does not. Thus the total is **2 or 3**, not 32, based on the supplied state. Zero missing Goldplates days is correct when checking through 1 October, or after a confirmed 2 October record exists. Tests cover both dates.

## Validation

Dummy credentials, an absent env file, and separate scratch STATE_ROOT directories were used. No live QBO/EPOS/Google requests were made.

- Portal suite: **543 tests passed** (535 existing + 8 regression tests).
- Pipeline suite: **652 tests passed**.
- Focused Home/Daily tests: **29 passed**, included in the full portal suite.
- Django template-variable guard and whitespace check passed.

Regression coverage includes imported Goldplates metadata without a dry-run key, the August failure, a genuinely newer failure, the still-unconfirmed 2 October day, Company A with/without imported go-live evidence, linked October artifacts, duplicate evidence, malformed/preview records, and rendered singular wording.

## Handover

Deploy through the normal process, outside **17:45–19:30 Africa/Lagos**. This task does not deploy or restart anything, edit pipeline code, alter production records, or post transactions.

No one-off data fix is needed for Goldplates' already-imported records. If Company A's 1 October metadata exists under Uploaded but is not in the database, Claude/Marvin can run the existing read-only import after checking that file's MATCH/non-preview evidence:

```sh
python manage.py ingest_run_history --days 60
```

If the metadata is absent, retain the unconfirmed label and investigate the original posting evidence separately. Do not re-run sales merely to fill the portal record.

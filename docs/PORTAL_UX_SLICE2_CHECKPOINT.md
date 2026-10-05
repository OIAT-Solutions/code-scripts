# Portal UX slice 2: schedules and run evidence

Local branch: `codex/oiat-portal-ux`. No server, DNS, financial engine or scheduler configuration changes.

## Changes

- One schedule overview with Active, Paused and History views across both execution systems. Akponora is no longer a separate overview card above the portal schedules.
- Execution timestamp and business date are separate. Next/last timestamps use Lagos time; recurring timing retains the configured timezone. An active schedule without a next timestamp says Next time not recorded.
- Worker diagnostics remain inside settings; the main schedule overview does not explain internal scheduling ownership.
- Existing create/edit/toggle/run controls and worker history remain in a labeled disclosure. There is one page-level heading. The controls disclosure still lists portal-managed schedules; it is not a second cross-company overview.
- Generic run details expose sales evidence before Downloads and diagnostics. Preview status and missing amounts remain explicit. Non-sales runs do not acquire an empty sales panel.
- Successful Akponora steps collapse; exceptions remain open. Deposit facts show scalar counts, and preview attempts do not claim recorded deposits.
- Follow-ups are labeled as historical findings from this attempt. Downloads are grouped by workflow rather than one long flat file list.
- Page title eyebrows removed from Schedules and both run-detail presentations.

## Validation and preview

Browser inspected Active and Paused schedules and Akponora run details against synthetic state. Confirmed one page heading, separate execution/business dates, readable deposit counts, collapsed successful steps and expanded review steps. Generic evidence visibility and preview exclusion also have regression tests.

Local preview remains `http://127.0.0.1:8014`. Synthetic preview user now has schedule-management permission so schedule controls can be inspected; it has no posting/trigger permission and no production credentials. A synthetic Goldplates schedule was added locally. No scheduler runs in this preview. Sample times and dates are not production settings.

The final full portal suite passed **544 tests** in 39.8 seconds. `git diff --check` passed. Existing approval, queue, evidence-path and permission tests are included. Mobile/alternate production-role journeys are not yet verified.

## Next

Follow-up on 5 October: removed the Akponora-specific configuration explanation, the overview worker warning and the per-row service-management label. The disclosure now reads **Schedule settings**. Browser review confirmed the revised overview and settings disclosure. The 45 schedule UI and Company A operations tests passed; `git diff --check` passed. `CLAUDE_SCHEDULING_AUTHORITY_BRIEF.md` provides the separate scheduling consolidation brief and release constraints. No scheduling configuration changed.

Capability-driven company Overview and task responsibilities, followed by stock/register detail layouts. Agency-wide registry and summary integration remain operations-console work. Deployment still requires review of the concrete local release and separate approval.

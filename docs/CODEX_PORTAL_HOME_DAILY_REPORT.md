# Home and shared Daily runs — local redesign pass

3 October 2026. Branch: `codex/portal-redesign-phase1`, originally created from `ac4f15f`. This follows the completed phase 1 inbox commit and the experience review. Nothing was pushed, deployed or posted to QuickBooks.

## Delivered

- Home now shows confirmed sales for the last closed business day, review items and days without confirmation. It uses Company A daily evidence for October instead of the old June/July dashboard records. The business-day cutoff is respected.
- One aligned company row shows the last confirmed sales date, known issue, next recorded schedule and an appropriate next action. Missing days, a sales pause, stuck/failed work and a connection requiring attention cannot appear green. Connection expiry that can renew normally is not itself a new blocker.
- Coverage is explicit: the last 30 closed days, or since 1 October for Akponora. Missing portal evidence is described as missing confirmation, not proof that no transaction exists in QuickBooks. Users are told to review before retrying.
- Confirmed-sales amounts are shown only when every company selected has a recorded confirmation and amount for that day. An unknown or incomplete amount is not silently replaced with zero.
- Desktop and mobile use the same shared navigation: Home, Needs your attention, Companies, Daily runs, Schedules and Admin. Admin appears only for staff or users who can manage portal settings. Account remains accessible from the profile area. The sidebar has no company-specific entry.
- Daily runs combines Company A daily records and the older portal's sales records. It groups attempts by company and business date, keeps earlier confirmation visible after a failed retry, distinguishes previews, and supports company, date and outcome filters plus pagination. Undated jobs remain explicitly undated. Pre-October Company A sales history remains available from archived records.
- Logs bookmarks redirect to Daily runs, retaining company/date/status parameters. Stock checks and review jobs remain available as Other recent activity; they cannot be mistaken for sales confirmation.
- Routine run controls are collapsed beneath the history. Akponora still goes through the phase 1 confirmation and existing tool gates; other-company controls retain their existing backend. Akponora is excluded from the other-company sales selector.
- The inbox starts with waiting decisions; its routine run form moved to Daily runs. Outdated text claiming that portal approvals arrive in phase 2 was corrected.
- A restrained shared style provides aligned panels, consistent navigation, readable forms and status labels, including mobile and dark-mode layouts. A mobile navigation-close hit-area issue found during browser checks was fixed.

## Validation

- Full portal suite: **470 tests passed**, including **21 new tests** for confirmation, duplicates, previews, missing amounts, failed retries, coverage gaps, cutoff dates, company filters, historical records, schedule scope, navigation, permissions and activity separation.
- Pipeline suite: **591 tests passed**. Pipeline modules were not modified.
- Tailwind rebuilt; Django system check, migration-drift check and template-variable guard passed. Diff whitespace checked with CRLF-aware settings because the existing base template uses CRLF.
- Local browser verification at 1440 px desktop and 390 px mobile: Home, Daily runs, company filtering, drawer open/close and dark mode. The mobile Home and Daily runs content measured 390 px wide, with no horizontal overflow.
- Browser and tests use a scratch state directory without production tokens or an environment file. Screenshot amounts, review items and connection warnings are synthetic examples, not statements about the live accounts.

Screens are copied to the original checkout's gitignored `outputs/portal_phase1_review/` directory:

- `phase2-home-desktop.jpg`
- `phase2-home-mobile.jpg`
- `phase2-home-mobile-dark.jpg`
- `phase2-daily-runs-desktop.jpg`
- `phase2-daily-runs-mobile.jpg`
- `phase2-daily-runs-mobile-history.jpg`
- `phase2-navigation-mobile.jpg`
- `phase2-admin-mobile.jpg`

## Boundaries and remaining work

This completes the authorised first redesign pass: **Home, shared navigation and Daily runs**. It does not finish all of phase 2. Company tabs, the complete rewrite of existing run-detail screens, and the wider plain-English message catalogue remain. Detailed products/suppliers/deposits editors remain phase 3 work. Admin is now a shared destination; splitting the existing settings and connection screens internally remains later work.

History is deliberately bounded to recent evidence (the existing daily reader's 200-run cap, up to 1,000 archived sales records and 200 portal jobs before grouping). Date/company filters narrow archived record queries before that limit. A missing historical record is not an assertion that a day was never posted. Larger historical browsing may need a persistent index of daily evidence.

Company A's external schedule is displayed from the portal's current environment, as before; this is not proof that the external container is running. No automation switches were enabled. No supplier/product approval capability, financial permission, approval requirement or posting engine was expanded. Company B data and the pipeline/server files owned by the parallel pipeline work were not changed.

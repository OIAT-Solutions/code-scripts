# Company pages and supporting screens

Completed locally on 3 October 2026 on `codex/portal-redesign-phase1`, continuing the branch created from `ac4f15f`. Nothing pushed or deployed.

## What changed

- Company pages now have tabs for the work each company supports. Akponora has Sales, Purchases, Products & Stock, Suppliers and Deposits. Settings appears only for users allowed to edit company settings.
- Switching companies keeps the selected tab when the destination supports it, otherwise opens Sales. This keeps the sidebar short as more companies are added.
- The company directory presents latest sales, connection status and waiting decisions in aligned, searchable rows.
- Purchases and other tabs show existing waiting decisions and recorded daily results. Review links open the exact item in the attention inbox.
- Schedules show readable summaries first, with existing management forms and technical records behind a disclosure. Akponora remains managed separately through its existing controls.
- Daily details put the business date, company and outcome first. Supporting files, logs and internal references sit behind a disclosure.
- Missing evidence is shown as not recorded or not confirmed. A successful stock check does not claim that sales were posted. A later failed attempt does not hide earlier confirmed sales for the same day.
- Phone layouts now wrap tabs and banners, keep company switching within the screen, and prevent long breadcrumbs from crowding the top bar.

## Validation

- Portal suite: **489 tests passed**.
- Pipeline suite: **591 tests passed**.
- Django system checks and migration consistency checks passed.
- Tailwind stylesheet rebuilt; whitespace check passed with the existing CRLF file handled appropriately.
- Browser checks covered company tabs, company directory, schedules and daily details at desktop and phone widths. Checked phone pages had no sideways overflow.
- Screenshots used an isolated local database and synthetic review records. They are illustrative, not live accounting evidence.

## Scope and remaining work

These tabs use existing review records and run results. Complete stock, supplier and deposit registers and their editors are a later phase. The existing permission checks, financial posting gates, schedule actions and approval flows are retained. No production QBO actions, automation activation, mapping installation or pipeline changes were performed.

## Screenshots

Saved under `outputs/portal_phase1_review/` in the original checkout:

- `company-sales-desktop.png` and `company-sales-mobile.png`
- `company-purchases-desktop.png` and `company-purchases-mobile.png`
- `company-products-desktop.png`, `company-suppliers-desktop.png`, `company-deposits-desktop.png`
- `company-other-mobile.png`
- `companies-directory-desktop.png` and `companies-directory-mobile.png`
- `schedules-summary-desktop.png` and `schedules-summary-mobile.png`
- `daily-detail-desktop.png` and `daily-detail-mobile.png`
- `other-run-detail-desktop.png` and `other-run-detail-mobile.png`

Screenshots stay outside the commit.

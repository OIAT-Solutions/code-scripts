# OIAT Portal — page-by-page experience review

Reviewed 3 October 2026, against the portal redesign brief and Marvin's request for simple English, efficient work and a premium dashboard.

This is a review and proposed design direction, not an implemented redesign. It covers every EPOS/QuickBooks page template, shared navigation and the account/sign-in pages on the local branch based on ac4f15f, including the completed attention-inbox work. Earlier desktop/mobile checks cover the inbox, Home and approval screen; this review does not claim that every page has been visually tested in a browser. No production actions or Company B records were changed. The separate Websites application is outside this review.

## Main recommendation

Organise the portal around the work a person needs to finish. Home should answer: **Are sales up to date? What needs my decision? What has stopped?** A company page should show its business records. Daily runs should explain what happened. Setup and troubleshooting belong in Admin.

Keep one shared sidebar: **Home · Needs your attention · Companies · Daily runs · Schedules · Admin**. Put Account under the profile menu. Company names belong in a searchable company selector and the Companies page, not in additional sidebar entries. Switching company should retain the same tab when it applies. Show tabs only for features that company supports.

## Proposed Home

1. Page title **Home**, a clear date and **Last checked [time]**. One company filter and one date selection, reused throughout the page.
2. A red notice only when posting is stopped or days are missing: company, affected dates, known reason and a useful next action. Do not repeat the same notice in multiple cards.
3. Three compact summaries: **Sales posted**, **Waiting for your decision**, **Days not posted**. Every amount states the period it covers. Missing information displays **Not checked yet**, never a reassuring zero.
4. One aligned row per company: company name, **Sales posted through [date]**, current issue, waiting count, next scheduled run and one primary action. Use the same sources here and on the company page.
5. Sales chart below the operational rows, with explicit coverage dates. A chart should never distract from missing days or imply that an old snapshot is current.

Example wording, using illustrative values only: **“Akponora — Sales posted through 1 October. Two bills need your review.”** Button: **Review 2 bills**. If the evidence does not establish the cause: **“Sales for 2 October have not been confirmed. Check the latest run.”** Do not invent a download or connection failure.

## Page-by-page recommendations

| Current page | What slows people down | Recommended presentation and action |
| --- | --- | --- |
| Home / Overview | Repeated company status, separate chart filters, headline runtime/success figures and old dates compete with today's work. | Use the Home arrangement above. Make missing days and waiting decisions prominent. Move runtime and success rates into Admin details. |
| Needs your attention | The run form precedes the queue; mixed item types and raw details make each card harder to scan. | Put decisions first. Filter by company and type; show reason, amount where relevant, date and age. Use **Review bill**, **Choose supplier**, **Review products**, **Review deposits**. Move routine run controls into Daily runs. |
| Approval / confirmation | Full-width card is now fixed, but the screen needs more decision context; a generic confirmation button obscures the action. | Show the exact bill/supplier/day, amount, relevant lines and consequence before confirmation. Use **Approve bill** or **Confirm supplier**. Distinguish **Review later** from **Already handled elsewhere**; do not treat postponing as resolution. Keep the owner's approval requirement, described simply. |
| Companies | Configuration fields and repeated health counters crowd the directory. | Show searchable company rows: name, sales through date, waiting count, current problem and **Open company**. Put **Add company** in Admin. |
| Company detail | Operational history, connection information, configuration and record metadata share one long page. | Use **Sales · Purchases · Products & Stock · Suppliers · Deposits · Settings**. Begin each tab with current position and its next action. Put company identifiers and raw settings under Admin details. |
| Company A daily runs | A dedicated company page duplicates the shared runs screen and exposes container/schedule wording. | Include these records in shared Daily runs, filtered by company. Keep existing detail routes available through the shared table; remove the company-specific sidebar entry. |
| Runs | Complex run form and internal columns appear before readable results; Company A is a separate block. | One row per company and business day: date, outcome, sales amount, bills and waiting decisions. Expand repeated attempts inside that day. Buttons: **Run a day** and **Run past dates**, subject to the existing company gates. |
| Company A run detail | Step evidence is useful, but filenames, exit values and a stale “approvals come in phase 2” message interrupt the task. | Start with a sentence explaining the day's outcome. Link waiting decisions to the inbox. Show each step's outcome beneath it. Move logs/files into **Details** and correct the stale approval guidance. |
| Other run detail | Commands, internal IDs and dense stock-review information dominate the page. | Start with **What finished**, **What is waiting** and **What to do next**. Show stages with readable outcomes. Put internal IDs, commands and diagnostic data behind **Details**. |
| Logs | Repeats run counts, filters and run history from other pages. | Merge human-readable activity into Daily runs and its detail pages. Retain technical logs in Admin details for support. |
| Supporting files / evidence viewer | Filenames and machine-formatted records become the screen's main identity. | Use **Supporting records**, meaningful descriptions and **Download**. State the record's date and whether the view is incomplete. Preserve the underlying evidence for support. |
| Schedules | Cron expressions, worker status, infrastructure guidance and two scheduling approaches are mixed together. | Show **Every day at 6 pm, Lagos time**, next run, last outcome and whether scheduled runs are on. Use a simple time/frequency form where supported. An alive scheduler does not mean sales posted successfully. Company A controls need existing-tool support and approval; do not imply they already work. |
| Inventory review | “Marked reviewed” can clear a warning while the underlying issue remains; pack/base and starting-quantity language is hard to follow. | Separate **Reviewed** from **Fixed**. Show product, unit, EPOS quantity, QuickBooks quantity, last checked and explanation. Use company-specific tools. Company A must not inherit a generic legacy-quantity correction flow. |
| Missing-product preview | Account numbers, mapping fields and starting-stock options overwhelm the product decision. | Show product, the QuickBooks item it will use, unit, cost and what will happen. For Company A, route through its approved product process with new items starting at zero; no generic import shortcut. |
| Add company / basic setup | Setup competes with routine company management. | Put it under Admin, with a short guided form and readable labels. Show a final summary before saving. Keep required identifiers accurate in an expandable connection section. |
| Advanced company settings | Business rules, inventory switches, pack-suffix behaviour and raw configuration are mixed. | Split connection settings from business rules. Give each rule one clear explanation. Company A product units come from approved EPOS evidence, not guessed name endings. Advanced settings should be visible only to the appropriate staff. |
| QuickBooks connections | Routine token expiry can look like a fault; access/refresh terminology dominates. | Show **Connected**, **Connection needs attention** or **Not connected**, with last checked time and a useful action. Say **Check connection**; offer **Reconnect QuickBooks** only when a supported reconnect flow exists. Keep token details for support. |
| Settings | Personal preferences, shared defaults and automatic-update diagnostics share one destination. | Move personal preferences to Account. Put shared defaults, connections and alerts in Admin. Keep company-specific business settings on the company Settings tab. |
| Tools | Queries, commands and a destructive-tool reference are presented as routine work. | Place under restricted Admin. Offer named read-only checks where possible. Keep accurate technical detail in support sections; remove command references from normal business journeys. |
| Account | Raw permission names and “superuser” describe implementation rather than access. | Use **You can review bills**, **You can manage schedules**, etc., based on actual permissions. Keep Profile, Preferences and Password together. Label the administration link **Manage users and access**. |
| Sign in | Sparse branding and inconsistent button wording. | Match the portal's typography and colours. Use **Sign in** consistently, clear field errors and a brief help route. Retain the existing authentication behaviour. |
| Password change / success | Separate visual treatment and small fixed containers differ from operational pages. | Reuse the same form styling, dark-mode support and success feedback. Narrow password forms are appropriate; align their enclosing page with the shared layout. |
| Workspace chooser / coming soon | A different shell and vague unavailable-feature message disrupt continuity. | Use consistent OIAT branding and navigation. Say which feature is unavailable and give **Back to Home**. Hide unavailable work from routine navigation where appropriate. |

## Simple-English wording

Use a shared set of messages so the same outcome means the same thing on every page. Translation must follow the actual evidence, not just soften an error.

| Current wording | Suggested wording |
| --- | --- |
| EPOS → QBO synchronisation | Sales and purchases in QuickBooks |
| Dry run | Preview — nothing will be posted |
| Reconciled / MATCH | Sales totals match, when that specific check passed |
| Run succeeded | List what actually completed: **Sales posted**, **Bills posted**, or **Preview ready** |
| Subprocess exited with code 1 | **This step could not finish. Open details for the recorded reason.** Use a more specific sentence only when the cause is known. |
| Access token expired | **Connected**, when automatic renewal is working; explain a real renewal failure separately |
| HOLD | **Needs review** or **Sales paused**, depending on the actual reason |
| Artifact | Supporting record |
| Realm ID | QuickBooks company ID, in connection details |
| Cron | Run time; keep the precise expression in Admin details |
| Clear hold | Allow sales to run again — explain that this does not itself post sales |
| Confirm and queue | Name the action, then show **Waiting to start**, **In progress**, **Finished** or **Could not finish** |

Use full company names in headings; avoid Company A / Company B as primary names. Keep necessary business words such as bill, supplier, stock and deposit. Use **Already posted** separately from **Posted now**. Use **No records yet** separately from **No problems found**. A skipped or preview-only run must never look like completed posting.

## What will make it feel premium

- Consistent, full-width operational cards and tables, aligned to the same page edges. Use readable text widths within them rather than an unexpectedly narrow outer card.
- A quiet white/light-grey surface, dark readable text and one restrained accent colour. Reserve red for stopped/missing work, amber for review and green for confirmed completion. Pair every colour with words.
- Clear hierarchy: page title, one short explanation, useful summary, then the work. Remove repeated headings, unnecessary counters and decorative status icons.
- Consistent spacing, corner sizes, inputs, buttons and table rows. Use readable body text; critical reasons and approval facts should not be tiny captions.
- One clear primary action per item. Put secondary actions in a menu; give destructive actions an appropriate separate confirmation.
- Right-align money, use consistent currency formatting and label totals clearly. Format dates as **2 October**, with year where needed, and explicitly name Lagos time for schedules.
- On mobile, turn wide rows into stacked summaries with the amount, reason and primary action visible. Use accessible tabs/filters, a short breadcrumb and generous tap targets. Do not require sideways scrolling to understand a decision.
- Design empty, loading, waiting and failure states as carefully as populated screens. Preserve filters and scroll position after a decision. Show its result immediately and explain any remaining work.

## Efficiency changes worth doing first

1. **Trustworthy Home and one set of outcomes.** Replace competing status summaries with current evidence and explicit coverage dates. Staff should not open several pages to find out whether a day posted.
2. **Shared navigation and Daily runs.** Merge duplicate destinations, retain day/company filters and link directly from a problem to its relevant record or decision.
3. **Better approval context.** Show enough information to decide without downloading a file; preserve the existing tools, approval requirements and audit history. Avoid blanket approval for unrelated bills or unresolved holds.
4. **Company tabs.** Bring sales, purchases, products, suppliers and deposits together without adding sidebar entries. Keep each company's capabilities and operating rules intact.
5. **Consistent visual treatment and simple copy.** Apply shared layout, table, form, status and feedback components across desktop and mobile.
6. **Admin cleanup.** Move setup and support material out of daily work, and split personal preferences from shared business settings.

Items 1, 2, 4 and shared wording belong to phase 2 of the brief. Detailed stock/supplier/deposit screens belong to phase 3. Admin separation belongs to phase 4. Improve approval context alongside these without silently expanding what phase 1 can approve.

## Support needed before promising more actions

The presentation can improve immediately, but certain controls need evidence or existing-tool support: individual product approvals instead of whole-plan application; changing Company A's external schedule; a complete reconnect journey; richer bill and deposit decision details; and safely rerunning past dates across the differing company workflows. Check those interfaces with the pipeline owner before adding buttons. This review does not authorise pipeline changes, automation activation, QuickBooks writes or production deployment.

## Acceptance checks for the redesign

- In one glance, staff can tell which companies are up to date and which need action, with the relevant dates visible.
- Every problem says what is known, which day/company it affects and what to do next. Unknown causes remain explicit.
- Approval screens identify the exact action, supporting facts, amount where relevant and financial consequence.
- Acknowledging an issue does not mark it fixed; queueing an action does not mark it completed.
- Company filtering, day history, amounts and statuses agree across Home, company tabs, inbox and Daily runs.
- Routine pages contain no unexplained commands, internal IDs, raw configuration or developer errors.
- Desktop/mobile, keyboard use, dark mode and empty/failure states are checked. Permission checks and the existing financial gates remain in place.

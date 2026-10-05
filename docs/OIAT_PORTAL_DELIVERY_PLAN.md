# OIAT portal and client workspaces: delivery plan

Status: agreed direction; local implementation started. Updated 5 October 2026 Lagos time.

## Product boundaries

- `oiatsolutions.com`: public OIAT website, services, enquiries and recruitment.
- `portal.oiatsolutions.com`: OIAT staff overview, client registry, tasks, support, activity and access to client workspaces.
- `noramart.oiatsolutions.com`, `gpfh.oiatsolutions.com`, `bigz.oiatsolutions.com`, `polygrace.oiatsolutions.com`: proposed internal OIAT client workspaces. Clients do not use these dashboards. Final slugs must be agreed before DNS changes.
- Client-owned domains such as `admin.workingpeopleunited.com`: client business applications, where needed. Staff forms and support requests can originate here.

There is no second client-facing OIAT portal in this plan. OIAT client workspaces share staff identity, navigation conventions and a route back to the agency overview. A subdomain does not imply a separate codebase or deployment.

## Repository ownership

`oiat-operations-console` owns the agency shell, client/project registry, service offering catalogue, central task/support summaries, client workspace entry points and integration presentation. It currently contains sample data, not live integrations.

`code-scripts` owns EPOS/QBO execution, company configuration, approved mapping, evidence, queues, locks, approval contracts and the detailed EPOS/QBO workspace. Improve this workspace here. Do not duplicate its financial engine in the console.

`OIAT Website` owns public content and intake. `wopu-admin` and other client application repositories own their business records and staff workflows. They are reference-only for this work unless a later change is expressly authorized.

This plan is copied to both participating repositories. Update both copies together while this work spans them. The console copy owns the agency direction; the code-scripts copy carries its EPOS/QBO implementation checkpoints. Keep commits separated by repository. Do not rename repositories or change hostnames during the first release.

## Organization and data model

Client relationship → company/legal entity → service engagement → enabled workflows.

Projects describe delivery work and managed applications. Environments, repositories and technical components describe implementation. An OIAT service offering (EPOS → QBO, website management, booking platform) is distinct from a technical component (API, scheduler, database).

Nora and Goldplates remain separate company records with separate QBO evidence and permissions. Shared commercial relationships may group them without merging their operations. Each can use the EPOS/QBO offering with different enabled workflows. Shared repositories can support multiple engagements; do not force repository ownership to one client merely because the prototype has a single project association.

The console owns agency metadata: owners, contacts, engagements, support requests, assignments and registry links. Client systems own membership, booking, inventory and accounting records. The console receives bounded operational summaries and evidence links. It must not infer a complete financial ledger from those summaries.

## Proposed screens

Agency navigation: Overview, Clients, Tasks, Services, Activity, Support, Administration. Technical deployments, repositories and integrations remain accessible within managed projects and expert views; avoid an oversized main menu.

Agency Overview: urgent OIAT work, requests awaiting response, monitored service exceptions, recent activity and upcoming work. Display scope and freshness. Do not make unrelated clients' combined sales the agency's headline KPI.

Client workspace: client identity and OIAT owner, supported engagements, current tasks, requests, recent work and service-specific entry points. Client navigation depends on supported capabilities.

EPOS/QBO workspace: Overview, Sales, Purchases, Products & stock, Suppliers, Deposits, Activity, Schedules and Settings where supported. Nora's extra workflows do not appear as mandatory features for Goldplates.

Support: one request identity with client/company/engagement context; separate client-visible replies and OIAT-only notes. Central and client views reference the same record. A ticket is not a financial approval. Start with request intake and assignment before SLA automation or external messaging.

## Phase 1 — correct the current EPOS/QBO presentation

Owner: code-scripts. Work locally in an isolated worktree; preserve concurrent accounting work.

First slice:
- Use the same closed-trading-date rule in Home and Tasks.
- Keep incomplete till-sheet amounts missing; suppress misleading comparisons.
- Normalize list-valued deposit summaries into counts; retain raw evidence in diagnostics.
- Replace "Waiting for your decision" with "Open tasks" where the count still mixes responsibilities. Do not relabel all nine records as approvals or independently actionable tasks.
- Explain task coverage for companies where review tooling is not available.
- Remove the Home title eyebrow and the most repetitive labels.

Next slices:
- Normalize current tasks versus historical findings and group supplier/bill dependencies.
- Repair business-date-specific evidence links and unavailable-record states.
- Unify schedule presentation across both execution systems; separate Active, Paused and History.
- Present business evidence before logs/downloads; normalize both run-detail layouts.
- Improve stock units, filters, pagination, focused details and alert destinations.
- Separate supplier aliases, standing handling rules and exclusions.
- Add capability-driven company Overview and preserve filter/date context.

Acceptance: before the trading cutoff, Home and Tasks agree; blank cash is not zero; confirmed posting survives a later failed attempt; no raw dictionary appears as a headline metric; unsupported workflows are not reported healthy; business evidence is traceable to its date; approval and lock contracts remain unchanged.

## Phase 2 — establish the real agency registry

Owner: oiat-operations-console.

Replace fictional sample clients with an explicit registry. Register OIAT internal systems, Nora, Goldplates and WoPU using verified identifiers and links. Future clients remain proposed until confirmed. Add a client detail route and service engagements. Distinguish offering names from technical components.

Choose durable storage and staff authentication from the actual server/identity environment before implementation. Do not use localStorage or in-memory preview data as production authority. No broad identity migration is assumed.

Acceptance: each known client has an owner, engagements, lifecycle, supported tools and verified workspace links. Sample health, uptime, costs and incidents are absent from production views.

## Phase 3 — connect one real service end to end

Owners: code-scripts publishes bounded read-only summaries; console consumes them.

Start with EPOS/QBO. Define a small versioned summary contract: client/company/engagement IDs, enabled workflows, last confirmed business date, recorded sales scope, current task counts by responsibility/type, last attempt, next schedule, source timestamp and evidence links.

Use an authenticated read-only endpoint or a controlled summary export, chosen after inspecting server networking. Include unavailable and stale states. The console does not read QBO tokens, run arbitrary commands or query QBO directly. Detailed actions continue through the existing EPOS/QBO workspace and approval flow.

Acceptance: a real console company summary links to the correct detail; source unavailability is explicit; financial execution remains independent of console availability; no duplicated scheduler is introduced.

## Phase 4 — client-specific entry points

Owners: console for agency routing; code-scripts for service routing; infrastructure changes reviewed separately.

Define staff access per client and action. Reuse staff login where practical while keeping application permissions enforced on every request. Resolve an allowed hostname to a registered client; never use a hostname alone as authorization. Separate sessions, CSRF/origin configuration and access rules must be reviewed before activation.

Preview client context with local routes first. Then configure approved internal subdomains, proxy routes and TLS on OIAT-SRV-01. Preserve the current tailnet access boundary unless a separate access change is approved. Selecting a company changes context; it must not imply user impersonation.

Acceptance: OIAT staff see only assigned clients; client users cannot access OIAT-only domains; unknown hosts fail closed; company data cannot leak through filters or object links; central-to-client navigation works.

## Phase 5 — support intake and additional services

Add one genuine client-facing request intake in the appropriate client application or a focused form. Persist requests centrally and show them in both OIAT views. Keep client-visible content separate from internal notes. External notifications and replies require explicit authorization when added.

Connect WoPU with selected operational status rather than member data. Add future booking/healthcare systems using the same registry and summary conventions, with domain-specific workflows and data access decisions. Do not prebuild their business applications now.

## Local preview and testing

Use isolated synthetic state, a local SQLite database and local preview accounts. Never load production credentials or start schedulers for preview. Show two companies with different capabilities, completed work, missing input and genuine discrepancy states. Label synthetic examples.

Test trading-date boundaries, preview exclusion, missing versus zero, task count units, evidence/date links, stale summaries and permissions. Run existing portal suites and relevant approval/queue tests. Inspect desktop/mobile, keyboard focus, tables, disclosures and confirmation pages in the browser. Existing external write paths must stay untouched.

## Server release procedure

Read-only SSH inspection on 5 October verified server checkout `bd45831`; web, scheduler and akponora-ops are running. Container build revision is not yet verified. The server checkout has unrelated untracked operational files: preserve them and do not copy them into commits or build plans.

1. Finish and validate the local slice; review the preview with Marvin.
2. Record the approved commit, exact changed files and rollback image/commit.
3. Recheck server checkout, running-image revision, active jobs/locks, migrations, state mounts and available disk space. Do not expose environment secrets.
4. Obtain release approval for the concrete change. Automated production modes and QBO writes still require their specific authorization under AGENTS.md.
5. Build through the supported Windows workflow. Current handover notes say image builds over SSH fail because of the Windows credential store; verify rather than assume this has changed.
6. Deploy only necessary services outside active run windows. A portal-only change should not restart accounting workers merely for convenience. Apply migrations only if explicitly part of the reviewed release.
7. Verify sign-in, Home, both companies, Tasks, Deposits, run evidence and Schedules; verify automation remained operational. Keep rollback ready.

Never treat checkout revision as proof of running image revision. Do not rename code-scripts or move production state until a separate migration plan is reviewed.

## Current checkpoint

- Detailed UX audit exists at `outputs/portal_ux_review_2026-10-04/REVIEW.md` in the primary local checkout (gitignored review evidence).
- Isolated worktree: `/Users/marvinmokolo/.codex/worktrees/oiat-portal-ux/code-scripts`.
- Phase 1 first slice is complete locally: consistent trading-date blockers, missing-sheet comparisons, deposit counts and initial copy changes. All 542 portal tests pass; Home and Deposits were checked in the synthetic browser preview. See `PORTAL_UX_PHASE1_CHECKPOINT.md` in the implementation worktree. Later slices/phases remain planned.
- No production writes, deployments, DNS changes or new client-facing portals are authorized by this plan alone.

# Nora runtime efficiency — active checkpoint

Updated: 24 October 2026, 16:07 UTC. Owner requested implementation and production-host validation before
the next 17:00 UTC run. Branch: `codex/nora-run-efficiency`, base `7d3bf5b` (current GitHub master/production).

## Authorization and scope

- Implement the reviewed runtime improvements without changing accounting policy, caps, approvals,
  mappings, paid/unpaid treatment, deposit behavior, or stock-adjustment eligibility.
- No EPOS API. Use existing browser/report collection and QBO read queries.
- Owner confirms received POs cannot change; each later delivery needs a new PO. Still refresh a cached
  order when the live list fingerprint differs, and refuse incomplete identity evidence.
- Sheets remain staff's Daily Till Sales Breakdown, Bill payments and Invoice/Credit payments; real
  readings expected from November. Existing payment posting is already enabled. Practice in a copy.
- Do not infer repayments from EPOS credit limits; repayments stay outside daily sales totals.
- Production checkout and running code were current at `7d3bf5b`. The Mac was 77 commits behind and is now
  on the new branch from that master. Its pre-existing tracker edit is preserved in the named git stash;
  `.agents/`, `.playwright-mcp/` and `skills-lock.json` were pre-existing untracked files: do not commit.

## Delivery plan

- [x] Refresh Mac from master; preserve existing changes; record this checkpoint in AGENTS.md.
- [x] Group exact QBO sales item reads, retaining every existing validator and both fresh checks.
- [x] Reuse complete received-PO details inside the active bills window; retain live list discovery,
  fingerprint invalidation, held/capped bills, live QBO duplicate checks and existing cursor semantics.
- [x] Share receipt caches between catalogue and bills where freshness/identity permit.
- [x] Add useful capture/step timing metadata and test the affected contracts.
- [ ] Validate cached evidence and read-only QBO/browser behavior on the production host.
- [ ] Push branch and PR, deploy the validated branch without interrupting running jobs, verify services.
- [ ] Observe next scheduled run before claiming live acceptance; merge to master only when justified.

## Evidence and recovery

Gitignored investigation and implementation evidence: `outputs/run_performance_20261024/`.
No QBO exports, runtime state, live stock data, tokens or workbooks may be committed.
Initial production revision: `7d3bf5b6c5c8e2c844d7870588a42b56c4102548`.
Before replacing any image, save the old image under a timestamped rollback tag and record its ID here.
Do not recreate web/scheduler while any RunJob or global lock is active. Do not run duplicate posting
writers. If validation/deployment cannot safely finish before the scheduled run, leave the known-good
runtime intact, preserve the branch and explain exactly what remains.

## Current result

16:11 UTC: 240 focused tests pass, including the two-pass standing-approval flows, September history,
October Inventory/NonInventory validation, held/capped bills, mapping changes, shared-cache replay and
payment contracts. A 563-item regression proves 29 reads per pass (58 across both), with missing,
duplicate, wrong-identity/asset and partial responses refusing before any partial resolution is published.
All required live validation fields are queried with SELECT *; the legacy projected reader is unchanged.

Received-PO cache hits are copied into each run's evidence so offline replay remains complete. The live
list is still fetched; changed fingerprints, wrong IDs, missing Products and unreceived records cannot
use the received-order guarantee. Cache sharing is read-only between steps, each writing its own cache.
The accounting completion cursor and all unresolved plans remain unchanged: faster capture never marks
a held/capped bill complete or skips newly received old orders within the existing discovery window.

Stock-movement detail caching is not enabled without evidence that completed adjustments cannot change.
Existing movement coverage/classification/posting is retained; new per-subprocess timing separates the
snapshot from movement capture. Broader incremental discovery/cursor redesign and payment-ID/schema
changes are outside this timed release. No runtime changes or financial writes have occurred yet.

# Nora runtime efficiency — deployed checkpoint

Updated: 24 October 2026, 16:25 UTC. Owner requested implementation and production-host validation before
the next 17:00 UTC run. Implemented on `codex/nora-run-efficiency` from base `7d3bf5b`; merged through
PR #64 as `8632c766ede9ee1ee2869afaf2f6698e5219e7c6`. Mac and production checkouts now use `master`.

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
  updated master after the implementation branch was merged. Its pre-existing tracker edit is preserved in the named git stash;
  `.agents/`, `.playwright-mcp/` and `skills-lock.json` were pre-existing untracked files: do not commit.

## Delivery plan

- [x] Refresh Mac from master; preserve existing changes; record this checkpoint in AGENTS.md.
- [x] Group exact QBO sales item reads, retaining every existing validator and both fresh checks.
- [x] Reuse complete received-PO details inside the active bills window; retain live list discovery,
  fingerprint invalidation, held/capped bills, live QBO duplicate checks and existing cursor semantics.
- [x] Share receipt caches between catalogue and bills where freshness/identity permit.
- [x] Add useful capture/step timing metadata and test the affected contracts.
- [x] Validate cached evidence and read-only QBO/browser behavior on the production host.
- [x] Push branch and PR, deploy the validated branch without interrupting running jobs, verify services.
- [x] Merge validated PR #64 into master and return both Mac and production checkouts to master.
- [ ] Observe next scheduled run before claiming full live acceptance or a total runtime improvement.

## Evidence and recovery

Gitignored investigation and implementation evidence: `outputs/run_performance_20261024/`.
No QBO exports, runtime state, live stock data, tokens or workbooks may be committed.
Initial production revision: `7d3bf5b6c5c8e2c844d7870588a42b56c4102548`.
Rollback image saved as `oiat-portal:nora-before-efficiency-20261024`, image ID
`sha256:d32fcf4f3feba96ba86d2d847ad47bb045219d1d4236183705283498dda32757`.
Rollback: tag that image `oiat-portal:latest`, then recreate only web and scheduler while idle.
Do not recreate web/scheduler while any RunJob or global lock is active. Do not run duplicate posting
writers. If validation/deployment cannot safely finish before the scheduled run, leave the known-good
runtime intact, preserve the branch and explain exactly what remains.

## Current result

16:25 UTC: all 474 code_scripts tests pass locally AND in the final isolated, network-disabled
candidate image (12.686 seconds on the server). Coverage includes the two-pass standing-approval flows, September history,
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
changes are outside this timed release.

Production-host checks (GET/browser reads only; no financial writes):
- Fresh grouped QBO validation resolved 563 items (500 Inventory, 63 NonInventory) in 7.787 seconds,
  using 29 queries. Twenty records independently matched exact GETs for all required validator fields.
- Fresh EPOS browser capture fetched 331 list entries, selected 198 and reused complete matching
  saved evidence. No detail pages were needed; no selected details were missing. Capture took
  14.529 seconds. All source fields for 155 historical plan POs exactly match original raw evidence.
- Historical-plan summaries intentionally omit raw lines; compare raw evidence, not the summary,
  when proving line equivalence. Initial comparison was corrected for that format distinction.
- Live production caches were only read; validation used a temporary cache copy.
- PR #64 merged at 16:24 UTC: https://github.com/OIAT-Solutions/code-scripts/pull/64.
  Both checkouts returned to master; no second service restart was necessary because the merged
  functional code is identical to the validated running image. Follow-up changes are documentation only.
- Implementation `7c3d5aa6202d4f0f0219b570bf67351c3361480d` deployed at 16:22 UTC. Web and scheduler
  both run candidate image `sha256:6b8c6ecfdf2bc3bc00e1317faa27d311fb593355df20a9d8be9806df25cac1f2`.
  Portal HTTP 200; Django system checks clear; all four changed runtime sources match the Mac after
  normalizing Windows CRLF. Caddy and Cloudflare Tunnel were left running.
- Idle check immediately before activation: no queued/running jobs; global lock free. Next Nora
  schedule remains enabled at 17:00 UTC. Financial gate settings and runtime evidence were preserved.
- Follow-up hardens resumed evidence: incomplete records are removed from disk even without a
  persistent cache, so offline replay cannot revive invalid records. Regression covered locally.

No complete scheduled run has executed with the new image yet. Do not report live acceptance until
its outcome and per-step timing are checked. The read-only timings above are component measurements,
not a promised total daily runtime.


## Next scheduled run: verification still required

The next Nora routine is 24 October 2026, 17:00 UTC / 18:00 Lagos, expected business date 23 October.
Check the new RunJob and `/data/ops/company_a/daily/2026-10-23/run_*/progress.json`, then `summary.json`.
Inspect bills/catalogue `capture_metrics.json` and subprocess `timing.json` for cache hits, missing
selected details and grouped-read failures. Existing held/capped bills and incomplete staff sheets can
still produce REVIEW; do not raise caps or bypass holds to make the result look clean.
Compare the full elapsed time with the recent 52-minute average / 66m43s latest run, keeping workload
and existing review states visible. No full-run duration claim is validated by this deployment alone.
Stock movements still take the existing path; ask whether completed Stock Take/Wastage adjustments can
be edited before proposing immutable detail reuse there.

Recovery if needed: while no job is active, tag `oiat-portal:nora-before-efficiency-20261024` as
`oiat-portal:latest` and recreate only `web` and `scheduler` with `docker compose up -d --no-deps web scheduler`.
The old image is retained locally; do not prune it before the first normal run is accepted. Record the
rollback and do not restart containers during an active financial run. Checkout rollback alone does not
change a running container image.

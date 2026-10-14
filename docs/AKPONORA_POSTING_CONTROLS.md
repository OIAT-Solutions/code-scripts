# AKPONORA (Company A) sales posting controls

**Read [`AGENTS.md`](../AGENTS.md) first.** This page is the live contract for Company A sales posting from 1 Oct 2026: what the mapping must contain, what the uploader checks, how approvals and the standing auto-approval work, and what to do when a day is held. Day-to-day running: [`AKPONORA_DAILY_OPERATIONS.md`](AKPONORA_DAILY_OPERATIONS.md). Server env and hold clearing on the server: [`SERVER_SETUP.md`](SERVER_SETUP.md).

Production realm `9341455406194328`. The one-time cutover plan this page grew out of (W0–W8 order, create-night offset, staff inputs, rehearsal evidence) was removed on 5 Oct 2026 and is in git history as `docs/AKPONORA_CANONICAL_CUTOVER_RUNBOOK.md`.


## Runtime contract and mapping installation

- Approved Inventory rows require `Canonical Family Key`, `Canonical Unit` and `Staff Approved Purchase Multiplier`; every family must target one canonical Item Id/name/SKU/unit. Approved rows only. Lookup priority: Product ID, then SKU only when ID absent, then controlled unique exact name only when both absent. Barcode never identifies a mapping.
- Transform carries every source identity, source quantity, business date, mapping row and mapping SHA-256 in `_Conversion Proof`. Aggregation retains all proofs. Upload re-resolves them against the same file and recomputes quantities before any posting. This is an audit trail, not an authentication signature: operator raw-source reconciliation remains required.
- October Inventory targets require exact mapped QBO Item Id, Inventory type, AKP- SKU (not `AKP-NS-`), active quantity tracking, InvStartDate `2026-10-01`, AssetAccountRef `77`, and matching name. A different approved family's Id is not acceptable.
- October NonInventory targets (non-stock goods, see below) require exact mapped QBO Item Id, name, Type `NonInventory`, SKU `AKP-NS-...` and Active; no quantity tracking, asset account or InvStartDate checks apply.
- Company A cutoff cannot be moved later via configuration. Historical catch-all is exactly `15030`, NonInventory, before Oct 1. No date means blocked; source timestamps must fall within the declared Lagos trading day. Changed business date means regenerate from correctly split raw data. Start-date bypass is forbidden in conversion mode.
- Non-positive sales quantities: **before 1 Oct** (catch-all history) they post exactly as the pre-cutover code did (Qty coerced to 1, the refund's negative gross kept in `TaxInclusiveAmt`), so September days with refunds/0-qty lines still upload. **From 1 Oct** a non-positive or invalid quantity fails the whole business day before any POST, with the receipt and line named; October returns/zero lines need a separately reviewed workflow. A genuine `0` source quantity is valid input to the transform (only blank/NaN/Infinity is refused). Purchase quantity and unit cost must use the same canonical unit.
- CSVs transformed before `_Conversion Proof` existed may still be uploaded **only** when every line is dated before 1 Oct and already targets the catch-all; anything else must be regenerated from raw EPOS.
- Missing/changed mapping, missing proof, malformed numbers, conflicting identity and missing live items fail before a posting batch starts. QBO item lookup is by exact Id. No item creation, type repair, legacy renaming or quantity adjustment is part of sales.

### Non-stock products: NonInventory rule (accountant decision, 26 Sep 2026)

EPOS products that are **not stock-tracked in EPOS and have no stock master** (e.g. frozen food sold by kg, eggs, loose rice) map from 1 Oct to a dedicated **NonInventory** QBO item, one per EPOS product, SKU `AKP-NS-{EPOS ProductID}`. They do not enter Inventory and carry no QtyOnHand/asset value.

- From TxnDate 2026-10-01 an approved rule is valid only if it is `Inventory` with an `AKP-` SKU that does not start `AKP-NS-`, or `NonInventory` with an `AKP-NS-` SKU. Service, legacy SKUs, mismatched prefix/type pairs and the catch-all (`AKP-UNMAPPED-EPOS-SALES`, Id 15030) all fail closed (`october_target_error` in `code_scripts/product_conversion.py`, used by transform, upload contract and installer).
- Quantity is EPOS quantity x the approved `Staff Approved Sale Multiplier` (usually 1), exactly as for Inventory; no canonical family/unit/purchase multiplier is required.
- Upload re-reads the item by exact Id and checks Id/Name/Type/SKU/Active only.
- Create proposals: `code_scripts.cutover_drafts.noninventory_drafts` builds Name, Sku, Type `NonInventory`, IncomeAccountRef (400xxx) and ExpenseAccountRef (purchases 200xxx) by EPOS category; no asset/qty/start-date fields. Stock-tracked products and unknown categories are refused. Creating the items remains a separately approved QBO write, never the sales uploader.

Repository template: `templates/product_conversion_empty.csv` (header only, safe to track). Runtime config uses `state:mappings/company_a/approved.csv`, resolving beneath `STATE_ROOT` (locally `runtime`, container `/data`). The container entrypoint seeds the empty template **only if absent**, including on existing deployments with old persisted company JSON. It sets `COMPANY_A_PRODUCT_CONVERSION_FILE` unless explicitly overridden. Never silently fall back to an older map if a file disappears.

Install only an explicitly approved file:

```sh
python -m code_scripts.scripts.install_conversion_mapping \
  --source /secure/reviewed-approved.csv \
  --destination /data/mappings/company_a/approved.csv \
  --sha256 REVIEWED_SHA256 --approval-ref 'chat approval reference'
```

Installer validates rows, pins the digest, preserves versioned CSVs and an approval receipt, then replaces atomically. Both web and scheduler use the same persistent volume. Back up the mapping directory with the operational database; keep exports, approval files and live stock out of Git. Rebuild image, verify same mapping digest, run a dry-run and confirm no map was overwritten. The installer is an operator command, not a scheduled task.

## Explicit posting controls

### Pre-October vs October behaviour

"Controlled" = Company A receipt with TxnDate on/after `fail_closed_from` (never later than 2026-10-01; unparseable dates count as controlled).

| Behaviour | Pre-October (catch-all history, e.g. W8 16–30 Sep backfill) | October onward (controlled) |
|---|---|---|
| Approval manifest | Not required | Required for every payload (exact SHA-256) |
| Posting hold | Never written or checked | Written on any receipt failure or non-MATCH/NOT RUN reconcile; blocks later October posts |
| Existing DocNumber in QBO | Skipped harmlessly (as before) | Content verified exactly; mismatch fails the day |
| Existing DocNumber with other date | Warn and attempt (as before; QBO rejects a true duplicate) | Fails the day |
| One receipt fails | That receipt fails, others continue, run exits 1 (as before) | Preflight failure: nothing posted. POST failure: hold written, run stops |
| Qty ≤ 0 | Legacy coercion (Qty 1, negative gross kept) | Day refused before any POST |
| Proposal queue / Intuit `requestid` | Not used | Used |

### October approval flow

Company A October receipts require `COMPANY_A_POSTING_APPROVAL_FILE`: a restricted local JSON manifest containing `realm`, `entity: SalesReceipt`, `approved_by`, `chat_approval_ref`, timezone-aware future `expires_at`, and exact `payload_sha256` list. Never invent the human approval reference.

1. Dry run (no QBO writes; read-only item/receipt lookups):
   `python run_pipeline.py --company company_a --target-date 2026-10-01 --dry-run`
   (or `python code_scripts/qbo_upload.py --company company_a --target-date 2026-10-01 --dry-run` on an already-transformed CSV). This writes `STATE_ROOT/conversion_preflight/company_a_sales_batch_<date>.json` with every October payload and its hash, and `"complete": false` if any receipt failed preflight. `run_pipeline --dry-run` is single-day only and stops before reconcile/archive.
2. Compare the evidence with raw EPOS controls, then get the chat yes.
3. Build the manifest (refuses incomplete evidence; digests are recomputed from the payloads):
   `python -m code_scripts.operations_controls make-manifest --evidence STATE_ROOT/conversion_preflight/company_a_sales_batch_2026-10-01.json --out /secure/approval_2026-10-01.json --approved-by NAME --chat-ref 'chat reference' --expires-hours 24`
4. `export COMPANY_A_POSTING_APPROVAL_FILE=/secure/approval_2026-10-01.json` and run the day normally.

Conversion upload prebuilds every pending October payload and checks every approval before the first POST. A changed payload requires re-review.

Unattended runs replace steps 2–4 with automatic gates and an auto-built manifest when the owner's standing approval is configured; see "Unattended daily operation (standing approval)" below.

An approval is bound to one realm, entity, exact immutable payload digest(s), approver/reference and expiry. Changing any financial field invalidates it. After an uncertain response, query by stable identifiers; do not resubmit with a new idempotency key. Failed or ambiguous batches stop later posting; automatic deletion or compensation is forbidden.

### Retries and the proposal queue (October)

`requestid` = hash of realm, entity, DocNumber:TxnDate, payload digest and the count of confirmed failures. So:
- Transport error, 5xx, or 2xx without a receipt Id → outcome **UNKNOWN**; a same-payload retry reuses the same `requestid`, so QBO de-duplicates it ([Intuit request-id guidance](https://blogs.a.intuit.com/2018/09/10/quickbooks-online-api-best-practices/)).
- 4xx → **FAILED** (QBO rejected; nothing posted). The retry, or a corrected payload, gets a fresh `requestid`, so QBO does not replay the cached failure.
- A different payload for a DocNumber whose outcome is UNKNOWN/PROPOSED is refused. Confirm by live read that the receipt does **not** exist in QBO, then: `python -m code_scripts.operations_controls mark-failed --doc-number SR-... --txn-date 2026-10-01 --approved-by NAME --reason '...'`. A POSTED outcome never changes.

The local `ProposalQueue` (`STATE_ROOT/company_a_proposals.sqlite`) is an offline foundation: stable source identity, duplicate suppression and append-only-by-API audit. It cannot approve or post. HTTP success without an actual receipt Id is unknown and never marks the local upload ledger successful. A local manifest is not a multi-user authorization system; protect OS access. Full role-based approval UI and scheduled bank/AP integrations are future work.

### Unattended daily operation (standing approval)

The owner's standing approval replaces the per-day manifest **only** when every automatic gate passes. It is code in `code_scripts/standing_approval.py`, called from `run_pipeline.py`.

**Turn on** (server `.env`, which `docker-compose.yml` loads into both `web` and `scheduler` via `env_file`; then `docker compose up -d scheduler web`):

```sh
OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1
OIAT_COMPANY_A_STANDING_APPROVAL_REF=owner standing approval, 1 Oct 2026, <chat reference>
# optional: refuse any day whose EPOS gross is above this (naira)
OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS=15000000
```

Both of the first two must be set. Either missing means manual mode, exactly as before. `OIAT_COMPANY_A_STANDING_APPROVAL_REF` is deliberately not listed under the scheduler's `environment:` block, so a compose interpolation can never blank it. An invalid cap fails closed (hold written). The mode applies to every `run_pipeline` invocation that has both vars: scheduler runs, portal "Run now", and a CLI run on a machine whose `.env` has them. It applies only to Company A, conversion on, business date on/after 1 Oct (each day of a range). Explicit `--dry-run` runs never post. Other companies and pre-October days are unchanged. With the mode on, `run_all_companies` runs Company A last, so a refused Company A day cannot stop other companies in a sequential stop-on-failure run.

**Per business day:** download → split → transform as normal. Then `qbo_upload --dry-run` (any `COMPANY_A_POSTING_APPROVAL_FILE` removed for that subprocess) writes `STATE_ROOT/conversion_preflight/company_a_sales_batch_<date>.json`. The gates, all evaluated and all reported:

| Gate | Passes when |
| --- | --- |
| `evidence_complete` | Dry-run exited 0. The evidence is fresh (written by this run), Company A realm, `SalesReceipt`, target date = the day, and `complete: true`. Every payload digest recomputes, TxnDate = the day, Qty > 0. Every line's ItemRef is an Item Id in the installed approved mapping whose rule passes the October contract (Inventory `AKP-` / NonInventory `AKP-NS-`; never `15030` or legacy). |
| `totals_match_epos` | Gross of the payloads to post, plus receipts already verified in QBO, = EPOS raw `TOTAL Sales` of the split raw file for the day (±₦1). Receipt count = transformed CSV receipts; line count = transformed CSV rows. |
| `no_posting_hold` | `STATE_ROOT/company_a_posting_hold.json` does not exist. |
| `mapping_sha_matches` | Mapping SHA-256 recorded in the evidence = installed mapping file SHA = every `_Conversion Proof` SHA in the CSV. |
| `max_gross_cap` | Only if the cap is set: the day's EPOS raw gross ≤ the cap. |

If all pass, the run builds the manifest with the same `build_posting_manifest` used by `make-manifest`: `approved_by: auto:scheduler`, `chat_approval_ref` = the standing ref, expiry 6 h, exact payload digests. It is written to `STATE_ROOT/approvals/company_a_auto_<date>_<timestamp>.json` (file 0600, directory 0700). `qbo_upload` then posts with `COMPANY_A_POSTING_APPROVAL_FILE` set **for that subprocess only**. The upload rebuilds every payload and checks each digest again, so anything that changed since the dry-run is refused with nothing posted. Then reconcile and archive as normal: a non-MATCH reconcile writes the hold, exactly as in manual mode. A day already fully in QBO (rerun) passes the gates with zero payloads to approve. No manifest is written, existing receipts are content-verified and skipped, nothing is posted, and the run succeeds.

**Failure:** nothing is posted. The hold is written with `source: standing_auto_approval`, `result.status: AUTO_APPROVAL_REFUSED`, the failed gates and a summary (gross to post, gross already in QBO, EPOS gross, mapping SHA). An existing hold is never overwritten. The run exits 1 and the normal Slack failure notification carries the reason, e.g. `Company A standing auto-approval refused 2026-10-02; nothing posted. Failed gate(s): [totals_match_epos] payload gross ₦… != EPOS raw gross ₦…`. A failed preflight (unmapped line, missing item, refund line, QBO read error) counts as `evidence_complete`. Download/split failures happen before the gates; they post nothing and do not write the hold (the next run retries).

**Clearing:** `python -m code_scripts.operations_controls show-hold`; fix the cause (mapping rebuild + install, EPOS late data, etc.); confirm in QBO what exists for the day; then `clear-hold --approved-by NAME --reason '...'`. The next scheduled run, or `python run_pipeline.py --company company_a --target-date <day>`, re-evaluates the gates from scratch. Days are independent: a refused day does not stop later days except through the hold. Once the hold is cleared, each missed day must be re-run explicitly (the scheduler only runs "yesterday"). **Turn off** by unsetting `OIAT_COMPANY_A_STANDING_APPROVAL_REF` (back to manual manifests) or `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED` (Company A out of schedules).

**Timing:** Company A sales run inside `daily_run`, started by the portal schedule worker from the "Nora daily routine" schedule at 18:00 Africa/Lagos (17:00 UTC); see [`SCHEDULING_AUTHORITY.md`](SCHEDULING_AUTHORITY.md). The job's target date is `get_target_trading_date`: the Lagos date minus 1, or minus 2 before the 05:00 cutoff. A run at 18:00 Lagos on **2 Oct** therefore posts business day **1 Oct** (05:00 1 Oct → 05:00 2 Oct), which closed 13 hours earlier. A manual run before 05:00 Lagos gets the day before that. CLI runs without `--target-date` use the machine's local "yesterday", so always pass `--target-date` by hand.

### October onward

QBO perpetual FIFO COGS is primary. Item-based purchases/credits use the same canonical units and purchase evidence. Month-end compares new-item subledger, GL, physical count and verified costs; post **only verified variance** after approval. Do not repeat a full historical opening+purchases−closing COGS journal on top of FIFO. See [Intuit inventory workflow](https://developers.intuit.com/app/developer/qbo/docs/learn/learn-basic-bookkeeping/manage-inventory).

## Rollback / incident response

For an **October-or-later** business date, a failed or not-run Company A reconciliation, any failed/uncertain receipt POST, or (standing approval on) any failed automatic gate, writes `STATE_ROOT/company_a_posting_hold.json` (before archiving). Later October posts refuse while that hold exists; a subsequent MATCH does not clear it automatically. Pre-October days never write or check the hold. Reconcile the evidence and obtain approval, then clear it (the file is archived next to it with who/why, never deleted):

```sh
python -m code_scripts.operations_controls show-hold
python -m code_scripts.operations_controls clear-hold --approved-by NAME --reason 'reconciled: ...'
```

Pause Company A schedule on any unresolved mapping, unexpected item identity/date/account, negative new stock, VAT mismatch, duplicate document, failed request or unexplained IA variance. Retain raw sources, payload hashes, mapping versions, QBO request/response IDs and last successful source key. Do not disable conversion or restore legacy mapping. Do not delete receipts/items or reverse financial entries automatically. Determine which writes succeeded by live read, reconcile, prepare an exact correction and obtain chat approval. Revert code/map only to a known compatible version while posting stays off; a prior empty map means fail-closed, not catch-all in October. A mapping change invalidates transformed CSVs and payload approvals: regenerate from raw sources.

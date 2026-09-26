# AKPONORA canonical Inventory cutover

Updated 19 September 2026. Owner: Marvin / explicitly assigned operator. Production realm `9341455406194328`. Follow AGENTS.md safety gates. This is a preparation runbook, not approval to post or activate.

## Current status and handoff

- Originally prepared in `/Users/marvinmokolo/.codex/worktrees/d618/code-scripts` (branch `codex/akponora-canonical-cutover`, based on commit `a9413d3cc045a436044a854e81235da408cafa27`). Brought over with the 25 Sep code-review fixes on branch `claude/nora-cutover-fixes`; see "Pre-October vs October behaviour" below.
- PR 62 was incorrectly based on `main`. Changed its base to `master` (actual parent `0cdeb40`), removing unrelated portal/website history. Existing untracked root-level investigation scripts are preserved. Local new fixes are not pushed, committed, merged or deployed.
- Live read on 18 Sep: active Inventory **4,316**, Inventory Asset account **77 = ₦142,028,049.94**. Evidence: `outputs/akponora_canonical_2026-09-18/live_read_check.json`. Access used an existing token read-only; no refresh or financial write.
- Final validation is recorded in the verification section below.
- Pipeline/server off is inherited operational status, not independently re-proved on the server in this task. No server start or production write was performed.

## Catalogue correction: supersedes W2's 5,664-create claim

The old W2 builder proposes an Inventory item for each EPOS row and copies that row's full-unit cost, while sales can multiply quantity. Do not post its `w4_dryrun_create_payloads_qty0.*`. They are preserved historical evidence, not an approved create list.

A wine bottle is quantity 1. A crate containing 24 bottles maps to **the same** bottle Item Id with sale multiplier 24. A purchase of two crates is 48 bottles; crate cost divided by 24 is bottle cost. Do not track both the full crate and the bottles as independent stock. The canonical unit is the smallest separately sold and counted unit, not necessarily the smallest piece inside a sealed retail package.

Rebuild from preserved 16 Sep conversion CSV, 18 Sep QBO dump and June–15 Sep sales detail. Sales evidence adds **930** unique Product IDs (**3,416** rows now have IDs) and exposes **38 row conflicts across 19 families**. Conflicts remain blocked; candidate IDs are retained for staff review. The source feed does contain `ProductId`, superseding the older assumption that it is name-only. Numeric IDs promoted by CSV loading to strings such as `1631625.0` are normalized to the same integer identity, never matched by barcode.

Counts:

| Measure | Count |
|---|---:|
| EPOS rows preserved | 6,038 |
| Candidate families | 4,339 |
| Structurally eligible technical rehearsal stubs | 3,621 |
| Families blocked for structural review | 718 |
| Families without exactly one tracked stock owner | 546 |
| Families with source-review blocks | 202 |
| Families with Product ID conflicts | 19 |
| Families with duplicate EPOS names | 20 |
| Missing-cost flags | 217 |
| Negative-stock zero-floor flags | 162 |
| Candidate-name collisions in available live dump | 3,927 |
| Approved production mappings / create payloads | **0 / 0** |

Issue counts overlap. Candidate families come from the earlier `Family Candidate`, itself a proposal; they do not prove master/child identity. Stock conversion uses the explicit EPOS Volume Denominator where available. Missing denominator yields a single-unit **proposal**, never approval. Multiple stock owners are quarantined rather than summed. Partial candidate gross-cost valuation is **₦129,778,180.8251361**, excluding structurally blocked families; it is not a complete stock valuation, not a ledger target, and not a journal amount. Cost tax basis remains unverified.

Artifacts are in `outputs/akponora_canonical_2026-09-18/` (ignored): `canonical_families.csv`, `product_conversion_review.csv`, `legacy_rename_proposals.csv`, `catalogue_rehearsal.jsonl`, `summary.json`. `staff_questions.csv` contains 1,011 affected-family question sets, with 709 on-till structural reviews prioritized first. The rehearsal stubs deliberately omit unverified posting accounts/tax/cost fields and are not production API payloads.

### Staff decisions now

For every sellable variant in a multi-row or uncertain family, capture:
1. EPOS Product ID and master Product ID (actual configuration, not barcode).
2. What one till button physically sells; canonical units consumed per sale.
3. Which single row owns stock; whether child counts duplicate the master's count.
4. Canonical unit; full-unit and loose-volume conversion; purchase-unit conversion.
5. Supplier invoice supporting cost, discount/freight allocation and VAT treatment.
6. Keep, duplicate, or stop-selling decision; test-sale result showing master deduction.

For simple single-row products, approve the reviewed identity/unit list as a batch. Do not require retyping thousands of names. Unresolved till products block the day until resolved or explicitly removed from till availability by staff. The old 237 review rows are not the complete canonical ownership exception set.

### Final inputs on 30 September (short staff request)

After the last sale in the agreed Lagos trading day, send **one dated pack**:
- Product List with Product IDs, master/child links, Volume of Sale, child Master Product Amount, till status and SKU.
- Stock Levels/count at that exact cutoff: full packs, loose units, negatives, damage/expiry and confirmed costs. Include the last sale/stock-movement timestamp.
- Detailed sales, refunds/voids, tender and VAT totals for 16–30 Sep, with source transaction/line IDs if available.
- Goods received, supplier invoices/credits and stock movements since 16 Sep; separate received goods from unreceived orders.
- Completed unit/stock-owner exception decisions and cost evidence.

Bookkeeper separately supplies QBO inventory valuation, Balance Sheet, P&L, inventory/COGS transaction detail, AP ageing, supplier balances, VAT detail, and bank/Moniepoint statements with settlement references through the same cutoff. Statements are required to infer payments or bank movement.

## Runtime contract and mapping installation

- Approved Inventory rows require `Canonical Family Key`, `Canonical Unit` and `Staff Approved Purchase Multiplier`; every family must target one canonical Item Id/name/SKU/unit. Approved rows only. Lookup priority: Product ID, then SKU only when ID absent, then controlled unique exact name only when both absent. Barcode never identifies a mapping.
- Transform carries every source identity, source quantity, business date, mapping row and mapping SHA-256 in `_Conversion Proof`. Aggregation retains all proofs. Upload re-resolves them against the same file and recomputes quantities before any posting. This is an audit trail, not an authentication signature: operator raw-source reconciliation remains required.
- October requires exact mapped QBO Item Id, Inventory type, AKP- SKU, active quantity tracking, InvStartDate `2026-10-01`, AssetAccountRef `77`, and matching name. A different approved family's Id is not acceptable.
- Company A cutoff cannot be moved later via configuration. Historical catch-all is exactly `15030`, NonInventory, before Oct 1. No date means blocked; source timestamps must fall within the declared Lagos trading day. Changed business date means regenerate from correctly split raw data. Start-date bypass is forbidden in conversion mode.
- Non-positive sales quantities: **before 1 Oct** (catch-all history) they post exactly as the pre-cutover code did (Qty coerced to 1, the refund's negative gross kept in `TaxInclusiveAmt`), so September days with refunds/0-qty lines still upload. **From 1 Oct** a non-positive or invalid quantity fails the whole business day before any POST, with the receipt and line named; October returns/zero lines need a separately reviewed workflow. A genuine `0` source quantity is valid input to the transform (only blank/NaN/Infinity is refused). Purchase quantity and unit cost must use the same canonical unit.
- CSVs transformed before `_Conversion Proof` existed may still be uploaded **only** when every line is dated before 1 Oct and already targets the catch-all; anything else must be regenerated from raw EPOS.
- Missing/changed mapping, missing proof, malformed numbers, conflicting identity and missing live items fail before a posting batch starts. QBO item lookup is by exact Id. No item creation, type repair, legacy renaming or quantity adjustment is part of sales.

Repository template: `templates/product_conversion_empty.csv` (header only, safe to track). Runtime config uses `state:mappings/company_a/approved.csv`, resolving beneath `STATE_ROOT` (locally `runtime`, container `/data`). The container entrypoint seeds the empty template **only if absent**, including on existing deployments with old persisted company JSON. It sets `COMPANY_A_PRODUCT_CONVERSION_FILE` unless explicitly overridden. Never silently fall back to an older map if a file disappears.

Install only an explicitly approved file after W7 returns new IDs:

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

Offline check on archived 16 and 23 Sep: fixed-code payloads are byte-identical to the `a9413d3` payloads (all lines → Id `15030`, gross equals the archived EPOS reconcile total).

### October approval flow

Company A October receipts require `COMPANY_A_POSTING_APPROVAL_FILE`: a restricted local JSON manifest containing `realm`, `entity: SalesReceipt`, `approved_by`, `chat_approval_ref`, timezone-aware future `expires_at`, and exact `payload_sha256` list. No approved manifest has been created. Do not invent the human approval reference.

1. Dry run (no QBO writes; read-only item/receipt lookups):
   `python run_pipeline.py --company company_a --target-date 2026-10-01 --dry-run`
   (or `python code_scripts/qbo_upload.py --company company_a --target-date 2026-10-01 --dry-run` on an already-transformed CSV). This writes `STATE_ROOT/conversion_preflight/company_a_sales_batch_<date>.json` with every October payload and its hash, and `"complete": false` if any receipt failed preflight. `run_pipeline --dry-run` is single-day only and stops before reconcile/archive.
2. Compare the evidence with raw EPOS controls, then get the chat yes.
3. Build the manifest (refuses incomplete evidence; digests are recomputed from the payloads):
   `python -m code_scripts.operations_controls make-manifest --evidence STATE_ROOT/conversion_preflight/company_a_sales_batch_2026-10-01.json --out /secure/approval_2026-10-01.json --approved-by NAME --chat-ref 'chat reference' --expires-hours 24`
4. `export COMPANY_A_POSTING_APPROVAL_FILE=/secure/approval_2026-10-01.json` and run the day normally.

Conversion upload prebuilds every pending October payload and checks every approval before the first POST. A changed payload requires re-review.

### Retries and the proposal queue (October)

`requestid` = hash of realm, entity, DocNumber:TxnDate, payload digest and the count of confirmed failures. So:
- Transport error, 5xx, or 2xx without a receipt Id → outcome **UNKNOWN**; a same-payload retry reuses the same `requestid`, so QBO de-duplicates it ([Intuit request-id guidance](https://blogs.a.intuit.com/2018/09/10/quickbooks-online-api-best-practices/)).
- 4xx → **FAILED** (QBO rejected; nothing posted). The retry, or a corrected payload, gets a fresh `requestid`, so QBO does not replay the cached failure.
- A different payload for a DocNumber whose outcome is UNKNOWN/PROPOSED is refused. Confirm by live read that the receipt does **not** exist in QBO, then: `python -m code_scripts.operations_controls mark-failed --doc-number SR-... --txn-date 2026-10-01 --approved-by NAME --reason '...'`. A POSTED outcome never changes.

The local `ProposalQueue` (`STATE_ROOT/company_a_proposals.sqlite`) is an offline foundation: stable source identity, duplicate suppression and append-only-by-API audit. It cannot approve or post. HTTP success without an actual receipt Id is unknown and never marks the local upload ledger successful. A local manifest is not a multi-user authorization system; protect OS access. Full role-based approval UI and scheduled bank/AP integrations are future work.

### Scheduler

Until W9, `OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED` is unset/0: every all-company schedule (system fallback and user-created) passes `--exclude-company company_a`, and any single-company Company A sales schedule is skipped with a recorded reason. Set it to `1` only after the W9 chat yes.

## Cutover order and acceptance

1. **W0 freeze now:** human sends/enforces existing freeze note. Capture acknowledgement. No new QBO Inventory, no pre-cutover inventory-product bills or bills to 120000. Existing specified purchase expense accounts remain until cutover. Verify no new item or account refill drift daily by read-only checks.
2. **W2/W3:** approve canonical family/owner/sale/purchase relationships, aliases and tax/cost basis. Preserve staff-facing canonical names. Refresh collisions against all QBO item types and proposed LEGACY names. Resolve duplicates, length limits and existing LEGACY-name collisions; never truncate blindly.
3. **W5 rename proposal:** exact current Id/SyncToken/name, proposed unique LEGACY name, before/after type/quantity/value invariant, full dry-run and chat yes. Rename only. No bulk inactivation.
4. **W6 cutoff:** capture exact 30 Sep pack and statement sources with hashes, timestamps and row counts. Reconcile till totals, refund/voids and late sales around 05:00 Lagos. No arbitrary midnight split.
5. **W8 September:** confirm existing QBO daily receipts before backfill. Backfill only verified missing sales for 16–30 Sep to 15030, once, after approval. Same DocNumber alone is not proof of matching content: compare gross/net/VAT, date, tender, line count and source IDs. Reconcile partial/uncertain retries before continuing.
6. **Sep close:** Jan–Aug historical COGS corrections stand, subject to bank/AP/VAT reconciliation. For Sep, verify opening and closing values on consistent cost/tax basis; when purchases already sit on 200xxx, proposed journal is stock movement only, `closing - opening`: positive = debit IA 77 / credit COGS 76, negative reverses. Do not post purchases twice. Chat yes before posting. Resolve legacy asset subaccount refill balances separately with evidence.
7. **W7 create:** one new Inventory item per approved canonical family, unique AKP SKU, exact category income/COGS accounts and tax codes, IA 77. Opening canonical qty from final count, zero-floor negative physical counts with disclosed exceptions, missing cost 0 explicitly flagged. Validate all fields and source evidence before create approval. Create receipt register maps family→new Id→SyncToken. Retry only missing records after querying QBO; no duplicate names or January qty-10 importer.
8. **Same create-night IA offset:** see procedure below. Require separately approved journal and verify posted GL.
9. **Install new-ID mapping**, pin SHA, deploy after approval; verify both processes can read identical map. Run complete representative October day with zero unresolved lines and no writes. Include each, crate, nested pack, fractional loose qty and refund exception. Verify gross/net/VAT and counts, no legacy ItemRefs and no catch-all.
10. **W9 chat yes:** enable only the agreed Company A sales schedule, not legacy remediation jobs. Process one approved batch; confirm exact receipt Ids, amounts, item quantities, FIFO COGS and IA. Retry identical batch and verify zero duplicates. Confirm purchases/receipts precede sales chronologically. Only then release routine operation.
11. **W10 after stable operation:** separate sandbox/lab, then small Z0 batches with full valuation proof. Qty-zero does not prove FIFO value zero. Any legacy quantity-to-zero adjustment offsets 300150 only, individually approved batch; then separately approved inactivation. Never automated bulk inactivation or shrinkage cleanup.

### Create-night offset calculation

[QBO opening inventory uses quantity × cost at InvStartDate](https://static.developer.intuit.com/sdkdocs/qbv3doc/ippdotnetdevkitv3/html/9e3dd37d-e874-156d-fc39-6d4a05942eb7.htm). Never reuse the 16 Sep amount as the future offset.

Let `B` be IA 77 immediately before creates, `C` the **verified actual** IA increase from creates, and `V` the approved 30 Sep canonical opening valuation on the same cost/tax basis. Proposed credit IA = `B + C - V`; debit 300150 (Id 86) by the same amount. If negative, reverse debit/credit. If C equals V, offset equals B, not V. Inspect QBO-created opening equity entries and subledger as well as the GL. Snapshot all three numbers and source reports; reconcile post-create IA to B+C and post-offset IA to V. Stop if any unexplained difference or concurrent movement exists. Chat yes is required for the exact dated journal. `create_night_offset` computes a proposal only.

### October onward

QBO perpetual FIFO COGS is primary. Item-based purchases/credits use the same canonical units and purchase evidence. Month-end compares new-item subledger, GL, physical count and verified costs; post **only verified variance** after approval. Do not repeat a full historical opening+purchases−closing COGS journal on top of FIFO. See [Intuit inventory workflow](https://developers.intuit.com/app/developer/qbo/docs/learn/learn-basic-bookkeeping/manage-inventory).

## Rollback / incident response

For an **October-or-later** business date, a failed or not-run Company A reconciliation, or any failed/uncertain receipt POST, writes `STATE_ROOT/company_a_posting_hold.json` (before archiving). Later October posts refuse while that hold exists; a subsequent MATCH does not clear it automatically. Pre-October days never write or check the hold. Reconcile the evidence and obtain approval, then clear it (the file is archived next to it with who/why, never deleted):

```sh
python -m code_scripts.operations_controls show-hold
python -m code_scripts.operations_controls clear-hold --approved-by NAME --reason 'reconciled: ...'
```

Pause Company A schedule on any unresolved mapping, unexpected item identity/date/account, negative new stock, VAT mismatch, duplicate document, failed request or unexplained IA variance. Retain raw sources, payload hashes, mapping versions, QBO request/response IDs and last successful source key. Do not disable conversion or restore legacy mapping. Do not delete receipts/items or reverse financial entries automatically. Determine which writes succeeded by live read, reconcile, prepare an exact correction and obtain chat approval. Revert code/map only to a known compatible version while posting stays off; a prior empty map means fail-closed, not catch-all in October. A mapping change invalidates transformed CSVs and payload approvals: regenerate from raw sources.

## Remaining acceptance evidence

A failed/uncertain October Company A posting stops the remaining batch and sets the persistent reconciliation hold; it does not proceed to the next receipt. Pre-October receipts keep the legacy per-receipt behaviour.

No final production mapping, approved canonical units, final 30 Sep quantities, approved tax/cost basis, W5/W7 writes, deployment proof, authenticated server-state check or post-cutover acceptance exists yet. Continue offline preparation; do not describe the system as ready with only calendar waiting remaining. Staff relationship decisions and approvals are genuine external prerequisites.

## Verification and reproducible preparation

- After the 25 Sep review fixes: pipeline suite **342 tests passed**, portal suite (`apps.epos_qbo apps.dashboard apps.core`) **398 tests passed**. Regression tests: `code_scripts/tests/test_cutover_review_fixes.py`, `apps/epos_qbo/tests/test_schedule_worker.py`. No test invoked real QBO posting.
- Original Codex figures (pre-fix): pipeline 319; evidence and hashes in `outputs/akponora_canonical_2026-09-18/verification.json`.
- `compileall`, Django system check and shell syntax check completed. Git diff whitespace check passed.
- Real **15 Sep** offline rehearsal: 1,266 source lines → 771 transformed lines / 7 receipt groups. Source and output agree to the penny: **gross ₦3,607,420.00; net ₦3,357,065.27; VAT ₦250,354.73**. Source retains fractional kobo (`.0002`) beyond display precision; float aggregation differences below 1e-8 are recorded, not concealed. Zero non-positive transformed lines. This is already-posted history used for rehearsal, not backfill authorization.
- Same product set under a synthetic October business date fails closed on all 1,266 lines with the empty approved map. This is safety proof, not October acceptance.
- Workbook formula counts verified; sample staff input changes the count, then is restored. Every tab visually reviewed. CSVs retain all 6,038 source rows. No staff approval was invented. `AKPONORA_Canonical_Product_Review.xlsx` contains the editable family, sale-unit and legacy-name review tables.
- Legacy W2 builder and external MISC calendar were reviewed, including create-night offset and W10. External calendar text suggesting immediate delete/reverse after a bad adjustment is superseded: stop, read/reconcile, prepare exact correction and obtain explicit chat approval. No automatic reversal/deletion.

Rebuild review CSVs (read-only inputs, no QBO writes):

```sh
python -m code_scripts.scripts.akponora_canonical_catalog \
  --conversion /secure/akponora_product_conversion.csv \
  --qbo-items /secure/w1_qbo_items_live.csv \
  --sales /secure/BookKeeping_June_to_15Sep.csv \
  --out outputs/akponora_canonical_refresh
```

Use `cutover_drafts.catalogue_drafts` for approved family decisions + final dated full/loose counts: duplicate family/name/SKU/stock-owner and extra/unmapped count owners fail closed. Quantity uses explicit full and loose factors; cost is already per canonical unit with verified tax basis. Negative counts floor to 0 and missing-cost positive stock remains visibly flagged. `bill_draft` converts approved purchase units and verifies exact live Inventory identity; it returns only an unapproved proposal requiring invoice, GRN, duplicate/vendor/tax reconciliation and chat yes.

No financial or personal data is committed or placed in the deploy image: root outputs/downloads/exports, databases and workbooks are excluded by `.dockerignore` as well as Git rules. The empty mapping template is the only new tracked CSV candidate.

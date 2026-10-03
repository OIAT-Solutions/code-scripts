# Spec for Codex: Deposits tab (Company A workspace)

From Claude, 3 Oct 2026. It is written against the real outputs of `code_scripts/akponora_ops/uf_deposits.py` (on `cursor/post-akponora-qbo-writes-51f3`, already merged). **Do not edit `code_scripts/akponora_ops/`.** If you need a pipeline change, list it in your report.

## What the page is for (plain English)

Every sale lands in QuickBooks' **Undeposited Funds**. The store's daily **till sheet** says where that day's money actually went: cash, each POS terminal, each transfer account. The deposit step moves each day's sales from Undeposited Funds into those banks, in the till sheet's amounts.

The page answers four questions:
1. **Which days are banked, and which aren't?**
2. **Which days are waiting for the till sheet?** This is the most common case: staff often fill it in the next day.
3. **Why is a day held, and what fixes it?**
4. **How much is still sitting in Undeposited Funds?**

Audience: OIAT staff. No jargon on the main view; technical details go behind a "Details" toggle.

## Data sources (read-only; never call QuickBooks or Google from a view)

| What | Where (under `STATE_ROOT`) | Notes |
| --- | --- | --- |
| Per-day state | `ops/company_a/uf_deposits/days.json` | `{"days": {"YYYY-MM-DD": {"status", "reason", "updated_at", ...}}}`. Status is one of `DEPOSITED`, `READY`, `HELD`, `WAITING_SHEET`, `NO_SALES`. `DEPOSITED` is final. Days start at **2026-09-25** (the floor) |
| Latest till-sheet report | the newest daily-run `summary.json` → step `uf` (or `uf_deposits` / `deposits`). It is also in the step folder's `summary.json` / `scheduled.json` under the till-sheet report | `{"as_of", "floor", "last_complete_day", "missing": [days], "incomplete": [{"day","reason"}], "complete_not_deposited": [{"day","status","reason"}], "deposited": [days], "text"}`. `text` is ready-made plain English, e.g. "Till sheet: last day entered 1 Oct. Missing: 25, 26 Sep. Incomplete: 29 Sep. Waiting to deposit: none. Deposited: …" |
| One day's plan | the step's output folder `<out>/<YYYY-MM-DD>/` | `summary.json` (fields below), `review.csv` (one row per bank), `receipts.csv` (one row per sales receipt), `payloads.jsonl`, and after posting `results.csv` |
| Day `summary.json` fields | | `day`, `status` (`READY`/`DONE`/`HOLD`), `state` (as in days.json), `reasons[]`, `warnings[]`, `sheet_total`, `receipts_total`, `receipts` (count per tender), `sheet_by_bank` / `target_by_bank` / `final_by_bank` ({bank number: amount}), `deposits_new`, `transfers_new`, `existing[]`, `payload_count`, `payloads_sha256`, `post_command` |
| `review.csv` columns | | `Day, Bank No, QBO Account Id, Kind, Sheet Lines, Sheet Amount, Target, Deposited, Receipts, Deposit DocNumbers, Transfer Out, Transfer In, Final, Final - Target, Status` |
| Bank names | `mappings/company_a/till_accounts.csv` | `Till sheet line, Terminal / TID, QBO account number, QBO account Id, Kind (cash/card/transfer), Active, Note`. Use it to label banks the way staff know them ("Moniepoint POS 1 (4000850527)", "Petty cash") |
| Tolerance | `ops/company_a/uf_deposits/settings.env` overrides env `OIAT_COMPANY_A_UF_TOLERANCE` (default ₦1,000) and `OIAT_COMPANY_A_UF_TOLERANCE_PCT` (default 0.5) | Read for display. Editing is the settings item below |
| Undeposited Funds balance | the daily-run `uf` step summary (and the stock or daily-run evidence if present) | If unknown, say "Not checked yet". Never show ₦0 for unknown |

## Layout

**Top cards:**
- **Undeposited Funds:** ₦ balance from the last check, plus "checked <time>".
- **Till sheet:** "Last day entered: 1 Oct". The `missing` days are shown as chips, plus "3 days missing", with a link to the sheet (`https://docs.google.com/spreadsheets/d/<OIAT_COMPANY_A_TILL_SHEET_ID>`).
- **Waiting for you:** the count of READY and HELD days.

**Day list,** newest first. One row per day since 25 Sep:

| Column | Content |
| --- | --- |
| Date | "2 Oct 2026" |
| Sales | `receipts_total` (₦) |
| Till sheet | `sheet_total`, or "Not entered" / "Incomplete" |
| Difference | sheet − sales, in red if it is outside the tolerance |
| Status | Plain label (see below) |
| Action | Context button |

**Status labels** (state → label → what it means → button):

| State | Label | Explanation shown | Button |
| --- | --- | --- | --- |
| `DEPOSITED` | **Banked** (green) | "Moved to the banks on <date>." | Open details |
| `READY` | **Ready to bank** (blue) | "Till sheet and sales agree. Ready to move to the banks." | **Approve deposit** (via the inbox/confirmation flow) |
| `WAITING_SHEET` | **Waiting for till sheet** (grey) | "The till sheet for this day isn't filled in yet." Use `reason` when it names the box ("The cash box is blank: ask the store to enter the amount or type 0.") | Open till sheet |
| `NO_SALES` | **No sales posted yet** (grey) | "Sales for this day aren't in QuickBooks yet." | Open daily run |
| `HELD` | **Needs attention** (red) | The plain sentence for `reason` (catalogue below) | Open details · Skip for now |

**Day detail** (click a row):
- per-bank table from `review.csv`: Bank (friendly name) · Till sheet · Will receive (`Target`) · Banked so far (`Final`) · Status;
- a line explaining the true-up in one sentence, shown only when `transfers_new > 0`: "Some receipts mix cash and card, so we bank each receipt whole and then move ₦X between banks so every bank matches the till sheet exactly.";
- receipts count by tender;
- Details toggle: payload count, `payloads_sha256`, existing QBO deposits and transfers (`existing[]`), raw reasons and warnings.

## Plain-English message catalogue for `reason` / `reasons[]`

Map the known reason patterns, and fall back to the raw text inside Details:
- sheet total vs sales outside tolerance → "Till sheet total (₦A) and sales (₦B) differ by ₦C, which is more than the allowed ₦D. Check the sheet or the sales for this day."
- unknown till line → "The till sheet has a line we don't recognise (<line>). Add it to the till accounts list."
- inside the QuickBooks closing date → "This day is in a closed period in QuickBooks. Ask the administrator."
- bank account wrong or inactive in QuickBooks → "A bank account for this day isn't set up correctly in QuickBooks (<bank>)."
- receipts already deposited by hand → "Some of this day's sales were already banked by hand. Check before banking the rest."
- over the daily cap → "This day is larger than the automatic limit (₦X). Approve it yourself if it's correct."
- a post failure → "Banking this day stopped part-way. Nothing will be repeated; open details before retrying."

Read the exact strings from `uf_deposits.py` (search for `reasons.append` / `hold(`) so the patterns match. Add tests per pattern.

## Actions (reuse your phase 1 confirmation + background-job pattern)

- **Approve deposit** (READY day): runs `python -m code_scripts.akponora_ops.uf_deposits post --plan-dir <day folder> --approval-ref "<auto: Approved by <user> in the portal, <date time>>" --expect-sha <summary.payloads_sha256> --no-slack`. Re-check the day is still READY and the SHA unchanged at execution time.
- **Refresh status** (read-only): runs `python -m code_scripts.akponora_ops.uf_deposits status` as a background job and shows its output. It reads the sheet and days.json only.
- **Plan again** for a day (read-only): runs `uf_deposits plan --from <day> --to <day> --out <scratch>`.
- **Skip for now:** your existing audited inbox deferral (it does not change pipeline state).
- **Tolerance** (Settings tab, admin only): edit the two keys in `settings.env` with an audit record (who, old, new, reason). The step reads it on its next run; no restart is needed.
- **Till accounts** (Settings tab, admin only): view `till_accounts.csv`. Editing is a later phase; show "Edit with care: changes affect where money is banked."

## Not yet live (show honestly)

The deposit step is **off** on the server until the Google service account is set up (`OIAT_COMPANY_A_UF_DEPOSIT_ENABLED` unset). Show a calm banner in that case: "Deposits aren't switched on yet. Once the till sheet connection is set up, days will appear here." Don't show errors for missing files.

## Done means

Tests with fixture `days.json` and day folders for every state and every reason pattern. The page must render with **no** deposit files at all. Desktop and phone screenshots. No QuickBooks or Google calls from views.

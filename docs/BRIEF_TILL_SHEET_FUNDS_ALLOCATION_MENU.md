# Brief: "Funds Allocation" menu in the Nora Mart till sheet (Google Apps Script)

For: Claude in Chrome, working in the Google Sheet **"Nora Mart Daily Sales Account Breakdown"**
(`https://docs.google.com/spreadsheets/d/15lvfx6q-g7JYgzY4kQZC87JKK2za8SRvuUXjqhovd3A`). Written 4 Oct 2026 by the
Claude session that runs the accounting server.

## Background (what you need to know)

The store fills in this sheet every day. Each month has a tab (`Oct 2026`, ...). Each day is a block showing
where the day's takings went: cash, each POS terminal, each transfer account.

A server reads the sheet with a **read-only Google service account** (`oiat-sheets-reader@oiat-ops.iam.gserviceaccount.com`).
Every evening it moves each day's sales in QuickBooks from "Undeposited Funds" into the right banks, in the sheet's
amounts. We call this **funds allocation**. The server refuses a day if:
- the sales for that day aren't in QuickBooks yet;
- a box on the sheet is blank;
- the sheet total and the sales differ too much;
- the day has already been banked.

**The goal:** a staff member who has just finished a day's block can ask for it to be banked now, instead of waiting
for the evening run.

**The design is deliberate. Don't change it.** The sheet must **not** call the server: it is on a private
network and can't be reached from Google. Instead, the sheet **writes a request row** into a tab, and the server
**reads that tab every ~10 minutes** and does the work.

## What to build (bound Apps Script: Extensions → Apps Script)

1. **Custom menu** `Funds Allocation`, added in `onOpen`, with:
   - `Bank days now…`, which opens the modal below;
   - `Recent requests`, which shows the last 15 rows of the requests tab in a small modal.
2. **Modal** (`HtmlService`, about 360×320):
   - **From** and **To** date pickers, both defaulting to **yesterday** (Africa/Lagos time);
   - a note field, optional, up to 200 characters;
   - **Request banking** and **Cancel** buttons.

   Validation, with friendly messages shown in the modal:
   - From ≤ To;
   - To is no later than yesterday (Africa/Lagos);
   - From is no earlier than **2026-09-25**;
   - the range is at most **31 days**;
   - if an identical range is already `requested` (not yet processed), don't add a duplicate; say it's already queued.

   On success, show: "Requested. The server checks every ~10 minutes; the result appears in Slack and in
   Recent requests."
3. **Requests tab** named exactly **`Bank requests`**, created by the script if it's missing.
   - Header row, exactly (A–I):
     `Request ID | Requested at | Requested by | From | To | Note | Status | Result | Processed at`
   - The script writes A–G only:
     - **Request ID:** `Utilities.getUuid()`;
     - **Requested at:** ISO date-time with offset, Africa/Lagos;
     - **Requested by:** `Session.getActiveUser().getEmail()`, or `unknown` if empty;
     - **From** and **To:** as `YYYY-MM-DD` **text**. Set the number format to `@` before writing, so Sheets keeps them as text and doesn't turn them into dates;
     - **Note**;
     - **Status:** `requested`.
   - **H and I belong to the server.** Never write them.
   - Freeze the header row. Protect the tab with **warning-only** protection (`setWarningOnly(true)`), so staff don't edit it by accident.

## Rules

- **Don't modify any month tab** or any existing cell outside `Bank requests`. No formulas, formatting or values anywhere else.
- **No external calls:** no `UrlFetchApp`, no triggers other than the simple `onOpen`, no secrets, no other files.
- **Small and readable:** keep it to a single `Code.gs` and one `Dialog.html`, with comments explaining each part.
- **Script time zone:** `Africa/Lagos` (Project Settings).
- **When done:**
  1. Run a test request for **one past day**, e.g. 3 Oct, so the owner can see the row.
  2. Report the exact header row you created and paste the script.
  3. Don't delete the test row; the server will process it.

## The server's side (for information, not for you to build)

Every ~10 minutes the server reads `Bank requests`. For each `requested` row it runs the same banking routine as the
evening run, for From..To, with every safety check. It reports to Slack.

If the owner gives the service account **edit** access to this sheet (only this sheet), the server also writes:
- **Status:** `done`, `held` or `refused`;
- **Result**, for example "Banked 2 Oct ₦4,868,700; 3 Oct held: cash box blank";
- **Processed at**.

Otherwise it tracks processed Request IDs on its side, and the outcome shows in Slack and the portal.

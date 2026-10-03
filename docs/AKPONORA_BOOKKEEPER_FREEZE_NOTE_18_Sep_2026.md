# Bookkeeper freeze — Akponora QBO (updated 26 Sep 2026)

Please apply this immediately. It stays in force until Marvin confirms the QuickBooks inventory fix is complete and the new catalogue is live (target ~1 Oct).

**Paused — do not enter anything new in QuickBooks for now**

- **Supplier bills: paused.** Keep every supplier invoice / delivery note (paper or photo) in a folder by date. They will be entered after the fix, against the correct accounts. Suppliers can still be paid from the bank as normal. Just note the payment against the invoice in the folder, and do not create a bill in QuickBooks to match it.
- **Customer invoices (e.g. Gold Plates Feast House / GPFH): paused.** Keep a written list of every delivery (date, customer, products, quantities, price). They will be invoiced after the fix. Make sure every delivery is also recorded as a stock-out in EPOS.
- **No backdating** when entries resume. Use the real date of the document.

**Do not**

- Create any new Inventory product in QuickBooks (example of what must stop: Red Bull Watermelon on 17 Sep).
- Put an Inventory **product** on a bill or invoice.
- Post anything to `120000 - Inventory` or its children (`120100`, `120200`, `120201`, `120202`, `120300`).
- Inactivate, delete, merge, or change quantities on existing products.
- Use “Inventory adjustment” / Shrinkage.
- Edit or void old bills or invoices. Past periods are being corrected by journal.

**Why:** the old QuickBooks products have broken quantities and costs. Any bill or invoice on them produces wrong cost of sales. Between 21 and 23 Sep, 22 backdated Gold Plates invoices posted ₦13.7M of wrong cost and pushed the Grocery stock account to −₦13.4M.

**When entries resume (after the fix)**

- Bills: on the **new `AKP-` products** in the product's own unit (e.g. 5 cartons × 24 cans = 120 cans). September deliveries go against the "Goods received not invoiced" account instead, so they are not counted twice.
- Invoices: on the new `AKP-` products only, with the same stock-out recorded in EPOS. Invoice numbers must not start with `SR-` (use e.g. `INV-GPFH-20261002-01`).

Sales at the till continue as normal.

Questions: Marvin. Do not “just create the item so the bill can be entered.”

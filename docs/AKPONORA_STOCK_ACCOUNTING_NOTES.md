# Stock correction decisions — 6 October 2026

Owner instruction: act as the accountant, use reasonable notes in clear writing, and apply the fixes.

## Pack deductions: corrected

The Coke six-pack must deduct six bottles, and the Colgate single must deduct one tube. Their EPOS master amounts were changed from 12 to 6 and from 12 to 1. The matching sale and purchase multipliers were installed under the global pipeline lock. Product IDs and QBO Item IDs stay the same. Prices and historical sales documents were not edited. Mapping versions and approval receipts are retained on the server.

Applied at approximately 19:13–19:15 Toronto time on 6 October (00:13–00:15 Lagos on 7 October). The final mapping SHA is `d71115bdabd84b3664099ff60433537ce3a5eea8642c1bdd71ee71d194e40262`. The new mapping corrects quantities in future pipeline runs, including pending sales; old transformed files must be regenerated. Already-posted days must not be replayed under the new map without a separate historical review.

Suggested memo: **Corrected the pack deduction: six bottles per Coke six-pack; one tube per Colgate single. This changes future deductions. Earlier stock and COGS differences remain in the reconciliation review.**

## Verified October count differences

Use existing QBO account **82, Inventory Shrinkage**, under **Cost of Goods Sold**, for supported October count gains or losses. Use the description “Stock count variance” in the correction memo. A separate account with that name is optional; its creation was blocked by automatic approval review because it needs a specific approval. Do not rename either existing shrinkage account or use account 83 by accident.

Use the date of the verified, reconciled cutoff in the open period. Do not backdate merely to make 1 October balance, and do not wait until month-end if an interim count is properly supported. First reconcile pending sales and received purchases. A live EPOS quantity read after trading and a QBO quantity through yesterday are not comparable physical counts.

Suggested memo: **Stock count variance at [cutoff]. Verified count: [quantity and source]. Book quantity after sales and delivery checks: [quantity]. Correction: [difference]. No delivery has been added twice.**

## Deliveries

A genuine delivery belongs on a supplier bill using the received PO and the actual invoice. Keep the bill unpaid unless there is separate payment evidence and approval. Resolve missing or held bills before adjusting stock. A matching adjustment quantity is a lead, not permission to invent a supplier invoice.

Suggested note: **Possible missing delivery. Check the supplier invoice and received PO. If confirmed, record the bill; exclude the same units from the count correction.**

## Deliberately excluded opening stock

Keep the 78 opening exclusions separate. Most gaps equal the excluded quantities, but that does not establish that the goods were physically present, belonged to the business, or had a recoverable cost. No automatic restoration and no invented purchase cost. If a true opening omission is proved, prepare a separate opening-correction schedule showing original quantity, original cost and the effect of later sales; do not bury it in October shrinkage. Evaluate material prior-period errors separately from current-period losses.

Suggested note: **This quantity was deliberately excluded at cutover. The difference matches that exclusion, but stock ownership, physical quantity and cost are not yet proved. No entry posted.**

## Incomplete and negative records

The ten received EPOS adjustments with no item lines (1–6 Oct; 5529770, 5550027, 5581246–5582217, 5644927) are empty stock takes: EPOS shows no item grid because nothing was saved on them, and they changed no stock (checked 8 Oct in both the 6 Oct and the daily captures). They are recorded as `empty_transfers`, not errors. Negative EPOS quantities require a count and transaction review, not a negative physical-stock target. Do not write a made-up explanation to clear a card. The portal records the staff explanation and preserves the need for accounting follow-up.

Accounting basis: IAS 2 requires supported inventory cost and recognises inventory losses as expense in the period incurred; IAS 8 treats material prior-period errors separately. Sources: [IAS 2](https://www.ifrs.org/issued-standards/list-of-standards/ias-2-inventories/), [IAS 8](https://www.ifrs.org/issued-standards/list-of-standards/ias-8-basis-of-preparation-of-financial-statements/).

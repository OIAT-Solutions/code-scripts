# AKPONORA / NORA MINI MART: staff and owner checklist

Prepared 3 Oct 2026 from the team's **Master Product Review** workbook (filled in by Esther, admin notes by Precious). Nothing has been changed in EPOS or QuickBooks. This is the list of what to decide, check and set up.

**The idea in one line:** we buy many products in bulk (a 10 kg carton, a 50 kg bag, a box of 40) and sell them in small sizes (1 kg, a painter, one sweet). The bulk product becomes the **master** and holds the stock. Each small size is a **child**: when it is sold, EPOS takes its share off the master. Once a family is set up like this, QuickBooks can track it as real stock instead of writing it off as an expense.

| Where things stand | Products | Families |
| --- | ---: | ---: |
| READY: set up in EPOS now (section C) | 181 | 59 |
| YOUR DECISION: Marvin / Precious to decide (section A) | 48 | 6 |
| TEAM: the store must check, weigh or count (section B) | 39 | 5 |
| DEFER: wait (not bought for a long time / not available) | 20 | 7 |
| **Total** | **288** | **77** |

60 rows still have no Yes/No answer from the store (section B2). Rough September sales (1–25 Sep, till lines × price): READY families about ₦10.6M, owner-decision families about ₦2.8M, team families about ₦1.1M, deferred about ₦0.1M. Frozen food alone (families 01–13) is about ₦9.2M.

Supporting files:

- `outputs/master_review_2026-10-03/analysis.csv`: one row per family, with its status, the decision needed, how its products are set up in QuickBooks today, and estimated Sep sales.
- `outputs/master_review_2026-10-03/summary.json`: totals.
- `outputs/master_review_2026-10-03/ready_mapping_impact.csv`: for each READY product, what it posts to now and what it will post to after setup.
- `MISC/.../As of 30th September 2026/AKPONORA_EPOS_master_setup_tasks_2026-10-03.xlsx`: the EPOS click list (section C), the 51 unlinked products and the 78 ghost-stock counts, each with a Done column.

---

## A. Decisions for Marvin / Precious

There are 48 rows across 6 families: three kinds of rice, two loose oils and eggs. They are listed below with the biggest by September sales first. Each question is one sentence, followed by the options and our recommendation. Nothing in these families is set up until you answer.

### A1. My Choice Rice: 14 rows, about ₦1.63M Sep sales (largest)

Master: **MY CHOICE RICE 50kg** (1707168), tracking OFF today. The store agrees it should be the master.

1. **How many measures are in one 50 kg bag?** The team counted 12 painters, 24–25 half painters, 60–62 dericas, 120–124 half dericas and 240–250 milk cups. We used **12 / 24 / 61 / 122 / 245**; our first estimate had been 12.8 / 25.6 / 64 / 128 / 256.
   - *Recommendation:* accept the team's numbers, and do one weigh-in at the next new bag to confirm.
   - In EPOS, set the bag up in grams: volume 50,000 g. Then painter = 4,167 g, half painter = 2,083 g, derica = 820 g, half derica = 410 g, milk cup = 204 g.
2. **Should the sealed 10 kg (2432834) and 5 kg (2465279) bags be their own products, or children of the 50 kg bag?** The team says they stand alone ("it's a sealed rice"). The 10 kg is already stock-tracked on its own.
   - *Options:* (a) separate products with their own stock, if they are bought sealed from the supplier; (b) children of the 50 kg bag, if the store bags them itself.
   - *Recommendation:* (a). One physical product means one stock item.
3. **There are three "MY CHOICE RICE 25kg" products: which ones stay?** They are 2033794 (₦34,000, the only one sold in Sep), 2025943 (₦35,000) and 1711143 (₦43,500). The team says keep them, maybe renamed "25KG (FAMILY)".
   - *Recommendation:* if they are the same bag, keep 2033794 and **archive** the other two (do not delete).
   - If they really are different bags, rename each so the cashier can tell them apart, and treat each as its own sealed product like question 2.
4. **There are three "MY CHOICE RICE 5kg" products: which one stays?** They are 2465279, 2465281 and 2465267. The team wrote "archived" against 2465281 and "we can keep" against 2465267.
   - *Recommendation:* keep 2465279 (sold in Sep), confirm 2465281 is archived, and decide 2465267 the same way as question 3.
5. **Is the 12.5 kg bag (2080571) sealed like the 25/10/5 kg, or scooped from the 50 kg bag?** The team agreed with "¼ bag", but called the other sizes sealed.
   - *Recommendation:* treat it the same way as the other bag sizes.

| Ref | Product (EPOS ID) | Sep lines | Our number per master | Team says | Admin note |
| --- | --- | ---: | --- | --- | --- |
| 17-M | MY CHOICE RICE 50kg (1707168) | 12 | master | Yes | which duplicates to keep; sealed 10/5 kg alone? |
| 17-1 | MY CHOICE RICE 25kg (2033794) | 5 | 2 | keep, rename "25KG (FAMILY)" | |
| 17-2 | MY CHOICE RICE 25kg (2025943) | 0 | 2 | keep | |
| 17-3 | MY CHOICE RICE 25kg (1711143) | 0 | 2 | keep | |
| 17-4 | MY CHOICE RICE 12.5kg (2080571) | 0 | 4 | Yes | |
| 17-5 | MY CHOICE RICE 10kg (2432834) | n/a | 5 | stands alone, sealed | |
| 17-6 | MY CHOICE RICE 5kg (2465279) | 1 | 10 | stands alone, sealed | |
| 17-7 | MY CHOICE RICE 5kg (2465281) | 1 | 10 | archived | |
| 17-8 | MY CHOICE RICE 5kg (2465267) | 0 | 10 | we can keep | |
| 17-9 | MY CHOICE RICE PAINT (1688440) | 71 | 12.8 | 12 | used 12 |
| 17-10 | MY CHOICE RICE HALF PAINT (1691217) | 42 | 25.6 | 24–25 | used 24 |
| 17-11 | MY CHOICE RICE DERICA (1688443) | 50 | 64 | 60–62 | used 61 |
| 17-12 | MY CHOICE RICE HALF DERICA (1698962) | 18 | 128 | 120–124 | used 122 |
| 17-13 | MY CHOICE RICE MILK CUP (1698964) | 8 | 256 | 240–250 | used 245 |

### A2. Long Grain Rice: 10 rows, about ₦0.44M

Master: **LONG GRAIN RICE 50kg** (1666763), tracking OFF.

1. **Should the sealed 25 kg (2025945), 12.5 kg (2330572), 10 kg (1974105) and 5 kg (1974106) bags be their own products?** The team says they "can also stand on their own".
   - *Recommendation:* yes, as long as they are bought sealed. Each one gets its own stock, and the 50 kg bag is the master only for the loose measures.
2. **Accept the same measure counts as My Choice?** These are 12 painters, 24 half painters, 61 dericas, 122 half dericas and 245 milk cups per 50 kg bag, which the team gave the same answers for.
   - *Recommendation:* yes, using the same grams as A1.

| Ref | Product (EPOS ID) | Sep lines | Our number | Team says | Used |
| --- | --- | ---: | --- | --- | --- |
| 18-M | LONG GRAIN RICE 50kg (1666763) | 3 | master | Yes | |
| 18-1…18-4 | 25kg (2025945) / 12.5kg (2330572) / 10kg (1974105) / 5kg (1974106) | 2 / 0 / 0 / 0 | 2 / 4 / 5 / 10 | sealed, can stand alone (18-4 not answered) | decision |
| 18-5 | LONG GRAIN RICE(painter) (1614732) | 20 | 12.8 | 12 | 12 |
| 18-6 | (half painter) (1614736) | 12 | 25.6 | 24–25 | 24 |
| 18-7 | (derica) (1614738) | 22 | 64 | 60–62 | 61 |
| 18-8 | (half derica) (1614740) | 3 | 128 | 120–124 | 122 |
| 18-9 | (milk cup) (1614741) | 2 | 256 | 240–250 | 245 |

### A3. Eggs: 5 rows, about ₦0.25M

Master: **EGG 30PCS** (1610049). It is already stock-tracked (30 Each) and already an Inventory item in QuickBooks. EGG 1PC, EGG & CREATE BY 12 and EGG & CREATE BY 15 are **already linked** to it in EPOS and in QuickBooks. Only **EGG & CREATE BY 6** (2197651) is not linked, so it still posts as a non-stock item.

1. **Are the "EGG & CREATE BY 6/12/15" products just eggs from the same stock, sold in a plastic crate?** Our first note said to treat the crates as separate products. The team says "they are the same products".
   - *Recommendation:* agree with the team. EGG 30PCS stays the master, and EGG & CREATE BY 6 is linked as **6 Each** of the 30.
   - If the plastic crates are bought and counted separately, tell us, and the crates become their own product.

| Ref | Product (EPOS ID) | Sep lines | Per master | EPOS today |
| --- | --- | ---: | ---: | --- |
| 28-M | EGG 30PCS (1610049) | n/a | master | tracked, 30 Each |
| 28-1 | EGG & CREATE BY 15 (2197653) | 18 | 2 | linked 15 of 30 |
| 28-2 | EGG & CREATE BY 12 (2197652) | 18 | 2.5 | linked 12 of 30 |
| 28-3 | EGG & CREATE BY 6 (2197651) | 22 | 5 | **not linked** |
| 28-4 | EGG 1PC (1610050) | 534 | 30 | linked 1 of 30 |

### A4. Vegetable Oil (loose): 5 rows, about ₦0.25M

Proposed master: **VEGETABLE OIL 25ltr** (1674890). It is already stock-tracked, but its unit and volume are blank in EPOS.

1. **Are the 75cl / 50cl / 37.5cl bottles filled from the 25-litre keg?**
   - *Recommendation:* yes, so the keg is the master. Set it to unit ml, volume 25,000.
2. **Is the sealed 5-litre (2188944) its own product?** The team says yes ("it's a sealed product").
   - *Recommendation:* yes. Switch its own stock tracking ON, with no link to the keg.
3. **How many bottles come out of one keg?** The team counted 43–44 × 75cl and 86–88 × 37.5cl. We used 44 and 87. Nobody counted the 50cl, so we estimate 66.
   - Note: 44 × 75 cl is about 33 litres, which is more than 25. Either the bottles hold less than the label or the keg holds more. Stock still works if the counts are right.
   - *Recommendation:* do one test pour of a full keg and use those counts. With 44 / 66 / 87, the amounts are 75cl = 568 ml, 50cl = 379 ml and 37.5cl = 287 ml.

| Ref | Product (EPOS ID) | Sep lines | Our number | Team says | Used |
| --- | --- | ---: | --- | --- | --- |
| 26-M | VEGETABLE OIL 25ltr (1674890) | n/a | master | Yes | |
| 26-1 | VEGETABLE OIL5ltrs (2188944) | 0 | 5 | sealed, own product | decision |
| 26-2 | VEGETABLE OIL 75CL (1610099) | 53 | 33.33 | 43–44 | 44 |
| 26-3 | VEGETABLE OIL 50CL (1688505) | 1 | 50 | not sure | 66 (estimate) |
| 26-4 | VEGETABLE OIL 37.5CL (1610100) | 93 | 66.67 | 86–88 | 87 |

### A5. Short Grain Rice: 9 rows, about ₦0.14M

Master: **SHORT GRAIN RICE 50kg** (2025946), tracking OFF. The questions are the same as for Long Grain:

- **Sealed 25 kg (2025947), 10 kg (1974107) and 5 kg (1974111):** should they be their own products? *Recommendation:* yes.
- **Measures:** painter 12 (team 12), half painter 24 (team 24–25), derica 61 (team 60–62), half derica 122 (team 120–124), milk cup 245 (team 240–250). *Recommendation:* accept.

### A6. Palm Oil (loose): 5 rows, about ₦0.13M

The questions are the same as for vegetable oil (A4):

- **Master:** PALM OIL 25ltr (1686797), tracked, unit and volume blank.
- **Sealed 5-litre (2198112):** stands alone.
- **Bottles per keg:** 75cl (1610101) 44, 50cl (1688501) 66 (estimate), 37.5cl (1610102) 87.
- **Extra:** at 30 Sep, EPOS showed **20 palm oil kegs** that nobody could confirm. They were left out of the opening stock (₦865k at cost). Count the kegs on the shelf when this family is set up (see D2).

**Top owner decisions by sales impact:**

1. My Choice Rice (₦1.63M)
2. Long Grain Rice (₦0.44M)
3. Eggs (₦0.25M)
4. Vegetable Oil (₦0.25M)
5. Short Grain Rice (₦0.14M)

---

## B. For the store team

### B1. Check, weigh or count (39 TEAM rows, 5 families)

| Family | Rows | Sep sales (est) | What to check | Why |
| --- | ---: | ---: | --- | --- |
| **Big Titus Fish** (15) | 3 | ₦684k | **Already done in EPOS.** BIG TITUS FISH 20kg (2638376) exists and is stock-tracked at 20,000 g. The 1 kg and 0.5 kg were linked at 1,000 g and 500 g (checked 1 Oct), and QuickBooks already follows it. Just confirm one carton really is 20 kg, and count the cartons. | So the stock count starts right |
| **Oloyin Beans** (21) | 6 | ₦211k | At the next restock, weigh one bag and count the painters in it (the team thinks 14–15; we used 15). A new master **OLOYIN BEANS BAG** is then created. Also count how many milk cups fill one painter: the team wrote 22, we kept 20 (5 dericas × 4). Weigh one PACKAGED OLOYIN BEANS pack. | The painter is the master for now; the bag should be |
| **Olotu Beans** (22) | 6 | ₦50k | Weigh one PACKAGED OLOTU BEANS pack. Tell us if Olotu is bought in bags, and how many painters are in a bag. Same milk-cup question as Oloyin. | Same as above |
| **Gari** (25) | 20 | ₦154k | **On hold until the next gari purchase** (the team says bag sizes vary). Then, for each of Ijebu, Yellow and White: weigh the bag and count the painters in it (the team's research says 20 for Yellow). Also say which gari the 1 kg, 1.2 kg and 2 kg refills are. | One "A BAG OF GARI" cannot hold stock for three different garis, so we need three bag masters |
| **Butter Mint** (38) | 4 | ₦1k | The team wrote **40 pieces per 152 g bag**. Count one bag to confirm. Then the carton = 24 × 40 = 960 pieces, 3pcs = 3 and 1pc = 1. Count the cartons on the shelf before the change, because the carton's count unit changes. | The 3pcs and 1pc are not linked today |

### B2. Rows nobody has answered yet (60 rows, no Yes/No)

| Family | Status | Rows not answered | What to do |
| --- | --- | --- | --- |
| Gari (25) | TEAM | all 20 | Wait for next purchase (B1) |
| My Choice Rice (17) | YOUR DECISION | 17-1, 17-2, 17-3, 17-5, 17-6, 17-7, 17-8 (comments given, no Yes/No) | Covered by A1 |
| Brown Rice (20) | DEFER | 20-M and 4 children | Not available, so leave |
| Titus Fish (14), Chicken Breast (16), Familia Tissue (44) | DEFER | 3 each | Leave (see B3) |
| Big Titus Fish (15) | TEAM | 15-M, 15-1, 15-2 | Confirm the 20 kg carton (B1) |
| Oloyin Beans (21) | TEAM | 21-1 half painter, 21-5 packaged | Half painter = 2 per painter? Weigh the pack |
| Olotu Beans (22) | TEAM | 22-5 packaged | Weigh one pack |
| Eggs (28) | YOUR DECISION | 28-M, 28-4 | Covered by A3 |
| Meijan Soda Cracker (65), Miksi (71) | DEFER | 2 each | Leave |
| Long Grain Rice (18) | YOUR DECISION | 18-4 (5 kg) | Covered by A2 |
| Vegetable / Palm Oil (26, 27) | YOUR DECISION | 26-1, 27-1 (5-litre) | Covered by A4 / A6 |
| Butter Mint (38) | TEAM | 38-1 (152 g bag) | Yes/No: is one bag 1/24 of the carton? |
| Chicken Laps 0.25 kg (01-4), Wisdom toothbrush pcs (60-1), Dano master (72-M) | READY | 1 each | Just tick Yes. These are in the setup list |

### B3. Deferred (20 rows, 7 families): no action now

| Family | Why deferred | Revisit when |
| --- | --- | --- |
| Titus Fish (14): 1 kg, 0.5 kg | Not bought for a long time. Note: someone has since created **TITUS FISH 20KG** (2641339) in EPOS, not tracked yet | Next purchase: set it up like Big Titus |
| Chicken Breast (16): 1 kg, 0.5 kg | Not restocked for a long time, and both have ₦0 cost | Next restock: give the carton kg and cost |
| Brown Rice (20): painter and measures | Not available | When it comes back |
| Familia Ultra White Tissue (44) | Not available. The three products no longer appear in the EPOS catalogue or our mapping | If restocked |
| Meijan Soda Cracker 25g (65) | No longer sold | Archive in EPOS if never coming back |
| Miksi Drink Powder 20g (71) | Not available now; ₦0 cost | When restocked |
| Meridian Club Cream Liquor 50ml (74) | No longer purchased; ₦0 cost | Archive if never coming back |

---

## C. EPOS setup list (181 READY products, 59 families)

**Who:** Precious (store admin) or a browser agent in EPOS Now Back Office. The same list is in the xlsx with a Done column.

**How:**

1. Work family by family. Do the **MASTER** row first, then its children.
2. **Master:** open the product (Products → search the EPOS ID → Edit). Set **Stock tracking = ON**, then set the **Unit of sale** and **Volume of sale** exactly as in the "After" column. **Do not type a stock quantity**: the store counts it afterwards (section E).
3. **Child:** open the child → Stock Control / **Master Products** → add the master named in "After" with the amount shown (for example CHICKEN LAPS 1KG = **1000 g** of the 10,000 g carton). Leave the child's own stock tracking **OFF**.
4. Do not change prices, names, barcodes or categories. Do not delete anything.
5. If what you see does not match the "Before" column, stop and tell Marvin.
6. When a family is finished, write your initials and the date, and tell Marvin the same day.

Frozen food is set up in **grams**, the same way BIG TITUS FISH 20kg already is (20,000 g; the 1 kg child = 1,000 g). Painter-based families (Melon, Ogbono) count in **milk cups**: 1 painter = 20 milk cups.

### C1. Families that need work in EPOS (25 families, 83 rows)

#### 01. Chicken Laps (Orobo) - master: CHICKEN LAPS (OROBO) 10KG (1696079), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **MASTER** CHICKEN LAPS (OROBO) 10KG | 1696079 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 2 | CHICKEN LAPS (OROBO) 5KG | 1696081 | no master link; tracking OFF | 5000 g of master; tracking OFF | Set master product = CHICKEN LAPS (OROBO) 10KG, amount 5000 g | [ ] |
| 3 | CHICKEN LAPS (OROBO) 1KG | 1610115 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = CHICKEN LAPS (OROBO) 10KG, amount 1000 g | [ ] |
| 4 | CHICKEN LAPS (OROBO) 0.5KG HALF | 1610116 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = CHICKEN LAPS (OROBO) 10KG, amount 500 g | [ ] |
| 5 | CHICKEN LAPS (OROBO) 0.25KG  QUARTER | 1996214 | no master link; tracking OFF | 250 g of master; tracking OFF | Set master product = CHICKEN LAPS (OROBO) 10KG, amount 250 g | [ ] |

#### 02. Chicken (Soft) - master: CHICKEN (SOFT) 10KG (1696087), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 6 | **MASTER** CHICKEN (SOFT) 10KG | 1696087 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 7 | CHICKEN (SOFT) 5KG | 1696089 | no master link; tracking OFF | 5000 g of master; tracking OFF | Set master product = CHICKEN (SOFT) 10KG, amount 5000 g | [ ] |
| 8 | CHICKEN (SOFT) 1KG | 1610119 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = CHICKEN (SOFT) 10KG, amount 1000 g | [ ] |
| 9 | CHICKEN (SOFT) 0.5KG | 1610120 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = CHICKEN (SOFT) 10KG, amount 500 g | [ ] |

#### 03. Turkey - master: TURKEY 10kg (1678226), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | **MASTER** TURKEY 10kg | 1678226 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 11 | TURKEY 5kg | 1698654 | no master link; tracking OFF | 5000 g of master; tracking OFF | Set master product = TURKEY 10kg, amount 5000 g | [ ] |
| 12 | TURKEY 1KG | 1610113 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = TURKEY 10kg, amount 1000 g | [ ] |
| 13 | TURKEY 0.5KG | 1610114 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = TURKEY 10kg, amount 500 g | [ ] |

#### 04. Chicken Wings - master: CHICKEN WINGS 10KG (2067524), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 14 | **MASTER** CHICKEN WINGS 10KG | 2067524 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 15 | CHICKEN WINGS 5KG | 2067525 | no master link; tracking OFF | 5000 g of master; tracking OFF | Set master product = CHICKEN WINGS 10KG, amount 5000 g | [ ] |
| 16 | CHICKEN WINGS 1KG | 1610117 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = CHICKEN WINGS 10KG, amount 1000 g | [ ] |
| 17 | CHICKEN WINGS 0.5KG | 1610118 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = CHICKEN WINGS 10KG, amount 500 g | [ ] |

#### 05. Drumstick Chicken - master: DRUMSTICK CHICKEN 10kg (2274671), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 18 | **MASTER** DRUMSTICK CHICKEN 10kg | 2274671 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 19 | DRUMSTICK CHICKEN 5kg | 2351057 | no master link; tracking OFF | 5000 g of master; tracking OFF | Set master product = DRUMSTICK CHICKEN 10kg, amount 5000 g | [ ] |
| 20 | DRUMSTICK CHICKEN 1kg | 2274667 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = DRUMSTICK CHICKEN 10kg, amount 1000 g | [ ] |
| 21 | DRUMSTICK CHICKEN 0.5kg | 2278256 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = DRUMSTICK CHICKEN 10kg, amount 500 g | [ ] |

#### 06. Chicken (Old Layer) - master: CHICKEN(OLD LAYER) 10kg (2411870), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 22 | **MASTER** CHICKEN(OLD LAYER) 10kg | 2411870 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 23 | CHICKEN(OLD LAYER) 1kg | 1632727 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = CHICKEN(OLD LAYER) 10kg, amount 1000 g | [ ] |
| 24 | CHICKEN(OLD LAYER) 0.5kg | 1632728 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = CHICKEN(OLD LAYER) 10kg, amount 500 g | [ ] |

#### 07. Turkey Finger - master: TURKEY FINGER 10kg (2469525), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 25 | **MASTER** TURKEY FINGER 10kg | 2469525 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 26 | TURKEY FINGER 1kg | 2469531 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = TURKEY FINGER 10kg, amount 1000 g | [ ] |
| 27 | TURKEY FINGER 0.5kg | 2469708 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = TURKEY FINGER 10kg, amount 500 g | [ ] |

#### 08. Gizzard - master: GIZZARD CARTON 15kg (2274675), unit g, volume 15000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 28 | **MASTER** GIZZARD CARTON 15kg | 2274675 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 15000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 15000 | [ ] |
| 29 | GIZZARD 1kg | 1632170 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = GIZZARD CARTON 15kg, amount 1000 g | [ ] |
| 30 | GIZZARD 0.5kg | 1648929 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = GIZZARD CARTON 15kg, amount 500 g | [ ] |

#### 09. Kote Fish - master: KOTE FISH 20KG (2379252), unit g, volume 20000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 31 | **MASTER** KOTE FISH 20KG | 2379252 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 20000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 20000 | [ ] |
| 32 | KOTE FISH 1KG | 1610111 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = KOTE FISH 20KG, amount 1000 g | [ ] |
| 33 | KOTE FISH 0.5KG(HALF) | 1610112 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = KOTE FISH 20KG, amount 500 g | [ ] |
| 34 | KOTE FISH 0.25KG(QUARTER) | 1981568 | no master link; tracking OFF | 250 g of master; tracking OFF | Set master product = KOTE FISH 20KG, amount 250 g | [ ] |

#### 10. Sawa Fish - master: SAWA FISH 20KG (2379240), unit g, volume 20000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 35 | **MASTER** SAWA FISH 20KG | 2379240 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 20000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 20000 | [ ] |
| 36 | SAWA FISH 10KG | 2379241 | no master link; tracking OFF | 10000 g of master; tracking OFF | Set master product = SAWA FISH 20KG, amount 10000 g | [ ] |
| 37 | SAWA FISH 1KG | 1610107 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = SAWA FISH 20KG, amount 1000 g | [ ] |
| 38 | SAWA FISH 0.5KG | 1610108 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = SAWA FISH 20KG, amount 500 g | [ ] |

#### 11. Crocker Fish - master: CROCKER FISH 20KG (2379258), unit g, volume 20000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 39 | **MASTER** CROCKER FISH 20KG | 2379258 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 20000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 20000 | [ ] |
| 40 | CROCKER FISH 1KG | 1610109 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = CROCKER FISH 20KG, amount 1000 g | [ ] |
| 41 | CROCKER FISH 0.5KG | 1610110 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = CROCKER FISH 20KG, amount 500 g | [ ] |
| 42 | CROCKER FISH 0.25KG | 1726710 | no master link; tracking OFF | 250 g of master; tracking OFF | Set master product = CROCKER FISH 20KG, amount 250 g | [ ] |

#### 12. Panla Fish (Big) - master: PANLA FISH BIG 15KG (2288828), unit g, volume 15000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 43 | **MASTER** PANLA FISH BIG 15KG | 2288828 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 15000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 15000 | [ ] |
| 44 | PANLA FISH BIG 10KG | 2238118 | no master link; tracking OFF | 10000 g of master; tracking OFF | Set master product = PANLA FISH BIG 15KG, amount 10000 g | [ ] |
| 45 | PANLA FISH BIG 1KG | 1734436 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = PANLA FISH BIG 15KG, amount 1000 g | [ ] |
| 46 | PANLA FISH BIG 0.5KG | 1734438 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = PANLA FISH BIG 15KG, amount 500 g | [ ] |
| 47 | PANLA FISH BIG 0.25KG(quarter) | 2017695 | no master link; tracking OFF | 250 g of master; tracking OFF | Set master product = PANLA FISH BIG 15KG, amount 250 g | [ ] |

#### 13. Owere - master: OWERE 10KG (2394414), unit g, volume 10000

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 48 | **MASTER** OWERE 10KG | 2394414 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit g, volume 10000 | Switch stock tracking ON; set unit of sale to g; set volume of sale to 10000 | [ ] |
| 49 | OWERE 1KG | 1705844 | no master link; tracking OFF | 1000 g of master; tracking OFF | Set master product = OWERE 10KG, amount 1000 g | [ ] |
| 50 | OWERE 0.5KG | 1705848 | no master link; tracking OFF | 500 g of master; tracking OFF | Set master product = OWERE 10KG, amount 500 g | [ ] |
| 51 | OWERE 0.25KG | 1705850 | no master link; tracking OFF | 250 g of master; tracking OFF | Set master product = OWERE 10KG, amount 250 g | [ ] |

#### 23. Melon (Egusi) - master: MELON(EGUSI) PAINTER (2256490), unit Each, volume 20

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 52 | **MASTER** MELON(EGUSI) PAINTER | 2256490 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 20 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 20 | [ ] |
| 53 | MELON(EGUSI) HALF PAINTER | 2258776 | no master link; tracking OFF | 10 Each of master; tracking OFF | Set master product = MELON(EGUSI) PAINTER, amount 10 Each | [ ] |
| 54 | MELON(EGUSI) DERICA | 2258777 | no master link; tracking OFF | 4 Each of master; tracking OFF | Set master product = MELON(EGUSI) PAINTER, amount 4 Each | [ ] |
| 55 | MELON(EGUSI) HALF DERICA | 2258778 | no master link; tracking OFF | 2 Each of master; tracking OFF | Set master product = MELON(EGUSI) PAINTER, amount 2 Each | [ ] |
| 56 | MELON(EGUSI) MILK CUP | 2258780 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = MELON(EGUSI) PAINTER, amount 1 Each | [ ] |

#### 24. Ogbono - master: PAINTER OGBONO (2361294), unit Each, volume 20

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 57 | **MASTER** PAINTER OGBONO | 2361294 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 20 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 20 | [ ] |
| 58 | HALF PAINTER OGBONO | 2361296 | no master link; tracking OFF | 10 Each of master; tracking OFF | Set master product = PAINTER OGBONO, amount 10 Each | [ ] |
| 59 | DERICA OGBONO | 2361301 | no master link; tracking OFF | 4 Each of master; tracking OFF | Set master product = PAINTER OGBONO, amount 4 Each | [ ] |
| 60 | HALF DERICA OGBONO | 2361302 | no master link; tracking OFF | 2 Each of master; tracking OFF | Set master product = PAINTER OGBONO, amount 2 Each | [ ] |
| 61 | MILK CUP OGBONO | 2361306 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = PAINTER OGBONO, amount 1 Each | [ ] |

#### 49. Platter Take Away (Black) - master: PLATTER TAKE AWAY PACK_BIG 50PCS (BLACK) (1640070), unit Each, volume 50

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 122 | **MASTER** PLATTER TAKE AWAY PACK_BIG 50PCS (BLACK) | 1640070 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 50 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 50 | [ ] |
| 123 | PLATTER TAKE AWAY PACK_BIG 25PCS (BLACK) | 1640077 | no master link; tracking OFF | 25 Each of master; tracking OFF | Set master product = PLATTER TAKE AWAY PACK_BIG 50PCS (BLACK), amount 25 Each | [ ] |
| 124 | PLATTER TAKE AWAY PACK_BIG PCS (BLACK) | 1640074 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = PLATTER TAKE AWAY PACK_BIG 50PCS (BLACK), amount 1 Each | [ ] |

#### 62. Lucky Racer Ball Pen Blue - master: LUCKY RACER BALL PEN BLUE(PACK) (1619277), unit Each, volume 50

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 155 | **MASTER** LUCKY RACER BALL PEN BLUE(PACK) | 1619277 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 50 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 50 | [ ] |
| 156 | LUCKY RACER BALL PEN BLUE(pcs) | 1619278 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = LUCKY RACER BALL PEN BLUE(PACK), amount 1 Each | [ ] |

#### 63. Lucky Racer Ball Pen Black - master: LUCKY RACER BALL PEN BLACK(PACK) (1619289), unit Each, volume 50

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 157 | **MASTER** LUCKY RACER BALL PEN BLACK(PACK) | 1619289 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 50 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 50 | [ ] |
| 158 | LUCKY RACER BALL PEN BLACK(pcs) | 1619291 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = LUCKY RACER BALL PEN BLACK(PACK), amount 1 Each | [ ] |

#### 64. Kelifa Oat Chocolate Milk 10g - master: KELIFA-OAT CHOCOLATE MILK600g*18 (1657730), unit Each, volume 1080

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 159 | **MASTER** KELIFA-OAT CHOCOLATE MILK600g*18 | 1657730 | tracking ON, unit Each, volume 1116 | tracking ON, unit Each, volume 1080 | Set volume of sale to 1080 - CHANGES the count of an already-tracked product (1,116 -> 1,080). Do this straight after counting the packs on the shelf, then tell Marvin. | [ ] |
| 160 | KELIFA-OAT CHOCOLATE MILK10g | 1597330 | KELIFA-OAT CHOCOLATE MILK600g*18 (1657730): 1Each of 1116Each; tracking OFF | 1 Each of master; tracking OFF | No change - check only - Store corrected the count to 1080 per master. | [ ] |

#### 70. Cowbell Coffee 3in1 25g - master: COWBELL-COFFEE(3in1)25g*10 (1664689), unit Each, volume 10

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 169 | **MASTER** COWBELL-COFFEE(3in1)25g*10 | 1664689 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 10 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 10 | [ ] |
| 170 | COWBELL-COFFEE(3in1)25g | 1664687 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = COWBELL-COFFEE(3in1)25g*10, amount 1 Each | [ ] |

#### 72. Dano Chocolate Milk 22g - master: DANO-CHOCOLATE MILK POWDER22g*10 (2315896), unit Each, volume 10

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 171 | **MASTER** DANO-CHOCOLATE MILK POWDER22g*10 | 2315896 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 10 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 10 | [ ] |
| 172 | DANO-CHOCOLATE MILK POWDER22g | 2261705 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = DANO-CHOCOLATE MILK POWDER22g*10, amount 1 Each | [ ] |

#### 73. Nasa Joe Plantain Chips - master: NASA JOE PLANTAIN CHIPS*12 (1670532), unit Each, volume 12

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 173 | **MASTER** NASA JOE PLANTAIN CHIPS*12 | 1670532 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 12 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 12 | [ ] |
| 174 | NASA JOE PLANTAIN CHIPS | 1670529 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = NASA JOE PLANTAIN CHIPS*12, amount 1 Each | [ ] |

#### 75. Mother Goose Safety Matches - master: MOTHER GOOSE SAFETY MATCHES 10boxes (1628615), unit Each, volume 10

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 175 | **MASTER** MOTHER GOOSE SAFETY MATCHES 10boxes | 1628615 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 10 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 10 | [ ] |
| 176 | MOTHER GOOSE SAFETY MATCHES | 1628621 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = MOTHER GOOSE SAFETY MATCHES 10boxes, amount 1 Each | [ ] |

#### 76. Fancy Gift Nylon - master: FANCY GIFT NYLON*50 (2033849), unit Each, volume 50

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 177 | **MASTER** FANCY GIFT NYLON*50 | 2033849 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 50 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 50 | [ ] |
| 178 | FANCY GIFT NYLON*25 | 2033851 | no master link; tracking OFF | 25 Each of master; tracking OFF | Set master product = FANCY GIFT NYLON*50, amount 25 Each | [ ] |
| 179 | FANCY GIFT NYLON | 2033847 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = FANCY GIFT NYLON*50, amount 1 Each | [ ] |

#### 77. Premier Cool Black Soap 60g - master: PREMIER COOL-SHEA BUTTER & CAMWOOD BLACK SOAP60g*7 (2060341), unit Each, volume 7

| # | Product | EPOS ID | Before (EPOS today) | After | What to do | Done |
| --- | --- | --- | --- | --- | --- | --- |
| 180 | **MASTER** PREMIER COOL-SHEA BUTTER & CAMWOOD BLACK SOAP60g*7 | 2060341 | tracking OFF, unit (blank), volume (blank) | tracking ON, unit Each, volume 7 | Switch stock tracking ON; set unit of sale to Each; set volume of sale to 7 | [ ] |
| 181 | PREMIER COOL-SHEA BUTTER & CAMWOOD BLACK SOAP60g | 1635861 | no master link; tracking OFF | 1 Each of master; tracking OFF | Set master product = PREMIER COOL-SHEA BUTTER & CAMWOOD BLACK SOAP60g*7, amount 1 Each | [ ] |

### C2. Families already set up correctly in EPOS: check only (34 families, 98 rows)

These already show the right master, tracking and amounts (checked on 1 Oct). QuickBooks already treats them as stock. Open each master, confirm tracking is ON and the volume matches, then tick.

| Ref | Master (EPOS ID) | Should show: tracking, unit, volume | Children and their master amount | Checked |
| --- | --- | --- | --- | --- |
| 29 | BACKWOODS RUSSIAN CREAM CIGARS 5X1*40 (1651729) | ON, Each, 40 | BACKWOODS RUSSIAN CREAM CIGARS 5X1*5(pack) (1610087) = 5 Each; BACKWOODS RUSSIAN CREAM CIGARS 5X1(pcs) (1651742) = 1 Each | [ ] |
| 30 | BACKWOODS DARK STOUT CIGARS (5X1)*40 (1652317) | ON, Each, 40 | BACKWOODS DARK STOUT CIGARS 1X1 (1651777) = 1 Each | [ ] |
| 31 | TOM TOM152g*24 (1679481) | ON, Each, 960 | TOM TOM 40pcs (1597342) = 40 Each; TOM TOM 3pcs (1930426) = 3 Each; TOM TOM 1pc (1930415) = 1 Each | [ ] |
| 32 | VICKS LEMON PLUS90g*24 (1737692) | ON, Each, 720 | VICKS LEMON PLUS 30ps (1608286) = 30 Each; VICKS LEMON PLUS 3pcs (1930437) = 3 Each; VICKS LEMON PLUS 1pc (1930431) = 1 Each | [ ] |
| 33 | VICKS BLUE97g*24 (1970314) | ON, Each, 720 | VICKS BLUE97g (1597344) = 30 Each; VICKS BLUE 3pcs (1642046) = 3 Each; VICKS BLUE 1pc (1930441) = 1 Each | [ ] |
| 34 | SWEETCO SPLASH 150g*20 (2085274) | ON, Each, 960 | SWEETCO SPLASH 150g (2085271) = 48 Each; SWEETCO SPLASH 3pcs (2085288) = 3 Each; SWEETCO SPLASH 1pc (2085279) = 1 Each | [ ] |
| 35 | TOMY-RIO POP MEGA LOLLIPOP*480 (1641483) | ON, Each, 480 | TOMY-RIO POP MEGA LOLLIPOP48pcs(pack) (1608295) = 48 Each; TOMY-RIO POP MEGA LOLLIPOP(pcs) (1636401) = 1 Each | [ ] |
| 36 | TOMY-RIO POP LOLLIPOP*480 (1667378) | ON, Each, 480 | TOMY-RIO POP LOLLIPOP48pcs(pack) (1608297) = 48 Each; TOMY-RIO POP LOLLIPOP(pcs) (1636400) = 1 Each | [ ] |
| 37 | KELIFA-MILK BONBON CANDY500g(128pcs) (2469130) | ON, Each, 128 | KELIFA-MILK BONBON CANDY*3 (2469132) = 3 Each; KELIFA-MILK BONBON CANDY (2469098) = 1 Each | [ ] |
| 39 | MENTOS CHEWY DRAGEES*40 (1693247) | ON, Each, 40 | MENTOS CHEWY DRAGEES 3pcs (1693253) = 3 Each; MENTOS CHEWY DRAGEES 1pc (1693249) = 1 Each | [ ] |
| 40 | MICKS ECLAIRS 120pcs (2293966) | ON, Each, 120 | MICKS ECLAIRS 3pcs (2293960) = 3 Each; MICKS ECLAIRS 1pc (2293967) = 1 Each | [ ] |
| 41 | ROSE CARLA-TISSUE PAPER_2PLYpk of 48 (1676782) | ON, Each, 48 | ROSE CARLA-TISSUE PAPER_2PLYpk of 6 (1596384) = 6 Each; ROSE CARLA-TISSUE PAPER_2PLY (1596385) = 1 Each | [ ] |
| 42 | ROSE PLUS-TISSUE PAPER_2PLYpk of 48 (1666933) | ON, Each, 48 | ROSE PLUS-TISSUE PAPER_2PLYpk of 6 (1596386) = 6 Each; ROSE PLUS-TISSUE PAPER_2PLY (1596387) = 1 Each | [ ] |
| 43 | SOFTWAVE-TISSUE PAPERpk of 48 (1666946) | ON, Each, 48 | SOFTWAVE-TISSUE PAPERpk of 6 (1596428) = 6 Each; SOFTWAVE-TISSUE PAPER (1596429) = 1 Each | [ ] |
| 45 | SACHET WATER 50cl*20 (1615205) | ON, Each, 20 | COLD SACHET WATER 50cl*20 (1615212) = 20 Each; COLD SACHET WATER 50cl*10 (2027186) = 10 Each | [ ] |
| 46 | 1000ML TAKE AWAY PACK_BIG 100PCS (1609659) | ON, Each, 100 | 1000ML TAKE AWAY PACK_BIG 50PCS (1609660) = 50 Each; 1000ML TAKE AWAY PACK_BIG 25PCS (1697234) = 25 Each; 1000ML TAKE AWAY PACK_BIG (1609661) = 1 Each | [ ] |
| 47 | 750ML TAKE AWAY PACK_SMALL 100PCS (1609664) | ON, Each, 100 | 750ML TAKE AWAY PACK_SMALL 50PCS (1609667) = 50 Each; 750ML TAKE AWAY PACK_SMALL 25PCS (1697230) = 25 Each; 750ML TAKE AWAY PACK_SMALL (1609669) = 1 Each | [ ] |
| 48 | TAKE AWAY PACK_BIG ARS BLACK 100PCS (1639269) | ON, Each, 100 | TAKE AWAY PACK_BIG ARS BLACK 50PCS (1639270) = 50 Each; TAKE AWAY PACK_BIG ARS BLACK 25PCS (2252913) = 25 Each; TAKE AWAY PACK_BIG ARS BLACK PCS (1707962) = 1 Each | [ ] |
| 50 | 360ML ROUND TAKE AWAY PACK 50pcs (1700517) | ON, Each, 50 | 360ML ROUND TAKE AWAY PACK 25pcs (1777806) = 25 Each | [ ] |
| 51 | DISPOSABLE CUP_RED 50PCS (1609679) | ON, Each, 50 | DISPOSABLE CUP_RED 25PCS (1627217) = 25 Each; DISPOSABLE CUP_RED (1609680) = 1 Each | [ ] |
| 52 | TAKE AWAY SPOON_COLOURED*80pcs (1596434) | ON, Each, 80 | TAKE AWAY SPOON_COLOURED20pcs (1639255) = 20 Each | [ ] |
| 53 | TIGER HEAD UM-3 1.5V AA SMALL BATTERY 24PCS (1635056) | ON, Each, 24 | TIGER HEAD UM-3 1.5V AA SMALL BATTERY 12PCS (1635054) = 12 Each; TIGER HEAD UM-3 1.5V AA SMALL BATTERY 4PCS (1635053) = 4 Each; TIGER HEAD UM-3 1.5V AA SMALL BATTERY 2PCS (1635052) = 2 Each | [ ] |
| 54 | MCZEN UM-3 1.5V AA MEDIUM BATTERY 60PCS (1635068) | ON, Each, 60 | MCZEN UM-3 1.5V AA MEDIUM BATTERY 4PCS (1635067) = 4 Each; MCZEN UM-3 1.5V AA MEDIUM BATTERY 2PCS (1635066) = 2 Each | [ ] |
| 55 | MCZEN UM-4 1.5V AAA SMALL BATTERY 60PCS (1635071) | ON, Each, 60 | MCZEN UM-4 1.5V AAA SMALL BATTERY 4PCS (1635070) = 4 Each; MCZEN UM-4 1.5V AAA SMALL BATTERY 2PCS (1635069) = 2 Each | [ ] |
| 56 | TIGER SAFETY RAZOR BLADES PK OF 100 (1737785) | ON, Each, 100 | TIGER SAFETY RAZOR BLADES PK OF 10 (1636480) = 10 Each; TIGER SAFETY RAZOR BLADES PC (1636479) = 1 Each | [ ] |
| 57 | NOSE MASK DISPOSABLE 50pcs (1635057) | ON, Each, 50 | NOSE MASK DISPOSABLE (1635058) = 1 Each | [ ] |
| 58 | FACE TOWEL (12pcs) (1630000) | ON, Each, 12 | FACE TOWEL pcs (1629999) = 1 Each | [ ] |
| 59 | SMALL FACE TOWEL12pcs (2295340) | ON, Each, 12 | SMALL FACE TOWEL pcs (2295327) = 1 Each | [ ] |
| 60 | WISDOM 212 TOOTHBRUSH_EXTRA HARD(PACK) (1597182) | ON, Each, 12 | WISDOM 212 TOOTHBRUSH_EXTRA HARD(PCS) (1597181) = 1 Each | [ ] |
| 61 | VISTA DOLLAR BLUE BALL PEN*50pcs(pack) (1619296) | ON, Each, 50 | VISTA DOLLAR BLUE BALL PEN(pcs) (1619297) = 1 Each | [ ] |
| 66 | FEIRACO YOGUT 125gby4*12 (1910124) | ON, Each, 48 | FEIRACO YOGUT 125g (2411905) = 1 Each | [ ] |
| 67 | MONSTER ENERGY DRINK CAN 440ML*24 (1609705) | ON, Each, 24 | MONSTER ENERGY CAN DRINK 440ML (1609709) = 1 Each | [ ] |
| 68 | MILK BLOCK*100pcs (2152326) | ON, Each, 100 | MILK BLOCK 1pc (2152327) = 1 Each | [ ] |
| 69 | YOGHURT STICKS 44pcs (250g) (1996269) | ON, Each, 44 | YOGHURT STICKS 2pcs (1700247) = 2 Each | [ ] |

---

## D. Other open staff items (from the roadmap §3)

### D1. 51 products with no working master link

These are sold on the till but are not linked in EPOS to a stock-tracked master. Some have no link at all. Others point to a master that is not tracked or no longer exists. Examples are Coke, Fanta and Sprite 60CL singles and CWAY water singles. Today they post to QuickBooks as non-stock items, so their stock is not followed.

- **What to do:** for each one, link it to the right stock-tracked master with the right amount. If it really is sold on its own, switch its own stock tracking ON. Tick it off in the xlsx sheet "51 unlinked products".
- **Why:** after the fix we re-pull the catalogue and these become proper stock items (section E).

| EPOS ID | Product | Problem |
| --- | --- | --- |
| 1596337 | SOKLIN-DETERGENT POWDER170g | Linked to a master that is not stock-tracked / no longer exists |
| 1596345 | GINO-PEPPEED CHICKEN50g | No master link in EPOS |
| 1596347 | TASTY TOM-TOMATO PASTE55g | Linked to a master that is not stock-tracked / no longer exists |
| 1596348 | DE RICA-TOMATO PASTE55g | Linked to a master that is not stock-tracked / no longer exists |
| 1596403 | KINGS-COOKING MARGARINE250g | Linked to a master that is not stock-tracked / no longer exists |
| 1596851 | CHELSEA LONDON DRY GIN30ml | Linked to a master that is not stock-tracked / no longer exists |
| 1596857 | ACTION BITTERS50ml | Linked to a master that is not stock-tracked / no longer exists |
| 1596866 | LARSOR-FISH SEASONING10g | Linked to a master that is not stock-tracked / no longer exists |
| 1596867 | LARSOR-CHICKEN SEASONING10g | Linked to a master that is not stock-tracked / no longer exists |
| 1596871 | GOOD HEALTH-HOT PEPPE POWDER5g | Linked to a master that is not stock-tracked / no longer exists |
| 1596895 | IRISH SPRING SOAP100g | Linked to a master that is not stock-tracked / no longer exists |
| 1597021 | MAGGI-CRAYFISH SEASONING CUBE600G | No master link in EPOS |
| 1597063 | DELTA-MEDICATED SOAP60g | Linked to a master that is not stock-tracked / no longer exists |
| 1597147 | MAMA LEMON-DISH WASHING LIQUID550ml | No master link in EPOS |
| 1597360 | KEMPS-CREAM CRACKERS17g | No master link in EPOS |
| 1597393 | BESENSE-SANITARY PAD3pcs | Linked to a master that is not stock-tracked / no longer exists |
| 1597396 | MY GIRL-SANITARY PAD3pcs | Linked to a master that is not stock-tracked / no longer exists |
| 1597397 | MY GIRL-SANITARY PAD3pcs*10 | No master link in EPOS |
| 1597471 | CHECKERS CUSTARD-CHOCOLATE FLAVOUR45g | No master link in EPOS |
| 1597477 | QUAKER OATS-WHITE OATS35g | Linked to a master that is not stock-tracked / no longer exists |
| 1608035 | SONIA-CHILLI PEPPE POWDWR5g | Linked to a master that is not stock-tracked / no longer exists |
| 1608246 | McVITIES-DIGESTIVE_ORIGINAL78g | No master link in EPOS |
| 1609771 | FEARLESS ENERGY DRINK BOTTLE 500ML | No master link in EPOS |
| 1609857 | SCHWEPPES CARBONATED DRINK CAN 33CL | No master link in EPOS |
| 1609947 | COCA-COLA ORIGINAL SOFT DRINK BOTTLE 60CL | No master link in EPOS |
| 1610024 | NUTRI-MILK SUPER KIDS DRINK BOTTLE 210ML | No master link in EPOS |
| 1610035 | SPRITE SOFT DRINK BOTTLE 60CL | No master link in EPOS |
| 1610037 | SUPA KOMANDO ENERGY DRINK BOTTLE 50CL | No master link in EPOS |
| 1610039 | SUPA KOMANDO ENERGY DRINK BOTTLE 30CL | No master link in EPOS |
| 1610041 | SCHWEPPES BOTTLE 40CL | No master link in EPOS |
| 1610046 | CWAY NUTRI SOYA DRINK 400ML | No master link in EPOS |
| 1610896 | GUANJING TURMERIC FACE SERUM 30ML | No master link in EPOS |
| 1612993 | CWAY DRINKING WATERBOTTLE 750ML | No master link in EPOS |
| 1613053 | FANTA SOFT DRINK BOTTLE 60CL | No master link in EPOS |
| 1613239 | CHIVITA ACTIVE ZEST 125ML | No master link in EPOS |
| 1613472 | SUNSHINE-AIR FRESHENER BLOCK63g | Linked to a master that is not stock-tracked / no longer exists |
| 1631262 | BOBO MILK DRINK-180ML | No master link in EPOS |
| 1631857 | TOOTHPICK | No master link in EPOS |
| 1632739 | CWAY DRINKING WATERBOTTLE 1.5L | No master link in EPOS |
| 1636461 | CENTER FRUIT2.8g | No master link in EPOS |
| 1637698 | 2SURE-HAND SANITIZER 500ml | No master link in EPOS |
| 1637701 | VISA FRESH DETERGENT POWDER 170g | No master link in EPOS |
| 1637707 | SURE CLEAN DETERGENT POWDER 80g | No master link in EPOS |
| 1637710 | SURE CLEAN DETERGENT POWDER 170g | No master link in EPOS |
| 1639029 | MAMUDA-7TO7 BISCUIT18g | No master link in EPOS |
| 1639032 | OXFORD PRINCE COASTER BISCUIT 20g | No master link in EPOS |
| 1642145 | PREMIER COOL-ANTISEPTIC SOAP 110g | Linked to a master that is not stock-tracked / no longer exists |
| 1679668 | SIMAS COOKING MARGARINE250g | Linked to a master that is not stock-tracked / no longer exists |
| 2019583 | BNC-MOSQUITO SPRAY400ml | No master link in EPOS |
| 2159671 | FAMILIA-ULTRA COLOURED TISSUE PAPER_3PLYpk of 2 MAGIC FLOWER*12 | No master link in EPOS |
| 2362412 | BIRTHDAY NYLON/GIFT NYLON BIG*100 | No master link in EPOS |

### D2. 78 "ghost stock" products to count

At the 30 Sep count, EPOS showed stock for 78 products that nobody could confirm (often with ₦0 cost). Examples are 1,235 packs of CWAY 1.5L water and 406 iPhone screen guards. They were opened in QuickBooks at **0** so we would not book stock that may not exist. If real, the total would be **₦8.02M** at catalogue cost.

- **What to do:** count each one on the shelf and write the count, your name and the date in the xlsx sheet "78 ghost-stock counts". Write 0 if there is none.
- **Why:** any real stock goes into QuickBooks through one approved stock adjustment per product. Nothing goes in without a count.

The 15 largest by value:

| EPOS ID | Product | EPOS showed | Value if real (₦) |
| --- | --- | --- | --- |
| 1632887 | CWAY DRINKING WATERBOTTLE 1.5L*6 | 7415 | 1,839,377 |
| 2264105 | IPHONE PRIVACY SCREEN GUIDE | 406.00000 | 1,321,859 |
| 2264103 | SAMSUNG 25W PD ADAPTER CHARGER | 66.00000 | 1,013,023 |
| 1686797 | PALM OIL 25ltr | 20.00000 | 865,116 |
| 2264055 | IPHONE 16 PRO MAX CHARGER | 37.00000 | 619,535 |
| 2264075 | IPHONE BATTERY PACK WIRELESS | 4.00000 | 267,907 |
| 2264073 | SAMSUNG GALAXY WATCH7 | 4.00000 | 264,599 |
| 2264067 | 120W FAST CHARGE  | 33.00000 | 260,930 |
| 2264076 | IPHONE WATCH | 3.00000 | 223,253 |
| 2264063 | IPHONE CHARGER CORD | 62.00000 | 161,488 |
| 2318185 | WHITE SQUARE FLAT CERAMIC PLATE FLOWER(BIG) | 22.00000 | 139,163 |
| 2264082 | IPHONE AIRPODS PRO | 3.00000 | 136,744 |
| 1635963 | OREO ORIGINAL108g | 96.00000 | 102,698 |
| 2264061 | IPHONE CHARGER HEAD | 7.00000 | 63,814 |
| 1610980 | OLAY CERAMIDE | 3.00000 | 50,233 |

### D3. Pricing review (196 products)

`outputs/final_mapping_2026-10-01/pricing_review.csv` lists products whose EPOS cost or price does not fit the pack size. For example, VITA MILK 300ML single costs 12.7% more than 1/24 of the carton, and HEINEKEN CAN is 10% off. Together they sold about ₦2.9M in 1–25 Sep.

- **What to do:** for each one, check the EPOS **cost price** against the supplier invoice and fix it in EPOS if it is wrong. Two rows (BIG TITUS FISH 1 kg and 0.5 kg) only need the Master Products link confirming, which is covered in B1.
- **Why:** this is not a blocker, because QuickBooks follows what EPOS deducts. But wrong costs give wrong margins in EPOS reports and wrong values when we count stock in.

### D4. Ernest's 19 Sep stock adds: deliveries or recounts?

On 19 Sep, EPOS stock was raised in two ways:

- **"New Stock" by ERNEST JAITTO:** ₦2.35M, mostly spirits and wine. Examples: Hennessy VS +2 cases, Sierra tequila +2.5, Asconi +3.
- **"System" adds:** ₦5.96M. Examples: Titus sardines +5 cases, LARK socks +232.

- **What to do:** Ernest, or whoever knows, says for each block whether it was a **supplier delivery** (if so, which supplier and invoice) or a **recount / correction**. The detail is in `outputs/nora_gaps_2026-09-25/stock_bridge_0916_0925/bridge.csv`.
- **Why:** deliveries need a supplier bill. They go against the September "goods received not invoiced" account, not the stock items. Recounts need nothing more.

### D5. Possible duplicate purchase orders 3828, 3829, 3855, 3861

| PO | Date / supplier | Amount | Looks like a repeat of |
| --- | --- | ---: | --- |
| 3828 | 14 Sep, Nigerian Bottling | ₦7,184,250 | PO 3746 of 4 Sep, already billed (Bill 74504). Same Coke quantity (616) |
| 3829 | 14 Sep, Rite Foods | ₦6,037,960 | PO 3726 of 2 Sep (also not billed yet) |
| 3855 | 17 Sep, Seki Abimbola Kolawole | ₦560,000 | PO 3748 (billed 74502, Segun Endurance). Same lines, different supplier name |
| 3861 | 18 Sep | ₦560,000 | PO 3748 again |

- **What to do:** check each against the supplier's invoice or delivery note and answer "real second delivery" or "duplicate". If it is a duplicate, the PO should be cancelled or reversed in EPOS so its stock comes back out.
- **Why:** these POs are left out of September's goods-received total. If they were real deliveries, we must add them (and a bill will follow). If they were duplicates, EPOS stock is overstated.

### D6. Till sheet for 25, 26 and 29 September

The till breakdown (cash / POS / transfer per day) is blank for these three days.

- **What to do:** fill them in on the till sheet.
- **Why:** about ₦29.2M of takings from 25 Sep to 1 Oct sit in Undeposited Funds in QuickBooks until each day's breakdown exists, so they can be moved to the right bank accounts. Each day is now handled on its own: the missing days do not block later days, and the daily Slack message lists the days still missing.

---

## E. What happens on our side after EPOS is set up

The steps are below, in plain words. **Nothing changes in QuickBooks until Marvin approves it.**

1. **The store tells us a family is finished** (the Done column). Best time: in the evening after the till closes. That way one whole trading day runs on the new setup.
2. **We re-read the EPOS product list** (read-only). The daily run's catalogue check sees that, for example, CHICKEN LAPS 10KG is now stock-tracked and that the 1 kg, 0.5 kg and so on are linked to it.
3. **The products move from "non-stock" to "stock" in QuickBooks.**
   - Today these products post to non-stock items (`AKP-NS-…`). Each sale is just an expense, and no stock is followed.
   - After the change, each family gets **one** stock item, named after the master (`AKP-<master EPOS ID>`, for example `AKP-1696079` for Chicken Laps). It is created **with 0 quantity**.
   - Every child posts to that one item with the amount EPOS takes off. For example, the 1 kg chicken = 1,000 g of the carton, so QuickBooks counts frozen food in grams.
   - The old `AKP-NS-…` items are kept for history and get no new sales. Their QuickBooks names must not clash with the new item. We handle that when we create the items.
   - For the 59 READY families this means 81 products moving from non-stock to stock. The other 100 products are already stock items; only Kelifa Oat's carton count changes, from 1,116 to 1,080. Details are in `ready_mapping_impact.csv`.
4. **The store counts the new masters.** This is a physical count of each new master (cartons, bags or painters, with part-cartons in kg), on the morning before trading, on the day the new setup starts. Write it down with name and date.
5. **One stock adjustment per family.**
   - The count × the master's cost goes into QuickBooks as **one adjustment per family**, dated the first day of the new setup.
   - It is prepared as a draft first, and **Marvin approves it in chat** before it is posted.
   - The accountant agrees the other side of the entry. Our default: stock that was already on hand on 30 Sep goes against the same equity account used at go-live. Stock bought in October on the non-stock items was already expensed, so it comes back off purchases.
6. **We update the product mapping** to the new version, effective from that first day. It must be installed **before 06:00 the next morning**, because that is when the daily run posts the day's sales. From then on, sales of these products take cost from real stock (FIFO).
7. **We check the next morning.** We confirm that the day's sales posted to the new items, that stock went down by the right amounts, and that nothing went negative. Anything odd shows in the daily Slack summary, and the item check flags any sale that still lands on an old item.

**Timing in one line:** EPOS change in the evening (day 0) → count before opening (day 1) → items created at 0, adjustment approved, mapping installed before 06:00 on day 2 → the day-2 run posts day 1's sales on real stock.

If a family is changed in EPOS but our side is not done yet, nothing breaks. Sales keep posting to the old non-stock items until the mapping is updated. EPOS will simply start counting the master's stock earlier than QuickBooks does.

import csv
from decimal import Decimal
from decimal import ROUND_HALF_UP
from pathlib import Path


ROOT = Path("/Users/marvinmokolo/Downloads/AKPONORA Investigation")
MAY_EPOS = Path("/Users/marvinmokolo/Downloads/BookKeeping_2026_06_16_1956 May 1 to May 31.csv")


def money(value: str) -> Decimal:
    text = (value or "").strip()
    if not text or text == "-":
        return Decimal("0")
    negative = text.startswith("-") or text.startswith("(") or "-₦" in text
    text = (
        text.replace("₦", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .strip()
    )
    if not text:
        return Decimal("0")
    amount = Decimal(text)
    return -amount if negative else amount


def qbo_may_sales_receipt_cogs() -> None:
    path = ROOT / "AKPONORA VENTURES LTD_Inventory Valuation Detail May.csv"
    total = Decimal("0")
    rows = 0
    by_number: dict[str, Decimal] = {}
    by_product: dict[str, Decimal] = {}
    watch_numbers = {"SR-20260527-0007", "SR-20260527-0008"}
    watch_totals = {number: Decimal("0") for number in watch_numbers}
    current_product = ""

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] and len(row) > 1 and not row[1]:
                label = row[0]
                if not label.startswith("Total for ") and label not in {
                    "AKPONORA VENTURES LTD",
                    "Inventory Valuation Detail",
                    "May 2026",
                }:
                    current_product = label
            if len(row) > 8 and row[3] == "Sales Receipt":
                amount = money(row[8])
                total += amount
                rows += 1
                by_number[row[4]] = by_number.get(row[4], Decimal("0")) + amount
                by_product[current_product] = by_product.get(current_product, Decimal("0")) + amount
                if row[4] in watch_totals:
                    watch_totals[row[4]] += amount

    print(f"QBO May Sales Receipt inventory cost rows: {rows}")
    print(f"QBO May Sales Receipt inventory cost sum: {total}")
    print(f"QBO May Sales Receipt COGS positive: {-total}")
    print("Top 10 receipts by COGS:")
    for number, amount in sorted(by_number.items(), key=lambda item: item[1])[:10]:
        print(f"  {number}: {-amount}")
    print("Top 10 products by COGS:")
    for product, amount in sorted(by_product.items(), key=lambda item: item[1])[:10]:
        print(f"  {product}: {-amount}")
    print("Watched direct-to-bank receipt COGS:")
    for number, amount in sorted(watch_totals.items()):
        print(f"  {number}: {-amount}")


def qbo_may_transaction_detail_cogs_accounts() -> None:
    path = ROOT / "AKPONORA VENTURES LTD_Transaction Detail by Account May.csv"
    current_account = ""
    by_account: dict[str, Decimal] = {}
    by_account_sales_receipt: dict[str, Decimal] = {}
    all_sales_receipt_by_account: dict[str, Decimal] = {}
    direct_bank_receipts: list[tuple[str, str, str, Decimal]] = []

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] and len(row) > 1 and not row[1]:
                label = row[0]
                if not label.startswith("Total for ") and label not in {
                    "AKPONORA VENTURES LTD",
                    "Transaction Detail by Account",
                    "1-31 May, 2026",
                }:
                    current_account = label
            if len(row) > 8 and row[1] and current_account.startswith("200"):
                amount = money(row[8])
                by_account[current_account] = by_account.get(current_account, Decimal("0")) + amount
                if row[2] == "Sales Receipt":
                    by_account_sales_receipt[current_account] = (
                        by_account_sales_receipt.get(current_account, Decimal("0")) + amount
                    )
            if len(row) > 8 and row[1] and row[2] == "Sales Receipt":
                amount = money(row[8])
                all_sales_receipt_by_account[current_account] = (
                    all_sales_receipt_by_account.get(current_account, Decimal("0")) + amount
                )
                if current_account == "100205 - MONIEPOINT 6397730972":
                    direct_bank_receipts.append((row[1], row[4], row[3], amount))

    print("QBO May transaction detail 200xxx account totals:")
    for account, amount in sorted(by_account.items()):
        print(f"  {account}: {amount}")
    print("QBO May transaction detail 200xxx Sales Receipt-only totals:")
    for account, amount in sorted(by_account_sales_receipt.items()):
        print(f"  {account}: {amount}")
    print("QBO May transaction detail all Sales Receipt account totals over 100k abs:")
    for account, amount in sorted(all_sales_receipt_by_account.items()):
        if abs(amount) >= Decimal("100000"):
            print(f"  {account}: {amount}")
    print("QBO May Sales Receipts posted directly to MONIEPOINT:")
    for date, name, number, amount in direct_bank_receipts:
        print(f"  {date} {number} {name}: {amount}")


def june_11_epos_totals() -> None:
    path = ROOT / "Finalising" / "BookKeeping_2026_06_12_1904.csv"
    qty = Decimal("0")
    net = Decimal("0")
    gross = Decimal("0")
    cost = Decimal("0")
    rows = 0

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            qty += money(row["Quantity"])
            net += money(row["NET Sales"])
            gross += money(row["TOTAL Sales"])
            cost += money(row["Cost Price"]) * money(row["Quantity"])

    print(f"June 11 EPOS rows: {rows}")
    print(f"June 11 EPOS qty: {qty}")
    print(f"June 11 EPOS net sales: {net}")
    print(f"June 11 EPOS gross sales: {gross}")
    print(f"June 11 EPOS expected cost: {cost}")


def may_epos_totals() -> None:
    rows = 0
    qty = Decimal("0")
    net = Decimal("0")
    gross = Decimal("0")
    cost = Decimal("0")
    margin = Decimal("0")
    zero_cost_rows = 0
    zero_cost_net = Decimal("0")
    by_category: dict[str, dict[str, Decimal]] = {}
    min_date = None
    max_date = None

    with MAY_EPOS.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            category = row["Category"] or "(blank)"
            qty_value = money(row["Quantity"])
            net_value = money(row["NET Sales"])
            gross_value = money(row["TOTAL Sales"])
            cost_value = money(row["Cost Price"])
            margin_value = money(row["Margin"])
            qty += qty_value
            net += net_value
            gross += gross_value
            cost += cost_value
            margin += margin_value
            if cost_value == 0 and net_value != 0:
                zero_cost_rows += 1
                zero_cost_net += net_value
            if row["Date/Time"]:
                min_date = row["Date/Time"] if min_date is None else min(min_date, row["Date/Time"])
                max_date = row["Date/Time"] if max_date is None else max(max_date, row["Date/Time"])
            bucket = by_category.setdefault(
                category,
                {"qty": Decimal("0"), "net": Decimal("0"), "gross": Decimal("0"), "cost": Decimal("0")},
            )
            bucket["qty"] += qty_value
            bucket["net"] += net_value
            bucket["gross"] += gross_value
            bucket["cost"] += cost_value

    print(f"May EPOS rows: {rows}")
    print(f"May EPOS first/last text dates: {min_date} / {max_date}")
    print(f"May EPOS qty: {qty}")
    print(f"May EPOS net sales: {net}")
    print(f"May EPOS gross sales: {gross}")
    print(f"May EPOS expected cost: {cost}")
    print(f"May EPOS margin: {margin}")
    print(f"May EPOS zero-cost nonzero-net rows: {zero_cost_rows}")
    print(f"May EPOS zero-cost nonzero-net sales: {zero_cost_net}")
    print("May EPOS category totals:")
    for category, bucket in sorted(by_category.items()):
        print(
            f"  {category}: qty={bucket['qty']} net={bucket['net']} "
            f"gross={bucket['gross']} cost={bucket['cost']}"
        )

    epos_mapped = {
        "200100 - Purchases - Groceries": (
            by_category["CANNED GOOD, COOK OIL, SWALLOW & BAKING"]["cost"]
            + by_category["COOKING SPICES & SEASONINGS"]["cost"]
            + by_category["FROZEN FOODS"]["cost"]
            + by_category["PROVISIONS AND CEREALS"]["cost"]
        ),
        "200200 - Purchases - Drinks": Decimal("0"),
        "200201 - Alcoholic Drinks": by_category["ALCOHOLS & SPIRITS"]["cost"],
        "200202 - Non-Alcoholic Drinks": by_category["DRINKS & BEVERAGES"]["cost"],
        "200300 - Purchases - Non - food items": (
            by_category["COSMETICS AND TOILETRIES"]["cost"]
            + by_category["HOUSEHOLD GOODS & PACKAGING MATERIALS"]["cost"]
            + by_category["STATIONARY AND BOOKSHOP SUPPLIES"]["cost"]
        ),
        "Cost of sales": Decimal("0"),
    }
    qbo_cogs = {
        "200100 - Purchases - Groceries": Decimal("54715527.99"),
        "200200 - Purchases - Drinks": Decimal("59250.00"),
        "200201 - Alcoholic Drinks": Decimal("5922414.43"),
        "200202 - Non-Alcoholic Drinks": Decimal("51262521.35"),
        "200300 - Purchases - Non - food items": Decimal("15168398.30"),
        "Cost of sales": Decimal("317316.42"),
    }
    q = Decimal("0.01")
    print("May journal mapping, using categorized EPOS rows only:")
    print(f"  QBO COGS: {sum(qbo_cogs.values()).quantize(q)}")
    print(f"  EPOS expected cost: {sum(epos_mapped.values()).quantize(q)}")
    print(f"  COGS overstatement: {(sum(qbo_cogs.values()) - sum(epos_mapped.values())).quantize(q)}")
    for account in qbo_cogs:
        correction = (qbo_cogs[account] - epos_mapped[account]).quantize(q, rounding=ROUND_HALF_UP)
        print(
            f"  {account}: qbo={qbo_cogs[account].quantize(q)} "
            f"epos={epos_mapped[account].quantize(q)} correction={correction}"
        )


if __name__ == "__main__":
    qbo_may_sales_receipt_cogs()
    print()
    qbo_may_transaction_detail_cogs_accounts()
    print()
    may_epos_totals()
    print()
    june_11_epos_totals()

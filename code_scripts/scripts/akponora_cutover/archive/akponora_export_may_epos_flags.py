import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


SOURCE = Path("/Users/marvinmokolo/Downloads/BookKeeping_2026_06_16_1956 May 1 to May 31.csv")
OUT_DIR = Path("/Users/marvinmokolo/Developer Projects/OIAT/code-scripts")


def money(value: str) -> Decimal:
    text = (value or "").replace(",", "").strip()
    return Decimal(text) if text else Decimal("0")


def add(bucket: dict, row: dict) -> None:
    bucket["line_count"] += 1
    bucket["quantity"] += money(row["Quantity"])
    bucket["net_sales"] += money(row["NET Sales"])
    bucket["tax"] += money(row["Tax"])
    bucket["total_sales"] += money(row["TOTAL Sales"])
    bucket["cost_price"] += money(row["Cost Price"])
    bucket["margin"] += money(row["Margin"])
    date_time = row["Date/Time"]
    if date_time:
        bucket["first_date_time"] = min(bucket["first_date_time"], date_time) if bucket["first_date_time"] else date_time
        bucket["last_date_time"] = max(bucket["last_date_time"], date_time) if bucket["last_date_time"] else date_time
    if row["Barcode"] and row["Barcode"] not in bucket["barcodes"]:
        bucket["barcodes"].append(row["Barcode"])
    if row["Tender"] and row["Tender"] not in bucket["tenders"]:
        bucket["tenders"].append(row["Tender"])


def new_bucket(row: dict) -> dict:
    return {
        "product": row["Product"],
        "category": row["Category"],
        "line_count": 0,
        "quantity": Decimal("0"),
        "net_sales": Decimal("0"),
        "tax": Decimal("0"),
        "total_sales": Decimal("0"),
        "cost_price": Decimal("0"),
        "margin": Decimal("0"),
        "first_date_time": "",
        "last_date_time": "",
        "barcodes": [],
        "tenders": [],
    }


def write_summary(path: Path, rows: list[dict]) -> None:
    fields = [
        "product",
        "category",
        "line_count",
        "quantity",
        "net_sales",
        "tax",
        "total_sales",
        "cost_price",
        "margin",
        "first_date_time",
        "last_date_time",
        "barcodes",
        "tenders",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = row.copy()
            out["barcodes"] = " | ".join(out["barcodes"])
            out["tenders"] = " | ".join(out["tenders"])
            for key in ["quantity", "net_sales", "tax", "total_sales", "cost_price", "margin"]:
                out[key] = f"{out[key]:.5f}"
            writer.writerow(out)


def main() -> None:
    zero_cost = {}
    zero_net = {}
    zero_cost_lines = 0
    zero_net_lines = 0

    with SOURCE.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            if not row["Product"].strip() or row["Staff"].strip() == "Total:":
                continue
            key = (row["Product"], row["Category"])
            if money(row["Cost Price"]) == 0:
                zero_cost_lines += 1
                zero_cost.setdefault(key, new_bucket(row))
                add(zero_cost[key], row)
            if money(row["NET Sales"]) == 0:
                zero_net_lines += 1
                zero_net.setdefault(key, new_bucket(row))
                add(zero_net[key], row)

    zero_cost_rows = sorted(zero_cost.values(), key=lambda r: (r["net_sales"], r["line_count"]), reverse=True)
    zero_net_rows = sorted(zero_net.values(), key=lambda r: (r["cost_price"], r["line_count"]), reverse=True)

    zero_cost_path = OUT_DIR / "akponora_may_epos_zero_cost_price_items.csv"
    zero_net_path = OUT_DIR / "akponora_may_epos_zero_net_sales_items.csv"
    write_summary(zero_cost_path, zero_cost_rows)
    write_summary(zero_net_path, zero_net_rows)

    print(f"Zero cost price lines: {zero_cost_lines}")
    print(f"Zero cost price unique product/category rows: {len(zero_cost_rows)}")
    print(f"Wrote: {zero_cost_path}")
    print(f"Zero net sales lines: {zero_net_lines}")
    print(f"Zero net sales unique product/category rows: {len(zero_net_rows)}")
    print(f"Wrote: {zero_net_path}")


if __name__ == "__main__":
    main()

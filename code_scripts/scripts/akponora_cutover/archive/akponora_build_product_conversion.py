#!/usr/bin/env python3
"""Build provisional AKPONORA product-conversion data from read-only exports.

This script does not connect to EPOS or QuickBooks and does not write to either
system. It combines a current EPOS catalogue/stock snapshot with the July QBO
baseline, proposes deterministic canonical SKUs, compares stock with the July
EPOS baseline, and blocks ambiguous conversions for human review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DEFAULT_AUGUST_ROOT = Path(
    "/Users/marvinmokolo/Developer Projects/OIAT/MISC/AKPONORA Investigation/"
    "COGS Analysis/As of 22 August 2026"
)
DEFAULT_JULY_ROOT = Path(
    "/Users/marvinmokolo/Developer Projects/OIAT/MISC/AKPONORA Investigation/"
    "COGS Analysis/As of 23 July 2026"
)
DEFAULT_EVIDENCE_ROOT = DEFAULT_AUGUST_ROOT
DEFAULT_BASELINE_ROOT = DEFAULT_JULY_ROOT

TRAILING_MULTIPLIER_RE = re.compile(
    r"\s*\*\s*(\d+)\s*(?:\(\s*(?:pack|pcs?)\s*\))?\s*$", re.IGNORECASE
)
NESTED_NOTATION_RE = re.compile(
    r"(?:"
    r"\([^)]*\d+[^)]*\).*\*\s*\d+"
    r"|\*\s*\d+.*\*\s*\d+"
    r"|\b\d+\s*[xX]\s*\d+\b"
    r")",
    re.IGNORECASE,
)
CONTENT_COUNT_RE = re.compile(r"(?:\b\d+\s*pcs?\b|\(\s*\d+\s*pcs?\s*\))", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")


def natural_key(path: Path) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name))


def clean_text(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "").replace("\ufeff", "").strip())


def normalized_key(value: Any) -> str:
    return clean_text(value).casefold()


def decimal_or_none(value: Any) -> Decimal | None:
    text = clean_text(value).replace("₦", "").replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return -result if negative else result


def number_or_blank(value: Any) -> float | str:
    parsed = decimal_or_none(value)
    return float(parsed) if parsed is not None else ""


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {clean_text(key): clean_text(value) for key, value in row.items() if key is not None}
            for row in csv.DictReader(handle, delimiter=delimiter)
        ]


def read_many(paths: Iterable[Path], delimiter: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(paths, key=natural_key):
        for row in read_csv(path, delimiter=delimiter):
            row["_source_file"] = path.name
            rows.append(row)
    return rows


def trailing_multiplier(value: str) -> int | None:
    match = TRAILING_MULTIPLIER_RE.search(clean_text(value))
    return int(match.group(1)) if match else None


def strip_trailing_multiplier(value: str) -> str:
    return clean_text(TRAILING_MULTIPLIER_RE.sub("", clean_text(value)))


def deterministic_sku(category: str, family: str) -> str:
    digest = hashlib.sha1(f"{normalized_key(category)}|{normalized_key(family)}".encode()).hexdigest()[:10].upper()
    return f"AKP-{digest}"


def inferred_epos_volume(row: dict[str, str]) -> int | str:
    current = decimal_or_none(row.get("MeasuredCurrentStock"))
    volume = decimal_or_none(row.get("CurrentVolume"))
    total = decimal_or_none(row.get("TotalStock"))
    if current is None or volume is None or total is None or volume <= 0:
        return ""
    remainder = total - current
    if remainder <= 0:
        return ""
    candidate = volume / remainder
    integral = candidate.to_integral_value()
    if abs(candidate - integral) <= Decimal("0.0001") and integral > 0:
        return int(integral)
    return ""


def first(rows: list[dict[str, str]]) -> dict[str, str]:
    return rows[0] if rows else {}


def dated_export_path(folder: Path, pattern: str) -> Path:
    matches = sorted(folder.glob(pattern), key=natural_key)
    if not matches:
        raise FileNotFoundError(f"Missing required source export: {folder / pattern}")
    return matches[-1]


def export_date(path: Path) -> str:
    match = re.search(r"(20\d{2})_(\d{2})_(\d{2})", path.name)
    return "-".join(match.groups()) if match else "Unknown"


def build(
    evidence_root: Path,
    qbo_evidence_root: Path,
    baseline_evidence_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    epos_root = evidence_root / "EPOS"
    qbo_root = qbo_evidence_root / "QBO"
    baseline_epos_root = baseline_evidence_root / "EPOS"

    product_paths = list((epos_root / "Product List").glob("ProductList*.csv"))
    stock_paths = list((epos_root / "Stock Management").glob("StockManagement*.csv"))
    levels_path = dated_export_path(epos_root / "Stock Levels", "StockReport*.csv")
    daily_sales_path = dated_export_path(epos_root / "Extra", "DailySales*.csv")
    qbo_path = dated_export_path(qbo_root / "Products and Services List", "ProductsServicesList*.csv")
    baseline_product_paths = list((baseline_epos_root / "Product List").glob("ProductList*.csv"))
    baseline_stock_paths = list((baseline_epos_root / "Stock Management").glob("StockManagement*.csv"))
    baseline_levels_path = dated_export_path(baseline_epos_root / "Stock Levels", "StockReport*.csv")

    required = [levels_path, daily_sales_path, qbo_path, baseline_levels_path]
    if (
        not product_paths
        or not stock_paths
        or not baseline_product_paths
        or not baseline_stock_paths
        or any(not path.exists() for path in required)
    ):
        missing = [str(path) for path in required if not path.exists()]
        if not product_paths:
            missing.append(str(epos_root / "Product List" / "ProductList*.csv"))
        if not stock_paths:
            missing.append(str(epos_root / "Stock Management" / "StockManagement*.csv"))
        if not baseline_product_paths:
            missing.append(str(baseline_epos_root / "Product List" / "ProductList*.csv"))
        if not baseline_stock_paths:
            missing.append(str(baseline_epos_root / "Stock Management" / "StockManagement*.csv"))
        raise FileNotFoundError("Missing required source export(s): " + ", ".join(missing))

    products = read_many(product_paths, delimiter=";")
    stock_rows = read_many(stock_paths, delimiter=";")
    level_rows = read_csv(levels_path)
    daily_sales_rows = read_csv(daily_sales_path)
    qbo_rows = read_csv(qbo_path)
    baseline_products = read_many(baseline_product_paths, delimiter=";")
    baseline_stock_rows = read_many(baseline_stock_paths, delimiter=";")
    baseline_level_rows = read_csv(baseline_levels_path)

    stock_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    levels_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    qbo_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    products_by_name: Counter[str] = Counter()
    barcode_to_names: dict[str, set[str]] = defaultdict(set)
    daily_ids_by_name: dict[str, set[str]] = defaultdict(set)
    baseline_product_by_name: dict[str, dict[str, str]] = {}
    baseline_stock_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    baseline_levels_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in stock_rows:
        stock_by_name[normalized_key(row.get("ProductName"))].append(row)
    for row in level_rows:
        levels_by_name[normalized_key(row.get("Name"))].append(row)
    for row in qbo_rows:
        qbo_by_name[normalized_key(row.get("Product/Service Name"))].append(row)
    for row in daily_sales_rows:
        product_id = clean_text(row.get("ProductID"))
        if product_id:
            daily_ids_by_name[normalized_key(row.get("Name"))].add(product_id)
    for row in products:
        name_key = normalized_key(row.get("Name"))
        products_by_name[name_key] += 1
        for barcode in re.split(r"\s*,\s*", clean_text(row.get("Barcode"))):
            if barcode:
                barcode_to_names[barcode].add(name_key)
    for row in baseline_products:
        baseline_product_by_name.setdefault(normalized_key(row.get("Name")), row)
    for row in baseline_stock_rows:
        baseline_stock_by_name[normalized_key(row.get("ProductName"))].append(row)
    for row in baseline_level_rows:
        baseline_levels_by_name[normalized_key(row.get("Name"))].append(row)

    duplicate_barcodes = {barcode for barcode, names in barcode_to_names.items() if len(names) > 1}
    conversion_rows: list[dict[str, Any]] = []

    for source_index, product in enumerate(products, start=1):
        name = clean_text(product.get("Name"))
        description = clean_text(product.get("Description"))
        category = clean_text(product.get("CategoryId"))
        name_key = normalized_key(name)
        stock_matches = stock_by_name.get(name_key, [])
        level_matches = levels_by_name.get(name_key, [])
        qbo_matches = qbo_by_name.get(name_key, [])
        daily_product_ids = daily_ids_by_name.get(name_key, set())
        stock = first(stock_matches)
        level = first(level_matches)
        qbo = first(qbo_matches)

        name_mult = trailing_multiplier(name)
        desc_mult = trailing_multiplier(description)
        multiplier_conflict = bool(name_mult and desc_mult and name_mult != desc_mult)
        nested_pack = bool(NESTED_NOTATION_RE.search(name))
        content_count = bool(CONTENT_COUNT_RE.search(name))
        family_candidate = strip_trailing_multiplier(name) if name_mult else name
        source_sku = clean_text(product.get("OrderCode")) or clean_text(product.get("ArticleCode"))
        target_sku = source_sku or deterministic_sku(category, family_candidate)

        barcodes = [item for item in re.split(r"\s*,\s*", clean_text(product.get("Barcode"))) if item]
        duplicate_barcode = any(barcode in duplicate_barcodes for barcode in barcodes)
        current_stock = number_or_blank(stock.get("CurrentStock") or level.get("MeasuredCurrentStock"))
        current_volume = number_or_blank(level.get("CurrentVolume"))
        total_stock = number_or_blank(level.get("TotalStock"))
        if total_stock == "" and current_stock != "":
            total_stock = current_stock
        cost_inc = number_or_blank(
            stock.get("CostPriceIncTax")
            or product.get("CostPriceIncTax")
            or level.get("MeasuredCostPrice")
        )
        baseline_stock = first(baseline_stock_by_name.get(name_key, []))
        baseline_level = first(baseline_levels_by_name.get(name_key, []))
        baseline_total_stock = number_or_blank(
            baseline_level.get("TotalStock") or baseline_stock.get("CurrentStock")
        )
        baseline_cost_inc = number_or_blank(
            baseline_stock.get("CostPriceIncTax")
            or baseline_product_by_name.get(name_key, {}).get("CostPriceIncTax")
            or baseline_level.get("MeasuredCostPrice")
        )

        issues: list[str] = []
        decision_reasons: list[str] = []
        if multiplier_conflict:
            issues.append("NAME_DESCRIPTION_MULTIPLIER_CONFLICT")
            decision_reasons.append("Name and Description disagree on the outer multiplier")
        if nested_pack:
            issues.append("NESTED_PACK_REVIEW")
            decision_reasons.append("More than one physical packaging level is present")
        if content_count and name_mult:
            issues.append("CONTENT_VS_STOCK_UNIT_REVIEW")
            decision_reasons.append("Inner pieces may be pack content rather than separately tracked stock")
        if duplicate_barcode:
            issues.append("BARCODE_SHARED_ACROSS_PRODUCTS")
            decision_reasons.append("Barcode cannot safely identify this product/unit by itself")
        if len(stock_matches) > 1:
            issues.append("DUPLICATE_STOCK_NAME")
            decision_reasons.append("More than one Stock Management row has the same name")
        if len(level_matches) > 1:
            issues.append("DUPLICATE_STOCK_LEVEL_NAME")
            decision_reasons.append("More than one Stock Levels row has the same name")
        if products_by_name[name_key] > 1:
            issues.append("DUPLICATE_PRODUCT_NAME")
            decision_reasons.append("More than one Product List row has the same name")
        if isinstance(total_stock, float) and total_stock < 0:
            issues.append("NEGATIVE_STOCK")
            decision_reasons.append("Stock must be corrected or explicitly approved")
        if isinstance(total_stock, float) and total_stock > 0 and (cost_inc == "" or cost_inc <= 0):
            issues.append("MISSING_COST_WITH_POSITIVE_STOCK")
            decision_reasons.append("Positive stock needs a verified cost")
        if len(qbo_matches) > 1:
            issues.append("MULTIPLE_QBO_EXACT_NAME_MATCHES")
            decision_reasons.append("QBO exact-name mapping is not unique")
        if len(daily_product_ids) > 1:
            issues.append("MULTIPLE_EPOS_PRODUCT_IDS_FOR_NAME")
            decision_reasons.append("Daily Sales exposes more than one EPOS Product ID for this name")

        # NEGATIVE_STOCK is flagged but not blocking: treat as zero unless staff
        # confirm stock exists. BARCODE_SHARED is informational only.
        severe = bool(
            set(issues)
            & {
                "NAME_DESCRIPTION_MULTIPLIER_CONFLICT",
                "NESTED_PACK_REVIEW",
                "CONTENT_VS_STOCK_UNIT_REVIEW",
                "DUPLICATE_STOCK_NAME",
                "DUPLICATE_STOCK_LEVEL_NAME",
                "DUPLICATE_PRODUCT_NAME",
                "MISSING_COST_WITH_POSITIVE_STOCK",
                "MULTIPLE_QBO_EXACT_NAME_MATCHES",
                "MULTIPLE_EPOS_PRODUCT_IDS_FOR_NAME",
            }
        )

        proposed_full_multiplier: int | str
        proposed_volume_multiplier: int | str
        if severe:
            proposed_full_multiplier = ""
            proposed_volume_multiplier = ""
        elif name_mult:
            proposed_full_multiplier = name_mult
            proposed_volume_multiplier = 1
        else:
            proposed_full_multiplier = 1
            proposed_volume_multiplier = 1

        qbo_qty = number_or_blank(qbo.get("Quantity on hand"))
        qbo_cost = number_or_blank(qbo.get("Cost"))
        qbo_type = clean_text(qbo.get("Item type"))
        qbo_name = clean_text(qbo.get("Product/Service Name"))

        conversion_rows.append(
            {
                "Row ID": source_index,
                "EPOS Product ID": next(iter(daily_product_ids)) if len(daily_product_ids) == 1 else "",
                "EPOS Existing SKU": source_sku,
                "EPOS Name": name,
                "EPOS Description": description,
                "Category": category,
                "Barcode": clean_text(product.get("Barcode")),
                "Stock Tracked": clean_text(product.get("IsStockTracked")).lower() == "yes",
                "Sell on Till": clean_text(product.get("SellOnTill")).lower() == "yes",
                "EPOS Current Stock": current_stock,
                "EPOS Current Volume": current_volume,
                "EPOS Total Stock": total_stock,
                "EPOS Volume Denominator": inferred_epos_volume(level),
                "EPOS Cost Inc Tax": cost_inc,
                "Baseline Total Stock": baseline_total_stock,
                "Baseline Cost Inc Tax": baseline_cost_inc,
                "Name Trailing Multiplier": name_mult or "",
                "Description Trailing Multiplier": desc_mult or "",
                "Family Candidate": family_candidate,
                "Canonical Family SKU": target_sku,
                "Proposed Canonical Unit": "Needs staff decision" if severe else "Each / sellable unit",
                "Proposed Full Unit Multiplier": proposed_full_multiplier,
                "Proposed Volume Multiplier": proposed_volume_multiplier,
                "Pipeline Status": "BLOCK" if severe else "PROVISIONAL",
                "Issue Codes": "; ".join(dict.fromkeys(issues)),
                "Staff Decision Needed": "; ".join(dict.fromkeys(decision_reasons)),
                "QBO Exact Match": bool(qbo_matches),
                "QBO Product Name": qbo_name,
                "QBO Item Type": qbo_type,
                "QBO SKU": clean_text(qbo.get("SKU")),
                "QBO Qty on Hand": qbo_qty,
                "QBO Cost": qbo_cost,
                "QBO Income Account": clean_text(qbo.get("Income Account")),
                "QBO Expense Account": clean_text(qbo.get("Expense Account")),
                "QBO Inventory Asset Account": clean_text(qbo.get("Inventory asset account")),
                "Target QBO Item Type": "Non-inventory",
                "Target QBO Name": family_candidate if not severe else "",
                "Target QBO SKU": target_sku if not severe else "",
                "Staff Approved Canonical Unit": "",
                "Staff Approved Full Multiplier": "",
                "Staff Approved Volume Multiplier": "",
                "Verified Cost per Full Unit": "",
                "Cost Source": "",
                "Review Status": "Needs review" if severe else "Provisional",
                "Owner": "",
                "Review Notes": "",
                "Source Product File": clean_text(product.get("_source_file")),
                "Staff Approved Sale Multiplier": "",
                "Effective Date": "",
                "Approved By": "",
            }
        )

    current_first_by_name: dict[str, dict[str, Any]] = {}
    for row in conversion_rows:
        current_first_by_name.setdefault(normalized_key(row["EPOS Name"]), row)

    comparison_rows: list[dict[str, Any]] = []
    all_name_keys = set(baseline_product_by_name) | set(current_first_by_name)
    for name_key in sorted(all_name_keys):
        current = current_first_by_name.get(name_key)
        baseline_product = baseline_product_by_name.get(name_key, {})
        if current is None:
            baseline_stock = first(baseline_stock_by_name.get(name_key, []))
            baseline_level = first(baseline_levels_by_name.get(name_key, []))
            comparison_rows.append(
                {
                    "EPOS Name": clean_text(baseline_product.get("Name")),
                    "Category": clean_text(baseline_product.get("CategoryId")),
                    "Baseline Total Stock": number_or_blank(
                        baseline_level.get("TotalStock") or baseline_stock.get("CurrentStock")
                    ),
                    "Current Total Stock": "",
                    "Stock Change": "",
                    "Baseline Cost Inc Tax": number_or_blank(
                        baseline_stock.get("CostPriceIncTax")
                        or baseline_product.get("CostPriceIncTax")
                        or baseline_level.get("MeasuredCostPrice")
                    ),
                    "Current Cost Inc Tax": "",
                    "Cost Change": "",
                    "Classification": "REMOVED_PRODUCT",
                    "Current Pipeline Status": "REMOVED",
                    "Current Issue Codes": "",
                }
            )
            continue

        baseline_total = current["Baseline Total Stock"]
        current_total = current["EPOS Total Stock"]
        baseline_cost = current["Baseline Cost Inc Tax"]
        current_cost = current["EPOS Cost Inc Tax"]
        stock_change = (
            current_total - baseline_total
            if isinstance(current_total, float) and isinstance(baseline_total, float)
            else ""
        )
        cost_change = (
            current_cost - baseline_cost
            if isinstance(current_cost, float) and isinstance(baseline_cost, float)
            else ""
        )
        if name_key not in baseline_product_by_name:
            classification = "NEW_PRODUCT"
        elif isinstance(baseline_total, float) and isinstance(current_total, float):
            if baseline_total < 0 <= current_total:
                classification = "NEGATIVE_RESOLVED"
            elif baseline_total >= 0 > current_total:
                classification = "NEW_NEGATIVE"
            elif current_total != baseline_total or current_cost != baseline_cost:
                classification = "CHANGED"
            else:
                classification = "UNCHANGED"
        else:
            classification = "NO_COMPARABLE_STOCK"
        comparison_rows.append(
            {
                "EPOS Name": current["EPOS Name"],
                "Category": current["Category"],
                "Baseline Total Stock": baseline_total,
                "Current Total Stock": current_total,
                "Stock Change": stock_change,
                "Baseline Cost Inc Tax": baseline_cost,
                "Current Cost Inc Tax": current_cost,
                "Cost Change": cost_change,
                "Classification": classification,
                "Current Pipeline Status": current["Pipeline Status"],
                "Current Issue Codes": current["Issue Codes"],
            }
        )

    qbo_export_rows = [
        {
            "QBO Product Name": clean_text(row.get("Product/Service Name")),
            "Quantity on Hand": number_or_blank(row.get("Quantity on hand")),
            "Item Type": clean_text(row.get("Item type")),
            "Category": clean_text(row.get("Category")),
            "SKU": clean_text(row.get("SKU")),
            "Price": number_or_blank(row.get("Price")),
            "Cost": number_or_blank(row.get("Cost")),
            "Income Account": clean_text(row.get("Income Account")),
            "Expense Account": clean_text(row.get("Expense Account")),
            "Inventory Asset Account": clean_text(row.get("Inventory asset account")),
        }
        for row in qbo_rows
    ]

    issue_counts: Counter[str] = Counter()
    for row in conversion_rows:
        for issue in str(row["Issue Codes"]).split("; "):
            if issue:
                issue_counts[issue] += 1

    sku_families: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in conversion_rows:
        sku_families[str(row["Canonical Family SKU"])].add(
            (normalized_key(row["Category"]), normalized_key(row["Family Candidate"]))
        )
    generated_sku_collisions = sum(len(families) > 1 for families in sku_families.values())

    summary = {
        "as_of": export_date(levels_path),
        "baseline_as_of": export_date(baseline_levels_path),
        "built_on": "2026-08-22",
        "evidence_root": str(evidence_root),
        "qbo_evidence_root": str(qbo_evidence_root),
        "baseline_evidence_root": str(baseline_evidence_root),
        "product_file_count": len(product_paths),
        "stock_management_file_count": len(stock_paths),
        "product_rows": len(conversion_rows),
        "unique_product_names": len(products_by_name),
        "stock_management_rows": len(stock_rows),
        "stock_level_rows": len(level_rows),
        "daily_sales_rows": len(daily_sales_rows),
        "qbo_product_rows": len(qbo_rows),
        "blocked_rows": sum(row["Pipeline Status"] == "BLOCK" for row in conversion_rows),
        "provisional_rows": sum(row["Pipeline Status"] == "PROVISIONAL" for row in conversion_rows),
        "negative_stock_rows": issue_counts["NEGATIVE_STOCK"],
        "missing_cost_positive_stock_rows": issue_counts["MISSING_COST_WITH_POSITIVE_STOCK"],
        "nested_pack_rows": issue_counts["NESTED_PACK_REVIEW"],
        "name_description_conflicts": issue_counts["NAME_DESCRIPTION_MULTIPLIER_CONFLICT"],
        "shared_barcode_rows": issue_counts["BARCODE_SHARED_ACROSS_PRODUCTS"],
        "shared_barcode_values": len(duplicate_barcodes),
        "exact_qbo_matches": sum(bool(row["QBO Exact Match"]) for row in conversion_rows),
        "qbo_negative_quantity_rows": sum(
            isinstance(row["Quantity on Hand"], float) and row["Quantity on Hand"] < 0
            for row in qbo_export_rows
        ),
        "source_skus_present": sum(bool(row["EPOS Existing SKU"]) for row in conversion_rows),
        "source_product_ids_present": sum(bool(row["EPOS Product ID"]) for row in conversion_rows),
        "blank_epos_names": sum(not bool(row["EPOS Name"]) for row in conversion_rows),
        "generated_sku_collisions": generated_sku_collisions,
        "new_products": sum(row["Classification"] == "NEW_PRODUCT" for row in comparison_rows),
        "removed_products": sum(row["Classification"] == "REMOVED_PRODUCT" for row in comparison_rows),
        "negative_stock_resolved": sum(
            row["Classification"] == "NEGATIVE_RESOLVED" for row in comparison_rows
        ),
        "new_negative_stock": sum(row["Classification"] == "NEW_NEGATIVE" for row in comparison_rows),
        "changed_products": sum(row["Classification"] == "CHANGED" for row in comparison_rows),
        "issue_counts": dict(sorted(issue_counts.items())),
    }
    return conversion_rows, qbo_export_rows, comparison_rows, summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def exception_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    name = str(row.get("EPOS Name", "")).upper()
    issues = str(row.get("Issue Codes", ""))
    if "BACKWOODS" in name:
        rank = 0
    elif "NESTED_PACK_REVIEW" in issues:
        rank = 1
    elif "NAME_DESCRIPTION_MULTIPLIER_CONFLICT" in issues:
        rank = 2
    elif "MISSING_COST_WITH_POSITIVE_STOCK" in issues:
        rank = 3
    elif "NEGATIVE_STOCK" in issues:
        rank = 4
    else:
        rank = 5
    return rank, name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--qbo-evidence-root", type=Path, default=DEFAULT_JULY_ROOT)
    parser.add_argument("--baseline-evidence-root", type=Path, default=DEFAULT_BASELINE_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    conversion_rows, qbo_rows, comparison_rows, summary = build(
        args.evidence_root,
        args.qbo_evidence_root,
        args.baseline_evidence_root,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    exceptions = sorted(
        (row for row in conversion_rows if row["Pipeline Status"] == "BLOCK"),
        key=exception_sort_key,
    )
    write_csv(output_dir / "akponora_product_conversion.csv", conversion_rows)
    write_csv(output_dir / "akponora_product_exceptions.csv", exceptions)
    write_csv(output_dir / "akponora_july_to_august_changes.csv", comparison_rows)
    (output_dir / "conversion_rows.json").write_text(json.dumps(conversion_rows, indent=2), encoding="utf-8")
    (output_dir / "qbo_rows.json").write_text(json.dumps(qbo_rows, indent=2), encoding="utf-8")
    (output_dir / "comparison_rows.json").write_text(json.dumps(comparison_rows, indent=2), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

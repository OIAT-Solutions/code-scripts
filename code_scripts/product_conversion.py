"""Strict, approval-driven EPOS product conversion for AKPONORA sales.

The legacy transform infers a multiplier from a trailing ``*N`` product name.
That is unsafe for nested packs such as ``(5X1)*40``. This module instead reads
an approved Product Conversion List and fails closed when a sales row cannot be
resolved unambiguously.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


SPACE_RE = re.compile(r"\s+")
APPROVED_STATUS = "approved"


def normalize(value: Any) -> str:
    return clean(value).casefold()


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return SPACE_RE.sub(" ", str(value or "").strip())


def parse_positive_decimal(value: Any, field: str, row_number: int) -> Decimal:
    try:
        result = Decimal(clean(value).replace(",", ""))
    except InvalidOperation as exc:
        raise MappingValidationError(f"Row {row_number}: {field} must be a number") from exc
    if result <= 0:
        raise MappingValidationError(f"Row {row_number}: {field} must be greater than zero")
    return result


def parse_effective_date(value: Any, row_number: int) -> date:
    raw = clean(value)
    if not raw:
        raise MappingValidationError(f"Row {row_number}: Effective Date is required")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise MappingValidationError(
            f"Row {row_number}: Effective Date must use YYYY-MM-DD"
        ) from exc


class MappingValidationError(ValueError):
    """The conversion file is structurally unsafe to activate."""


class ProductResolutionError(ValueError):
    """A sales product has no unique, effective, approved conversion."""

    def __init__(self, code: str, product_name: str, detail: str):
        self.code = code
        self.product_name = product_name
        self.detail = detail
        super().__init__(f"{code}: {product_name}: {detail}")


ALLOWED_TARGET_TYPES = {
    "inventory": "Inventory",
    "noninventory": "NonInventory",
    "service": "Service",
}


def canonical_target_type(value: Any) -> str:
    normalized = re.sub(r"[\s_-]", "", clean(value).casefold())
    return ALLOWED_TARGET_TYPES.get(normalized, "")


@dataclass(frozen=True)
class ProductConversionRule:
    row_id: str
    epos_product_id: str
    epos_sku: str
    epos_name: str
    target_qbo_type: str
    target_qbo_name: str
    target_qbo_sku: str
    target_qbo_item_id: str
    sale_multiplier: Decimal
    effective_date: date
    approved_by: str


class ProductConversionRegistry:
    """Validated lookup indexes for approved conversion rows."""

    def __init__(
        self,
        rules: Iterable[ProductConversionRule],
        known_names: Iterable[str],
        allow_name_fallback: bool = True,
    ) -> None:
        self.rules = list(rules)
        self.allow_name_fallback = allow_name_fallback
        self.known_names = {normalize(name) for name in known_names if clean(name)}
        self.by_product_id = self._unique_index("EPOS Product ID", self.rules, lambda r: r.epos_product_id)
        self.by_sku = self._unique_index("EPOS SKU", self.rules, lambda r: r.epos_sku)
        self.by_name = self._unique_index("EPOS Name", self.rules, lambda r: r.epos_name)
        self.approved_target_item_ids = {
            clean(rule.target_qbo_item_id) for rule in self.rules if clean(rule.target_qbo_item_id)
        }
        self._validate_target_pairs(self.rules)

    @staticmethod
    def _unique_index(label: str, rules: list[ProductConversionRule], getter) -> dict[str, ProductConversionRule]:
        grouped: dict[str, list[ProductConversionRule]] = {}
        for rule in rules:
            key = normalize(getter(rule))
            if key:
                grouped.setdefault(key, []).append(rule)
        duplicates = {key: rows for key, rows in grouped.items() if len(rows) > 1}
        if duplicates:
            sample_key, sample_rows = next(iter(duplicates.items()))
            row_ids = ", ".join(row.row_id or "?" for row in sample_rows)
            raise MappingValidationError(
                f"Approved mapping has duplicate {label} '{sample_key}' on rows {row_ids}"
            )
        return {key: rows[0] for key, rows in grouped.items()}

    @staticmethod
    def _validate_target_pairs(rules: list[ProductConversionRule]) -> None:
        sku_by_name: dict[str, set[str]] = {}
        name_by_sku: dict[str, set[str]] = {}
        for rule in rules:
            target_name = normalize(rule.target_qbo_name)
            target_sku = normalize(rule.target_qbo_sku)
            sku_by_name.setdefault(target_name, set()).add(target_sku)
            name_by_sku.setdefault(target_sku, set()).add(target_name)
        conflicting_name = next((key for key, values in sku_by_name.items() if len(values) > 1), None)
        if conflicting_name:
            raise MappingValidationError(
                f"Approved target QBO name '{conflicting_name}' is assigned more than one SKU"
            )
        conflicting_sku = next((key for key, values in name_by_sku.items() if len(values) > 1), None)
        if conflicting_sku:
            raise MappingValidationError(
                f"Approved target QBO SKU '{conflicting_sku}' is assigned more than one name"
            )

    @classmethod
    def from_csv(cls, path: str | Path, allow_name_fallback: bool = True) -> "ProductConversionRegistry":
        source = Path(path)
        if not source.exists():
            raise MappingValidationError(f"Product conversion file not found: {source}")

        approved: list[ProductConversionRule] = []
        known_names: list[str] = []
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {
                "EPOS Name",
                "Pipeline Status",
                "Review Status",
                "Target QBO Item Type",
                "Target QBO Name",
                "Target QBO SKU",
                "Staff Approved Sale Multiplier",
                "Effective Date",
                "Approved By",
            }
            missing = sorted(required - set(reader.fieldnames or []))
            if missing:
                raise MappingValidationError(
                    "Product conversion file is missing required column(s): " + ", ".join(missing)
                )

            for row_number, row in enumerate(reader, start=2):
                epos_name = clean(row.get("EPOS Name"))
                if epos_name:
                    known_names.append(epos_name)
                if normalize(row.get("Review Status")) != APPROVED_STATUS:
                    continue
                if normalize(row.get("Pipeline Status")) == "block":
                    raise MappingValidationError(
                        f"Row {row_number}: BLOCK row cannot be marked Approved"
                    )

                target_name = clean(row.get("Target QBO Name"))
                target_sku = clean(row.get("Target QBO SKU"))
                target_type = canonical_target_type(row.get("Target QBO Item Type"))
                target_item_id = clean(row.get("Target QBO Item Id"))
                approved_by = clean(row.get("Approved By"))
                if not epos_name or not target_name or not target_sku or not approved_by:
                    raise MappingValidationError(
                        f"Row {row_number}: approved row requires EPOS Name, target QBO name/SKU and Approved By"
                    )
                if not target_type:
                    raise MappingValidationError(
                        f"Row {row_number}: Target QBO Item Type must be Inventory, Non-inventory, or Service"
                    )
                approved.append(
                    ProductConversionRule(
                        row_id=clean(row.get("Row ID")) or str(row_number),
                        epos_product_id=clean(row.get("EPOS Product ID")),
                        epos_sku=clean(row.get("EPOS Existing SKU")),
                        epos_name=epos_name,
                        target_qbo_type=target_type,
                        target_qbo_name=target_name,
                        target_qbo_sku=target_sku,
                        target_qbo_item_id=target_item_id,
                        sale_multiplier=parse_positive_decimal(
                            row.get("Staff Approved Sale Multiplier"),
                            "Staff Approved Sale Multiplier",
                            row_number,
                        ),
                        effective_date=parse_effective_date(row.get("Effective Date"), row_number),
                        approved_by=approved_by,
                    )
                )
        return cls(approved, known_names, allow_name_fallback=allow_name_fallback)

    def resolve(
        self,
        *,
        product_name: Any,
        product_id: Any = "",
        sku: Any = "",
        transaction_date: date | None = None,
    ) -> ProductConversionRule:
        name = clean(product_name)
        product_id_key = normalize(product_id)
        sku_key = normalize(sku)
        name_key = normalize(name)

        if product_id_key:
            rule = self.by_product_id.get(product_id_key)
            if rule is None:
                raise ProductResolutionError(
                    "UNMAPPED_PRODUCT_ID", name, f"EPOS Product ID {clean(product_id)} is not approved"
                )
        elif sku_key:
            rule = self.by_sku.get(sku_key)
            if rule is None:
                raise ProductResolutionError("UNMAPPED_SKU", name, f"EPOS SKU {clean(sku)} is not approved")
        elif self.allow_name_fallback:
            rule = self.by_name.get(name_key)
            if rule is None:
                code = "NOT_APPROVED" if name_key in self.known_names else "UNMAPPED_PRODUCT"
                raise ProductResolutionError(code, name, "No approved exact-name conversion")
        else:
            raise ProductResolutionError(
                "MISSING_STABLE_IDENTIFIER", name, "Sales row has neither EPOS Product ID nor SKU"
            )

        if transaction_date is not None and transaction_date < rule.effective_date:
            raise ProductResolutionError(
                "MAPPING_NOT_EFFECTIVE",
                name,
                f"Approved conversion begins {rule.effective_date.isoformat()}",
            )
        return rule


def _first_present(row: pd.Series, columns: Iterable[str]) -> Any:
    for column in columns:
        if column in row.index and clean(row.get(column)):
            return row.get(column)
    return ""


def _row_date(row: pd.Series) -> date | None:
    raw = _first_present(row, ["Date/Time", "Date", "Transaction Date"])
    text = clean(raw)
    if not text:
        return None
    if re.match(r"^20\d{2}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    for date_format in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date()


def apply_product_conversion_to_sales(
    sales: pd.DataFrame,
    registry: ProductConversionRegistry,
    *,
    catch_all_name: str = "",
    fail_closed_from: date | None = None,
) -> pd.DataFrame:
    """Return a mapped copy, optionally routing unresolved products to a catch-all.

    Invalid quantities always block the batch. Product resolution failures can
    use one pre-created QBO Non-inventory item so an unmapped till product does
    not stop daily sales — but only for TxnDate before ``fail_closed_from``.
    From that date, unmapped products fail closed (no catch-all, no auto-create).
    The original EPOS name is retained in the line description for later mapping review.
    """
    if "Product" not in sales.columns or "Quantity" not in sales.columns:
        raise ValueError("Sales data requires Product and Quantity columns")

    mapped = sales.copy()
    mapped["_EPOS Product Original"] = mapped["Product"]
    mapped["_QBO Line Description"] = mapped.get(
        "Category", pd.Series([""] * len(mapped), index=mapped.index)
    ).map(clean)
    failures: list[ProductResolutionError] = []
    target_names: list[str] = []
    target_quantities: list[float] = []
    line_descriptions: list[str] = []
    fallback_flags: list[bool] = []
    fallback_name = clean(catch_all_name)
    if isinstance(fail_closed_from, str):
        fail_closed_from = datetime.strptime(fail_closed_from[:10], "%Y-%m-%d").date()

    for _, row in mapped.iterrows():
        quantity = Decimal("0")
        try:
            quantity = Decimal(clean(row.get("Quantity")) or "0")
            rule = registry.resolve(
                product_name=row.get("Product"),
                product_id=_first_present(
                    row,
                    ["ProductId", "ProductID", "Product ID", "EPOS Product ID"],
                ),
                sku=_first_present(row, ["SKU", "OrderCode", "Order Code", "ArticleCode"]),
                transaction_date=_row_date(row),
            )
            target_names.append(rule.target_qbo_name)
            target_quantities.append(float(quantity * rule.sale_multiplier))
            line_descriptions.append(clean(row.get("Category")))
            fallback_flags.append(False)
        except ProductResolutionError as exc:
            txn_date = _row_date(row)
            allow_catch_all = bool(fallback_name) and (
                fail_closed_from is None
                or (txn_date is not None and txn_date < fail_closed_from)
            )
            if allow_catch_all:
                target_names.append(fallback_name)
                target_quantities.append(float(quantity))
                line_descriptions.append(f"Unmapped EPOS product: {clean(row.get('Product'))}")
                fallback_flags.append(True)
            else:
                failures.append(exc)
                target_names.append("")
                target_quantities.append(0.0)
                line_descriptions.append(clean(row.get("Category")))
                fallback_flags.append(False)
        except InvalidOperation:
            failures.append(
                ProductResolutionError("INVALID_QUANTITY", clean(row.get("Product")), clean(row.get("Quantity")))
            )
            target_names.append("")
            target_quantities.append(0.0)
            line_descriptions.append(clean(row.get("Category")))
            fallback_flags.append(False)

    if failures:
        counts = Counter(error.code for error in failures)
        examples = "; ".join(str(error) for error in failures[:5])
        summary = ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
        raise ProductResolutionError(
            "PRODUCT_CONVERSION_BLOCKED",
            f"{len(failures)} sales row(s)",
            f"{summary}. Examples: {examples}",
        )

    mapped["Product"] = target_names
    mapped["Quantity"] = target_quantities
    mapped["_QBO Line Description"] = line_descriptions
    mapped["_Product Conversion Fallback"] = fallback_flags
    return mapped


def audit_sales(
    sales: pd.DataFrame,
    registry: ProductConversionRegistry,
) -> list[dict[str, Any]]:
    aggregate: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    resolution_cache: dict[tuple[str, str, str, date | None], tuple[str, str, str, str, Decimal]] = {}
    for _, row in sales.iterrows():
        name = clean(row.get("Product"))
        product_id = clean(_first_present(row, ["ProductID", "Product ID", "EPOS Product ID"]))
        sku = clean(_first_present(row, ["SKU", "OrderCode", "Order Code", "ArticleCode"]))
        barcode = clean(row.get("Barcode"))
        transaction_date = _row_date(row)
        resolution_key = (normalize(name), normalize(product_id), normalize(sku), transaction_date)
        resolution = resolution_cache.get(resolution_key)
        if resolution is None:
            try:
                rule = registry.resolve(
                    product_name=name,
                    product_id=product_id,
                    sku=sku,
                    transaction_date=transaction_date,
                )
                resolution = (
                    "READY",
                    "",
                    rule.target_qbo_name,
                    rule.target_qbo_sku,
                    rule.sale_multiplier,
                )
            except ProductResolutionError as exc:
                resolution = ("BLOCK", f"{exc.code}: {exc.detail}", "", "", Decimal("0"))
            resolution_cache[resolution_key] = resolution
        status, reason, target_name, target_sku, multiplier = resolution

        key = (name, product_id, sku, barcode, status + reason)
        item = aggregate.setdefault(
            key,
            {
                "EPOS Product": name,
                "EPOS Product ID": product_id,
                "EPOS SKU": sku,
                "Barcode": barcode,
                "Status": status,
                "Reason": reason,
                "Target QBO Name": target_name,
                "Target QBO SKU": target_sku,
                "Sale Multiplier": float(multiplier) if multiplier else "",
                "Sales Lines": 0,
                "Source Quantity": 0.0,
                "Canonical Quantity": 0.0,
            },
        )
        try:
            source_quantity = float(clean(row.get("Quantity")).replace(",", "") or 0)
        except ValueError:
            source_quantity = 0.0
        item["Sales Lines"] += 1
        item["Source Quantity"] += source_quantity
        if status == "READY":
            item["Canonical Quantity"] += source_quantity * float(multiplier)
    return sorted(aggregate.values(), key=lambda item: (item["Status"] != "BLOCK", item["EPOS Product"]))


def write_audit(path: str | Path, rows: list[dict[str, Any]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["EPOS Product", "Status", "Reason"]
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


CATALOG_FIELDS = [
    "Target QBO Name",
    "Target QBO SKU",
    "Desired Item Type",
    "Existing QBO Name",
    "Existing QBO SKU",
    "Existing Item Type",
    "Status",
    "Reason",
    "Mapped EPOS Products",
]


def build_qbo_catalog_plan(
    registry: ProductConversionRegistry,
    qbo_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a read-only target catalogue plan; never create or update QBO items."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    by_sku: dict[str, list[dict[str, Any]]] = {}
    for row in qbo_rows:
        name = clean(row.get("Product/Service Name") or row.get("Name"))
        sku = clean(row.get("SKU"))
        if name:
            by_name.setdefault(normalize(name), []).append(row)
        if sku:
            by_sku.setdefault(normalize(sku), []).append(row)

    targets: dict[tuple[str, str], list[ProductConversionRule]] = {}
    for rule in registry.rules:
        targets.setdefault(
            (normalize(rule.target_qbo_name), normalize(rule.target_qbo_sku)), []
        ).append(rule)

    plan: list[dict[str, Any]] = []
    for (_, _), rules in sorted(targets.items(), key=lambda item: item[0]):
        rule = rules[0]
        name_matches = by_name.get(normalize(rule.target_qbo_name), [])
        sku_matches = by_sku.get(normalize(rule.target_qbo_sku), [])
        existing: dict[str, Any] = name_matches[0] if len(name_matches) == 1 else {}
        status = ""
        reason = ""
        existing_id = clean(
            existing.get("Id")
            or existing.get("Item Id")
            or existing.get("QBO Item Id")
        )
        if len(name_matches) > 1:
            status = "BLOCK_DUPLICATE_QBO_NAME"
            reason = "More than one QBO item has the approved target name"
        elif existing:
            item_type = clean(existing.get("Item type") or existing.get("Type"))
            normalized_type = re.sub(r"[\s_-]", "", item_type.casefold())
            existing_sku = clean(existing.get("SKU") or existing.get("Sku"))
            desired = re.sub(r"[\s_-]", "", rule.target_qbo_type.casefold())
            if desired == "inventory":
                approved_id = clean(rule.target_qbo_item_id)
                if normalized_type != "inventory":
                    status = "BLOCK_WRONG_TYPE"
                    reason = f"Existing exact-name item is Type={item_type or 'Unknown'}; Oct path needs Inventory"
                elif not approved_id:
                    status = "BLOCK_LEGACY_INVENTORY_NAME_COLLISION"
                    reason = (
                        f"Existing Inventory Id={existing_id or 'unknown'} is not an approved new catalogue Id"
                    )
                elif existing_id and existing_id != approved_id:
                    status = "BLOCK_LEGACY_INVENTORY_NAME_COLLISION"
                    reason = (
                        f"Existing Inventory Id={existing_id} is not the approved new Id {approved_id}"
                    )
                elif existing_sku and normalize(existing_sku) != normalize(rule.target_qbo_sku):
                    status = "BLOCK_QBO_SKU_MISMATCH"
                    reason = f"Existing exact-name item uses SKU {existing_sku}"
                else:
                    status = "READY_EXISTING"
            elif normalized_type not in {"noninventory", "service"}:
                status = "BLOCK_INVENTORY_NAME_COLLISION"
                reason = f"Existing exact-name item is Type={item_type or 'Unknown'}"
            elif existing_sku and normalize(existing_sku) != normalize(rule.target_qbo_sku):
                status = "BLOCK_QBO_SKU_MISMATCH"
                reason = f"Existing exact-name item uses SKU {existing_sku}"
            elif not existing_sku:
                status = "UPDATE_SKU_PENDING_APPROVAL"
                reason = "Existing non-inventory item has a blank SKU"
            else:
                status = "READY_EXISTING"
        elif sku_matches:
            status = "BLOCK_QBO_SKU_COLLISION"
            reason = "Approved target SKU is already used by a different QBO item"
        elif re.sub(r"[\s_-]", "", rule.target_qbo_type.casefold()) == "inventory":
            status = "CREATE_INVENTORY_PENDING_APPROVAL"
            reason = "No exact QBO target exists; creation is a separate approved batch, not the sales uploader"
        else:
            status = "CREATE_NONINVENTORY_PENDING_APPROVAL"
            reason = "No exact QBO target exists; creation is not performed by this plan"

        plan.append(
            {
                "Target QBO Name": rule.target_qbo_name,
                "Target QBO SKU": rule.target_qbo_sku,
                "Desired Item Type": rule.target_qbo_type,
                "Existing QBO Name": clean(existing.get("Product/Service Name") or existing.get("Name")),
                "Existing QBO SKU": clean(existing.get("SKU")),
                "Existing Item Type": clean(existing.get("Item type") or existing.get("Type")),
                "Status": status,
                "Reason": reason,
                "Mapped EPOS Products": " | ".join(sorted({item.epos_name for item in rules})),
            }
        )
    return plan


def write_catalog_plan(path: str | Path, rows: list[dict[str, Any]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit EPOS sales against an approved Product Conversion List")
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--sales", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--qbo-products", type=Path)
    parser.add_argument("--catalog-out", type=Path)
    parser.add_argument("--require-stable-id", action="store_true")
    args = parser.parse_args()

    registry = ProductConversionRegistry.from_csv(
        args.mapping,
        allow_name_fallback=not args.require_stable_id,
    )
    sales = pd.read_csv(args.sales, low_memory=False)
    rows = audit_sales(sales, registry)
    write_audit(args.out, rows)
    blocked = sum(row["Status"] == "BLOCK" for row in rows)
    ready = sum(row["Status"] == "READY" for row in rows)
    print(f"Audit written: {args.out}")
    print(f"Unique product keys ready={ready}, blocked={blocked}")
    if bool(args.qbo_products) != bool(args.catalog_out):
        parser.error("--qbo-products and --catalog-out must be supplied together")
    if args.qbo_products and args.catalog_out:
        with args.qbo_products.open("r", encoding="utf-8-sig", newline="") as handle:
            qbo_rows = list(csv.DictReader(handle))
        catalog_rows = build_qbo_catalog_plan(registry, qbo_rows)
        write_catalog_plan(args.catalog_out, catalog_rows)
        catalog_counts = Counter(row["Status"] for row in catalog_rows)
        print(f"Catalogue plan written: {args.catalog_out}")
        print("Catalogue statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(catalog_counts.items())))


if __name__ == "__main__":
    main()

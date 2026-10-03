"""Read side of the "don't ask again" list (review_exclusions.csv). Local file only.

Writes always go through ``python -m code_scripts.akponora_ops.review_exclusions add|remove`` in a
background job (see ``attention_actions``); this module never changes the file.
"""
from __future__ import annotations

from pathlib import Path

from . import company_a_ops as ops

KIND_LABELS = {"product": "Product", "vendor": "Supplier", "bill": "Bill"}


def path() -> Path:
    return ops.state_root() / "mappings" / ops.COMPANY_KEY / "review_exclusions.csv"


def history_path() -> Path:
    return path().parent / "review_exclusions_history.csv"


def normalize(kind: str, key) -> str:
    """The key exactly as the tool stores it (EPOS id, normalized supplier name, PO ref)."""
    from code_scripts.akponora_ops import review_exclusions

    try:
        return review_exclusions.normalize_key(kind, key)
    except review_exclusions.ExclusionError:
        return ""


def rows(include_expired: bool = False) -> list[dict]:
    """Rows of the exclusions file, each with ``active`` and a plain ``kind_label``."""
    from code_scripts.akponora_ops import review_exclusions

    file = path()
    if not file.exists():
        return []
    if file.is_symlink() or not file.resolve().is_relative_to(ops.state_root().resolve()):
        raise ValueError("The exclusions file is outside the state directory")
    out = []
    for row in review_exclusions.read_rows(file):
        active = review_exclusions.is_active(row)
        if active or include_expired:
            out.append(dict(row, active=active, kind_label=KIND_LABELS.get(row["kind"], row["kind"])))
    return out


def active_keys(kind: str) -> set[str]:
    try:
        return {r["key"] for r in rows() if r["kind"] == kind}
    except (OSError, ValueError):
        return set()


def is_excluded(kind: str, key) -> bool:
    nkey = normalize(kind, key)
    return bool(nkey) and nkey in active_keys(kind)


def find(kind: str, key) -> dict | None:
    nkey = normalize(kind, key)
    try:
        return next((r for r in rows() if r["kind"] == kind and r["key"] == nkey), None)
    except (OSError, ValueError):
        return None
